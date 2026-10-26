"""Build an immutable accounting report for the P3-08 behavior pilot scope.

This module does not execute catalog plugins. It binds the frozen P3-06 suite
to the admitted Worker/profile evidence and explicitly records positions that
cannot be dispatched because the suite contains prose stimuli rather than
runtime-specific Host event/setup fixtures.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .behavior import ROOT, SUITE_PATH, validate_behavior_suite


ARTIFACT_CACHE = Path(
    os.environ.get("LIGAMINA_ARTIFACT_CACHE", str(ROOT.parent / f"{ROOT.name}-artifact-cache"))
)
CATALOG_ROOT = ROOT / "catalogs/p2-41649744e057d61031d5"
WINDOWS_PROFILE_LOCKS = {
    "nonebot": ROOT / "outputs/acceptance/phase3-p305-windows-nonebot-evaluation-profile-lock-v2-20261025.json",
    "koishi": ROOT / "outputs/acceptance/phase3-p305-windows-koishi-evaluation-profile-lock-v1-20261026.json",
}
P304_ADMISSION = ROOT / "outputs/acceptance/phase3-p304-windows-admission-python31210-20261025.json"
P305_CLOSURE = ROOT / "outputs/acceptance/phase3-p305-overall-closure-20261026.json"
LINUX_ADMISSION = ROOT / "outputs/acceptance/phase3-p308-linux-admission-20261026.json"
LINUX_PROBES = ROOT / "outputs/acceptance/phase3-p308-linux-worker-probes-20261026.json"
P307_LEGACY = ROOT / "outputs/acceptance/phase3-p307-linux-legacy-20261020.json"


class BehaviorCampaignError(ValueError):
    """P3-08 accounting inputs do not match the frozen evidence."""


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BehaviorCampaignError(f"cannot read JSON {path}: {error}") from error
    if not isinstance(value, dict):
        raise BehaviorCampaignError(f"expected JSON object: {path}")
    return value


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _file_record(path: Path) -> dict[str, str]:
    raw = path.read_bytes()
    return {"path": path.relative_to(ROOT).as_posix(), "file_sha256": _sha256(raw)}


def _verified_cache_blob(lock: dict[str, Any]) -> dict[str, Any]:
    ref = lock.get("dependency_lock")
    if not isinstance(ref, dict):
        return {"status": "missing_identity"}
    cache_path = ref.get("cache_path")
    digest = ref.get("sha256")
    size = ref.get("size_bytes")
    if not isinstance(cache_path, str) or not isinstance(digest, str) or not isinstance(size, int):
        return {"status": "invalid_identity"}
    path = ARTIFACT_CACHE / cache_path
    try:
        raw = path.read_bytes()
    except OSError:
        return {"status": "not_available", "sha256": digest, "size_bytes": size}
    actual = _sha256(raw)
    return {
        "status": "verified" if actual == digest and len(raw) == size else "mismatch",
        "sha256": digest,
        "size_bytes": size,
        "observed_sha256": actual,
        "observed_size_bytes": len(raw),
    }


def _load_windows_profile_locks() -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, str]]]:
    rows: dict[str, dict[str, Any]] = {}
    records: dict[str, dict[str, str]] = {}
    for ecosystem, path in WINDOWS_PROFILE_LOCKS.items():
        document = _read(path)
        records[ecosystem] = _file_record(path)
        for row in document.get("entries", []):
            if row.get("ecosystem") == ecosystem and isinstance(row.get("registry_entry_id"), str):
                rows[row["registry_entry_id"]] = row
    return rows, records


def build_behavior_accounting_report() -> dict[str, Any]:
    """Account all 120 positions and their three planned attempts without dispatch."""
    suite_check = validate_behavior_suite(SUITE_PATH)
    suite = _read(SUITE_PATH)
    artifacts = _read(CATALOG_ROOT / "artifacts.lock.json")
    profiles = _read(CATALOG_ROOT / "profiles.lock.json")
    artifact_by_id = {row["registry_entry_id"]: row for row in artifacts["entries"]}
    profile_by_id = {row["registry_entry_id"]: row for row in profiles["entries"]}
    windows_profiles, windows_profile_records = _load_windows_profile_locks()

    admission = _read(P304_ADMISSION)
    linux_admission = _read(LINUX_ADMISSION)
    probes = _read(LINUX_PROBES)
    closure = _read(P305_CLOSURE)
    legacy = _read(P307_LEGACY)
    if admission.get("status") != "admitted" or admission.get("capabilities", {}).get("catalog_plugin_claims_allowed") is not False:
        raise BehaviorCampaignError("P3-04 Windows runtime-only admission state changed")
    if linux_admission.get("status") != "admitted" or linux_admission.get("capabilities", {}).get("claim_allowed") is not True:
        raise BehaviorCampaignError("P3-08 Linux Worker admission is not admitted")
    if probes.get("passed") is not True or len(probes.get("results", [])) != 10:
        raise BehaviorCampaignError("Linux Worker benign probe report is incomplete")
    if any(item.get("passed") is not True or item.get("cleanup_confirmed") is not True for item in probes["results"]):
        raise BehaviorCampaignError("Linux Worker benign probe did not reach its expected cleaned state")
    if closure.get("scope", {}).get("cross_platform_unique_units") != 3179 or closure.get("scope", {}).get("remaining_units") != 0:
        raise BehaviorCampaignError("P3-05 closure scope changed")
    if legacy.get("status") != "completed":
        raise BehaviorCampaignError("P3-07 historical regression evidence is not completed")

    positions: list[dict[str, Any]] = []
    seen_position_ids: set[str] = set()
    profile_verification_counts = {"verified": 0, "not_available": 0, "mismatch": 0, "missing_or_invalid": 0}
    for entry in suite["entries"]:
        identity = entry["registry_entry_id"]
        artifact_row = artifact_by_id.get(identity)
        suite_profile_row = profile_by_id.get(identity)
        if not isinstance(artifact_row, dict) or not isinstance(suite_profile_row, dict):
            raise BehaviorCampaignError(f"suite entry is missing catalog lock rows: {identity}")
        artifact_record = next((row.get("artifact") for row in artifact_row.get("artifacts", []) if row.get("status") == "locked"), None)
        if not isinstance(artifact_record, dict) or (artifact_record.get("computed_digests") or {}).get("sha256") != entry.get("artifact_sha256"):
            raise BehaviorCampaignError(f"artifact identity changed: {identity}")

        for case in entry["cases"]:
            stimulus = case.get("stimuli", [{}])[0]
            has_event_payload = isinstance(stimulus.get("events"), list) and bool(stimulus["events"])
            has_adapter_binding = isinstance(case.get("runtime_adapter_id"), str) and bool(case["runtime_adapter_id"])
            has_runtime_setup = isinstance(case.get("runtime_setup"), dict)
            fixture_ready = has_event_payload and has_adapter_binding and has_runtime_setup
            fixture_reason = None if fixture_ready else "missing_executable_host_fixture"

            for platform, profile_key in (("linux-x86_64", "linux"), ("windows-x86_64", "windows")):
                position_identity = {"registry_entry_id": identity, "case_id": case["case_id"], "platform": platform}
                position_id = _sha256(_canonical(position_identity))
                if position_id in seen_position_ids:
                    raise BehaviorCampaignError(f"duplicate behavior position: {position_identity}")
                seen_position_ids.add(position_id)

                if profile_key == "linux":
                    suite_lock = entry["dependency_locks"][platform]
                    catalog_lock = suite_profile_row.get("profiles", {}).get(platform, {})
                    profile_matches = (
                        suite_lock.get("status") == "eligible_not_run"
                        and catalog_lock.get("status") == "locked"
                        and suite_lock.get("profile_id") == catalog_lock.get("profile_id")
                        and suite_lock.get("dependency_lock_sha256") == (catalog_lock.get("dependency_lock") or {}).get("sha256")
                    )
                    selected_lock = catalog_lock
                    selected_profile_document = "catalogs/p2-41649744e057d61031d5/profiles.lock.json"
                else:
                    selected = windows_profiles.get(identity)
                    selected_lock = (selected or {}).get("profiles", {}).get(platform, {})
                    profile_matches = (
                        isinstance(selected, dict)
                        and selected.get("package") == entry.get("package")
                        and selected.get("advertised_version") == entry.get("version")
                        and selected_lock.get("status") == "locked"
                        and selected_lock.get("artifact_sha256") == entry.get("artifact_sha256")
                    )
                    selected_profile_document = windows_profile_records.get(entry["ecosystem"], {}).get("path")

                cache_check = _verified_cache_blob(selected_lock) if profile_matches else {"status": "identity_mismatch"}
                cache_status = cache_check.get("status")
                if cache_status == "verified":
                    profile_verification_counts["verified"] += 1
                elif cache_status == "not_available":
                    profile_verification_counts["not_available"] += 1
                elif cache_status == "mismatch":
                    profile_verification_counts["mismatch"] += 1
                else:
                    profile_verification_counts["missing_or_invalid"] += 1

                reason_codes: list[str] = []
                if not profile_matches:
                    reason_codes.append("profile_identity_not_resolved")
                elif cache_status != "verified":
                    reason_codes.append("locked_dependency_blob_not_verified")
                if not fixture_ready:
                    reason_codes.append(fixture_reason or "missing_executable_host_fixture")
                if platform == "windows-x86_64" and admission.get("capabilities", {}).get("catalog_plugin_claims_allowed") is not True:
                    reason_codes.append("windows_catalog_behavior_claim_not_admitted")
                if platform == "linux-x86_64" and linux_admission.get("capabilities", {}).get("claim_allowed") is not True:
                    reason_codes.append("linux_worker_claim_not_admitted")

                attempts = [
                    {
                        "attempt_number": attempt_number,
                        "status": "not_dispatched",
                        "reason_codes": reason_codes,
                        "worker_started": False,
                        "worker_cleanup": "not_applicable_before_worker_start",
                    }
                    for attempt_number in (1, 2, 3)
                ]
                positions.append({
                    "position_id": position_id,
                    **position_identity,
                    "ecosystem": entry["ecosystem"],
                    "package": entry["package"],
                    "version": entry["version"],
                    "artifact_sha256": entry["artifact_sha256"],
                    "case_class": case["class"],
                    "fixture": {
                        "status": "executable" if fixture_ready else "missing_runtime_binding",
                        "stimulus_fields": sorted(stimulus.keys()),
                        "assertion_paths": [item.get("path") for item in case.get("assertions", [])],
                    },
                    "profile": {
                        "status": "identity_and_cache_verified" if profile_matches and cache_status == "verified" else "blocked",
                        "profile_document": selected_profile_document,
                        "profile_id": selected_lock.get("profile_id") if isinstance(selected_lock, dict) else None,
                        "dependency_lock_sha256": (selected_lock.get("dependency_lock") or {}).get("sha256") if isinstance(selected_lock, dict) else None,
                        "cache_verification": cache_check,
                    },
                    "disposition": "dependency_attributed" if reason_codes else "eligible_for_execution",
                    "reason_codes": reason_codes,
                    "attempts": attempts,
                })

    positions.sort(key=lambda row: (row["registry_entry_id"], row["platform"], row["case_id"]))
    attempts = [attempt for position in positions for attempt in position["attempts"]]
    fixture_blocked = sum(position["fixture"]["status"] != "executable" for position in positions)
    profile_blocked = sum(position["profile"]["status"] != "identity_and_cache_verified" for position in positions)
    windows_admission_blocked = sum("windows_catalog_behavior_claim_not_admitted" in position["reason_codes"] for position in positions)

    sources = [
        _file_record(SUITE_PATH),
        _file_record(CATALOG_ROOT / "catalog.json"),
        _file_record(CATALOG_ROOT / "artifacts.lock.json"),
        _file_record(CATALOG_ROOT / "profiles.lock.json"),
        _file_record(P304_ADMISSION),
        _file_record(P305_CLOSURE),
        _file_record(LINUX_ADMISSION),
        _file_record(LINUX_PROBES),
        _file_record(P307_LEGACY),
        *windows_profile_records.values(),
    ]
    report = {
        "schema_version": 1,
        "kind": "phase3_p308_behavior_accounting_v1",
        "task_id": "P3-08",
        "status": "accounted_with_dispatch_blockers",
        "behavior_compatibility": "not_evaluated",
        "plugin_execution": False,
        "source_identity": {
            "suite_id": suite["suite_id"],
            "suite_canonical_sha256": _sha256(_canonical(suite)),
            "snapshot_id": suite["catalog_snapshot_id"],
            "p305_cross_platform_unique_units": closure["scope"]["cross_platform_unique_units"],
            "linux_worker_fingerprint": linux_admission["environment_fingerprint"],
            "windows_admission_scope": admission["execution_scope"],
        },
        "scope": {
            "entries": len(suite["entries"]),
            "required_cases": sum(len(entry["cases"]) for entry in suite["entries"]),
            "platforms": ["linux-x86_64", "windows-x86_64"],
            "required_positions": len(positions),
            "positions_by_platform": {
                platform: sum(position["platform"] == platform for position in positions)
                for platform in ("linux-x86_64", "windows-x86_64")
            },
            "planned_attempt_slots": len(attempts),
            "attempts_per_position": 3,
        },
        "accounting": {
            "dependency_attributed_positions": sum(position["disposition"] == "dependency_attributed" for position in positions),
            "eligible_for_execution_positions": sum(position["disposition"] == "eligible_for_execution" for position in positions),
            "missing_executable_fixture_positions": fixture_blocked,
            "unverified_profile_positions": profile_blocked,
            "windows_positions_blocked_by_admission_scope": windows_admission_blocked,
            "dispatched_attempts": 0,
            "not_dispatched_attempts": sum(attempt["status"] == "not_dispatched" for attempt in attempts),
            "worker_started_attempts": sum(attempt["worker_started"] for attempt in attempts),
            "worker_cleanup_confirmed": 0,
            "worker_cleanup_not_applicable": sum(attempt["worker_cleanup"] == "not_applicable_before_worker_start" for attempt in attempts),
            "profile_cache_checks": profile_verification_counts,
        },
        "gate": {
            "p3_behavior": "not_evaluated",
            "reason_codes": sorted({reason for position in positions for reason in position["reason_codes"]}),
        },
        "source_evidence": sources,
        "suite_validation": suite_check,
        "positions": positions,
    }
    report["canonical_sha256"] = _sha256(_canonical(report))
    return report
