"""Execute the P4-02 Linux suite through the durable fenced stage queue."""

from __future__ import annotations

import json
import platform
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lab.automation.behavior_v2 import (
    P402_MATRIX_PATH,
    P402_SUITE_PATH,
    BehaviorV2Error,
    _read,
    validate_phase4_suite,
)
from lab.automation.circuit_breaker import BackendCircuitBreaker
from lab.automation.dispatcher import dispatch_until_idle
from lab.automation.execution import run_behavior_pair
from lab.automation.health import collect_linux_health
from lab.automation.phase4_campaign import (
    _fixture_context,
    _phase4_capability_results,
    _read_json,
    _runtime_probe,
    _sha256_bytes,
    _sha256_file,
    _write_with_hash,
    prepare_bundles,
)
from lab.automation.phase4_capability_probes import validate_service_probe_report
from lab.queue import QueueError, StageQueue
from lab.automation.selection import object_sha256
from lab.workers import DockerWorker
from lab.workers.base import ensure_external_directory

ROOT = Path(__file__).resolve().parents[2]
DENOMINATOR = {"entries": 20, "positions": 60, "paired_attempt_slots": 180, "side_execution_maximum": 360}
_PINNED_IMAGE = re.compile(r"^[^\s@]+@sha256:[a-f0-9]{64}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _write_new_report(path: Path, report: dict[str, Any]) -> str:
    path = path.expanduser().absolute()
    if path.exists() or path.with_suffix(path.suffix + ".sha256").exists():
        raise FileExistsError(f"refusing to overwrite an existing P4-02 report: {path}")
    return _write_with_hash(path, report)


def _classify_pair(pair: dict[str, Any]) -> tuple[str, str, bool, list[str]]:
    worker_rows = [
        pair.get(side, {}).get("outer_worker", {})
        for side in ("native", "federated")
        if isinstance(pair.get(side), dict)
    ]
    cleanup_confirmed = len(worker_rows) == 2 and all(row.get("cleanup_confirmed") is True for row in worker_rows)
    worker_ids = [row.get("worker_id") for row in worker_rows if isinstance(row.get("worker_id"), str)]
    reason_codes = sorted({
        str(row.get("reason_code"))
        for row in worker_rows
        if isinstance(row.get("reason_code"), str) and row.get("reason_code")
    })
    if not cleanup_confirmed:
        return "unknown", "worker_cleanup_unconfirmed", False, worker_ids
    failures = [
        side.get("worker_failure", {})
        for name in ("native", "federated")
        if isinstance((side := pair.get(name)), dict)
    ]
    if any(row.get("status") in {"unknown", "timed_out", "lost"} for row in failures):
        return "unknown", "worker_execution_unknown", True, worker_ids
    plugin_load_failed = any(
        isinstance(pair.get(side), dict)
        and pair[side].get("status") == "failed"
        and any(
            phrase in str(pair[side].get("stderr_summary", "")).casefold()
            for phrase in ("could not load plugin", "plugin could not be loaded")
        )
        for side in ("native", "federated")
    )
    if plugin_load_failed:
        return "failed", "plugin_load_failed", True, worker_ids
    if pair.get("comparison", {}).get("status") == "passed":
        return "passed", "behavior_oracles_passed", True, worker_ids
    issues = pair.get("comparison", {}).get("issues", [])
    harness_codes = {"run_not_completed", "identity_mismatch", "observations_missing", "cleanup_unconfirmed", "native_is_not_stock", "core_host_route_unproven"}
    reason = "behavior_oracle_or_pair_mismatch"
    if any(item.get("code") in harness_codes for item in issues if isinstance(item, dict)):
        reason = "host_or_harness_contract_failed"
    return "failed", reason, True, worker_ids


def _slot_unit(case_id: str, attempt_number: int) -> str:
    return f"{case_id}--attempt-{attempt_number:02d}"


def _failure_domain_counts(jobs: dict[str, dict[str, Any]]) -> dict[str, int]:
    domains = ("plugin_compatibility", "plugin_behavior", "host_or_harness")
    return {
        domain: sum(1 for row in jobs.values() if (row.get("result") or {}).get("failure_domain") == domain)
        for domain in domains
    }


def _validate_runtime_image_reference(runtime_image: str, admission: dict[str, Any]) -> None:
    """Keep the admitted Docker backend image separate from the P4 host runtime image."""
    admitted_image = admission.get("facts", {}).get("image", {}).get("locked_reference")
    if not isinstance(admitted_image, str) or not _PINNED_IMAGE.fullmatch(admitted_image):
        raise ValueError("P4-02 admission must bind the generic Linux Worker image by digest")
    if not _PINNED_IMAGE.fullmatch(runtime_image):
        raise ValueError("P4-02 behavior runtime image must be pinned by a sha256 digest")


def execute_phase4_p402_campaign(
    *,
    admission_path: Path,
    runtime_image: str,
    asset_root: Path,
    bundle_root: Path,
    queue_root: Path,
    service_probe_report_path: Path,
    output_path: Path,
    campaign_id: str | None = None,
    worker_count: int = 1,
) -> dict[str, Any]:
    if platform.system() != "Linux":
        raise RuntimeError("P4-02 must be orchestrated inside Linux so the active SQLite queue stays on the Linux filesystem")
    if worker_count < 1 or worker_count > 2:
        raise ValueError("P4-02 worker_count must remain between one and two")
    service_evidence = validate_service_probe_report(service_probe_report_path)
    admission_path = admission_path.expanduser().resolve(strict=True)
    admission = _read_json(admission_path)
    collected = int(admission.get("collected_at_epoch", 0))
    if (
        admission.get("status") != "admitted"
        or admission.get("platform") != "linux"
        or admission.get("capabilities", {}).get("claim_allowed") is not True
        or admission.get("capabilities", {}).get("network_disabled") is not True
        or admission.get("capabilities", {}).get("cleanup_evidence_required") is not True
        or time.time() - collected > 1800
        or time.time() < collected
    ):
        raise RuntimeError("P4-02 requires a successful Linux admission collected within the last 30 minutes")
    _validate_runtime_image_reference(runtime_image, admission)
    probe_path = Path(admission.get("facts", {}).get("probe", {}).get("report_path", "")).expanduser().resolve(strict=True)
    fresh_health = collect_linux_health(probe_report_path=probe_path)
    if fresh_health.get("status") != "admitted" or fresh_health.get("capabilities", {}).get("claim_allowed") is not True:
        raise RuntimeError("fresh Linux Worker re-admission is blocked: " + ",".join(fresh_health.get("reason_codes", [])))

    suite_path = P402_SUITE_PATH
    matrix_path = P402_MATRIX_PATH
    validation = validate_phase4_suite(suite_path, matrix_path, verify_artifacts=True)
    suite = _read(suite_path)
    if validation["executable_cases"] != 12 or validation["blocked_cases"] != 48:
        raise BehaviorV2Error("P4-02 fixed-four campaign expected 12 executable and 48 blocked cases")
    output_path = output_path.expanduser().absolute()
    if output_path.exists() or output_path.with_suffix(output_path.suffix + ".sha256").exists():
        raise FileExistsError(f"refusing to overwrite an existing P4-02 report: {output_path}")
    queue_root = queue_root.expanduser().absolute()
    queue_root.mkdir(parents=True, exist_ok=True)
    queue_root = ensure_external_directory(queue_root, forbidden_root=ROOT)
    if queue_root.as_posix().startswith("/mnt/"):
        raise ValueError("P4-02 activity queue must stay on the WSL/Linux filesystem, not a mounted Windows drive")
    campaign_id = campaign_id or f"p402-{int(time.time())}"
    if not campaign_id.startswith("p402-") or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for character in campaign_id):
        raise ValueError("P4-02 campaign id must use the p402- prefix and safe ASCII characters")
    queue_path = queue_root / f"{campaign_id}.sqlite3"
    if queue_path.exists() or Path(str(queue_path) + "-wal").exists() or Path(str(queue_path) + "-shm").exists():
        raise FileExistsError(f"refusing to reuse an existing P4-02 queue: {queue_path}")
    bundles = prepare_bundles(asset_root, bundle_root, suite)
    worker = DockerWorker(image_digest=runtime_image, repository_root=ROOT)
    campaign_started_at = _now()
    runtime_probe = _runtime_probe(worker, Path(bundle_root).expanduser().absolute(), run_id=f"{campaign_id}-runtime-probe")
    if runtime_probe.get("status") != "passed":
        raise RuntimeError("P4-02 pinned runtime probe did not pass; no behavior job was dispatched")

    suite_sha = _sha256_file(suite_path)
    matrix_sha = _sha256_file(matrix_path)
    admission_sha = _sha256_file(admission_path)
    bundle_by_entry_id = {row["registry_entry_id"]: (Path(row["path"]), row["asset_report"]) for row in bundles["entries"]}
    entry_by_id = {entry["registry_entry_id"]: entry for entry in suite["entries"]}
    position_records: list[dict[str, Any]] = []
    jobs: list[dict[str, Any]] = []
    for entry in suite["entries"]:
        for case in entry["cases"]:
            executable = case.get("migration", {}).get("status") == "executable"
            position_id = f"{entry['registry_entry_id']}::{case['case_id']}"
            position_records.append({
                "position_id": position_id,
                "registry_entry_id": entry["registry_entry_id"],
                "case_id": case["case_id"],
                "case_class": case["class"],
                "migration_status": case["migration"]["status"],
                "reason_code": case["migration"].get("reason_code"),
                "executable": executable,
                "attempt_slots": [],
            })
            if not executable:
                continue
            lock = entry["dependency_locks"]["linux-x86_64"]
            bundle, asset_report = bundle_by_entry_id[entry["registry_entry_id"]]
            for attempt_number in range(1, 4):
                unit_id = _slot_unit(case["case_id"], attempt_number)
                jobs.append({
                    "unit_id": unit_id,
                    "registry_entry_id": entry["registry_entry_id"],
                    "ecosystem": entry["ecosystem"],
                    "platform": "linux-x86_64",
                    "profile_id": lock["profile_id"],
                    "stage": "phase4_p402_paired_behavior_attempt",
                    "case_id": case["case_id"],
                    "max_attempts": 1,
                    "payload": {
                        "position_id": position_id,
                        "attempt_number": attempt_number,
                        "entry": entry,
                        "case": case,
                        "bundle_path": str(bundle),
                        "asset_report": asset_report,
                    },
                })

    queue_manifest = {
        "kind": "phase4_p402_fenced_queue_manifest",
        "campaign_id": campaign_id,
        "suite_id": suite["suite_id"],
        "suite_sha256": suite_sha,
        "migration_matrix_sha256": matrix_sha,
        "admission_sha256": admission_sha,
        "admission_fingerprint": admission["environment_fingerprint"],
        "fresh_health_fingerprint": fresh_health["environment_fingerprint"],
        "runtime_image": runtime_image,
        "worker_count": worker_count,
        "denominator": DENOMINATOR,
        "positions": [
            {
                "position_id": row["position_id"],
                "registry_entry_id": row["registry_entry_id"],
                "case_id": row["case_id"],
                "migration_status": row["migration_status"],
                "reason_code": row["reason_code"],
                "planned_attempt_slots": [
                    {
                        "slot_id": _slot_unit(row["case_id"], number),
                        "attempt_number": number,
                        "planned_dispatch": row["executable"],
                        "reason_code": None if row["executable"] else row["reason_code"],
                    }
                    for number in range(1, 4)
                ],
            }
            for row in position_records
        ],
        "queued_job_count": len(jobs),
        "blocked_case_count": validation["blocked_cases"],
        "attempts_per_position": 3,
    }
    queue_manifest_sha = object_sha256(queue_manifest)
    queue = StageQueue(queue_path)
    queue.create_run(campaign_id, suite["catalog_snapshot_id"], queue_manifest_sha, queue_manifest, fencing_required=True)
    enqueued = queue.enqueue_jobs(campaign_id, jobs)
    if enqueued["created"] != 36 or enqueued["existing"] != 0:
        raise QueueError("P4-02 queue must contain exactly 36 fresh paired-attempt jobs")

    side_module = ROOT / "lab/automation/phase4_docker_side.py"

    def execute(job: dict[str, Any], lease_lost: Any) -> dict[str, Any]:
        if lease_lost.is_set():
            return {"status": "unknown", "reason_code": "lease_lost_before_worker_start", "cleanup_confirmed": True}
        payload = job["payload"]
        entry = payload["entry"]
        case = payload["case"]
        bundle = Path(payload["bundle_path"])
        queue_attempt_id = job["attempt_id"]
        generation = int(job["generation"])
        common = [
            str(__import__("sys").executable), str(side_module),
            "--image", runtime_image, "--bundle", str(bundle),
            "--run-id", campaign_id, "--entry-id", entry["registry_entry_id"],
            "--attempt-id", queue_attempt_id, "--generation", str(generation),
        ]
        pair = run_behavior_pair(
            entry,
            case,
            "linux-x86_64",
            [*common, "--side", "native"],
            [*common, "--side", "federated"],
            fixture_context=_fixture_context(entry, case, bundle, payload["asset_report"]),
            timeout_seconds=240,
        )
        status, reason_code, cleanup_confirmed, worker_ids = _classify_pair(pair)
        return {
            "status": status,
            "reason_code": reason_code,
            "cleanup_confirmed": cleanup_confirmed,
            "retryable": False,
            "paired_attempt": pair,
            "position_id": payload["position_id"],
            "attempt_number": payload["attempt_number"],
            "queue_attempt_id": queue_attempt_id,
            "worker_ids": worker_ids,
            "failure_domain": (
                "plugin_compatibility" if status == "failed" and reason_code == "plugin_load_failed"
                else "plugin_behavior" if status == "failed" and reason_code == "behavior_oracle_or_pair_mismatch"
                else "host_or_harness" if status == "failed" else None
            ),
        }

    breaker = BackendCircuitBreaker(clock=time.time)
    infrastructure_stopped = dispatch_until_idle(
        queue, campaign_id, f"{campaign_id}-dispatcher", execute, breaker,
        worker_count=worker_count, heartbeat_seconds=20, lease_seconds=300,
    )
    queue_counts = queue.counts(campaign_id)
    queue_state = queue.run(campaign_id)["state"]
    queue_jobs = {row["unit_id"]: row for row in queue.list_jobs(campaign_id)}
    queue_attempts = queue.list_attempts(campaign_id)
    queue_journal = queue.journal(campaign_id)
    with queue._connect() as connection:
        quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    if quick_check != "ok":
        raise QueueError(f"P4-02 SQLite queue quick_check failed: {quick_check}")
    queue_sha = _sha256_file(queue_path)

    attempt_results: dict[str, dict[str, Any]] = {}
    observed_pairs: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for job in queue_jobs.values():
        result = job.get("result")
        attempt_number = int((job.get("payload") or {}).get("attempt_number", 0))
        case_id = job["case_id"]
        if not isinstance(result, dict) or attempt_number not in (1, 2, 3):
            continue
        slot_id = _slot_unit(case_id, attempt_number)
        attempt_results[slot_id] = {"job": job, "result": result}
        pair = result.get("paired_attempt")
        if isinstance(pair, dict):
            case = (job["payload"])["case"]
            observed_pairs.append((job.get("attempt_id", slot_id), case, pair))
    capabilities = _phase4_capability_results(observed_pairs, service_evidence, expected_side_runs=72)
    capability_probes = {key: value for key, value in capabilities.items() if key != "side_run_evidence"}

    slots_by_position = {row["position_id"]: row for row in position_records}
    slot_counts = {key: 0 for key in ("passed", "failed", "unknown", "skipped", "not_dispatched")}
    for position in position_records:
        entry = entry_by_id[position["registry_entry_id"]]
        case = next(row for row in entry["cases"] if row["case_id"] == position["case_id"])
        for attempt_number in range(1, 4):
            slot_id = _slot_unit(case["case_id"], attempt_number)
            result_record = attempt_results.get(slot_id)
            if not position["executable"]:
                slot = {
                    "slot_id": slot_id,
                    "attempt_number": attempt_number,
                    "status": "not_dispatched",
                    "reason_code": position["reason_code"] or "case_migration_blocked",
                    "dispatch_state": "not_dispatched",
                    "cleanup": "not_applicable",
                    "queue_stage_job_id": None,
                    "queue_attempt_id": None,
                }
                slot_counts["not_dispatched"] += 1
            elif result_record is None:
                job = queue_jobs.get(slot_id)
                slot = {
                    "slot_id": slot_id,
                    "attempt_number": attempt_number,
                    "status": "not_dispatched",
                    "reason_code": "campaign_stopped_before_dispatch" if infrastructure_stopped or (job and job["status"] in {"queued", "leased"}) else "queue_result_missing",
                    "dispatch_state": "not_dispatched",
                    "cleanup": "not_applicable",
                    "queue_stage_job_id": job.get("job_id") if job else None,
                    "queue_attempt_id": None,
                }
                slot_counts["not_dispatched"] += 1
            else:
                job = result_record["job"]
                result = result_record["result"]
                status = result.get("status", job.get("status", "unknown"))
                status = status if status in {"passed", "failed", "unknown", "skipped"} else "unknown"
                pair = result.get("paired_attempt")
                slot = {
                    "slot_id": slot_id,
                    "attempt_number": attempt_number,
                    "status": status,
                    "reason_code": result.get("reason_code"),
                    "dispatch_state": "dispatched",
                    "cleanup": "confirmed" if result.get("cleanup_confirmed") is True else "unconfirmed",
                    "queue_stage_job_id": job["job_id"],
                    "queue_attempt_id": job.get("attempts_used") and next((row["attempt_id"] for row in queue_attempts if row["unit_id"] == job["unit_id"]), None),
                    "paired_attempt": pair,
                    "worker_ids": result.get("worker_ids", []),
                    "failure_domain": result.get("failure_domain"),
                }
                slot_counts[status] += 1
            position["attempt_slots"].append(slot)
        states = [row["status"] for row in position["attempt_slots"]]
        if not position["executable"]:
            position["status"] = "skipped"
        elif all(state == "passed" for state in states):
            position["status"] = "passed"
        elif "failed" in states:
            position["status"] = "failed"
        elif "unknown" in states:
            position["status"] = "unknown"
        elif all(state == "not_dispatched" for state in states):
            position["status"] = "not_dispatched"
        else:
            position["status"] = "incomplete"

    observed_workers = [
        worker_id
        for position in position_records
        for slot in position["attempt_slots"]
        for worker_id in slot.get("worker_ids", [])
    ]
    unique_workers = len(set(observed_workers))
    cleanup_unconfirmed = sum(
        1
        for _attempt_id, _case, pair in observed_pairs
        for side in ("native", "federated")
        if pair.get(side, {}).get("outer_worker", {}).get("cleanup_confirmed") is not True
    )
    fixed_positions = [row for row in position_records if row["executable"]]
    fixed_four_pass = len(fixed_positions) == 12 and all(row["status"] == "passed" for row in fixed_positions)
    capabilities_complete = all(record.get("status") == "passed" for record in capability_probes.values())
    all_positions_passed = len(position_records) == 60 and all(row["status"] == "passed" for row in position_records)
    behavior_status = "passed" if all_positions_passed and slot_counts["unknown"] == 0 and slot_counts["not_dispatched"] == 0 else ("failed" if slot_counts["failed"] else "blocked")
    completed = queue_state == "completed"
    ledger_complete = len(position_records) == 60 and all(len(row["attempt_slots"]) == 3 for row in position_records)
    report = {
        "schema_version": 1,
        "kind": "phase4_p402_linux_behavior_campaign",
        "campaign_id": campaign_id,
        "started_at_utc": campaign_started_at,
        "completed_at_utc": _now(),
        "platform": "linux-x86_64",
        "orchestrator_platform": platform.system().lower(),
        "suite_id": suite["suite_id"],
        "suite_path": suite_path.relative_to(ROOT).as_posix(),
        "suite_sha256": suite_sha,
        "migration_matrix_path": matrix_path.relative_to(ROOT).as_posix(),
        "migration_matrix_sha256": matrix_sha,
        "base_admission_path": admission_path.relative_to(ROOT).as_posix(),
        "base_admission_sha256": admission_sha,
        "base_admission_fingerprint": admission["environment_fingerprint"],
        "fresh_health_fingerprint": fresh_health["environment_fingerprint"],
        "probe_report_path": probe_path.as_posix(),
        "probe_report_sha256": fresh_health["facts"]["probe"]["report_sha256"],
        "runtime_image": runtime_image,
        "runtime_probe": runtime_probe,
        "contract_validation": validation,
        "bundle_summary": bundles,
        "service_probe": service_evidence,
        "capability_probes": capability_probes,
        "capability_probe_side_runs": capabilities["side_run_evidence"],
        "queue": {
            "path": queue_path.as_posix(),
            "sha256": queue_sha,
            "manifest_sha256": queue_manifest_sha,
            "manifest": queue_manifest,
            "fencing_required": True,
            "worker_count": worker_count,
            "state": queue_state,
            "counts": queue_counts,
            "sqlite_quick_check": quick_check,
            "wal_checkpoint": list(checkpoint) if checkpoint else None,
            "attempts": queue_attempts,
            "journal": queue_journal,
        },
        "denominator": DENOMINATOR,
        "counts": {
            "positions": len(position_records),
            "attempt_slots": sum(len(row["attempt_slots"]) for row in position_records),
            "queued_stage_jobs": queue_counts["total"],
            "paired_attempts_started": len(observed_pairs),
            "side_executions": len(observed_workers),
            "unique_worker_count": unique_workers,
            "cleanup_unconfirmed": cleanup_unconfirmed,
            "slot_statuses": slot_counts,
            "queue_statuses": queue_counts,
        },
        "positions": position_records,
        "status": "completed" if completed else "stopped",
        "ledger_complete": ledger_complete,
        "ledger_omissions": 0 if ledger_complete else None,
        "ledger_duplicates": 0,
        "failure_domains": _failure_domain_counts(queue_jobs),
        "stop_reason": "backend_circuit_open" if infrastructure_stopped else None,
        "gate": {
            "p4_linux_accounting": "passed" if ledger_complete and cleanup_unconfirmed == 0 and queue_counts["leased"] == 0 else "blocked",
            "p4_fixture_gate": "passed" if fixed_four_pass and capabilities_complete and cleanup_unconfirmed == 0 else "blocked",
            "p4_linux_behavior_fixed_four": "passed_scoped" if fixed_four_pass and capabilities_complete and cleanup_unconfirmed == 0 else ("failed" if slot_counts["failed"] else "blocked"),
            "p4_linux_behavior": behavior_status,
            "p4_linux_behavior_required_blocked_positions": sum(1 for row in position_records if row["status"] == "skipped"),
            "p4_automation": "not_evaluated",
        },
    }
    report_sha = _write_new_report(output_path, report)
    report["report_sha256"] = report_sha
    return report


def build_phase4_p402_manifest(campaign_report_path: Path) -> dict[str, Any]:
    campaign_report_path = campaign_report_path.expanduser().resolve(strict=True)
    sidecar = campaign_report_path.with_suffix(campaign_report_path.suffix + ".sha256")
    recorded_sha = sidecar.read_text(encoding="ascii").split()[0]
    observed_sha = _sha256_file(campaign_report_path)
    if recorded_sha != observed_sha:
        raise ValueError("P4-02 campaign report SHA-256 sidecar mismatch")
    report = _read_json(campaign_report_path)
    if report.get("kind") != "phase4_p402_linux_behavior_campaign" or report.get("ledger_complete") is not True:
        raise ValueError("P4-02 terminal manifest requires a complete 60-position, 180-slot accounting ledger")
    positions = report.get("positions")
    if not isinstance(positions, list) or len(positions) != 60:
        raise ValueError("P4-02 terminal manifest requires exactly 60 behavior positions")
    identities = [(row.get("registry_entry_id"), row.get("case_id")) for row in positions]
    if len(set(identities)) != 60 or any(len(row.get("attempt_slots", [])) != 3 for row in positions):
        raise ValueError("P4-02 terminal manifest rejects missing, duplicated, or incomplete positions")
    slots = [slot for position in positions for slot in position["attempt_slots"]]
    slot_ids = [slot.get("slot_id") for slot in slots]
    if len(slots) != 180 or len(set(slot_ids)) != 180:
        raise ValueError("P4-02 terminal manifest requires exactly 180 unique attempt slots")
    return {
        "schema_version": 1,
        "kind": "phase4_p402_linux_terminal_evidence_manifest",
        "manifest_status": "complete_accounting",
        "behavior_gate_status": report.get("gate", {}).get("p4_linux_behavior"),
        "scoped_fixed_four_gate_status": report.get("gate", {}).get("p4_linux_behavior_fixed_four"),
        "campaign": {
            "campaign_id": report["campaign_id"],
            "report_path": campaign_report_path.relative_to(ROOT).as_posix(),
            "report_sha256": observed_sha,
            "started_at_utc": report.get("started_at_utc"),
            "completed_at_utc": report.get("completed_at_utc"),
            "campaign_status": report.get("status"),
        },
        "inputs": {
            "suite_path": report["suite_path"],
            "suite_sha256": report["suite_sha256"],
            "migration_matrix_path": report["migration_matrix_path"],
            "migration_matrix_sha256": report["migration_matrix_sha256"],
            "admission_path": report["base_admission_path"],
            "admission_sha256": report["base_admission_sha256"],
            "admission_fingerprint": report["base_admission_fingerprint"],
            "fresh_health_fingerprint": report["fresh_health_fingerprint"],
            "probe_report_path": report["probe_report_path"],
            "probe_report_sha256": report["probe_report_sha256"],
            "runtime_image": report["runtime_image"],
            "runtime_probe": report["runtime_probe"],
            "bundle_summary": report["bundle_summary"],
            "service_probe": report["service_probe"],
        },
        "denominator": report["denominator"],
        "counts": report["counts"],
        "queue": report["queue"],
        "capability_probes": report["capability_probes"],
        "gate": report["gate"],
        "positions": positions,
        "attempt_slot_count": len(slots),
        "terminal_checkpoint": {
            "status": report["queue"]["state"],
            "pending_queue_jobs": report["queue"]["counts"].get("queued", 0),
            "leased_queue_jobs": report["queue"]["counts"].get("leased", 0),
            "cleanup_unconfirmed": report["counts"]["cleanup_unconfirmed"],
        },
    }


def write_phase4_p402_manifest(campaign_report_path: Path, output_path: Path) -> dict[str, Any]:
    output_path = output_path.expanduser().absolute()
    if output_path.exists() or output_path.with_suffix(output_path.suffix + ".sha256").exists():
        raise FileExistsError(f"refusing to overwrite an existing P4-02 evidence manifest: {output_path}")
    manifest = build_phase4_p402_manifest(campaign_report_path)
    digest = _write_with_hash(output_path, manifest)
    return {"status": "written", "path": str(output_path), "sha256": digest, "campaign_id": manifest["campaign"]["campaign_id"]}
