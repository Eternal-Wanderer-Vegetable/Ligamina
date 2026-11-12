"""Restartable executor pieces for the frozen AstrBot command campaign."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import threading
import uuid
from argparse import ArgumentParser
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from lab.automation.reconcile import DockerReconciler, resume_reconciled_run
from lab.astrbot_census import _summarize_worker_side
from lab.astrbot_campaign_plan import (
    DEFAULT_HOST_ID,
    _normalized_descriptor,
    differential_jobs_for_inventory,
    plan_astrbot_command_campaign,
    register_campaign_plan,
)
from lab.automation.p7_contracts import CONTRACT_IDS, evaluate_contract
from lab.scenario_generator import generate_scenarios
from lab.queue import QueueError, StageQueue
from lab.workers.base import WorkerLimits, WorkerResult, WorkerSpec


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _observation_projection(value: Any) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(value, dict) or value.get("status") != "completed":
        return None, "worker_observation_incomplete"
    if value.get("host_alive_after_shutdown") is not False:
        return None, "host_cleanup_unconfirmed"
    gateway_operations = value.get("gateway_operation_count")
    gateway_errors = value.get("gateway_errors")
    if (not isinstance(gateway_operations, int) or isinstance(gateway_operations, bool) or gateway_operations < 0
            or not isinstance(gateway_errors, list)):
        return None, "gateway_observation_incomplete"
    tool_calls = value.get("tool_calls")
    steps = tool_calls.get("steps") if isinstance(tool_calls, dict) else None
    if not isinstance(steps, list):
        return None, "command_observation_incomplete"
    projected_steps: list[list[dict[str, Any]]] = []
    meaningful = False
    for step in steps:
        if not isinstance(step, list) or not step:
            return None, "command_observation_incomplete"
        projected_step = []
        for call in step:
            if not isinstance(call, dict):
                return None, "command_observation_incomplete"
            command_id = call.get("command_id")
            is_error = call.get("is_error")
            replies = call.get("replies")
            if (not isinstance(command_id, str) or not command_id or not isinstance(is_error, bool)
                    or not isinstance(replies, list) or any(not isinstance(reply, str) for reply in replies)):
                return None, "command_observation_incomplete"
            if is_error or any(reply.strip() for reply in replies):
                meaningful = True
            projected_step.append({
                "command_id": command_id,
                "is_error": is_error,
                "reply_count": len(replies),
                "replies_sha256": _sha256(_canonical(replies)),
                "refused_operation_count": len(call.get("refused_operations", []))
                if isinstance(call.get("refused_operations", []), list) else None,
            })
        projected_steps.append(projected_step)
    if not meaningful:
        return None, "no_meaningful_command_observation"
    projection = {
        "steps": projected_steps,
        "gateway_operation_count": gateway_operations,
        "gateway_error_count": len(gateway_errors),
    }
    return projection, None


def compare_astrbot_observations(
    native_first: Any,
    native_second: Any,
    federated: Any,
) -> dict[str, Any]:
    """Compare bounded Native×2/Federated observations without retaining plugin replies."""
    inputs = {
        "native_first": native_first,
        "native_second": native_second,
        "federated": federated,
    }
    projections: dict[str, dict[str, Any]] = {}
    failures: dict[str, str] = {}
    for name, value in inputs.items():
        projection, error = _observation_projection(value)
        if error:
            failures[name] = error
        else:
            assert projection is not None
            projections[name] = projection
    hashes = {name: _sha256(_canonical(value)) for name, value in projections.items()}
    result: dict[str, Any] = {
        "category": "unknown",
        "reason_code": next(iter(failures.values())) if failures else "comparison_unclassified",
        "scenario_count": 0,
        "call_count": 0,
        "native_repeat_equal": None,
        "federated_gateway_operation_count": None,
        "observation_sha256": hashes,
    }
    if failures:
        result["incomplete_sides"] = failures
        return result

    first = projections["native_first"]
    second = projections["native_second"]
    host = projections["federated"]
    result["scenario_count"] = len(first["steps"])
    result["call_count"] = sum(len(step) for step in first["steps"])
    result["native_repeat_equal"] = hashes["native_first"] == hashes["native_second"]
    result["federated_gateway_operation_count"] = host["gateway_operation_count"]
    if not result["native_repeat_equal"]:
        result.update(category="nondeterministic", reason_code="native_observations_differ")
    elif host["gateway_operation_count"] or host["gateway_error_count"]:
        result.update(category="non_equivalent", reason_code="federated_gateway_activity")
    elif hashes["native_first"] == hashes["federated"]:
        result.update(category="equivalent", reason_code="stable_native_federated_equal")
    else:
        result.update(category="non_equivalent", reason_code="stable_native_federated_mismatch")
    return result


def _mcp_tool_name(host_id: str, command_id: str) -> str:
    def part(value: str) -> str:
        sanitized = "".join(char if char.isascii() and (char.isalnum() or char in "_-.") else "_" for char in value)
        return sanitized if sanitized == value else f"{sanitized}-{_sha256(value.encode('utf-8'))[:8]}"

    return f"cmd.{part(host_id)}.{part(command_id)}"


def project_astrbot_contracts(
    command_id: str,
    descriptors: Any,
    federated: Any,
    *,
    worker_cleanup_confirmed: bool,
    worker_execution_passed: bool = True,
    host_id: str = DEFAULT_HOST_ID,
    call_deadline_ms: int = 90_000,
) -> dict[str, dict[str, Any]]:
    """Project only observed AstrBot tool facts into the reusable C1-C8 oracles.

    This campaign emits no C1-C6/C8-specific stimuli, so those checks remain
    unknown. C7 is evaluated from the actual MCP listing, schema, bounded calls,
    and Gateway observation. Raw schemas, call arguments, and replies are never
    returned in the projection.
    """
    checks = {
        contract_id: {"status": "unknown", "reason_code": "astrbot_campaign_did_not_run_contract_stimulus"}
        for contract_id in CONTRACT_IDS
    }
    # Worker cleanup alone does not provide the measured load/ready/stop phases C1 requires.
    checks["C1"] = {"status": "unknown", "reason_code": "astrbot_lifecycle_phase_timings_not_observed"}

    if not worker_cleanup_confirmed:
        checks["C7"] = {"status": "unknown", "reason_code": "worker_cleanup_unconfirmed"}
        return checks
    if not worker_execution_passed:
        checks["C7"] = {"status": "unknown", "reason_code": "worker_execution_incomplete"}
        return checks

    if not isinstance(descriptors, list):
        checks["C7"] = {"status": "unknown", "reason_code": "command_inventory_not_available"}
        return checks
    normalized: list[dict[str, Any]] = []
    try:
        normalized = [_normalized_descriptor(value, f"AstrBot descriptor {index}")
                      for index, value in enumerate(descriptors)]
    except (TypeError, ValueError):
        checks["C7"] = {"status": "unknown", "reason_code": "command_inventory_invalid"}
        return checks
    expected = [value for value in normalized if value["command_id"] == command_id]
    if len(expected) != 1:
        checks["C7"] = {"status": "unknown", "reason_code": "command_inventory_identity_missing_or_ambiguous"}
        return checks

    tool_calls = federated.get("tool_calls") if isinstance(federated, dict) else None
    if not isinstance(tool_calls, dict):
        checks["C7"] = {"status": "unknown", "reason_code": "mcp_tool_observation_missing"}
        return checks
    listed = tool_calls.get("listed_command_tools")
    schema_rows = tool_calls.get("command_tool_schemas")
    steps = tool_calls.get("steps")
    if (not isinstance(listed, list) or any(not isinstance(name, str) for name in listed)
            or not isinstance(schema_rows, list) or not isinstance(steps, list)):
        checks["C7"] = {"status": "unknown", "reason_code": "mcp_tool_listing_or_calls_incomplete"}
        return checks
    if len(listed) != len(set(listed)) or any(not isinstance(step, list) for step in steps):
        checks["C7"] = {"status": "unknown", "reason_code": "mcp_tool_listing_or_calls_malformed"}
        return checks

    expected_name = _mcp_tool_name(host_id, command_id)
    if expected_name not in listed:
        checks["C7"] = {
            "status": "failed", "reason_code": "mcp_command_not_listed",
            "evidence": {"listed_command_count": len(listed), "command_listed": False},
        }
        return checks
    schemas = [row for row in schema_rows if isinstance(row, dict) and row.get("name") == expected_name]
    if len(schemas) != 1 or "input_schema" not in schemas[0]:
        checks["C7"] = {
            "status": "unknown", "reason_code": "mcp_command_schema_not_observed",
            "evidence": {"listed_command_count": len(listed), "command_listed": True},
        }
        return checks
    schema = schemas[0]["input_schema"]
    try:
        from jsonschema import Draft202012Validator

        if not isinstance(schema, dict):
            raise ValueError("MCP input schema must be an object")
        Draft202012Validator.check_schema(schema)
    except Exception:
        checks["C7"] = {
            "status": "failed", "reason_code": "mcp_command_schema_invalid",
            "evidence": {"listed_command_count": len(listed), "command_listed": True},
        }
        return checks

    calls = [call for step in steps if isinstance(step, list) for call in step]
    if not calls or any(not isinstance(call, dict) or not isinstance(call.get("is_error"), bool) for call in calls):
        checks["C7"] = {"status": "unknown", "reason_code": "mcp_command_calls_not_observed"}
        return checks
    if any(call.get("command_id") != command_id for call in calls):
        checks["C7"] = {"status": "failed", "reason_code": "mcp_command_call_identity_mismatch"}
        return checks
    if any(call.get("timed_out") is True for call in calls):
        checks["C7"] = {"status": "failed", "reason_code": "mcp_command_call_deadline_exceeded"}
        return checks
    if any(call.get("response_observed") is False for call in calls):
        checks["C7"] = {"status": "failed", "reason_code": "mcp_command_call_returned_no_tool_result"}
        return checks
    if any(call.get("response_observed") is not True for call in calls):
        checks["C7"] = {"status": "unknown", "reason_code": "mcp_command_call_result_not_observed"}
        return checks
    elapsed = [call.get("elapsed_ms") for call in calls]
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in elapsed):
        checks["C7"] = {"status": "unknown", "reason_code": "mcp_command_call_timing_missing"}
        return checks
    if not isinstance(call_deadline_ms, int) or isinstance(call_deadline_ms, bool) or call_deadline_ms < 0:
        raise ValueError("call_deadline_ms must be a non-negative integer")
    if max(elapsed) > call_deadline_ms:
        checks["C7"] = {
            "status": "failed", "reason_code": "command_call_deadline_exceeded",
            "evidence": {
                "listed_command_count": len(listed), "command_listed": True,
                "call_count": len(calls), "max_call_elapsed_ms": max(elapsed),
                "call_deadline_ms": call_deadline_ms,
            },
        }
        return checks
    gateway_count = federated.get("gateway_operation_count")
    gateway_errors = federated.get("gateway_errors")
    if (not isinstance(gateway_count, int) or isinstance(gateway_count, bool) or gateway_count < 0
            or not isinstance(gateway_errors, list) or gateway_errors):
        checks["C7"] = {"status": "unknown", "reason_code": "gateway_observation_incomplete"}
        return checks

    aggregate_call = {
        "command_id": command_id,
        "elapsed_ms": max(elapsed),
        "result": {"observed_call_count": len(calls)} if any(call.get("is_error") is not True for call in calls) else None,
        "is_error": all(call.get("is_error") is True for call in calls),
    }
    verdict = evaluate_contract("C7", {
        "descriptors": expected,
        "calls": [aggregate_call],
        "call_deadline_ms": call_deadline_ms,
        "gateway_operation_count": gateway_count,
    })
    checks["C7"] = {
        **verdict,
        "evidence": {
            "listed_command_count": len(listed),
            "command_listed": True,
            "command_schema_sha256": _sha256(_canonical(schema)),
            "call_count": len(calls),
            "max_call_elapsed_ms": max(elapsed),
            "call_deadline_ms": call_deadline_ms,
            "gateway_operation_count": gateway_count,
            "worker_cleanup_confirmed": worker_cleanup_confirmed,
        },
    }
    return checks


def _contract_status(checks: dict[str, dict[str, Any]]) -> str:
    statuses = {item.get("status") for item in checks.values()}
    if "failed" in statuses:
        return "failed"
    if statuses == {"passed"}:
        return "passed"
    if "passed" in statuses:
        return "partial"
    return "unknown"


def _contract_counts(queue: StageQueue, run_id: str) -> dict[str, Any]:
    by_contract = {
        contract_id: {status: 0 for status in ("passed", "failed", "unknown")}
        for contract_id in CONTRACT_IDS
    }
    evaluated_jobs = 0
    pending_jobs = 0
    for job in queue.list_jobs(run_id):
        if job["stage"] != "differential":
            continue
        checks = (job.get("result") or {}).get("contracts")
        if not isinstance(checks, dict):
            pending_jobs += 1
            continue
        evaluated_jobs += 1
        for contract_id in CONTRACT_IDS:
            result = checks.get(contract_id, {})
            status = result.get("status") if isinstance(result, dict) else None
            if status in by_contract[contract_id]:
                by_contract[contract_id][status] += 1
            else:
                by_contract[contract_id]["unknown"] += 1
    return {
        "evaluated_differential_jobs": evaluated_jobs,
        "pending_differential_jobs": pending_jobs,
        "total_registered_differential_jobs": evaluated_jobs + pending_jobs,
        "by_contract": by_contract,
        "scope_note": "Counts cover registered command-level differential jobs. Load non-passes, skipped identities, and entries without observed command inventories remain represented by the frozen census/plan dispositions without fabricated contract checks.",
        "acceptance_boundary": "Only directly observed evidence is classified; unrun stimuli remain unknown.",
    }


def _decode_worker_json(result: WorkerResult) -> tuple[dict[str, Any] | None, str | None]:
    if result.status != "passed":
        return None, result.reason_code or "worker_not_passed"
    if result.output_truncated:
        return None, "worker_output_truncated"
    try:
        lines = result.stdout.decode("utf-8").strip().splitlines()
        observation = json.loads(lines[-1])
    except (UnicodeDecodeError, IndexError, json.JSONDecodeError):
        return None, "worker_output_unreadable"
    return (observation, None) if isinstance(observation, dict) else (None, "worker_output_unreadable")


def _decode_worker_observation(result: WorkerResult) -> tuple[dict[str, Any] | None, str | None]:
    observation, error = _decode_worker_json(result)
    if error:
        return None, error
    projection, error = _observation_projection(observation)
    return (observation, None) if projection is not None else (None, error)


def _worker_summary(result: WorkerResult, observation_error: str | None) -> dict[str, Any]:
    return {
        "status": result.status,
        "reason_code": result.reason_code,
        "exit_code": result.exit_code,
        "elapsed_seconds": round(result.elapsed_seconds, 3),
        "cleanup_confirmed": result.cleanup_confirmed,
        "output_truncated": result.output_truncated,
        "stdout_sha256": result.stdout_sha256,
        "stderr_sha256": result.stderr_sha256,
        "worker_id": result.worker_id,
        "peak_memory_bytes": result.peak_memory_bytes,
        "observation_error": observation_error,
    }


def _validate_worker_specs(
    job: dict[str, Any],
    run_id: str,
    specs: Any,
) -> list[WorkerSpec]:
    expected_count = 1 if job["stage"] == "inventory" else 3 if job["stage"] == "differential" else 0
    if not expected_count:
        raise ValueError(f"unsupported AstrBot campaign stage: {job['stage']}")
    if not isinstance(specs, (list, tuple)) or len(specs) != expected_count or any(not isinstance(spec, WorkerSpec) for spec in specs):
        expectation = "one inventory" if job["stage"] == "inventory" else "Native×2 and Federated"
        raise ValueError(f"a {job['stage']} job requires {expectation} WorkerSpec values")
    for spec in specs:
        if (spec.run_id != run_id or spec.entry_id != job["registry_entry_id"] or spec.job_id != job["job_id"]
                or spec.attempt_id != job["attempt_id"] or spec.generation != job["generation"]):
            raise ValueError("WorkerSpec must carry the claimed StageQueue fencing identity")
    return list(specs)


def execute_campaign_once(
    queue: StageQueue,
    run_id: str,
    worker_id: str,
    worker: Any,
    spec_factory: Any,
    *,
    lease_seconds: float = 1500,
    reconciler: DockerReconciler | None = None,
) -> dict[str, Any]:
    """Recover, claim, run one Native×2/Federated job, and commit a bounded result.

    A cleanup failure quarantines the fenced run. A later call may claim again only
    after exact Docker-label reconciliation confirms cleanup.
    """
    enqueue_followups = getattr(spec_factory, "enqueue_followups", None)
    def register_followups() -> None:
        if not callable(enqueue_followups):
            return
        registration = enqueue_followups(queue, run_id)
        if registration.get("created", 0) and queue.run(run_id)["state"] == "completed":
            queue.resume_run(run_id)

    register_followups()
    current_run = queue.run(run_id)
    if current_run["state"] == "interrupted":
        if reconciler is None:
            return {"status": "blocked", "reason_code": "cleanup_reconciliation_required", "run_id": run_id}
        try:
            resumed = resume_reconciled_run(queue, run_id, reconciler)
        except QueueError:
            return {"status": "blocked", "reason_code": "cleanup_reconciliation_unconfirmed", "run_id": run_id}
        if resumed["run"]["state"] == "interrupted":
            return {"status": "blocked", "reason_code": "cleanup_reconciliation_unconfirmed", "run_id": run_id}

    job = queue.claim(run_id, worker_id, lease_seconds=lease_seconds)
    if job is None:
        return {"status": "idle", "run_id": run_id}
    try:
        specs = _validate_worker_specs(job, run_id, spec_factory(job))
    except Exception as error:  # A malformed frozen input must not be sent to a Worker.
        result = {"status": "failed", "reason_code": "worker_spec_invalid", "diagnostic_type": type(error).__name__}
        try:
            committed = queue.finish(job["job_id"], worker_id, result, attempt_id=job["attempt_id"])
        except QueueError:
            return {"status": "lease_lost", "run_id": run_id, "unit_id": job["unit_id"]}
        return {"status": "finished", "unit_id": job["unit_id"], "queue": committed, "result": result}

    stop_heartbeat = threading.Event()
    lease_lost = threading.Event()

    def heartbeat() -> None:
        interval = max(0.5, lease_seconds / 3)
        while not stop_heartbeat.wait(interval):
            try:
                queue.heartbeat(job["job_id"], worker_id, attempt_id=job["attempt_id"], lease_seconds=lease_seconds)
            except QueueError:
                lease_lost.set()
                return

    heartbeat_thread = threading.Thread(target=heartbeat, name=f"p7-heartbeat-{job['unit_id'][:12]}", daemon=True)
    heartbeat_thread.start()
    outcomes: list[WorkerResult] = []
    observations: list[dict[str, Any] | None] = []
    observation_errors: list[str | None] = []
    worker_error = False
    try:
        for spec in specs:
            try:
                outcome = worker.run(spec)
            except Exception:
                worker_error = True
                break
            if not isinstance(outcome, WorkerResult):
                worker_error = True
                break
            outcomes.append(outcome)
            if job["stage"] == "inventory":
                observation, observation_error = _decode_worker_json(outcome)
            else:
                observation, observation_error = _decode_worker_observation(outcome)
            observations.append(observation)
            observation_errors.append(observation_error)
            if not outcome.cleanup_confirmed:
                break
    finally:
        stop_heartbeat.set()
        heartbeat_thread.join(timeout=2)

    if lease_lost.is_set():
        return {"status": "lease_lost", "run_id": run_id, "unit_id": job["unit_id"]}

    worker_runs = [_worker_summary(result, error) for result, error in zip(outcomes, observation_errors)]
    expected_runs = 1 if job["stage"] == "inventory" else 3
    cleanup_confirmed = len(outcomes) == expected_runs and all(result.cleanup_confirmed for result in outcomes) and not worker_error
    if not cleanup_confirmed:
        result = {
            "status": "unknown",
            "reason_code": "worker_cleanup_unconfirmed" if any(not item.cleanup_confirmed for item in outcomes) else "worker_invocation_incomplete",
            "requires_reconciliation": True,
            "cleanup_confirmed": False,
            "worker_runs": worker_runs,
        }
        try:
            committed = queue.finish(job["job_id"], worker_id, result, attempt_id=job["attempt_id"])
            queue.interrupt_run(run_id)
        except QueueError:
            return {"status": "lease_lost", "run_id": run_id, "unit_id": job["unit_id"]}
        return {"status": "blocked", "unit_id": job["unit_id"], "queue": committed, "result": result}

    if job["stage"] == "inventory":
        side, side_error = observations[0], observation_errors[0]
        if side_error or not isinstance(side, dict):
            result = {"status": "unknown", "reason_code": side_error or "inventory_observation_incomplete",
                      "cleanup_confirmed": True, "worker_runs": worker_runs}
            retryable = any(item.status in {"failed", "unknown"} for item in outcomes)
        else:
            summarized = _summarize_worker_side(side)
            load = summarized.get("load", {})
            side_status = summarized.get("status")
            truncated = isinstance(load, dict) and load.get("commands_truncated") is True
            result_status = (
                "unknown" if truncated else side_status
                if side_status in {"passed", "skipped"} else "unknown"
            )
            result = {
                "status": result_status,
                "reason_code": "runtime_inventory_truncated" if truncated else
                    summarized.get("reason_code") if result_status != "passed" else None,
                "inventory": summarized,
                "inventory_truncated": truncated,
                "cleanup_confirmed": True,
                "worker_runs": worker_runs,
            }
            retryable = False
    else:
        comparison = compare_astrbot_observations(*observations)
        all_worker_runs_passed = all(item.status == "passed" for item in outcomes)
        descriptor_provider = getattr(spec_factory, "descriptors_for_job", None)
        try:
            descriptors = descriptor_provider(job) if callable(descriptor_provider) else None
        except Exception:
            descriptors = None
        contracts = project_astrbot_contracts(
            job["case_id"], descriptors, observations[2],
            worker_cleanup_confirmed=cleanup_confirmed,
            worker_execution_passed=all_worker_runs_passed,
        )
        result_status = "passed" if comparison["category"] != "unknown" and all_worker_runs_passed else "unknown"
        retryable = result_status == "unknown" and any(item.status in {"failed", "unknown"} for item in outcomes)
        result = {
            "status": result_status,
            "reason_code": comparison["reason_code"] if result_status == "unknown" else None,
            "comparison": comparison,
            "contracts": contracts,
            "contract_status": _contract_status(contracts),
            "cleanup_confirmed": True,
            "worker_runs": worker_runs,
            "acceptance_boundary": "Queue pass records a completed comparison only; equivalent is Native consistency evidence, not plugin correctness or support.",
        }
    try:
        committed = queue.finish(job["job_id"], worker_id, result, attempt_id=job["attempt_id"], retryable=retryable)
    except QueueError:
        return {"status": "lease_lost", "run_id": run_id, "unit_id": job["unit_id"]}
    if job["stage"] == "inventory":
        register_followups()
    return {"status": "finished", "unit_id": job["unit_id"], "queue": committed, "result": result}


try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows has no fcntl; the controlled executor runs on Linux.
    _fcntl = None


_LOCAL_LOCKS: dict[str, threading.Lock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()


@contextmanager
def _entry_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCAL_LOCKS_GUARD:
        local = _LOCAL_LOCKS.setdefault(str(path.resolve()), threading.Lock())
    with local:
        with path.open("a+b") as stream:
            if _fcntl is not None:
                _fcntl.flock(stream.fileno(), _fcntl.LOCK_EX)
            try:
                yield
            finally:
                if _fcntl is not None:
                    _fcntl.flock(stream.fileno(), _fcntl.LOCK_UN)


def _external_root(path: Path, repository_root: Path) -> Path:
    requested = path.expanduser().absolute()
    requested.mkdir(parents=True, exist_ok=True)
    current = requested
    while current != current.parent:
        if current.is_symlink() or current.is_junction():
            raise ValueError(f"campaign storage path must not traverse a link: {current}")
        current = current.parent
    resolved = requested.resolve(strict=True)
    repository = repository_root.resolve(strict=True)
    if resolved == repository or repository in resolved.parents:
        raise ValueError(f"campaign storage must be outside the repository: {resolved}")
    return resolved


class AstrBotCampaignSpecFactory:
    """Prepare immutable external bundles and fenced WorkerSpecs for registered P7 jobs."""

    def __init__(
        self,
        *,
        plan: dict[str, Any],
        census: dict[str, Any],
        lock: dict[str, Any],
        queue: StageQueue,
        cache_root: Path,
        bundle_root: Path,
        runtime_archive: Path,
        repository_root: Path,
    ) -> None:
        self.plan = plan
        self.census = census
        self.lock = lock
        self.queue = queue
        self.run_id = plan["run_id"]
        self.inputs = plan["inputs"]
        self.lock_entries = {
            entry["registry_entry_id"]: entry
            for entry in lock.get("entries", [])
            if entry.get("status") == "frozen"
        }
        self.census_rows = {
            row["registry_entry_id"]: row for row in census.get("results", [])
        }
        self.repository_root = repository_root.resolve(strict=True)
        self.bundle_root = _external_root(bundle_root, self.repository_root)
        self.run_root = self.bundle_root / self.run_id
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.cache_root = _external_root(cache_root, self.repository_root)
        self.runtime_archive = runtime_archive.expanduser().resolve(strict=True)
        _external_root(self.runtime_archive.parent, self.repository_root)
        runtime_identity = self.inputs["native_runtime"]
        if _sha256_file(self.runtime_archive) != runtime_identity["archive_sha256"]:
            raise ValueError("Native AstrBot runtime archive differs from the campaign plan")
        self.runtime_identity = runtime_identity

    def _shared_runtime_archive(self) -> Path:
        runtime_root = self.run_root / "runtime"
        runtime_root.mkdir(parents=True, exist_ok=True)
        target = runtime_root / f"site-packages-{self.runtime_identity['archive_sha256']}.tar.gz"
        lock_path = runtime_root / ".runtime-copy.lock"
        with _entry_lock(lock_path):
            if target.exists():
                if _sha256_file(target) != self.runtime_identity["archive_sha256"]:
                    raise ValueError("shared Native runtime archive cache has the wrong digest")
                return target
            temporary = target.with_name(target.name + f".tmp-{uuid.uuid4().hex}")
            shutil.copy2(self.runtime_archive, temporary)
            if _sha256_file(temporary) != self.runtime_identity["archive_sha256"]:
                temporary.unlink(missing_ok=True)
                raise ValueError("copied Native runtime archive failed its digest check")
            os.replace(temporary, target)
        return target

    def _entry_base(self, entry_id: str) -> Path:
        entry = self.lock_entries.get(entry_id)
        if not isinstance(entry, dict):
            raise ValueError(f"campaign job is not bound to a frozen artifact: {entry_id}")
        expected_artifact = entry.get("artifact", {}).get("computed_digests", {}).get("sha256")
        marker = {
            "snapshot_id": self.inputs["snapshot_id"],
            "registry_entry_id": entry_id,
            "artifact_sha256": expected_artifact,
            "runtime_archive_sha256": self.runtime_identity["archive_sha256"],
        }
        entry_root = self.run_root / "entries" / entry_id
        entry_root.mkdir(parents=True, exist_ok=True)
        base = entry_root / "prepared"
        with _entry_lock(entry_root / ".prepare.lock"):
            if base.exists():
                marker_path = base / "campaign-bundle.json"
                if not marker_path.is_file() or json.loads(marker_path.read_text(encoding="utf-8")) != marker:
                    raise ValueError(f"prepared bundle exists with an incomplete or different identity: {entry_id}")
                return base
            temporary = entry_root / f".prepare-{uuid.uuid4().hex}"
            try:
                from lab.astrbot_tool_campaign import prepare as prepare_federated_bundle

                prepare_federated_bundle(entry, entry, temporary, self.cache_root)
                (temporary / "runtime-site.tar.gz").parent.mkdir(parents=True, exist_ok=True)
                os.link(self._shared_runtime_archive(), temporary / "runtime-site.tar.gz")
                shutil.copy2(
                    self.repository_root / "lab/astrbot_native_tool_side.py",
                    temporary / "side/astrbot_native_tool_side.py",
                )
                for path in temporary.rglob("*"):
                    if path.is_symlink():
                        raise ValueError(f"campaign bundle contains a link: {path}")
                (temporary / "campaign-bundle.json").write_text(
                    json.dumps(marker, sort_keys=True) + "\n", encoding="utf-8",
                )
                os.replace(temporary, base)
            except BaseException:
                shutil.rmtree(temporary, ignore_errors=True)
                raise
        return base

    def _inventory_descriptors(self, entry_id: str) -> list[dict[str, Any]]:
        row = self.census_rows.get(entry_id, {})
        side = row.get("result", {}).get("side", {}) if isinstance(row.get("result"), dict) else {}
        load = side.get("load", {}) if isinstance(side, dict) else {}
        descriptors = load.get("command_descriptors") if isinstance(load, dict) else None
        if (row.get("status") == "passed" and isinstance(side, dict) and side.get("status") == "passed"
                and isinstance(load, dict) and load.get("commands_truncated") is not True
                and isinstance(descriptors, list)):
            return descriptors
        for job in self.queue.list_jobs(self.run_id):
            if job["stage"] != "inventory" or job["registry_entry_id"] != entry_id:
                continue
            job_result = job.get("result") or {}
            if job_result.get("status") != "passed" or job_result.get("inventory_truncated") is True:
                continue
            inventory = job_result.get("inventory")
            recovered_load = inventory.get("load", {}) if isinstance(inventory, dict) else {}
            recovered = recovered_load.get("command_descriptors") if isinstance(recovered_load, dict) else None
            if isinstance(recovered, list) and recovered_load.get("commands_truncated") is not True:
                return recovered
        raise ValueError(f"no observed or backfilled command inventory for {entry_id}")

    def descriptors_for_job(self, job: dict[str, Any]) -> list[dict[str, Any]]:
        """Return the complete sanitized inventory bound to this differential job."""
        descriptors = [
            _normalized_descriptor(value, f"{job['registry_entry_id']} contract descriptor {index}")
            for index, value in enumerate(self._inventory_descriptors(job["registry_entry_id"]))
        ]
        if not any(item["command_id"] == job["case_id"] for item in descriptors):
            raise ValueError("differential command is absent from its frozen inventory")
        return descriptors

    def _scenario_batch(self, job: dict[str, Any]):
        entry_id = job["registry_entry_id"]
        lock_entry = self.lock_entries[entry_id]
        artifact_sha = lock_entry.get("artifact", {}).get("computed_digests", {}).get("sha256")
        descriptors = self._inventory_descriptors(entry_id)
        _, expected_jobs = differential_jobs_for_inventory(
            snapshot_id=self.inputs["snapshot_id"],
            artifacts_lock_sha256=self.inputs["artifacts_lock_sha256"],
            selection_sha256=self.inputs["selection_sha256"],
            worker_image=self.inputs["worker_image"],
            native_runtime=self.runtime_identity,
            admission_environment_sha256=self.inputs["admission_environment_sha256"],
            registry_entry_id=entry_id,
            artifact_sha256=artifact_sha,
            descriptors=descriptors,
            max_scenarios=self.inputs["max_scenarios"],
        )
        expected = next((item for item in expected_jobs if item["unit_id"] == job["unit_id"]), None)
        if expected is None or expected["payload"] != job["payload"]:
            raise ValueError(f"differential job inputs no longer match the frozen inventory: {job['unit_id']}")
        descriptor = next(item for item in (
            _normalized_descriptor(value, f"{entry_id} descriptor") for value in descriptors
        ) if item["command_id"] == job["case_id"])
        seed = job["payload"]["generator"]["seed"]
        batch = generate_scenarios(
            inventory_sha256=job["payload"]["inventory_sha256"],
            descriptor=descriptor,
            host_id=DEFAULT_HOST_ID,
            seed=seed,
            max_scenarios=self.inputs["max_scenarios"],
        )
        encoded = _canonical(batch.to_dict())
        if _sha256(encoded) != job["payload"]["scenario_batch_sha256"] or len(batch.scenarios) != job["payload"]["scenario_count"]:
            raise ValueError(f"regenerated scenario batch differs from its frozen job: {job['unit_id']}")
        return batch

    def __call__(self, job: dict[str, Any]) -> list[WorkerSpec]:
        limits = WorkerLimits(timeout_seconds=420, memory_mb=3072, cpu_cores=2.0, process_limit=256,
                              output_limit_bytes=1024 * 1024)
        identity = {
            "run_id": self.run_id,
            "entry_id": job["registry_entry_id"],
            "attempt_id": job["attempt_id"],
            "generation": job["generation"],
            "job_id": job["job_id"],
            "limits": limits,
            "entrypoint": "/usr/bin/env",
        }
        if job["stage"] == "inventory":
            bundle = self._entry_base(job["registry_entry_id"])
            return [WorkerSpec(**identity, command=("/opt/astrbot/venv/bin/python", "/input/side/astrbot_census_side.py"), input_dir=bundle)]
        batch = self._scenario_batch(job)
        tool_calls = []
        for scenario in batch.scenarios:
            if len(scenario.get("steps", [])) != 1 or scenario["steps"][0].get("kind") != "tool_call":
                raise ValueError(f"scenario is not supported by the AstrBot tool adapters: {scenario.get('scenario_id')}")
            step = scenario["steps"][0]
            if step.get("command_id") != job["case_id"]:
                raise ValueError("scenario command differs from the frozen campaign job")
            tool_calls.append([{
                "command_id": step["command_id"],
                "arguments": {"arguments": step.get("arguments", "")},
            }])
        native_common = {
            "case_id": job["case_id"],
            "tool_calls": tool_calls,
            "runtime_archive_sha256": self.runtime_identity["archive_sha256"],
            "runtime_manifest_sha256": self.runtime_identity["manifest_sha256"],
            "runtime_site_packages_sha256": self.runtime_identity["site_packages_sha256"],
            "requirements_lock_sha256": self.runtime_identity["requirements_lock_sha256"],
        }
        bundle = self._entry_base(job["registry_entry_id"])
        native_specs = []
        for _ in range(2):
            native_specs.append(WorkerSpec(
                **identity,
                command=("/usr/local/bin/python", "/input/side/astrbot_native_tool_side.py"),
                input_dir=bundle,
                stdin_data=_canonical(native_common),
            ))
        federated_request = _canonical({
            "case_id": job["case_id"], "host_id": DEFAULT_HOST_ID, "tool_calls": tool_calls,
        })
        federated = WorkerSpec(
            **identity,
            command=("/opt/astrbot/venv/bin/python", "/input/side/astrbot_tool_side.py"),
            input_dir=bundle,
            stdin_data=federated_request,
        )
        return [*native_specs, federated]

    def enqueue_followups(self, queue: StageQueue, run_id: str) -> dict[str, int]:
        if run_id != self.run_id:
            raise ValueError("follow-up registration run differs from the frozen campaign plan")
        jobs = []
        for inventory_job in queue.list_jobs(run_id):
            if inventory_job["stage"] != "inventory":
                continue
            result = inventory_job.get("result") or {}
            if result.get("status") != "passed" or result.get("inventory_truncated") is True:
                continue
            inventory = result.get("inventory")
            load = inventory.get("load", {}) if isinstance(inventory, dict) else {}
            descriptors = load.get("command_descriptors") if isinstance(load, dict) else None
            if not isinstance(descriptors, list) or not descriptors or load.get("commands_truncated") is True:
                continue
            entry_id = inventory_job["registry_entry_id"]
            lock_entry = self.lock_entries.get(entry_id)
            if lock_entry is None:
                raise ValueError(f"backfilled inventory is not bound to a frozen artifact: {entry_id}")
            artifact_sha = lock_entry.get("artifact", {}).get("computed_digests", {}).get("sha256")
            _, generated = differential_jobs_for_inventory(
                snapshot_id=self.inputs["snapshot_id"],
                artifacts_lock_sha256=self.inputs["artifacts_lock_sha256"],
                selection_sha256=self.inputs["selection_sha256"],
                worker_image=self.inputs["worker_image"],
                native_runtime=self.runtime_identity,
                admission_environment_sha256=self.inputs["admission_environment_sha256"],
                registry_entry_id=entry_id,
                artifact_sha256=artifact_sha,
                descriptors=descriptors,
                max_scenarios=self.inputs["max_scenarios"],
            )
            jobs.extend(generated)
        registration = queue.enqueue_jobs(run_id, jobs) if jobs else {"created": 0, "existing": 0}
        if registration["created"] and queue.run(run_id)["state"] == "completed":
            queue.resume_run(run_id)
        return registration


def _verify_sidecar(path: Path) -> str:
    data = path.read_bytes()
    digest = _sha256(data)
    sidecar = path.with_suffix(path.suffix + ".sha256")
    fields = sidecar.read_text(encoding="ascii").strip().split()
    if len(fields) != 2 or fields[0] != digest or fields[1] != path.name:
        raise ValueError(f"SHA-256 sidecar does not match: {path}")
    return digest


def _write_execution_report(path: Path, report: dict[str, Any]) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to overwrite an existing campaign execution report: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    temporary = path.with_name(path.name + f".tmp-{uuid.uuid4().hex}")
    temporary.write_bytes(data)
    os.replace(temporary, path)
    path.with_suffix(path.suffix + ".sha256").write_text(f"{_sha256(data)}  {path.name}\n", encoding="ascii")


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--census", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--admission", type=Path, required=True)
    parser.add_argument("--runtime-archive", type=Path, required=True)
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--bundle-root", type=Path, required=True)
    parser.add_argument("--queue-db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-jobs", type=int, default=100)
    parser.add_argument("--worker-id", default="astrbot-p709-executor")
    options = parser.parse_args(argv)
    try:
        if sys.platform != "linux":
            raise RuntimeError("AstrBot campaign Workers must be dispatched from Linux/WSL")
        if options.max_jobs < 1:
            raise ValueError("--max-jobs must be positive")
        plan_path = options.plan.expanduser().resolve(strict=True)
        census_path = options.census.expanduser().resolve(strict=True)
        plan_sha = _verify_sidecar(plan_path)
        census_sha = _verify_sidecar(census_path)
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        census = json.loads(census_path.read_text(encoding="utf-8"))
        if census_sha != plan.get("inputs", {}).get("census_report_sha256"):
            raise ValueError("completed census bytes differ from the campaign plan")
        lock_path = options.snapshot / "artifacts.lock.json"
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        from lab.astrbot_native_tool_campaign import _admission, _runtime_archive_manifest

        runtime_manifest, archive_sha, manifest_sha = _runtime_archive_manifest(options.runtime_archive)
        native_runtime = {
            "archive_sha256": archive_sha,
            "manifest_sha256": manifest_sha,
            "site_packages_sha256": runtime_manifest["site_packages_sha256"],
            "requirements_lock_sha256": runtime_manifest["requirements_lock_sha256"],
        }
        admission_path = options.admission.expanduser().resolve(strict=True)
        admission_bytes = admission_path.read_bytes()
        admission = _admission(admission_path)
        from lab.astrbot_campaign_plan import _load_full_artifact_lock

        lock, verified_lock_path = _load_full_artifact_lock(options.snapshot)
        recomputed = plan_astrbot_command_campaign(
            lock,
            census,
            artifact_lock_sha256=_sha256(verified_lock_path.read_bytes()),
            census_report_sha256=census_sha,
            worker_image=plan["inputs"]["worker_image"],
            admission=admission,
            admission_sha256=_sha256(admission_bytes),
            native_runtime=native_runtime,
            max_scenarios=plan["inputs"]["max_scenarios"],
        )
        if (recomputed["run_id"] != plan.get("run_id")
                or recomputed["manifest_sha256"] != plan.get("manifest_sha256")
                or recomputed["jobs"] != plan.get("jobs")):
            raise ValueError("recomputed campaign identity or jobs differ from the frozen plan")

        queue_path = options.queue_db.expanduser().absolute()
        registration = register_campaign_plan(str(queue_path), plan)
        queue = StageQueue(queue_path)
        run = queue.run(plan["run_id"])
        if run["manifest_sha256"] != plan["manifest_sha256"]:
            raise ValueError("StageQueue manifest differs from the frozen campaign plan")
        repository_root = Path(__file__).resolve().parents[1]
        spec_factory = AstrBotCampaignSpecFactory(
            plan=plan, census=census, lock=lock, queue=queue,
            cache_root=options.cache_root, bundle_root=options.bundle_root,
            runtime_archive=options.runtime_archive, repository_root=repository_root,
        )
        from lab.workers import DockerWorker

        worker = DockerWorker(image_digest=plan["inputs"]["worker_image"], repository_root=repository_root)
        calls = []
        for _ in range(options.max_jobs):
            outcome = execute_campaign_once(
                queue, plan["run_id"], options.worker_id, worker, spec_factory,
                reconciler=DockerReconciler(),
            )
            calls.append({
                "status": outcome["status"],
                "unit_id": outcome.get("unit_id"),
                "reason_code": (outcome.get("result") or {}).get("reason_code") or outcome.get("reason_code"),
                "comparison": (outcome.get("result") or {}).get("comparison", {}).get("category"),
                "contract_status": (outcome.get("result") or {}).get("contract_status"),
                "c7_status": ((outcome.get("result") or {}).get("contracts") or {}).get("C7", {}).get("status"),
            })
            if outcome["status"] in {"idle", "blocked", "lease_lost"}:
                break
        current_run = queue.run(plan["run_id"])
        report = {
            "schema_version": 1,
            "kind": "p7_p709_astrbot_campaign_execution_checkpoint",
            "status": current_run["state"],
            "run_id": plan["run_id"],
            "plan_sha256": plan_sha,
            "census_sha256": census_sha,
            "frozen_denominator": plan.get("denominator"),
            "campaign_plan_counts": plan.get("counts"),
            "queue_db": str(queue_path),
            "queue_registration": registration,
            "queue_counts": queue.counts(plan["run_id"]),
            "worker_attempt_count": len(queue.list_attempts(plan["run_id"])),
            "contracts": _contract_counts(queue, plan["run_id"]),
            "calls": calls,
            "acceptance_boundary": "Execution and observation comparison only; equivalent is not plugin correctness or support. Unknown remains distinct and cleanup must be reconciled before retry.",
        }
        _write_execution_report(options.output.expanduser().absolute(), report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["status"] in {"running", "completed"} else 3
    except Exception as error:
        print(f"astrbot campaign execution blocked: {type(error).__name__}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
