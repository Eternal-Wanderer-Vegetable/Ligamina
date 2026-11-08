"""AB-05: tool-level behavior acceptance for frozen AstrBot market plugins.

Each case calls a plugin's native commands as MCP tools through the real Core inside a
fresh, admitted, network-disabled Linux Docker Worker (lab/astrbot_tool_side.py), three
times. Expected replies come from contracts/lab/automation/tools/v1/astrbot-tool-cases.json,
whose every plugin citation is re-checked here against the frozen archive (file digest,
line range, and the cited string constant or text) before anything is dispatched. The
Worker receives only the stimulus.

Run inside WSL with the control environment:
  python -m lab.astrbot_tool_campaign --snapshot catalogs/<ab-id> --admission <admission.json>
      --image ligamina-astrbot-tool-runtime@sha256:<id> --bundle-root /home/<user>/<new dir>
      --output outputs/acceptance/<new report>.json
  python -m lab.astrbot_tool_campaign --snapshot catalogs/<ab-id> --validate-only
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import re
import shutil
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lab.astrbot_census import DEFAULT_CACHE, download_wheels, prepare_bundle  # noqa: E402
from lab.workers import DockerWorker, WorkerLimits, WorkerSpec  # noqa: E402

CASES_PATH = ROOT / "contracts/lab/automation/tools/v1/astrbot-tool-cases.json"
ATTEMPTS = 3
FRAMEWORK = ("astrbot", "4.29.1")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tool_name_part(value: str) -> str:
    """Mirror of ligamina_core::commands::tool_name_part."""
    sanitized = "".join(char if char.isascii() and (char.isalnum() or char in "_-.") else "_" for char in value)
    return sanitized if sanitized == value else f"{sanitized}-{_sha256(value.encode('utf-8'))[:8]}"


def _string_constants(source: str, first: int, last: int) -> list[str]:
    return [node.value for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and node.lineno >= first and (node.end_lineno or node.lineno) <= last]


def _check_citation(citation: dict[str, Any], expected: str | None, files: dict[str, bytes], label: str) -> None:
    if "framework" in citation:
        if (citation["framework"], citation["version"]) != FRAMEWORK:
            raise ValueError(f"{label}: framework citation is not the locked {FRAMEWORK}")
        return  # locked by the runtime image's hash-checked requirements; recorded, not re-read here
    raw = files.get(citation["file"])
    if raw is None:
        raise ValueError(f"{label}: cited file {citation['file']} is not in the frozen plugin tree")
    if _sha256(raw) != citation["file_sha256"]:
        raise ValueError(f"{label}: {citation['file']} differs from the cited digest")
    source = raw.decode("utf-8-sig")
    first, last = citation["lines"]
    if citation["check"] == "string_constant":
        target = citation.get("text", expected)
        if target not in _string_constants(source, first, last):
            raise ValueError(f"{label}: no string constant at {citation['file']}:{first}-{last} equals the expected reply")
    elif citation["check"] == "contains":
        if citation["text"] not in "\n".join(source.splitlines()[first - 1:last]):
            raise ValueError(f"{label}: {citation['file']}:{first}-{last} does not contain the cited text")
    else:
        raise ValueError(f"{label}: unknown citation check {citation['check']}")


def plugin_files(entry: dict[str, Any], lock_entry: dict[str, Any], cache_root: Path) -> dict[str, bytes]:
    artifact = lock_entry["artifact"]
    data = (cache_root / artifact["cache_path"]).read_bytes()
    if _sha256(data) != artifact["computed_digests"]["sha256"] or entry["artifact_sha256"] != _sha256(data):
        raise ValueError(f"{entry['registry_entry_id']}: archive differs from the frozen lock")
    prefix = lock_entry["inspection"]["plugin_root"] + "/"
    if entry["plugin_root"] + "/" != prefix:
        raise ValueError(f"{entry['registry_entry_id']}: plugin root differs from the frozen lock")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {name[len(prefix):]: archive.read(name) for name in archive.namelist()
                if name.startswith(prefix) and not name.endswith("/")}


def validate_cases(cases: dict[str, Any], lock: dict[str, Any], cache_root: Path) -> dict[str, Any]:
    """Bind every case to the frozen snapshot and every expected reply to its cited source."""
    if cases["snapshot_id"] != lock["snapshot_id"]:
        raise ValueError("cases name a different snapshot")
    frozen = {entry["registry_entry_id"]: entry for entry in lock["entries"] if entry["status"] == "frozen"}
    checked = []
    for entry in cases["entries"]:
        lock_entry = frozen.get(entry["registry_entry_id"])
        if lock_entry is None:
            raise ValueError(f"{entry['registry_entry_id']} is not a frozen snapshot entry")
        files = plugin_files(entry, lock_entry, cache_root)
        for case in entry["cases"]:
            steps, expected_steps = case["stimulus"]["tool_calls"], case["oracle"]["calls"]
            if len(steps) != len(expected_steps) or any(len(a) != len(b) for a, b in zip(steps, expected_steps)):
                raise ValueError(f"{case['case_id']}: Oracle shape differs from its stimulus")
            citations = 0
            for step_index, expected_step in enumerate(expected_steps):
                for call_index, expected in enumerate(expected_step):
                    label = f"{case['case_id']} step{step_index}.call{call_index}"
                    for reply in expected["replies"]:
                        if ("text" in reply) == ("pattern" in reply) or not reply.get("sources"):
                            raise ValueError(f"{label}: each reply needs exactly one of text/pattern and sources")
                        if "pattern" in reply:
                            re.compile(reply["pattern"])
                        for citation in reply["sources"]:
                            _check_citation(citation, reply.get("text"), files, label)
                            citations += 1
                    for citation in expected.get("silent_sources", []):
                        _check_citation(citation, None, files, label)
                        citations += 1
                    if not expected["replies"] and not expected.get("silent_sources"):
                        raise ValueError(f"{label}: an expected silence needs a cited source")
            checked.append({"case_id": case["case_id"], "citations_checked": citations,
                            "oracle_sha256": _sha256(json.dumps(case["oracle"], ensure_ascii=False, sort_keys=True).encode())})
    return {"status": "valid", "cases": checked}


def evaluate(case: dict[str, Any], host_id: str, result: dict[str, Any], global_oracle: dict[str, Any],
             cleanup_confirmed: bool) -> list[str]:
    """Oracle issues for one attempt; an empty list means it passed."""
    issues: list[str] = []
    if not cleanup_confirmed:
        issues.append("worker_cleanup_unconfirmed")
    if result.get("status") != "completed":
        issues.append(f"side_status:{result.get('status')}:{str(result.get('diagnostic', ''))[:160]}")
        return issues
    if result.get("host_alive_after_shutdown") is not False:
        issues.append(f"host_not_reaped:{result.get('host_alive_after_shutdown')}")
    if result.get("gateway_operation_count") != global_oracle["gateway_operation_count"] or result.get("gateway_errors"):
        issues.append(f"gateway:{result.get('gateway_operation_count')}:{result.get('gateway_errors')}")
    tool_calls = result.get("tool_calls") or {}
    listed = set(tool_calls.get("listed_command_tools", []))
    steps = tool_calls.get("steps", [])
    if len(steps) != len(case["oracle"]["calls"]):
        return [*issues, "step_count_mismatch"]
    for step_index, (observed_step, expected_step, stimulus_step) in enumerate(
            zip(steps, case["oracle"]["calls"], case["stimulus"]["tool_calls"])):
        if len(observed_step) != len(expected_step):
            issues.append(f"step{step_index}:call_count_mismatch")
            continue
        for call_index, (observed, expected, stimulus) in enumerate(zip(observed_step, expected_step, stimulus_step)):
            label = f"step{step_index}.call{call_index}"
            tool = f"cmd.{tool_name_part(host_id)}.{tool_name_part(stimulus['command_id'])}"
            if tool not in listed:
                issues.append(f"{label}:tool_not_listed:{tool}")
            if observed.get("is_error") or observed.get("protocol_error"):
                issues.append(f"{label}:error:{observed.get('protocol_error') or observed.get('error_text')}")
                continue
            replies = observed.get("replies", [])
            wanted = expected["replies"]
            if len(replies) != len(wanted):
                issues.append(f"{label}:reply_count:{len(replies)}:{json.dumps(replies, ensure_ascii=False)[:200]}")
            else:
                for reply_index, (got, want) in enumerate(zip(replies, wanted)):
                    ok = got == want["text"] if "text" in want else re.fullmatch(want["pattern"], got) is not None
                    if not ok:
                        issues.append(f"{label}.reply{reply_index}:{json.dumps(got, ensure_ascii=False)[:200]}")
            if len(observed.get("refused_operations", [])) != global_oracle["refused_operations"]:
                issues.append(f"{label}:refused_operations:{len(observed.get('refused_operations', []))}")
    return issues


def prepare(entry: dict[str, Any], lock_entry: dict[str, Any], bundle: Path, cache_root: Path) -> dict[str, Any]:
    info = prepare_bundle(bundle, lock_entry, cache_root)
    if info["requirement_count"]:
        info["dependencies"] = download_wheels(bundle)
        if info["dependencies"]["status"] != "resolved":
            raise RuntimeError(f"{entry['registry_entry_id']}: dependencies unresolvable")
    for name in ("astrbot_tool_side.py", "mcp_http.py"):
        shutil.copy2(ROOT / "lab" / name, bundle / "side" / name)
    # The census bundle omits the Host's stdio entry point, which Core launches here.
    shutil.copy2(ROOT / "hosts/astrbot/ligamina_astrbot/worker.py",
                 bundle / "project/hosts/astrbot/ligamina_astrbot/worker.py")
    return info


def run(options: argparse.Namespace) -> dict[str, Any]:
    output = options.output.expanduser().absolute()
    if output.exists():
        raise FileExistsError(f"refusing to overwrite an existing report: {output}")
    admission = json.loads(options.admission.read_text(encoding="utf-8"))
    capabilities = admission.get("capabilities", {})
    if not (admission.get("status") == "admitted" and admission.get("platform") == "linux"
            and capabilities.get("network_disabled") and capabilities.get("cleanup_evidence_required")):
        raise RuntimeError("the Linux Worker backend has no admitted report")
    if "@sha256:" not in options.image:
        raise ValueError("the runtime image must be pinned by sha256")
    lock_path = options.snapshot / "artifacts.lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    validation = validate_cases(cases, lock, options.cache_root)
    bundle_root = options.bundle_root.expanduser().absolute()
    if bundle_root.exists():
        raise FileExistsError(f"refusing to reuse a bundle root: {bundle_root}")
    frozen = {entry["registry_entry_id"]: entry for entry in lock["entries"]}
    worker = DockerWorker(image_digest=options.image, repository_root=ROOT)
    campaign_id = f"ab05-{int(time.time())}"
    host_id = cases["host_id"]
    report: dict[str, Any] = {
        "schema_version": 1, "kind": "astrbot_tool_linux_campaign", "campaign_id": campaign_id,
        "started_at_utc": _now(), "scope": "tool-surface (AstrBot)",
        "cases_path": CASES_PATH.relative_to(ROOT).as_posix(), "cases_sha256": _sha256(CASES_PATH.read_bytes()),
        "snapshot_id": lock["snapshot_id"], "artifacts_lock_sha256": _sha256(lock_path.read_bytes()),
        "case_validation": validation,
        "admission_path": str(options.admission), "admission_sha256": _sha256(options.admission.read_bytes()),
        "runtime_image": options.image, "attempts_per_case": ATTEMPTS, "bundles": [], "positions": [],
        "counts": {"positions": 0, "attempts": 0, "passed_attempts": 0, "failed_attempts": 0, "cleanup_unconfirmed": 0},
    }
    stop = False
    for entry in cases["entries"]:
        bundle = bundle_root / entry["registry_entry_id"]
        info = prepare(entry, frozen[entry["registry_entry_id"]], bundle, options.cache_root)
        report["bundles"].append({"registry_entry_id": entry["registry_entry_id"], "plugin_dir": info["plugin_dir"],
                                  "requirement_count": info["requirement_count"],
                                  "wheels": sorted((info.get("dependencies") or {}).get("wheels", {}).items())})
        for case in entry["cases"]:
            position: dict[str, Any] = {"registry_entry_id": entry["registry_entry_id"], "plugin": entry["plugin"],
                                        "case_id": case["case_id"], "case_class": case["class"], "attempts": []}
            for attempt_number in range(1, ATTEMPTS + 1):
                attempt_id = f"{case['case_id']}-a{attempt_number}"
                request = {"case_id": case["case_id"], "host_id": host_id, "tool_calls": case["stimulus"]["tool_calls"]}
                (bundle / "request.json").write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
                spec = WorkerSpec(
                    run_id=campaign_id, entry_id=entry["registry_entry_id"], attempt_id=attempt_id,
                    generation=attempt_number, job_id=f"{entry['registry_entry_id']}:{attempt_id}",
                    command=("/opt/astrbot/venv/bin/python", "/input/side/astrbot_tool_side.py"),
                    input_dir=bundle,
                    limits=WorkerLimits(timeout_seconds=420, memory_mb=3072, cpu_cores=2.0, process_limit=256,
                                        output_limit_bytes=1024 * 1024),
                    entrypoint="/usr/bin/env",
                )
                outcome = worker.run(spec)
                try:
                    result = json.loads(outcome.stdout.decode("utf-8").strip().splitlines()[-1])
                except (IndexError, UnicodeDecodeError, json.JSONDecodeError):
                    result = {"status": "unknown", "diagnostic": f"worker output unreadable ({outcome.reason_code})"}
                issues = evaluate(case, host_id, result, cases["global_oracle"], outcome.cleanup_confirmed)
                position["attempts"].append({
                    "attempt_id": attempt_id, "status": "passed" if not issues else "failed", "issues": issues,
                    "worker": {"worker_id": outcome.worker_id, "status": outcome.status, "reason_code": outcome.reason_code,
                               "exit_code": outcome.exit_code, "cleanup_confirmed": outcome.cleanup_confirmed,
                               "elapsed_seconds": round(outcome.elapsed_seconds, 1),
                               "peak_memory_bytes": outcome.peak_memory_bytes,
                               "stdout_sha256": outcome.stdout_sha256, "stderr_sha256": outcome.stderr_sha256},
                    "observation": {key: result.get(key) for key in (
                        "status", "diagnostic", "install", "host_status", "tool_calls", "gateway_operation_count",
                        "gateway_errors", "core_exit_code", "core_killed", "host_alive_after_shutdown")},
                    "core_stderr_tail": (result.get("core_stderr_tail") or "")[-1500:] if issues else None,
                })
                report["counts"]["attempts"] += 1
                report["counts"]["passed_attempts" if not issues else "failed_attempts"] += 1
                print(f"{attempt_id} {'passed' if not issues else 'failed ' + '; '.join(issues)[:300]}",
                      file=sys.stderr, flush=True)
                if not outcome.cleanup_confirmed:
                    report["counts"]["cleanup_unconfirmed"] += 1
                    report["stop_reason"] = "worker_cleanup_unconfirmed"
                    stop = True
                    break
            position["status"] = "passed" if (len(position["attempts"]) == ATTEMPTS
                                              and all(a["status"] == "passed" for a in position["attempts"])) else "failed"
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
        "astrbot_tool_linux": "passed_scoped" if all_passed and not report["counts"]["cleanup_unconfirmed"]
        else ("failed" if report["counts"]["failed_attempts"] else "not_evaluated"),
        "scope_note": "AstrBot tool-surface results for the listed plugins only; they do not change Native/Federated, field or release gates.",
    }
    data = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(data)
    output.with_suffix(output.suffix + ".sha256").write_text(f"{_sha256(data)}  {output.name}\n", encoding="ascii")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--admission", type=Path)
    parser.add_argument("--image")
    parser.add_argument("--bundle-root", type=Path)
    parser.add_argument("--output", type=Path)
    options = parser.parse_args(argv)
    if options.validate_only:
        lock = json.loads((options.snapshot / "artifacts.lock.json").read_text(encoding="utf-8"))
        cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
        print(json.dumps(validate_cases(cases, lock, options.cache_root), ensure_ascii=False, indent=2))
        return 0
    missing = [name for name in ("admission", "image", "bundle_root", "output") if getattr(options, name) is None]
    if missing:
        parser.error(f"missing required options: {', '.join(missing)}")
    report = run(options)
    print(json.dumps({"status": report["status"], "gate": report["gate"], "counts": report["counts"]}, ensure_ascii=False))
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
