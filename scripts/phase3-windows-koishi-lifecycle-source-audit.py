"""Inspect lock-marked npm lifecycle scripts in frozen Koishi archives offline.

The audit verifies exact tarball bytes against each target lock's SRI, reads
package manifests and native-file member hashes, and records consumer entries.
It never extracts files to disk, evaluates JavaScript, runs npm, or executes a
catalog plugin, Worker, or Sandbox.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tarfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lab.pilot_workers import _package_archive_ref  # noqa: E402

MATRIX_PATH = ROOT / "outputs/acceptance/phase3-p305-windows-koishi-entry-requirements-matrix-v1-20261026.json"
MATRIX_EXPECTED_CANONICAL_SHA = "e5fa8a4a67dcb3a0ba771d36ec137c8386768919e83e5a292ba0e88feebfb042"
OUTPUT_PATH = ROOT / "outputs/acceptance/phase3-p305-windows-koishi-lifecycle-source-audit-v1-20261026.json"
EXPECTED_OCCURRENCES = 113
LIFECYCLE_KEYS = ("preinstall", "install", "postinstall", "prepare")
NATIVE_SUFFIXES = {".node", ".dll", ".exe"}


class AuditError(RuntimeError):
    """Raised when an input identity, archive, or join fails validation."""


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return sha256_bytes(raw)


def read_json(path: Path) -> tuple[Any, bytes]:
    raw = path.read_bytes()
    try:
        return json.loads(raw.decode("utf-8")), raw
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuditError(f"invalid UTF-8 JSON: {path}: {exc}") from exc


def verify_sidecar(path: Path) -> str:
    actual = sha256_file(path)
    sidecar = path.with_suffix(path.suffix + ".sha256")
    if sidecar.is_file():
        expected = sidecar.read_text(encoding="utf-8").strip().split()[0]
        if expected != actual:
            raise AuditError(f"sidecar mismatch: {path}: expected={expected}, actual={actual}")
    return actual


def package_name_from_lock_path(path: str) -> str:
    marker = "node_modules/"
    offset = path.rfind(marker)
    if offset < 0:
        return path
    return path[offset + len(marker):]


def platform_constraint_excludes(values: Any, accepted: set[str]) -> bool:
    if values is None:
        return False
    if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
        raise AuditError(f"malformed npm os/cpu constraints: {values!r}")
    normalized = {item.lower() for item in values}
    if any(item.startswith("!") and item[1:] in accepted for item in normalized):
        return True
    positive = {item for item in normalized if not item.startswith("!")}
    return bool(positive) and not bool(positive.intersection(accepted))


def target_windows_x64_constraint_status(os_values: Any, cpu_values: Any) -> str:
    if platform_constraint_excludes(os_values, {"win32", "windows"}):
        return "excluded_by_os_constraint"
    if platform_constraint_excludes(cpu_values, {"x64"}):
        return "excluded_by_cpu_constraint"
    if isinstance(os_values, list) and isinstance(cpu_values, list):
        return "explicitly_allows_win32_x64"
    return "not_excluded_by_declared_constraints"


def inspect_archive(path: Path, expected_algorithm: str, expected_digest: str) -> dict[str, Any]:
    if not path.is_file():
        raise AuditError(f"frozen lifecycle dependency archive missing: {path}")
    actual_integrity = hashlib.new(expected_algorithm)
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            actual_integrity.update(chunk)
    if actual_integrity.hexdigest() != expected_digest:
        raise AuditError(f"archive lock integrity mismatch: {path}")
    archive_sha = sha256_file(path)
    try:
        with tarfile.open(path, mode="r:gz") as archive:
            members = [item for item in archive.getmembers() if item.isfile()]
            manifests = [
                item for item in members
                if item.name.endswith("/package.json") and item.name.count("/") == 1
            ]
            if len(manifests) != 1:
                raise AuditError(f"package manifest missing/ambiguous in {path}: {len(manifests)}")
            package_root = manifests[0].name.rsplit("/", 1)[0]
            manifest_stream = archive.extractfile(manifests[0])
            if manifest_stream is None:
                raise AuditError(f"package manifest unreadable in {path}")
            manifest_raw = manifest_stream.read()
            try:
                manifest = json.loads(manifest_raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise AuditError(f"invalid package manifest in {path}: {exc}") from exc
            native_files = []
            for item in members:
                suffix = Path(item.name).suffix.lower()
                if suffix not in NATIVE_SUFFIXES:
                    continue
                member_stream = archive.extractfile(item)
                if member_stream is None:
                    raise AuditError(f"native-looking tar member unreadable: {item.name}")
                digest = hashlib.sha256()
                for chunk in iter(lambda: member_stream.read(1024 * 1024), b""):
                    digest.update(chunk)
                native_files.append({"member": item.name, "size_bytes": item.size, "sha256": digest.hexdigest()})
    except (OSError, tarfile.TarError) as exc:
        raise AuditError(f"cannot read frozen npm tarball {path}: {exc}") from exc
    scripts = manifest.get("scripts", {}) if isinstance(manifest, dict) else {}
    if not isinstance(scripts, dict):
        scripts = {}
    return {
        "archive_path": str(path),
        "archive_size_bytes": path.stat().st_size,
        "archive_sha256": archive_sha,
        "lock_integrity_algorithm": expected_algorithm,
        "lock_integrity_digest": expected_digest,
        "lock_integrity_matched": True,
        "package_manifest_sha256": sha256_bytes(manifest_raw),
        "package_manifest_name": manifest.get("name"),
        "package_manifest_version": manifest.get("version"),
        "lifecycle_scripts": {key: scripts[key] for key in LIFECYCLE_KEYS if key in scripts},
        "binding_gyp_present": any(item.name == f"{package_root}/binding.gyp" for item in members),
        "gypfile_declared": manifest.get("gypfile") is True,
        "binary_metadata": manifest.get("binary"),
        "os_constraints": manifest.get("os"),
        "cpu_constraints": manifest.get("cpu"),
        "native_files_in_archive": sorted(native_files, key=lambda item: item["member"]),
    }


def build_report(generated_at_utc: str | None = None) -> dict[str, Any]:
    matrix, _ = read_json(MATRIX_PATH)
    if not isinstance(matrix, dict):
        raise AuditError("matrix report must be an object")
    matrix_canonical = canonical_sha256({key: value for key, value in matrix.items() if key != "report_sha256"})
    if matrix_canonical != matrix.get("report_sha256") or matrix_canonical != MATRIX_EXPECTED_CANONICAL_SHA:
        raise AuditError(f"matrix report identity changed: {matrix_canonical}")
    matrix_file_sha = verify_sidecar(MATRIX_PATH)
    if matrix.get("summary", {}).get("has_install_script_dependency_occurrences") != EXPECTED_OCCURRENCES:
        raise AuditError("matrix lifecycle occurrence count differs from the frozen expectation")

    inputs = matrix.get("inputs", {})
    profiles_path = ROOT / "catalogs/p2-41649744e057d61031d5/profiles.lock.json"
    profile_raw = profiles_path.read_bytes()
    profile_sha = sha256_bytes(profile_raw)
    if profile_sha != inputs.get("profiles_lock_file_sha256"):
        raise AuditError("current profile lock differs from the matrix-bound profile lock")
    profiles = json.loads(profile_raw.decode("utf-8"))
    profile_by_id = {row.get("registry_entry_id"): row for row in profiles.get("entries", [])}
    cache_root = Path(inputs["cache_audit"]["frozen_cache_root"])
    archive_cache_root = cache_root / "dependency-archives"
    target_lock_root = cache_root / "profile-locks/koishi/sha256"

    package_records: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    occurrence_count = 0
    consumer_count = 0
    lock_sha_by_id = {}
    for unit in matrix.get("units", []):
        entry_id = unit["registry_entry_id"]
        lock_sha = unit["target_dependency_lock_sha256"]
        lock_path = target_lock_root / lock_sha[:2] / lock_sha
        lock_raw = lock_path.read_bytes()
        if sha256_bytes(lock_raw) != lock_sha:
            raise AuditError(f"target lock blob SHA mismatch: {entry_id}")
        lock_sha_by_id[entry_id] = lock_sha
        profile_row = profile_by_id.get(entry_id, {})
        lock_ref = ((profile_row.get("profiles") or {}).get("windows-x86_64") or {}).get("dependency_lock") or {}
        if lock_ref.get("sha256") != lock_sha:
            raise AuditError(f"profile-lock target reference mismatch: {entry_id}")
        lock_doc = json.loads(lock_raw.decode("utf-8"))
        for dependency_path, dependency in lock_doc.get("packages", {}).items():
            if dependency.get("hasInstallScript") is not True:
                continue
            url, integrity = dependency.get("resolved"), dependency.get("integrity")
            if not isinstance(url, str) or not isinstance(integrity, str):
                raise AuditError(f"lock-marked package lacks url/integrity: {entry_id} {dependency_path}")
            algorithm, expected_digest, relative = _package_archive_ref(url, integrity, entry_path=dependency_path)
            archive_path = cache_root / relative
            key = (package_name_from_lock_path(dependency_path), str(dependency.get("version")), url, integrity)
            row = package_records.setdefault(key, {
                "package": key[0],
                "version": key[1],
                "resolved": url,
                "integrity": integrity,
                "archive_path": archive_path,
                "algorithm": algorithm,
                "expected_digest": expected_digest,
                "occurrences": [],
            })
            row["occurrences"].append({
                "registry_entry_id": entry_id,
                "consumer_package": unit["package"],
                "consumer_version": unit["version"],
                "dependency_path": dependency_path,
                "optional": dependency.get("optional") is True,
                "os_constraints": dependency.get("os"),
                "cpu_constraints": dependency.get("cpu"),
                "windows_x64_constraint_status": target_windows_x64_constraint_status(dependency.get("os"), dependency.get("cpu")),
            })
            occurrence_count += 1
    if occurrence_count != EXPECTED_OCCURRENCES:
        raise AuditError(f"recomputed target lock occurrence count differs: {occurrence_count}")

    package_audits = []
    lifecycle_signatures = Counter()
    windows_x64_constraint_counts = Counter()
    windows_x64_explicit_lifecycle_occurrences = 0
    scriptless_markers = []
    identity_mismatches = []
    native_archive_files = 0
    for key, row in sorted(package_records.items()):
        archive_info = inspect_archive(row["archive_path"], row["algorithm"], row["expected_digest"])
        if archive_info["package_manifest_name"] != row["package"] or archive_info["package_manifest_version"] != row["version"]:
            identity_mismatches.append({
                "package": row["package"],
                "version": row["version"],
                "manifest_name": archive_info["package_manifest_name"],
                "manifest_version": archive_info["package_manifest_version"],
                "resolved": row["resolved"],
            })
        if not archive_info["lifecycle_scripts"] and not archive_info["binding_gyp_present"]:
            scriptless_markers.append({
                "package": row["package"],
                "version": row["version"],
                "resolved": row["resolved"],
                "os_constraints": archive_info["os_constraints"],
                "cpu_constraints": archive_info["cpu_constraints"],
                "native_files_in_archive": archive_info["native_files_in_archive"],
                "lock_occurrence_count": len(row["occurrences"]),
            })
        signature = json.dumps(archive_info["lifecycle_scripts"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        lifecycle_signatures[signature] += len(row["occurrences"])
        for occurrence in row["occurrences"]:
            windows_x64_constraint_counts[occurrence["windows_x64_constraint_status"]] += 1
            if (
                not occurrence["windows_x64_constraint_status"].startswith("excluded_by_")
                and archive_info["lifecycle_scripts"]
            ):
                windows_x64_explicit_lifecycle_occurrences += 1
        native_archive_files += len(archive_info["native_files_in_archive"])
        package_audits.append({
            "package": row["package"],
            "version": row["version"],
            "resolved": row["resolved"],
            "integrity": row["integrity"],
            "target_lock_has_install_script_marker": True,
            **{key: value for key, value in archive_info.items() if key != "archive_path"},
            "lock_occurrence_count": len(row["occurrences"]),
            "consumer_entry_count": len({item["registry_entry_id"] for item in row["occurrences"]}),
            "windows_x64_constraint_excluded_occurrence_count": sum(item["windows_x64_constraint_status"].startswith("excluded_by_") for item in row["occurrences"]),
            "consumer_entries": sorted(row["occurrences"], key=lambda item: (item["registry_entry_id"], item["dependency_path"])),
        })
    if identity_mismatches:
        raise AuditError(f"package name/version mismatch for {len(identity_mismatches)} frozen lifecycle packages")

    summary = {
        "target_units": len(matrix.get("units", [])),
        "target_lock_blobs_reverified": len(lock_sha_by_id),
        "has_install_script_occurrences": occurrence_count,
        "windows_x64_constraint_status_counts": dict(sorted(windows_x64_constraint_counts.items())),
        "windows_x64_occurrences_not_excluded_by_lock_metadata": occurrence_count - sum(count for status, count in windows_x64_constraint_counts.items() if status.startswith("excluded_by_")),
        "windows_x64_occurrences_with_explicit_lifecycle_scripts_not_excluded_by_lock_metadata": windows_x64_explicit_lifecycle_occurrences,
        "unique_lifecycle_archive_identities": len(package_audits),
        "unique_lifecycle_package_names": len({row["package"] for row in package_audits}),
        "package_archives_sri_verified": sum(row["lock_integrity_matched"] is True for row in package_audits),
        "package_manifest_identity_matches": len(package_audits) - len(identity_mismatches),
        "packages_with_explicit_lifecycle_scripts": sum(bool(row["lifecycle_scripts"]) for row in package_audits),
        "lock_marked_packages_without_explicit_lifecycle_or_binding_gyp": len(scriptless_markers),
        "lock_marked_packages_without_explicit_lifecycle_or_binding_gyp_detail": scriptless_markers,
        "native_files_observed_in_archives": native_archive_files,
        "packages_with_win32_x64_prebuilt_node_payload": sum(
            any(item["member"].lower().endswith(".node") and ("win32-x64" in item["member"].lower() or "win32_x64" in item["member"].lower()) for item in row["native_files_in_archive"])
            for row in package_audits
        ),
        "win32_x64_prebuilt_node_payloads": [
            {
                "package": row["package"],
                "version": row["version"],
                "members": [item for item in row["native_files_in_archive"] if item["member"].lower().endswith(".node") and ("win32-x64" in item["member"].lower() or "win32_x64" in item["member"].lower())],
            }
            for row in package_audits
            if any(item["member"].lower().endswith(".node") and ("win32-x64" in item["member"].lower() or "win32_x64" in item["member"].lower()) for item in row["native_files_in_archive"])
        ],
        "lifecycle_signature_occurrence_counts": dict(sorted(lifecycle_signatures.items())),
        "identity_mismatches": identity_mismatches,
        "profile_ready": False,
        "interpretation": "This audit exposes commands and archive contents only. Lifecycle scripts remain disabled; the audit does not establish that a script is safe to run or that any native file is loadable or behaviorally compatible on Windows.",
    }
    without_hash: dict[str, Any] = {
        "schema_version": 1,
        "kind": "phase3_p305_windows_koishi_lifecycle_source_audit",
        "generated_at_utc": generated_at_utc or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "scope": "offline inspection of exact SRI-locked npm tarballs referenced by the 167 frozen Windows Koishi target locks; no extraction to disk, install, lifecycle script, package code, Worker, or Sandbox execution",
        "inputs": {
            "matrix_path": MATRIX_PATH.relative_to(ROOT).as_posix(),
            "matrix_canonical_sha256": matrix_canonical,
            "matrix_file_sha256": matrix_file_sha,
            "profiles_lock_file_sha256": profile_sha,
            "catalog_snapshot_id": inputs.get("catalog_snapshot_id"),
            "target_recovery_manifest_canonical_sha256": inputs.get("recovery_manifest_canonical_sha256"),
            "cache_root": str(cache_root),
            "lock_script_occurrence_count": occurrence_count,
        },
        "summary": summary,
        "lifecycle_packages": package_audits,
        "limitations": [
            "A lock hasInstallScript marker or package.json lifecycle command does not show what an executed script would do under the Sandbox environment.",
            "Native archive members are hash observations only; no file was extracted to disk or loaded.",
            "This report does not authorize lifecycle scripts, modify the Worker no-scripts policy, or create a combined Host profile.",
            "A complete independent profile, per-entry provider config/combined lock, native binding policy, manifest, checkpoint, report, and validate-only result are still required before dispatch.",
        ],
    }
    return {**without_hash, "report_sha256": canonical_sha256(without_hash)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--check", action="store_true")
    options = parser.parse_args()
    if options.check:
        existing, _ = read_json(options.output)
        report = build_report(existing.get("generated_at_utc") if isinstance(existing, dict) else None)
        expected = report["report_sha256"]
        if not isinstance(existing, dict) or existing.get("report_sha256") != expected:
            raise AuditError(f"existing lifecycle report differs from frozen inputs: expected={expected}, actual={existing.get('report_sha256') if isinstance(existing, dict) else None}")
        print(json.dumps({"status": "verified", "canonical_sha256": expected, "file_sha256": verify_sidecar(options.output), "summary": report["summary"]}, ensure_ascii=False, indent=2))
        return 0
    report = build_report()
    output = options.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    output.write_bytes(raw)
    file_sha = sha256_bytes(raw)
    output.with_suffix(output.suffix + ".sha256").write_text(f"{file_sha}  {output.name}\n", encoding="ascii", newline="\n")
    print(json.dumps({"status": "written", "path": str(output), "canonical_sha256": report["report_sha256"], "file_sha256": file_sha, "summary": report["summary"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, KeyError, TypeError, ValueError, AuditError) as exc:
        print(f"lifecycle source audit failed: {exc}")
        raise SystemExit(2)
