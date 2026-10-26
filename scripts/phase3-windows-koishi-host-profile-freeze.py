"""Freeze per-entry Koishi Windows host profiles for P3-05.

This resolver creates package-lock-only locks from exact catalog identities and
hash-checks every direct package against the frozen artifact index. It never
installs packages or runs package code. Registry archives referenced by the
resulting locks are copied only to the external content-addressed cache after
their SRI values verify. Entries with a frozen, unresolvable host/source
requirement receive an explicit pre-dispatch terminal classification.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
ACCEPTANCE = ROOT / "outputs/acceptance"
SNAPSHOT = ROOT / "catalogs/p2-41649744e057d61031d5"
CACHE_ROOT = Path(r"E:\stella\Ligamina-artifact-cache")
PROFILE_LOCK_PATH = ACCEPTANCE / "phase3-p305-windows-koishi-evaluation-profile-lock-v1-20261026.json"
PREFLIGHT_PATH = ACCEPTANCE / "phase3-p305-windows-koishi-evaluation-profile-preflight-v1-20261026.json"
MANIFEST_PATH = ACCEPTANCE / "phase3-p305-windows-koishi-evaluation-manifest-v1-20261026.json"
MATRIX_PATH = ACCEPTANCE / "phase3-p305-windows-koishi-entry-requirements-matrix-v1-20261026.json"
RESOLUTION_PATH = ACCEPTANCE / "phase3-p305-windows-koishi-provider-resolution-audit-v1-20261026.json"
CLOSURE_PATH = ACCEPTANCE / "phase3-p305-windows-koishi-provider-closure-audit-v1-20261026.json"
SOURCE_PATH = ACCEPTANCE / "phase3-p305-windows-koishi-service-source-audit-v1-20261026.json"
PROFILE_SOURCE_PATH = SNAPSHOT / "profiles.lock.json"
ARTIFACT_LOCK_PATH = SNAPSHOT / "artifacts.lock.json"
RECOVERY_PATH = ACCEPTANCE / "phase3-p301-recovery-manifest-20261020.json"
PILOT_MANIFEST_PATH = ACCEPTANCE / "phase3-p305-windows-pilot-manifest-v2-20261025.json"
PILOT_REPORT_PATH = ACCEPTANCE / "phase3-p305-windows-pilot-v2-execution-20261025.json"
NONEBOT_MANIFEST_PATH = ACCEPTANCE / "phase3-p305-windows-nonebot-evaluation-manifest-20261025.json"
ADMISSION_PATH = ACCEPTANCE / "phase3-p304-windows-admission-python31210-20261025.json"
POLICY_REPORT_PATH = ACCEPTANCE / "phase3-p305-windows-default-evaluation-preflight-20261025" / "preflight-report.json"

CORE = "@koishijs/core"
CORE_VERSION = "4.18.11"
NODE_RUNTIME = {
    "version": "24.20.0",
    "archive_url": "https://nodejs.org/dist/v24.20.0/node-v24.20.0-win-x64.zip",
    "archive_sha256": "6cac9ffbca8f6a47091e4b5c772e0606049c3871cb67d900c0cedde630e545ba",
    "node_executable_sha256": "5c976096e04e5c2c1f091938926234cc9fbebfe9787ddd149351b3b0ecc707b5",
}
POLICY_ID = "windows-sandbox-default-sac-evaluation-v1"
BASE_GUID = "{1283ac0f-fff1-49ae-ada1-8a933130cad6}"
SUPPLEMENTAL_GUID = "{2678656c-05ef-481f-bc5b-ebd8c991502d}"

# Provider choices are exact catalog identities from the frozen candidate and
# source-crosswalk reports. Service names absent here are either Core built-ins
# or intentionally classified before dispatch.
SERVICE_PROVIDER_IDS = {
    **{name: "ko-429c6634899115798dfa92ab" for name in (
        "BiliBiliLive", "BiliBiliLogin", "BiliBiliMovie", "BiliBiliSearch", "BiliBiliVideo"
    )},
    "QhzySharp": "ko-31a0a85bbbf89e5f2955070e",
    "assets": "ko-bf875ca9163b0dcaf037c26f",
    "canvas": "ko-1f0a60eb24250c958c406c43",
    "chatluna": "ko-66dc9d9768acc226c523fdb6",
    "console": "ko-ff7131d86d452213eb0350ce",
    "cron": "ko-8ebf383d9a00ea24c0dad5a2",
    "database": "ko-425f40033e5723525e57e410",
    "ffmpeg": "ko-0dba6420b86c5eabd90254d4",
    "fonts": "ko-d5d43667d9446f27dd1b1433",
    "localstorage": "ko-187be45372ea214c5d8fce80",
    "logger": "ko-72935a6f46ef54cadc949aa3",
    "milvus": "ko-f02d20177f0df4b6977781e6",
    "monetary": "ko-8a094788cb8c30923a5cfe9d",
    "nazrin": "ko-8ab0ce43e89bc209361f3cf7",
    "notifier": "ko-d15ce50af3a34319d68fd103",
    "puppeteer": "ko-ae7966cfbc28d4e180b086e7",
    "server": "ko-6e1c2637c442783d5161a773",
    "server.temp": "ko-457b69bf0da1b4711754bf80",
    "skia": "ko-68501ea7ad6acec9815245ef",
    "subscription": "ko-a0d7f1d397a89d3676bd5842",
}
NODE_PROVIDER_ID = "ko-e449e2096b9b92bd46f5c2b0"

# Source-verified required injections of selected providers. Optional services
# are deliberately not made mandatory. These edges are recursively closed.
PROVIDER_REQUIRED_SERVICES = {
    "ko-429c6634899115798dfa92ab": ["database"],  # BiliBili service
    "ko-31a0a85bbbf89e5f2955070e": ["node"],      # QhzySharp
    "ko-0dba6420b86c5eabd90254d4": ["logger"],    # ffmpeg service
    "ko-d5d43667d9446f27dd1b1433": ["database"],  # fonts service
    "ko-8a094788cb8c30923a5cfe9d": ["database"],  # monetary service
    "ko-8ab0ce43e89bc209361f3cf7": ["puppeteer"], # nazrin service
    "ko-ff7131d86d452213eb0350ce": ["server"],   # console service
    "ko-457b69bf0da1b4711754bf80": ["server"],   # server.temp service
}
PROVIDER_SERVICES = {
    "ko-429c6634899115798dfa92ab": ["BiliBiliLive", "BiliBiliLogin", "BiliBiliMovie", "BiliBiliSearch", "BiliBiliVideo"],
    "ko-31a0a85bbbf89e5f2955070e": ["QhzySharp"],
    "ko-bf875ca9163b0dcaf037c26f": ["assets"],
    "ko-1f0a60eb24250c958c406c43": ["canvas"],
    "ko-66dc9d9768acc226c523fdb6": ["chatluna"],
    "ko-ff7131d86d452213eb0350ce": ["console"],
    "ko-8ebf383d9a00ea24c0dad5a2": ["cron"],
    "ko-425f40033e5723525e57e410": ["database"],
    "ko-0dba6420b86c5eabd90254d4": ["ffmpeg"],
    "ko-d5d43667d9446f27dd1b1433": ["fonts"],
    "ko-187be45372ea214c5d8fce80": ["localstorage"],
    "ko-72935a6f46ef54cadc949aa3": ["logger"],
    "ko-f02d20177f0df4b6977781e6": ["milvus"],
    "ko-8a094788cb8c30923a5cfe9d": ["monetary"],
    "ko-8ab0ce43e89bc209361f3cf7": ["nazrin"],
    "ko-d15ce50af3a34319d68fd103": ["notifier"],
    "ko-ae7966cfbc28d4e180b086e7": ["puppeteer"],
    "ko-6e1c2637c442783d5161a773": ["server"],
    "ko-457b69bf0da1b4711754bf80": ["server.temp"],
    "ko-68501ea7ad6acec9815245ef": ["skia"],
    "ko-a0d7f1d397a89d3676bd5842": ["subscription"],
    NODE_PROVIDER_ID: ["node"],
}
GIT_SSH_TARGETS = {
    "ko-6fe5b7f33ce97eb5093b3005": "koishi-plugin-capture-website@1.5.5",
    "ko-955c6fd1bf22bd37901a4244": "koishi-plugin-gfm@1.0.1",
}
MARKET_BLOCKER_SERVICE = "market"


class FreezeError(RuntimeError):
    pass


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def canonical_sha256(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return sha256(raw)


def pretty_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")


def read_json(path: Path) -> tuple[Any, bytes]:
    raw = path.read_bytes()
    return json.loads(raw.decode("utf-8")), raw


def verify_sidecar(path: Path) -> str:
    actual = sha256(path.read_bytes())
    sidecar = path.with_name(path.name + ".sha256")
    if not sidecar.is_file() or sidecar.read_text(encoding="ascii").split()[0] != actual:
        raise FreezeError(f"SHA-256 sidecar mismatch: {path}")
    return actual


def verify_canonical_report(path: Path) -> tuple[dict[str, Any], str, str]:
    value, raw = read_json(path)
    if not isinstance(value, dict):
        raise FreezeError(f"expected report object: {path}")
    canonical = canonical_sha256({key: item for key, item in value.items() if key != "report_sha256"})
    if value.get("report_sha256") != canonical:
        raise FreezeError(f"canonical report hash mismatch: {path}")
    return value, canonical, verify_sidecar(path)


def write_immutable(path: Path, raw: bytes) -> str:
    sidecar = path.with_name(path.name + ".sha256")
    if path.exists() or sidecar.exists():
        raise FreezeError(f"refusing to overwrite frozen output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    file_sha = sha256(raw)
    sidecar.write_text(f"{file_sha}  {path.name}\n", encoding="ascii")
    return file_sha


def checked_lock_cache(cache_root: Path, raw: bytes) -> dict[str, Any]:
    digest = sha256(raw)
    relative = Path("profile-locks") / "koishi" / "sha256" / digest[:2] / digest
    destination = cache_root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file():
        if sha256(destination.read_bytes()) != digest:
            raise FreezeError(f"content-addressed lock cache mismatch: {destination}")
    else:
        temp = destination.with_suffix(f".{os.getpid()}.tmp")
        temp.write_bytes(raw)
        try:
            temp.replace(destination)
        finally:
            temp.unlink(missing_ok=True)
    return {"cache_path": relative.as_posix(), "sha256": digest, "size_bytes": len(raw)}


def lock_command_inputs(cache_root: Path) -> tuple[Path, Path, Path]:
    from lab.pilot_workers import _download_windows_node_archive, _extract_windows_node_runtime

    archive = _download_windows_node_archive(cache_root)
    temp_root = cache_root / "p305-koishi-lock-runtime"
    if temp_root.exists():
        node_existing = temp_root / "node.exe"
        if node_existing.is_file() and sha256(node_existing.read_bytes()) == NODE_RUNTIME["node_executable_sha256"]:
            npm_json = json.loads((temp_root / "node_modules" / "npm" / "package.json").read_text(encoding="utf-8"))
            return node_existing, temp_root / "node_modules" / "npm" / "bin" / "npm-cli.js", Path(npm_json["version"])
        raise FreezeError(f"incomplete pinned lock-resolution runtime directory: {temp_root}")
    _extract_windows_node_runtime(archive, temp_root)
    node = temp_root / "node.exe"
    npm_cli = temp_root / "node_modules" / "npm" / "bin" / "npm-cli.js"
    npm_json = json.loads((temp_root / "node_modules" / "npm" / "package.json").read_text(encoding="utf-8"))
    if sha256(node.read_bytes()) != NODE_RUNTIME["node_executable_sha256"]:
        raise FreezeError("pinned Windows Node executable hash mismatch")
    return node, npm_cli, Path(npm_json["version"])


def artifact_record(artifacts: dict[str, Any], entry_id: str) -> dict[str, Any]:
    entry = artifacts.get(entry_id)
    if not isinstance(entry, dict):
        raise FreezeError(f"missing frozen artifact entry: {entry_id}")
    result = next((row.get("artifact") for row in entry.get("artifacts", []) if row.get("platform") == "all"), None)
    if not isinstance(result, dict) or not isinstance(result.get("computed_digests", {}).get("sha256"), str):
        raise FreezeError(f"missing exact all-platform target archive: {entry_id}")
    return result


def verify_direct_hash(lock: dict[str, Any], package: str, version: str, artifact: dict[str, Any], entry_id: str) -> None:
    from lab.pilot_workers import _integrity_values

    record = lock.get("packages", {}).get(f"node_modules/{package}")
    if not isinstance(record, dict) or record.get("version") != version:
        raise FreezeError(f"combined npm lock lacks exact direct package/version for {entry_id}: {package}@{version}")
    expected = artifact.get("computed_digests", {}).get("sha512")
    if not isinstance(expected, str) or not any(algo == "sha512" and digest.hex() == expected for algo, digest in _integrity_values(record.get("integrity", ""))):
        raise FreezeError(f"combined npm lock direct integrity differs from frozen artifact for {entry_id}: {package}@{version}")


def provider_binding(entry_id: str, profile_entries: dict[str, dict[str, Any]], artifacts: dict[str, Any], cache_root: Path) -> dict[str, Any]:
    entry = profile_entries.get(entry_id)
    if not isinstance(entry, dict):
        raise FreezeError(f"provider entry missing from frozen profile lock: {entry_id}")
    profile = entry.get("profiles", {}).get("windows-x86_64", {})
    ref = profile.get("dependency_lock")
    if profile.get("status") != "locked" or not isinstance(ref, dict):
        raise FreezeError(f"provider Windows lock is not frozen: {entry_id}")
    raw = (cache_root / ref["cache_path"]).read_bytes()
    if len(raw) != ref.get("size_bytes") or sha256(raw) != ref.get("sha256"):
        raise FreezeError(f"provider lock cache changed: {entry_id}")
    artifact = artifact_record(artifacts, entry_id)
    artifact_path = cache_root / artifact["cache_path"]
    if not artifact_path.is_file() or sha256(artifact_path.read_bytes()) != artifact["computed_digests"]["sha256"]:
        raise FreezeError(f"provider artifact cache changed: {entry_id}")
    return {"entry": entry, "profile": profile, "lock_ref": ref, "artifact": artifact}


def exact_provider_bindings(profile_entries: dict[str, dict[str, Any]], artifacts: dict[str, Any], cache_root: Path) -> dict[str, dict[str, Any]]:
    ids = set(SERVICE_PROVIDER_IDS.values()) | {NODE_PROVIDER_ID}
    return {entry_id: provider_binding(entry_id, profile_entries, artifacts, cache_root) for entry_id in sorted(ids)}


def resolve_services(
    matrix_unit: dict[str, Any],
    resolution_rows: dict[tuple[str, str], dict[str, Any]],
    provider_bindings: dict[str, dict[str, Any]],
) -> tuple[list[str], list[str] | None]:
    selected: set[str] = set()
    queue: list[str] = []
    for service in matrix_unit.get("static_required_services", []):
        row = resolution_rows.get((matrix_unit["registry_entry_id"], service))
        if service in {"http", "i18n"} or (row and row.get("final_disposition") == "excluded_known_scanner_false_positive"):
            continue
        if row and row.get("final_disposition") == "no_archive_closed_provider_candidate":
            return [], [service]
        provider = SERVICE_PROVIDER_IDS.get(service)
        if provider is None:
            return [], [service]
        if provider not in provider_bindings:
            raise FreezeError(f"selected provider is not in the verified closure: {provider}")
        queue.append(provider)

    while queue:
        provider_id = queue.pop(0)
        if provider_id in selected:
            continue
        selected.add(provider_id)
        for required_service in PROVIDER_REQUIRED_SERVICES.get(provider_id, []):
            if required_service in {"http", "i18n"}:
                continue
            dependency_id = SERVICE_PROVIDER_IDS.get(required_service)
            if required_service == "node":
                dependency_id = NODE_PROVIDER_ID
            if dependency_id is None:
                return [], [required_service]
            if dependency_id not in provider_bindings:
                raise FreezeError(f"recursive provider is not hash locked: {dependency_id}")
            queue.append(dependency_id)
    return sorted(selected), None


def host_plugins(provider_ids: list[str], provider_bindings: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for entry_id in provider_ids:
        binding = provider_bindings[entry_id]
        entry = binding["entry"]
        result.append({
            "registry_entry_id": entry_id,
            "package": entry["package"],
            "version": entry["advertised_version"],
            "artifact_sha256": binding["artifact"]["computed_digests"]["sha256"],
            "config": {},
            "provides": PROVIDER_SERVICES.get(entry_id, []),
            "source": "exact frozen Koishi catalog entry plus its hash-verified Windows provider lock",
        })
    return result


def npm_package_lock(
    node: Path,
    npm_cli: Path,
    npm_version: Path,
    dependencies: dict[str, str],
    cache_root: Path,
    timeout_seconds: int,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    package_name = "ligamina-p305-koishi-host-profile"
    with tempfile.TemporaryDirectory(prefix="p305-koishi-profile-resolve-") as temporary:
        temp = Path(temporary)
        project = temp / "project"
        project.mkdir()
        (project / "package.json").write_bytes(pretty_bytes({
            "name": package_name,
            "version": "0.0.0",
            "private": True,
            "dependencies": dict(sorted(dependencies.items())),
        }))
        user_config = temp / "user.npmrc"
        global_config = temp / "global.npmrc"
        user_config.write_text("registry=https://registry.npmjs.org/\n", encoding="utf-8")
        global_config.write_text("registry=https://registry.npmjs.org/\n", encoding="utf-8")
        npm_cache = cache_root / "npm-cache"
        npm_cache.mkdir(parents=True, exist_ok=True)
        command = [
            str(node), str(npm_cli), "install", "--package-lock-only", "--ignore-scripts",
            "--strict-peer-deps", "--engine-strict", "--no-audit", "--no-fund",
            "--lockfile-version=3", "--registry=https://registry.npmjs.org/",
            f"--userconfig={user_config}", f"--globalconfig={global_config}", f"--cache={npm_cache}",
        ]
        env = {key: os.environ[key] for key in (
            "PATH", "SystemRoot", "WINDIR", "TEMP", "TMP", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
            "http_proxy", "https_proxy", "no_proxy",
        ) if key in os.environ}
        env.update({"NPM_CONFIG_REGISTRY": "https://registry.npmjs.org/", "NPM_CONFIG_CACHE": str(npm_cache)})
        try:
            result = subprocess.run(command, cwd=project, env=env, capture_output=True, text=True,
                                    timeout=timeout_seconds, check=False)
        except subprocess.TimeoutExpired as error:
            return None, {"status": "unknown", "reason_code": "npm_lock_resolution_timeout",
                          "timeout_seconds": timeout_seconds,
                          "stdout_sha256": sha256((error.stdout or b"") if isinstance(error.stdout, bytes) else str(error.stdout or "").encode("utf-8")),
                          "stderr_sha256": sha256((error.stderr or b"") if isinstance(error.stderr, bytes) else str(error.stderr or "").encode("utf-8"))}
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        diagnostics = (stdout + "\n" + stderr).encode("utf-8", errors="replace")
        lock_path = project / "package-lock.json"
        if result.returncode != 0 or not lock_path.is_file():
            combined = (stdout + "\n" + stderr).upper()
            deterministic = "ERESOLVE" in combined or "PEER DEPENDENCY" in combined
            return None, {
                "status": "blocked" if deterministic else "unknown",
                "reason_code": "strict_peer_dependency_conflict" if deterministic else "npm_lock_resolution_failed",
                "exit_code": result.returncode,
                "npm_version": str(npm_version),
                "diagnostic_sha256": sha256(diagnostics),
            }
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        return lock, {
            "status": "locked",
            "npm_version": str(npm_version),
            "exit_code": result.returncode,
            "package_count": max(0, len(lock.get("packages", {})) - 1),
            "root_dependencies": lock.get("packages", {}).get("", {}).get("dependencies", {}),
        }


def fetch_lock_archives(lock: dict[str, Any], cache_root: Path) -> dict[str, int]:
    from lab.pilot_workers import _download_npm_archive, _package_archive_ref

    packages = lock.get("packages", {})
    refs: dict[tuple[str, str], tuple[str, str, str]] = {}
    for package_path, package in packages.items():
        if not package_path:
            continue
        url = package.get("resolved")
        integrity = package.get("integrity")
        if not isinstance(url, str) or not isinstance(integrity, str):
            raise FreezeError(f"combined lock has no frozen registry archive ref: {package_path}")
        algorithm, digest, relative = _package_archive_ref(url, integrity, entry_path=package_path)
        refs[(algorithm, digest)] = (url, integrity, package_path)
    downloaded = 0
    bytes_downloaded = 0
    for (algorithm, digest), (url, integrity, package_path) in refs.items():
        _, size, cached = _download_npm_archive(url, integrity, cache_root, entry_path=package_path)
        downloaded += int(not cached)
        bytes_downloaded += size
    return {"unique_archives": len(refs), "downloaded_archives": downloaded,
            "downloaded_bytes": bytes_downloaded, "archive_refs_verified": len(refs)}


def verify_report_inputs() -> dict[str, Any]:
    from lab.automation.selection import object_sha256, validate_manifest

    recovery, recovery_raw = read_json(RECOVERY_PATH)
    validate_manifest(recovery, verify_source_files=True)
    if object_sha256({key: item for key, item in recovery.items() if key != "manifest_sha256"}) != recovery.get("manifest_sha256"):
        raise FreezeError("P3-01 recovery manifest canonical SHA mismatch")
    pilot, _ = read_json(PILOT_MANIFEST_PATH)
    pilot_report, pilot_report_raw = read_json(PILOT_REPORT_PATH)
    pilot_report_file_sha = verify_sidecar(PILOT_REPORT_PATH)
    for path in (PILOT_MANIFEST_PATH, PILOT_REPORT_PATH):
        verify_sidecar(path)
    if object_sha256({key: item for key, item in pilot.items() if key != "manifest_sha256"}) != pilot.get("manifest_sha256"):
        raise FreezeError("terminal Windows pilot manifest canonical SHA mismatch")
    if pilot.get("campaign_id") != "p3-recovery-windows-pilot-v2-9666ebb4609f" or pilot_report.get("status") != "completed":
        raise FreezeError("expected immutable Windows pilot is not completed")
    if pilot_report.get("unit_counts") != {"passed": 0, "failed": 2, "skipped": 8, "unknown": 10}:
        raise FreezeError("immutable pilot outcomes changed")
    policy_manifest, _ = read_json(NONEBOT_MANIFEST_PATH)
    verify_sidecar(NONEBOT_MANIFEST_PATH)
    if object_sha256({key: item for key, item in policy_manifest.items() if key != "manifest_sha256"}) != policy_manifest.get("manifest_sha256"):
        raise FreezeError("existing Evaluation manifest canonical SHA mismatch")
    admission_file_sha256 = sha256(ADMISSION_PATH.read_bytes())
    if (pilot.get("runtime_admission_sha256") != admission_file_sha256
            or policy_manifest.get("runtime_admission_sha256") != admission_file_sha256):
        raise FreezeError("P3-04 admission file hash differs from the immutable pilot and Evaluation manifest pins")
    return {
        "recovery": recovery,
        "recovery_file_sha256": sha256(recovery_raw),
        "pilot": pilot,
        "pilot_report": pilot_report,
        "pilot_report_file_sha256": pilot_report_file_sha,
        "policy_manifest": policy_manifest,
        "admission_file_sha256": admission_file_sha256,
    }


def generate(timeout_seconds: int) -> dict[str, Any]:
    from lab.pilot_workers import WINDOWS_NODE_RUNTIME

    for path in (PROFILE_LOCK_PATH, PREFLIGHT_PATH, MANIFEST_PATH):
        if path.exists() or path.with_name(path.name + ".sha256").exists():
            raise FreezeError(f"refusing to overwrite frozen output: {path}")
    if WINDOWS_NODE_RUNTIME["archive_sha256"] != NODE_RUNTIME["archive_sha256"] or WINDOWS_NODE_RUNTIME["node_executable_sha256"] != NODE_RUNTIME["node_executable_sha256"]:
        raise FreezeError("profile builder Node runtime differs from the P3-05 worker runtime pin")

    inputs = verify_report_inputs()
    matrix, matrix_canonical, matrix_file_sha = verify_canonical_report(MATRIX_PATH)
    resolution, resolution_canonical, resolution_file_sha = verify_canonical_report(RESOLUTION_PATH)
    closure, closure_canonical, closure_file_sha = verify_canonical_report(CLOSURE_PATH)
    source, source_canonical, source_file_sha = verify_canonical_report(SOURCE_PATH)
    snapshot_lock, snapshot_lock_raw = read_json(PROFILE_SOURCE_PATH)
    artifact_lock, artifact_lock_raw = read_json(ARTIFACT_LOCK_PATH)
    profiles = {row["registry_entry_id"]: row for row in snapshot_lock["entries"]}
    artifacts = {row["registry_entry_id"]: row for row in artifact_lock["entries"]}
    cache_root = CACHE_ROOT.resolve(strict=True)
    resolution_rows = {(row["target_registry_entry_id"], row["service_name"]): row for row in resolution.get("unit_service_resolutions", [])}

    closed_ids = {row["registry_entry_id"] for row in closure.get("provider_lock_blobs", [])}
    service_provider_ids = set(SERVICE_PROVIDER_IDS.values())
    if not service_provider_ids.issubset(closed_ids):
        raise FreezeError("selected service providers are not all present in the hash-verified closure audit")
    for service, provider_id in SERVICE_PROVIDER_IDS.items():
        if not any(row.get("service_name") == service and provider_id in row.get("provider_candidate_ids", [])
                   for row in resolution.get("unit_service_resolutions", [])):
            raise FreezeError(f"chosen provider does not appear in the frozen service-candidate evidence: {service} {provider_id}")
    bindings = exact_provider_bindings(profiles, artifacts, cache_root)

    pilot_source_hashes = sorted({row.get("source_identity_sha256") for row in inputs["pilot"].get("selection", [])
                                  if row.get("identity", {}).get("ecosystem") == "koishi"})
    if len(pilot_source_hashes) != 10 or None in pilot_source_hashes:
        raise FreezeError("terminal pilot does not have the expected ten Koishi source identities")
    source_rows = [row for row in inputs["recovery"].get("selection", [])
                   if row.get("identity", {}).get("platform") == "windows"
                   and row.get("identity", {}).get("ecosystem") == "koishi"]
    if len(source_rows) != 177:
        raise FreezeError(f"expected 177 frozen Windows Koishi recovery entries, got {len(source_rows)}")
    source_rows = [row for row in source_rows if row.get("identity_sha256") not in set(pilot_source_hashes)]
    if len(source_rows) != 167:
        raise FreezeError(f"remaining Koishi selection differs from 167: {len(source_rows)}")
    source_order = [row["identity"]["registry_entry_id"] for row in source_rows]
    source_by_id = {row["identity"]["registry_entry_id"]: row for row in source_rows}
    matrix_units = {row["registry_entry_id"]: row for row in matrix.get("units", [])}
    if set(source_by_id) != set(matrix_units):
        raise FreezeError("requirements matrix identities differ from the remaining recovery Koishi selection")

    node, npm_cli, npm_version = lock_command_inputs(cache_root)
    alt_profiles = json.loads(json.dumps(snapshot_lock))
    alt_entries = {row["registry_entry_id"]: row for row in alt_profiles["entries"]}
    unit_work: dict[str, dict[str, Any]] = {}
    lock_cache: dict[tuple[str, ...], tuple[dict[str, Any], bytes, dict[str, Any], dict[str, int]]] = {}

    for entry_id in sorted(source_by_id):
        source_row = source_by_id[entry_id]
        unit = matrix_units[entry_id]
        entry = profiles[entry_id]
        target_artifact = artifact_record(artifacts, entry_id)
        all_services = [row["service_name"] for row in resolution.get("unit_service_resolutions", [])
                        if row.get("target_registry_entry_id") == entry_id]
        skip: dict[str, Any] | None = None
        if entry_id in GIT_SSH_TARGETS:
            skip = {"status": "skipped", "reason_code": "frozen_git_ssh_source_not_worker_allowlisted",
                    "attempted": False,
                    "detail": "The immutable target lock references a Git SSH dependency source that the Windows Worker does not allow; the locked commit was not substituted."}
        if skip is None and any(service == MARKET_BLOCKER_SERVICE and resolution_rows[(entry_id, service)]["final_disposition"] == "no_archive_closed_provider_candidate"
                                for service in all_services):
            skip = {"status": "skipped", "reason_code": "required_service_provider_peer_conflict",
                    "attempted": False,
                    "detail": "The frozen market service candidate requires koishi-plugin-pointmint@^0.0.9, while the frozen catalog provides only 0.0.8; no provider or target identity was substituted."}

        provider_ids: list[str] = []
        service_error = None
        if skip is None:
            provider_ids, service_error = resolve_services(unit, resolution_rows, bindings)
            if service_error:
                skip = {"status": "skipped", "reason_code": "required_service_provider_unavailable",
                        "attempted": False,
                        "detail": f"No exact archive-closed provider profile can satisfy required service {service_error[0]} without changing the frozen catalog identity."}

        original_profile = entry.get("profiles", {}).get("windows-x86_64", {})
        lock_ref = original_profile.get("dependency_lock")
        if skip is not None:
            if not isinstance(lock_ref, dict):
                raise FreezeError(f"pre-dispatch terminal unit lacks original frozen lock identity: {entry_id}")
            selected_profiles = []
            npm_diagnostics = {"status": "not_run", "reason_code": skip["reason_code"]}
        else:
            selected_profiles = list(provider_ids)
            if not provider_ids:
                if not isinstance(lock_ref, dict):
                    raise FreezeError(f"core-only target has no frozen original dependency lock: {entry_id}")
                lock_raw = (cache_root / lock_ref["cache_path"]).read_bytes()
                if len(lock_raw) != lock_ref.get("size_bytes") or sha256(lock_raw) != lock_ref.get("sha256"):
                    raise FreezeError(f"core-only target dependency lock changed: {entry_id}")
                lock_doc = json.loads(lock_raw.decode("utf-8"))
                root_dependencies = lock_doc.get("packages", {}).get("", {}).get("dependencies", {})
                if root_dependencies.get(CORE) != CORE_VERSION or root_dependencies.get(entry["package"]) != entry["advertised_version"]:
                    raise FreezeError(f"core-only frozen lock does not bind the exact target: {entry_id}")
                verify_direct_hash(lock_doc, entry["package"], entry["advertised_version"], target_artifact, entry_id)
                resolution_meta = {"status": "reused_frozen_target_lock", "package_count": max(0, len(lock_doc.get("packages", {})) - 1)}
                downloads = {"unique_archives": 0, "downloaded_archives": 0, "downloaded_bytes": 0, "archive_refs_verified": 0}
                npm_diagnostics = resolution_meta
            else:
                dependencies = {CORE: CORE_VERSION, entry["package"]: entry["advertised_version"]}
                for provider_id in provider_ids:
                    provider = bindings[provider_id]["entry"]
                    prior = dependencies.get(provider["package"])
                    version = provider["advertised_version"]
                    if prior is not None and prior != version:
                        raise FreezeError(f"selected provider collides with a different target version: {entry_id} {provider['package']}")
                    dependencies[provider["package"]] = version
                cache_key = tuple([entry_id, *provider_ids])
                if cache_key not in lock_cache:
                    lock, resolution_meta = npm_package_lock(node, npm_cli, npm_version, dependencies, cache_root, timeout_seconds)
                    if lock is None:
                        if resolution_meta.get("status") == "blocked" and resolution_meta.get("reason_code") == "strict_peer_dependency_conflict":
                            skip = {"status": "skipped", "reason_code": "combined_host_profile_peer_conflict",
                                    "attempted": False,
                                    "detail": "Strict peer dependency resolution rejected this target/provider set; no package install or Worker was started.",
                                    "diagnostic_sha256": resolution_meta.get("diagnostic_sha256")}
                            lock_ref = original_profile.get("dependency_lock")
                            if not isinstance(lock_ref, dict):
                                raise FreezeError(f"peer-conflict unit lacks original lock identity: {entry_id}")
                        else:
                            raise FreezeError(f"combined lock resolution did not complete for {entry_id}: {resolution_meta}")
                    else:
                        verify_direct_hash(lock, entry["package"], entry["advertised_version"], target_artifact, entry_id)
                        for provider_id in provider_ids:
                            provider = bindings[provider_id]
                            verify_direct_hash(lock, provider["entry"]["package"], provider["entry"]["advertised_version"],
                                               provider["artifact"], f"provider:{provider_id}")
                        lock_raw = pretty_bytes(lock)
                        lock_ref = checked_lock_cache(cache_root, lock_raw)
                        downloads = fetch_lock_archives(lock, cache_root)
                        lock_cache[cache_key] = (lock, lock_raw, resolution_meta, downloads)
                if skip is None:
                    lock, lock_raw, resolution_meta, downloads = lock_cache[cache_key]
                    lock_ref = checked_lock_cache(cache_root, lock_raw)
                    npm_diagnostics = resolution_meta

        profile = alt_entries[entry_id]["profiles"]["windows-x86_64"]
        if skip is None:
            profile_lock_key = tuple([entry_id, *provider_ids])
            profile_lock_doc = lock_cache[profile_lock_key][0] if profile_lock_key in lock_cache else None
            profile.update({
                "status": "locked",
                "reason_code": None,
                "profile_id": f"koishi-p305-windows-host-profile-v1-{entry_id}",
                "dependency_lock": lock_ref,
                "dependency_count": max(0, len(profile_lock_doc.get("packages", {})) - 1)
                    if profile_lock_doc is not None else int(original_profile.get("dependency_count", 0)),
                "resolver": "official Node 24.20.0 bundled npm package-lock-only; --ignore-scripts --strict-peer-deps --engine-strict; direct artifacts verified against catalog SHA-512",
                "portability": "Windows x86_64 Evaluation profile; Worker runs npm ci offline with lifecycle scripts disabled",
                "host_service_profile_id": f"koishi-p305-host-services-v1-{entry_id}",
                "host_service_plugins": host_plugins(provider_ids, bindings),
            })
        else:
            profile.update({
                "profile_id": f"koishi-p305-windows-terminal-skip-v1-{entry_id}",
                "host_service_profile_id": f"koishi-p305-host-services-v1-{entry_id}",
                "host_service_plugins": [],
                "profile_disposition": "pre_dispatch_terminal",
            })
        unit_work[entry_id] = {
            "source_row": source_row,
            "matrix_unit": unit,
            "provider_ids": [] if skip is not None else provider_ids,
            "skip": skip,
            "lock_ref": lock_ref,
            "service_demands": unit.get("static_required_services", []),
            "npm_resolution": npm_diagnostics,
        }

    profile_raw = pretty_bytes(alt_profiles)
    profile_sha = sha256(profile_raw)

    admission_raw = ADMISSION_PATH.read_bytes()
    admission = json.loads(admission_raw.decode("utf-8"))
    if admission.get("status") != "admitted" or admission.get("runtime_profile", {}).get("profile_id") != "windows-cpython-3.12.10-official-embed-amd64-psf-signed":
        raise FreezeError("P3-04 Windows admission identity is not admitted")
    policy = json.loads(POLICY_REPORT_PATH.read_text(encoding="utf-8"))
    policy_profile = policy.get("sac_evaluation_baseline", {})
    if (policy_profile.get("base_policy_guid") != BASE_GUID or policy_profile.get("flight_supplemental_guid") != SUPPLEMENTAL_GUID
            or policy_profile.get("base_audit_mode_enabled") is not True or policy_profile.get("supplemental_audit_mode_enabled") is not True
            or policy.get("codeintegrity_event_counts") != {"3076": 0, "3077": 0, "3089": 0, "3099": 18}):
        raise FreezeError("existing default SAC Evaluation policy evidence no longer matches")
    policy_evidence = {}
    for name in ("guest-policy-inventory.json", "CodeIntegrity-Operational.evtx", "codeintegrity-events.json"):
        evidence_path = POLICY_REPORT_PATH.parent / name
        verify_sidecar(evidence_path)
        policy_evidence[name] = {"path": evidence_path.relative_to(ROOT).as_posix(), "sha256": sha256(evidence_path.read_bytes())}
    verify_sidecar(POLICY_REPORT_PATH)
    if sha256(admission_raw) != inputs["admission_file_sha256"]:
        raise FreezeError("P3-04 admission bytes changed after immutable manifest pin validation")

    selection: list[dict[str, Any]] = []
    for entry_id in source_order:
        work = unit_work[entry_id]
        src = work["source_row"]
        old_identity = src["identity"]
        profile = alt_entries[entry_id]["profiles"]["windows-x86_64"]
        dependency_ref = profile["dependency_lock"]
        identity = dict(old_identity)
        identity.update({
            "profile_id": profile["profile_id"],
            "profile_lock_sha256": profile_sha,
            "dependency_lock_sha256": dependency_ref["sha256"],
        })
        providers = host_plugins(work["provider_ids"], bindings)
        probe_profile = {
            "kind": "koishi-windows-offline-native-load-host-profile-v1",
            "profile_lock_sha256": profile_sha,
            "runtime_admission_sha256": sha256(admission_raw),
            "sandbox_policy_profile_id": POLICY_ID,
            "host_service_profile_id": profile["host_service_profile_id"],
            "host_service_plugins": providers,
            "probe_semantics_version": 3,
            "lifecycle_scripts": "disabled",
        }
        selection_row = {
            "selection_kind": "affected_windows_remaining_koishi",
            "identity": identity,
            "identity_sha256": canonical_sha256(identity),
            "source_identity": old_identity,
            "source_identity_sha256": src["identity_sha256"],
            "source_selection_reasons": src.get("selection_reasons", []),
            "old_to_new_identity": {
                "source_identity_sha256": src["identity_sha256"],
                "profile_id_changed": old_identity.get("profile_id") != identity["profile_id"],
                "profile_lock_sha256_changed": old_identity.get("profile_lock_sha256") != profile_sha,
                "dependency_lock_sha256_changed": old_identity.get("dependency_lock_sha256") != dependency_ref["sha256"],
            },
            "probe_profile": probe_profile,
        }
        if work["skip"] is not None:
            selection_row["pre_dispatch_terminal_result"] = {
                **work["skip"],
                "schema_version": 1,
                "task_id": "P3-05",
                "result_id": f"catalog:{entry_id}|windows",
                "registry_entry_id": entry_id,
                "ecosystem": "koishi",
                "package": old_identity.get("package"),
                "version": old_identity.get("version"),
                "platform": "windows",
            }
        selection.append(selection_row)

    if len(selection) != 167 or len({row["identity"]["registry_entry_id"] for row in selection}) != 167:
        raise FreezeError("new Koishi selection is not exactly 167 unique identities")
    selected_ids = {row["identity"]["registry_entry_id"] for row in selection}
    skip_counts: dict[str, int] = {}
    for row in selection:
        skip = row.get("pre_dispatch_terminal_result")
        if skip:
            reason = skip["reason_code"]
            skip_counts[reason] = skip_counts.get(reason, 0) + 1
    locked_profiles = len(selection) - sum(skip_counts.values())
    profile_lock_file_sha = profile_sha
    manifest = {
        "schema_version": 1,
        "contract": "phase3-windows-affected-koishi-v1",
        "kind": "phase3_p305_windows_koishi_affected_manifest_v1",
        "campaign_id": "p3-recovery-windows-koishi-evaluation-20261026",
        "snapshot_id": inputs["recovery"]["snapshot_id"],
        "platform": "windows",
        "ecosystem": "koishi",
        "expected_units": 167,
        "required_stage_positions": 167 * 3,
        "stage_scope": ["install", "native", "cleanup"],
        "probe_semantics_version": 3,
        "source_recovery_manifest_path": RECOVERY_PATH.relative_to(ROOT).as_posix(),
        "source_recovery_manifest_sha256": inputs["recovery"]["manifest_sha256"],
        "source_recovery_manifest_file_sha256": inputs["recovery_file_sha256"],
        "artifact_lock_path": ARTIFACT_LOCK_PATH.relative_to(ROOT).as_posix(),
        "artifact_lock_sha256": sha256(artifact_lock_raw),
        "source_profile_lock_path": PROFILE_SOURCE_PATH.relative_to(ROOT).as_posix(),
        "source_profile_lock_sha256": sha256(snapshot_lock_raw),
        "profile_lock_path": PROFILE_LOCK_PATH.relative_to(ROOT).as_posix(),
        "profile_lock_sha256": profile_lock_file_sha,
        "runtime_admission_path": ADMISSION_PATH.relative_to(ROOT).as_posix(),
        "runtime_admission_sha256": sha256(admission_raw),
        "windows_node_runtime": NODE_RUNTIME,
        "worker_policy": {
            "sandbox_memory_mb": 16384,
            "cpu_cores": 2,
            "process_limit": 256,
            "network": "disabled",
            "input": "read-only",
            "npm_install": "offline --ignore-scripts",
            "windows_sandbox_required": True,
            "sandbox_policy_profile_id": POLICY_ID,
        },
        "sandbox_policy_profile": {
            "profile_id": POLICY_ID,
            "source": "default Windows Sandbox SAC Evaluation configuration; no App Control policy is changed",
            "mode": "Smart App Control Evaluation (Audit Mode)",
            "preflight_report_path": POLICY_REPORT_PATH.relative_to(ROOT).as_posix(),
            "preflight_report_file_sha256": sha256(POLICY_REPORT_PATH.read_bytes()),
            "preflight_evidence": policy_evidence,
            "guest_os_build": policy.get("guest", {}).get("BuildNumber"),
            "base_policy_guid": BASE_GUID,
            "base_options": "0xb18d2714",
            "base_audit_mode_enabled": True,
            "flight_supplemental_guid": SUPPLEMENTAL_GUID,
            "flight_supplemental_options": "0xd1892304",
            "supplemental_audit_mode_enabled": True,
            "mode_claim": "evaluation-scoped load evidence only; not enforcement-mode acceptance",
        },
        "exclusions": {
            "terminal_campaign_id": inputs["pilot"]["campaign_id"],
            "terminal_manifest_sha256": inputs["pilot"]["manifest_sha256"],
            "terminal_report_path": PILOT_REPORT_PATH.relative_to(ROOT).as_posix(),
            "terminal_report_file_sha256": inputs["pilot_report_file_sha256"],
            "source_identity_sha256": pilot_source_hashes,
            "excluded_units": len(pilot_source_hashes),
            "outcomes_immutable": True,
        },
        "selection_algorithm": "retain the frozen P3-01 recovery order for Windows Koishi; exclude the ten immutable Koishi pilot identities",
        "profile_resolution": {
            "profile_builder": "scripts/phase3-windows-koishi-host-profile-freeze.py",
            "profile_preflight_report_path": PREFLIGHT_PATH.relative_to(ROOT).as_posix(),
            "profile_preflight_report_sha256": None,
            "provider_selection": "exact archive-closed catalog providers, source-verified provider identities, and recursively included required provider services; empty config objects; no secrets",
            "terminal_pre_dispatch_rows": skip_counts,
            "lifecycle_scripts": "disabled for every Windows Sandbox npm ci invocation",
        },
        "selection": selection,
    }
    report = {
        "schema_version": 1,
        "kind": "phase3_p305_windows_koishi_host_profile_preflight",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "campaign_id": manifest["campaign_id"],
        "status": "ready_for_validate_only" if locked_profiles + sum(skip_counts.values()) == 167 else "blocked",
        "profile_ready": locked_profiles + sum(skip_counts.values()) == 167,
        "summary": {
            "target_units": 167,
            "combined_host_lock_units": locked_profiles,
            "pre_dispatch_terminal_units": sum(skip_counts.values()),
            "dispatch_eligible_units": locked_profiles,
            "unique_combined_locks": len(lock_cache),
            "pre_dispatch_reason_counts": skip_counts,
            "profile_configurations": locked_profiles,
            "compatibility": "not_evaluated",
        },
        "identities": {
            "requirements_matrix_canonical_sha256": matrix_canonical,
            "requirements_matrix_file_sha256": matrix_file_sha,
            "provider_resolution_canonical_sha256": resolution_canonical,
            "provider_resolution_file_sha256": resolution_file_sha,
            "provider_closure_canonical_sha256": closure_canonical,
            "provider_closure_file_sha256": closure_file_sha,
            "service_source_canonical_sha256": source_canonical,
            "service_source_file_sha256": source_file_sha,
            "source_profile_lock_sha256": sha256(snapshot_lock_raw),
            "alternate_profile_lock_sha256": profile_lock_file_sha,
            "profile_lock_file_sha256": profile_sha,
            "recovery_manifest_sha256": inputs["recovery"]["manifest_sha256"],
            "pilot_manifest_sha256": inputs["pilot"]["manifest_sha256"],
            "pilot_report_sha256": inputs["pilot_report_file_sha256"],
            "admission_sha256": sha256(admission_raw),
            "evaluation_policy_report_sha256": sha256(POLICY_REPORT_PATH.read_bytes()),
            "node_runtime_archive_sha256": NODE_RUNTIME["archive_sha256"],
            "node_executable_sha256": NODE_RUNTIME["node_executable_sha256"],
            "npm_version": str(npm_version),
        },
        "units": [
            {
                "registry_entry_id": entry_id,
                "package": source_by_id[entry_id]["identity"]["package"],
                "version": source_by_id[entry_id]["identity"]["version"],
                "required_services": unit_work[entry_id]["service_demands"],
                "providers": unit_work[entry_id]["provider_ids"],
                "disposition": "pre_dispatch_terminal" if unit_work[entry_id]["skip"] else "locked_profile",
                "reason_code": unit_work[entry_id]["skip"].get("reason_code") if unit_work[entry_id]["skip"] else None,
                "dependency_lock_sha256": unit_work[entry_id]["lock_ref"]["sha256"],
                "npm_lock_resolution": unit_work[entry_id]["npm_resolution"],
            }
            for entry_id in sorted(unit_work)
        ],
        "blockers": [
            "Sandbox Evaluation remains Audit Mode and does not establish enforcement-mode acceptance",
            "npm lifecycle scripts remain disabled; targets requiring unavailable native bindings may fail their isolated load probe",
            "two frozen Git SSH targets and the one market peer-conflict target are recorded as terminal pre-dispatch skips",
        ],
        "limitations": [
            "No npm package install, lifecycle script, catalog plugin, Worker, or Sandbox was executed by the profile builder.",
            "Provider configuration is an empty, secret-free profile; optional external credentials and behavior fixtures are outside this load campaign.",
            "A completed load campaign with failed/skipped/unknown outcomes does not imply behavior compatibility; compatibility remains not_evaluated.",
        ],
    }
    report["report_sha256"] = canonical_sha256(report)
    preflight_raw = pretty_bytes(report)
    report_file_sha = write_immutable(PREFLIGHT_PATH, preflight_raw)
    manifest["profile_resolution"]["profile_preflight_report_sha256"] = report["report_sha256"]
    manifest["manifest_sha256"] = canonical_sha256(manifest)
    revised_manifest_raw = pretty_bytes(manifest)
    manifest_file_sha = write_immutable(MANIFEST_PATH, revised_manifest_raw)
    write_immutable(PROFILE_LOCK_PATH, profile_raw)
    return {
        "campaign_id": manifest["campaign_id"],
        "profile_lock_path": PROFILE_LOCK_PATH.relative_to(ROOT).as_posix(),
        "profile_lock_sha256": profile_sha,
        "profile_preflight_path": PREFLIGHT_PATH.relative_to(ROOT).as_posix(),
        "profile_preflight_report_sha256": report["report_sha256"],
        "profile_preflight_file_sha256": report_file_sha,
        "manifest_path": MANIFEST_PATH.relative_to(ROOT).as_posix(),
        "manifest_sha256": manifest["manifest_sha256"],
        "manifest_file_sha256": manifest_file_sha,
        "target_units": 167,
        "combined_host_lock_units": locked_profiles,
        "pre_dispatch_terminal_units": sum(skip_counts.values()),
        "dispatch_eligible_units": locked_profiles,
        "unique_combined_locks": len(lock_cache),
        "worker_started": False,
        "npm_install_ran": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Freeze Koishi Windows P3-05 combined host profiles without installing packages.")
    parser.add_argument("--timeout", type=int, default=360, help="per-profile npm lock-only resolution timeout")
    args = parser.parse_args()
    try:
        print(json.dumps(generate(args.timeout), ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, FreezeError,
            urllib.error.URLError, subprocess.SubprocessError) as error:
        print(f"Koishi Host profile freeze blocked: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
