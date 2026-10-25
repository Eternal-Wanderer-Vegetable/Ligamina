from __future__ import annotations

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from lab.catalog_run import CatalogRunError, STAGES, build_stage_jobs, partition_units
from lab.queue import QueueError, StageQueue


class StageQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="ligamina-queue-test-")
        self.path = Path(self.temporary.name) / "queue.sqlite3"
        self.queue = StageQueue(self.path)
        self.queue.create_run("run-1", "snapshot-1", "a" * 64, {"schema_version": 1, "options": {}})
        self.unit = {
            "unit_id": "entry-1|linux|catalog",
            "registry_entry_id": "entry-1",
            "ecosystem": "nonebot",
            "platform": "linux",
            "profile_id": "nonebot-profile-linux",
            "case_id": "catalog",
            "artifact_sha256": "b" * 64,
        }
        self.jobs = build_stage_jobs(self.unit)
        self.queue.enqueue_jobs("run-1", self.jobs, now=100)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def claim(self, worker="worker-1", now=100, lease=10):
        return self.queue.claim("run-1", worker, lease_seconds=lease, now=now)

    def test_stage_dependencies_gate_claims_and_survive_reopen(self) -> None:
        first = self.claim()
        self.assertEqual(first["stage"], "prepare")
        self.assertEqual(first["result_id"], self.queue.get_job("run-1", first["unit_id"])["result_id"])
        self.assertIsNone(self.claim(worker="worker-2", now=101))
        self.queue.finish(first["job_id"], "worker-1", {"status": "passed"}, now=102)

        reopened = StageQueue(self.path)
        next_job = reopened.claim("run-1", "worker-2", now=103)
        self.assertEqual(next_job["stage"], "install")
        self.assertEqual(next_job["attempts_used"], 1)
        self.assertEqual(reopened.run("run-1")["state"], "running")

    def test_failed_stage_skips_dependents_but_always_runs_cleanup(self) -> None:
        first = self.claim()
        self.queue.finish(first["job_id"], "worker-1", {"status": "failed", "reason_code": "prepare_failed"}, now=101)
        cleanup = self.claim(worker="cleaner", now=102)
        self.assertEqual(cleanup["stage"], "cleanup")
        statuses = {job["stage"]: job["status"] for job in self.queue.list_jobs("run-1")}
        self.assertEqual(statuses["prepare"], "failed")
        for stage in ("install", "native", "federated", "compare"):
            self.assertEqual(statuses[stage], "skipped")
            self.assertEqual(self.queue.get_job("run-1", f"{self.unit['unit_id']}|{stage}")["result"]["reason_code"], "upstream_stage_not_passed")
        self.queue.finish(cleanup["job_id"], "cleaner", {"status": "passed", "cleanup": {"remaining_pids": []}}, now=103)
        self.assertEqual(self.queue.run("run-1")["state"], "completed")

    def test_interruption_and_worker_loss_preserve_unknown_attempt_before_retry(self) -> None:
        first = self.claim(worker="old-worker", now=100, lease=5)
        self.queue.interrupt_run("run-1", now=101)
        with self.assertRaises(QueueError):
            self.claim(worker="old-worker", now=102)

        reopened = StageQueue(self.path)
        resumed = reopened.resume_run("run-1", now=106)
        self.assertEqual(resumed["expired_leases_recovered"], 1)
        retried = reopened.claim("run-1", "new-worker", now=106, lease_seconds=10)
        self.assertEqual(retried["stage"], "prepare")
        self.assertEqual(retried["attempts_used"], 2)
        with self.assertRaises(QueueError):
            reopened.finish(first["job_id"], "old-worker", {"status": "passed"}, now=107)
        reopened.finish(retried["job_id"], "new-worker", {"status": "passed"}, now=107)
        attempts = reopened.list_attempts("run-1")
        self.assertEqual([attempt["status"] for attempt in attempts], ["unknown", "passed"])
        self.assertEqual(attempts[0]["result"]["reason_code"], "worker_lost")
        self.assertEqual(reopened.journal("run-1")[-1]["event_type"], "attempt_finished")

    def test_unknown_attempt_is_retained_when_retry_succeeds(self) -> None:
        first = self.claim(now=100)
        artifacts = [{"path": "attempts/prepare.log", "sha256": "e" * 64}]
        first_result = self.queue.finish(
            first["job_id"], "worker-1",
            {"status": "unknown", "reason_code": "worker_timeout", "artifacts": artifacts},
            retryable=True,
            now=101,
        )
        self.assertTrue(first_result["retry_queued"])
        second = self.claim(worker="worker-2", now=102)
        self.assertEqual(second["stage"], "prepare")
        self.assertEqual(second["attempts_used"], 2)
        self.queue.finish(second["job_id"], "worker-2", {"status": "passed"}, now=103)
        attempts = self.queue.list_attempts("run-1")
        self.assertEqual([attempt["status"] for attempt in attempts], ["unknown", "passed"])
        self.assertEqual(attempts[0]["result"]["artifacts"], artifacts)
        self.assertEqual(self.queue.get_job("run-1", first["unit_id"])["status"], "passed")

    def test_fenced_run_requires_reconciliation_and_rejects_stale_same_owner_token(self) -> None:
        path = Path(self.temporary.name) / "fenced.sqlite3"
        queue = StageQueue(path)
        queue.create_run("fenced", "snapshot-1", "a" * 64, {"schema_version": 1}, fencing_required=True)
        job = {
            "unit_id": "fenced-unit", "registry_entry_id": "entry-1", "ecosystem": "nonebot",
            "platform": "linux", "profile_id": "profile-linux", "stage": "probe", "case_id": "case-1",
            "payload": {}, "max_attempts": 2,
        }
        queue.enqueue_jobs("fenced", [job], now=100)
        first = queue.claim("fenced", "reused-owner", lease_seconds=5, now=100)
        self.assertEqual(first["generation"], 1)
        self.assertTrue(first["attempt_id"])
        resumed = queue.resume_run("fenced", now=106)
        self.assertTrue(resumed["reconciliation_required"])
        self.assertEqual(queue.run("fenced")["state"], "interrupted")
        with self.assertRaisesRegex(QueueError, "resumed before workers"):
            queue.claim("fenced", "reused-owner", now=107)
        with self.assertRaisesRegex(QueueError, "cleanup reconciliation"):
            queue.complete_reconciliation("fenced", now=107)

        queue.record_reconciliation(
            "fenced", "fenced-unit", first["attempt_id"],
            {"cleanup_confirmed": True, "resource_ids": [], "source": "docker_label_scan"}, now=108,
        )
        queue.complete_reconciliation("fenced", now=109)
        second = queue.claim("fenced", "reused-owner", lease_seconds=10, now=110)
        self.assertEqual(second["generation"], 2)
        self.assertNotEqual(first["attempt_id"], second["attempt_id"])
        with self.assertRaisesRegex(QueueError, "fencing token"):
            queue.heartbeat(second["job_id"], "reused-owner", attempt_id=first["attempt_id"], now=111)
        with self.assertRaisesRegex(QueueError, "fencing token"):
            queue.finish(second["job_id"], "reused-owner", {"status": "passed"}, attempt_id=first["attempt_id"], now=111)
        queue.finish(second["job_id"], "reused-owner", {"status": "passed"}, attempt_id=second["attempt_id"], now=112)
        attempts = queue.list_attempts("fenced")
        self.assertEqual([attempt["status"] for attempt in attempts], ["unknown", "passed"])
        self.assertTrue(attempts[0]["result"]["reconciliation"]["cleanup_confirmed"])

    def test_fenced_cleanup_unknown_is_quarantined_without_retry(self) -> None:
        path = Path(self.temporary.name) / "fenced-quarantine.sqlite3"
        queue = StageQueue(path)
        queue.create_run("quarantine", "snapshot-1", "b" * 64, {"schema_version": 1}, fencing_required=True)
        job = {
            "unit_id": "quarantine-unit", "registry_entry_id": "entry-1", "ecosystem": "nonebot",
            "platform": "linux", "profile_id": "profile-linux", "stage": "probe", "case_id": "case-1",
            "payload": {}, "max_attempts": 3,
        }
        queue.enqueue_jobs("quarantine", [job], now=10)
        first = queue.claim("quarantine", "worker", lease_seconds=1, now=10)
        queue.resume_run("quarantine", now=12)
        result = queue.record_reconciliation(
            "quarantine", "quarantine-unit", first["attempt_id"],
            {"cleanup_confirmed": False, "reason_code": "docker_api_unavailable"}, now=13,
        )
        self.assertFalse(result["retry_queued"])
        with self.assertRaisesRegex(QueueError, "no cleanup reconciliation"):
            queue.complete_reconciliation("quarantine", now=14)
        self.assertEqual(queue.get_job("quarantine", "quarantine-unit")["status"], "unknown")
        with self.assertRaisesRegex(QueueError, "resumed before workers"):
            queue.claim("quarantine", "worker", now=15)

    def test_interruption_at_each_stage_reopens_with_unknown_attempt_and_retryable_job(self) -> None:
        for target_stage in STAGES:
            with self.subTest(stage=target_stage):
                path = Path(self.temporary.name) / f"restart-{target_stage}.sqlite3"
                queue = StageQueue(path)
                queue.create_run(target_stage, "snapshot-1", "f" * 64, {"schema_version": 1})
                queue.enqueue_jobs(target_stage, self.jobs, now=100)
                now = 100
                while True:
                    claimed = queue.claim(target_stage, f"worker-{target_stage}", lease_seconds=5, now=now)
                    self.assertIsNotNone(claimed)
                    if claimed["stage"] == target_stage:
                        break
                    queue.finish(claimed["job_id"], f"worker-{target_stage}", {"status": "passed"}, now=now + 1)
                    now += 1

                queue.interrupt_run(target_stage, now=now + 1)
                reopened = StageQueue(path)
                resumed = reopened.resume_run(target_stage, now=now + 7)
                self.assertEqual(resumed["expired_leases_recovered"], 1)
                job = reopened.get_job(target_stage, claimed["unit_id"])
                self.assertEqual(job["status"], "queued")
                attempts = reopened.list_attempts(target_stage)
                target_attempts = [attempt for attempt in attempts if attempt["unit_id"] == claimed["unit_id"]]
                self.assertEqual(target_attempts[0]["status"], "unknown")
                self.assertEqual(target_attempts[0]["result"]["reason_code"], "worker_lost")

    def test_expired_final_attempt_becomes_unknown_and_is_not_retried(self) -> None:
        jobs = build_stage_jobs(self.unit, max_attempts=1)
        path = Path(self.temporary.name) / "single-attempt.sqlite3"
        queue = StageQueue(path)
        queue.create_run("single", "snapshot-1", "c" * 64, {"schema_version": 1})
        queue.enqueue_jobs("single", jobs, now=10)
        first = queue.claim("single", "worker", lease_seconds=2, now=10)
        queue.resume_run("single", now=13)
        self.assertEqual(queue.get_job("single", first["unit_id"])["status"], "unknown")
        self.assertEqual(queue.list_attempts("single")[0]["status"], "unknown")
        cleanup = queue.claim("single", "cleaner", now=13)
        self.assertEqual(cleanup["stage"], "cleanup")

    def test_queue_claim_is_atomic_across_concurrent_workers(self) -> None:
        path = Path(self.temporary.name) / "race.sqlite3"
        queue = StageQueue(path)
        queue.create_run("race", "snapshot-1", "d" * 64, {"schema_version": 1})
        simple = {
            "unit_id": "one-job", "registry_entry_id": "entry-1", "ecosystem": "koishi",
            "platform": "windows", "profile_id": "koishi-win", "stage": "prepare", "case_id": "load",
            "payload": {},
        }
        queue.enqueue_jobs("race", [simple])
        barrier = threading.Barrier(2)

        def claim(worker: str):
            barrier.wait()
            return queue.claim("race", worker)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, ("w1", "w2")))
        claimed = [result for result in results if result is not None]
        self.assertEqual(len(claimed), 1)
        self.assertEqual(queue.counts("race")["leased"], 1)

    def test_large_independent_queue_drains_with_two_concurrent_workers(self) -> None:
        path = Path(self.temporary.name) / "large-concurrent.sqlite3"
        queue = StageQueue(path)
        queue.create_run("large-race", "snapshot-1", "e" * 64, {"schema_version": 1})
        jobs = []
        for index in range(64):
            unit = {
                **self.unit,
                "unit_id": f"entry-{index:03d}|linux|catalog",
                "registry_entry_id": f"entry-{index:03d}",
            }
            jobs.extend(build_stage_jobs(unit))
        queue.enqueue_jobs("large-race", jobs, now=100)
        first_claims = threading.Barrier(2)

        def drain(worker_id: str) -> None:
            synchronize_first_pair = True
            while True:
                # This test stresses concurrent SQLite claims; keep lease expiry outside
                # slow CI and filesystem windows so it does not become a timing test.
                job = queue.claim("large-race", worker_id, lease_seconds=300)
                if job is None:
                    return
                if synchronize_first_pair:
                    first_claims.wait(timeout=10)
                    synchronize_first_pair = False
                queue.finish(job["job_id"], worker_id, {"status": "passed"}, attempt_id=job["attempt_id"])

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(drain, ("large-w1", "large-w2")))

        counts = queue.counts("large-race")
        self.assertEqual(counts["passed"], len(jobs))
        self.assertEqual(counts["leased"], 0)
        self.assertEqual(counts["queued"], 0)
        self.assertEqual(queue.run("large-race")["state"], "completed")
        attempts = queue.list_attempts("large-race")
        self.assertEqual({attempt["worker_id"] for attempt in attempts}, {"large-w1", "large-w2"})

    def test_enqueue_is_idempotent_but_conflicting_identity_is_rejected(self) -> None:
        repeated = self.queue.enqueue_jobs("run-1", self.jobs, now=101)
        self.assertEqual(repeated, {"created": 0, "existing": 6})
        changed = [dict(job) for job in self.jobs]
        changed[0] = {**changed[0], "payload": {"changed": True}}
        with self.assertRaises(QueueError):
            self.queue.enqueue_jobs("run-1", changed, now=101)
        changed_dependencies = [dict(job) for job in self.jobs]
        changed_dependencies[1] = {**changed_dependencies[1], "dependencies": []}
        with self.assertRaises(QueueError):
            self.queue.enqueue_jobs("run-1", changed_dependencies, now=101)

    def test_shards_are_a_stable_disjoint_partition_and_reject_duplicate_units(self) -> None:
        units = [{"unit_id": f"entry-{index}|linux"} for index in range(37)]
        shards = [partition_units(units, index, 5) for index in range(5)]
        memberships = [{unit["unit_id"] for unit in shard} for shard in shards]
        self.assertTrue(all(memberships[left].isdisjoint(memberships[right]) for left in range(5) for right in range(left + 1, 5)))
        self.assertEqual(set.union(*memberships), {unit["unit_id"] for unit in units})
        self.assertEqual(partition_units(units, 2, 5), partition_units(units, 2, 5))
        with self.assertRaises(CatalogRunError):
            partition_units([units[0], units[0]], 0, 1)

    def test_stage_graph_installs_once_runs_both_paths_then_compares_and_cleans(self) -> None:
        jobs = {job["stage"]: job for job in self.jobs}
        self.assertEqual(jobs["install"]["dependencies"], [jobs["prepare"]["unit_id"]])
        self.assertEqual(jobs["native"]["dependencies"], [jobs["install"]["unit_id"]])
        self.assertEqual(jobs["federated"]["dependencies"], [jobs["install"]["unit_id"]])
        self.assertEqual(set(jobs["compare"]["dependencies"]), {jobs["native"]["unit_id"], jobs["federated"]["unit_id"]})
        self.assertEqual(set(jobs["cleanup"]["dependencies"]), {jobs[stage]["unit_id"] for stage in ("install", "native", "federated", "compare")})
        self.assertTrue(jobs["cleanup"]["always_run"])


if __name__ == "__main__":
    unittest.main()
