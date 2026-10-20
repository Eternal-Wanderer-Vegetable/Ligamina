"""Run entry-bound behavior cases through isolated native and federated runners."""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any

from lab.native_runner import execute_harness


class BehaviorExecutionError(ValueError):
    """The case cannot be safely bound to a pair of execution paths."""


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _get_path(value: Any, path: str) -> tuple[bool, Any]:
    current = value
    for component in path.split("."):
        if isinstance(current, dict) and component in current:
            current = current[component]
        elif isinstance(current, list) and component.isdecimal() and int(component) < len(current):
            current = current[int(component)]
        else:
            return False, None
    return True, current


def _matches(assertion: dict[str, Any], observation: Any) -> bool:
    operator = assertion["operator"]
    expected = assertion.get("value")
    if operator == "equals":
        return observation == expected
    if operator == "not_equals":
        return observation != expected
    if operator == "contains":
        try:
            return expected in observation
        except TypeError:
            return False
    if operator == "not_contains":
        try:
            return expected not in observation
        except TypeError:
            return False
    if operator == "length_equals":
        try:
            return len(observation) == expected
        except TypeError:
            return False
    if operator == "in_range":
        return isinstance(observation, (int, float)) and not isinstance(observation, bool) and assertion["minimum"] <= observation <= assertion["maximum"]
    if operator == "unchanged":
        return observation is True
    return False


def _assertions(case: dict[str, Any], observation: dict[str, Any], side: str) -> list[dict[str, str]]:
    issues = []
    for assertion in case.get("assertions", []):
        path = assertion.get("path", "")
        found, actual = _get_path({"plugin_observation": observation}, path)
        if not found:
            issues.append({"code": "observation_missing", "side": side, "path": path})
        elif not _matches(assertion, actual):
            issues.append({"code": "oracle_mismatch", "side": side, "path": path})
    return issues


def _valid_identity(entry: dict[str, Any], case: dict[str, Any], platform: str) -> dict[str, Any]:
    if platform not in {"linux-x86_64", "windows-x86_64"}:
        raise BehaviorExecutionError("unsupported behavior platform identity")
    profile = entry.get("dependency_locks", {}).get(platform, {})
    if profile.get("status") != "eligible_not_run":
        raise BehaviorExecutionError("entry is not eligible on the selected platform")
    required = ("registry_entry_id", "ecosystem", "package", "version", "artifact_sha256")
    if any(not isinstance(entry.get(key), str) or not entry[key] for key in required):
        raise BehaviorExecutionError("entry identity is incomplete")
    if not case.get("case_id") or not isinstance(case.get("assertions"), list) or not case["assertions"]:
        raise BehaviorExecutionError("case identity or independent oracle assertions are missing")
    if case.get("class") not in {"positive", "negative", "isolation"}:
        raise BehaviorExecutionError("case class is not a frozen behavior class")
    return {
        "registry_entry_id": entry["registry_entry_id"],
        "ecosystem": entry["ecosystem"],
        "package": entry["package"],
        "version": entry["version"],
        "artifact_sha256": entry["artifact_sha256"],
        "platform": platform,
        "profile_id": profile["profile_id"],
        "dependency_lock_sha256": profile["dependency_lock_sha256"],
        "case_id": case["case_id"],
    }


def run_behavior_pair(
    entry: dict[str, Any],
    case: dict[str, Any],
    platform: str,
    native_command: list[str],
    federated_command: list[str],
    *,
    fixture_context: dict[str, Any] | None = None,
    timeout_seconds: float = 180,
    workspace_root: Path | None = None,
) -> dict[str, Any]:
    """Execute both paths in independent workspaces and apply oracle then pair checks.

    Runner stdout is a JSON object containing the exact ``binding``, a
    ``plugin_observation`` object, cleanup evidence, and a route trace. The
    fixture oracle itself is never sent to either runner.
    """
    if timeout_seconds <= 0:
        raise BehaviorExecutionError("timeout must be greater than zero")
    if len(case.get("stimuli", [])) != 1:
        raise BehaviorExecutionError("behavior case must freeze exactly one deterministic stimulus")
    if not case.get("assertions"):
        raise BehaviorExecutionError("behavior case has no independent expected assertions")
    identity = _valid_identity(entry, case, platform)
    def contains_oracle(value: Any) -> bool:
        if isinstance(value, dict):
            return any(key.casefold() in {"expected", "assertions", "oracle"} or contains_oracle(child) for key, child in value.items())
        if isinstance(value, list):
            return any(contains_oracle(item) for item in value)
        return False

    if contains_oracle(fixture_context or {}):
        raise BehaviorExecutionError("fixture context cannot contain expected oracle data")
    fixture_sha256 = hashlib.sha256(_canonical({"stimuli": case["stimuli"], "assertions": case["assertions"], "oracle": case.get("oracle")})).hexdigest()
    binding = {**identity, "fixture_sha256": fixture_sha256}
    stimulus = copy.deepcopy(case["stimuli"][0])
    deterministic = {
        "fixture_clock": stimulus.get("fixture_clock"),
        "random_seed": stimulus.get("random_seed"),
        "session_scope": stimulus.get("session_scope"),
    }

    def run_side(mode: str, command: list[str], workspace: Path) -> dict[str, Any]:
        workspace.mkdir(parents=True, exist_ok=False)
        context = copy.deepcopy(fixture_context or {})
        if "execution" in context:
            raise BehaviorExecutionError("fixture context cannot override the execution workspace")
        context.update({
            "binding": binding,
            "fixture_context_sha256": hashlib.sha256(_canonical(fixture_context or {})).hexdigest(),
            "execution": {"mode": mode, "workspace": str(workspace.resolve()), **deterministic},
        })
        request = {"case_id": case["case_id"], "stimulus": stimulus, "context": context}
        result = execute_harness(command, request, mode, timeout_seconds, cwd=workspace)
        return result

    temporary_context = tempfile.TemporaryDirectory(prefix="ligamina-behavior-") if workspace_root is None else None
    root = Path(temporary_context.name) if temporary_context is not None else Path(workspace_root).expanduser().absolute()
    try:
        root.mkdir(parents=True, exist_ok=True)
        native = run_side("native", native_command, root / "native")
        federated = run_side("federated", federated_command, root / "federated")
    finally:
        if temporary_context is not None:
            temporary_context.cleanup()

    issues: list[dict[str, str]] = []
    normalized: dict[str, Any] = {}
    for side, result in (("native", native), ("federated", federated)):
        if result.get("status") != "completed":
            issues.append({"code": "run_not_completed", "side": side})
            continue
        if result.get("binding") != binding:
            issues.append({"code": "identity_mismatch", "side": side})
        observation = result.get("plugin_observation")
        if not isinstance(observation, dict):
            issues.append({"code": "observations_missing", "side": side})
            continue
        issues.extend(_assertions(case, observation, side))
        cleanup = result.get("cleanup")
        if not isinstance(cleanup, dict) or cleanup.get("worker_cleanup_confirmed") is not True or cleanup.get("active_capability_count") != 0 or cleanup.get("live_processes") != []:
            issues.append({"code": "cleanup_unconfirmed", "side": side})
        if side == "native":
            trace = result.get("native_trace")
            if not isinstance(trace, dict) or trace.get("bridge_adapter_imported") is not False:
                issues.append({"code": "native_is_not_stock", "side": side})
        else:
            trace = result.get("federated_trace")
            observed_count = observation.get("response_or_operation_count")
            if (
                not isinstance(trace, dict)
                or trace.get("core_host_route") != "passed"
                or trace.get("host_pid_reaped") is not True
                or not isinstance(trace.get("gateway_operation_count"), int)
                or isinstance(trace.get("gateway_operation_count"), bool)
                or (isinstance(observed_count, int) and trace["gateway_operation_count"] != observed_count)
            ):
                issues.append({"code": "core_host_route_unproven", "side": side})
        normalized[side] = observation

    if "native" in normalized and "federated" in normalized and _canonical(normalized["native"]) != _canonical(normalized["federated"]):
        issues.append({"code": "behavior_mismatch", "side": "pair"})
    return {
        "status": "passed" if not issues else "failed",
        "case_id": case["case_id"],
        "binding": binding,
        "execution_started": True,
        "native": native,
        "federated": federated,
        "comparison": {"status": "passed" if not issues else "failed", "issues": issues},
    }
