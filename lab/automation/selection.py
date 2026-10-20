"""Build and validate immutable P3 recovery selections from S2 evidence."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

STAGES = ("prepare", "install", "native", "federated", "compare", "cleanup")
TERMINAL = {"passed", "failed", "skipped", "unknown"}
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ROOT = Path(__file__).resolve().parents[2]
INFRASTRUCTURE_DOMAINS = {"worker-backend", "preparation", "worker-exit", "worker-resource-limit", "worker-timeout", "report-capture", "harness", "infrastructure"}


class AutomationContractError(ValueError):
    """An automation manifest or one of its frozen source inputs is invalid."""


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def object_sha256(value: Any) -> str:
    return sha256(canonical_bytes(value))


def _strict_json(path: Path) -> tuple[dict[str, Any], bytes]:
    raw = path.read_bytes()

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise AutomationContractError(f"invalid strict JSON input {path}: {error}") from error
    if not isinstance(value, dict):
        raise AutomationContractError(f"JSON root must be an object: {path}")
    return value, raw


def _input_hash(path: Path) -> dict[str, str]:
    resolved = path.resolve(strict=True)
    return {"path": _display_path(resolved), "sha256": sha256(resolved.read_bytes())}


def _display_path(path: Path) -> str:
    try:
        display_path = path.relative_to(ROOT).as_posix()
    except ValueError:
        display_path = str(path)
    return display_path


def _identity(row: dict[str, Any], snapshot_id: str) -> dict[str, Any]:
    return {
        "snapshot_id": snapshot_id,
        "registry_entry_id": row.get("registry_entry_id"),
        "ecosystem": row.get("ecosystem"),
        "package": row.get("package"),
        "version": row.get("version"),
        "platform": row.get("platform"),
        "profile_id": row.get("profile_id"),
        "profile_lock_sha256": row.get("profile_lock_sha256"),
        "artifact_sha256": row.get("artifact_sha256"),
        "dependency_lock_sha256": row.get("dependency_lock_sha256"),
    }


def _reason_codes(row: dict[str, Any], attribution: dict[str, Any] | None) -> list[str]:
    reasons: set[str] = set()
    for stage, status in row["stage_statuses"].items():
        if status == "unknown":
            reasons.add(f"stage_unknown:{stage}")
    domain = (attribution or {}).get("attribution_domain")
    if domain in INFRASTRUCTURE_DOMAINS:
        reasons.add(f"failure_domain:{domain}")
    if (attribution or {}).get("detector_defect_confirmed") is True:
        reasons.add("confirmed_detector_defect")
    return sorted(reasons)


def build_recovery_manifest(
    snapshot_dir: Path,
    audit_path: Path,
    attribution_path: Path,
    shard_reports: Iterable[Path],
    *,
    campaign_id: str = "p3-recovery-s211",
    static_eligibility: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Recompute the S2-11 stage ledger and freeze all impacted units.

    The full source shard set is required. Any unknown stage selects its whole
    entry×OS unit, including otherwise-passing stages, because P3 recovery reruns
    install/native/cleanup as a fresh attempt rather than projecting old stages.
    """
    from ..catalog_run import _verify_snapshot_inputs

    snapshot_dir = Path(snapshot_dir).resolve(strict=True)
    snapshot_identity = _verify_snapshot_inputs(snapshot_dir)
    audit, _ = _strict_json(Path(audit_path))
    attribution_doc, _ = _strict_json(Path(attribution_path))
    snapshot_id = snapshot_identity["snapshot_id"]
    if audit.get("task_id") != "S2-12" or audit.get("source_snapshot_id") != snapshot_id:
        raise AutomationContractError("S2-12 audit snapshot identity mismatch")
    if attribution_doc.get("task_id") != "S2-11" or attribution_doc.get("source_snapshot_id") != snapshot_id:
        raise AutomationContractError("S2-11 attribution snapshot identity mismatch")
    if attribution_doc.get("source_aggregate_file_sha256") != audit.get("source_sha256", {}).get("s210_aggregate"):
        raise AutomationContractError("S2-11 attribution and S2-12 audit source aggregate mismatch")

    paths = sorted((Path(path).resolve(strict=True) for path in shard_reports), key=lambda path: str(path))
    if len(paths) != 512 or len({str(path) for path in paths}) != 512:
        raise AutomationContractError("recovery selection requires exactly 512 unique S2-11 shard reports")
    shard_rows: dict[str, dict[str, Any]] = {}
    result_sources: dict[str, tuple[Path, str]] = {}
    seen_shards: set[tuple[str, int]] = set()
    for path in paths:
        report, _ = _strict_json(path)
        if report.get("kind") != "phase2_s210_census_shard" or report.get("snapshot_id") != snapshot_id:
            raise AutomationContractError(f"unexpected shard report identity: {path}")
        platform, shard = report.get("platform"), report.get("shard", {})
        key = (platform, shard.get("index"))
        if platform not in {"linux", "windows"} or shard.get("count") != 256 or key in seen_shards:
            raise AutomationContractError(f"duplicate or invalid shard identity: {path}")
        seen_shards.add(key)
        if report.get("status") != "completed" or report.get("denominator", {}).get("all_selected_stages_terminal") is not True:
            raise AutomationContractError(f"non-terminal source shard: {path}")
        if report.get("source_hashes", {}).get("catalog_sha256") != sha256((snapshot_dir / "catalog.json").read_bytes()):
            raise AutomationContractError(f"source catalog hash mismatch: {path}")
        for row in report.get("plugin_reports", []):
            result_id = row.get("result_id")
            if not isinstance(result_id, str) or not result_id or result_id in shard_rows:
                raise AutomationContractError(f"missing or duplicate result identity in {path}")
            statuses = row.get("stage_statuses")
            hashes = row.get("stage_result_sha256")
            if not isinstance(statuses, dict) or set(statuses) != set(STAGES) or not isinstance(hashes, dict) or set(hashes) != set(STAGES):
                raise AutomationContractError(f"stage ledger is incomplete: {result_id}")
            if any(statuses[stage] not in TERMINAL for stage in STAGES) or any(not isinstance(hashes[stage], str) or not SHA256.fullmatch(hashes[stage]) for stage in STAGES):
                raise AutomationContractError(f"stage ledger contains invalid status or digest: {result_id}")
            shard_rows[result_id] = row
            result_sources[result_id] = (path, sha256(path.read_bytes()))
    if seen_shards != {(platform, index) for platform in ("linux", "windows") for index in range(256)}:
        raise AutomationContractError("S2-11 source shard set has a missing shard")
    if len(shard_rows) != 11380:
        raise AutomationContractError(f"S2-11 source denominator mismatch: expected 11380 units, found {len(shard_rows)}")

    attribution: dict[str, dict[str, Any]] = {}
    for item in attribution_doc.get("results", []):
        result_id = item.get("result_id")
        if result_id in attribution or result_id not in shard_rows:
            raise AutomationContractError("attribution ledger has duplicate or unexpected result identity")
        attribution[result_id] = item

    selected: list[dict[str, Any]] = []
    static_rows: list[dict[str, Any]] = []
    input_hashes = [_input_hash(Path(audit_path)), _input_hash(Path(attribution_path))]
    input_hashes.extend(_input_hash(path) for path in paths)
    for result_id, row in sorted(shard_rows.items()):
        attr = attribution.get(result_id)
        reasons = _reason_codes(row, attr)
        explicit_static = (static_eligibility or {}).get(result_id)
        profile_status, artifact_status = row.get("profile_status"), row.get("artifact_status")
        if explicit_static or profile_status != "locked" or artifact_status != "locked":
            static_rows.append({
                "result_id": result_id,
                "identity": _identity(row, snapshot_id),
                "identity_sha256": object_sha256(_identity(row, snapshot_id)),
                "status": "skipped",
                "reason_code": (explicit_static or {}).get("reason_code") or row.get("profile_reason_code") or row.get("artifact_reason_code") or "catalog_profile_not_locked",
                "source_report_path": _display_path(result_sources[result_id][0]),
                "source_report_sha256": result_sources[result_id][1],
            })
            continue
        if not reasons:
            continue
        identity = _identity(row, snapshot_id)
        selected.append({
            "result_id": result_id,
            "identity": identity,
            "identity_sha256": object_sha256(identity),
            "selection_reasons": reasons,
            "source": {
                "task_id": "S2-11",
                "shard_report_path": _display_path(result_sources[result_id][0]),
                "shard_report_sha256": result_sources[result_id][1],
                "unit_status": row.get("unit_status"),
                "attribution_domain": (attr or {}).get("attribution_domain"),
                "stage_statuses": {stage: row["stage_statuses"][stage] for stage in STAGES},
                "stage_result_sha256": {stage: row["stage_result_sha256"][stage] for stage in STAGES},
            },
            "required_stages": list(STAGES),
        })

    selected_ids = [row["result_id"] for row in selected]
    static_ids = [row["result_id"] for row in static_rows]
    if len(set(selected_ids)) != len(selected_ids) or len(set(static_ids)) != len(static_ids) or set(selected_ids) & set(static_ids):
        raise AutomationContractError("selection ledger contains duplicate or overlapping result identities")
    expected_s2_counts = audit.get("s211_full_rerun", {}).get("unit_counts", {})
    actual_counts = Counter(row.get("unit_status") for row in shard_rows.values())
    if actual_counts != Counter({key: value for key, value in expected_s2_counts.items() if value}):
        raise AutomationContractError("recomputed S2 unit counts do not match audited denominator")
    manifest = {
        "schema_version": 1,
        "contract": "ligamina-automation-manifest-v1",
        "campaign_id": campaign_id,
        "purpose": "recovery",
        "snapshot_id": snapshot_id,
        "source_identity": {
            "s2_audit_sha256": sha256(Path(audit_path).read_bytes()),
            "s2_attribution_sha256": sha256(Path(attribution_path).read_bytes()),
            "catalog_sha256": sha256((snapshot_dir / "catalog.json").read_bytes()),
            "artifact_lock_sha256": sha256((snapshot_dir / "artifacts.lock.json").read_bytes()),
            "profile_lock_sha256": sha256((snapshot_dir / "profiles.lock.json").read_bytes()),
        },
        "expected": {"source_units": 11380, "selected_units": len(selected), "static_skipped_units": len(static_rows), "selected_stage_rows": len(selected) * len(STAGES)},
        "selection": selected,
        "static_skipped": static_rows,
        "source_files": input_hashes,
        "gate": {
            "id": "P3-RECOVERY",
            "required_stages": ["install", "native", "cleanup"],
            "pass_statuses": ["passed", "failed", "skipped"],
            "deny_statuses": ["unknown"],
            "require_cleanup_confirmed": True,
            "scope_hash": "",
        },
    }
    manifest["gate"]["scope_hash"] = object_sha256({"selected": selected_ids, "static_skipped": static_ids, "gate": {key: value for key, value in manifest["gate"].items() if key != "scope_hash"}})
    manifest["manifest_sha256"] = object_sha256({key: value for key, value in manifest.items() if key != "manifest_sha256"})
    validate_manifest(manifest, verify_source_files=True)
    return manifest


def validate_manifest(manifest: dict[str, Any], *, verify_source_files: bool = False) -> dict[str, Any]:
    """Recompute counts, identities and hashes; reject gaps, duplicates and swaps."""
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1 or manifest.get("contract") != "ligamina-automation-manifest-v1":
        raise AutomationContractError("unsupported automation manifest contract")
    if manifest.get("purpose") not in {"recovery", "behavior", "full"}:
        raise AutomationContractError("invalid campaign purpose")
    if not isinstance(manifest.get("campaign_id"), str) or not manifest["campaign_id"] or not isinstance(manifest.get("snapshot_id"), str):
        raise AutomationContractError("campaign and snapshot identities are required")
    selection, static_rows = manifest.get("selection"), manifest.get("static_skipped")
    if not isinstance(selection, list) or not isinstance(static_rows, list):
        raise AutomationContractError("selection and static_skipped must be arrays")
    all_ids = [item.get("result_id") for item in selection + static_rows if isinstance(item, dict)]
    if len(all_ids) != len(selection) + len(static_rows) or len(set(all_ids)) != len(all_ids):
        raise AutomationContractError("missing, duplicate, or overlapping ledger identities")
    for item in selection + static_rows:
        identity = item.get("identity")
        if not isinstance(identity, dict) or identity.get("snapshot_id") != manifest["snapshot_id"]:
            raise AutomationContractError("entry identity belongs to another snapshot")
        if item.get("result_id") != f"catalog:{identity.get('registry_entry_id')}|{identity.get('platform')}":
            raise AutomationContractError("result identity does not match entry/platform")
        if item.get("identity_sha256") != object_sha256(identity):
            raise AutomationContractError("entry/profile/artifact identity hash mismatch")
        if item in selection and item.get("required_stages") != list(STAGES):
            raise AutomationContractError("selected recovery unit must account for every stage")
    for left_index, left in enumerate(selection):
        for right in selection[left_index + 1 :]:
            if left["identity"]["registry_entry_id"] == right["identity"]["registry_entry_id"] and left["identity"]["platform"] == right["identity"]["platform"]:
                if left["identity"] != right["identity"]:
                    raise AutomationContractError("one entry×OS unit has conflicting profile/artifact identities")
    expected = manifest.get("expected", {})
    if expected.get("selected_units") != len(selection) or expected.get("static_skipped_units") != len(static_rows):
        raise AutomationContractError("manifest selection denominator mismatch")
    stage_rows = sum(len(item.get("required_stages", [])) for item in selection)
    if expected.get("selected_stage_rows") != stage_rows:
        raise AutomationContractError("manifest selected-stage denominator mismatch")
    source_files = manifest.get("source_files")
    if not isinstance(source_files, list) or any(not isinstance(item, dict) or not SHA256.fullmatch(str(item.get("sha256", ""))) for item in source_files):
        raise AutomationContractError("manifest source file hashes are malformed")
    if len({item["path"] for item in source_files}) != len(source_files):
        raise AutomationContractError("manifest source file set contains duplicates")
    if verify_source_files:
        for item in source_files:
            path = Path(item["path"])
            if not path.is_absolute():
                path = ROOT / path
            if not path.is_file() or sha256(path.read_bytes()) != item["sha256"]:
                raise AutomationContractError(f"source evidence hash changed or is unavailable: {path}")
    gate = manifest.get("gate")
    if not isinstance(gate, dict) or not SHA256.fullmatch(str(gate.get("scope_hash", ""))):
        raise AutomationContractError("gate scope hash is missing")
    expected_scope = object_sha256({
        "selected": [item["result_id"] for item in selection],
        "static_skipped": [item["result_id"] for item in static_rows],
        "gate": {key: value for key, value in gate.items() if key != "scope_hash"},
    })
    if gate["scope_hash"] != expected_scope:
        raise AutomationContractError("gate scope hash mismatch")
    digest = object_sha256({key: value for key, value in manifest.items() if key != "manifest_sha256"})
    if manifest.get("manifest_sha256") != digest:
        raise AutomationContractError("manifest SHA-256 mismatch")
    return {"status": "valid", "manifest_sha256": digest, "selected_units": len(selection), "static_skipped_units": len(static_rows), "selected_stage_rows": stage_rows}
