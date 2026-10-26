"""Audit frozen Koishi service-provider candidates without installing or running them.

The report maps statically discovered `inject` names to exact catalog
implementations and verifies candidate tarball/lock-file bytes in the local
cache. A provider candidate is not a selected Host profile and does not prove
that the target plugin can load or behave correctly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import tarfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE_ROOT = ROOT.parent / "Ligamina-artifact-cache"
BUILTIN_DOC_NAMES = {"http", "i18n"}
DOCS = {
    "service_plugins": "https://koishi.chat/en-US/market/service",
    "core_context": "https://koishi.chat/en-US/api/core/context",
    "publishing_metadata": "https://koishi.chat/en-US/guide/develop/publish",
    "database": "https://koishi.chat/en-US/guide/database/",
}


class AuditError(RuntimeError):
    """Raised when a frozen identity or cached provider input does not verify."""


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


def read_json(path: Path) -> tuple[dict[str, Any], bytes]:
    raw = path.read_bytes()
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise AuditError(f"expected JSON object: {path}")
    return value, raw


def verify_sidecar(path: Path, expected_file_sha: str | None = None) -> str:
    actual = sha256_file(path)
    if expected_file_sha is not None and actual != expected_file_sha:
        raise AuditError(f"bound input file SHA mismatch for {path}: {actual}")
    sidecar = path.with_suffix(path.suffix + ".sha256")
    if sidecar.is_file():
        expected = sidecar.read_text(encoding="utf-8").strip().split()[0]
        if expected != actual:
            raise AuditError(f"sidecar SHA mismatch for {path}: sidecar={expected}, actual={actual}")
    return actual


def verify_manifest(path: Path, expected: str, label: str) -> dict[str, Any]:
    value, _ = read_json(path)
    without_sha = {key: item for key, item in value.items() if key != "manifest_sha256"}
    actual = canonical_sha256(without_sha)
    if value.get("manifest_sha256") != actual or actual != expected:
        raise AuditError(f"{label} frozen manifest SHA mismatch: expected={expected}, actual={actual}")
    return value


def package_service_implements(archive_path: Path) -> tuple[str | None, list[str] | None, str | None, str | None]:
    try:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            matches = [item for item in archive.getmembers() if item.isfile() and item.name == "package/package.json"]
            if len(matches) != 1:
                return None, None, None, "package_manifest_missing_or_ambiguous"
            stream = archive.extractfile(matches[0])
            if stream is None:
                return None, None, None, "package_manifest_unreadable"
            metadata = json.loads(stream.read().decode("utf-8"))
    except (OSError, tarfile.TarError, UnicodeDecodeError, json.JSONDecodeError):
        return None, None, None, "package_archive_unreadable"
    koishi = metadata.get("koishi", {})
    service = koishi.get("service", {}) if isinstance(koishi, dict) else {}
    implements = service.get("implements") if isinstance(service, dict) else None
    if not isinstance(implements, list) or any(not isinstance(name, str) for name in implements):
        implements = None
    return metadata.get("name"), implements, metadata.get("version"), None


def run(options: argparse.Namespace) -> dict[str, Any]:
    static_path = options.static_audit.resolve(strict=True)
    static, static_raw = read_json(static_path)
    static_without_sha = {key: value for key, value in static.items() if key != "report_sha256"}
    if static.get("report_sha256") != canonical_sha256(static_without_sha):
        raise AuditError("static audit canonical SHA mismatch")
    static_file_sha = verify_sidecar(static_path)
    inputs = static.get("inputs", {})

    recovery_path = ROOT / inputs["recovery_manifest_path"]
    pilot_path = ROOT / inputs["closed_pilot_manifest_path"]
    verify_manifest(recovery_path, inputs["recovery_manifest_canonical_sha256"], "recovery")
    verify_manifest(pilot_path, inputs["closed_pilot_manifest_canonical_sha256"], "closed Koishi pilot")

    catalog_path = ROOT / "catalogs/p2-41649744e057d61031d5/catalog.json"
    artifacts_path = ROOT / inputs["artifact_lock_path"]
    profiles_path = ROOT / inputs["profiles_lock_path"]
    cache_root = options.cache_root.resolve(strict=True)
    catalog, catalog_raw = read_json(catalog_path)
    artifacts, artifacts_raw = read_json(artifacts_path)
    profiles, profiles_raw = read_json(profiles_path)
    if sha256_bytes(artifacts_raw) != inputs["artifact_lock_file_sha256"]:
        raise AuditError("artifact lock file differs from static-audit input")
    if sha256_bytes(profiles_raw) != inputs["profiles_lock_file_sha256"]:
        raise AuditError("profile lock file differs from static-audit input")
    if artifacts.get("snapshot_id") != catalog.get("snapshot_id") or profiles.get("snapshot_id") != catalog.get("snapshot_id"):
        raise AuditError("catalog/artifact/profile snapshot IDs differ")
    if profiles.get("artifacts_lock_sha256") != sha256_bytes(artifacts_raw):
        raise AuditError("profile lock is not bound to current artifact lock bytes")
    verify_sidecar(artifacts_path)
    verify_sidecar(profiles_path)

    selected = static.get("selection", [])
    selected_by_id = {row.get("registry_entry_id"): row for row in selected}
    if len(selected) != 167 or len(selected_by_id) != 167:
        raise AuditError("expected 167 unique remaining Windows Koishi units")
    required_by_service: dict[str, list[dict[str, str]]] = {}
    for row in selected:
        for service in row.get("static_required_services", []):
            required_by_service.setdefault(service, []).append({
                "registry_entry_id": row["registry_entry_id"],
                "package": row["package"],
                "version": row["version"],
            })
    service_names = sorted(required_by_service)

    artifacts_by_id = {
        row.get("registry_entry_id"): row
        for row in artifacts.get("entries", [])
        if row.get("ecosystem") == "koishi"
    }
    profiles_by_id = {
        row.get("registry_entry_id"): row
        for row in profiles.get("entries", [])
        if row.get("ecosystem") == "koishi"
    }
    providers_by_service: dict[str, list[dict[str, Any]]] = {name: [] for name in service_names}
    for entry in catalog.get("entries", []):
        if entry.get("ecosystem") != "koishi":
            continue
        metadata = entry.get("source_record", {}).get("manifest", {}).get("service", {})
        implements = metadata.get("implements", []) if isinstance(metadata, dict) else []
        if not isinstance(implements, list):
            continue
        matching_services = sorted(set(service_names).intersection(item for item in implements if isinstance(item, str)))
        if not matching_services:
            continue

        entry_id = entry.get("registry_entry_id")
        artifact_row = artifacts_by_id.get(entry_id, {})
        profile_row = profiles_by_id.get(entry_id, {})
        windows_profile = (profile_row.get("profiles") or {}).get("windows-x86_64", {})
        artifact_locks = [
            item.get("artifact", {}) for item in artifact_row.get("artifacts", [])
            if item.get("status") == "locked"
            and item.get("artifact", {}).get("packagetype") == "npm-tarball"
        ]
        artifact = artifact_locks[0] if len(artifact_locks) == 1 else None
        artifact_path = cache_root / artifact.get("cache_path", "") if artifact else None
        artifact_sha = sha256_file(artifact_path) if artifact_path and artifact_path.is_file() else None
        artifact_hash_match = bool(
            artifact and artifact_sha
            and artifact_sha == artifact.get("computed_digests", {}).get("sha256")
        )
        lock = windows_profile.get("dependency_lock") or {}
        lock_path = cache_root / lock.get("cache_path", "") if lock.get("cache_path") else None
        lock_sha = sha256_file(lock_path) if lock_path and lock_path.is_file() else None
        lock_hash_match = bool(lock_sha and lock_sha == lock.get("sha256"))

        package_name = entry.get("package")
        version = entry.get("advertised_version")
        tar_name, tar_implements, tar_version, tar_error = (None, None, None, "artifact_not_verified")
        if artifact_hash_match and artifact_path:
            tar_name, tar_implements, tar_version, tar_error = package_service_implements(artifact_path)
        registry_implements = sorted(set(item for item in implements if isinstance(item, str)))
        tar_metadata_identity_match = tar_name == package_name and tar_version == version
        tar_implements_match = tar_implements is not None and sorted(set(tar_implements)) == registry_implements

        candidate = {
            "registry_entry_id": entry_id,
            "package": package_name,
            "version": version,
            "official_maintained": entry.get("official_maintained") is True,
            "catalog_service_implements": registry_implements,
            "windows_profile_status": windows_profile.get("status"),
            "windows_profile_id": windows_profile.get("profile_id"),
            "provider_tarball": {
                "locked": artifact is not None,
                "sha256": artifact_sha,
                "lock_sha256": (artifact or {}).get("computed_digests", {}).get("sha256"),
                "cache_hash_matches_lock": artifact_hash_match,
                "package_manifest_identity_matches": tar_metadata_identity_match,
                "package_manifest_implements_matches_catalog": tar_implements_match,
                "verification_error": tar_error,
            },
            "windows_dependency_lock": {
                "status": windows_profile.get("status"),
                "sha256": lock_sha,
                "lock_sha256": lock.get("sha256"),
                "cache_blob_hash_matches_lock": lock_hash_match,
                "dependency_archives_reverified": False,
            },
            "combined_target_entry_lock_frozen": False,
            "configuration_frozen": False,
        }
        for service in matching_services:
            providers_by_service[service].append(candidate)

    service_rows = []
    no_catalog_match = []
    fully_verified_candidate_service_names = []
    candidate_count = 0
    fully_verified_candidate_count = 0
    for service in service_names:
        candidates = sorted(providers_by_service[service], key=lambda item: (not item["official_maintained"], item["package"], item["version"]))
        candidate_count += len(candidates)
        fully_verified = [
            item for item in candidates
            if item["windows_profile_status"] == "locked"
            and item["provider_tarball"]["cache_hash_matches_lock"] is True
            and item["provider_tarball"]["package_manifest_identity_matches"] is True
            and item["provider_tarball"]["package_manifest_implements_matches_catalog"] is True
            and item["windows_dependency_lock"]["cache_blob_hash_matches_lock"] is True
        ]
        fully_verified_candidate_count += len(fully_verified)
        if fully_verified:
            fully_verified_candidate_service_names.append(service)
        if not candidates:
            no_catalog_match.append(service)
        classification = "documented_core_api_name" if service in BUILTIN_DOC_NAMES else (
            "catalog_candidate_available" if candidates else "unresolved_in_frozen_catalog"
        )
        service_rows.append({
            "service_name": service,
            "classification": classification,
            "static_required_by_unit_count": len(required_by_service[service]),
            "static_required_by_units": sorted(required_by_service[service], key=lambda item: item["registry_entry_id"]),
            "catalog_implementer_candidates": len(candidates),
            "fully_verified_cached_candidates": len(fully_verified),
            "candidates": candidates,
        })

    report: dict[str, Any] = {
        "schema_version": 1,
        "kind": "phase3_p305_windows_koishi_provider_candidate_audit",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "scope": "offline cross-reference of statically discovered services against frozen Koishi catalog/provider metadata; no npm install, lifecycle script, catalog plugin, Worker, or Sandbox execution",
        "inputs": {
            "static_audit_path": static_path.relative_to(ROOT).as_posix(),
            "static_audit_canonical_sha256": static["report_sha256"],
            "static_audit_file_sha256": static_file_sha,
            "static_audit_raw_sha256": sha256_bytes(static_raw),
            "recovery_manifest_path": inputs["recovery_manifest_path"],
            "recovery_manifest_canonical_sha256": inputs["recovery_manifest_canonical_sha256"],
            "closed_pilot_manifest_path": inputs["closed_pilot_manifest_path"],
            "closed_pilot_manifest_canonical_sha256": inputs["closed_pilot_manifest_canonical_sha256"],
            "catalog_snapshot_id": catalog.get("snapshot_id"),
            "catalog_file_sha256": sha256_bytes(catalog_raw),
            "artifact_lock_file_sha256": sha256_bytes(artifacts_raw),
            "profiles_lock_file_sha256": sha256_bytes(profiles_raw),
            "cache_root": str(cache_root),
        },
        "official_document_references": DOCS,
        "summary": {
            "remaining_units": 167,
            "static_required_service_names": len(service_names),
            "service_names_with_catalog_implementer_candidates": len(service_names) - len(no_catalog_match),
            "service_names_with_fully_verified_cached_candidates": len(fully_verified_candidate_service_names),
            "service_names_without_catalog_implementer_metadata": no_catalog_match,
            "documented_core_api_names": sorted(BUILTIN_DOC_NAMES.intersection(service_names)),
            "provider_candidate_records": candidate_count,
            "fully_verified_cached_candidate_records": fully_verified_candidate_count,
            "combined_target_entry_locks_frozen": 0,
            "provider_configurations_frozen": 0,
            "profile_ready": False,
            "readiness_blockers": [
                "Static inject discovery is not complete runtime service attribution; the remaining 167 units have not run under this Host profile.",
                "Several service names have no implementer metadata in the frozen catalog; this does not prove no provider exists outside the frozen snapshot.",
                "Candidate provider locks are separate profiles; no target-entry combined lock includes the selected plugin and provider set.",
                "Provider choice/configuration and required native assets have not been selected and frozen.",
                "The existing cache audit still records two unsupported Git SSH references, lifecycle-script occurrences, and the sqlite3 Windows binding issue.",
            ],
            "next_actions": [
                "Classify each no-match name as Koishi built-in API, package-local/custom service, or unresolved external provider using the exact frozen artifacts and runtime protocol evidence.",
                "Select only providers supported by exact archive identity and profile constraints; define whether service providers are minimal, universal, or entry-specific.",
                "Resolve Git source and lifecycle/native binding policy before constructing target-entry combined locks.",
                "Freeze a new independent Host profile, combined locks, manifest, checkpoint, and report; validate-only before any Sandbox dispatch.",
            ],
        },
        "service_inventory": service_rows,
        "interpretation": "A verified candidate establishes only that an exact provider package and its own lock blob are present in the frozen cache. It does not establish that the provider set is sufficient, that provider dependencies are all available, that target-entry combined locks resolve, or that any plugin loads or behaves correctly.",
    }
    report["report_sha256"] = canonical_sha256(report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--static-audit", type=Path, default=ROOT / "outputs/acceptance/phase3-p305-windows-koishi-host-profile-static-audit-v3-20261026.json")
    parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    parser.add_argument("--output", type=Path, default=ROOT / "outputs/acceptance/phase3-p305-windows-koishi-provider-candidate-audit-v1-20261026.json")
    options = parser.parse_args()
    try:
        report = run(options)
        output = options.output.resolve()
        sidecar = output.with_suffix(output.suffix + ".sha256")
        if output.exists() or sidecar.exists():
            raise AuditError(f"refusing to overwrite existing evidence: {output}")
        output.parent.mkdir(parents=True, exist_ok=True)
        raw = (json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        output.write_bytes(raw)
        file_sha = sha256_bytes(raw)
        sidecar.write_text(f"{file_sha}  {output.name}\n", encoding="utf-8")
        print(json.dumps({
            "report": str(output),
            "report_sha256": report["report_sha256"],
            "file_sha256": file_sha,
            "summary": report["summary"],
        }, ensure_ascii=False, sort_keys=True))
    except (AuditError, OSError, json.JSONDecodeError) as error:
        parser.exit(2, f"audit failed: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
