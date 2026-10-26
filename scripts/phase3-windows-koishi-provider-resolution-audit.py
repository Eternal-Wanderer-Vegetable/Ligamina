"""Join Koishi service requirements to providers with closed frozen archives.

This is offline identity and coverage analysis. It does not choose providers,
freeze configuration, build combined locks, install packages, extract archives,
run lifecycle scripts, execute plugins, start Workers, or launch Windows Sandbox.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ACCEPTANCE = ROOT / "outputs/acceptance"
MATRIX_PATH = ACCEPTANCE / "phase3-p305-windows-koishi-entry-requirements-matrix-v1-20261026.json"
CANDIDATE_PATH = ACCEPTANCE / "phase3-p305-windows-koishi-provider-candidate-audit-v1-20261026.json"
SOURCE_PATH = ACCEPTANCE / "phase3-p305-windows-koishi-service-source-audit-v1-20261026.json"
CLOSURE_PATH = ACCEPTANCE / "phase3-p305-windows-koishi-provider-closure-audit-v1-20261026.json"
SNAPSHOT = ROOT / "catalogs/p2-41649744e057d61031d5"
PROFILE_LOCK_PATH = SNAPSHOT / "profiles.lock.json"
OUTPUT_PATH = ACCEPTANCE / "phase3-p305-windows-koishi-provider-resolution-audit-v1-20261026.json"


class AuditError(RuntimeError):
    pass


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def canonical_sha256(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return sha256(raw)


def read_json(path: Path) -> tuple[dict[str, Any], bytes]:
    raw = path.read_bytes()
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise AuditError(f"expected JSON object: {path}")
    return value, raw


def load_report(path: Path) -> tuple[dict[str, Any], str, str]:
    report, raw = read_json(path)
    canonical = canonical_sha256({key: value for key, value in report.items() if key != "report_sha256"})
    if report.get("report_sha256") != canonical:
        raise AuditError(f"canonical report hash mismatch: {path}")
    sidecar = path.with_suffix(path.suffix + ".sha256")
    if not sidecar.is_file():
        raise AuditError(f"missing report sidecar: {sidecar}")
    file_sha = sha256(raw)
    if sidecar.read_text(encoding="utf-8").strip().split()[0] != file_sha:
        raise AuditError(f"report sidecar mismatch: {path}")
    return report, canonical, file_sha


def locked_provider_ids(matrix: dict[str, Any], source: dict[str, Any]) -> dict[str, dict[str, Any]]:
    providers: dict[str, dict[str, Any]] = {}

    def add(registry_id: str, package: str, version: str | None, service: str, origin: str) -> None:
        provider = providers.setdefault(registry_id, {
            "registry_entry_id": registry_id,
            "package": package,
            "version": version,
            "services": set(),
            "origins": set(),
        })
        if provider["package"] != package or (provider["version"] and version and provider["version"] != version):
            raise AuditError(f"provider identity conflicts across inputs: {registry_id}")
        if provider["version"] is None:
            provider["version"] = version
        provider["services"].add(service)
        provider["origins"].add(origin)

    for unit in matrix.get("units", []):
        for requirement in unit.get("service_requirements", []):
            for candidate in requirement.get("candidates", []):
                if candidate.get("provider_windows_lock_blob_hash_matched") is True:
                    add(candidate["registry_entry_id"], candidate["package"], candidate.get("version"),
                        requirement["service_name"], candidate.get("origin", "catalog_candidate"))
    for archive in source.get("provider_archives", []):
        for service in archive.get("services", []):
            add(archive["registry_entry_id"], archive["package"], archive.get("version"),
                service["service_name"], "hash_locked_source_archive_candidate")
    return providers


def audit(generated_at_utc: str) -> dict[str, Any]:
    matrix, matrix_canonical, matrix_file = load_report(MATRIX_PATH)
    candidates, candidate_canonical, candidate_file = load_report(CANDIDATE_PATH)
    source, source_canonical, source_file = load_report(SOURCE_PATH)
    closure, closure_canonical, closure_file = load_report(CLOSURE_PATH)
    profile_lock, profile_raw = read_json(PROFILE_LOCK_PATH)
    profile_sha = sha256(profile_raw)
    for label, report in (("matrix", matrix), ("candidate audit", candidates), ("source audit", source)):
        if report.get("inputs", {}).get("profiles_lock_file_sha256") != profile_sha:
            raise AuditError(f"{label} references another catalog profile lock")
    if closure.get("inputs", {}).get("profiles_lock_file_sha256") != profile_sha:
        raise AuditError("provider closure references another catalog profile lock")

    profile_by_id = {entry["registry_entry_id"]: entry for entry in profile_lock.get("entries", [])}
    closure_by_id = {entry["registry_entry_id"]: entry for entry in closure.get("provider_lock_blobs", [])}
    providers = locked_provider_ids(matrix, source)
    if set(providers) != set(closure_by_id):
        raise AuditError("provider closure identities differ from the hash-verified candidate/source set")
    source_by_service: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for archive in source.get("provider_archives", []):
        for service in archive.get("services", []):
            source_by_service[service["service_name"]].append({
                "registry_entry_id": archive["registry_entry_id"],
                "package": archive["package"],
                "version": archive.get("version"),
                "origin": "hash_locked_source_archive_candidate",
                "artifact_sha256": archive.get("artifact_sha256"),
                "dependency_lock_sha256": archive.get("dependency_lock_sha256"),
                "artifact_identity_verified": archive.get("artifact_sha256_matches_lock") is True,
                "profile_lock_blob_verified": archive.get("dependency_lock_blob_sha256_matches_lock") is True,
            })

    rows: list[dict[str, Any]] = []
    service_counts: Counter[str] = Counter()
    disposition_counts: Counter[str] = Counter()
    provider_demand_rows = 0
    provider_closed_rows = 0
    for unit in matrix.get("units", []):
        for requirement in unit.get("service_requirements", []):
            service = requirement["service_name"]
            disposition = requirement.get("resolution")
            candidate_rows: dict[str, dict[str, Any]] = {}
            for candidate in requirement.get("candidates", []):
                registry_id = candidate.get("registry_entry_id")
                if not registry_id:
                    continue
                entry = profile_by_id.get(registry_id, {})
                profile = entry.get("profiles", {}).get("windows-x86_64", {})
                closure_record = closure_by_id.get(registry_id)
                fully_verified = (
                    candidate.get("provider_tarball_locked_and_hash_matched") is True
                    and candidate.get("provider_package_manifest_identity_matched") is True
                    and candidate.get("provider_windows_lock_blob_hash_matched") is True
                    and closure_record is not None
                )
                candidate_rows[registry_id] = {
                    "registry_entry_id": registry_id,
                    "package": candidate.get("package"),
                    "version": candidate.get("version"),
                    "origin": candidate.get("origin"),
                    "artifact_sha256": candidate.get("provider_artifact_sha256"),
                    "dependency_lock_sha256": candidate.get("provider_windows_lock_sha256"),
                    "artifact_identity_verified": candidate.get("provider_tarball_locked_and_hash_matched") is True
                        and candidate.get("provider_package_manifest_identity_matched") is True,
                    "profile_lock_blob_verified": candidate.get("provider_windows_lock_blob_hash_matched") is True,
                    "transitive_archives_closed": closure_record is not None,
                    "profile_status": profile.get("status"),
                    "profile_reason_code": profile.get("reason_code"),
                    "candidate_eligible_for_profile_work": fully_verified,
                }
            for candidate in source_by_service.get(service, []):
                registry_id = candidate["registry_entry_id"]
                closure_record = closure_by_id.get(registry_id)
                row = candidate_rows.get(registry_id)
                source_candidate_eligible = (
                    closure_record is not None
                    and candidate.get("artifact_identity_verified") is True
                    and candidate.get("profile_lock_blob_verified") is True
                )
                if row is None:
                    candidate_rows[registry_id] = {
                        **candidate,
                        "transitive_archives_closed": closure_record is not None,
                        "profile_status": profile_by_id.get(registry_id, {}).get("profiles", {}).get("windows-x86_64", {}).get("status"),
                        "profile_reason_code": profile_by_id.get(registry_id, {}).get("profiles", {}).get("windows-x86_64", {}).get("reason_code"),
                        "candidate_eligible_for_profile_work": source_candidate_eligible,
                    }
                else:
                    row["transitive_archives_closed"] = closure_record is not None
                    row["source_archive_identity_verified"] = source_candidate_eligible
                    row["candidate_eligible_for_profile_work"] = (
                        row.get("candidate_eligible_for_profile_work") is True or source_candidate_eligible
                    )

            eligible = sorted(
                (candidate for candidate in candidate_rows.values()
                 if candidate.get("candidate_eligible_for_profile_work") is True),
                key=lambda item: item["registry_entry_id"],
            )
            if disposition == "scanner_false_positive_same_package_service_candidate":
                final_disposition = "excluded_known_scanner_false_positive"
            elif disposition == "documented_core_builtin":
                final_disposition = "documented_core_builtin"
            elif eligible:
                final_disposition = "archive_closed_provider_candidate_available"
                provider_demand_rows += 1
                provider_closed_rows += 1
            else:
                final_disposition = "no_archive_closed_provider_candidate"
                if disposition == "candidate_evidence_only":
                    provider_demand_rows += 1
            service_counts[service] += 1
            disposition_counts[final_disposition] += 1
            rows.append({
                "target_registry_entry_id": unit["registry_entry_id"],
                "target_package": unit["package"],
                "service_name": service,
                "source_resolution": disposition,
                "final_disposition": final_disposition,
                "candidate_count": len(candidate_rows),
                "archive_closed_candidate_count": len(eligible),
                "provider_candidate_ids": [item["registry_entry_id"] for item in eligible],
                "candidates": sorted(candidate_rows.values(), key=lambda item: item["registry_entry_id"]),
            })

    unresolved = [row for row in rows if row["final_disposition"] == "no_archive_closed_provider_candidate"]
    units = matrix.get("units", [])
    units_with_unresolved = sorted({row["target_registry_entry_id"] for row in unresolved})
    summary = {
        "target_units": len(units),
        "static_service_demand_rows_including_known_false_positive": len(rows),
        "static_service_demand_rows_after_false_positive_exclusion": sum(
            row["final_disposition"] != "excluded_known_scanner_false_positive" for row in rows
        ),
        "documented_core_builtin_rows": disposition_counts["documented_core_builtin"],
        "known_false_positive_rows_excluded": disposition_counts["excluded_known_scanner_false_positive"],
        "external_provider_demand_rows": provider_demand_rows,
        "external_provider_demand_rows_with_archive_closed_candidate": provider_closed_rows,
        "external_provider_demand_rows_without_archive_closed_candidate": provider_demand_rows - provider_closed_rows,
        "service_names": len(service_counts),
        "service_names_with_unresolved_provider_candidate": sorted({row["service_name"] for row in unresolved}),
        "units_with_unresolved_provider_candidate": len(units_with_unresolved),
        "combined_target_host_locks_frozen": matrix.get("summary", {}).get("combined_host_profile_locks_frozen", 0),
        "provider_configurations_frozen": matrix.get("summary", {}).get("provider_configurations_frozen", 0),
        "dispatch_eligible_units": 0,
        "profile_ready": False,
        "compatibility": "not_evaluated",
    }
    blockers = [
        {"reason": "provider_selection_and_configuration_not_frozen", "target_units": len(units)},
        {"reason": "combined_target_host_locks_not_frozen", "target_units": len(units)},
    ]
    if unresolved:
        blockers.append({
            "reason": "no_archive_closed_provider_candidate_for_service",
            "service_names": sorted({row["service_name"] for row in unresolved}),
            "target_registry_entry_ids": units_with_unresolved,
            "details": [
                {
                    "target_registry_entry_id": row["target_registry_entry_id"],
                    "target_package": row["target_package"],
                    "service_name": row["service_name"],
                    "candidate_profile_reasons": sorted({
                        candidate.get("profile_reason_code")
                        for candidate in row["candidates"] if candidate.get("profile_reason_code")
                    }),
                }
                for row in unresolved
            ],
        })
    report_body = {
        "schema_version": 1,
        "kind": "phase3_p305_windows_koishi_provider_resolution_audit",
        "generated_at_utc": generated_at_utc,
        "scope": "offline per-entry service candidate coverage joined to exact Windows provider lock and transitive archive evidence; provider choice/configuration and combined locks are intentionally not inferred",
        "inputs": {
            "requirements_matrix_path": MATRIX_PATH.relative_to(ROOT).as_posix(),
            "requirements_matrix_canonical_sha256": matrix_canonical,
            "requirements_matrix_file_sha256": matrix_file,
            "provider_candidate_audit_path": CANDIDATE_PATH.relative_to(ROOT).as_posix(),
            "provider_candidate_audit_canonical_sha256": candidate_canonical,
            "provider_candidate_audit_file_sha256": candidate_file,
            "service_source_audit_path": SOURCE_PATH.relative_to(ROOT).as_posix(),
            "service_source_audit_canonical_sha256": source_canonical,
            "service_source_audit_file_sha256": source_file,
            "provider_closure_audit_path": CLOSURE_PATH.relative_to(ROOT).as_posix(),
            "provider_closure_audit_canonical_sha256": closure_canonical,
            "provider_closure_audit_file_sha256": closure_file,
            "profiles_lock_path": PROFILE_LOCK_PATH.relative_to(ROOT).as_posix(),
            "profiles_lock_file_sha256": profile_sha,
        },
        "summary": summary,
        "service_demand_counts": dict(sorted(service_counts.items())),
        "disposition_counts": dict(sorted(disposition_counts.items())),
        "unit_service_resolutions": rows,
        "blockers": blockers,
        "limitations": [
            "An archive-closed candidate is evidence for profile construction, not a selected Host service or a compatibility result.",
            "No provider configuration was inferred; empty configuration is not assumed to satisfy a provider's runtime schema.",
            "Provider start-up side effects, configuration schemas, native asset availability and service interoperability were not executed or accepted.",
            "Git SSH dependencies, lifecycle policy, sqlite3 binding and Skia resource closure remain separate profile-readiness checks.",
        ],
    }
    return {**report_body, "report_sha256": canonical_sha256(report_body)}


def write_report(path: Path, report: dict[str, Any]) -> tuple[str, str]:
    raw = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    file_sha = sha256(raw)
    path.with_suffix(path.suffix + ".sha256").write_text(f"{file_sha}  {path.name}\n", encoding="ascii", newline="\n")
    return report["report_sha256"], file_sha


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--check", action="store_true", help="verify the saved report against current frozen inputs")
    args = parser.parse_args()
    try:
        if args.check:
            saved, saved_canonical, saved_file = load_report(args.output)
            rebuilt = audit(saved["generated_at_utc"])
            if rebuilt["report_sha256"] != saved_canonical:
                raise AuditError("saved provider resolution report is stale for current frozen inputs")
            print(json.dumps({
                "status": "verified",
                "canonical_sha256": saved_canonical,
                "file_sha256": saved_file,
                "summary": saved["summary"],
                "blockers": saved["blockers"],
            }, ensure_ascii=False, indent=2))
            return 0
        generated = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        report = audit(generated)
        canonical, file_sha = write_report(args.output, report)
        print(json.dumps({
            "status": "blocked" if not report["summary"]["profile_ready"] else "ready",
            "path": str(args.output),
            "canonical_sha256": canonical,
            "file_sha256": file_sha,
            "summary": report["summary"],
            "blockers": report["blockers"],
        }, ensure_ascii=False, indent=2))
        return 0
    except (AuditError, OSError, KeyError, ValueError, json.JSONDecodeError) as error:
        print(f"Koishi provider resolution audit failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
