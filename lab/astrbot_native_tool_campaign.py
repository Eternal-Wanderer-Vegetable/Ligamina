"""P7-03: compare stock AstrBot's native test adapter to the AB-05 Federated oracle.

Plugin code runs only in a fresh, admitted, network-disabled Linux Docker Worker.
The native side uses AstrBot's own plugin manager, command filters, and pipeline;
it does not import the Ligamina AstrBot Host or Bridge.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lab.astrbot_tool_campaign import (  # noqa: E402
    CASES_PATH,
    DEFAULT_CACHE,
    evaluate,
    tool_name_part,
    validate_cases,
)
from lab.astrbot_census import (  # noqa: E402
    LOCK_FILE as ASTRBOT_LOCK_FILE,
    constraints_text,
    download_wheels,
    extract_plugin,
    locked_names,
    _requirement_name,
)
from lab.workers import DockerWorker, WorkerLimits, WorkerSpec  # noqa: E402

DEFAULT_FEDERATED_REPORT = ROOT / "outputs/acceptance/ab05-astrbot-tool-linux-20261108-v2.json"
ATTEMPTS = 3
ADMISSION_MAX_AGE_SECONDS = 1800
PLUGIN_IDS = {
    "ab-01803ad6c618f2321ed12e48",
    "ab-00d908510026dba4d2cc6acd",
    "ab-026af14d509a0e3b248ae734",
}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    files = sorted((path for path in root.rglob("*") if path.is_file() and path.name != "runtime-manifest.json"),
                   key=lambda path: path.relative_to(root).as_posix())
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _runtime_manifest(runtime_site: Path) -> dict[str, Any]:
    manifest_path = runtime_site / "runtime-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_lock_sha256 = _sha256(ASTRBOT_LOCK_FILE.read_bytes())
    if (manifest.get("schema_version") != 1
            or manifest.get("kind") != "astrbot_native_test_runtime"
            or manifest.get("python_version") != "3.12.14"
            or manifest.get("astrbot_version") != "4.29.1"
            or manifest.get("requirements_lock_sha256") != expected_lock_sha256
            or manifest.get("site_packages_sha256") != _tree_sha256(runtime_site)
            or manifest.get("plugin_code_executed") is not False):
        raise ValueError("AstrBot native runtime manifest is invalid, stale, or not hash-locked")
    return manifest


def _runtime_archive_manifest(runtime_archive: Path) -> tuple[dict[str, Any], str, str]:
    """Validate the compressed runtime bundle without expanding it onto the mounted drive."""
    entries: dict[str, tarfile.TarInfo] = {}
    metadata_versions: dict[str, str] = {}
    manifest: dict[str, Any] | None = None
    manifest_bytes = b""
    tree = hashlib.sha256()
    with tarfile.open(runtime_archive, mode="r:gz") as tar:
        for member in tar.getmembers():
            name = member.name
            parts = PurePosixPath(name).parts
            if (not name.startswith("site-packages/") or name.startswith("/") or ".." in parts
                    or name in entries or not (member.isdir() or member.isfile())):
                raise ValueError(f"unsafe or duplicate entry in AstrBot runtime archive: {name}")
            entries[name] = member
        expected_prefix = "site-packages/"
        for name in sorted(entries):
            member = entries[name]
            if not member.isfile():
                continue
            relative = name.removeprefix(expected_prefix)
            stream = tar.extractfile(member)
            if stream is None:
                raise ValueError(f"unreadable runtime archive entry: {name}")
            file_digest = hashlib.sha256()
            content = bytearray() if relative == "runtime-manifest.json" or relative.endswith(".dist-info/METADATA") else None
            with stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    file_digest.update(block)
                    if content is not None:
                        content.extend(block)
            if relative != "runtime-manifest.json":
                encoded = relative.encode("utf-8")
                tree.update(len(encoded).to_bytes(4, "big"))
                tree.update(encoded)
                tree.update(file_digest.digest())
            if relative == "runtime-manifest.json":
                manifest_bytes = bytes(content or b"")
                manifest = json.loads(manifest_bytes.decode("utf-8"))
            elif relative.endswith(".dist-info/METADATA"):
                metadata = bytes(content or b"").decode("utf-8", errors="replace").splitlines()
                dist_name = next((line.partition(":")[2].strip() for line in metadata if line.lower().startswith("name:")), "")
                version = next((line.partition(":")[2].strip() for line in metadata if line.lower().startswith("version:")), "")
                metadata_versions[dist_name.casefold().replace("-", "_")] = version
    if manifest is None:
        raise ValueError("AstrBot runtime archive has no runtime-manifest.json")
    expected_lock_sha256 = _sha256(ASTRBOT_LOCK_FILE.read_bytes())
    if (manifest.get("schema_version") != 1
            or manifest.get("kind") != "astrbot_native_test_runtime"
            or manifest.get("python_version") != "3.12.14"
            or manifest.get("astrbot_version") != "4.29.1"
            or manifest.get("requirements_lock_path") != ASTRBOT_LOCK_FILE.relative_to(ROOT).as_posix()
            or manifest.get("requirements_lock_sha256") != expected_lock_sha256
            or manifest.get("site_packages_sha256") != tree.hexdigest()
            or manifest.get("distribution_count") != len(metadata_versions)
            or metadata_versions.get("astrbot") != "4.29.1"
            or manifest.get("plugin_code_executed") is not False):
        raise ValueError("AstrBot runtime archive manifest, package metadata, or site digest is invalid")
    return manifest, _sha256_file(runtime_archive), _sha256(manifest_bytes)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _replies(observation: dict[str, Any]) -> list[list[list[str]]]:
    tool_calls = observation.get("tool_calls", observation)
    calls = tool_calls.get("steps", [])
    return [[list(call.get("replies", [])) for call in step] for step in calls]


def _oracle_equivalent(case: dict[str, Any], native: list[list[list[str]]],
                       federated: list[list[list[str]]]) -> bool:
    """Compare exact Oracle replies literally and pattern replies by their frozen contract."""
    expected_steps = case.get("oracle", {}).get("calls", [])
    if len(native) != len(expected_steps) or len(federated) != len(expected_steps):
        return False
    for step_index, expected_step in enumerate(expected_steps):
        if len(native[step_index]) != len(expected_step) or len(federated[step_index]) != len(expected_step):
            return False
        for call_index, expected in enumerate(expected_step):
            expected_replies = expected.get("replies", [])
            native_replies = native[step_index][call_index]
            federated_replies = federated[step_index][call_index]
            if len(native_replies) != len(expected_replies) or len(federated_replies) != len(expected_replies):
                return False
            for native_text, federated_text, oracle in zip(native_replies, federated_replies, expected_replies):
                if "text" in oracle:
                    if native_text != oracle["text"] or federated_text != oracle["text"]:
                        return False
                elif "pattern" in oracle:
                    if (re.fullmatch(oracle["pattern"], native_text) is None
                            or re.fullmatch(oracle["pattern"], federated_text) is None):
                        return False
                else:
                    return False
    return True


def _validate_federated_report(path: Path, cases: dict[str, Any], lock: dict[str, Any], lock_path: Path) -> tuple[dict, str]:
    report_bytes = path.read_bytes()
    report = json.loads(report_bytes)
    expected_cases = sum(len(entry["cases"]) for entry in cases["entries"])
    expected_attempts = expected_cases * ATTEMPTS
    if report.get("kind") != "astrbot_tool_linux_campaign" or report.get("status") != "completed":
        raise ValueError("AB-05 Federated report is not a completed tool campaign")
    if report.get("snapshot_id") != lock["snapshot_id"]:
        raise ValueError("AB-05 Federated report names a different catalog snapshot")
    if report.get("cases_sha256") != _sha256(CASES_PATH.read_bytes()):
        raise ValueError("AB-05 Federated report was produced from different case bytes")
    if report.get("artifacts_lock_sha256") != _sha256(lock_path.read_bytes()):
        raise ValueError("AB-05 Federated report was produced from a different artifact lock")
    if report.get("counts", {}).get("positions") != expected_cases or report.get("counts", {}).get("attempts") != expected_attempts:
        raise ValueError("AB-05 Federated report does not close all P7-03 positions and attempts")
    if report.get("gate", {}).get("astrbot_tool_linux") != "passed_scoped":
        raise ValueError("AB-05 Federated report does not have the scoped pass gate")
    failures = [position["case_id"] for position in report["positions"] if position.get("status") != "passed"]
    if failures:
        raise ValueError(f"AB-05 Federated positions failed: {failures}")
    failed_attempts = [attempt["attempt_id"] for position in report["positions"]
                       for attempt in position.get("attempts", []) if attempt.get("status") != "passed"]
    if failed_attempts:
        raise ValueError(f"AB-05 Federated attempts failed: {failed_attempts}")
    if report.get("counts", {}).get("passed_attempts") != expected_attempts:
        raise ValueError("AB-05 Federated pass count differs from its completed attempts")
    return report, _sha256(report_bytes)


def _admission(admission_path: Path) -> dict[str, Any]:
    report = json.loads(admission_path.read_text(encoding="utf-8"))
    if report.get("status") != "admitted" or report.get("platform") != "linux":
        raise RuntimeError("a current admitted Linux Worker report is required")
    capabilities = report.get("capabilities", {})
    if not capabilities.get("network_disabled") or not capabilities.get("cleanup_evidence_required"):
        raise RuntimeError("Linux Worker admission must enforce network denial and cleanup evidence")
    collected = report.get("collected_at_epoch")
    if not isinstance(collected, int) or time.time() - collected > ADMISSION_MAX_AGE_SECONDS:
        raise RuntimeError("Linux Worker admission is older than 30 minutes; refresh the benign preflight")
    docker = report.get("facts", {}).get("docker", {})
    current = subprocess.run(
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if current.returncode != 0 or current.stdout.strip() != docker.get("server_version"):
        raise RuntimeError("Docker server differs from the admitted environment fingerprint")
    image = report.get("facts", {}).get("image", {})
    if (not isinstance(image.get("locked_reference"), str)
            or "@sha256:" not in image["locked_reference"]
            or image.get("present") is not True):
        raise RuntimeError("admission does not bind a pinned Worker image")
    return report


def _federated_by_case(report: dict) -> dict[str, dict]:
    return {position["case_id"]: position for position in report["positions"]}


def _prepare_native_bundle(entry: dict[str, Any], lock_entry: dict[str, Any], bundle: Path,
                           cache_root: Path) -> dict[str, Any]:
    """Bundle only the frozen plugin, its extra wheels and native runner inputs."""
    bundle.mkdir(parents=True)
    (bundle / "wheelhouse").mkdir()
    artifact = cache_root / lock_entry["artifact"]["cache_path"]
    plugin_name, name_source = extract_plugin(artifact, lock_entry["inspection"]["plugin_root"], bundle / "plugin")
    runtime_locked = locked_names()
    requirements, satisfied = [], []
    for line in lock_entry["inspection"].get("requirements") or []:
        if line.startswith(("-", "git+", "http:", "https:")):
            requirements.append(line)
            continue
        (satisfied if _requirement_name(line) in runtime_locked else requirements).append(line)
    (bundle / "requirements.txt").write_text("\n".join(requirements) + ("\n" if requirements else ""), encoding="utf-8")
    (bundle / "constraints.txt").write_text(constraints_text(), encoding="utf-8")
    (bundle / "side").mkdir()
    return {"plugin_dir": plugin_name, "plugin_dir_source": name_source, "requirement_count": len(requirements),
            "satisfied_by_runtime": satisfied}


def run(options: argparse.Namespace) -> dict[str, Any]:
    output = options.output.expanduser().absolute()
    if output.exists():
        raise FileExistsError(f"refusing to overwrite an existing P7-03 report: {output}")
    admission = _admission(options.admission)
    admitted_image = admission.get("facts", {}).get("image", {}).get("locked_reference")
    if "@sha256:" not in options.image or options.image != admitted_image:
        raise ValueError("the Worker image must exactly match the sha256-pinned admitted image")
    runtime_archive = options.runtime_archive.expanduser().absolute()
    runtime_manifest, runtime_archive_sha256, runtime_manifest_sha256 = _runtime_archive_manifest(runtime_archive)
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    lock_path = options.snapshot / "artifacts.lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    validation = validate_cases(cases, lock, options.cache_root)
    if validation.get("status") != "valid":
        raise RuntimeError("AB-05 case citations do not validate against the frozen archives")
    requested_case_id = getattr(options, "case_id", None)
    selected_entries = []
    for entry in cases["entries"]:
        if entry["registry_entry_id"] not in PLUGIN_IDS:
            continue
        selected_cases = [case for case in entry["cases"] if requested_case_id is None or case["case_id"] == requested_case_id]
        if selected_cases:
            selected_entries.append({**entry, "cases": selected_cases})
    if requested_case_id is not None and sum(len(entry["cases"]) for entry in selected_entries) != 1:
        raise ValueError(f"requested P7-03 case does not resolve uniquely: {requested_case_id}")
    federated, federated_sha256 = _validate_federated_report(options.federated_report, cases, lock, lock_path)
    federated_positions = _federated_by_case(federated)
    output_bundle = options.bundle_root.expanduser().absolute()
    if output_bundle.exists():
        raise FileExistsError(f"refusing to reuse a Worker bundle root: {output_bundle}")

    frozen = {entry["registry_entry_id"]: entry for entry in lock["entries"]}
    worker = DockerWorker(image_digest=options.image, repository_root=ROOT)
    campaign_id = f"p7-03-native-{int(time.time())}"
    report: dict[str, Any] = {
        "schema_version": 1,
        "kind": "astrbot_native_tool_baseline",
        "campaign_id": campaign_id,
        "started_at_utc": _now(),
        "status": "running",
        "scope": "stock AstrBot 4.29.1 native command pipeline via private test platform; AB-05 frozen teamup/gallery/pet cases",
        "native_side_sha256": _sha256((ROOT / "lab/astrbot_native_tool_side.py").read_bytes()),
        "bridge_imported": False,
        "cases_path": CASES_PATH.relative_to(ROOT).as_posix(),
        "cases_sha256": _sha256(CASES_PATH.read_bytes()),
        "snapshot_id": lock["snapshot_id"],
        "artifacts_lock_sha256": _sha256(lock_path.read_bytes()),
        "federated_report_path": str(options.federated_report),
        "federated_report_sha256": federated_sha256,
        "admission_path": str(options.admission),
        "admission_sha256": _sha256(options.admission.read_bytes()),
        "runtime_image": options.image,
        "runtime_manifest": runtime_manifest,
        "runtime_manifest_sha256": runtime_manifest_sha256,
        "runtime_archive_path": str(runtime_archive),
        "runtime_archive_sha256": runtime_archive_sha256,
        "runtime_site_packages_sha256": runtime_manifest["site_packages_sha256"],
        "attempts_per_case": ATTEMPTS,
        "selection": {"kind": "single_case_smoke" if requested_case_id else "full_ab05_scope",
                      "case_ids": [case["case_id"] for entry in selected_entries for case in entry["cases"]]},
        "case_validation": validation,
        "positions": [],
        "counts": {"positions": 0, "attempts": 0, "passed_attempts": 0, "failed_attempts": 0, "cleanup_unconfirmed": 0},
    }
    stop = False
    for entry in selected_entries:
        bundle = output_bundle / entry["registry_entry_id"]
        bundle_info = _prepare_native_bundle(entry, frozen[entry["registry_entry_id"]], bundle, options.cache_root)
        shutil.copy2(runtime_archive, bundle / "runtime-site.tar.gz")
        shutil.copy2(ROOT / "lab/astrbot_native_tool_side.py", bundle / "side" / "astrbot_native_tool_side.py")
        if bundle_info["requirement_count"]:
            bundle_info["dependencies"] = download_wheels(bundle)
            if bundle_info["dependencies"]["status"] != "resolved":
                raise RuntimeError(f"{entry['registry_entry_id']}: plugin dependencies unresolvable")
        report.setdefault("bundles", []).append({
            "registry_entry_id": entry["registry_entry_id"],
            "plugin_dir": bundle_info["plugin_dir"],
            "requirement_count": bundle_info["requirement_count"],
            "satisfied_by_runtime": bundle_info["satisfied_by_runtime"],
            "runtime_site_packages_sha256": runtime_manifest["site_packages_sha256"],
            "wheels": sorted((bundle_info.get("dependencies") or {}).get("wheels", {}).items()),
        })
        for case in entry["cases"]:
            position: dict[str, Any] = {"registry_entry_id": entry["registry_entry_id"], "plugin": entry["plugin"],
                                        "case_id": case["case_id"], "case_class": case["class"], "attempts": []}
            federated_position = federated_positions.get(case["case_id"])
            if federated_position is None:
                raise RuntimeError(f"AB-05 Federated position missing: {case['case_id']}")
            for attempt_number in range(1, ATTEMPTS + 1):
                attempt_id = f"{case['case_id']}-native-a{attempt_number}"
                request = {"case_id": case["case_id"], "tool_calls": case["stimulus"]["tool_calls"],
                           "runtime_archive_sha256": runtime_archive_sha256,
                           "runtime_manifest_sha256": runtime_manifest_sha256,
                           "runtime_site_packages_sha256": runtime_manifest["site_packages_sha256"],
                           "requirements_lock_sha256": runtime_manifest["requirements_lock_sha256"]}
                (bundle / "request.json").write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
                spec = WorkerSpec(
                    run_id=campaign_id,
                    entry_id=entry["registry_entry_id"],
                    attempt_id=attempt_id,
                    generation=attempt_number,
                    job_id=f"{entry['registry_entry_id']}:{attempt_id}",
                    command=("/usr/local/bin/python", "/input/side/astrbot_native_tool_side.py"),
                    input_dir=bundle,
                    limits=WorkerLimits(timeout_seconds=420, memory_mb=3072, cpu_cores=2.0, process_limit=256, output_limit_bytes=1024 * 1024),
                    entrypoint="/usr/bin/env",
                )
                outcome = worker.run(spec)
                try:
                    result = json.loads(outcome.stdout.decode("utf-8").strip().splitlines()[-1])
                except (IndexError, UnicodeDecodeError, json.JSONDecodeError):
                    result = {"status": "unknown", "diagnostic": f"worker output unreadable ({outcome.reason_code})"}
                issues = evaluate(case, cases["host_id"], result, cases["global_oracle"], outcome.cleanup_confirmed)
                native_replies = _replies(result.get("tool_calls", {}))
                federated_attempt = federated_position["attempts"][attempt_number - 1]
                federated_replies = _replies(federated_attempt.get("observation", {}))
                same_as_federated = native_replies == federated_replies
                oracle_equivalent = _oracle_equivalent(case, native_replies, federated_replies)
                if not oracle_equivalent:
                    issues.append("native_federated_oracle_mismatch")
                position["attempts"].append({
                    "attempt_id": attempt_id,
                    "status": "passed" if not issues else "failed",
                    "issues": issues,
                    "native_matches_federated": same_as_federated,
                    "native_federated_oracle_equivalent": oracle_equivalent,
                    "native_worker": {"worker_id": outcome.worker_id, "status": outcome.status, "reason_code": outcome.reason_code,
                                      "exit_code": outcome.exit_code, "cleanup_confirmed": outcome.cleanup_confirmed,
                                      "elapsed_seconds": round(outcome.elapsed_seconds, 1), "peak_memory_bytes": outcome.peak_memory_bytes,
                                      "stdout_sha256": outcome.stdout_sha256, "stderr_sha256": outcome.stderr_sha256},
                    "native_observation": {key: result.get(key) for key in ("status", "diagnostic", "install", "native_adapter", "bridge_imported", "tool_calls", "gateway_operation_count", "gateway_errors", "host_alive_after_shutdown")},
                })
                report["counts"]["attempts"] += 1
                report["counts"]["passed_attempts" if not issues else "failed_attempts"] += 1
                if not outcome.cleanup_confirmed:
                    report["counts"]["cleanup_unconfirmed"] += 1
                    report["stop_reason"] = "worker_cleanup_unconfirmed"
                    stop = True
                print(f"{attempt_id} {'passed' if not issues else 'failed: ' + '; '.join(issues)[:300]}", file=sys.stderr, flush=True)
                if stop:
                    break
            position["status"] = "passed" if len(position["attempts"]) == ATTEMPTS and all(a["status"] == "passed" for a in position["attempts"]) else "failed"
            position["federated_status"] = federated_position["status"]
            report["positions"].append(position)
            report["counts"]["positions"] += 1
            if stop:
                break
        if stop:
            break
    expected_positions = sum(len(entry["cases"]) for entry in selected_entries)
    complete = report["counts"]["positions"] == expected_positions and report["counts"]["attempts"] == expected_positions * ATTEMPTS
    all_passed = complete and all(position["status"] == "passed" for position in report["positions"])
    full_scope = requested_case_id is None and expected_positions == sum(
        len(entry["cases"]) for entry in cases["entries"] if entry["registry_entry_id"] in PLUGIN_IDS
    )
    report["completed_at_utc"] = _now()
    report["status"] = "completed" if complete and full_scope else ("partial" if complete else "stopped")
    report["full_scope"] = full_scope
    report["gate"] = {
        "astrbot_native_baseline": "passed_scoped" if all_passed and full_scope and not report["counts"]["cleanup_unconfirmed"] else ("failed" if report["counts"]["failed_attempts"] else "not_evaluated"),
        "scope_note": "Native and Federated used the same frozen AB-05 cases and source-derived Oracle; adapter identity is p7_native_test and exposes no native client or external account.",
    }
    data = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(data)
    output.with_suffix(output.suffix + ".sha256").write_text(f"{_sha256(data)}  {output.name}\n", encoding="ascii")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--admission", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--runtime-archive", type=Path, required=True, help="deterministic tar.gz from astrbot_native_runtime")
    parser.add_argument("--case-id", help="run one AB-05 case as a smoke check; never closes the scoped gate")
    parser.add_argument("--bundle-root", type=Path, required=True)
    parser.add_argument("--federated-report", type=Path, default=DEFAULT_FEDERATED_REPORT)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args(argv)
    report = run(options)
    print(json.dumps({"status": report["status"], "full_scope": report["full_scope"], "gate": report["gate"], "counts": report["counts"]}, ensure_ascii=False))
    return 0 if report["full_scope"] and report["status"] == "completed" and report["gate"]["astrbot_native_baseline"] == "passed_scoped" else 1


if __name__ == "__main__":
    raise SystemExit(main())
