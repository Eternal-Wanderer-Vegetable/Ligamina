"""Audit frozen Koishi artifacts for Host service-profile discovery.

This is an offline metadata/source inspection tool. It never installs or runs
plugins, invokes npm lifecycle scripts, starts a Worker, or edits frozen inputs.
Static `inject` matches are discovery evidence, not proof of complete runtime
service requirements.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import tarfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE_ROOT = ROOT.parent / "Ligamina-artifact-cache"
SOURCE_SUFFIXES = {".js", ".mjs", ".cjs", ".ts"}
NON_RUNTIME_DIRS = {"example", "examples", "fixture", "fixtures", "test", "tests", "doc", "docs", "documentation", "bench", "benchmarks"}
INJECT_ASSIGNMENT = re.compile(
    r"(?:(?:exports|module\.exports|[A-Za-z_$][\w$]*)\.)?inject\s*=\s*(\[[^\]]{0,2000}\]|\{[^}]{0,4000}\})",
    re.DOTALL,
)
STRING_LITERAL = re.compile(r"['\"]([^'\"]+)['\"]")
REQUIRED_ARRAY = re.compile(r"\brequired\s*:\s*(\[[^\]]*\])", re.DOTALL)
OPTIONAL_ARRAY = re.compile(r"\boptional\s*:\s*(\[[^\]]*\])", re.DOTALL)


class AuditError(RuntimeError):
    """Raised when a frozen input or cache identity does not verify."""


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


def find_inject_assignments(text: str) -> list[dict[str, Any]]:
    assignments: list[dict[str, Any]] = []
    for match in INJECT_ASSIGNMENT.finditer(text):
        block = match.group(1)
        required: list[str] = []
        optional: list[str] = []
        syntax = "array"
        if block.lstrip().startswith("{"):
            syntax = "service-object"
            required_match = REQUIRED_ARRAY.search(block)
            optional_match = OPTIONAL_ARRAY.search(block)
            if required_match:
                required = STRING_LITERAL.findall(required_match.group(1))
            if optional_match:
                optional = STRING_LITERAL.findall(optional_match.group(1))
        else:
            required = STRING_LITERAL.findall(block)
        assignments.append(
            {
                "offset": match.start(),
                "syntax": syntax,
                "required": sorted(set(required)),
                "optional": sorted(set(optional)),
                "literal_sha256": sha256_bytes(block.encode("utf-8")),
            }
        )
    return assignments


def scan_artifact(path: Path, expected_name: str, expected_version: str) -> dict[str, Any]:
    try:
        archive = tarfile.open(path, mode="r:gz")
    except (OSError, tarfile.TarError) as error:
        raise AuditError(f"invalid npm tarball {path}: {error}") from error
    with archive:
        members = archive.getmembers()
        metadata_candidates: list[tuple[str, dict[str, Any]]] = []
        source_files = 0
        source_bytes = 0
        assignments: list[dict[str, Any]] = []
        inject_mention_files: set[str] = set()
        for member in members:
            if not member.isfile():
                continue
            posix = PurePosixPath(member.name)
            if posix.name == "package.json" and member.name.startswith("package/"):
                try:
                    loaded = json.loads(archive.extractfile(member).read().decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError, AttributeError) as error:
                    raise AuditError(f"invalid package metadata in {path}: {error}") from error
                if isinstance(loaded, dict):
                    metadata_candidates.append((member.name, loaded))
            if (
                posix.suffix.lower() not in SOURCE_SUFFIXES
                or posix.name.endswith(".d.ts")
                or member.size > 8_000_000
                or NON_RUNTIME_DIRS.intersection(part.lower() for part in posix.parts[1:-1])
            ):
                continue
            source_files += 1
            raw = archive.extractfile(member).read()
            source_bytes += len(raw)
            text = raw.decode("utf-8", "replace")
            current = find_inject_assignments(text)
            for assignment in current:
                assignment["file"] = member.name
            assignments.extend(current)
            if re.search(r"\binject\b", text, re.IGNORECASE):
                inject_mention_files.add(member.name)
    if not metadata_candidates:
        raise AuditError(f"package metadata missing from {path}")
    metadata_candidates.sort(key=lambda item: (item[0] != "package/package.json", item[0]))
    selected_metadata = next(
        (
            item
            for item in metadata_candidates
            if item[1].get("name") == expected_name and item[1].get("version") == expected_version
        ),
        metadata_candidates[0],
    )
    metadata_path, metadata = selected_metadata

    service = ((metadata.get("koishi") or {}).get("service") or {})
    if not isinstance(service, dict):
        service = {}
    metadata_required = service.get("required", [])
    metadata_optional = service.get("optional", [])
    metadata_implements = service.get("implements", [])
    static_required = sorted({name for item in assignments for name in item["required"]})
    static_optional = sorted({name for item in assignments for name in item["optional"]})
    recognized_files = {item["file"] for item in assignments}
    return {
        "package_metadata": {
            "path": metadata_path,
            "name": metadata.get("name"),
            "version": metadata.get("version"),
            "matches_catalog_identity": metadata.get("name") == expected_name and metadata.get("version") == expected_version,
            "manifest_candidates": [
                {"path": candidate_path, "name": candidate.get("name"), "version": candidate.get("version")}
                for candidate_path, candidate in metadata_candidates
            ],
            "service_required": metadata_required if isinstance(metadata_required, list) else [],
            "service_optional": metadata_optional if isinstance(metadata_optional, list) else [],
            "service_implements": metadata_implements if isinstance(metadata_implements, list) else [],
            "dependencies": metadata.get("dependencies", {}) if isinstance(metadata.get("dependencies", {}), dict) else {},
            "peer_dependencies": metadata.get("peerDependencies", {}) if isinstance(metadata.get("peerDependencies", {}), dict) else {},
            "optional_dependencies": metadata.get("optionalDependencies", {}) if isinstance(metadata.get("optionalDependencies", {}), dict) else {},
        },
        "source_files_scanned": source_files,
        "source_bytes_scanned": source_bytes,
        "inject_assignments": sorted(assignments, key=lambda item: (item["file"], item["offset"])),
        "inject_mention_files_without_recognized_assignment": sorted(inject_mention_files - recognized_files),
        "static_required_services": static_required,
        "static_optional_services": static_optional,
    }


def _sha_field(value: dict[str, Any], label: str) -> str:
    digest = value.get("manifest_sha256")
    actual = canonical_sha256({key: item for key, item in value.items() if key != "manifest_sha256"})
    if digest != actual:
        raise AuditError(f"{label} canonical SHA mismatch: expected={digest!r} actual={actual}")
    return actual


def _profile_lock_for(entry: dict[str, Any], profiles_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    profile_entry = profiles_by_id.get(entry["registry_entry_id"])
    if not profile_entry:
        raise AuditError(f"profile lock entry missing: {entry['registry_entry_id']}")
    profile = profile_entry.get("profiles", {}).get("windows-x86_64")
    if not isinstance(profile, dict) or profile.get("status") != "locked":
        raise AuditError(f"Windows dependency lock missing: {entry['registry_entry_id']}")
    return profile


def _artifact_for(entry: dict[str, Any], identity: dict[str, Any]) -> dict[str, Any]:
    matches = [
        artifact.get("artifact", {})
        for artifact in entry.get("artifacts", [])
        if artifact.get("status") == "locked"
        and artifact.get("artifact", {}).get("packagetype") == "npm-tarball"
        and artifact.get("artifact", {}).get("computed_digests", {}).get("sha256") == identity.get("artifact_sha256")
    ]
    if len(matches) != 1:
        raise AuditError(f"expected one exact npm tarball lock for {identity.get('registry_entry_id')}; got {len(matches)}")
    return matches[0]


def run_audit(options: argparse.Namespace) -> dict[str, Any]:
    recovery_manifest, recovery_raw = read_json(options.recovery_manifest)
    pilot_manifest, pilot_manifest_raw = read_json(options.pilot_manifest)
    pilot_report, pilot_report_raw = read_json(options.pilot_report)
    artifact_lock, artifact_lock_raw = read_json(options.artifact_lock)
    profiles_lock, profiles_lock_raw = read_json(options.profiles_lock)
    cache_audit, cache_audit_raw = read_json(options.cache_audit)

    recovery_sha = _sha_field(recovery_manifest, "recovery manifest")
    pilot_sha = _sha_field(pilot_manifest, "pilot manifest")
    if pilot_report.get("manifest_sha256") != pilot_sha:
        raise AuditError("pilot report is not bound to the supplied pilot manifest")
    if artifact_lock.get("snapshot_id") != recovery_manifest.get("snapshot_id") or profiles_lock.get("snapshot_id") != recovery_manifest.get("snapshot_id"):
        raise AuditError("catalog lock snapshot identity mismatch")
    if cache_audit.get("inputs", {}).get("recovery_manifest_sha256") != recovery_sha:
        raise AuditError("cache audit is not bound to the supplied recovery manifest")

    closed_pilot_ids = {
        row.get("identity", {}).get("registry_entry_id")
        for row in pilot_manifest.get("selection", [])
        if row.get("identity", {}).get("ecosystem") == "koishi"
    }
    if len(closed_pilot_ids) != 10:
        raise AuditError(f"expected 10 closed Koishi pilot units; got {len(closed_pilot_ids)}")
    rows = [
        row
        for row in recovery_manifest.get("selection", [])
        if row.get("identity", {}).get("ecosystem") == "koishi"
        and row.get("identity", {}).get("platform") == "windows"
        and row.get("identity", {}).get("registry_entry_id") not in closed_pilot_ids
    ]
    if len(rows) != 167:
        raise AuditError(f"expected 167 remaining Windows Koishi units; got {len(rows)}")

    artifact_by_id = {
        row.get("registry_entry_id"): row
        for row in artifact_lock.get("entries", [])
        if row.get("ecosystem") == "koishi"
    }
    profile_by_id = {
        row.get("registry_entry_id"): row
        for row in profiles_lock.get("entries", [])
        if row.get("ecosystem") == "koishi"
    }
    cache_root = options.cache_root.resolve(strict=True)
    package_rows: list[dict[str, Any]] = []
    service_counts: Counter[str] = Counter()
    metadata_service_units = 0
    assignment_units = 0
    required_units = 0
    unresolved_inject_units = 0
    manifest_identity_mismatches = 0
    root_manifests_missing = 0

    for selection in rows:
        identity = selection["identity"]
        entry_id = identity["registry_entry_id"]
        artifact_entry = artifact_by_id.get(entry_id)
        if not artifact_entry:
            raise AuditError(f"artifact lock entry missing: {entry_id}")
        artifact = _artifact_for(artifact_entry, identity)
        artifact_path = cache_root / artifact["cache_path"]
        artifact_raw_sha = sha256_file(artifact_path)
        if artifact_raw_sha != identity.get("artifact_sha256"):
            raise AuditError(f"plugin artifact SHA mismatch: {entry_id}")
        if artifact.get("computed_digests", {}).get("sha256") != artifact_raw_sha:
            raise AuditError(f"artifact-lock digest mismatch: {entry_id}")

        profile = _profile_lock_for(artifact_entry, profile_by_id)
        lock_ref = profile.get("dependency_lock", {})
        dependency_lock_path = cache_root / lock_ref.get("cache_path", "")
        dependency_lock_sha = sha256_file(dependency_lock_path)
        if dependency_lock_sha != identity.get("dependency_lock_sha256") or dependency_lock_sha != lock_ref.get("sha256"):
            raise AuditError(f"dependency-lock SHA mismatch: {entry_id}")

        scan = scan_artifact(artifact_path, identity["package"], identity["version"])
        metadata = scan["package_metadata"]
        if not metadata["matches_catalog_identity"]:
            manifest_identity_mismatches += 1
        if metadata["path"] != "package/package.json":
            root_manifests_missing += 1
        if metadata["service_required"] or metadata["service_optional"] or metadata["service_implements"]:
            metadata_service_units += 1
        if scan["inject_assignments"]:
            assignment_units += 1
        if scan["static_required_services"]:
            required_units += 1
        if scan["inject_mention_files_without_recognized_assignment"]:
            unresolved_inject_units += 1
        for name in scan["static_required_services"]:
            service_counts[name] += 1

        package_rows.append(
            {
                "registry_entry_id": entry_id,
                "package": identity["package"],
                "version": identity["version"],
                "artifact_sha256": artifact_raw_sha,
                "dependency_lock_sha256": dependency_lock_sha,
                "package_metadata": metadata,
                "source_files_scanned": scan["source_files_scanned"],
                "source_bytes_scanned": scan["source_bytes_scanned"],
                "inject_assignments": scan["inject_assignments"],
                "inject_mention_files_without_recognized_assignment": scan["inject_mention_files_without_recognized_assignment"],
                "static_required_services": scan["static_required_services"],
                "static_optional_services": scan["static_optional_services"],
            }
        )

    pilot_report_by_id = {
        row.get("result", {}).get("registry_entry_id"): row.get("result", {})
        for row in pilot_report.get("results", [])
        if row.get("result", {}).get("ecosystem") == "koishi"
    }
    pilot_reference: list[dict[str, Any]] = []
    comparable = 0
    matched = 0
    mismatched = 0
    for row in pilot_manifest.get("selection", []):
        identity = row.get("identity", {})
        if identity.get("ecosystem") != "koishi":
            continue
        artifact_entry = artifact_by_id.get(identity.get("registry_entry_id"))
        if not artifact_entry:
            raise AuditError(f"pilot artifact lock entry missing: {identity.get('registry_entry_id')}")
        artifact = _artifact_for(artifact_entry, identity)
        artifact_path = cache_root / artifact["cache_path"]
        if sha256_file(artifact_path) != identity.get("artifact_sha256"):
            raise AuditError(f"pilot plugin artifact SHA mismatch: {identity.get('registry_entry_id')}")
        scan = scan_artifact(artifact_path, identity["package"], identity["version"])
        report_row = pilot_report_by_id.get(identity.get("registry_entry_id"), {})
        protocol = report_row.get("worker", {}).get("protocol_report", {})
        native = protocol.get("native_load", {}) if isinstance(protocol, dict) else {}
        reported_services = native.get("required_services") if isinstance(native, dict) else None
        if isinstance(reported_services, list):
            comparable += 1
            actual = sorted(set(reported_services))
            discovered = scan["static_required_services"]
            if actual == discovered:
                matched += 1
            else:
                mismatched += 1
        else:
            actual = None
        pilot_reference.append(
            {
                "registry_entry_id": identity.get("registry_entry_id"),
                "package": identity.get("package"),
                "reported_required_services": actual,
                "static_required_services": scan["static_required_services"],
                "comparable": isinstance(reported_services, list),
                "match": actual == scan["static_required_services"] if isinstance(reported_services, list) else None,
            }
        )

    cache_audit_summary = cache_audit.get("cache_audit", {})
    service_inventory = [
        {"name": name, "units": count}
        for name, count in sorted(service_counts.items())
    ]
    worker_contract_paths = [
        ROOT / "lab/pilot_workers.py",
        ROOT / "lab/automation/recovery.py",
        ROOT / "scripts/phase3-windows-pilot.py",
    ]
    readiness_blockers = [
        "Static source discovery is heuristic and does not resolve dynamic or runtime-dependent service requirements.",
        "A versioned provider package, artifact digest, configuration, and combined per-entry dependency-lock binding are not frozen for every required service.",
        "The verified cache audit still reports two unsupported Git SSH dependency refs and 113 lifecycle-script package occurrences across the frozen dependency locks.",
        "The closed Windows pilot observed a missing sqlite3 Node binding with lifecycle scripts disabled; no independent Windows binding artifact or approved install route is frozen.",
    ]
    if manifest_identity_mismatches or root_manifests_missing:
        readiness_blockers.append("A plugin tarball package manifest does not match the catalog identity or is not at the package root; investigate before profile freeze.")
    report: dict[str, Any] = {
        "schema_version": 1,
        "kind": "phase3_p305_windows_koishi_host_profile_static_audit",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "scope": "offline frozen-tarball source discovery only; no npm install, lifecycle script, catalog plugin, Worker, or Sandbox execution",
        "scanner": {
            "source_suffixes": sorted(SOURCE_SUFFIXES),
            "excluded_directory_names": sorted(NON_RUNTIME_DIRS),
            "excluded_declaration_files": ["*.d.ts"],
            "recognized_forms": ["literal inject array", "literal inject object with required/optional arrays"],
            "limitations": ["regex-based source discovery", "dynamic/computed values are not resolved", "only runtime-like bundled/source files are scanned"],
        },
        "inputs": {
            "recovery_manifest_path": options.recovery_manifest.relative_to(ROOT).as_posix(),
            "recovery_manifest_canonical_sha256": recovery_sha,
            "recovery_manifest_file_sha256": sha256_bytes(recovery_raw),
            "closed_pilot_manifest_path": options.pilot_manifest.relative_to(ROOT).as_posix(),
            "closed_pilot_manifest_canonical_sha256": pilot_sha,
            "closed_pilot_manifest_file_sha256": sha256_bytes(pilot_manifest_raw),
            "closed_pilot_report_path": options.pilot_report.relative_to(ROOT).as_posix(),
            "closed_pilot_report_file_sha256": sha256_bytes(pilot_report_raw),
            "artifact_lock_path": options.artifact_lock.relative_to(ROOT).as_posix(),
            "artifact_lock_file_sha256": sha256_bytes(artifact_lock_raw),
            "profiles_lock_path": options.profiles_lock.relative_to(ROOT).as_posix(),
            "profiles_lock_file_sha256": sha256_bytes(profiles_lock_raw),
            "cache_audit_path": options.cache_audit.relative_to(ROOT).as_posix(),
            "cache_audit_file_sha256": sha256_bytes(cache_audit_raw),
            "cache_root": str(cache_root),
            "worker_contract_sources": [
                {"path": path.relative_to(ROOT).as_posix(), "file_sha256": sha256_file(path)}
                for path in worker_contract_paths
            ],
        },
        "summary": {
            "expected_remaining_units": 167,
            "verified_plugin_artifacts": len(package_rows),
            "verified_dependency_locks": len(package_rows),
            "package_metadata_service_declarations": metadata_service_units,
            "artifact_package_manifest_identity_mismatches": manifest_identity_mismatches,
            "artifact_package_manifest_identity_mismatch_units": [
                {"registry_entry_id": row["registry_entry_id"], "package": row["package"], "version": row["version"], "package_metadata": row["package_metadata"]}
                for row in package_rows if not row["package_metadata"]["matches_catalog_identity"]
            ],
            "artifact_root_package_manifests_missing": root_manifests_missing,
            "artifact_root_package_manifest_missing_units": [
                {"registry_entry_id": row["registry_entry_id"], "package": row["package"], "version": row["version"], "package_metadata": row["package_metadata"]}
                for row in package_rows if row["package_metadata"]["path"] != "package/package.json"
            ],
            "units_with_literal_inject_assignments": assignment_units,
            "units_with_static_required_services": required_units,
            "units_with_inject_mentions_without_recognized_assignment": unresolved_inject_units,
            "unique_static_required_service_names": len(service_counts),
            "static_required_service_inventory": service_inventory,
            "pilot_reference_units": len(pilot_reference),
            "pilot_reference_comparable_units": comparable,
            "pilot_reference_matches": matched,
            "pilot_reference_mismatches": mismatched,
            "pilot_reference_incomparable_units": len(pilot_reference) - comparable,
            "pilot_reference_rows": pilot_reference,
            "worker_lock_contract": "the current Koishi worker prepares the project from the selected entry dependency lock; provider names in host_service_plugins do not themselves add provider artifacts to that lock",
            "profile_ready": False,
            "readiness_blockers": readiness_blockers,
            "next_actions": [
                "Resolve each static service name to a pinned provider package and config, including custom services; classify unresolved or dynamic requirements explicitly.",
                "Build and verify a combined per-entry dependency lock that contains the plugin and every frozen provider, without changing the closed pilot or affected manifests.",
                "Resolve the exact Git SSH commit source and lifecycle-script/native-binding policy as separate, hash-bound inputs.",
                "Only after those bindings are complete, freeze a new independent profile lock, manifest, checkpoint, and report; validate without dispatch, then run catalog units only in Windows Sandbox.",
            ],
        },
        "selection": package_rows,
        "cache_audit_blockers": {
            "unsupported_dependency_refs": cache_audit_summary.get("unsupported_dependency_refs", []),
            "lifecycle_script_package_occurrences": cache_audit_summary.get("lifecycle_script_package_occurrences"),
            "lifecycle_script_package_names": cache_audit_summary.get("lifecycle_script_package_names", []),
            "sqlite3_lifecycle_rows": cache_audit_summary.get("sqlite3_lifecycle_rows", []),
        },
        "interpretation": "This report establishes hash-bound offline source observations only. Literal inject matches are useful candidates for Host-profile research, not an exhaustive dependency contract, a profile-ready decision, or a plugin compatibility result.",
    }
    report["report_sha256"] = canonical_sha256(report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recovery-manifest", type=Path, default=ROOT / "outputs/acceptance/phase3-p301-recovery-manifest-20261020.json")
    parser.add_argument("--pilot-manifest", type=Path, default=ROOT / "outputs/acceptance/phase3-p305-windows-pilot-manifest-v2-20261025.json")
    parser.add_argument("--pilot-report", type=Path, default=ROOT / "outputs/acceptance/phase3-p305-windows-pilot-v2-execution-20261025.json")
    parser.add_argument("--artifact-lock", type=Path, default=ROOT / "catalogs/p2-41649744e057d61031d5/artifacts.lock.json")
    parser.add_argument("--profiles-lock", type=Path, default=ROOT / "catalogs/p2-41649744e057d61031d5/profiles.lock.json")
    parser.add_argument("--cache-audit", type=Path, default=ROOT / "outputs/acceptance/phase3-p305-windows-koishi-cache-audit-20261025.json")
    parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    parser.add_argument("--output", type=Path, default=ROOT / "outputs/acceptance/phase3-p305-windows-koishi-host-profile-static-audit-v3-20261026.json")
    options = parser.parse_args()
    try:
        report = run_audit(options)
        output = options.output.resolve()
        sidecar = output.with_suffix(output.suffix + ".sha256")
        if output.exists() or sidecar.exists():
            raise AuditError(f"refusing to overwrite existing evidence: {output}")
        output.parent.mkdir(parents=True, exist_ok=True)
        raw = (json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        output.write_bytes(raw)
        file_sha = sha256_bytes(raw)
        sidecar.write_text(f"{file_sha}  {output.name}\n", encoding="utf-8")
        print(json.dumps({"report": str(output), "report_sha256": report["report_sha256"], "file_sha256": file_sha, "summary": report["summary"]}, ensure_ascii=False, sort_keys=True))
    except (AuditError, OSError, json.JSONDecodeError, tarfile.TarError) as error:
        parser.exit(2, f"audit failed: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
