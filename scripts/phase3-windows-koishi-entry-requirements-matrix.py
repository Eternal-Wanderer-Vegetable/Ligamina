"""Build an offline, per-entry Koishi Windows requirements matrix.

The matrix joins frozen static source observations, provider candidates, source
crosswalks, and existing npm lock blobs. It does not choose providers, create
combined locks, install or execute packages, run lifecycle scripts, or start a
Worker or Windows Sandbox. Candidate mappings are evidence for profile work,
not runtime readiness or compatibility.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
ACCEPTANCE = ROOT / "outputs/acceptance"
STATIC_PATH = ACCEPTANCE / "phase3-p305-windows-koishi-host-profile-static-audit-v3-20261026.json"
PROVIDER_PATH = ACCEPTANCE / "phase3-p305-windows-koishi-provider-candidate-audit-v1-20261026.json"
SOURCE_PATH = ACCEPTANCE / "phase3-p305-windows-koishi-service-source-audit-v1-20261026.json"
CACHE_AUDIT_PATH = ACCEPTANCE / "phase3-p305-windows-koishi-cache-audit-20261025.json"
OUTPUT_PATH = ACCEPTANCE / "phase3-p305-windows-koishi-entry-requirements-matrix-v1-20261026.json"

EXPECTED_REPORTS = {
    "static": "793ceb01e02dc0311123fd6d32c7b0db67f9dea650b03774f77530f3b4cc4508",
    "provider": "7f8e2efcbfa8ca7daa125327f38af21d5b105b5259a09a0dae361f0102703204",
    "source": "08e59310407435f3e50dd989eb716197a09faa6c31d8a97f89ec9283265bd669",
}
EXPECTED_TARGETS = 167
EXPECTED_LOCK_SCRIPT_OCCURRENCES = 113


class MatrixError(RuntimeError):
    """Raised when any frozen input or join invariant fails."""


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256_bytes(raw)


def read_json(path: Path) -> tuple[Any, bytes]:
    raw = path.read_bytes()
    try:
        return json.loads(raw.decode("utf-8")), raw
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MatrixError(f"invalid UTF-8 JSON: {path}: {exc}") from exc


def verify_sidecar(path: Path) -> str:
    actual = sha256_file(path)
    sidecar = path.with_suffix(path.suffix + ".sha256")
    if sidecar.is_file():
        expected = sidecar.read_text(encoding="utf-8").strip().split()[0]
        if expected != actual:
            raise MatrixError(f"sidecar SHA mismatch for {path}: expected={expected}, actual={actual}")
    return actual


def verify_report(path: Path, expected_canonical: str | None = None) -> tuple[dict[str, Any], dict[str, str]]:
    value, _ = read_json(path)
    if not isinstance(value, dict):
        raise MatrixError(f"expected report object: {path}")
    without_hash = {key: item for key, item in value.items() if key != "report_sha256"}
    actual_canonical = canonical_sha256(without_hash)
    if value.get("report_sha256") != actual_canonical:
        raise MatrixError(f"canonical report SHA mismatch for {path}: {actual_canonical}")
    if expected_canonical and actual_canonical != expected_canonical:
        raise MatrixError(f"frozen report identity changed for {path}: {actual_canonical}")
    return value, {
        "path": path.relative_to(ROOT).as_posix(),
        "canonical_sha256": actual_canonical,
        "file_sha256": verify_sidecar(path),
    }


def verify_manifest(path: Path, expected_sha: str, field: str) -> dict[str, Any]:
    value, _ = read_json(path)
    if not isinstance(value, dict):
        raise MatrixError(f"expected manifest object: {path}")
    actual = canonical_sha256({key: item for key, item in value.items() if key != field})
    if value.get(field) != actual or actual != expected_sha:
        raise MatrixError(f"frozen manifest identity mismatch for {path}: expected={expected_sha}, actual={actual}")
    verify_sidecar(path)
    return value


def load_hash_file(path: Path, expected_sha: str) -> bytes:
    raw = path.read_bytes()
    actual = sha256_bytes(raw)
    if actual != expected_sha:
        raise MatrixError(f"frozen lock file changed: {path}: expected={expected_sha}, actual={actual}")
    verify_sidecar(path)
    return raw


def package_name_from_lock_path(path: str) -> str:
    marker = "node_modules/"
    offset = path.rfind(marker)
    if offset < 0:
        return path
    return path[offset + len(marker):]


def candidate_row(service: dict[str, Any], source_archive_by_id: dict[str, dict[str, Any]], target_ids: set[str]) -> dict[str, Any]:
    entry_id = service["registry_entry_id"]
    source_archive = source_archive_by_id.get(entry_id, {})
    tarball = service.get("provider_tarball") or {}
    lock = service.get("windows_dependency_lock") or {}
    return {
        "origin": "catalog_service_implements_metadata",
        "package": service["package"],
        "version": service["version"],
        "registry_entry_id": entry_id,
        "is_target_unit": entry_id in target_ids,
        "declared_services": service.get("catalog_service_implements", []),
        "provider_tarball_locked_and_hash_matched": bool(tarball.get("locked") and tarball.get("cache_hash_matches_lock")),
        "provider_package_manifest_identity_matched": bool(tarball.get("package_manifest_identity_matches")),
        "provider_package_manifest_implements_matched": bool(tarball.get("package_manifest_implements_matches_catalog")),
        "provider_artifact_sha256": tarball.get("sha256"),
        "provider_windows_lock_blob_hash_matched": bool(lock.get("cache_blob_hash_matches_lock")),
        "provider_windows_lock_sha256": lock.get("sha256"),
        "provider_transitive_archives_reverified": bool(lock.get("dependency_archives_reverified")),
        "provider_profile_status": service.get("windows_profile_status"),
        "provider_configuration_frozen": bool(service.get("configuration_frozen")),
        "combined_target_entry_lock_frozen": bool(service.get("combined_target_entry_lock_frozen")),
        "official_maintained": bool(service.get("official_maintained")),
        "source_audit_artifact_sha256": source_archive.get("artifact_sha256"),
        "source_audit_artifact_hash_matched": source_archive.get("artifact_sha256_matches_lock"),
        "source_audit_windows_lock_sha256": source_archive.get("dependency_lock_sha256"),
        "source_audit_windows_lock_blob_hash_matched": source_archive.get("dependency_lock_blob_sha256_matches_lock"),
        "source_audit_transitive_archives_reverified": source_archive.get("dependency_archives_reverified"),
    }


def build_report(generated_at_utc: str | None = None) -> dict[str, Any]:
    static, static_identity = verify_report(STATIC_PATH, EXPECTED_REPORTS["static"])
    provider, provider_identity = verify_report(PROVIDER_PATH, EXPECTED_REPORTS["provider"])
    source, source_identity = verify_report(SOURCE_PATH, EXPECTED_REPORTS["source"])
    cache, _ = read_json(CACHE_AUDIT_PATH)
    cache_file_sha = verify_sidecar(CACHE_AUDIT_PATH)
    if cache.get("kind") != "phase3_p305_windows_koishi_cache_audit":
        raise MatrixError("unexpected cache audit kind")

    static_inputs = static.get("inputs", {})
    provider_inputs = provider.get("inputs", {})
    source_inputs = source.get("inputs", {})
    cache_inputs = cache.get("inputs", {})
    if provider_inputs.get("static_audit_canonical_sha256") != static_identity["canonical_sha256"]:
        raise MatrixError("provider audit is bound to a different static audit")
    if source_inputs.get("provider_candidate_audit_canonical_sha256") != provider_identity["canonical_sha256"]:
        raise MatrixError("source audit is bound to a different provider candidate audit")
    if source_inputs.get("static_audit_canonical_sha256") != static_identity["canonical_sha256"]:
        raise MatrixError("source audit is bound to a different static audit")
    if cache_inputs.get("recovery_manifest_sha256") != static_inputs.get("recovery_manifest_canonical_sha256"):
        raise MatrixError("cache audit and static audit recovery identities differ")

    recovery_path = ROOT / static_inputs["recovery_manifest_path"]
    recovery = verify_manifest(recovery_path, static_inputs["recovery_manifest_canonical_sha256"], "manifest_sha256")
    pilot_path = ROOT / static_inputs["closed_pilot_manifest_path"]
    verify_manifest(pilot_path, static_inputs["closed_pilot_manifest_canonical_sha256"], "manifest_sha256")

    artifact_lock_path = ROOT / static_inputs["artifact_lock_path"]
    profile_lock_path = ROOT / static_inputs["profiles_lock_path"]
    artifact_raw = load_hash_file(artifact_lock_path, static_inputs["artifact_lock_file_sha256"])
    profile_raw = load_hash_file(profile_lock_path, static_inputs["profiles_lock_file_sha256"])
    artifacts = json.loads(artifact_raw.decode("utf-8"))
    profiles = json.loads(profile_raw.decode("utf-8"))
    catalog_path = ROOT / "catalogs/p2-41649744e057d61031d5/catalog.json"
    catalog, catalog_raw = read_json(catalog_path)
    if catalog.get("snapshot_id") != provider_inputs.get("catalog_snapshot_id"):
        raise MatrixError("catalog snapshot differs from frozen provider audit")
    if artifacts.get("snapshot_id") != catalog.get("snapshot_id") or profiles.get("snapshot_id") != catalog.get("snapshot_id"):
        raise MatrixError("catalog/artifact/profile snapshots differ")
    if profiles.get("artifacts_lock_sha256") != sha256_bytes(artifact_raw):
        raise MatrixError("profile lock is not bound to current artifact lock")

    selected = static.get("selection", [])
    target_ids = {row.get("registry_entry_id") for row in selected}
    if len(selected) != EXPECTED_TARGETS or len(target_ids) != EXPECTED_TARGETS:
        raise MatrixError(f"expected {EXPECTED_TARGETS} unique target entries; got rows={len(selected)} unique={len(target_ids)}")

    catalog_by_id = {row.get("registry_entry_id"): row for row in catalog.get("entries", [])}
    artifacts_by_id = {row.get("registry_entry_id"): row for row in artifacts.get("entries", [])}
    profiles_by_id = {row.get("registry_entry_id"): row for row in profiles.get("entries", [])}
    candidate_services = {row["service_name"]: row for row in provider.get("service_inventory", [])}
    source_services = {row["service_name"]: row for row in source.get("service_crosswalk", [])}
    source_archives = {row["registry_entry_id"]: row for row in source.get("provider_archives", [])}
    scanner_caveats: dict[tuple[str, str], dict[str, Any]] = {}
    for caveat in source.get("static_scanner_caveats", []):
        for assignment in caveat.get("static_audit_assignments", []):
            if assignment.get("not_runtime_inject_evidence") is True:
                marker = (caveat["registry_entry_id"], "hajimi")
                scanner_caveats.setdefault(marker, {"reason": caveat["interpretation"], "assignments": []})["assignments"].append({
                    "member": assignment.get("member"),
                    "member_sha256": assignment.get("member_sha256"),
                    "literal_sha256": assignment.get("literal_sha256"),
                    "inside_usage_template_literal": assignment.get("inside_usage_template_literal"),
                })
    target_lock_root = Path(cache_inputs["cache_root"]) / "profile-locks/koishi/sha256"
    if not target_lock_root.is_dir():
        raise MatrixError(f"Koishi profile lock cache missing: {target_lock_root}")

    rows: list[dict[str, Any]] = []
    lock_script_occurrences = 0
    lock_script_units = 0
    git_ssh_refs: list[dict[str, Any]] = []
    sqlite3_units: list[dict[str, Any]] = []
    script_names = Counter()
    service_resolution_counts = Counter()
    required_demand_count = 0
    known_scanner_false_positive_count = 0
    units_with_required = 0
    source_candidate_services = set()
    catalog_candidate_services = set()

    for selected_row in sorted(selected, key=lambda row: row["registry_entry_id"]):
        entry_id = selected_row["registry_entry_id"]
        package = selected_row["package"]
        version = selected_row["version"]
        catalog_row = catalog_by_id.get(entry_id)
        artifact_row = artifacts_by_id.get(entry_id)
        profile_row = profiles_by_id.get(entry_id)
        if not catalog_row or not artifact_row or not profile_row:
            raise MatrixError(f"catalog/artifact/profile row missing for target {entry_id}")
        win_profile = (profile_row.get("profiles") or {}).get("windows-x86_64", {})
        if win_profile.get("status") != "locked":
            raise MatrixError(f"target profile is no longer locked for {entry_id}: {win_profile.get('status')}")
        lock_ref = win_profile.get("dependency_lock") or {}
        lock_sha = selected_row["dependency_lock_sha256"]
        if lock_ref.get("sha256") != lock_sha:
            raise MatrixError(f"target lock identity differs between static audit and profile lock for {entry_id}")
        lock_path = target_lock_root / lock_sha[:2] / lock_sha
        lock_raw = lock_path.read_bytes()
        if sha256_bytes(lock_raw) != lock_sha:
            raise MatrixError(f"target dependency lock blob SHA mismatch for {entry_id}")
        lock_doc = json.loads(lock_raw.decode("utf-8"))
        lock_packages = lock_doc.get("packages", {})
        script_rows = [
            {
                "path": path,
                "name": package_name_from_lock_path(path),
                "version": detail.get("version"),
                "resolved": detail.get("resolved"),
                "optional": detail.get("optional") is True,
                "os_constraints": detail.get("os"),
                "cpu_constraints": detail.get("cpu"),
            }
            for path, detail in lock_packages.items()
            if detail.get("hasInstallScript") is True
        ]
        lock_script_occurrences += len(script_rows)
        lock_script_units += bool(script_rows)
        for script in script_rows:
            script_names[script["name"]] += 1
        ssh_rows = [
            {"path": path, "name": path.rsplit("/", 1)[-1], "version": detail.get("version"), "resolved": detail.get("resolved")}
            for path, detail in lock_packages.items()
            if isinstance(detail.get("resolved"), str) and detail["resolved"].startswith("git+ssh://")
        ]
        for item in ssh_rows:
            git_ssh_refs.append({"registry_entry_id": entry_id, "package": package, **item})
        sqlite_rows = [
            {"path": path, "version": detail.get("version"), "has_install_script": detail.get("hasInstallScript") is True, "resolved": detail.get("resolved")}
            for path, detail in lock_packages.items()
            if path.rsplit("/", 1)[-1] == "sqlite3"
        ]
        if sqlite_rows:
            sqlite3_units.append({"registry_entry_id": entry_id, "package": package, "version": version, "dependencies": sqlite_rows})

        required_services = sorted(set(selected_row.get("static_required_services", [])))
        optional_services = sorted(set(selected_row.get("static_optional_services", [])))
        if required_services:
            units_with_required += 1
        required_demand_count += len(required_services)
        service_requirements = []
        blockers: set[str] = {"combined_target_host_dependency_lock_not_frozen"}
        if required_services:
            blockers.add("required_host_provider_configuration_not_frozen")
        if script_rows:
            blockers.add("lock_contains_hasInstallScript_dependencies_while_scripts_disabled")
        if ssh_rows:
            blockers.add("git_ssh_dependency_source_outside_worker_allowlist")
        if sqlite_rows:
            blockers.add("sqlite3_windows_binding_requires_separately_frozen_artifact_or_install_route")

        for name in required_services:
            demand = candidate_services.get(name)
            crosswalk = source_services.get(name)
            classification = demand.get("classification") if demand else None
            source_caveat = scanner_caveats.get((entry_id, name))
            options: list[dict[str, Any]] = []
            if demand:
                for option in demand.get("candidates", []):
                    options.append(candidate_row(option, source_archives, target_ids))
                    catalog_candidate_services.add(name)
            source_evidence = (crosswalk or {}).get("source_evidence") or {}
            for archive in source_archives.values():
                # Service-scoped membership is derived from the immutable archive row;
                # the source crosswalk deliberately stores evidence rather than a selected provider list.
                service_details = [item for item in archive.get("services", []) if item.get("service_name") == name]
                if not service_details:
                    continue
                options.append({
                    "origin": "hash_locked_source_archive_candidate",
                    "package": archive["package"],
                    "version": archive["version"],
                    "registry_entry_id": archive["registry_entry_id"],
                    "is_target_unit": archive["registry_entry_id"] in target_ids,
                    "service_source_evidence": service_details[0],
                    "provider_artifact_sha256": archive.get("artifact_sha256"),
                    "provider_artifact_hash_matched": archive.get("artifact_sha256_matches_lock"),
                    "provider_windows_lock_sha256": archive.get("dependency_lock_sha256"),
                    "provider_windows_lock_blob_hash_matched": archive.get("dependency_lock_blob_sha256_matches_lock"),
                    "provider_transitive_archives_reverified": archive.get("dependency_archives_reverified"),
                    "provider_configuration_frozen": service_details[0].get("profile_configuration_frozen_for_remaining_units"),
                    "combined_target_entry_lock_frozen": False,
                    "same_package_self_service_candidate": service_details[0].get("same_package_self_service_candidate", False),
                })
                source_candidate_services.add(name)
            builtin = classification == "documented_core_api_name" or (crosswalk or {}).get("source_audit_classification") == "documented_core_builtin_service"
            if source_caveat:
                resolution = "scanner_false_positive_same_package_service_candidate"
            elif builtin:
                resolution = "documented_core_builtin"
            elif options:
                resolution = "candidate_evidence_only"
            else:
                resolution = "unresolved"
            service_resolution_counts[resolution] += 1
            known_scanner_false_positive_count += bool(source_caveat)
            service_requirements.append({
                "service_name": name,
                "provider_metadata_classification": classification,
                "source_audit_classification": (crosswalk or {}).get("source_audit_classification"),
                "resolution": resolution,
                "static_scanner_caveat": source_caveat,
                "candidate_count": len(options),
                "candidates": sorted(options, key=lambda option: (option.get("package", ""), option.get("registry_entry_id", ""))),
                "provider_selection_frozen": False,
                "external_provider_required": not builtin and not source_caveat,
                "provider_configuration_frozen": False,
            })
        if any(name in {"skia", "QhzySharp", "fonts"} for name in required_services):
            blockers.add("native_provider_closure_not_verified_for_windows")
        rows.append({
            "registry_entry_id": entry_id,
            "package": package,
            "version": version,
            "target_artifact_sha256": selected_row["artifact_sha256"],
            "target_dependency_lock_sha256": lock_sha,
            "target_lock_blob_sha256_verified": True,
            "static_required_services": required_services,
            "static_optional_services": optional_services,
            "required_service_count": len(required_services),
            "service_requirements": service_requirements,
            "lock_observations": {
                "package_records": len(lock_packages),
                "has_install_script_dependency_count": len(script_rows),
                "has_install_script_dependencies": script_rows,
                "unsupported_git_ssh_dependency_count": len(ssh_rows),
                "unsupported_git_ssh_dependencies": ssh_rows,
                "sqlite3_dependency_count": len(sqlite_rows),
                "sqlite3_dependencies": sqlite_rows,
            },
            "combined_host_profile_lock_frozen": False,
            "configuration_frozen": False,
            "dispatch_eligible": False,
            "blockers": sorted(blockers),
        })

    if lock_script_occurrences != EXPECTED_LOCK_SCRIPT_OCCURRENCES:
        raise MatrixError(f"target lock script occurrence count changed: expected={EXPECTED_LOCK_SCRIPT_OCCURRENCES}, actual={lock_script_occurrences}")
    cache_count = cache.get("cache_audit", {}).get("lifecycle_script_package_occurrences")
    if cache_count != lock_script_occurrences:
        raise MatrixError(f"lock-derived lifecycle-script count differs from cache audit: lock={lock_script_occurrences}, cache={cache_count}")
    unsupported_cache = cache.get("cache_audit", {}).get("unsupported_dependency_refs", [])
    if {(row.get("registry_entry_id"), row.get("resolved")) for row in unsupported_cache} != {(row.get("registry_entry_id"), row.get("resolved")) for row in git_ssh_refs}:
        raise MatrixError("lock-derived Git SSH references differ from cache audit")
    if len(sqlite3_units) != 1 or sqlite3_units[0]["registry_entry_id"] != "ko-c2cd287f0be05c4c80bed280":
        raise MatrixError("expected the one frozen sqlite3 target entry to remain unchanged")
    if {row["registry_entry_id"] for row in rows} != target_ids:
        raise MatrixError("matrix target identities do not equal frozen static selection")

    inputs = {
        "static_audit": static_identity,
        "provider_candidate_audit": provider_identity,
        "service_source_audit": source_identity,
        "cache_audit": {
            "path": CACHE_AUDIT_PATH.relative_to(ROOT).as_posix(),
            "file_sha256": cache_file_sha,
            "frozen_cache_root": cache_inputs.get("cache_root"),
        },
        "catalog_snapshot_id": catalog.get("snapshot_id"),
        "catalog_file_sha256": sha256_bytes(catalog_raw),
        "artifact_lock_file_sha256": sha256_bytes(artifact_raw),
        "profiles_lock_file_sha256": sha256_bytes(profile_raw),
        "recovery_manifest_path": static_inputs["recovery_manifest_path"],
        "recovery_manifest_canonical_sha256": static_inputs["recovery_manifest_canonical_sha256"],
        "recovery_manifest_file_sha256": sha256_file(recovery_path),
        "closed_pilot_manifest_path": static_inputs["closed_pilot_manifest_path"],
        "closed_pilot_manifest_canonical_sha256": static_inputs["closed_pilot_manifest_canonical_sha256"],
        "closed_pilot_manifest_file_sha256": sha256_file(pilot_path),
    }
    summary = {
        "target_units": len(rows),
        "target_units_with_static_required_services": units_with_required,
        "target_units_without_static_required_services": len(rows) - units_with_required,
        "static_required_service_demands": required_demand_count,
        "known_false_positive_static_service_demands": known_scanner_false_positive_count,
        "static_service_demands_after_known_false_positive_exclusion": required_demand_count - known_scanner_false_positive_count,
        "unique_static_required_services": len(candidate_services),
        "service_demand_resolution_counts": dict(sorted(service_resolution_counts.items())),
        "service_names_with_catalog_candidate_records": len(catalog_candidate_services),
        "catalog_candidate_records": sum(len(row.get("candidates", [])) for row in provider.get("service_inventory", [])),
        "fully_verified_cached_catalog_candidate_records": sum(row.get("fully_verified_cached_candidates", 0) for row in provider.get("service_inventory", [])),
        "service_names_with_fully_verified_cached_catalog_candidates": sum(bool(row.get("fully_verified_cached_candidates", 0)) for row in provider.get("service_inventory", [])),
        "service_names_with_source_archive_candidate_records": len(source_candidate_services),
        "target_entries_with_has_install_script_dependencies": lock_script_units,
        "has_install_script_dependency_occurrences": lock_script_occurrences,
        "has_install_script_dependency_names": dict(sorted(script_names.items())),
        "unsupported_git_ssh_dependency_references": len(git_ssh_refs),
        "target_entries_with_unsupported_git_ssh": len({row["registry_entry_id"] for row in git_ssh_refs}),
        "sqlite3_target_entries": len(sqlite3_units),
        "combined_host_profile_locks_frozen": 0,
        "provider_configurations_frozen": 0,
        "dispatch_eligible_units": 0,
        "profile_ready": False,
        "interpretation": "All service mappings are either documented built-in evidence or static/catalog candidates. No provider was selected, no provider configuration or combined target lock was frozen, and no runtime behavior is evaluated.",
    }
    without_hash: dict[str, Any] = {
        "schema_version": 1,
        "kind": "phase3_p305_windows_koishi_entry_requirements_matrix",
        "generated_at_utc": generated_at_utc or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "scope": "offline per-entry join of frozen source requirements, provider candidates, source crosswalks, and existing Windows npm locks; no install, lifecycle script, plugin, Worker, or Sandbox execution",
        "inputs": inputs,
        "summary": summary,
        "known_blockers": [
            "provider candidates and built-ins are not selected/frozen into a Host profile for target entries",
            "provider dependency-lock blobs are hash-bound, but their transitive archives were not reverified by the provider audits",
            "two Git SSH dependency references remain outside the Worker source allowlist",
            "113 lock dependency occurrences are marked hasInstallScript while the Worker policy disables lifecycle scripts",
            "the sleep-shiyi target's sqlite3 Windows binding has no separately frozen/approved install artifact or route",
            "the skia service candidate native/resource closure remains unverified for Windows",
        ],
        "units": rows,
        "limitations": [
            "Static inject/service discovery is heuristic and cannot establish dynamic or runtime-dependent service requirements.",
            "Candidate identity, package metadata, and lock hashes are not evidence that a provider loads or behaves correctly.",
            "This matrix does not authorize changing the frozen recovery manifest, catalog locks, App Control policy, or install-script policy.",
            "Every unit remains ineligible for dispatch until a complete independent Windows Host/native profile, provider configs, combined per-entry locks, manifest, checkpoint, and report are frozen and validate-only succeeds.",
        ],
    }
    return {**without_hash, "report_sha256": canonical_sha256(without_hash)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--check", action="store_true", help="verify an existing report against current frozen inputs without rewriting it")
    options = parser.parse_args()
    if options.check:
        existing, _ = read_json(options.output)
        report = build_report(existing.get("generated_at_utc") if isinstance(existing, dict) else None)
        expected = canonical_sha256({key: value for key, value in report.items() if key != "report_sha256"})
        actual = existing.get("report_sha256") if isinstance(existing, dict) else None
        if actual != expected:
            raise MatrixError(f"existing report does not match current matrix: expected={expected}, actual={actual}")
        file_sha = verify_sidecar(options.output)
        print(json.dumps({"status": "verified", "canonical_sha256": expected, "file_sha256": file_sha, "summary": report["summary"]}, ensure_ascii=False, indent=2))
        return 0
    report = build_report()
    output = options.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    output.write_bytes(raw)
    file_sha = sha256_bytes(raw)
    sidecar = output.with_suffix(output.suffix + ".sha256")
    sidecar.write_text(f"{file_sha}  {output.name}\n", encoding="ascii", newline="\n")
    print(json.dumps({"status": "written", "path": str(output), "canonical_sha256": report["report_sha256"], "file_sha256": file_sha, "summary": report["summary"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, KeyError, TypeError, ValueError, MatrixError) as exc:
        print(f"entry requirements matrix failed: {exc}")
        raise SystemExit(2)
