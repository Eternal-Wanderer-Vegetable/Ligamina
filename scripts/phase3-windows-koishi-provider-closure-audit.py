"""Verify the cached dependency-archive closure of frozen Koishi providers.

This audit reads the existing Windows x64 npm lock blobs for hash-verified
provider candidates. It never installs packages or executes lifecycle scripts,
plugins, Workers, or Sandbox code. With --fetch-missing, it retrieves only
archive URLs already present in those lock blobs and verifies each frozen SRI
before writing the content-addressed bytes to the external artifact cache.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lab.pilot_workers import _download_npm_archive, _package_archive_ref  # noqa: E402

MATRIX_PATH = ROOT / "outputs/acceptance/phase3-p305-windows-koishi-entry-requirements-matrix-v1-20261026.json"
SOURCE_AUDIT_PATH = ROOT / "outputs/acceptance/phase3-p305-windows-koishi-service-source-audit-v1-20261026.json"
PROFILE_LOCK_PATH = ROOT / "catalogs/p2-41649744e057d61031d5/profiles.lock.json"
OUTPUT_PATH = ROOT / "outputs/acceptance/phase3-p305-windows-koishi-provider-closure-audit-v1-20261026.json"


class AuditError(RuntimeError):
    pass


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def canonical_sha256(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(raw)


def read_json(path: Path) -> tuple[dict[str, Any], bytes]:
    raw = path.read_bytes()
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise AuditError(f"expected JSON object: {path}")
    return value, raw


def verify_sidecar(path: Path) -> str:
    actual = sha256(path.read_bytes())
    sidecar = path.with_suffix(path.suffix + ".sha256")
    if not sidecar.is_file():
        raise AuditError(f"missing sidecar: {sidecar}")
    expected = sidecar.read_text(encoding="utf-8").strip().split()[0]
    if expected != actual:
        raise AuditError(f"sidecar mismatch: {path}: expected={expected} actual={actual}")
    return actual


def verify_report(path: Path) -> tuple[dict[str, Any], str, str]:
    report, raw = read_json(path)
    canonical = canonical_sha256({key: value for key, value in report.items() if key != "report_sha256"})
    if report.get("report_sha256") != canonical:
        raise AuditError(f"report canonical SHA mismatch: {path}")
    return report, canonical, verify_sidecar(path)


def provider_ids(matrix: dict[str, Any], source_audit: dict[str, Any]) -> dict[str, dict[str, Any]]:
    providers: dict[str, dict[str, Any]] = {}
    for unit in matrix.get("units", []):
        for requirement in unit.get("service_requirements", []):
            for candidate in requirement.get("candidates", []):
                if candidate.get("provider_windows_lock_blob_hash_matched") is True:
                    registry_id = candidate["registry_entry_id"]
                    provider = providers.setdefault(
                        registry_id,
                        {
                            "registry_entry_id": registry_id,
                            "package": candidate["package"],
                            "version": candidate["version"],
                            "candidate_services": set(),
                            "candidate_origins": set(),
                        },
                    )
                    if (provider["package"], provider["version"]) != (candidate["package"], candidate["version"]):
                        raise AuditError(f"candidate identity conflicts for {registry_id}")
                    provider["candidate_services"].add(requirement["service_name"])
                    provider["candidate_origins"].add(candidate["origin"])

    for archive in source_audit.get("provider_archives", []):
        registry_id = archive["registry_entry_id"]
        provider = providers.setdefault(
            registry_id,
            {
                "registry_entry_id": registry_id,
                "package": archive["package"],
                "version": archive.get("version"),
                "candidate_services": set(),
                "candidate_origins": set(),
            },
        )
        if provider["package"] != archive["package"]:
            raise AuditError(f"source crosswalk package conflicts for {registry_id}")
        for service in archive.get("services", []):
            provider["candidate_services"].add(service["service_name"])
        provider["candidate_origins"].add("source_archive_crosswalk")

    return providers


def audit(cache_root: Path, generated_at_utc: str, fetch_missing: bool) -> tuple[dict[str, Any], dict[str, int]]:
    matrix, _ = read_json(MATRIX_PATH)
    matrix, matrix_canonical_sha, matrix_file_sha = verify_report(MATRIX_PATH)
    source_audit, source_canonical_sha, source_file_sha = verify_report(SOURCE_AUDIT_PATH)
    profile_lock, profile_lock_raw = read_json(PROFILE_LOCK_PATH)

    matrix_inputs = matrix.get("inputs", {})
    if matrix_canonical_sha != matrix.get("report_sha256"):
        raise AuditError("matrix identity is not self-consistent")
    if source_audit.get("inputs", {}).get("profiles_lock_file_sha256") != sha256(profile_lock_raw):
        raise AuditError("provider source crosswalk references a different catalog profiles lock")
    if matrix_inputs.get("profiles_lock_file_sha256") != sha256(profile_lock_raw):
        raise AuditError("requirements matrix references a different catalog profiles lock")
    expected_cache_root = Path(matrix_inputs["cache_audit"]["frozen_cache_root"]).resolve()
    cache_root = cache_root.resolve()
    if cache_root != expected_cache_root:
        raise AuditError(f"cache root differs from the frozen audit input: expected={expected_cache_root}")

    providers = provider_ids(matrix, source_audit)
    profile_entries = {
        entry["registry_entry_id"]: entry for entry in profile_lock.get("entries", [])
    }
    package_locks: list[dict[str, Any]] = []
    archive_refs: dict[tuple[str, str], dict[str, Any]] = {}
    missing_locks: list[str] = []
    invalid_locks: list[str] = []
    unresolved_refs: list[dict[str, Any]] = []
    candidate_archive_refs = 0

    for registry_id, provider in sorted(providers.items()):
        entry = profile_entries.get(registry_id)
        if not entry or entry.get("ecosystem") != "koishi":
            missing_locks.append(registry_id)
            continue
        if (entry.get("package"), entry.get("advertised_version")) != (provider["package"], provider["version"]):
            raise AuditError(f"provider candidate identity differs from profiles lock: {registry_id}")
        profile = entry.get("profiles", {}).get("windows-x86_64", {})
        lock_ref = profile.get("dependency_lock")
        if profile.get("status") != "locked" or not isinstance(lock_ref, dict):
            missing_locks.append(registry_id)
            continue
        lock_path = (cache_root / lock_ref["cache_path"]).resolve()
        try:
            lock_path.relative_to(cache_root)
        except ValueError as error:
            raise AuditError(f"profile lock path escaped cache root: {registry_id}") from error
        if not lock_path.is_file():
            missing_locks.append(registry_id)
            continue
        lock_raw = lock_path.read_bytes()
        lock_sha = sha256(lock_raw)
        if len(lock_raw) != lock_ref.get("size_bytes") or lock_sha != lock_ref.get("sha256"):
            invalid_locks.append(registry_id)
            continue
        lock = json.loads(lock_raw.decode("utf-8"))
        package_count = 0
        lifecycle_count = 0
        lock_archive_count = 0
        for package_path, package in lock.get("packages", {}).items():
            if not package_path:
                continue
            package_count += 1
            lifecycle_count += int(package.get("hasInstallScript") is True)
            url = package.get("resolved")
            integrity = package.get("integrity")
            if not isinstance(url, str) or not isinstance(integrity, str):
                unresolved_refs.append({
                    "registry_entry_id": registry_id,
                    "package_path": package_path,
                    "package": package.get("name"),
                    "version": package.get("version"),
                    "resolved": url,
                    "reason": "missing_resolved_url_or_integrity",
                })
                continue
            try:
                algorithm, digest, relative = _package_archive_ref(url, integrity, entry_path=package_path)
            except Exception as error:
                unresolved_refs.append({
                    "registry_entry_id": registry_id,
                    "package_path": package_path,
                    "package": package.get("name"),
                    "version": package.get("version"),
                    "resolved": url,
                    "reason": str(error),
                })
                continue
            key = (algorithm, digest)
            record = archive_refs.setdefault(
                key,
                {
                    "algorithm": algorithm,
                    "digest_hex": digest,
                    "cache_path": relative.as_posix(),
                    "resolved": url,
                    "integrity": integrity,
                    "references": [],
                },
            )
            if record["integrity"] != integrity:
                raise AuditError(f"same archive digest has inconsistent SRI values: {digest}")
            record["references"].append({
                "registry_entry_id": registry_id,
                "package_path": package_path,
                "package": package.get("name"),
                "version": package.get("version"),
            })
            lock_archive_count += 1
        candidate_archive_refs += lock_archive_count
        package_locks.append({
            "registry_entry_id": registry_id,
            "package": provider["package"],
            "version": provider["version"],
            "profile_id": profile.get("profile_id"),
            "dependency_lock_sha256": lock_sha,
            "dependency_lock_size_bytes": len(lock_raw),
            "candidate_services": sorted(provider["candidate_services"]),
            "candidate_origins": sorted(provider["candidate_origins"]),
            "package_records": package_count,
            "has_install_script_records": lifecycle_count,
            "resolved_archive_references": lock_archive_count,
        })

    missing_archives: list[dict[str, Any]] = []
    mismatched_archives: list[dict[str, Any]] = []
    verified_bytes = 0
    downloaded_archive_count = 0
    downloaded_bytes = 0
    for key, record in sorted(archive_refs.items()):
        archive_path = cache_root / record["cache_path"]
        if not archive_path.is_file() and fetch_missing:
            try:
                _, size, already_cached = _download_npm_archive(
                    record["resolved"], record["integrity"], cache_root, entry_path=record["references"][0]["package_path"]
                )
                if not already_cached:
                    downloaded_archive_count += 1
                    downloaded_bytes += size
            except Exception as error:
                missing_archives.append({**record, "reason": f"fetch_failed: {error}"})
                continue
        if not archive_path.is_file():
            missing_archives.append(record)
            continue
        digest = hashlib.new(record["algorithm"])
        size_bytes = 0
        with archive_path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                size_bytes += len(chunk)
        actual = digest.hexdigest()
        if actual != record["digest_hex"]:
            mismatched_archives.append({**record, "actual_digest_hex": actual})
            continue
        verified_bytes += size_bytes
        record["size_bytes"] = size_bytes
        record["status"] = "verified"

    summary = {
        "unique_provider_entries": len(providers),
        "provider_lock_blobs_verified": len(package_locks),
        "package_records_in_provider_locks": sum(item["package_records"] for item in package_locks),
        "provider_lock_archive_references": candidate_archive_refs,
        "unique_locked_archives": len(archive_refs),
        "locked_archives_verified": len(archive_refs) - len(missing_archives) - len(mismatched_archives),
        "missing_archives": len(missing_archives),
        "mismatched_archives": len(mismatched_archives),
        "unresolved_package_refs": len(unresolved_refs),
        "archive_bytes_verified": verified_bytes,
        "combined_target_host_locks_frozen": 0,
        "provider_configurations_frozen": 0,
        "profile_ready": False,
    }
    blockers = []
    if missing_locks:
        blockers.append({"reason": "provider_windows_lock_missing", "registry_entry_ids": sorted(missing_locks)})
    if invalid_locks:
        blockers.append({"reason": "provider_windows_lock_hash_mismatch", "registry_entry_ids": sorted(invalid_locks)})
    if missing_archives:
        blockers.append({"reason": "provider_locked_archive_missing", "count": len(missing_archives)})
    if mismatched_archives:
        blockers.append({"reason": "provider_locked_archive_hash_mismatch", "count": len(mismatched_archives)})
    if unresolved_refs:
        blockers.append({"reason": "provider_lock_reference_unresolved", "count": len(unresolved_refs)})
    blockers.extend([
        {"reason": "provider_candidates_not_selected_or_configured_per_target"},
        {"reason": "combined_target_host_locks_not_frozen", "target_units": 167},
    ])
    report_body = {
        "schema_version": 1,
        "kind": "phase3_p305_windows_koishi_provider_closure_audit",
        "generated_at_utc": generated_at_utc,
        "scope": "offline verification of cached Windows x64 dependency archives in hash-verified Koishi provider candidate locks; no package installation, lifecycle script, plugin, Worker, or Sandbox execution",
        "inputs": {
            "requirements_matrix_path": MATRIX_PATH.relative_to(ROOT).as_posix(),
            "requirements_matrix_canonical_sha256": matrix_canonical_sha,
            "requirements_matrix_file_sha256": matrix_file_sha,
            "service_source_audit_path": SOURCE_AUDIT_PATH.relative_to(ROOT).as_posix(),
            "service_source_audit_canonical_sha256": source_canonical_sha,
            "service_source_audit_file_sha256": source_file_sha,
            "profiles_lock_path": PROFILE_LOCK_PATH.relative_to(ROOT).as_posix(),
            "profiles_lock_file_sha256": sha256(profile_lock_raw),
            "external_cache_root": str(cache_root),
        },
        "summary": summary,
        "provider_lock_blobs": package_locks,
        "unique_archive_refs": [archive_refs[key] for key in sorted(archive_refs)],
        "missing_provider_lock_ids": sorted(missing_locks),
        "invalid_provider_lock_ids": sorted(invalid_locks),
        "missing_archives": missing_archives,
        "mismatched_archives": mismatched_archives,
        "unresolved_package_refs": unresolved_refs,
        "blockers": blockers,
        "limitations": [
            "A fully verified provider dependency archive closure is not a provider selection, target-entry configuration, or combined Host profile lock.",
            "The archive audit does not establish that lifecycle scripts are safe or necessary, that native payloads load, or that plugin behavior is compatible.",
            "Target-entry git+ssh dependencies and sqlite3/Skia native/resource installation routes remain separate blockers.",
        ],
    }
    report = {**report_body, "report_sha256": canonical_sha256(report_body)}
    return report, {
        "downloaded_archive_count": downloaded_archive_count,
        "downloaded_bytes": downloaded_bytes,
    }


def write_report(path: Path, report: dict[str, Any]) -> tuple[str, str]:
    raw = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    file_digest = sha256(raw)
    sidecar = path.with_suffix(path.suffix + ".sha256")
    sidecar.write_text(f"{file_digest}  {path.name}\n", encoding="ascii", newline="\n")
    return report["report_sha256"], file_digest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-root", type=Path, default=Path(r"E:\stella\Ligamina-artifact-cache"))
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--fetch-missing", action="store_true", help="fetch only exact frozen npm registry URLs with their lock SRI")
    parser.add_argument("--check", action="store_true", help="verify the saved report against current frozen inputs and cache bytes")
    args = parser.parse_args()
    if args.fetch_missing and args.check:
        parser.error("--fetch-missing cannot be combined with --check")

    try:
        if args.check:
            saved, _, saved_file_sha = verify_report(args.output)
            report, _ = audit(args.cache_root, saved["generated_at_utc"], fetch_missing=False)
            if report["report_sha256"] != saved["report_sha256"]:
                raise AuditError("saved provider closure report is stale for current frozen inputs/cache")
            print(json.dumps({
                "status": "verified" if not report["summary"]["missing_archives"] and not report["summary"]["mismatched_archives"] and not report["summary"]["unresolved_package_refs"] else "blocked",
                "canonical_sha256": report["report_sha256"],
                "file_sha256": saved_file_sha,
                "summary": report["summary"],
                "blockers": report["blockers"],
            }, ensure_ascii=False, indent=2))
            return 0 if report["report_sha256"] == saved["report_sha256"] else 2

        generated_at_utc = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        report, actions = audit(args.cache_root, generated_at_utc, fetch_missing=args.fetch_missing)
        canonical, file_digest = write_report(args.output, report)
        passed = (
            not report["summary"]["missing_archives"]
            and not report["summary"]["mismatched_archives"]
            and not report["summary"]["unresolved_package_refs"]
            and not report["missing_provider_lock_ids"]
            and not report["invalid_provider_lock_ids"]
        )
        print(json.dumps({
            "status": "verified" if passed else "blocked",
            "path": str(args.output),
            "canonical_sha256": canonical,
            "file_sha256": file_digest,
            "summary": report["summary"],
            "cache_population_this_run": actions,
            "blockers": report["blockers"],
        }, ensure_ascii=False, indent=2))
        return 0 if passed else 2
    except (AuditError, OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"provider closure audit failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
