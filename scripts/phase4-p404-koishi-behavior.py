from __future__ import annotations

import argparse
import base64
import hashlib
import json
import shutil
import tarfile
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lab.automation.behavior_v2 import build_phase4_suite
from lab.automation.execution import _assertions, _canonical, _valid_identity
from lab.automation.phase4_campaign import _fixture_context
from lab.pilot_workers import (
    _entry_inputs,
    _load_catalog_context,
    _load_npm_lock,
    _prepare_npm_project,
)
from lab.workers import WindowsSandboxWorker, WorkerLimits, WorkerSpec

ENTRY_ID = "ko-163c3bd4cf87c72c24210251"
DEFAULT_CASE_ID = "ko-koishijs-plugin-echo-isolation"
CASE_IDS = (
    "ko-koishijs-plugin-echo-positive",
    "ko-koishijs-plugin-echo-negative",
    "ko-koishijs-plugin-echo-isolation",
)
NODE_SHA256 = "5c976096e04e5c2c1f091938926234cc9fbebfe9787ddd149351b3b0ecc707b5"
HOST_LOCK_PATH = ROOT / "hosts" / "koishi" / "package-lock.json"
HOST_LOCK_SHA256 = "5e3df82b0c353a8ded6c32708a8a591d9854182a38c6af9924c3e2671862bbcb"
BASE_POLICY_GUID = "{0283ac0f-fff1-49ae-ada1-8a933130cad6}"
BASE_POLICY_HASH = "2668895A5B233A80432D00D67251D7B7F52686A3FB13780F4B242C5A1F937A01"
BASE_POLICY_OPTIONS = "0xb18c2714"
PACKAGE = "@koishijs/plugin-echo"
VERSION = "2.2.5"
CACHE_ROOT = Path(r"E:\stella\Ligamina-artifact-cache")
SNAPSHOT = ROOT / "catalogs" / "p2-41649744e057d61031d5"
PACKAGE_ROOT = ROOT / "deploy" / "windows" / "phase4-p404-koishi-behavior"
SOURCE_FILES = (
    "lab/baselines/koishi/runner.mjs",
    "lab/baselines/koishi/phase4_probe_plugin.mjs",
    "lab/baselines/koishi/artifact_fingerprint.mjs",
)
PROVIDER_PACKAGES = (
    "@minatojs/driver-sqlite",
    "@minatojs/sql-utils",
    "@minatojs/sql.js",
)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    path.with_suffix(path.suffix + ".sha256").write_text(f"{digest}  {path.name}\n", encoding="ascii")
    return digest


def profile_lock_digest(lock: dict[str, Any], raw: bytes) -> tuple[str, str]:
    record = lock.get("integrity")
    if not isinstance(record, str) or not record.startswith("sha512-"):
        raise ValueError("host provider package has no frozen SHA-512 integrity")
    digest = base64.b64decode(record.split("-", 1)[1], validate=True).hex()
    return digest, "sha512"


def extend_profile_with_host_provider(
    lock: dict[str, Any], *, project_dir: Path, cache_root: Path,
) -> dict[str, Any]:
    host_lock_raw = HOST_LOCK_PATH.read_bytes()
    host_lock_sha = hashlib.sha256(host_lock_raw).hexdigest()
    if host_lock_sha != HOST_LOCK_SHA256:
        raise ValueError("Koishi Host provider lock changed from the frozen P4-04 identity")
    host_lock = json.loads(host_lock_raw)
    combined = json.loads(json.dumps(lock))
    combined_packages = combined["packages"]
    root_record = combined_packages[""]
    dependencies = root_record.setdefault("dependencies", {})
    if dependencies.get("@minatojs/driver-sqlite") not in (None, "4.7.0"):
        raise ValueError("catalog profile declares a conflicting SQLite driver version")
    dependencies["@minatojs/driver-sqlite"] = "4.7.0"
    package_json_path = project_dir / "package.json"
    package_json = json.loads(package_json_path.read_text(encoding="utf-8"))
    package_json.setdefault("dependencies", {})["@minatojs/driver-sqlite"] = "4.7.0"

    existing_packages = lock["packages"]
    for name in PROVIDER_PACKAGES:
        key = "node_modules/" + name
        if key in existing_packages:
            raise ValueError(f"provider package unexpectedly overlaps the frozen catalog lock: {name}")
        record = json.loads(json.dumps(host_lock["packages"][key]))
        url = record.get("resolved")
        integrity = record.get("integrity")
        if not isinstance(url, str) or not isinstance(integrity, str):
            raise ValueError(f"Koishi host provider lock is incomplete for {name}")
        digest_hex, algorithm = profile_lock_digest(record, host_lock_raw)
        archive = cache_root / "dependency-archives" / "npm" / algorithm / digest_hex
        if not archive.is_file():
            raise ValueError(f"locked host provider archive is missing from the external cache: {name}")
        actual = hashlib.new(algorithm, archive.read_bytes()).hexdigest()
        if actual != digest_hex:
            raise ValueError(f"locked host provider archive integrity differs: {name}")
        target = project_dir / "tarballs" / algorithm / f"{digest_hex}.tgz"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copyfile(archive, target)
        record["resolved"] = f"file:tarballs/{algorithm}/{digest_hex}.tgz"
        combined_packages[key] = record

    # The catalog profile already freezes the compatible peer and shared runtime packages.
    for name, expected_version in (("minato", "3.7.0"), ("cosmokit", "1.8.1")):
        key = "node_modules/" + name
        host_record = host_lock["packages"].get(key)
        catalog_record = combined_packages.get(key)
        if (
            not isinstance(host_record, dict)
            or not isinstance(catalog_record, dict)
            or host_record.get("version") != expected_version
            or catalog_record.get("version") != expected_version
            or host_record.get("integrity") != catalog_record.get("integrity")
        ):
            raise ValueError(f"catalog and Host provider locks do not share the expected {name} identity")

    package_json_path.write_text(json.dumps(package_json, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    combined_raw = (json.dumps(combined, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    lock_path = project_dir / "package-lock.json"
    lock_path.write_bytes(combined_raw)

    provider_archive_review = {"package_count": 0, "archive_sha512": {}, "native_members": [], "install_scripts": []}
    for name in PROVIDER_PACKAGES:
        key = "node_modules/" + name
        record = host_lock["packages"][key]
        digest_hex, algorithm = profile_lock_digest(record, host_lock_raw)
        archive = cache_root / "dependency-archives" / "npm" / algorithm / digest_hex
        provider_archive_review["package_count"] += 1
        provider_archive_review["archive_sha512"][name] = digest_hex
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar.getmembers():
                lower_name = member.name.lower()
                if lower_name.endswith((".node", ".dll", ".exe", ".so", ".dylib")) or lower_name.endswith("binding.gyp"):
                    provider_archive_review["native_members"].append(f"{name}:{member.name}")
                if lower_name.endswith("package.json"):
                    stream = tar.extractfile(member)
                    if stream:
                        package_doc = json.loads(stream.read().decode("utf-8"))
                        scripts = package_doc.get("scripts", {})
                        if any(key in scripts for key in ("preinstall", "install", "postinstall")):
                            provider_archive_review["install_scripts"].append(name)

    return {
        "host_lock_sha256": host_lock_sha,
        "combined_package_lock_sha256": hashlib.sha256(combined_raw).hexdigest(),
        "combined_package_lock": combined,
        "combined_raw": combined_raw,
        "provider_package_versions": {name: host_lock["packages"]["node_modules/" + name]["version"] for name in PROVIDER_PACKAGES},
        "provider_archive_review": provider_archive_review,
    }


def frozen_case(case_id: str = DEFAULT_CASE_ID) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    suite, _ = build_phase4_suite()
    entry = next((row for row in suite["entries"] if row["registry_entry_id"] == ENTRY_ID), None)
    if not isinstance(entry, dict):
        raise ValueError(f"frozen P4 suite is missing entry {ENTRY_ID}")
    if case_id not in CASE_IDS:
        raise ValueError(f"case id is not in the reviewed Koishi Echo fixture allowlist: {case_id}")
    case = next((row for row in entry["cases"] if row["case_id"] == case_id), None)
    if not isinstance(case, dict) or case.get("migration", {}).get("status") != "executable":
        raise ValueError(f"frozen P4 case is not executable: {case_id}")
    normalized = {**case, "stimuli": [case["stimulus"]], "assertions": case["oracle"]["assertions"]}
    identity = _valid_identity(entry, normalized, "windows-x86_64")
    fixture_digest = hashlib.sha256(_canonical({
        "stimuli": normalized["stimuli"],
        "assertions": normalized["assertions"],
        "oracle": case.get("oracle"),
    })).hexdigest()
    return entry, case, {**identity, "fixture_sha256": fixture_digest}


def make_request(entry: dict[str, Any], case: dict[str, Any], binding: dict[str, Any], project: Path) -> dict[str, Any]:
    context = _fixture_context(entry, case, project, {})
    guest_project = r"C:\Ligamina\Workspace\project"
    guest_workspace = r"C:\Ligamina\Workspace\run"
    context["repo_root"] = guest_project
    context["work_dir"] = guest_project
    context["node_executable"] = r"C:\Ligamina\Workspace\runtime\node.exe"
    context["binding"] = binding
    context["execution"] = {"mode": "native", "workspace": guest_workspace, **case["determinism"]}
    context["behavior_fixture"] = {
        key: case[key] for key in ("adapter", "setup", "determinism", "observation")
    }
    context["fingerprint_paths"] = {
        "artifact": guest_project + r"\catalog-plugin.tgz",
        "lockfile": guest_project + r"\frozen-profile-lock.json",
        "profile": guest_project + r"\samples\profiles\koishi-group-v0.1.json",
    }
    context["fixture_context_sha256"] = hashlib.sha256(_canonical(context)).hexdigest()
    return {"case_id": case["case_id"], "stimulus": case["stimulus"], "context": context}


def parse_worker_output(stdout: bytes) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    guest_result = None
    code_integrity = None
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        if line.startswith("LIGAMINA_P404_KOISHI="):
            try:
                guest_result = json.loads(line.split("=", 1)[1])
            except json.JSONDecodeError:
                pass
        elif line.startswith("LIGAMINA_CODEINTEGRITY="):
            try:
                code_integrity = json.loads(line.split("=", 1)[1])
            except json.JSONDecodeError:
                pass
    return guest_result, code_integrity


def policy_activation_matches(events: dict[str, Any] | None) -> bool:
    if not isinstance(events, dict):
        return False
    for row in events.get("events", []):
        if row.get("id") != 3099 or not isinstance(row.get("xml"), str):
            continue
        try:
            import xml.etree.ElementTree as ET

            root = ET.fromstring(row["xml"])
        except ET.ParseError:
            continue
        values = {
            item.attrib.get("Name"): item.text
            for item in root.iter()
            if item.tag.endswith("Data") and item.attrib.get("Name")
        }
        if (
            values.get("PolicyGUID") == BASE_POLICY_GUID
            and (values.get("Options") or "").lower() == BASE_POLICY_OPTIONS
            and values.get("PolicyHash") == BASE_POLICY_HASH
        ):
            return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one frozen Koishi P4 case through the Windows Sandbox Host dispatch path.")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case-id", choices=CASE_IDS, default=DEFAULT_CASE_ID)
    parser.add_argument("--cache-root", type=Path, default=CACHE_ROOT)
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument(
        "--profile-lock-output",
        type=Path,
        default=ROOT / "outputs/acceptance/phase4-p404-koishi-combined-package-lock-20261028.json",
    )
    args = parser.parse_args()
    started = datetime.now(timezone.utc).isoformat()
    cache_root = args.cache_root.resolve(strict=True)
    case_id = args.case_id
    entry, case, binding = frozen_case(case_id)
    context = _load_catalog_context(SNAPSHOT, "windows")
    unit, _catalog_entry, artifact_record, lock_ref, artifact_lock_raw, profile_lock_raw = _entry_inputs(
        SNAPSHOT, ENTRY_ID, "windows", context=context,
    )
    artifact_path = cache_root / artifact_record["cache_path"]
    lock_path = cache_root / lock_ref["cache_path"]
    lock, lock_raw = _load_npm_lock(lock_path, lock_ref)
    artifact_hash = sha256_file(artifact_path)
    if (
        entry["package"] != PACKAGE
        or entry["version"] != VERSION
        or artifact_hash != entry["artifact_sha256"]
        or lock_ref["sha256"] != entry["dependency_locks"]["windows-x86_64"]["dependency_lock_sha256"]
    ):
        raise ValueError("catalog artifact, selected P4 entry, or Windows dependency lock identity differs")

    runtime_root = cache_root / "p305-koishi-lock-runtime"
    node_source = runtime_root / "node.exe"
    npm_source = runtime_root / "node_modules" / "npm"
    if sha256_file(node_source) != NODE_SHA256 or not (npm_source / "package.json").is_file():
        raise ValueError("cached signed Node 24.20.0 runtime or bundled npm is missing or changed")
    npm_version = json.loads((npm_source / "package.json").read_text(encoding="utf-8")).get("version")

    with tempfile.TemporaryDirectory(prefix="ligamina-p404-koishi-behavior-", dir=cache_root) as temporary:
        bundle = Path(temporary)
        project = bundle / "project"
        dependency_stats = _prepare_npm_project(
            lock, package_name=PACKAGE, cache_root=cache_root, project_dir=project,
        )
        localized_lock = json.loads((project / "package-lock.json").read_text(encoding="utf-8"))
        provider_profile = extend_profile_with_host_provider(localized_lock, project_dir=project, cache_root=cache_root)
        lock_output = args.profile_lock_output.resolve()
        if lock_output.exists():
            if lock_output.read_bytes() != provider_profile["combined_raw"]:
                raise ValueError("existing combined Koishi profile lock differs; choose a new --profile-lock-output")
        else:
            lock_output.parent.mkdir(parents=True, exist_ok=True)
            lock_output.write_bytes(provider_profile["combined_raw"])
        lock_sidecar = lock_output.with_suffix(lock_output.suffix + ".sha256")
        if lock_sidecar.exists():
            if lock_sidecar.read_text(encoding="ascii").split()[0] != provider_profile["combined_package_lock_sha256"]:
                raise ValueError("existing combined Koishi profile lock sidecar differs")
        else:
            lock_sidecar.write_text(
                f"{provider_profile['combined_package_lock_sha256']}  {lock_output.name}\n", encoding="ascii"
            )
        if sha256_file(lock_output) != provider_profile["combined_package_lock_sha256"]:
            raise ValueError("combined Koishi profile lock sidecar identity differs")
        dependency_stats = {
            **dependency_stats,
            "catalog_locked_package_count": dependency_stats["locked_package_count"],
            "host_provider_package_count": len(PROVIDER_PACKAGES),
            "locked_package_count": dependency_stats["locked_package_count"] + len(PROVIDER_PACKAGES),
            "combined_package_lock_sha256": provider_profile["combined_package_lock_sha256"],
            "host_provider_lock_sha256": provider_profile["host_lock_sha256"],
        }
        shutil.copyfile(artifact_path, project / "catalog-plugin.tgz")
        shutil.copyfile(lock_path, project / "frozen-profile-lock.json")
        profile_source = ROOT / "samples" / "profiles" / "koishi-group-v0.1.json"
        profile_target = project / "samples" / "profiles" / profile_source.name
        profile_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(profile_source, profile_target)
        for relative in SOURCE_FILES:
            target = project / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, target)
        runtime = bundle / "runtime"
        runtime.mkdir()
        shutil.copyfile(node_source, runtime / "node.exe")
        shutil.copytree(npm_source, runtime / "node_modules" / "npm")
        shutil.copyfile(PACKAGE_ROOT / "Run-KoishiBehavior.ps1", bundle / "Run-KoishiBehavior.ps1")
        shutil.copyfile(PACKAGE_ROOT / "Execute-KoishiBehavior.mjs", bundle / "Execute-KoishiBehavior.mjs")
        request_path = bundle / "request.json"
        request = make_request(entry, case, binding, project)
        request_path.write_text(json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")

        worker = WindowsSandboxWorker(
            repository_root=ROOT,
            temporary_root=cache_root / "s209-windows-worker-temp",
            collect_script_path=PACKAGE_ROOT / "Collect-CodeIntegrityOutput.ps1",
        )
        worker_result = worker.run(WorkerSpec(
            run_id=f"phase4-p404-koishi-{case_id.rsplit('-', 1)[-1]}",
            entry_id=ENTRY_ID,
            command=(
                r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
                "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", r"C:\Ligamina\Input\Run-KoishiBehavior.ps1",
            ),
            input_dir=bundle,
            limits=WorkerLimits(
                timeout_seconds=args.timeout_seconds,
                memory_mb=16384,
                cpu_cores=2,
                process_limit=256,
            ),
        ))
        guest_result, code_integrity = parse_worker_output(worker_result.stdout)
        native_report = guest_result.get("native_report") if isinstance(guest_result, dict) else None
        observation = native_report.get("plugin_observation") if isinstance(native_report, dict) else None
        normalized_case = {**case, "assertions": case["oracle"]["assertions"]}
        oracle_issues = _assertions(normalized_case, observation, "native") if isinstance(observation, dict) else [
            {"code": "observations_missing", "side": "native"}
        ]
        binding_matches = isinstance(native_report, dict) and native_report.get("binding") == binding
        policy_matches = policy_activation_matches(code_integrity)
        if worker_result.status == "unknown" or not worker_result.cleanup_confirmed:
            status, reason = "unknown", worker_result.reason_code or "worker_cleanup_unconfirmed"
        elif worker_result.status != "passed":
            status, reason = "unknown", worker_result.reason_code or "worker_execution_failed"
        elif not policy_matches:
            status, reason = "unknown", "current_enforcement_policy_activation_not_observed"
        elif not binding_matches:
            status, reason = "failed", "behavior_binding_mismatch"
        elif oracle_issues:
            status, reason = "failed", "behavior_oracle_mismatch"
        elif not isinstance(native_report, dict) or native_report.get("status") != "completed":
            status, reason = "unknown", "stock_native_runner_not_completed"
        else:
            status, reason = "passed_native_scoped", None

        source_hashes = {
            "scripts/phase4-p404-koishi-behavior.py": sha256_file(ROOT / "scripts/phase4-p404-koishi-behavior.py"),
            **{relative: sha256_file(ROOT / relative) for relative in SOURCE_FILES},
        }
        source_hashes.update({
            str((PACKAGE_ROOT / name).relative_to(ROOT)).replace("\\", "/"): sha256_file(PACKAGE_ROOT / name)
            for name in ("Run-KoishiBehavior.ps1", "Execute-KoishiBehavior.mjs", "Collect-CodeIntegrityOutput.ps1")
        })
        report = {
            "schema_version": 1,
            "kind": "phase4_p404_koishi_windows_native_behavior",
            "started_at_utc": started,
            "completed_at_utc": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "reason_code": reason,
            "scope": "single native Koishi Host dispatch behavior case in Windows Sandbox; no Federated parity or full catalog claim",
            "catalog": {
                "snapshot": "catalogs/p2-41649744e057d61031d5",
                "registry_entry_id": ENTRY_ID,
                "package": PACKAGE,
                "version": VERSION,
                "artifact_sha256": artifact_hash,
                "artifact_lock_sha256": hashlib.sha256(artifact_lock_raw).hexdigest(),
                "profile_document_sha256": hashlib.sha256(profile_lock_raw).hexdigest(),
                "dependency_lock_sha256": hashlib.sha256(lock_raw).hexdigest(),
                "profile_id": unit["profile_id"],
                "dependency_archives": dependency_stats,
            },
            "host_provider_profile": {
                "host_package_lock_path": "hosts/koishi/package-lock.json",
                "host_package_lock_sha256": provider_profile["host_lock_sha256"],
                "combined_package_lock_path": str(lock_output.relative_to(ROOT)).replace("\\", "/"),
                "combined_package_lock_sha256": provider_profile["combined_package_lock_sha256"],
                "provider_package_versions": provider_profile["provider_package_versions"],
                "provider_archive_review": provider_profile["provider_archive_review"],
            },
            "behavior_fixture": {
                "case_id": case_id,
                "fixture_binding": binding,
                "adapter": case["adapter"],
                "determinism": case["determinism"],
                "oracle_source_member": case["oracle"]["source_member"],
                "oracle_assertions": case["oracle"]["assertions"],
                "issues": oracle_issues,
            },
            "runtime": {
                "node_sha256": sha256_file(node_source),
                "node_authenticode_expected": "Valid / OpenJS Foundation (guest runner verified the signature)",
                "npm_version": npm_version,
                "npm_lifecycle_scripts": "disabled",
                "network": "disabled by Windows Sandbox policy",
            },
            "policy": {
                "baseline_report": "outputs/acceptance/phase4-p404-policy-preflight-20261028.json",
                "observed_current_base_policy_activation": policy_matches,
                "expected_base_policy_guid": BASE_POLICY_GUID,
                "expected_base_policy_hash": BASE_POLICY_HASH,
                "expected_base_policy_options": BASE_POLICY_OPTIONS,
                "citool_full_inventory": "unknown; CiTool returns 0x80073BC3",
                "codeintegrity_collection": code_integrity,
            },
            "execution": {
                "worker_status": worker_result.status,
                "worker_reason_code": worker_result.reason_code,
                "worker_id": worker_result.worker_id,
                "worker_exit_code": worker_result.exit_code,
                "cleanup_confirmed": worker_result.cleanup_confirmed,
                "elapsed_seconds": round(worker_result.elapsed_seconds, 3),
                "peak_memory_bytes": worker_result.peak_memory_bytes,
                "stdout_sha256": worker_result.stdout_sha256,
                "stderr_sha256": worker_result.stderr_sha256,
                "stdout_tail": worker_result.stdout.decode("utf-8", errors="replace")[-12000:],
                "stderr_tail": worker_result.stderr.decode("utf-8", errors="replace")[-6000:],
                "guest_result": guest_result,
            },
            "source_sha256": source_hashes,
            "limitations": [
                "One Native Host execution only; repeat as three fresh Sandboxes before any Windows behavior gate claim.",
                "The report covers only the selected fixture; other cases for this catalog entry remain separate.",
                "Federated Core-to-Host behavior and comparison were not run.",
                "CiTool policy inventory remains unavailable; activation is corroborated by the current 3099 event and the separately recorded matching 3077 enforcement event.",
                "The catalog entry remains a pilot identity; this does not authorize broader catalog execution.",
            ],
        }
        digest = write_json(args.output.resolve(), report)
        print(json.dumps({
            "path": str(args.output.resolve()),
            "sha256": digest,
            "status": status,
            "reason_code": reason,
            "worker_status": worker_result.status,
            "cleanup_confirmed": worker_result.cleanup_confirmed,
            "policy_activation_matches": policy_matches,
            "oracle_issue_count": len(oracle_issues),
        }, ensure_ascii=False, indent=2))
        return 0 if status == "passed_native_scoped" else 1


if __name__ == "__main__":
    raise SystemExit(main())
