"""Docker resource reconciliation scoped to exact campaign/job/attempt labels."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any

from lab.queue import QueueError, StageQueue


@dataclass(frozen=True)
class ReconciliationResult:
    cleanup_confirmed: bool
    resource_ids: tuple[str, ...]
    removed_ids: tuple[str, ...]
    reason_code: str | None

    def as_evidence(self) -> dict[str, Any]:
        return {
            "cleanup_confirmed": self.cleanup_confirmed,
            "resource_ids": list(self.resource_ids),
            "removed_ids": list(self.removed_ids),
            "reason_code": self.reason_code,
            "source": "docker_label_reconciliation",
        }


class DockerReconciler:
    LABELS = {
        "campaign": "io.ligamina.campaign",
        "job": "io.ligamina.job",
        "attempt": "io.ligamina.attempt",
        "generation": "io.ligamina.generation",
    }

    def __init__(self, docker: str | None = None):
        self.docker = docker or shutil.which("docker")

    def reconcile(self, *, campaign_id: str, job_id: str, attempt_id: str, generation: int) -> ReconciliationResult:
        if not self.docker:
            return ReconciliationResult(False, (), (), "docker_cli_unavailable")
        if not campaign_id or not job_id or not attempt_id or generation < 1:
            raise ValueError("exact campaign, job, attempt and positive generation are required")
        filters = [
            f"label={self.LABELS['campaign']}={campaign_id}",
            f"label={self.LABELS['job']}={job_id}",
            f"label={self.LABELS['attempt']}={attempt_id}",
            f"label={self.LABELS['generation']}={generation}",
        ]
        try:
            listed = self._run(["ps", "--all", "--quiet", *sum((["--filter", item] for item in filters), [])])
            if listed.returncode != 0:
                return ReconciliationResult(False, (), (), "docker_api_unavailable")
            resource_ids = tuple(sorted(set(listed.stdout.decode("ascii", errors="strict").split())))
            if not resource_ids:
                return ReconciliationResult(True, (), (), None)
            inspected = self._run(["inspect", *resource_ids])
            if inspected.returncode != 0:
                return ReconciliationResult(False, resource_ids, (), "docker_api_unavailable")
            records = json.loads(inspected.stdout.decode("utf-8"))
            exact = set()
            for record in records:
                labels = record.get("Config", {}).get("Labels", {}) or {}
                expected = {
                    self.LABELS["campaign"]: campaign_id,
                    self.LABELS["job"]: job_id,
                    self.LABELS["attempt"]: attempt_id,
                    self.LABELS["generation"]: str(generation),
                }
                if all(labels.get(key) == value for key, value in expected.items()):
                    exact.add(record.get("Id"))
            targets = tuple(sorted(item for item in exact if item))
            if len(targets) != len(resource_ids):
                return ReconciliationResult(False, resource_ids, (), "resource_label_identity_mismatch")
            removed = self._run(["rm", "--force", *targets])
            if removed.returncode != 0:
                return ReconciliationResult(False, resource_ids, (), "docker_cleanup_failed")
            verify = self._run(["ps", "--all", "--quiet", *sum((["--filter", item] for item in filters), [])])
            if verify.returncode != 0:
                return ReconciliationResult(False, resource_ids, targets, "docker_api_unavailable")
            remaining = verify.stdout.decode("ascii", errors="strict").split()
            return ReconciliationResult(not remaining, resource_ids, targets, None if not remaining else "docker_cleanup_unconfirmed")
        except (OSError, subprocess.TimeoutExpired, UnicodeError, json.JSONDecodeError):
            return ReconciliationResult(False, (), (), "docker_api_unavailable")

    def _run(self, arguments: list[str]) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run([self.docker, *arguments], stdin=subprocess.DEVNULL, capture_output=True, timeout=15, check=False)


def reconcile_expired_attempts(queue: StageQueue, run_id: str, reconciler: DockerReconciler, *, now: float | None = None) -> dict[str, Any]:
    """Reconcile each lost fenced attempt; resume is deliberately separate and requires all cleanup true."""
    outcomes = []
    attempts = queue.list_attempts(run_id)
    for attempt in attempts:
        result = attempt.get("result") or {}
        if attempt["status"] != "unknown" or not result.get("requires_reconciliation"):
            continue
        if result.get("reconciliation") is not None:
            continue
        job = queue.get_job(run_id, attempt["unit_id"])
        evidence = reconciler.reconcile(
            campaign_id=run_id,
            job_id=job["job_id"],
            attempt_id=attempt["attempt_id"],
            generation=attempt["attempt_number"],
        ).as_evidence()
        outcomes.append(queue.record_reconciliation(
            run_id, attempt["unit_id"], attempt["attempt_id"], evidence, now=now,
        ))
    return {"run_id": run_id, "reconciled_attempts": len(outcomes), "outcomes": outcomes}


def resume_reconciled_run(
    queue: StageQueue,
    run_id: str,
    reconciler: DockerReconciler,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Mark expired fenced leases unknown, reconcile their exact resources, then reopen claims."""
    resumed = queue.resume_run(run_id, now=now)
    reconciliation = {"run_id": run_id, "reconciled_attempts": 0, "outcomes": []}
    if resumed["reconciliation_required"]:
        reconciliation = reconcile_expired_attempts(queue, run_id, reconciler, now=now)
        queue.complete_reconciliation(run_id, now=now)
    return {"resume": resumed, "reconciliation": reconciliation, "run": queue.run(run_id)}
