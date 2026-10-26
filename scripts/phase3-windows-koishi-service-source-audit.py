"""Bind Koishi service-provider source clues to frozen Windows artifacts.

This is an offline source audit only. It reads locked npm tarballs and lock
blobs; it does not install or execute packages, run lifecycle scripts, start a
Worker, or launch Windows Sandbox. Source evidence is not runtime readiness.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE_ROOT = ROOT.parent / "Ligamina-artifact-cache"
STATIC_AUDIT = ROOT / "outputs/acceptance/phase3-p305-windows-koishi-host-profile-static-audit-v3-20261026.json"
PROVIDER_AUDIT = ROOT / "outputs/acceptance/phase3-p305-windows-koishi-provider-candidate-audit-v1-20261026.json"
OUTPUT = ROOT / "outputs/acceptance/phase3-p305-windows-koishi-service-source-audit-v1-20261026.json"

# These exact catalog identities were selected from the no-metadata service
# inventory. Each source rule is intentionally narrow and must match the
# hash-locked archive member before any claim is emitted.
PROVIDERS: tuple[dict[str, Any], ...] = (
    {
        "package": "koishi-plugin-bilibili-login", "version": "0.1.50",
        "registry_entry_id": "ko-429c6634899115798dfa92ab",
        "services": [
            ("BiliBiliLive", "BiliBiliLive", "package/lib/API/Service/BiliBiliLive.js"),
            ("BiliBiliLogin", "BiliBiliLogin", "package/lib/API/Service/BilibiliLogin.js"),
            ("BiliBiliMovie", "BiliBiliMovie", "package/lib/API/Service/BiliBiliMovie.js"),
            ("BiliBiliSearch", "BiliBiliSearch", "package/lib/API/Service/BiliBiliSearch.js"),
            ("BiliBiliVideo", "BiliBiliVideo", "package/lib/API/Service/BilibiliVideo.js"),
        ],
        "registration_member": "package/lib/API/index.js",
        "registration_pattern": r"ctx\.plugin\(Service_1\.{class_name}\s*\)",
    },
    {
        "package": "@quanhuzeyu/koishi-plugin-qhzy-sharp", "version": "1.2.1",
        "registry_entry_id": "ko-31a0a85bbbf89e5f2955070e",
        "services": [("QhzySharp", "SharpService", "package/lib/index.js")],
        "registration_member": "package/lib/index.js",
        "registration_pattern": r"ctx\.plugin\(SharpService\s*\)",
    },
    {
        "package": "koishi-plugin-fonts", "version": "1.0.4",
        "registry_entry_id": "ko-d5d43667d9446f27dd1b1433",
        "services": [("fonts", "Fonts", "package/lib/index.js")],
        "registration_member": "package/lib/index.js",
        "registration_pattern": r"ctx\.plugin\(Fonts\s*,\s*config\s*\)",
    },
    {
        "package": "koishi-plugin-smmcat-localstorage", "version": "0.0.2",
        "registry_entry_id": "ko-187be45372ea214c5d8fce80",
        "services": [("localstorage", "localstorageClass", "package/lib/index.js")],
        "registration_member": "package/lib/index.js",
        "registration_pattern": r"ctx\.plugin\(localstorageClass\s*,\s*config\s*\)",
    },
    {
        "package": "koishi-plugin-nazrin-core", "version": "0.1.25",
        "registry_entry_id": "ko-8ab0ce43e89bc209361f3cf7",
        "services": [("nazrin", "nazrin", "package/lib/Service/index.js")],
        "registration_member": "package/lib/Core/index.js",
        "registration_pattern": r"ctx\.plugin\(Service_1\.nazrin\s*\)",
    },
    {
        "package": "@ltxhhz/koishi-plugin-skia-canvas", "version": "0.0.10",
        "registry_entry_id": "ko-68501ea7ad6acec9815245ef",
        "services": [("skia", "Skia", "package/lib/index.js")],
        "registration_member": None,
        "registration_pattern": None,
    },
    {
        "package": "koishi-plugin-hajimi-lizard", "version": "1.1.0",
        "registry_entry_id": "ko-1713c88871bd1765d4e787e4",
        "services": [("hajimi", "HajimiService", "package/lib/index.js")],
        "registration_member": "package/lib/index.js",
        "registration_pattern": r"ctx\.plugin\(HajimiService\s*,\s*config\s*\)",
        "self_service_candidate": True,
    },
)

CORE_SERVICES = {"http", "i18n"}
DOCS = {
    "core_context": "https://koishi.chat/en-US/api/core/context",
    "core_builtin_services": "https://koishi.chat/en-US/api/",
    "service_plugins": "https://koishi.chat/en-US/market/service",
    "publishing_metadata": "https://koishi.chat/en-US/guide/develop/publish",
}


class AuditError(RuntimeError):
    """Raised when an identity or source assertion fails."""


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


def verify_sidecar(path: Path) -> str:
    actual = sha256_file(path)
    sidecar = path.with_suffix(path.suffix + ".sha256")
    if sidecar.is_file():
        expected = sidecar.read_text(encoding="utf-8").strip().split()[0]
        if expected != actual:
            raise AuditError(f"sidecar mismatch for {path}: expected={expected}, actual={actual}")
    return actual


def verify_report(path: Path) -> tuple[dict[str, Any], bytes, str]:
    value, raw = read_json(path)
    without_hash = {key: item for key, item in value.items() if key != "report_sha256"}
    actual = canonical_sha256(without_hash)
    if value.get("report_sha256") != actual:
        raise AuditError(f"canonical report hash mismatch: {path}")
    return value, raw, verify_sidecar(path)


def source_evidence(member_name: str, raw: bytes, pattern: str, label: str) -> dict[str, Any]:
    text = raw.decode("utf-8", "strict")
    match = re.search(pattern, text, re.DOTALL)
    if not match:
        raise AuditError(f"expected {label} source pattern missing in {member_name}")
    start = match.start()
    line_number = text.count("\n", 0, start) + 1
    excerpt = text[max(0, start - 80):min(len(text), match.end() + 120)].replace("\n", " ")
    return {
        "member": member_name,
        "member_sha256": sha256_bytes(raw),
        "member_size_bytes": len(raw),
        "line": line_number,
        "char_offset": start,
        "matched_source": match.group(0),
        "excerpt": excerpt[:360],
    }


def usage_template_contains(text: str, offset: int) -> bool:
    marker = re.search(r"(?:export\s+)?(?:const|let|var)\s+usage\s*=\s*`", text[:offset])
    if not marker:
        return False
    end = offset
    while True:
        end = text.find("`", end)
        if end < 0:
            return False
        backslashes = 0
        cursor = end - 1
        while cursor >= 0 and text[cursor] == "\\":
            backslashes += 1
            cursor -= 1
        if backslashes % 2 == 0:
            return True
        end += 1


def run(options: argparse.Namespace) -> dict[str, Any]:
    static_path = options.static_audit.resolve(strict=True)
    provider_path = options.provider_audit.resolve(strict=True)
    static, static_raw, static_file_sha = verify_report(static_path)
    prior, prior_raw, prior_file_sha = verify_report(provider_path)
    if prior.get("inputs", {}).get("static_audit_canonical_sha256") != static.get("report_sha256"):
        raise AuditError("provider audit is bound to a different static-audit canonical hash")

    inputs = prior.get("inputs", {})
    cache_root = options.cache_root.resolve(strict=True)
    catalog_path = ROOT / "catalogs/p2-41649744e057d61031d5/catalog.json"
    artifact_lock_path = ROOT / "catalogs/p2-41649744e057d61031d5/artifacts.lock.json"
    profiles_lock_path = ROOT / "catalogs/p2-41649744e057d61031d5/profiles.lock.json"
    catalog, catalog_raw = read_json(catalog_path)
    artifacts, artifacts_raw = read_json(artifact_lock_path)
    profiles, profiles_raw = read_json(profiles_lock_path)
    if sha256_bytes(artifacts_raw) != inputs.get("artifact_lock_file_sha256"):
        raise AuditError("artifact lock file changed since provider audit")
    if sha256_bytes(profiles_raw) != inputs.get("profiles_lock_file_sha256"):
        raise AuditError("profiles lock file changed since provider audit")
    if profiles.get("artifacts_lock_sha256") != sha256_bytes(artifacts_raw):
        raise AuditError("profiles lock is not bound to current artifact lock bytes")
    verify_sidecar(artifact_lock_path)
    verify_sidecar(profiles_lock_path)
    if catalog.get("snapshot_id") != inputs.get("catalog_snapshot_id"):
        raise AuditError("catalog snapshot identity changed")

    catalog_by_id = {row.get("registry_entry_id"): row for row in catalog.get("entries", [])}
    artifacts_by_id = {row.get("registry_entry_id"): row for row in artifacts.get("entries", [])}
    profiles_by_id = {row.get("registry_entry_id"): row for row in profiles.get("entries", [])}
    selected_by_id = {row.get("registry_entry_id"): row for row in static.get("selection", [])}
    inventory_by_name = {row.get("service_name"): row for row in prior.get("service_inventory", [])}

    providers = []
    service_proof: dict[str, dict[str, Any]] = {}
    for spec in PROVIDERS:
        entry_id = spec["registry_entry_id"]
        catalog_entry = catalog_by_id.get(entry_id)
        if not catalog_entry or catalog_entry.get("package") != spec["package"] or catalog_entry.get("advertised_version") != spec["version"]:
            raise AuditError(f"catalog identity mismatch for {spec['package']}@{spec['version']}")
        artifact_row = artifacts_by_id.get(entry_id)
        profile_row = profiles_by_id.get(entry_id)
        if not artifact_row or not profile_row:
            raise AuditError(f"frozen lock row missing for {entry_id}")
        win_profile = (profile_row.get("profiles") or {}).get("windows-x86_64", {})
        if win_profile.get("status") != "locked":
            raise AuditError(f"Windows profile is not locked for {entry_id}")
        artifact_matches = [
            item.get("artifact", {}) for item in artifact_row.get("artifacts", [])
            if item.get("status") == "locked" and item.get("artifact", {}).get("packagetype") == "npm-tarball"
        ]
        if len(artifact_matches) != 1:
            raise AuditError(f"expected one locked tarball for {entry_id}")
        artifact = artifact_matches[0]
        tarball_path = cache_root / artifact.get("cache_path", "")
        if not tarball_path.is_file():
            raise AuditError(f"locked tarball unavailable for {entry_id}")
        archive_sha = sha256_file(tarball_path)
        expected_archive_sha = artifact.get("computed_digests", {}).get("sha256")
        if archive_sha != expected_archive_sha:
            raise AuditError(f"locked tarball SHA mismatch for {entry_id}")
        lock = win_profile.get("dependency_lock") or {}
        lock_path = cache_root / lock.get("cache_path", "")
        if not lock_path.is_file() or sha256_file(lock_path) != lock.get("sha256"):
            raise AuditError(f"locked dependency-lock blob mismatch for {entry_id}")

        with tarfile.open(tarball_path, mode="r:gz") as archive:
            members = {item.name: item for item in archive.getmembers() if item.isfile()}
            manifest_member = members.get("package/package.json")
            if not manifest_member:
                raise AuditError(f"package manifest missing in {entry_id}")
            manifest_raw = archive.extractfile(manifest_member).read()
            package_manifest = json.loads(manifest_raw.decode("utf-8", "strict"))
            if package_manifest.get("name") != spec["package"] or package_manifest.get("version") != spec["version"]:
                raise AuditError(f"tarball package identity mismatch for {entry_id}")

            service_rows = []
            for service_name, class_name, member_name in spec["services"]:
                source_member = members.get(member_name)
                if not source_member:
                    raise AuditError(f"source member missing for {entry_id}: {member_name}")
                raw = archive.extractfile(source_member).read()
                escaped_class = re.escape(class_name)
                class_pattern = (
                    rf"(?:class\s+{escaped_class}\s+extends"
                    rf"|(?:var|let|const)\s+{escaped_class}\s*=\s*class(?:\s+[A-Za-z_$][\w$]*)?\s+extends?"
                    rf"|(?:var|let|const)\s+{escaped_class}\s*=\s*class\s+extends)"
                    rf".{{0,2400}}?super\(ctx\s*,\s*['\"]{re.escape(service_name)}['\"]"
                )
                class_proof = source_evidence(member_name, raw, class_pattern, f"{class_name} service constructor")
                registration_proof = None
                if spec.get("registration_member"):
                    reg_member_name = spec["registration_member"]
                    reg_member = members.get(reg_member_name)
                    if not reg_member:
                        raise AuditError(f"registration member missing for {entry_id}: {reg_member_name}")
                    reg_raw = archive.extractfile(reg_member).read()
                    pattern = spec["registration_pattern"].format(class_name=re.escape(class_name))
                    registration_proof = source_evidence(reg_member_name, reg_raw, pattern, f"{service_name} plugin registration")

                service_meta_paths = []
                koishi = package_manifest.get("koishi", {})
                for key_path, value in (
                    ("koishi.implements", koishi.get("implements") if isinstance(koishi, dict) else None),
                    ("koishi.service.implements", ((koishi.get("service") or {}).get("implements") if isinstance(koishi, dict) and isinstance(koishi.get("service"), dict) else None)),
                    ("koishi.services.implements", ((koishi.get("services") or {}).get("implements") if isinstance(koishi, dict) and isinstance(koishi.get("services"), dict) else None)),
                ):
                    if isinstance(value, list):
                        service_meta_paths.append({"path": key_path, "values": value})
                service_rows.append({
                    "service_name": service_name,
                    "class_name": class_name,
                    "static_service_constructor": class_proof,
                    "static_registration": registration_proof,
                    "package_service_metadata": service_meta_paths,
                    "required_by_units": [
                        {"registry_entry_id": row_id, "package": selected_by_id[row_id].get("package"), "version": selected_by_id[row_id].get("version")}
                        for row_id, selected in selected_by_id.items()
                        if service_name in selected.get("static_required_services", [])
                    ],
                    "same_package_self_service_candidate": spec.get("self_service_candidate", False),
                    "profile_configuration_frozen_for_remaining_units": False,
                })
                service_proof[service_name] = service_rows[-1]

        providers.append({
            "package": spec["package"],
            "version": spec["version"],
            "registry_entry_id": entry_id,
            "artifact_sha256": archive_sha,
            "artifact_sha256_matches_lock": True,
            "windows_profile_id": win_profile.get("profile_id"),
            "windows_profile_status": win_profile.get("status"),
            "dependency_lock_sha256": lock.get("sha256"),
            "dependency_lock_blob_sha256_matches_lock": True,
            "dependency_archives_reverified": False,
            "package_manifest_sha256": sha256_bytes(manifest_raw),
            "package_manifest_identity_matches_catalog": True,
            "services": service_rows,
        })

    unmatched_names = sorted(prior.get("summary", {}).get("service_names_without_catalog_implementer_metadata", []))
    if len(unmatched_names) != 13 or any(name not in inventory_by_name for name in unmatched_names):
        raise AuditError(f"expected the prior audit's 13 unmatched service names, got {len(unmatched_names)}")
    crosswalk = []
    for service in unmatched_names:
        prior_row = inventory_by_name[service]
        if service in CORE_SERVICES:
            classification = "documented_core_builtin_service"
            evidence = {"document": DOCS["core_builtin_services"], "provider_package_selected": False}
        elif service in service_proof:
            classification = "hash_locked_archive_source_candidate"
            evidence = service_proof[service]
        else:
            raise AuditError(f"no source provider or built-in classification for {service}")
        crosswalk.append({
            "service_name": service,
            "previous_metadata_audit_classification": prior_row.get("classification"),
            "static_required_by_unit_count": prior_row.get("static_required_by_unit_count"),
            "static_required_by_units": prior_row.get("static_required_by_units", []),
            "source_audit_classification": classification,
            "source_evidence": evidence,
        })

    hajimi_false_positive = None
    hajimi_selection = next((row for row in static.get("selection", []) if row.get("registry_entry_id") == "ko-1713c88871bd1765d4e787e4"), None)
    if hajimi_selection:
        provider_spec = next(item for item in PROVIDERS if item["registry_entry_id"] == "ko-1713c88871bd1765d4e787e4")
        entry = catalog_by_id[provider_spec["registry_entry_id"]]
        art_row = artifacts_by_id[provider_spec["registry_entry_id"]]
        artifact = next(item["artifact"] for item in art_row["artifacts"] if item.get("status") == "locked" and item.get("artifact", {}).get("packagetype") == "npm-tarball")
        with tarfile.open(cache_root / artifact["cache_path"], mode="r:gz") as archive:
            members = {item.name: item for item in archive.getmembers() if item.isfile()}
            assignments = []
            for assignment in hajimi_selection.get("inject_assignments", []):
                member_name = assignment["file"]
                raw = archive.extractfile(members[member_name]).read()
                text = raw.decode("utf-8", "strict")
                offset = assignment["offset"]
                inside_usage = usage_template_contains(text, offset)
                if assignment.get("required") == ["hajimi"] and not inside_usage:
                    raise AuditError("expected the hajimi static match to be inside usage template text")
                assignments.append({
                    "member": member_name,
                    "member_sha256": sha256_bytes(raw),
                    "char_offset": offset,
                    "literal_sha256": assignment.get("literal_sha256"),
                    "inside_usage_template_literal": inside_usage,
                    "not_runtime_inject_evidence": inside_usage,
                })
            hajimi_false_positive = {
                "registry_entry_id": entry["registry_entry_id"],
                "package": entry["package"],
                "static_audit_assignments": assignments,
                "interpretation": "The scanner matches the text 'export const inject = [\'hajimi\']' inside the package usage template literal in both generated JS and source TS; these offsets are not executable inject declarations. The same locked package does statically construct/register HajimiService, so treat it as a self-service provider candidate rather than an external required-service dependency.",
            }

    report: dict[str, Any] = {
        "schema_version": 1,
        "kind": "phase3_p305_windows_koishi_service_source_audit",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "scope": "offline inspection of exact hash-locked Koishi Windows catalog provider tarballs and their source members; no package installation, code execution, lifecycle script, Worker, Sandbox, or App Control changes",
        "inputs": {
            "static_audit_path": static_path.relative_to(ROOT).as_posix(),
            "static_audit_canonical_sha256": static["report_sha256"],
            "static_audit_file_sha256": static_file_sha,
            "provider_candidate_audit_path": provider_path.relative_to(ROOT).as_posix(),
            "provider_candidate_audit_canonical_sha256": prior["report_sha256"],
            "provider_candidate_audit_file_sha256": prior_file_sha,
            "catalog_snapshot_id": catalog.get("snapshot_id"),
            "catalog_file_sha256": sha256_bytes(catalog_raw),
            "artifact_lock_file_sha256": sha256_bytes(artifacts_raw),
            "profiles_lock_file_sha256": sha256_bytes(profiles_raw),
            "cache_root": str(cache_root),
        },
        "official_document_references": DOCS,
        "summary": {
            "remaining_windows_koishi_units": 167,
            "static_required_service_names": len(inventory_by_name),
            "previously_unmatched_service_names": len(unmatched_names),
            "documented_core_builtin_service_names": len(CORE_SERVICES.intersection(unmatched_names)),
            "names_with_exact_archive_source_candidates": len(service_proof),
            "provider_package_archives_hash_verified": len(providers),
            "custom_service_names_in_six_external_provider_archives": sum(len(p["services"]) for p in providers if not any(s["same_package_self_service_candidate"] for s in p["services"])),
            "same_package_self_service_candidates": sum(1 for p in providers for s in p["services"] if s["same_package_self_service_candidate"]),
            "hajimi_static_inject_template_false_positive_confirmed": bool(hajimi_false_positive),
            "combined_target_entry_locks_frozen": 0,
            "provider_configurations_frozen": 0,
            "dependency_archives_reverified_for_provider_profiles": 0,
            "profile_ready": False,
        },
        "provider_archives": providers,
        "service_crosswalk": crosswalk,
        "static_scanner_caveats": [hajimi_false_positive] if hajimi_false_positive else [],
        "readiness_blockers": [
            "Source constructors and registration calls are static evidence only; none of these providers or target plugins was loaded in a Host or Sandbox.",
            "Provider dependency-lock blobs are hash-verified, but provider dependency archives and the combined target-entry dependency closures have not been verified for execution.",
            "No service provider set or configuration has been selected and frozen for the 167 remaining Koishi units; combined target-entry locks remain 0/167.",
            "The frozen profiles still include two unsupported Git SSH dependency references and 113 lifecycle-script package occurrences; sqlite3 Windows binding remains unresolved with scripts disabled.",
            "The Skia service is native-sensitive and its locked package source downloads platform assets at runtime; no approved offline Windows native asset closure is frozen.",
        ],
        "interpretation": "This source audit narrows service-provider discovery and corrects a template-string false positive. It is not a Host/native profile, dependency-closure, installation, load, behavior, or compatibility acceptance. Keep Koishi undispatched until a complete new profile and per-entry locks/configurations, manifest, checkpoint, and report are independently frozen and validate-only succeeds.",
    }
    report["report_sha256"] = canonical_sha256(report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--static-audit", type=Path, default=STATIC_AUDIT)
    parser.add_argument("--provider-audit", type=Path, default=PROVIDER_AUDIT)
    parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    parser.add_argument("--output", type=Path, default=OUTPUT)
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
    except (AuditError, OSError, json.JSONDecodeError, KeyError, tarfile.TarError, UnicodeDecodeError) as error:
        parser.exit(2, f"audit failed: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
