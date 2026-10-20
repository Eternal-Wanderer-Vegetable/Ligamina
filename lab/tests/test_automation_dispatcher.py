from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from lab.automation.circuit_breaker import BackendCircuitBreaker, CircuitPolicy
from lab.automation.dispatcher import Dispatcher
from lab.automation.host_lock import HostLock
from lab.automation.reconcile import DockerReconciler, ReconciliationResult, reconcile_expired_attempts
from lab.queue import QueueError, StageQueue
from lab.workers import DockerWorker, WorkerSpec


class AutomationDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="ligamina-dispatch-test-")
        self.queue = StageQueue(Path(self.temporary.name) / "queue.sqlite3")
        self.queue.create_run("campaign", "snapshot", "a" * 64, {"schema_version": 1}, fencing_required=True)
        self.queue.enqueue_jobs("campaign", [{
            "unit_id": "unit-1", "registry_entry_id": "entry-1", "ecosystem": "nonebot",
            "platform": "linux", "profile_id": "profile", "stage": "probe", "case_id": "case",
            "payload": {},
        }], now=10)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_dispatcher_commits_with_claim_token_and_backend_result(self) -> None:
        now = 10.0
        breaker = BackendCircuitBreaker(clock=lambda: now)
        dispatcher = Dispatcher(
            self.queue, "campaign", "worker", lambda job, lost: {"status": "passed", "cleanup_confirmed": True},
            breaker, heartbeat_seconds=.01, lease_seconds=2, clock=lambda: now,
        )
        result = dispatcher.dispatch_one()
        self.assertTrue(result["committed"])
        self.assertEqual(self.queue.list_attempts("campaign")[0]["status"], "passed")
        self.assertEqual(breaker.state, "closed")

    def test_dispatcher_renews_lease_during_a_long_running_worker(self) -> None:
        now = 10.0
        breaker = BackendCircuitBreaker(clock=lambda: now)

        def execute(job, lost):
            time.sleep(.04)
            self.assertFalse(lost.is_set())
            return {"status": "passed", "cleanup_confirmed": True}

        dispatcher = Dispatcher(
            self.queue, "campaign", "worker", execute, breaker,
            heartbeat_seconds=.01, lease_seconds=.05, clock=lambda: now,
        )
        result = dispatcher.dispatch_one()
        self.assertTrue(result["committed"])
        self.assertGreaterEqual(sum(item["event_type"] == "lease_renewed" for item in self.queue.journal("campaign")), 1)

    def test_expired_fenced_lease_stays_interrupted_until_cleanup_evidence(self) -> None:
        claim = self.queue.claim("campaign", "same-owner", lease_seconds=1, now=10)
        self.assertIsNone(self.queue.claim("campaign", "same-owner", lease_seconds=1, now=12))
        resumed = self.queue.resume_run("campaign", now=13)
        self.assertTrue(resumed["reconciliation_required"])
        self.assertEqual(resumed["state"], "interrupted")
        self.queue.record_reconciliation("campaign", "unit-1", claim["attempt_id"], {
            "cleanup_confirmed": True, "resource_ids": [],
        }, now=14)
        self.queue.complete_reconciliation("campaign", now=15)
        self.assertEqual(self.queue.run("campaign")["state"], "running")

    def test_backend_breaker_opens_immediately_and_requires_three_sentinels(self) -> None:
        now = [0.0]
        breaker = BackendCircuitBreaker(
            CircuitPolicy(cooldown_seconds=60, half_open_passes=3), clock=lambda: now[0],
        )
        breaker.record(healthy=False, reason_code="docker_daemon_unavailable")
        self.assertEqual(breaker.state, "open")
        self.assertFalse(breaker.allow_dispatch())
        now[0] = 60
        self.assertEqual(breaker.state, "half_open")
        self.assertFalse(breaker.allow_dispatch())
        self.assertTrue(breaker.allow_dispatch(sentinel=True))
        for _ in range(2):
            breaker.record(healthy=True)
            self.assertEqual(breaker.state, "half_open")
        breaker.record(healthy=True)
        self.assertEqual(breaker.state, "closed")

    @unittest.skipIf(os.name == "nt", "Linux host-lock test; Windows execution is deferred")
    def test_host_lock_rejects_a_second_linux_controller(self) -> None:
        path = Path(self.temporary.name) / "controller.lock"
        first = HostLock(path)
        second = HostLock(path)
        first.acquire()
        try:
            with self.assertRaisesRegex(RuntimeError, "already held"):
                second.acquire()
        finally:
            first.release()
        second.acquire()
        second.release()

    def test_docker_reconciler_removes_only_exact_generation_labels(self) -> None:
        exact_labels = {
            "io.ligamina.campaign": "campaign", "io.ligamina.job": "job-id",
            "io.ligamina.attempt": "attempt-id", "io.ligamina.generation": "2",
        }
        replies = [
            CompletedProcess([], 0, b"container-1\n", b""),
            CompletedProcess([], 0, json.dumps([{"Id": "container-1", "Config": {"Labels": exact_labels}}]).encode(), b""),
            CompletedProcess([], 0, b"container-1\n", b""),
            CompletedProcess([], 0, b"", b""),
        ]
        with patch("lab.automation.reconcile.subprocess.run", side_effect=replies) as run:
            result = DockerReconciler("docker").reconcile(
                campaign_id="campaign", job_id="job-id", attempt_id="attempt-id", generation=2,
            )
        self.assertTrue(result.cleanup_confirmed)
        self.assertEqual(result.resource_ids, ("container-1",))
        self.assertEqual(run.call_args_list[2].args[0][-2:], ["--force", "container-1"])

    def test_docker_reconciler_fails_closed_when_label_identity_does_not_match(self) -> None:
        replies = [
            CompletedProcess([], 0, b"container-1\n", b""),
            CompletedProcess([], 0, b'[{"Id":"container-1","Config":{"Labels":{}}}]', b""),
        ]
        with patch("lab.automation.reconcile.subprocess.run", side_effect=replies) as run:
            result = DockerReconciler("docker").reconcile(
                campaign_id="campaign", job_id="job-id", attempt_id="attempt-id", generation=2,
            )
        self.assertFalse(result.cleanup_confirmed)
        self.assertEqual(result.reason_code, "resource_label_identity_mismatch")
        self.assertEqual(run.call_count, 2)

    def test_run_reconciliation_connects_exact_queue_attempt_and_preserves_quarantine(self) -> None:
        claim = self.queue.claim("campaign", "worker", lease_seconds=1, now=10)
        self.queue.resume_run("campaign", now=12)

        class FakeReconciler:
            def __init__(self):
                self.request = None

            def reconcile(self, **kwargs):
                self.request = kwargs
                return ReconciliationResult(False, (), (), "docker_api_unavailable")

        reconciler = FakeReconciler()
        outcome = reconcile_expired_attempts(self.queue, "campaign", reconciler, now=13)
        self.assertEqual(outcome["reconciled_attempts"], 1)
        self.assertEqual(reconciler.request["job_id"], claim["job_id"])
        with self.assertRaisesRegex(QueueError, "no cleanup reconciliation"):
            self.queue.complete_reconciliation("campaign", now=14)
        resumed = self.queue.resume_run("campaign", now=15)
        self.assertEqual(resumed["state"], "interrupted")

    def test_worker_spec_emits_generation_scoped_docker_labels(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ligamina-worker-input-") as directory:
            spec = WorkerSpec(
                "campaign", "entry", ("python", "-c", "pass"), Path(directory),
                attempt_id="attempt-token", generation=3, job_id="job-token",
            )
            with patch("lab.workers.linux.shutil.which", return_value="/usr/bin/docker"):
                worker = DockerWorker(
                    image_digest="registry.example/worker@sha256:" + "a" * 64,
                    repository_root=Path(__file__).resolve().parents[2],
                )
            command = worker.command(spec, worker_id="worker-test")
        labels = [command[index + 1] for index, item in enumerate(command[:-1]) if item == "--label"]
        self.assertEqual(labels, [
            "io.ligamina.campaign=campaign", "io.ligamina.job=job-token",
            "io.ligamina.attempt=attempt-token", "io.ligamina.generation=3",
        ])


if __name__ == "__main__":
    unittest.main()
