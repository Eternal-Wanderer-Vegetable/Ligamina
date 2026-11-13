"""P7-05 contract oracles and evidence-bound pilot adjudication.

This module evaluates reusable C1-C8 observations. Missing evidence is unknown;
it is never treated as a pass. The pilot adapter only reuses facts recorded by
P7-04 and leaves checks requiring dedicated stimuli unresolved.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
CONTRACT_IDS = tuple(f"C{index}" for index in range(1, 9))
COMMAND_SCHEMA_PATH = ROOT / "contracts/v0.2/command-descriptor.schema.json"
FIXTURES_PATH = ROOT / "contracts/lab/contract/v1/fixtures.json"


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _unknown(reason: str) -> dict[str, Any]:
    return {"status": "unknown", "reason_code": reason}


def _result(passed: bool, reason: str | None = None) -> dict[str, Any]:
    return {"status": "passed" if passed else "failed", "reason_code": reason}


def evaluate_contract(contract_id: str, evidence: dict[str, Any]) -> dict[str, Any]:
    """Evaluate one contract from an explicit, serializable observation."""
    if contract_id not in CONTRACT_IDS:
        raise ValueError(f"unknown contract: {contract_id}")
    if not isinstance(evidence, dict):
        return _unknown("missing_contract_evidence")

    if contract_id == "C1":
        deadlines = evidence.get("deadlines_ms")
        elapsed = evidence.get("elapsed_ms")
        cleanup = evidence.get("cleanup")
        if not all(isinstance(value, dict) for value in (deadlines, elapsed, cleanup)):
            return _unknown("incomplete_lifecycle_evidence")
        phases = ("load", "ready", "stop")
        if any(not isinstance(deadlines.get(key), int) or not isinstance(elapsed.get(key), int) for key in phases):
            return _unknown("incomplete_lifecycle_deadlines")
        bounded = all(0 <= elapsed[key] <= deadlines[key] for key in phases)
        cleaned = cleanup.get("process_reaped") is True and cleanup.get("container_removed") is True
        return _result(bounded and cleaned, "lifecycle_deadline_or_cleanup_failed" if not (bounded and cleaned) else None)

    if contract_id == "C2":
        lines = evidence.get("stdout_lines")
        unknown_event = evidence.get("unknown_event")
        if not isinstance(lines, list) or not isinstance(unknown_event, dict):
            return _unknown("incomplete_protocol_evidence")
        try:
            packets = [json.loads(line) for line in lines if isinstance(line, str)]
        except json.JSONDecodeError:
            return _result(False, "stdout_contains_non_json_line")
        if len(packets) != len(lines) or any(
            not isinstance(packet, dict) or packet.get("jsonrpc") != "2.0" for packet in packets
        ):
            return _result(False, "stdout_is_not_ndjson_protocol")
        survived = (
            unknown_event.get("rejected") is True
            and unknown_event.get("host_alive_after") is True
            and unknown_event.get("next_valid_event_completed") is True
        )
        return _result(survived, "unknown_event_stopped_host" if not survived else None)

    if contract_id == "C3":
        events = evidence.get("events")
        operations = evidence.get("operations")
        if not isinstance(events, dict) or not isinstance(operations, list):
            return _unknown("incomplete_operation_scope_evidence")
        for operation in operations:
            if not isinstance(operation, dict) or not isinstance(operation.get("event_id"), str):
                return _unknown("malformed_operation_scope_evidence")
            event = events.get(operation["event_id"])
            target = operation.get("target")
            if not isinstance(event, dict) or not isinstance(target, dict):
                return _unknown("unbound_operation_scope")
            allowed = event.get("native_allowed_targets")
            if not isinstance(allowed, list):
                return _unknown("native_scope_oracle_missing")
            if target not in allowed:
                return _result(False, "operation_target_outside_native_event_scope")
        return _result(True)

    if contract_id == "C4":
        operations = evidence.get("idle_operations")
        if not isinstance(operations, list):
            return _unknown("idle_window_not_observed")
        if not operations:
            return _result(True)
        timer = evidence.get("timer")
        if not isinstance(timer, dict):
            return _unknown("timer_declaration_or_native_oracle_missing")
        declared = timer.get("declared") is True
        native = timer.get("native_operations")
        federated = timer.get("federated_operations")
        if not isinstance(native, list) or not isinstance(federated, list):
            return _unknown("timer_native_comparison_missing")
        ok = declared and operations == federated and native == federated
        return _result(ok, "undeclared_or_non_native_idle_operation" if not ok else None)

    if contract_id == "C5":
        exception = evidence.get("handler_exception")
        if not isinstance(exception, dict):
            return _unknown("handler_exception_not_exercised")
        ok = (
            exception.get("raised") is True
            and exception.get("host_alive_after") is True
            and exception.get("next_event_status") == "completed"
        )
        return _result(ok, "handler_exception_disrupted_following_event" if not ok else None)

    if contract_id == "C6":
        timeout = evidence.get("timeout")
        if not isinstance(timeout, dict):
            return _unknown("handler_timeout_not_exercised")
        deadline = timeout.get("deadline_ms")
        closed = timeout.get("core_closed_elapsed_ms")
        if not isinstance(deadline, int) or not isinstance(closed, int):
            return _unknown("incomplete_timeout_deadline_evidence")
        ok = (
            0 <= closed <= deadline
            and timeout.get("late_reply_observed") is True
            and timeout.get("late_reply_in_closed_session") is False
        )
        return _result(ok, "deadline_or_late_reply_contract_failed" if not ok else None)

    if contract_id == "C7":
        descriptors = evidence.get("descriptors")
        calls = evidence.get("calls")
        deadline = evidence.get("call_deadline_ms")
        gateway_count = evidence.get("gateway_operation_count")
        if not isinstance(descriptors, list) or not isinstance(calls, list) or not isinstance(deadline, int):
            return _unknown("command_tool_surface_not_exercised")
        schema = json.loads(COMMAND_SCHEMA_PATH.read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema)
        if any(not isinstance(item, dict) or list(validator.iter_errors(item)) for item in descriptors):
            return _result(False, "invalid_command_descriptor_schema")
        by_id = {call.get("command_id"): call for call in calls if isinstance(call, dict)}
        if len(by_id) != len(calls) or set(by_id) != {row.get("command_id") for row in descriptors}:
            return _result(False, "command_tool_call_coverage_mismatch")
        calls_ok = all(
            isinstance(call.get("elapsed_ms"), int)
            and 0 <= call["elapsed_ms"] <= deadline
            and (call.get("result") is not None or call.get("is_error") is True)
            for call in by_id.values()
        )
        if not calls_ok or gateway_count != 0:
            return _result(False, "command_tool_call_or_gateway_contract_failed")
        return _result(True)

    if contract_id == "C8":
        crash = evidence.get("plugin_crash")
        if not isinstance(crash, dict):
            return _unknown("multi_plugin_isolation_not_exercised")
        ok = (
            crash.get("first_plugin_crashed") is True
            and crash.get("second_plugin_event_status") == "completed"
            and crash.get("host_alive_after") is True
        )
        return _result(ok, "one_plugin_crash_disrupted_peer_plugin" if not ok else None)

    raise AssertionError("unreachable contract")


def evaluate_synthetic_fixtures(fixtures: dict[str, Any]) -> dict[str, Any]:
    """Run fixture observations through the real oracle and enforce both polarities."""
    rows = []
    for case in fixtures.get("cases", []):
        actual = evaluate_contract(case["contract_id"], case.get("evidence"))
        expected = case["expected_status"]
        rows.append({
            "fixture_id": case["fixture_id"],
            "contract_id": case["contract_id"],
            "polarity": case["polarity"],
            "expected_status": expected,
            "actual_status": actual["status"],
            "reason_code": actual.get("reason_code"),
            "passed": actual["status"] == expected,
        })
    counts = {
        "fixtures": len(rows),
        "passed": sum(row["passed"] for row in rows),
        "failed": sum(not row["passed"] for row in rows),
        "positive": sum(row["polarity"] == "positive" for row in rows),
        "negative": sum(row["polarity"] == "negative" for row in rows),
    }
    by_contract = {
        contract_id: {
            polarity: sum(row["contract_id"] == contract_id and row["polarity"] == polarity and row["passed"] for row in rows)
            for polarity in ("positive", "negative")
        }
        for contract_id in CONTRACT_IDS
    }
    valid = (
        counts["failed"] == 0
        and counts["positive"] == len(CONTRACT_IDS)
        and counts["negative"] == len(CONTRACT_IDS)
        and all(value == {"positive": 1, "negative": 1} for value in by_contract.values())
    )
    return {"status": "passed" if valid else "failed", "counts": counts, "by_contract": by_contract, "cases": rows}


def _pilot_entry_contracts(entry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Use P7-04 evidence for bounded lifecycle only; mark absent stimuli unknown."""
    rounds = entry.get("rounds")
    discovery = entry.get("native_discovery") or {}
    discovery_cleanup = (discovery.get("outer_worker") or {}).get("cleanup_confirmed") is True
    lifecycle_pass = (
        entry.get("status") == "completed"
        and discovery.get("status") == "completed"
        and discovery_cleanup
        and isinstance(rounds, list)
        and len(rounds) == 3
        and all(
            row.get("cleanup_confirmed") is True
            and row.get("federated_host_reaped") is True
            and all(
                worker.get("status") == "completed"
                and (worker.get("outer_worker") or {}).get("cleanup_confirmed") is True
                for worker in (row.get("fresh_workers") or {}).values()
            )
            and len(row.get("fresh_workers") or {}) == 3
            for row in rounds
        )
    )
    checks = {contract_id: _unknown("pilot_did_not_run_contract_probe") for contract_id in CONTRACT_IDS}
    if lifecycle_pass:
        checks["C1"] = {
            "status": "passed",
            "reason_code": None,
            "evidence_note": "All three P7-04 reruns completed within the bound Worker budget; all side Workers and the Federated Host were reaped.",
        }
    else:
        checks["C1"] = _unknown("pilot_lifecycle_or_host_cleanup_evidence_incomplete")
    return checks


def build_pilot_report(pilot: dict[str, Any], *, source_path: Path) -> dict[str, Any]:
    if pilot.get("kind") != "p7_p704_generated_differential_pilot" or pilot.get("status") != "completed":
        raise ValueError("expected a completed P7-04 differential pilot report")
    entries = []
    totals = {contract_id: {status: 0 for status in ("passed", "failed", "unknown")} for contract_id in CONTRACT_IDS}
    for source in pilot.get("entries", []):
        checks = _pilot_entry_contracts(source)
        for contract_id, result in checks.items():
            totals[contract_id][result["status"]] += 1
        entries.append({
            "registry_entry_id": source.get("registry_entry_id"),
            "ecosystem": source.get("ecosystem"),
            "package": source.get("package"),
            "version": source.get("version"),
            "pilot_status": source.get("status"),
            "contracts": checks,
        })
    return {
        "schema_version": 1,
        "kind": "p7_p705_pilot_contract_adjudication",
        "status": "completed",
        "source_report": source_path.resolve().relative_to(ROOT).as_posix() if source_path.resolve().is_relative_to(ROOT) else str(source_path.resolve()),
        "source_report_sha256": _sha256(source_path.read_bytes()),
        "contract_suite": "contracts/lab/contract/v1/fixtures.json",
        "scope_note": "C1 is derived only when P7-04 recorded complete bounded Worker/Host cleanup. C2-C8 require stimuli or observations absent from P7-04 and therefore remain unknown per entry.",
        "counts": {"entries": len(entries), "checks": len(entries) * len(CONTRACT_IDS), "by_contract": totals},
        "entries": entries,
    }


def write_report(path: Path, report: dict[str, Any]) -> str:
    path = path.expanduser().absolute()
    if path.exists():
        raise FileExistsError(f"refusing to overwrite P7-05 report: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    path.write_bytes(raw)
    digest = _sha256(raw)
    path.with_suffix(path.suffix + ".sha256").write_text(f"{digest}  {path.name}\n", encoding="ascii")
    return digest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot", type=Path, required=True, help="completed P7-04 report")
    parser.add_argument("--fixtures", type=Path, default=FIXTURES_PATH)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args(argv)
    fixtures = json.loads(options.fixtures.read_text(encoding="utf-8"))
    fixture_result = evaluate_synthetic_fixtures(fixtures)
    if fixture_result["status"] != "passed":
        print(json.dumps(fixture_result, ensure_ascii=False, indent=2))
        return 1
    pilot_path = options.pilot.expanduser().resolve(strict=True)
    pilot = json.loads(pilot_path.read_text(encoding="utf-8"))
    report = build_pilot_report(pilot, source_path=pilot_path)
    report["synthetic_fixture_validation"] = fixture_result
    digest = write_report(options.output, report)
    print(json.dumps({"status": report["status"], "entries": report["counts"]["entries"], "checks": report["counts"]["checks"], "output": str(options.output.resolve()), "sha256": digest}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
