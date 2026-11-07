"""TOOL-04: run frozen catalog plugins through the MCP command-tool surface.

Each case calls a plugin's native command as an MCP tool inside a fresh, admitted,
network-disabled Linux Docker Worker (``phase4_docker_side.py --side tool``). The
Worker gets the same frozen artifact, dependency lock, plugin config and random seed
as the parent Phase 4 case; the Oracle stays here on the orchestrator side.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lab.automation.behavior_v2 import SUITE_PATH, _canonical, _read
from lab.automation.phase4_campaign import _fixture_context, _runtime_probe, _sha256_file, _write_with_hash, prepare_bundles
from lab.workers import DockerWorker

CASES_PATH = ROOT / "contracts/lab/automation/tools/v1/tool-surface-cases.json"
ATTEMPTS = 3
TEXT_PATH_SUFFIX = ".params.message.0.text"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _tool_name_part(value: str) -> str:
    return "".join(char if char.isascii() and (char.isalnum() or char in "_-.") else "_" for char in value)


def _parent_texts(case: dict[str, Any]) -> list[str]:
    """Expected reply texts of a parent v2 case, in operation order."""
    texts: list[tuple[int, str]] = []
    for assertion in case["oracle"]["assertions"]:
        path = assertion.get("path", "")
        if path.startswith("plugin_observation.operations.") and path.endswith(TEXT_PATH_SUFFIX) and assertion.get("operator") == "equals":
            index = int(path.split(".")[2])
            texts.append((index, assertion["value"]))
    return [text for _, text in sorted(texts)]


def validate_cases(cases: dict[str, Any], suite: dict[str, Any]) -> dict[str, Any]:
    """Bind every tool case to the frozen parent suite and reject Oracle drift."""
    entries = {entry["registry_entry_id"]: entry for entry in suite["entries"]}
    checked = []
    for tool_entry in cases["entries"]:
        entry = entries.get(tool_entry["registry_entry_id"])
        if entry is None or entry["package"] != tool_entry["package"] or entry["version"] != tool_entry["version"]:
            raise ValueError(f"tool entry is not the frozen parent entry: {tool_entry['registry_entry_id']}")
        parent_cases = {case["case_id"]: case for case in entry["cases"]}
        for case in tool_entry["cases"]:
            for step in case["stimulus"]["tool_calls"]:
                for call in step:
                    if call["command_id"] != tool_entry["command_id"]:
                        raise ValueError(f"{case['case_id']} calls an unexpected command")
            if len(case["oracle"]["calls"]) != len(case["stimulus"]["tool_calls"]) or any(
                len(expected) != len(step) for expected, step in zip(case["oracle"]["calls"], case["stimulus"]["tool_calls"])
            ):
                raise ValueError(f"{case['case_id']} Oracle shape differs from its stimulus")
            parent_id = case.get("parent_case_id")
            for name in ("parent_case_id", "derived_from_case_id", "setup_case_id"):
                if case.get(name) is not None and case[name] not in parent_cases:
                    raise ValueError(f"{case['case_id']} {name} names an unknown parent case")
            cited = 0
            for expected_step in case["oracle"]["calls"]:
                for expected in expected_step:
                    sources = expected.get("reply_sources")
                    if sources is None:
                        continue
                    if len(sources) != len(expected.get("replies", [])):
                        raise ValueError(f"{case['case_id']} cites a different number of sources than replies")
                    for reply, source in zip(expected["replies"], sources):
                        source_case, _, index = source.partition("#")
                        texts = _parent_texts(parent_cases[source_case]) if source_case in parent_cases else []
                        if not index.isdigit() or int(index) >= len(texts) or texts[int(index)] != reply:
                            raise ValueError(f"{case['case_id']} expected text {reply!r} does not match {source}")
                        cited += 1
            checked.append({
                "case_id": case["case_id"],
                "parent_case_id": parent_id,
                "derived_from_case_id": case.get("derived_from_case_id"),
                "parent_oracle_sha256": _sha256_bytes(_canonical(parent_cases[parent_id]["oracle"])) if parent_id else None,
                "cited_parent_texts": cited,
            })
    return {"status": "valid", "cases": checked}


def _setup_case(entry: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    """Parent case whose frozen setup/determinism the tool case reuses."""
    parent_id = case.get("setup_case_id") or case.get("parent_case_id") or next(c["case_id"] for c in entry["cases"] if c["class"] == "positive")
    return next(c for c in entry["cases"] if c["case_id"] == parent_id)


def build_request(entry: dict[str, Any], case: dict[str, Any], bundle: Path, asset_report: dict[str, Any]) -> dict[str, Any]:
    setup = _setup_case(entry, case)
    stimulus = copy.deepcopy(case["stimulus"])
    context = _fixture_context(entry, setup, bundle, asset_report)
    context["expected_environment"]["stimulus_sha256"] = _sha256_bytes(_canonical(stimulus))
    context["binding"] = {
        "registry_entry_id": entry["registry_entry_id"],
        "package": entry["package"],
        "version": entry["version"],
        "artifact_sha256": entry["artifact_sha256"],
        "case_id": case["case_id"],
        "setup_case_id": setup["case_id"],
    }
    context["execution"] = {"mode": "tool", **copy.deepcopy(setup["determinism"])}
    context["behavior_fixture"] = {
        "adapter": copy.deepcopy(setup["adapter"]),
        "setup": copy.deepcopy(setup["setup"]),
        "determinism": copy.deepcopy(setup["determinism"]),
    }
    return {"case_id": case["case_id"], "stimulus": stimulus, "context": context}


def evaluate(case: dict[str, Any], command_id: str, host_id: str, result: dict[str, Any], global_oracle: dict[str, Any]) -> list[str]:
    """Return Oracle issues for one attempt; an empty list means it passed."""
    issues: list[str] = []
    outer = result.get("outer_worker", {})
    if outer.get("cleanup_confirmed") is not True:
        issues.append("worker_cleanup_unconfirmed")
    if result.get("status") != "completed":
        issues.append(f"side_status:{result.get('status')}:{result.get('reason_code') or result.get('diagnostic', '')[:120]}")
        return issues
    if result.get("federated_trace", {}).get("host_pid_reaped") is not True:
        issues.append("host_not_reaped")
    observation = result.get("plugin_observation", {})
    if observation.get("gateway_operation_count") != global_oracle["gateway_operation_count"]:
        issues.append(f"gateway_operations:{observation.get('gateway_operation_count')}")
    tool_calls = observation.get("tool_calls") or {}
    expected_tool = f"cmd.{_tool_name_part(host_id)}.{_tool_name_part(command_id)}"
    if expected_tool not in tool_calls.get("listed_command_tools", []):
        issues.append(f"tool_not_listed:{expected_tool}")
    steps = tool_calls.get("steps", [])
    if len(steps) != len(case["oracle"]["calls"]):
        issues.append("step_count_mismatch")
        return issues
    for step_index, (observed_step, expected_step) in enumerate(zip(steps, case["oracle"]["calls"])):
        if len(observed_step) != len(expected_step):
            issues.append(f"step{step_index}:call_count_mismatch")
            continue
        for call_index, (observed, expected) in enumerate(zip(observed_step, expected_step)):
            label = f"step{step_index}.call{call_index}"
            if observed.get("is_error") or observed.get("protocol_error"):
                issues.append(f"{label}:error:{observed.get('protocol_error') or observed.get('error_text')}")
                continue
            if observed.get("dispatch") != global_oracle["dispatch"]:
                issues.append(f"{label}:dispatch:{observed.get('dispatch')}")
            replies = observed.get("replies", [])
            if "replies" in expected and replies != expected["replies"]:
                issues.append(f"{label}:replies:{json.dumps(replies, ensure_ascii=False)[:200]}")
            if "reply_count" in expected and len(replies) != expected["reply_count"]:
                issues.append(f"{label}:reply_count:{len(replies)}")
            forbidden = expected.get("replies_must_not_contain")
            if forbidden and any(forbidden in reply for reply in replies):
                issues.append(f"{label}:forbidden_text_delivered")
            if len(observed.get("refused_operations", [])) != expected.get("refused_operations", 0):
                issues.append(f"{label}:refused_operations:{len(observed.get('refused_operations', []))}")
    return issues


def execute(*, admission_path: Path, runtime_image: str, asset_root: Path, bundle_root: Path, output_path: Path) -> dict[str, Any]:
    output_path = output_path.expanduser().absolute()
    if output_path.exists():
        raise FileExistsError(f"refusing to overwrite an existing TOOL-04 report: {output_path}")
    admission = json.loads(admission_path.read_text(encoding="utf-8"))
    capabilities = admission.get("capabilities", {})
    if not (
        admission.get("status") == "admitted"
        and admission.get("platform") == "linux"
        and capabilities.get("claim_allowed") is True
        and capabilities.get("network_disabled") is True
        and capabilities.get("cleanup_evidence_required") is True
    ):
        raise RuntimeError("the Linux Worker backend has no admitted report")
    if "@sha256:" not in runtime_image:
        raise ValueError("the runtime image must be pinned by sha256")
    suite = _read(SUITE_PATH)
    cases = _read(CASES_PATH)
    validation = validate_cases(cases, suite)
    bundles = prepare_bundles(asset_root, bundle_root, suite)
    worker = DockerWorker(image_digest=runtime_image, repository_root=ROOT)
    campaign_id = f"tool04-{int(time.time())}"
    runtime_probe = _runtime_probe(worker, bundle_root.expanduser().absolute(), run_id=f"{campaign_id}-runtime-probe")
    report: dict[str, Any] = {
        "schema_version": 1,
        "kind": "tool_surface_linux_campaign",
        "campaign_id": campaign_id,
        "started_at_utc": _now(),
        "platform": "linux-x86_64",
        "orchestrator_platform": platform.system().lower(),
        "scope": "tool-surface",
        "cases_path": CASES_PATH.relative_to(ROOT).as_posix(),
        "cases_sha256": _sha256_file(CASES_PATH),
        "parent_suite_path": SUITE_PATH.relative_to(ROOT).as_posix(),
        "parent_suite_sha256": _sha256_file(SUITE_PATH),
        "case_validation": validation,
        "admission_path": str(admission_path),
        "admission_sha256": _sha256_file(admission_path),
        "runtime_image": runtime_image,
        "runtime_probe": runtime_probe,
        "bundle_summary": bundles,
        "positions": [],
        "counts": {"positions": 0, "attempts": 0, "passed_attempts": 0, "failed_attempts": 0, "cleanup_unconfirmed": 0},
        "status": "blocked",
        "gate": {"tool_surface_linux": "not_evaluated"},
    }
    if runtime_probe["status"] != "passed":
        report["stop_reason"] = "runtime_image_probe_failed"
        _write_with_hash(output_path, report)
        return report

    suite_entries = {entry["registry_entry_id"]: entry for entry in suite["entries"]}
    bundle_by_ecosystem = {row["ecosystem"]: (Path(row["path"]), row["asset_report"]) for row in bundles["entries"]}
    side_module = ROOT / "lab/automation/phase4_docker_side.py"
    stop = False
    for tool_entry in cases["entries"]:
        entry = suite_entries[tool_entry["registry_entry_id"]]
        bundle, asset_report = bundle_by_ecosystem[entry["ecosystem"]]
        for case in tool_entry["cases"]:
            position: dict[str, Any] = {
                "registry_entry_id": entry["registry_entry_id"],
                "package": entry["package"],
                "version": entry["version"],
                "artifact_sha256": entry["artifact_sha256"],
                "case_id": case["case_id"],
                "case_class": case["class"],
                "parent_case_id": case.get("parent_case_id"),
                "oracle_sha256": _sha256_bytes(_canonical(case["oracle"])),
                "attempts": [],
            }
            for attempt_number in range(1, ATTEMPTS + 1):
                attempt_id = f"{case['case_id']}-a{attempt_number}"
                request = build_request(entry, case, bundle, asset_report)
                host_id = f"sample-phase4-{entry['registry_entry_id']}-{case['case_id'].rsplit('-', 1)[-1]}"
                started = time.monotonic()
                process = subprocess.run(
                    [sys.executable, str(side_module), "--image", runtime_image, "--bundle", str(bundle),
                     "--run-id", campaign_id, "--entry-id", entry["registry_entry_id"], "--attempt-id", attempt_id,
                     "--generation", str(attempt_number), "--side", "tool"],
                    input=json.dumps(request, ensure_ascii=False), capture_output=True, text=True, encoding="utf-8",
                    timeout=600, check=False,
                )
                try:
                    result = json.loads(process.stdout.strip().splitlines()[-1])
                except (IndexError, json.JSONDecodeError):
                    result = {"status": "failed", "diagnostic": f"side runner output unreadable: {process.stderr[-500:]}"}
                issues = evaluate(case, tool_entry["command_id"], host_id, result, cases["global_oracle"])
                attempt = {
                    "attempt_id": attempt_id,
                    "status": "passed" if not issues else "failed",
                    "issues": issues,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "outer_worker": result.get("outer_worker"),
                    "environment": result.get("environment"),
                    "federated_trace": result.get("federated_trace"),
                    "observation": result.get("plugin_observation"),
                    "diagnostic_summary": result.get("stderr_summary") or result.get("diagnostics_summary") or result.get("diagnostic"),
                }
                position["attempts"].append(attempt)
                report["counts"]["attempts"] += 1
                report["counts"]["passed_attempts" if not issues else "failed_attempts"] += 1
                if (result.get("outer_worker") or {}).get("cleanup_confirmed") is not True:
                    report["counts"]["cleanup_unconfirmed"] += 1
                    report["stop_reason"] = "worker_cleanup_unconfirmed"
                    stop = True
                    break
            position["status"] = "passed" if len(position["attempts"]) == ATTEMPTS and all(a["status"] == "passed" for a in position["attempts"]) else "failed"
            report["positions"].append(position)
            report["counts"]["positions"] += 1
            if stop:
                break
        if stop:
            break

    expected_positions = sum(len(entry["cases"]) for entry in cases["entries"])
    complete = report["counts"]["positions"] == expected_positions and report["counts"]["attempts"] == expected_positions * ATTEMPTS
    all_passed = complete and all(position["status"] == "passed" for position in report["positions"])
    report["completed_at_utc"] = _now()
    report["status"] = "completed" if complete else "stopped"
    report["gate"] = {
        "tool_surface_linux": "passed_scoped" if all_passed and report["counts"]["cleanup_unconfirmed"] == 0
        else ("failed" if report["counts"]["failed_attempts"] else "not_evaluated"),
        "scope_note": "Tool-surface results do not change Native/Federated behavior or field gates.",
    }
    _write_with_hash(output_path, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--admission", type=Path)
    parser.add_argument("--runtime-image")
    parser.add_argument("--asset-root", type=Path)
    parser.add_argument("--bundle-root", type=Path)
    parser.add_argument("--output", type=Path)
    options = parser.parse_args(argv)
    if options.validate_only:
        print(json.dumps(validate_cases(_read(CASES_PATH), _read(SUITE_PATH)), ensure_ascii=False, indent=2))
        return 0
    missing = [name for name in ("admission", "runtime_image", "asset_root", "bundle_root", "output") if getattr(options, name) is None]
    if missing:
        parser.error(f"missing required options: {', '.join(missing)}")
    report = execute(
        admission_path=options.admission.expanduser().resolve(strict=True),
        runtime_image=options.runtime_image,
        asset_root=options.asset_root,
        bundle_root=options.bundle_root,
        output_path=options.output,
    )
    print(json.dumps({"status": report["status"], "gate": report["gate"], "counts": report["counts"]}, ensure_ascii=False))
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
