"""Freeze an additive Koishi HTTP Host-service follow-up for P3-05.

The source campaign is immutable. This script selects only its terminal units
whose native-load evidence explicitly reports the missing ``http`` service,
adds the exact catalog-pinned ``@koishijs/plugin-http`` provider, resolves
package-lock-only profiles with scripts disabled, verifies every direct
artifact and every archive SRI, and writes a new profile lock, preflight,
manifest, and SHA-256 sidecars. It never installs or executes a plugin.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_base_spec = importlib.util.spec_from_file_location(
    "phase3_windows_koishi_host_profile_freeze",
    ROOT / "scripts/phase3-windows-koishi-host-profile-freeze.py",
)
if _base_spec is None or _base_spec.loader is None:
    raise RuntimeError("cannot load the immutable-source Koishi profile resolver helpers")
base = importlib.util.module_from_spec(_base_spec)
sys.modules[_base_spec.name] = base
_base_spec.loader.exec_module(base)
from lab.automation.selection import object_sha256
from lab.pilot_workers import _download_npm_archive, _integrity_values, _load_npm_lock, _package_archive_ref

ACCEPTANCE = ROOT / "outputs/acceptance"
SNAPSHOT = ROOT / "catalogs/p2-41649744e057d61031d5"
CACHE_ROOT = Path(r"E:\stella\Ligamina-artifact-cache")
BASE_MANIFEST_PATH = ACCEPTANCE / "phase3-p305-windows-koishi-evaluation-manifest-v1-20261026.json"
BASE_PROFILE_PATH = ACCEPTANCE / "phase3-p305-windows-koishi-evaluation-profile-lock-v1-20261026.json"
BASE_REPORT_PATH = ACCEPTANCE / "phase3-p305-windows-koishi-evaluation-execution-v1-20261026.json"
PRIOR_FOLLOWUP_MANIFEST_PATH = ACCEPTANCE / "phase3-p305-windows-koishi-http-followup-manifest-v1-20261026.json"
PRIOR_FOLLOWUP_PROFILE_PATH = ACCEPTANCE / "phase3-p305-windows-koishi-http-followup-profile-lock-v1-20261026.json"
PRIOR_FOLLOWUP_REPORT_PATH = ACCEPTANCE / "phase3-p305-windows-koishi-http-followup-execution-v1-20261026.json"
MATRIX_PATH = ACCEPTANCE / "phase3-p305-windows-koishi-entry-requirements-matrix-v1-20261026.json"
PROFILE_PATH = ACCEPTANCE / "phase3-p305-windows-koishi-http-followup-profile-lock-v2-20261026.json"
PREFLIGHT_PATH = ACCEPTANCE / "phase3-p305-windows-koishi-http-followup-profile-preflight-v2-20261026.json"
MANIFEST_PATH = ACCEPTANCE / "phase3-p305-windows-koishi-http-followup-manifest-v2-20261026.json"
SEMANTICS_VERSION = 4

HTTP_PROVIDER_ID = "ko-bcc8a237e43796843e1c8bc1"
HTTP_PACKAGE = "@koishijs/plugin-http"
HTTP_VERSION = "0.6.3"
HTTP_ARTIFACT_SHA256 = "3fc813dfefea83993103784da11d196424f5a13812886d9212014d637f86b8e8"
HTTP_EXPORT_MEMBER = "package/index.cjs"
HTTP_EXPORT_TEXT = b"module.exports = require('@cordisjs/plugin-http')"
HTTP_EXPORT_NAME = "HTTP"
HTTP_EXPORT_DECLARATION = b"HTTP: () => HTTP"
HTTP_IMPLEMENTATION_PACKAGE = "@cordisjs/plugin-http"
HTTP_IMPLEMENTATION_VERSION = "0.6.3"
HTTP_IMPLEMENTATION_MEMBER = "package/lib/index.cjs"
HTTP_SERVICE_DECLARATION = b"static provide = \"http\""


class FollowupFreezeError(RuntimeError):
    pass


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def canonical_sha256(value: Any) -> str:
    return object_sha256(value)


def pretty_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")


def read_json(path: Path) -> tuple[Any, bytes]:
    raw = path.read_bytes()
    return json.loads(raw.decode("utf-8")), raw


def write_immutable(path: Path, raw: bytes) -> str:
    sidecar = path.with_name(path.name + ".sha256")
    if path.exists() or sidecar.exists():
        raise FollowupFreezeError(f"refusing to overwrite frozen output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    digest = sha256(raw)
    sidecar.write_text(f"{digest}  {path.name}\n", encoding="ascii")
    return digest


def extract_member(archive_path: Path, member_name: str) -> bytes:
    try:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            member = archive.getmember(member_name)
            stream = archive.extractfile(member)
            if stream is None:
                raise FollowupFreezeError(f"archive member is not a file: {member_name}")
            return stream.read()
    except (OSError, tarfile.TarError, KeyError) as error:
        raise FollowupFreezeError(f"cannot inspect exact provider archive member {member_name}: {error}") from error


def npm_lock_with_exact_archived_target(
    node: Path,
    npm_cli: Path,
    npm_version: Path,
    dependencies: dict[str, str],
    target_package: str,
    target_version: str,
    target_artifact: dict[str, Any],
    cache_root: Path,
    timeout_seconds: int,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Resolve a profile using the target's exact frozen tarball as a local root.

    Some catalog artifacts are no longer present in the live registry metadata.
    Using the previously hash-verified tarball preserves that exact target while
    npm resolves its dependency/peer closure; the resulting portable lock is
    normalized back to the frozen registry URL and SHA-512 integrity.
    """
    artifact_path = cache_root / target_artifact["cache_path"]
    artifact_sha = target_artifact.get("computed_digests", {}).get("sha256")
    artifact_sha512 = target_artifact.get("computed_digests", {}).get("sha512")
    if not artifact_path.is_file() or sha256(artifact_path.read_bytes()) != artifact_sha:
        raise FollowupFreezeError(f"exact target archive is missing or changed: {target_package}@{target_version}")
    metadata = json.loads(extract_member(artifact_path, "package/package.json").decode("utf-8"))
    if metadata.get("name") != target_package or metadata.get("version") != target_version:
        raise FollowupFreezeError(f"exact target archive package identity mismatch: {target_package}@{target_version}")
    if not isinstance(artifact_sha512, str):
        raise FollowupFreezeError(f"exact target archive has no frozen SHA-512: {target_package}@{target_version}")

    with tempfile.TemporaryDirectory(prefix="p305-koishi-http-followup-lock-") as temporary:
        temp = Path(temporary)
        project = temp / "project"
        project.mkdir()
        local_target = temp / "frozen-target.tgz"
        shutil.copyfile(artifact_path, local_target)
        if sha256(local_target.read_bytes()) != artifact_sha:
            raise FollowupFreezeError(f"temporary exact-target archive copy changed: {target_package}@{target_version}")
        local_dependencies = dict(dependencies)
        local_dependencies[target_package] = f"file:{local_target.resolve().as_posix()}"
        (project / "package.json").write_bytes(base.pretty_bytes({
            "name": "ligamina-p305-koishi-http-followup-profile",
            "version": "0.0.0",
            "private": True,
            "dependencies": dict(sorted(local_dependencies.items())),
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
            raw_stdout = (error.stdout or b"") if isinstance(error.stdout, bytes) else str(error.stdout or "").encode("utf-8")
            raw_stderr = (error.stderr or b"") if isinstance(error.stderr, bytes) else str(error.stderr or "").encode("utf-8")
            return None, {
                "status": "unknown", "reason_code": "npm_lock_resolution_timeout",
                "timeout_seconds": timeout_seconds,
                "stdout_sha256": sha256(raw_stdout), "stderr_sha256": sha256(raw_stderr),
            }
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        diagnostics = (stdout + "\n" + stderr).encode("utf-8", errors="replace")
        lock_path = project / "package-lock.json"
        if result.returncode != 0 or not lock_path.is_file():
            upper = diagnostics.decode("utf-8", errors="replace").upper()
            deterministic = "ERESOLVE" in upper or "PEER DEPENDENCY" in upper
            return None, {
                "status": "blocked" if deterministic else "unknown",
                "reason_code": "strict_peer_dependency_conflict" if deterministic else "npm_lock_resolution_failed",
                "exit_code": result.returncode,
                "npm_version": str(npm_version),
                "diagnostic_sha256": sha256(diagnostics),
                "diagnostic_tail": diagnostics.decode("utf-8", errors="replace")[-3000:],
            }
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        package_path = f"node_modules/{target_package}"
        target_record = lock.get("packages", {}).get(package_path)
        root_dependencies = lock.get("packages", {}).get("", {}).get("dependencies", {})
        metadata_mismatches = {
            field: {"lock": target_record.get(field), "archive": metadata.get(field)}
            for field in ("dependencies", "peerDependencies")
            if target_record.get(field, {}) != metadata.get(field, {})
        } if isinstance(target_record, dict) else {"target_record": {"lock": target_record, "archive": metadata}}
        if (
            not isinstance(target_record, dict)
            or target_record.get("version") != target_version
            or root_dependencies.get(target_package) != local_dependencies[target_package]
            or target_record.get("dependencies", {}) != metadata.get("dependencies", {})
            or target_record.get("peerDependencies", {}) != metadata.get("peerDependencies", {})
        ):
            raise FollowupFreezeError(
                f"npm did not lock the exact archived target metadata: {target_package}@{target_version}; "
                f"version={target_record.get('version') if isinstance(target_record, dict) else None}; "
                f"root={root_dependencies.get(target_package)}; expected_root={local_dependencies[target_package]}; "
                f"metadata_mismatches={metadata_mismatches}"
            )
        # Convert the file reference into the immutable registry source + integrity
        # that the Windows Worker can materialize from the archive cache offline.
        lock["packages"][""]["dependencies"][target_package] = target_version
        target_record["resolved"] = target_artifact["url"]
        target_record["integrity"] = "sha512-" + base64.b64encode(bytes.fromhex(artifact_sha512)).decode("ascii")
        return lock, {
            "status": "locked",
            "npm_version": str(npm_version),
            "exit_code": result.returncode,
            "package_count": max(0, len(lock.get("packages", {})) - 1),
            "root_dependencies": lock.get("packages", {}).get("", {}).get("dependencies", {}),
            "target_source": "exact catalog artifact tarball supplied as file root; normalized to frozen registry URL and SHA-512",
            "target_archive_sha256": artifact_sha,
            "diagnostic_sha256": sha256(diagnostics),
        }


def verify_http_provider(
    profile_entries: dict[str, dict[str, Any]],
    artifact_entries: dict[str, dict[str, Any]],
    cache_root: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    binding = base.provider_binding(HTTP_PROVIDER_ID, profile_entries, artifact_entries, cache_root)
    entry = binding["entry"]
    if (entry.get("package") != HTTP_PACKAGE or entry.get("advertised_version") != HTTP_VERSION
            or binding["artifact"].get("computed_digests", {}).get("sha256") != HTTP_ARTIFACT_SHA256):
        raise FollowupFreezeError("exact catalog http provider identity or artifact hash changed")
    provider_lock_path = cache_root / binding["lock_ref"]["cache_path"]
    provider_lock, _ = _load_npm_lock(provider_lock_path, binding["lock_ref"])
    provider_package_record = provider_lock.get("packages", {}).get(f"node_modules/{HTTP_PACKAGE}")
    if (not isinstance(provider_package_record, dict)
            or provider_package_record.get("dependencies", {}).get(HTTP_IMPLEMENTATION_PACKAGE) != f"^{HTTP_IMPLEMENTATION_VERSION}"):
        raise FollowupFreezeError("frozen http provider lock no longer reexports the exact Cordis implementation version")

    artifact_path = cache_root / binding["artifact"]["cache_path"]
    export_source = extract_member(artifact_path, HTTP_EXPORT_MEMBER)
    if export_source.strip() != HTTP_EXPORT_TEXT:
        raise FollowupFreezeError("catalog provider package entry does not reexport the expected Cordis HTTP implementation")
    implementation = provider_lock.get("packages", {}).get(f"node_modules/{HTTP_IMPLEMENTATION_PACKAGE}")
    if not isinstance(implementation, dict) or implementation.get("version") != HTTP_IMPLEMENTATION_VERSION:
        raise FollowupFreezeError("exact Cordis HTTP implementation package is missing from the frozen provider lock")
    algorithm, digest, relative = _package_archive_ref(
        implementation.get("resolved", ""), implementation.get("integrity", ""),
        entry_path=f"node_modules/{HTTP_IMPLEMENTATION_PACKAGE}",
    )
    archive_path, _, _ = _download_npm_archive(
        implementation["resolved"], implementation["integrity"], cache_root,
        entry_path=f"node_modules/{HTTP_IMPLEMENTATION_PACKAGE}",
    )
    implementation_source = extract_member(archive_path, HTTP_IMPLEMENTATION_MEMBER)
    if HTTP_SERVICE_DECLARATION not in implementation_source:
        raise FollowupFreezeError("hash-locked Cordis HTTP implementation does not declare the http service")
    if HTTP_EXPORT_DECLARATION not in implementation_source:
        raise FollowupFreezeError("hash-locked Cordis HTTP implementation no longer exports the HTTP Service class")
    sri_values = [f"{algo}:{value.hex()}" for algo, value in _integrity_values(implementation["integrity"])]
    return binding, {
        "provider_registry_entry_id": HTTP_PROVIDER_ID,
        "provider_package": HTTP_PACKAGE,
        "provider_version": HTTP_VERSION,
        "provider_artifact_sha256": HTTP_ARTIFACT_SHA256,
        "provider_export_member": HTTP_EXPORT_MEMBER,
        "provider_export_member_sha256": sha256(export_source),
        "provider_service_export_name": HTTP_EXPORT_NAME,
        "provider_service_export_declaration": HTTP_EXPORT_DECLARATION.decode("ascii"),
        "provider_service_export_declaration_sha256": sha256(HTTP_EXPORT_DECLARATION),
        "reexport_target_package": HTTP_IMPLEMENTATION_PACKAGE,
        "reexport_target_version": HTTP_IMPLEMENTATION_VERSION,
        "implementation_archive_cache_path": relative.as_posix(),
        "implementation_archive_hash_algorithm": algorithm,
        "implementation_archive_digest": digest,
        "implementation_archive_sri": sri_values,
        "implementation_member": HTTP_IMPLEMENTATION_MEMBER,
        "implementation_member_sha256": sha256(implementation_source),
        "service_declaration_sha256": sha256(HTTP_SERVICE_DECLARATION),
        "service_declaration": HTTP_SERVICE_DECLARATION.decode("ascii"),
        "evidence_scope": "static source in exact hash-verified catalog archives; no plugin code executed by the freezer",
    }


def generate(timeout_seconds: int) -> dict[str, Any]:
    from lab.pilot_workers import WINDOWS_NODE_RUNTIME

    for path in (PROFILE_PATH, PREFLIGHT_PATH, MANIFEST_PATH):
        if path.exists() or path.with_name(path.name + ".sha256").exists():
            raise FollowupFreezeError(f"refusing to overwrite frozen output: {path}")

    inputs = base.verify_report_inputs()
    base_manifest, base_manifest_raw = read_json(BASE_MANIFEST_PATH)
    base_report, _ = read_json(BASE_REPORT_PATH)
    base_profile, base_profile_raw = read_json(BASE_PROFILE_PATH)
    base_manifest_file_sha = base.verify_sidecar(BASE_MANIFEST_PATH)
    base_profile_file_sha = base.verify_sidecar(BASE_PROFILE_PATH)
    prior_report_file_sha = base.verify_sidecar(BASE_REPORT_PATH)
    if (
        canonical_sha256({key: value for key, value in base_manifest.items() if key != "manifest_sha256"}) != base_manifest.get("manifest_sha256")
        or base_manifest.get("campaign_id") != "p3-recovery-windows-koishi-evaluation-20261026"
        or base_manifest.get("manifest_sha256") != "143fed20ff0018c3e81adb932b4d5de8e2c1b88126ef8c8892cdf1df6aecdb98"
        or base_manifest_file_sha != sha256(base_manifest_raw)
        or base_manifest.get("profile_lock_sha256") != base_profile_file_sha
        or base_report.get("status") != "completed"
        or base_report.get("campaign_id") != base_manifest.get("campaign_id")
        or base_report.get("manifest_sha256") != base_manifest.get("manifest_sha256")
        or base_report.get("manifest_file_sha256") != base_manifest_file_sha
        or base_report.get("profile_lock_sha256") != base_profile_file_sha
        or base_report.get("settled_units") != 167
        or base_report.get("remaining_units") != 0
    ):
        raise FollowupFreezeError("source Koishi campaign is not the expected closed immutable 167-unit report")
    if base_profile_file_sha != "9a7b627a84bd89af1667e14881253d1635ad793cfad44870c395c19bc0e25abd":
        raise FollowupFreezeError("source Koishi profile lock hash differs from the accepted frozen identity")
    if WINDOWS_NODE_RUNTIME["archive_sha256"] != base_manifest.get("windows_node_runtime", {}).get("archive_sha256"):
        raise FollowupFreezeError("pinned Node runtime differs from the source campaign")

    matrix, matrix_canonical, matrix_file_sha = base.verify_canonical_report(MATRIX_PATH)
    matrix_units = {row["registry_entry_id"]: row for row in matrix.get("units", [])}
    prior_manifest_rows = {row["identity"]["registry_entry_id"]: row for row in base_manifest.get("selection", [])}
    prior_report_rows = {
        row.get("result", {}).get("registry_entry_id"): row
        for row in base_report.get("results", [])
    }
    eligible: list[str] = []
    previous_results: dict[str, dict[str, Any]] = {}
    for entry_id, report_row in prior_report_rows.items():
        result = report_row.get("result", {})
        worker = result.get("worker")
        protocol = worker.get("protocol_report") if isinstance(worker, dict) else None
        native = protocol.get("native_load") if isinstance(protocol, dict) else None
        native = native if isinstance(native, dict) else {}
        if (
            result.get("reason_code") == "required_host_service_unavailable"
            and result.get("status") == "skipped"
            and native.get("missing_services") == ["http"]
        ):
            eligible.append(entry_id)
            previous_results[entry_id] = {
                "registry_entry_id": entry_id,
                "result_id": result.get("result_id"),
                "reason_code": result.get("reason_code"),
                "missing_services": native.get("missing_services"),
                "result_sha256": canonical_sha256(result),
                "selection_identity_sha256": canonical_sha256(prior_manifest_rows[entry_id]["identity"]),
            }
    if len(eligible) != 6:
        raise FollowupFreezeError(f"expected the six frozen missing-http skips, found {len(eligible)}")
    if any("http" not in matrix_units.get(entry_id, {}).get("static_required_services", []) for entry_id in eligible):
        raise FollowupFreezeError("a follow-up result does not have a frozen static http service demand")

    recovery_by_id = {
        row["identity"]["registry_entry_id"]: row
        for row in inputs["recovery"].get("selection", [])
    }
    source_identity_ids = set()
    for entry_id in eligible:
        source_row = recovery_by_id.get(entry_id)
        campaign_row = prior_manifest_rows.get(entry_id)
        if (
            source_row is None
            or campaign_row is None
            or source_row.get("identity_sha256") != campaign_row.get("source_identity_sha256")
            or source_row.get("identity") != campaign_row.get("source_identity")
        ):
            raise FollowupFreezeError(f"follow-up target differs from the frozen P3-01 source identity: {entry_id}")
        source_identity_ids.add(source_row["identity_sha256"])
    if len(source_identity_ids) != 6:
        raise FollowupFreezeError("HTTP follow-up source identities are not unique")

    prior_followup_manifest, prior_followup_manifest_raw = read_json(PRIOR_FOLLOWUP_MANIFEST_PATH)
    prior_followup_report, _ = read_json(PRIOR_FOLLOWUP_REPORT_PATH)
    prior_followup_profile_sha = base.verify_sidecar(PRIOR_FOLLOWUP_PROFILE_PATH)
    prior_followup_manifest_file_sha = base.verify_sidecar(PRIOR_FOLLOWUP_MANIFEST_PATH)
    prior_followup_report_file_sha = base.verify_sidecar(PRIOR_FOLLOWUP_REPORT_PATH)
    if (
        canonical_sha256({key: value for key, value in prior_followup_manifest.items() if key != "manifest_sha256"}) != prior_followup_manifest.get("manifest_sha256")
        or prior_followup_manifest.get("manifest_sha256") != "df01597f7b3710a478e28d2d2ab5d6002ccfb20b45291847c2b879aef1a7d199"
        or prior_followup_manifest.get("campaign_id") != "p3-recovery-windows-koishi-http-followup-20261026"
        or prior_followup_manifest.get("profile_lock_sha256") != prior_followup_profile_sha
        or prior_followup_report.get("campaign_id") != prior_followup_manifest.get("campaign_id")
        or prior_followup_report.get("manifest_sha256") != prior_followup_manifest.get("manifest_sha256")
        or prior_followup_report.get("manifest_file_sha256") != prior_followup_manifest_file_sha
        or prior_followup_report.get("profile_lock_sha256") != prior_followup_profile_sha
        or prior_followup_report.get("status") != "completed"
        or prior_followup_report.get("settled_units") != 6
        or prior_followup_report.get("remaining_units") != 0
        or prior_followup_report.get("unit_counts") != {"passed": 0, "failed": 6, "skipped": 0, "unknown": 0}
    ):
        raise FollowupFreezeError("the prior HTTP follow-up v1 is not the expected immutable closed result set")
    prior_followup_rows = {
        row.get("result", {}).get("registry_entry_id"): row
        for row in prior_followup_report.get("results", [])
    }
    if set(prior_followup_rows) != set(eligible):
        raise FollowupFreezeError("the prior HTTP follow-up v1 result identities differ from the six HTTP skips")
    prior_followup_results: dict[str, dict[str, Any]] = {}
    for entry_id, row in prior_followup_rows.items():
        result = row.get("result", {})
        native = result.get("worker", {}).get("protocol_report", {}).get("native_load", {})
        detail = native.get("error_tail", "")
        if (
            result.get("status") != "failed"
            or result.get("reason_code") != "native_plugin_load_failed"
            or native.get("error_name") != "TypeError"
            or "Host service provider @koishijs/plugin-http does not export a Koishi plugin" not in detail
        ):
            raise FollowupFreezeError(f"prior HTTP v1 failure differs from the known probe-class mismatch: {entry_id}")
        prior_followup_results[entry_id] = {
            "registry_entry_id": entry_id,
            "result_id": result.get("result_id"),
            "status": result.get("status"),
            "reason_code": result.get("reason_code"),
            "result_sha256": canonical_sha256(result),
        }

    snapshot_lock, snapshot_lock_raw = read_json(SNAPSHOT / "profiles.lock.json")
    artifact_lock, artifact_lock_raw = read_json(SNAPSHOT / "artifacts.lock.json")
    profiles = {row["registry_entry_id"]: row for row in snapshot_lock["entries"]}
    artifacts = {row["registry_entry_id"]: row for row in artifact_lock["entries"]}
    cache_root = CACHE_ROOT.resolve(strict=True)
    http_binding, http_source_evidence = verify_http_provider(profiles, artifacts, cache_root)
    node, npm_cli, npm_version = base.lock_command_inputs(cache_root)

    alt_profiles = json.loads(base_profile_raw.decode("utf-8"))
    alt_entries = {row["registry_entry_id"]: row for row in alt_profiles["entries"]}
    profile_results: dict[str, dict[str, Any]] = {}
    for entry_id in sorted(eligible):
        prior_manifest_row = prior_manifest_rows[entry_id]
        prior_identity = prior_manifest_row["identity"]
        profile_entry = alt_entries[entry_id]
        profile = profile_entry.get("profiles", {}).get("windows-x86_64", {})
        old_lock_ref = profile.get("dependency_lock")
        if profile.get("status") != "locked" or not isinstance(old_lock_ref, dict):
            raise FollowupFreezeError(f"source campaign profile has no frozen target lock: {entry_id}")
        old_lock_path = (cache_root / old_lock_ref["cache_path"]).resolve(strict=True)
        old_lock, _ = _load_npm_lock(old_lock_path, old_lock_ref)
        dependencies = dict(old_lock.get("packages", {}).get("", {}).get("dependencies", {}))
        target_entry = profiles.get(entry_id, {})
        target_artifact = base.artifact_record(artifacts, entry_id)
        if (
            dependencies.get(target_entry.get("package")) != target_entry.get("advertised_version")
            or target_entry.get("package") != prior_identity.get("package")
            or target_entry.get("advertised_version") != prior_identity.get("version")
        ):
            raise FollowupFreezeError(f"source package lock does not preserve its exact target: {entry_id}")
        for provider in prior_manifest_row.get("probe_profile", {}).get("host_service_plugins", []):
            provider_entry = profiles.get(provider.get("registry_entry_id"), {})
            if (
                provider_entry.get("package") != provider.get("package")
                or provider_entry.get("advertised_version") != provider.get("version")
                or dependencies.get(provider.get("package")) != provider.get("version")
            ):
                raise FollowupFreezeError(f"source lock no longer binds the old Host provider: {entry_id}")
        existing_http_version = dependencies.get(HTTP_PACKAGE)
        if existing_http_version not in (None, HTTP_VERSION):
            raise FollowupFreezeError(f"source dependency graph conflicts with exact HTTP provider for {entry_id}")
        dependencies[HTTP_PACKAGE] = HTTP_VERSION
        lock, resolution = npm_lock_with_exact_archived_target(
            node, npm_cli, npm_version, dependencies,
            target_entry["package"], target_entry["advertised_version"], target_artifact,
            cache_root, timeout_seconds,
        )
        if lock is None:
            if resolution.get("status") == "blocked" and resolution.get("reason_code") == "strict_peer_dependency_conflict":
                profile_results[entry_id] = {
                    "status": "pre_dispatch_terminal",
                    "reason_code": "combined_host_profile_peer_conflict",
                    "detail": "Strict peer resolution rejected adding the exact HTTP provider; the target was not dispatched or substituted.",
                    "diagnostic_sha256": resolution.get("diagnostic_sha256"),
                    "lock_ref": old_lock_ref,
                    "lock": old_lock,
                    "resolution": resolution,
                    "downloads": {"unique_archives": 0, "downloaded_archives": 0, "downloaded_bytes": 0},
                    "providers": list(prior_manifest_row["probe_profile"]["host_service_plugins"]),
                }
                continue
            raise FollowupFreezeError(f"HTTP provider combined lock resolution did not complete for {entry_id}: {resolution}")

        base.verify_direct_hash(lock, target_entry["package"], target_entry["advertised_version"], target_artifact, entry_id)
        for provider in prior_manifest_row.get("probe_profile", {}).get("host_service_plugins", []):
            provider_id = provider["registry_entry_id"]
            provider_binding = base.provider_binding(provider_id, profiles, artifacts, cache_root)
            base.verify_direct_hash(lock, provider["package"], provider["version"], provider_binding["artifact"], f"provider:{provider_id}")
        base.verify_direct_hash(lock, HTTP_PACKAGE, HTTP_VERSION, http_binding["artifact"], f"provider:{HTTP_PROVIDER_ID}")
        lock_raw = base.pretty_bytes(lock)
        lock_ref = base.checked_lock_cache(cache_root, lock_raw)
        downloads = base.fetch_lock_archives(lock, cache_root)
        providers = list(prior_manifest_row["probe_profile"]["host_service_plugins"])
        providers.append({
            "registry_entry_id": HTTP_PROVIDER_ID,
            "package": HTTP_PACKAGE,
            "version": HTTP_VERSION,
            "artifact_sha256": HTTP_ARTIFACT_SHA256,
            "config": {},
            "provides": ["http"],
            "service_kind": "cordis-service",
            "export_name": HTTP_EXPORT_NAME,
            "source": "P3-05 HTTP follow-up: exact hash-verified catalog provider and Cordis service declaration",
        })
        profile_results[entry_id] = {
            "status": "locked",
            "reason_code": None,
            "lock_ref": lock_ref,
            "lock": lock,
            "lock_file_sha256": sha256(lock_raw),
            "resolution": resolution,
            "downloads": downloads,
            "providers": providers,
        }

    alt_profiles["created_at_utc"] = datetime.now(timezone.utc).isoformat()
    selection: list[dict[str, Any]] = []
    for entry_id in [row["identity"]["registry_entry_id"] for row in base_manifest["selection"] if row["identity"]["registry_entry_id"] in set(eligible)]:
        old_row = prior_manifest_rows[entry_id]
        old_identity = old_row["identity"]
        profile_result = profile_results[entry_id]
        entry_profile = alt_entries[entry_id]["profiles"]["windows-x86_64"]
        entry_profile.update({
            "profile_id": f"koishi-p305-http-followup-v2-host-profile-v1-{entry_id}",
            "host_service_profile_id": f"koishi-p305-http-followup-v2-v1-{entry_id}",
            "host_service_plugins": profile_result["providers"],
            "dependency_lock": profile_result["lock_ref"],
            "dependency_count": max(0, len(profile_result["lock"].get("packages", {})) - 1),
            "resolver": "official Node 24.20.0 bundled npm package-lock-only; --ignore-scripts --strict-peer-deps --engine-strict; exact target and provider artifacts verified against catalog SHA-512",
            "portability": "Windows x86_64 Evaluation follow-up v2 profile; Worker runs npm ci offline with lifecycle scripts disabled",
        })
        # The final profile-lock digest is bound after every selected row is updated.
        identity = dict(old_identity)
        identity.update({
            "profile_id": entry_profile["profile_id"],
            "dependency_lock_sha256": profile_result["lock_ref"]["sha256"],
        })
        previous = previous_results[entry_id]
        row = {
            "selection_kind": "koishi_http_host_service_followup",
            "identity": identity,
            "identity_sha256": canonical_sha256(identity),
            "source_identity": old_row["source_identity"],
            "source_identity_sha256": old_row["source_identity_sha256"],
            "source_selection_reasons": old_row.get("source_selection_reasons", []),
            "prior_evaluation": previous,
            "root_evaluation": old_row.get("prior_evaluation"),
            "superseded_followup": prior_followup_results[entry_id],
            "probe_profile": {
                "kind": "koishi-windows-offline-native-load-http-followup-v2",
                "profile_lock_sha256": None,
                "runtime_admission_sha256": base_manifest["runtime_admission_sha256"],
                "sandbox_policy_profile_id": base_manifest["sandbox_policy_profile"]["profile_id"],
                "host_service_profile_id": entry_profile["host_service_profile_id"],
                "host_service_plugins": profile_result["providers"],
                "probe_semantics_version": SEMANTICS_VERSION,
                "lifecycle_scripts": "disabled",
            },
            "old_to_new_identity": {
                "previous_campaign_id": base_manifest["campaign_id"],
                "previous_identity_sha256": canonical_sha256(old_identity),
                "profile_id_changed": True,
                "profile_lock_sha256_changed": True,
                "dependency_lock_sha256_changed": profile_result["lock_ref"]["sha256"] != old_identity["dependency_lock_sha256"],
            },
        }
        if profile_result["status"] == "pre_dispatch_terminal":
            row["pre_dispatch_terminal_result"] = {
                "schema_version": 1,
                "task_id": "P3-05",
                "result_id": f"catalog:{entry_id}|windows",
                "registry_entry_id": entry_id,
                "ecosystem": "koishi",
                "package": identity["package"],
                "version": identity["version"],
                "platform": "windows",
                "status": "skipped",
                "reason_code": profile_result["reason_code"],
                "attempted": False,
                "detail": profile_result["detail"],
                "diagnostic_sha256": profile_result.get("diagnostic_sha256"),
            }
        selection.append(row)

    if len(selection) != 6 or len({row["identity"]["registry_entry_id"] for row in selection}) != 6:
        raise FollowupFreezeError("HTTP follow-up selection differs from six unique prior skip identities")
    profile_raw = pretty_bytes(alt_profiles)
    profile_sha = sha256(profile_raw)
    for row in selection:
        row["identity"]["profile_lock_sha256"] = profile_sha
        row["identity_sha256"] = canonical_sha256(row["identity"])
        row["probe_profile"]["profile_lock_sha256"] = profile_sha

    admission_raw = base.ADMISSION_PATH.read_bytes()
    runtime_admission_sha = sha256(admission_raw)
    prior_report_file_sha = base.verify_sidecar(BASE_REPORT_PATH)
    preterminal_count = sum("pre_dispatch_terminal_result" in row for row in selection)
    locked_count = len(selection) - preterminal_count
    now = datetime.now(timezone.utc).isoformat()
    prior_result_rows = [previous_results[entry_id] for entry_id in eligible]
    preflight = {
        "schema_version": 1,
        "kind": "phase3_p305_windows_koishi_http_followup_preflight",
        "generated_at_utc": now,
        "campaign_id": "p3-recovery-windows-koishi-http-followup-v2-20261026",
        "status": "ready_for_validate_only" if locked_count + preterminal_count == 6 else "blocked",
        "profile_ready": locked_count + preterminal_count == 6,
        "summary": {
            "target_units": 6,
            "combined_http_host_lock_units": locked_count,
            "pre_dispatch_terminal_units": preterminal_count,
            "dispatch_eligible_units": locked_count,
            "pre_dispatch_reason_counts": dict(__import__("collections").Counter(
                row["pre_dispatch_terminal_result"]["reason_code"] for row in selection if "pre_dispatch_terminal_result" in row
            )),
            "compatibility": "not_evaluated",
            "worker_started": False,
            "npm_install_ran": False,
            "catalog_plugin_executed": False,
        },
        "identities": {
            "source_campaign_id": base_manifest["campaign_id"],
            "source_manifest_sha256": base_manifest["manifest_sha256"],
            "source_manifest_file_sha256": base_manifest_file_sha,
            "source_report_file_sha256": prior_report_file_sha,
            "source_profile_lock_sha256": base_profile_file_sha,
            "new_profile_lock_sha256": profile_sha,
            "requirements_matrix_canonical_sha256": matrix_canonical,
            "requirements_matrix_file_sha256": matrix_file_sha,
            "recovery_manifest_sha256": inputs["recovery"]["manifest_sha256"],
            "admission_sha256": runtime_admission_sha,
            "http_provider": http_source_evidence,
            "http_provider_lock_sha256": http_binding["lock_ref"]["sha256"],
            "node_runtime_archive_sha256": base.NODE_RUNTIME["archive_sha256"],
            "node_executable_sha256": base.NODE_RUNTIME["node_executable_sha256"],
            "npm_version": str(npm_version),
            "probe_sources": {
                "pilot_driver_path": "scripts/phase3-windows-pilot.py",
                "pilot_driver_sha256": sha256((ROOT / "scripts/phase3-windows-pilot.py").read_bytes()),
                "worker_source_path": "lab/pilot_workers.py",
                "worker_source_sha256": sha256((ROOT / "lab/pilot_workers.py").read_bytes()),
            },
        },
        "units": [
            {
                "registry_entry_id": row["identity"]["registry_entry_id"],
                "package": row["identity"]["package"],
                "version": row["identity"]["version"],
                "prior_result_sha256": row["prior_evaluation"]["result_sha256"],
                "profile_status": profile_results[row["identity"]["registry_entry_id"]]["status"],
                "reason_code": profile_results[row["identity"]["registry_entry_id"]].get("reason_code"),
                "dependency_lock_sha256": row["identity"]["dependency_lock_sha256"],
                "npm_lock_resolution": profile_results[row["identity"]["registry_entry_id"]]["resolution"],
                "archive_closure": profile_results[row["identity"]["registry_entry_id"]]["downloads"],
                "provider_registry_entry_id": HTTP_PROVIDER_ID,
                "superseded_followup_result_sha256": row["superseded_followup"]["result_sha256"],
            }
            for row in selection
        ],
        "blockers": [
            "The source campaign's Evaluation policies are Audit Mode and do not establish enforcement-mode acceptance.",
            "The P3-05 behavior fixture is absent; native-load evidence only and plugin compatibility remains not_evaluated.",
            "This follow-up supersedes only the six v1 failures caused by the provider-class integration defect; it does not alter the v1 result ledger.",
        ],
        "limitations": [
            "No catalog plugin, npm install, lifecycle script, Worker, or Sandbox was executed by the freezer.",
            "The exact HTTP provider and static Cordis service declaration are archive/hash evidence; this does not establish network behavior because Sandbox networking remains disabled.",
            "The original six result rows and source report are immutable; this is a separate additive campaign identity.",
        ],
    }
    preflight["report_sha256"] = canonical_sha256(preflight)
    preflight_raw = pretty_bytes(preflight)
    preflight_file_sha = write_immutable(PREFLIGHT_PATH, preflight_raw)

    recovery = inputs["recovery"]
    policy_profile = base_manifest["sandbox_policy_profile"]
    follow_up = {
        "prior_campaign_id": base_manifest["campaign_id"],
        "prior_manifest_path": BASE_MANIFEST_PATH.relative_to(ROOT).as_posix(),
        "prior_manifest_sha256": base_manifest["manifest_sha256"],
        "prior_manifest_file_sha256": base_manifest_file_sha,
        "prior_report_path": BASE_REPORT_PATH.relative_to(ROOT).as_posix(),
        "prior_report_file_sha256": prior_report_file_sha,
        "reason_code": "required_host_service_unavailable",
        "provider_service": "http",
        "eligible_registry_entry_ids": eligible,
        "prior_results": prior_result_rows,
        "prior_followup": {
            "campaign_id": prior_followup_manifest["campaign_id"],
            "manifest_path": PRIOR_FOLLOWUP_MANIFEST_PATH.relative_to(ROOT).as_posix(),
            "manifest_sha256": prior_followup_manifest["manifest_sha256"],
            "manifest_file_sha256": prior_followup_manifest_file_sha,
            "profile_lock_sha256": prior_followup_profile_sha,
            "report_path": PRIOR_FOLLOWUP_REPORT_PATH.relative_to(ROOT).as_posix(),
            "report_file_sha256": prior_followup_report_file_sha,
            "eligible_registry_entry_ids": sorted(prior_followup_rows),
        },
        "prior_followup_results": [prior_followup_results[entry_id] for entry_id in eligible],
    }
    manifest = {
        "schema_version": 1,
        "contract": "phase3-windows-koishi-http-followup-v2",
        "kind": "phase3_p305_windows_koishi_http_followup_v2_manifest_v1",
        "campaign_id": "p3-recovery-windows-koishi-http-followup-v2-20261026",
        "snapshot_id": recovery["snapshot_id"],
        "platform": "windows",
        "ecosystem": "koishi",
        "expected_units": 6,
        "required_stage_positions": 18,
        "stage_scope": ["install", "native", "cleanup"],
        "probe_semantics_version": SEMANTICS_VERSION,
        "source_recovery_manifest_path": base.RECOVERY_PATH.relative_to(ROOT).as_posix(),
        "source_recovery_manifest_sha256": recovery["manifest_sha256"],
        "source_recovery_manifest_file_sha256": inputs["recovery_file_sha256"],
        "artifact_lock_path": (SNAPSHOT / "artifacts.lock.json").relative_to(ROOT).as_posix(),
        "artifact_lock_sha256": sha256((SNAPSHOT / "artifacts.lock.json").read_bytes()),
        "source_profile_lock_path": BASE_PROFILE_PATH.relative_to(ROOT).as_posix(),
        "source_profile_lock_sha256": base_profile_file_sha,
        "profile_lock_path": PROFILE_PATH.relative_to(ROOT).as_posix(),
        "profile_lock_sha256": profile_sha,
        "runtime_admission_path": base.ADMISSION_PATH.relative_to(ROOT).as_posix(),
        "runtime_admission_sha256": runtime_admission_sha,
        "probe_sources": {
            "pilot_driver_path": "scripts/phase3-windows-pilot.py",
            "pilot_driver_sha256": sha256((ROOT / "scripts/phase3-windows-pilot.py").read_bytes()),
            "worker_source_path": "lab/pilot_workers.py",
            "worker_source_sha256": sha256((ROOT / "lab/pilot_workers.py").read_bytes()),
        },
        "windows_node_runtime": base.NODE_RUNTIME,
        "worker_policy": base_manifest["worker_policy"],
        "sandbox_policy_profile": policy_profile,
        "expected_previous_result_reason_code": "required_host_service_unavailable",
        "follow_up": follow_up,
        "profile_resolution": {
            "profile_builder": "scripts/phase3-windows-koishi-http-followup-v2-freeze.py",
            "profile_preflight_report_path": PREFLIGHT_PATH.relative_to(ROOT).as_posix(),
            "profile_preflight_report_sha256": preflight["report_sha256"],
            "profile_preflight_report_file_sha256": preflight_file_sha,
            "provider": {
                "registry_entry_id": HTTP_PROVIDER_ID,
                "package": HTTP_PACKAGE,
                "version": HTTP_VERSION,
                "artifact_sha256": HTTP_ARTIFACT_SHA256,
                "provides": ["http"],
                "service_kind": "cordis-service",
                "export_name": HTTP_EXPORT_NAME,
                "source_evidence": http_source_evidence,
            },
            "resolution": "official Node 24.20.0 bundled npm package-lock-only with --ignore-scripts --strict-peer-deps --engine-strict; all direct artifacts match frozen catalog SHA-512; all archive SRI verified",
        },
        "exclusions": {
            "base_campaign_id": base_manifest["campaign_id"],
            "base_manifest_sha256": base_manifest["manifest_sha256"],
            "base_report_file_sha256": prior_report_file_sha,
            "superseded_followup_campaign_id": prior_followup_manifest["campaign_id"],
            "resampled_result_ids": [f"catalog:{entry_id}|windows" for entry_id in eligible],
            "other_base_units_not_repeated": 167 - len(eligible),
            "base_outcomes_immutable": True,
        },
        "selection_algorithm": "select only the six v1 follow-up units whose exact Cordis HTTP provider class was misclassified as a Koishi plugin; preserve each v1 result digest and root P3-01 source identity without editing either prior campaign",
        "selection": selection,
    }
    manifest["manifest_sha256"] = canonical_sha256(manifest)
    manifest_raw = pretty_bytes(manifest)
    manifest_file_sha = write_immutable(MANIFEST_PATH, manifest_raw)
    profile_file_sha = write_immutable(PROFILE_PATH, profile_raw)
    if profile_file_sha != profile_sha:
        raise FollowupFreezeError("written HTTP follow-up profile lock hash changed")
    return {
        "campaign_id": manifest["campaign_id"],
        "prior_campaign_id": base_manifest["campaign_id"],
        "selected_units": len(selection),
        "profile_lock_path": PROFILE_PATH.relative_to(ROOT).as_posix(),
        "profile_lock_sha256": profile_sha,
        "profile_preflight_path": PREFLIGHT_PATH.relative_to(ROOT).as_posix(),
        "profile_preflight_file_sha256": preflight_file_sha,
        "manifest_path": MANIFEST_PATH.relative_to(ROOT).as_posix(),
        "manifest_sha256": manifest["manifest_sha256"],
        "manifest_file_sha256": manifest_file_sha,
        "eligible_registry_entry_ids": eligible,
        "combined_http_host_lock_units": locked_count,
        "pre_dispatch_terminal_units": preterminal_count,
        "worker_started": False,
        "npm_install_ran": False,
        "catalog_plugin_executed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Freeze a separate exact-provider Koishi HTTP follow-up without running catalog plugins.")
    parser.add_argument("--timeout", type=int, default=360, help="per-profile npm lock-only resolution timeout")
    args = parser.parse_args()
    try:
        print(json.dumps(generate(args.timeout), ensure_ascii=False, indent=2), flush=True)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, base.FreezeError,
            FollowupFreezeError, tarfile.TarError, TimeoutError) as error:
        print(f"Koishi HTTP follow-up freeze blocked: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
