"""Run the frozen first-wave Phase 4 cases in separate Linux Docker Workers."""

from __future__ import annotations

import hashlib
import json
import platform
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from lab.automation.behavior_v2 import ARTIFACT_CACHE, CATALOG_ROOT, RUNNABLE_IDS, RUNNABLE_IDS_BY_SUITE, SUITE_PATH, MATRIX_PATH, BehaviorV2Error, _canonical, _read, validate_phase4_suite
from lab.automation.execution import run_behavior_pair
from lab.automation.phase4_capability_probes import validate_service_probe_report
from lab.representative import HOST_VERSIONS
from lab.workers import DockerWorker, WorkerLimits, WorkerSpec
from lab.workers.base import ensure_external_directory

ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS_LOCK_PATH = CATALOG_ROOT / "artifacts.lock.json"
PROFILES_LOCK_PATH = CATALOG_ROOT / "profiles.lock.json"
PROFILE_ASSET_REPORTS = {
    "nonebot": "nonebot/wheelhouse-report.json",
    "koishi": "koishi/npm-cache-report.json",
}


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object in {path}")
    return value


def _copy_file(source: Path, destination: Path) -> None:
    if not source.is_file() or source.is_symlink():
        raise ValueError(f"required bundle source is missing or linked: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _copy_python_sources(project: Path) -> None:
    files = (
        "lab/compare.py",
        "lab/recipes.py",
        "lab/native_runner.py",
        "lab/representative.py",
        "lab/baselines/federated.py",
        "lab/baselines/nonebot/runner.py",
        "lab/baselines/nonebot/fixtures/phase4_random_seed.py",
        "lab/baselines/nonebot/fixtures/phase4_probe_observer.py",
        "lab/baselines/koishi/runner.mjs",
        "lab/baselines/koishi/phase4_probe_plugin.mjs",
        "lab/automation/phase4_worker_side.py",
        "hosts/nonebot/ligamina_nonebot/__init__.py",
        "hosts/nonebot/ligamina_nonebot/adapter.py",
        "hosts/nonebot/ligamina_nonebot/capabilities.py",
        "hosts/nonebot/ligamina_nonebot/contracts.py",
        "hosts/nonebot/ligamina_nonebot/lifecycle.py",
        "hosts/nonebot/ligamina_nonebot/worker.py",
        "samples/profiles/nonebot-group-v0.1.json",
        "samples/profiles/koishi-group-v0.1.json",
        "contracts/v0.1/event-envelope.schema.json",
        "contracts/v0.1/platform-operation.schema.json",
        "contracts/v0.1/capability-descriptor.schema.json",
    )
    for relative in files:
        _copy_file(ROOT / relative, project / relative)


def _entry_cache_paths(entry: dict[str, Any], platform_id: str, artifacts_lock: dict[str, Any], profiles_lock: dict[str, Any]) -> tuple[Path, Path]:
    entry_id = entry["registry_entry_id"]
    artifact_record = next((row for row in artifacts_lock["entries"] if row.get("registry_entry_id") == entry_id), None)
    profile_record = next((row for row in profiles_lock["entries"] if row.get("registry_entry_id") == entry_id), None)
    if artifact_record is None or profile_record is None:
        raise ValueError(f"catalog locks have no exact artifact/profile for {entry_id}")
    artifact_platform = "all" if entry["ecosystem"] == "koishi" else platform_id
    artifact_value = next((row.get("artifact") for row in artifact_record.get("artifacts", []) if row.get("platform") == artifact_platform), None)
    profile_value = profile_record.get("profiles", {}).get(platform_id, {}).get("dependency_lock")
    if not isinstance(artifact_value, dict) or not isinstance(profile_value, dict):
        raise ValueError(f"catalog locks are missing the selected Linux artifact/profile for {entry_id}")
    artifact_path = (ARTIFACT_CACHE / artifact_value["cache_path"]).resolve(strict=True)
    profile_path = (ARTIFACT_CACHE / profile_value["cache_path"]).resolve(strict=True)
    if _sha256_file(artifact_path) != entry["artifact_sha256"]:
        if entry["ecosystem"] == "koishi":
            import base64

            observed = "sha512-" + base64.b64encode(hashlib.sha512(artifact_path.read_bytes()).digest()).decode("ascii")
            if observed != entry["artifact_sha256"]:
                raise ValueError(f"cached Koishi artifact differs from frozen identity: {entry_id}")
        else:
            raise ValueError(f"cached Python artifact differs from frozen identity: {entry_id}")
    lock_hash = entry["dependency_locks"][platform_id]["dependency_lock_sha256"]
    if _sha256_file(profile_path) != lock_hash:
        raise ValueError(f"cached dependency lock differs from frozen identity: {entry_id}")
    return artifact_path, profile_path


def _validate_asset_report(asset_root: Path, ecosystem: str, lock_sha256: str) -> dict[str, Any]:
    relative = PROFILE_ASSET_REPORTS[ecosystem]
    report_path = asset_root / relative
    report = _read_json(report_path)
    bound_locks = report.get("profile_lock_sha256s")
    if not isinstance(bound_locks, list):
        bound_locks = [report.get("profile_lock_sha256")]
    if report.get("status") != "passed" or lock_sha256 not in bound_locks:
        raise ValueError(f"{ecosystem} profile assets are not bound to the frozen dependency lock")
    if ecosystem == "nonebot":
        wheels = sorted(path for path in (asset_root / "nonebot/wheelhouse").iterdir() if path.is_file() and path.suffix == ".whl")
        if not wheels or len(wheels) != report.get("wheel_count"):
            raise ValueError("Python wheelhouse count differs from its preparation report")
        report = {**report, "wheelhouse_file_sha256": {path.name: _sha256_file(path) for path in wheels}}
    else:
        cache = asset_root / "koishi/npm-cache.tar.gz"
        if not cache.is_file() or _sha256_file(cache) != report.get("npm_cache_sha256"):
            raise ValueError("Koishi offline npm cache differs from its preparation report")
        report = {**report, "npm_cache_sha256": _sha256_file(cache)}
    return report


def prepare_bundles(asset_root: Path, bundle_root: Path, suite: dict[str, Any]) -> dict[str, Any]:
    """Create external, Oracle-free input bundles for the two exact P4 entries."""
    asset_root = asset_root.expanduser().resolve(strict=True)
    requested_bundle = bundle_root.expanduser().absolute()
    if requested_bundle.exists():
        raise FileExistsError(f"refusing to overwrite an existing Phase 4 bundle root: {requested_bundle}")
    bundle_root = requested_bundle
    bundle_root.mkdir(parents=True)
    artifacts_lock = _read_json(ARTIFACTS_LOCK_PATH)
    profiles_lock = _read_json(PROFILES_LOCK_PATH)
    summary: dict[str, Any] = {"bundle_root": str(bundle_root), "entries": []}

    runnable_ids = RUNNABLE_IDS_BY_SUITE[suite["suite_id"]]
    for entry in suite["entries"]:
        entry_id = entry.get("registry_entry_id")
        if entry_id not in runnable_ids or not any(case.get("migration", {}).get("status") == "executable" for case in entry.get("cases", [])):
            continue
        ecosystem = entry["ecosystem"]
        profile_id = entry["dependency_locks"]["linux-x86_64"]["dependency_lock_sha256"]
        asset_report = _validate_asset_report(asset_root, ecosystem, profile_id)
        artifact_path, lock_path = _entry_cache_paths(entry, "linux-x86_64", artifacts_lock, profiles_lock)
        bundle = bundle_root / ecosystem
        if suite["suite_id"].startswith("phase4-linux-behavior-p402-"):
            bundle = bundle / entry_id
        project = bundle / "project"
        sample = project / "sample"
        _copy_python_sources(project)
        sample.mkdir(parents=True)
        _copy_file(artifact_path, sample / "artifact.bin")
        _copy_file(ROOT / "samples/profiles" / ("nonebot-group-v0.1.json" if ecosystem == "nonebot" else "koishi-group-v0.1.json"), project / "samples/profiles" / ("nonebot-group-v0.1.json" if ecosystem == "nonebot" else "koishi-group-v0.1.json"))
        if ecosystem == "nonebot":
            _copy_file(lock_path, sample / "requirements.txt")
            wheelhouse = asset_root / "nonebot/wheelhouse"
            shutil.copytree(wheelhouse, sample / "wheelhouse")
            lock_name = "requirements.txt"
        else:
            _copy_file(lock_path, sample / "package-lock.json")
            lock_document = _read_json(lock_path)
            root_package = lock_document.get("packages", {}).get("")
            if not isinstance(root_package, dict) or not isinstance(root_package.get("dependencies"), dict):
                raise ValueError("Koishi lock has no explicit catalog profile root dependencies")
            package = {
                "name": root_package.get("name", "ligamina-catalog-profile"),
                "version": root_package.get("version", "0.0.0"),
                "private": True,
                "dependencies": root_package["dependencies"],
            }
            (sample / "package.json").write_text(json.dumps(package, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            _copy_file(asset_root / "koishi/npm-cache.tar.gz", sample / "npm-cache.tar.gz")
            lock_name = "package-lock.json"

        summary["entries"].append(
            {
                "path": str(bundle),
                "registry_entry_id": entry_id,
                "ecosystem": ecosystem,
                "artifact_sha256": entry["artifact_sha256"],
                "profile_lock_sha256": profile_id,
                "artifact_path": str(sample / "artifact.bin"),
                "lock_path": str(sample / lock_name),
                "asset_report": asset_report,
                "host_lock_sha256": _sha256_file(ROOT / "hosts/koishi/package-lock.json") if ecosystem == "koishi" else None,
            }
        )
        # Never place suite fixtures, schema files for behavior-v2, or source artifacts in input.
        forbidden = [
            path for path in project.rglob("*")
            if path.is_file() and ("behavior-suite" in path.name or "phase4-v2" in path.name or path.name.endswith(".oracle.json"))
        ]
        if forbidden:
            raise ValueError(f"Oracle-bearing files were found in Worker input: {forbidden}")
        ensure_external_directory(bundle, forbidden_root=ROOT)
    return summary


def _runtime_probe(worker: DockerWorker, bundle_root: Path, *, run_id: str = "phase4-p401-runtime-probe") -> dict[str, Any]:
    probe_dir = bundle_root / "_runtime_probe_input"
    probe_dir.mkdir()
    code = (
        "import json,os,platform,socket,subprocess,sys; from pathlib import Path; "
        "assert platform.system()=='Linux', platform.system(); "
        "assert platform.machine()=='x86_64', platform.machine(); "
        "assert platform.python_version()=='3.12.14', platform.python_version(); "
        "node=subprocess.check_output(['/usr/local/bin/node','--version'],text=True).strip(); "
        "assert node=='v24.20.0', node; "
        "npm=subprocess.check_output(['/usr/local/bin/node','/usr/local/lib/node_modules/npm/bin/npm-cli.js','--version'],text=True).strip(); "
        "core=subprocess.run(['/usr/local/bin/ligamina-cli','--help'],capture_output=True,text=True,timeout=10); "
        "assert core.returncode==0, core.stderr[-1000:]; "
        "assert os.access('/work',os.W_OK); "
        "exec(\"try: Path('/input/p401-write-probe').write_text('x')\\nexcept OSError: pass\\nelse: raise AssertionError('input mount is writable')\"); "
        "exec(\"try: Path('/etc/p401-write-probe').write_text('x')\\nexcept OSError: pass\\nelse: raise AssertionError('image root is writable')\"); "
        "exec(\"try: socket.create_connection(('1.1.1.1',443),timeout=1)\\nexcept OSError: network='blocked'\\nelse: raise AssertionError('network is reachable')\"); "
        "assert not Path('/opt/ligamina/contracts/lab/automation/v2').exists(); "
        "assert not Path('/opt/ligamina/contracts/lab/v1/fixtures').exists(); "
        "koishi=Path('/opt/ligamina/hosts/koishi'); "
        "host_lock=koishi/'package-lock.json'; "
        "host_worker=koishi/'dist/worker.js'; "
        "sqlite_driver=koishi/'node_modules/@minatojs/driver-sqlite'; "
        "host_lock_sha256=__import__('hashlib').sha256(host_lock.read_bytes()).hexdigest() if host_lock.is_file() else None; "
        f"assert host_lock_sha256=='{_sha256_file(ROOT / 'hosts/koishi/package-lock.json')}', host_lock_sha256; "
        "assert host_worker.is_file(); assert sqlite_driver.is_dir(); "
        "print(json.dumps({'os':platform.system(),'architecture':platform.machine(),'python':platform.python_version(),'node':node,'npm':npm,'core_help':core.returncode,'network':network,'oracle_contracts':'absent','koishi_host_assets':'passed','host_lock_sha256':host_lock_sha256}))"
    )
    spec = WorkerSpec(
        run_id=run_id,
        job_id="runtime-image-admission",
        entry_id="p401-runtime-image",
        attempt_id="runtime-probe",
        generation=1,
        command=("/usr/local/bin/python", "-c", code),
        input_dir=probe_dir,
        limits=WorkerLimits(timeout_seconds=60, memory_mb=4096, cpu_cores=2.0, process_limit=256, output_limit_bytes=65536),
        entrypoint="/usr/bin/env",
    )
    outcome = worker.run(spec)
    try:
        facts = json.loads(outcome.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        facts = None
    passed = (
        outcome.status == "passed"
        and outcome.cleanup_confirmed
        and isinstance(facts, dict)
        and facts.get("oracle_contracts") == "absent"
        and facts.get("koishi_host_assets") == "passed"
    )
    return {
        "status": "passed" if passed else "blocked",
        "image_digest": worker.image_digest,
        "worker": {
            "status": outcome.status,
            "reason_code": outcome.reason_code,
            "exit_code": outcome.exit_code,
            "worker_id": outcome.worker_id,
            "cleanup_confirmed": outcome.cleanup_confirmed,
            "stdout_sha256": outcome.stdout_sha256,
            "stderr_sha256": outcome.stderr_sha256,
            "elapsed_seconds": round(outcome.elapsed_seconds, 3),
        },
        "facts": facts,
    }


def _fixture_context(entry: dict[str, Any], case: dict[str, Any], bundle: Path, asset_report: dict[str, Any]) -> dict[str, Any]:
    ecosystem = entry["ecosystem"]
    profile_lock = entry["dependency_locks"]["linux-x86_64"]["dependency_lock_sha256"]
    profile_file = ROOT / "samples/profiles" / ("nonebot-group-v0.1.json" if ecosystem == "nonebot" else "koishi-group-v0.1.json")
    artifact_algorithm = "sha256"
    module_entry = entry["package"]
    plugin_config = case["setup"].get("plugin_config", {})
    sample = {
        "sample_id": f"p4-{entry['registry_entry_id']}-{case['class']}",
        "ecosystem": ecosystem,
        "package": entry["package"],
        "version": entry["version"],
        "_entry": module_entry,
        "_artifact_hash": entry["artifact_sha256"],
        "_artifact_hash_algorithm": artifact_algorithm,
        "preload_plugins": [],
        "fixture": {"plugin_config": plugin_config},
    }
    config = {
        "ecosystem": ecosystem,
        "package": entry["package"],
        "version": entry["version"],
        "entry": module_entry,
        "preload_plugins": [],
        "host_profile": HOST_VERSIONS[ecosystem],
        "nonebot_config": {},
        "plugin_config": plugin_config,
        "environment": {},
    }
    stimulus = case["stimulus"]
    runtime = "3.12.14" if ecosystem == "nonebot" else "24.20.0"
    framework = "2.5.0" if ecosystem == "nonebot" else "4.18.11"
    expected_environment = {
        "ecosystem": ecosystem,
        "framework_version": framework,
        "runtime_version": runtime,
        "lockfile_sha256": profile_lock,
        "profile_sha256": _sha256_file(profile_file),
        "artifact_hash": entry["artifact_sha256"],
        "config_sha256": _sha256_bytes(_canonical(config)),
        "stimulus_sha256": _sha256_bytes(_canonical(stimulus)),
    }
    host_lock = _sha256_file(ROOT / "hosts/koishi/package-lock.json") if ecosystem == "koishi" else None
    return {
        "repo_root": "/input/project",
        "work_dir": "/work/profile",
        "site_packages": "/work/profile/site-packages",
        "node_executable": "/usr/local/bin/node",
        "sample": sample,
        "fixture": {"plugin_config": plugin_config},
        "account_id": str(stimulus["events"][0]["account_id"]),
        "config": config,
        "sample_environment": {},
        "host_environment": {},
        "koishi_entry_url": "",
        "expected_environment": expected_environment,
        "fingerprint_paths": {
            "artifact": "/input/project/sample/artifact.bin",
            "lockfile": "/input/project/sample/requirements.txt" if ecosystem == "nonebot" else "/input/project/sample/package-lock.json",
            "profile": f"/input/project/samples/profiles/{profile_file.name}",
        },
        **({"host_lock_sha256": host_lock, "npm_cache_sha256": asset_report.get("npm_cache_sha256")} if ecosystem == "koishi" else {}),
    }


def _write_with_hash(path: Path, document: dict[str, Any]) -> str:
    data = json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    digest = _sha256_bytes(data)
    path.with_suffix(path.suffix + ".sha256").write_text(f"{digest}  {path.name}\n", encoding="ascii")
    return digest


def _phase4_capability_results(
    attempts: list[tuple[str, dict[str, Any], dict[str, Any]]],
    service_evidence: dict[str, Any],
    *,
    expected_side_runs: int = 36,
) -> dict[str, Any]:
    source_paths = {
        "nonebot": ROOT / "lab/baselines/nonebot/fixtures/phase4_probe_observer.py",
        "koishi": ROOT / "lab/baselines/koishi/phase4_probe_plugin.mjs",
    }
    expected_source_hashes = {key: _sha256_file(path) for key, path in source_paths.items()}
    side_runs: list[dict[str, Any]] = []
    seen_worker_ids: set[str] = set()
    for attempt_id, case, attempt in attempts:
        expected_events = case.get("stimulus", {}).get("events", [])
        ecosystem = "nonebot" if case.get("case_id", "").startswith("nb-") else "koishi"
        expected = [
            {
                "event_id": str(event["event_id"]),
                "account_id": str(event["account_id"]),
                "actor_id": str(event["actor_id"]),
                "conversation": {"kind": str(event["conversation"]["kind"]), "id": str(event["conversation"]["id"])},
                "timestamp": str(event["timestamp"]),
            }
            for event in expected_events
        ]
        pair_worker_ids: list[str] = []
        for side in ("native", "federated"):
            result = attempt.get(side, {})
            outer = result.get("outer_worker", {})
            worker_id = outer.get("worker_id")
            worker_id = worker_id if isinstance(worker_id, str) and worker_id else None
            issues: list[str] = []
            if worker_id:
                pair_worker_ids.append(worker_id)
                if worker_id in seen_worker_ids:
                    issues.append("worker_id_reused")
                seen_worker_ids.add(worker_id)
            else:
                issues.append("worker_id_missing")
            if outer.get("cleanup_confirmed") is not True:
                issues.append("cleanup_unconfirmed")
            observation = result.get("capability_probe_observation")
            storage = observation.get("storage", {}) if isinstance(observation, dict) else {}
            relative_path = storage.get("relative_path")
            path_parts = PurePosixPath(relative_path).parts if isinstance(relative_path, str) else ()
            storage_valid = (
                isinstance(observation, dict)
                and observation.get("status") == "recorded"
                and isinstance(relative_path, str)
                and bool(path_parts)
                and not PurePosixPath(relative_path).is_absolute()
                and ".." not in path_parts
                and "phase4-probes" in path_parts
                and any(part.endswith("-data") for part in path_parts)
                and path_parts[-1] == "observations.ndjson"
                and storage.get("record_count") == len(expected)
                and isinstance(storage.get("file_sha256"), str)
                and len(storage["file_sha256"]) == 64
                and all(character in "0123456789abcdef" for character in storage["file_sha256"])
                and storage.get("probe_source_sha256") == expected_source_hashes[ecosystem]
            )
            if not storage_valid:
                issues.append("storage_evidence_invalid")
            rows = observation.get("events") if isinstance(observation, dict) else None
            clock_valid = isinstance(rows, list) and len(rows) == len(expected)
            if clock_valid:
                for actual, expected_row in zip(rows, expected, strict=True):
                    if not isinstance(actual, dict) or any(actual.get(key) != value for key, value in expected_row.items()):
                        clock_valid = False
                        break
            if not clock_valid:
                issues.append("event_scope_or_clock_mismatch")
            completed = result.get("status") == "completed"
            side_runs.append({
                "attempt_id": attempt_id,
                "side": side,
                "ecosystem": ecosystem,
                "worker_id": worker_id,
                "worker_completed": completed,
                "cleanup_confirmed": outer.get("cleanup_confirmed") is True,
                "observation_count": len(rows) if isinstance(rows, list) else 0,
                "storage": {
                    "valid": storage_valid,
                    "relative_path": relative_path if isinstance(relative_path, str) else None,
                    "file_sha256": storage.get("file_sha256"),
                    "probe_source_sha256": storage.get("probe_source_sha256"),
                },
                "clock_scope": {"valid": clock_valid, "matched_events": len(expected) if clock_valid else 0},
                "issues": issues,
            })
        if len(pair_worker_ids) == 2 and pair_worker_ids[0] == pair_worker_ids[1]:
            for row in side_runs[-2:]:
                row["issues"].append("native_federated_worker_reused")

    complete_run_count = len(side_runs)
    storage_ok = complete_run_count == expected_side_runs and all(
        row["storage"]["valid"] and row["cleanup_confirmed"] and row["worker_id"] for row in side_runs
    ) and len(seen_worker_ids) == expected_side_runs
    clock_ok = complete_run_count == expected_side_runs and all(row["clock_scope"]["valid"] for row in side_runs)
    if not storage_ok and complete_run_count < expected_side_runs:
        storage_status = "unknown"
    else:
        storage_status = "passed" if storage_ok else "failed"
    if not clock_ok and complete_run_count < expected_side_runs:
        clock_status = "unknown"
    else:
        clock_status = "passed" if clock_ok else "failed"
    storage_record_count = sum(row["observation_count"] for row in side_runs)
    return {
        "service": {"status": "passed", **service_evidence},
        "storage": {
            "status": storage_status,
            "probe_id": "phase4-storage-probe-v1",
            "expected_side_runs": expected_side_runs,
            "observed_side_runs": complete_run_count,
            "record_count": storage_record_count,
            "unique_worker_count": len(seen_worker_ids),
            "all_workers_cleaned": all(row["cleanup_confirmed"] for row in side_runs),
        },
        "clock": {
            "status": clock_status,
            "probe_id": "phase4-clock-probe-v1",
            "expected_side_runs": expected_side_runs,
            "observed_side_runs": complete_run_count,
            "matched_event_count": sum(row["clock_scope"]["matched_events"] for row in side_runs),
        },
        "side_run_evidence": side_runs,
    }


def build_phase4_evidence_manifest(campaign_report_path: Path) -> dict[str, Any]:
    """Build a terminal evidence index from a completed, hash-verified P4-01 campaign."""
    campaign_report_path = campaign_report_path.expanduser().resolve(strict=True)
    sidecar = campaign_report_path.with_suffix(campaign_report_path.suffix + ".sha256")
    recorded_hash = sidecar.read_text(encoding="ascii").split()[0]
    observed_hash = _sha256_file(campaign_report_path)
    if observed_hash != recorded_hash:
        raise ValueError("P4 campaign report SHA-256 sidecar mismatch")
    report = _read_json(campaign_report_path)
    if report.get("kind") != "phase4_p401_behavior_campaign" or report.get("status") != "completed":
        raise ValueError("P4 evidence manifest requires a completed behavior campaign")
    if report.get("gate", {}).get("p4_fixture_gate") != "passed" or report.get("gate", {}).get("p4_first_wave_behavior") != "passed_scoped":
        raise ValueError("P4 evidence manifest requires the fixture and scoped first-wave gates to pass")
    counts = report.get("counts", {})
    if counts != {
        "positions": 6,
        "attempts": 18,
        "side_executions": 36,
        "passed_attempts": 18,
        "failed_attempts": 0,
        "cleanup_unconfirmed": 0,
    }:
        raise ValueError("P4 evidence manifest refuses incomplete or failing first-wave denominators")
    if len(report.get("positions", [])) != 6 or any(
        position.get("status") != "passed" or len(position.get("attempts", [])) != 3
        for position in report.get("positions", [])
    ):
        raise ValueError("P4 evidence manifest requires six passed positions with three attempts each")
    if len(report.get("capability_probe_evidence", {}).get("side_runs", [])) != 36:
        raise ValueError("P4 evidence manifest requires all 36 Host side-probe records")
    if len({row.get("worker_id") for row in report["capability_probe_evidence"]["side_runs"]}) != 36:
        raise ValueError("P4 evidence manifest requires unique Worker IDs for all Host executions")
    if any(value.get("status") != "passed" for value in report.get("capability_probes", {}).values()):
        raise ValueError("P4 evidence manifest requires service, storage, and clock probes to pass")

    validation = validate_phase4_suite(verify_artifacts=True)
    suite_hash = _sha256_file(SUITE_PATH)
    matrix_hash = _sha256_file(MATRIX_PATH)
    if report.get("suite_sha256") != suite_hash or report.get("migration_matrix_sha256") != matrix_hash:
        raise ValueError("P4 campaign report does not match the current frozen suite and matrix")
    service_record = report["capability_probes"]["service"]
    service_path = ROOT / "outputs/acceptance" / Path(service_record["path"]).name
    service_evidence = validate_service_probe_report(service_path)
    if service_evidence["sha256"] != service_record.get("sha256"):
        raise ValueError("P4 campaign service report identity differs from the verified service probe")
    admission_path = ROOT / "outputs/acceptance" / Path(report["base_admission_path"]).name
    if _sha256_file(admission_path) != report.get("base_admission_sha256"):
        raise ValueError("P4 campaign admission report identity differs from the current evidence")
    matrix = _read(MATRIX_PATH)

    positions = []
    attempt_ledger = []
    for position in report["positions"]:
        positions.append({
            "registry_entry_id": position["registry_entry_id"],
            "package": position["package"],
            "version": position["version"],
            "artifact_sha256": position["artifact_sha256"],
            "profile_id": position["profile_id"],
            "profile_lock_sha256": position["profile_lock_sha256"],
            "case_id": position["case_id"],
            "case_class": position["case_class"],
            "oracle_sha256": position["oracle_sha256"],
            "status": position["status"],
            "attempt_ids": [attempt["attempt_id"] for attempt in position["attempts"]],
        })
        for attempt in position["attempts"]:
            attempt_ledger.append({
                "attempt_id": attempt["attempt_id"],
                "case_id": attempt["case_id"],
                "comparison": attempt["comparison"],
                "sides": {
                    side: {
                        "status": attempt[side].get("status"),
                        "worker_id": attempt[side].get("outer_worker", {}).get("worker_id"),
                        "worker_cleanup_confirmed": attempt[side].get("outer_worker", {}).get("cleanup_confirmed"),
                        "worker_stdout_sha256": attempt[side].get("outer_worker", {}).get("stdout_sha256"),
                        "worker_stderr_sha256": attempt[side].get("outer_worker", {}).get("stderr_sha256"),
                        "environment": attempt[side].get("environment"),
                        "trace": attempt[side].get("native_trace") or attempt[side].get("federated_trace"),
                    }
                    for side in ("native", "federated")
                },
            })
    service_path_relative = service_path.relative_to(ROOT).as_posix()
    return {
        "schema_version": 1,
        "kind": "phase4_p401_terminal_evidence_manifest",
        "manifest_status": "complete_scoped",
        "materialized_after_campaign": True,
        "campaign": {
            "campaign_id": report["campaign_id"],
            "report_path": campaign_report_path.relative_to(ROOT).as_posix(),
            "report_sha256": observed_hash,
            "started_at_utc": report.get("started_at_utc"),
            "completed_at_utc": report.get("completed_at_utc"),
        },
        "inputs": {
            "suite_path": SUITE_PATH.relative_to(ROOT).as_posix(),
            "suite_sha256": suite_hash,
            "migration_matrix_path": MATRIX_PATH.relative_to(ROOT).as_posix(),
            "migration_matrix_sha256": matrix_hash,
            "migration_matrix_validation": validation,
            "candidate_selection_revision": matrix.get("candidate_selection_revision"),
            "admission_path": admission_path.relative_to(ROOT).as_posix(),
            "admission_sha256": report["base_admission_sha256"],
            "service_probe_path": service_path_relative,
            "service_probe_sha256": service_evidence["sha256"],
            "runtime_image": report["runtime_image"],
            "asset_root": report["bundle_summary"]["bundle_root"],
            "bundle_summary": report["bundle_summary"],
        },
        "denominator": {"positions": 6, "attempts": 18, "side_executions": 36},
        "counts": counts,
        "capability_probes": report["capability_probes"],
        "gate": report["gate"],
        "positions": positions,
        "attempt_ledger": attempt_ledger,
        "side_probe_evidence": report["capability_probe_evidence"]["side_runs"],
        "terminal_checkpoint": {
            "status": "completed",
            "pending_positions": 0,
            "pending_attempts": 0,
            "cleanup_unconfirmed": 0,
        },
    }


def write_phase4_evidence_manifest(campaign_report_path: Path, output_path: Path) -> dict[str, Any]:
    output_path = output_path.expanduser().absolute()
    if output_path.exists() or output_path.with_suffix(output_path.suffix + ".sha256").exists():
        raise FileExistsError(f"refusing to overwrite an existing P4 evidence manifest: {output_path}")
    manifest = build_phase4_evidence_manifest(campaign_report_path)
    digest = _write_with_hash(output_path, manifest)
    return {"status": "written", "path": str(output_path), "sha256": digest, "campaign_id": manifest["campaign"]["campaign_id"]}


def execute_phase4_campaign(
    *,
    admission_path: Path,
    runtime_image: str,
    asset_root: Path,
    bundle_root: Path,
    output_path: Path,
    service_probe_report_path: Path,
    attempts_per_position: int = 3,
) -> dict[str, Any]:
    if attempts_per_position != 3:
        raise ValueError("P4-01 requires exactly three fresh attempts per position")
    service_evidence = validate_service_probe_report(service_probe_report_path)
    admission_path = admission_path.expanduser().resolve(strict=True)
    admission = _read_json(admission_path)
    if (
        admission.get("status") != "admitted"
        or admission.get("platform") != "linux"
        or admission.get("capabilities", {}).get("claim_allowed") is not True
        or admission.get("capabilities", {}).get("network_disabled") is not True
        or admission.get("capabilities", {}).get("cleanup_evidence_required") is not True
    ):
        raise RuntimeError("the current Linux Worker backend has no admitted base report")
    validation = validate_phase4_suite(SUITE_PATH, MATRIX_PATH, verify_artifacts=True)
    suite = _read(SUITE_PATH)
    bundles = prepare_bundles(asset_root, bundle_root, suite)
    image = runtime_image
    if "@sha256:" not in image:
        raise ValueError("P4 runtime image must be pinned by a sha256 digest")
    worker = DockerWorker(image_digest=image, repository_root=ROOT)
    runtime_probe = _runtime_probe(worker, bundle_root)
    started = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    campaign_id = f"p401-{int(time.time())}"
    report: dict[str, Any] = {
        "schema_version": 1,
        "kind": "phase4_p401_behavior_campaign",
        "campaign_id": campaign_id,
        "started_at_utc": started,
        "platform": "linux-x86_64",
        "orchestrator_platform": platform.system().lower(),
        "suite_id": suite["suite_id"],
        "suite_sha256": _sha256_file(SUITE_PATH),
        "migration_matrix_sha256": _sha256_file(MATRIX_PATH),
        "base_admission_path": str(admission_path),
        "base_admission_sha256": _sha256_file(admission_path),
        "runtime_image": image,
        "runtime_probe": runtime_probe,
        "contract_validation": validation,
        "bundle_summary": bundles,
        "capability_probes": {
            "service": {"status": "passed", **service_evidence},
            "storage": {"status": "pending", "probe_id": "phase4-storage-probe-v1"},
            "clock": {"status": "pending", "probe_id": "phase4-clock-probe-v1"},
        },
        "capability_probe_evidence": {"service": service_evidence},
        "attempts_per_position": attempts_per_position,
        "positions": [],
        "counts": {"positions": 0, "attempts": 0, "side_executions": 0, "passed_attempts": 0, "failed_attempts": 0, "cleanup_unconfirmed": 0},
        "status": "blocked",
        "gate": {"p4_fixture_gate": "blocked", "p4_first_wave_behavior": "not_evaluated"},
    }
    if runtime_probe["status"] != "passed":
        report["stop_reason"] = "runtime_image_probe_failed"
        _write_with_hash(output_path, report)
        return report

    entry_by_id = {entry["registry_entry_id"]: entry for entry in suite["entries"]}
    bundle_by_ecosystem = {row["ecosystem"]: (Path(row["path"]), row["asset_report"]) for row in bundles["entries"]}
    side_module = ROOT / "lab/automation/phase4_docker_side.py"
    observed_attempts: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    stop_for_cleanup = False
    for entry_id in sorted(RUNNABLE_IDS):
        entry = entry_by_id[entry_id]
        bundle, asset_report = bundle_by_ecosystem[entry["ecosystem"]]
        for case in entry["cases"]:
            if case.get("migration", {}).get("status") != "executable":
                continue
            position = {
                "registry_entry_id": entry_id,
                "package": entry["package"],
                "version": entry["version"],
                "artifact_sha256": entry["artifact_sha256"],
                "profile_id": entry["dependency_locks"]["linux-x86_64"]["profile_id"],
                "profile_lock_sha256": entry["dependency_locks"]["linux-x86_64"]["dependency_lock_sha256"],
                "case_id": case["case_id"],
                "case_class": case["class"],
                "oracle_sha256": _sha256_bytes(_canonical(case["oracle"])),
                "attempts": [],
            }
            for attempt_number in range(1, attempts_per_position + 1):
                attempt_id = f"{case['case_id']}-a{attempt_number}"
                common = [
                    sys.executable,
                    str(side_module),
                    "--image", image,
                    "--bundle", str(bundle),
                    "--run-id", campaign_id,
                    "--entry-id", entry_id,
                    "--attempt-id", attempt_id,
                    "--generation", str(attempt_number),
                ]
                native_command = [*common, "--side", "native"]
                federated_command = [*common, "--side", "federated"]
                attempt = run_behavior_pair(
                    entry,
                    case,
                    "linux-x86_64",
                    native_command,
                    federated_command,
                    fixture_context=_fixture_context(entry, case, bundle, asset_report),
                    timeout_seconds=240,
                )
                attempt["attempt_id"] = attempt_id
                attempt["fresh_worker_pair"] = True
                position["attempts"].append(attempt)
                observed_attempts.append((attempt_id, case, attempt))
                report["counts"]["attempts"] += 1
                report["counts"]["side_executions"] += 2
                if attempt["comparison"]["status"] == "passed":
                    report["counts"]["passed_attempts"] += 1
                else:
                    report["counts"]["failed_attempts"] += 1
                for side in ("native", "federated"):
                    outer = attempt.get(side, {}).get("outer_worker", {})
                    if outer.get("cleanup_confirmed") is not True:
                        report["counts"]["cleanup_unconfirmed"] += 1
                        stop_for_cleanup = True
                if stop_for_cleanup:
                    report["stop_reason"] = "worker_cleanup_unconfirmed"
                    break
            position["status"] = "passed" if len(position["attempts"]) == 3 and all(item["comparison"]["status"] == "passed" for item in position["attempts"]) else "failed"
            report["positions"].append(position)
            report["counts"]["positions"] += 1
            if stop_for_cleanup:
                break
        if stop_for_cleanup:
            break

    positions_complete = len(report["positions"]) == 6 and all(len(position["attempts"]) == 3 for position in report["positions"])
    all_passed = positions_complete and all(position["status"] == "passed" for position in report["positions"])
    capability_results = _phase4_capability_results(observed_attempts, service_evidence)
    report["capability_probes"] = {name: value for name, value in capability_results.items() if name != "side_run_evidence"}
    report["capability_probe_evidence"] = {
        "service": service_evidence,
        "side_runs": capability_results["side_run_evidence"],
    }
    capabilities_complete = all(value["status"] == "passed" for value in report["capability_probes"].values())
    report["completed_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    report["status"] = "completed" if positions_complete else "stopped"
    report["gate"] = {
        "p4_fixture_gate": "passed" if all_passed and capabilities_complete and report["counts"]["cleanup_unconfirmed"] == 0 else "blocked",
        "p4_first_wave_behavior": "passed_scoped" if all_passed and report["counts"]["cleanup_unconfirmed"] == 0 else ("failed" if report["counts"]["failed_attempts"] else "not_evaluated"),
    }
    if not capabilities_complete:
        report["gate"]["limitations"] = [f"{name}_capability_probe_{value['status']}" for name, value in report["capability_probes"].items() if value["status"] != "passed"]
    _write_with_hash(output_path, report)
    return report
