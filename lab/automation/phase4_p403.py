"""P4-03 exact-parent unknown recovery planning and Linux campaign binding."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from lab.automation.evidence_hash import matches_text_sha256
from lab.automation.recovery import (
    RecoveryPilotError,
    _build_core_only_host_service_profiles,
    build_linux_pilot_manifest,
    execute_linux_recovery_pilot,
    validate_linux_pilot_manifest,
)
from lab.automation.reconcile import DockerReconciler
from lab.automation.selection import ROOT, object_sha256
from lab.queue import StageQueue


class Phase4P403Error(ValueError):
    """A P4-03 manifest or its parent evidence failed validation."""


def _json_load(path: Path) -> dict[str, Any]:
    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(
            Path(path).read_text(encoding="utf-8"),
            object_pairs_hook=unique_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise Phase4P403Error(f"invalid P4-03 JSON input {path}: {error}") from error
    if not isinstance(value, dict):
        raise Phase4P403Error(f"P4-03 JSON root must be an object: {path}")
    return value


def _file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError as error:
        raise Phase4P403Error(f"required P4-03 evidence file is unavailable: {path}") from error


def _text_file_sha256_matches(path: Path, expected: str) -> bool:
    try:
        return matches_text_sha256(Path(path).read_bytes(), expected)
    except OSError as error:
        raise Phase4P403Error(f"required P4-03 evidence file is unavailable: {path}") from error


def _relative(path_value: str) -> Path:
    path = Path(path_value)
    return path if path.is_absolute() else ROOT / path


def _verified_p3_linux_worker_images(parent: dict[str, Any]) -> tuple[dict[str, str], dict[str, Any]]:
    sources = [source for source in parent.get("sources", []) if source.get("cohort") == "linux"]
    if len(sources) != 1:
        raise Phase4P403Error("P4-03 must bind exactly one P3 Linux recovery source")
    source = sources[0]

    def verified_repo_file(path_value: Any, expected_sha256: Any, label: str) -> tuple[Path, str]:
        if not isinstance(path_value, str) or not path_value:
            raise Phase4P403Error(f"P3 Linux {label} path is missing")
        if not isinstance(expected_sha256, str) or len(expected_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in expected_sha256
        ):
            raise Phase4P403Error(f"P3 Linux {label} SHA-256 is invalid")
        path = _relative(path_value).resolve(strict=True)
        try:
            relative_path = path.relative_to(ROOT.resolve(strict=True)).as_posix()
        except ValueError as error:
            raise Phase4P403Error(f"P3 Linux {label} escapes the repository") from error
        if not _text_file_sha256_matches(path, expected_sha256):
            raise Phase4P403Error(f"P3 Linux {label} SHA-256 mismatch: {relative_path}")
        return path, relative_path

    manifest_path, manifest_relative = verified_repo_file(
        source.get("manifest_path"), source.get("manifest_file_sha256"), "worker image manifest",
    )
    report_path, report_relative = verified_repo_file(
        source.get("report_path"), source.get("report_file_sha256"), "worker image report",
    )
    source_manifest = _json_load(manifest_path)
    source_report = _json_load(report_path)
    manifest_sha256 = source.get("manifest_canonical_sha256")
    if (
        not isinstance(manifest_sha256, str)
        or source_manifest.get("manifest_sha256") != manifest_sha256
        or source_report.get("manifest_sha256") != manifest_sha256
        or source_report.get("campaign_id") != source_manifest.get("campaign_id")
        or source_report.get("status") != "completed"
    ):
        raise Phase4P403Error("P3 Linux recovery source report is not bound to its completed manifest")

    worker_images = source_manifest.get("worker_images")
    if not isinstance(worker_images, dict) or set(worker_images) != {"nonebot", "koishi"}:
        raise Phase4P403Error("P3 Linux recovery manifest must pin both Worker images")
    inventory = source_report.get("worker_image_inventory")
    if not isinstance(inventory, list):
        raise Phase4P403Error("P3 Linux recovery report has no Worker image inventory")
    inventory_by_ecosystem = {
        item.get("ecosystem"): item for item in inventory if isinstance(item, dict)
    }
    if len(inventory_by_ecosystem) != len(inventory) or set(inventory_by_ecosystem) != set(worker_images):
        raise Phase4P403Error("P3 Linux recovery Worker image inventory is incomplete or duplicated")
    for ecosystem, reference in worker_images.items():
        if not isinstance(reference, str) or "@sha256:" not in reference:
            raise Phase4P403Error(f"P3 Linux {ecosystem} Worker image is not digest pinned")
        digest = reference.rsplit("@sha256:", 1)[1]
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise Phase4P403Error(f"P3 Linux {ecosystem} Worker image digest is invalid")
        image = inventory_by_ecosystem[ecosystem]
        if (
            image.get("status") != "present"
            or image.get("reference") != reference
            or image.get("image_id") != f"sha256:{digest}"
        ):
            raise Phase4P403Error(f"P3 Linux {ecosystem} Worker image inventory does not match its pinned digest")

    binding = {
        "manifest_path": manifest_relative,
        "manifest_file_sha256": source["manifest_file_sha256"],
        "manifest_sha256": manifest_sha256,
        "report_path": report_relative,
        "report_file_sha256": source["report_file_sha256"],
        "campaign_id": source_report["campaign_id"],
    }
    return dict(worker_images), binding


def _closure_sources(closure: dict[str, Any]) -> dict[str, dict[str, Any]]:
    campaign_for = {"linux": "linux", "windows_pilot": "pilot", "windows_preparation": "nonebot"}
    result: dict[str, dict[str, Any]] = {}
    campaigns = closure.get("campaigns", {})
    for cohort, campaign_name in campaign_for.items():
        campaign = campaigns.get(campaign_name, {})
        report = campaign.get("report", {})
        manifest = campaign.get("manifest", {})
        result[cohort] = {
            "cohort": cohort,
            "report_path": report.get("path"),
            "report_file_sha256": report.get("file_sha256"),
            "manifest_path": manifest.get("path"),
            "manifest_file_sha256": manifest.get("file_sha256"),
            "manifest_canonical_sha256": manifest.get("canonical_sha256"),
            "profile_lock_path": manifest.get("profile_lock_path"),
            "profile_lock_sha256": manifest.get("profile_lock_sha256"),
        }
    return result


def _expected_unknown_rows(cohort: str, report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    if cohort == "linux":
        candidates = [
            row for row in report.get("plugin_reports", [])
            if "unknown" in row.get("old_to_new", {}).get("new_stage_statuses", {}).values()
        ]
        for row in candidates:
            unknown_attempts = [attempt for attempt in row.get("attempts", []) if attempt.get("status") == "unknown"]
            expected = {
                "report_row_sha256": object_sha256(row),
                "identity_sha256": row.get("identity_sha256"),
                "package": row.get("package"),
                "version": row.get("version"),
                "reason_code": row.get("source", {}).get("reason_code"),
                "attempts": [
                    {
                        "attempt_id": attempt.get("attempt_id"),
                        "unit_id": attempt.get("unit_id"),
                        "attempt_record_sha256": object_sha256(attempt),
                        "reason_code": attempt.get("result", {}).get("reason_code"),
                    }
                    for attempt in unknown_attempts
                ],
                "stage_statuses": row.get("old_to_new", {}).get("new_stage_statuses", {}),
                "source_report_path": row.get("source", {}).get("report_path"),
                "source_report_sha256": row.get("source", {}).get("report_sha256"),
                "source_result_id": row.get("source", {}).get("result_id"),
            }
            result[row["registry_entry_id"]] = expected
    else:
        for item in report.get("results", []):
            row = item.get("result", {})
            if row.get("status") != "unknown":
                continue
            entry_id = row.get("registry_entry_id")
            identity_sha = item.get("selection_identity_sha256") or item.get("source_identity_sha256")
            result[entry_id] = {
                "report_row_sha256": object_sha256(item),
                "identity_sha256": identity_sha,
                "package": row.get("package"),
                "version": row.get("version"),
                "reason_code": row.get("reason_code"),
                "attempt_id": row.get("result_id"),
                "attempted": row.get("attempted"),
                "profile_lock_sha256": item.get("probe_profile", {}).get("profile_lock_sha256"),
            }
    if any(not entry_id for entry_id in result):
        raise Phase4P403Error(f"{cohort} parent report contains an unknown row without registry_entry_id")
    return result


def validate_p403_recovery_manifest(manifest_path: Path, *, verify_sources: bool = True) -> dict[str, Any]:
    """Validate exact 39/10/4 coverage against the immutable P3-05 parent rows."""
    manifest_path = Path(manifest_path).expanduser().resolve(strict=True)
    manifest = _json_load(manifest_path)
    if manifest.get("kind") != "phase4_p403_unknown_recovery_manifest" or manifest.get("task_id") != "P4-03":
        raise Phase4P403Error("not a P4-03 unknown recovery manifest")
    if manifest.get("status") != "prepared_not_dispatched":
        raise Phase4P403Error("P4-03 parent manifest must remain in its prepared state")
    expected_manifest_sha = object_sha256({key: value for key, value in manifest.items() if key != "manifest_sha256"})
    if manifest.get("manifest_sha256") != expected_manifest_sha:
        raise Phase4P403Error("P4-03 manifest self hash mismatch")
    sidecar = manifest_path.with_suffix(manifest_path.suffix + ".sha256")
    if sidecar.is_file():
        sidecar_value = sidecar.read_text(encoding="ascii").split()[0]
        if sidecar_value != _file_sha256(manifest_path):
            raise Phase4P403Error("P4-03 manifest file SHA-256 sidecar mismatch")

    closure_ref = manifest.get("parent_closure", {})
    closure_path = _relative(closure_ref.get("path", ""))
    if verify_sources:
        if not _text_file_sha256_matches(closure_path, closure_ref.get("file_sha256", "")):
            raise Phase4P403Error("P3-05 closure file hash differs from the P4-03 manifest")
    closure = _json_load(closure_path)
    if closure.get("report_sha256") != closure_ref.get("report_sha256"):
        raise Phase4P403Error("P3-05 closure canonical report hash differs from the P4-03 manifest")

    source_specs = manifest.get("sources")
    if not isinstance(source_specs, list) or len(source_specs) != 3:
        raise Phase4P403Error("P4-03 must bind exactly three P3-05 parent cohorts")
    expected_sources = _closure_sources(closure)
    source_by_cohort = {item.get("cohort"): item for item in source_specs if isinstance(item, dict)}
    if set(source_by_cohort) != set(expected_sources) or len(source_by_cohort) != len(source_specs):
        raise Phase4P403Error("P4-03 source cohort identities are incomplete or duplicated")
    parent_rows: dict[str, tuple[str, dict[str, Any]]] = {}
    for cohort, expected_source in expected_sources.items():
        source_spec = source_by_cohort[cohort]
        for key, value in expected_source.items():
            if source_spec.get(key) != value:
                raise Phase4P403Error(f"P4-03 {cohort} source binding differs from P3-05 closure: {key}")
        report_path = _relative(source_spec["report_path"])
        if verify_sources:
            if not _text_file_sha256_matches(report_path, source_spec["report_file_sha256"]):
                raise Phase4P403Error(f"P4-03 {cohort} report SHA-256 mismatch")
            manifest_file = _relative(source_spec["manifest_path"])
            if not _text_file_sha256_matches(manifest_file, source_spec["manifest_file_sha256"]):
                raise Phase4P403Error(f"P4-03 {cohort} frozen manifest SHA-256 mismatch")
            if source_spec.get("profile_lock_path"):
                if not _text_file_sha256_matches(
                    _relative(source_spec["profile_lock_path"]),
                    source_spec.get("profile_lock_sha256", ""),
                ):
                    raise Phase4P403Error(f"P4-03 {cohort} profile lock SHA-256 mismatch")
        parent_report = _json_load(report_path)
        expected = _expected_unknown_rows(cohort, parent_report)
        for entry_id, row in expected.items():
            if entry_id in parent_rows:
                raise Phase4P403Error(f"P4-03 parent registry identity is duplicated: {entry_id}")
            parent_rows[entry_id] = (cohort, row)

    rows = manifest.get("rows")
    if not isinstance(rows, list) or len(rows) != 53:
        raise Phase4P403Error("P4-03 must contain exactly 53 parent identities")
    seen: set[str] = set()
    counts: Counter[str] = Counter()
    for row in rows:
        if not isinstance(row, dict):
            raise Phase4P403Error("P4-03 rows must be objects")
        entry_id = row.get("registry_entry_id")
        if not isinstance(entry_id, str) or entry_id in seen:
            raise Phase4P403Error("P4-03 contains a missing or duplicate registry identity")
        seen.add(entry_id)
        cohort, expected = parent_rows.get(entry_id, (None, None))
        if cohort is None or row.get("cohort") != cohort:
            raise Phase4P403Error(f"P4-03 row does not match a P3-05 unknown parent: {entry_id}")
        parent = row.get("parent", {})
        historical = row.get("historical_outcome", {})
        source = source_by_cohort[cohort]
        if (
            parent.get("report_path") != source["report_path"]
            or parent.get("report_file_sha256") != source["report_file_sha256"]
            or parent.get("report_row_sha256") != expected["report_row_sha256"]
            or row.get("identity_sha256") != expected["identity_sha256"]
            or row.get("package") != expected["package"]
            or row.get("version") != expected["version"]
            or historical.get("status") != "unknown"
            or historical.get("reason_code") != expected["reason_code"]
        ):
            raise Phase4P403Error(f"P4-03 parent lineage or historical unknown changed: {entry_id}")
        if cohort == "linux":
            listed_attempts = parent.get("parent_unknown_attempts", [])
            if [item.get("attempt_id") for item in listed_attempts] != [item.get("attempt_id") for item in expected["attempts"]]:
                raise Phase4P403Error(f"P4-03 Linux unknown attempt identities changed: {entry_id}")
            for listed, frozen in zip(listed_attempts, expected["attempts"]):
                if listed.get("attempt_record_sha256") != frozen["attempt_record_sha256"] or listed.get("reason_code") != frozen["reason_code"]:
                    raise Phase4P403Error(f"P4-03 Linux unknown attempt evidence hash changed: {entry_id}")
            if historical.get("stage_statuses") != expected["stage_statuses"]:
                raise Phase4P403Error(f"P4-03 Linux unknown stage projection changed: {entry_id}")
            if expected["source_report_path"]:
                original_source = _relative(expected["source_report_path"])
                if verify_sources and _file_sha256(original_source) != expected["source_report_sha256"]:
                    raise Phase4P403Error(f"P4-03 Linux original S2-11 source hash mismatch: {entry_id}")
                if parent.get("source_report_path") != expected["source_report_path"] or parent.get("source_report_sha256") != expected["source_report_sha256"]:
                    raise Phase4P403Error(f"P4-03 Linux original S2-11 source lineage changed: {entry_id}")
        else:
            if parent.get("parent_attempt_id") != expected["attempt_id"] or historical.get("attempted") != expected["attempted"]:
                raise Phase4P403Error(f"P4-03 Windows parent attempt changed: {entry_id}")
            if cohort == "windows_pilot" and parent.get("profile_lock_sha256") != expected["profile_lock_sha256"]:
                raise Phase4P403Error(f"P4-03 Windows pilot lock identity changed: {entry_id}")
        recovery = row.get("recovery", {})
        retry_budget = recovery.get("retry_budget")
        expected_retry_budget = 1 if cohort == "linux" else 0
        if (
            recovery.get("status") != "not_attempted"
            or not isinstance(retry_budget, int)
            or retry_budget != expected_retry_budget
        ):
            raise Phase4P403Error(f"P4-03 retry budget/status invalid for {entry_id}")
        counts[cohort] += 1

    expected_counts = {"linux": 39, "windows_pilot": 10, "windows_preparation": 4}
    if seen != set(parent_rows) or dict(counts) != expected_counts:
        raise Phase4P403Error("P4-03 parent identity coverage must be exactly 39/10/4 with no omissions")
    if manifest.get("denominator") != {"total": 53, "by_cohort": expected_counts}:
        raise Phase4P403Error("P4-03 denominator does not match the frozen parent identities")
    return {
        "status": "valid",
        "manifest_sha256": expected_manifest_sha,
        "manifest_file_sha256": _file_sha256(manifest_path),
        "total": len(rows),
        "by_cohort": expected_counts,
        "linux_unknown_attempts": sum(len(row.get("parent", {}).get("parent_unknown_attempts", [])) for row in rows if row.get("cohort") == "linux"),
    }


def build_p403_linux_executor_manifest(
    parent_manifest_path: Path,
    *,
    snapshot_dir: Path = ROOT / "catalogs/p2-41649744e057d61031d5",
    reports_dir: Path = ROOT / "outputs/acceptance",
    worker_policy_id: str = "s205-locked-profile-images-v1",
    worker_timeout_seconds: int = 240,
) -> dict[str, Any]:
    """Bind the Linux execution manifest to exactly the 39 parent unknown identities."""
    parent_manifest_path = Path(parent_manifest_path).expanduser().resolve(strict=True)
    parent = _json_load(parent_manifest_path)
    validation = validate_p403_recovery_manifest(parent_manifest_path)
    closure = _json_load(_relative(parent["parent_closure"]["path"]))
    source_info = closure.get("windows_scope_reconciliation", {})
    source_manifest_path = _relative(source_info.get("source_manifest_path", ""))
    if _file_sha256(source_manifest_path) != source_info.get("source_manifest_file_sha256"):
        raise Phase4P403Error("P3-01 recovery source manifest hash mismatch")
    worker_images, worker_image_source = _verified_p3_linux_worker_images(parent)
    base = build_linux_pilot_manifest(
        source_manifest_path,
        snapshot_dir=snapshot_dir,
        reports_dir=reports_dir,
        worker_policy_id=worker_policy_id,
        worker_images=worker_images,
        worker_timeout_seconds=worker_timeout_seconds,
        probe_semantics_version=3,
        selection_scope="affected-linux",
    )
    parent_linux = {row["registry_entry_id"]: row for row in parent["rows"] if row.get("cohort") == "linux"}
    selected = []
    base_by_id = {row["identity"]["registry_entry_id"]: row for row in base["selection"]}
    if set(parent_linux) - set(base_by_id):
        missing = sorted(set(parent_linux) - set(base_by_id))
        raise Phase4P403Error(f"P4-03 parent unknowns are not locked Linux recovery identities: {missing}")
    for entry_id in sorted(parent_linux):
        parent_row = parent_linux[entry_id]
        source_row = base_by_id[entry_id]
        if source_row.get("identity_sha256") != parent_row.get("identity_sha256"):
            raise Phase4P403Error(f"P4-03 Linux identity no longer matches the frozen P3 snapshot: {entry_id}")
        bound = dict(source_row)
        bound["phase4_parent"] = {
            "recovery_id": parent_row["recovery_id"],
            "manifest_sha256": parent.get("manifest_sha256"),
            "report_row_sha256": parent_row["parent"]["report_row_sha256"],
            "parent_unknown_attempt_ids": [attempt["attempt_id"] for attempt in parent_row["parent"]["parent_unknown_attempts"]],
            "retry_budget": parent_row["recovery"]["retry_budget"],
        }
        selected.append(bound)
    counts = Counter(row["identity"]["ecosystem"] for row in selected)
    core = {key: value for key, value in base.items() if key not in {"campaign_id", "manifest_sha256"}}
    core.update({
        "expected_units": len(selected),
        "recovery_per_ecosystem": {ecosystem: counts[ecosystem] for ecosystem in ("nonebot", "koishi")},
        "failed_controls_per_ecosystem": {"nonebot": 0, "koishi": 0},
        "selection_algorithm": "P4-03 exact intersection with the 39 P3-05 Linux unknown parent identities; no replacement",
        "selection_scope": "p301-all-affected-linux-v1",
        "selection": selected,
        "host_service_profiles": _build_core_only_host_service_profiles(selected),
        "phase4_parent_manifest": {
            "path": parent_manifest_path.relative_to(ROOT).as_posix(),
            "file_sha256": validation["manifest_file_sha256"],
            "manifest_sha256": validation["manifest_sha256"],
            "closure_file_sha256": parent["parent_closure"]["file_sha256"],
            "expected_linux_parent_count": 39,
        },
        "worker_image_source": worker_image_source,
    })
    seed = object_sha256(core)
    executor_manifest = {"campaign_id": f"p4-p403-linux-{seed[:12]}", **core}
    executor_manifest["manifest_sha256"] = object_sha256(executor_manifest)
    try:
        validate_linux_pilot_manifest(executor_manifest)
    except RecoveryPilotError as error:
        raise Phase4P403Error(f"generated P4-03 Linux execution manifest is invalid: {error}") from error
    return executor_manifest


def validate_p403_linux_executor_manifest(parent_manifest_path: Path, executor_manifest_path: Path) -> dict[str, Any]:
    parent_manifest_path = Path(parent_manifest_path).expanduser().resolve(strict=True)
    executor_manifest_path = Path(executor_manifest_path).expanduser().resolve(strict=True)
    parent = _json_load(parent_manifest_path)
    parent_validation = validate_p403_recovery_manifest(parent_manifest_path)
    executor = _json_load(executor_manifest_path)
    try:
        low_level = validate_linux_pilot_manifest(executor)
    except RecoveryPilotError as error:
        raise Phase4P403Error(f"invalid P4-03 Linux executor manifest: {error}") from error
    worker_images, worker_image_source = _verified_p3_linux_worker_images(parent)
    if executor.get("worker_images") != worker_images or executor.get("worker_image_source") != worker_image_source:
        raise Phase4P403Error("P4-03 Linux executor Worker images are not bound to the verified P3 source run")
    parent_binding = executor.get("phase4_parent_manifest", {})
    if (
        parent_binding.get("file_sha256") != parent_validation["manifest_file_sha256"]
        or parent_binding.get("manifest_sha256") != parent_validation["manifest_sha256"]
        or parent_binding.get("closure_file_sha256") != parent.get("parent_closure", {}).get("file_sha256")
        or parent_binding.get("expected_linux_parent_count") != 39
    ):
        raise Phase4P403Error("P4-03 Linux executor manifest is not bound to the current parent manifest")
    parents = {row["registry_entry_id"]: row for row in parent["rows"] if row.get("cohort") == "linux"}
    selected = executor.get("selection", [])
    if len(selected) != 39 or {item["identity"]["registry_entry_id"] for item in selected} != set(parents):
        raise Phase4P403Error("P4-03 Linux executor selection is not the exact 39 parent identities")
    for item in selected:
        entry_id = item["identity"]["registry_entry_id"]
        phase4_parent = item.get("phase4_parent", {})
        parent_row = parents[entry_id]
        if (
            item.get("identity_sha256") != parent_row.get("identity_sha256")
            or phase4_parent.get("recovery_id") != parent_row.get("recovery_id")
            or phase4_parent.get("manifest_sha256") != parent_validation["manifest_sha256"]
            or phase4_parent.get("report_row_sha256") != parent_row["parent"].get("report_row_sha256")
            or phase4_parent.get("parent_unknown_attempt_ids") != [a["attempt_id"] for a in parent_row["parent"]["parent_unknown_attempts"]]
            or phase4_parent.get("retry_budget") != parent_row["recovery"].get("retry_budget")
        ):
            raise Phase4P403Error(f"P4-03 Linux executor row lost its parent identity: {entry_id}")
    return {
        "status": "valid",
        "parent_manifest_sha256": parent_validation["manifest_sha256"],
        "executor_manifest_sha256": low_level["manifest_sha256"],
        "selected_units": 39,
        "ecosystem_counts": low_level["counts"],
    }


def execute_p403_linux_recovery(
    parent_manifest_path: Path,
    executor_manifest_path: Path,
    admission_path: Path,
    *,
    snapshot_dir: Path = ROOT / "catalogs/p2-41649744e057d61031d5",
    cache_root: Path,
    evidence_root: Path,
    queue_root: Path,
    timeout_seconds: int | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    binding = validate_p403_linux_executor_manifest(parent_manifest_path, executor_manifest_path)
    executor = _json_load(executor_manifest_path)
    report = execute_linux_recovery_pilot(
        executor_manifest_path,
        admission_path,
        snapshot_dir=snapshot_dir,
        cache_root=cache_root,
        evidence_root=evidence_root,
        queue_root=queue_root,
        timeout_seconds=timeout_seconds,
        now=now,
    )
    parent = _json_load(parent_manifest_path)
    parents = {row["registry_entry_id"]: row for row in parent["rows"] if row.get("cohort") == "linux"}
    for row in report.get("plugin_reports", []):
        parent_row = parents.get(row.get("registry_entry_id"))
        if parent_row:
            row["p4_parent"] = {
                "recovery_id": parent_row["recovery_id"],
                "historical_status": parent_row["historical_outcome"]["status"],
                "historical_reason_code": parent_row["historical_outcome"]["reason_code"],
                "parent_report_row_sha256": parent_row["parent"]["report_row_sha256"],
                "parent_unknown_attempt_ids": [attempt["attempt_id"] for attempt in parent_row["parent"]["parent_unknown_attempts"]],
            }
    report.update({
        "kind": "phase4_p403_linux_unknown_recovery",
        "phase4_parent_manifest_sha256": binding["parent_manifest_sha256"],
        "phase4_executor_manifest_sha256": binding["executor_manifest_sha256"],
        "parent_unknown_count": 39,
        "windows_parent_unknowns_not_dispatched": 14,
        "automation_gate": "blocked_pending_linux_results_windows_admission_and_runner_acceptance",
    })
    report["report_sha256"] = object_sha256(report)
    return report


def reconcile_p403_linux_runner_state(
    parent_manifest_path: Path,
    executor_manifest_path: Path,
    queue_root: Path,
) -> dict[str, Any]:
    """Inspect/remove only Docker resources carrying this P4-03 campaign's exact labels."""
    binding = validate_p403_linux_executor_manifest(parent_manifest_path, executor_manifest_path)
    executor = _json_load(executor_manifest_path)
    campaign_id = executor["campaign_id"]
    queue_root = Path(queue_root).expanduser().resolve()
    queue_path = (queue_root / campaign_id / "linux" / "queue.sqlite3").resolve()
    if not queue_path.is_relative_to(queue_root):
        raise Phase4P403Error("P4-03 queue path escapes the requested external queue root")
    if not queue_path.is_file():
        report = {
            "schema_version": 1,
            "kind": "phase4_p403_linux_outer_cleanup",
            "status": "not_started",
            "campaign_id": campaign_id,
            "parent_manifest_sha256": binding["parent_manifest_sha256"],
            "executor_manifest_sha256": binding["executor_manifest_sha256"],
            "queue_path": queue_path.as_posix(),
            "attempts_reconciled": 0,
            "cleanup_confirmed": None,
            "detail": "No durable queue exists, so the P4-03 campaign did not claim a Worker attempt.",
        }
        report["report_sha256"] = object_sha256(report)
        return report

    queue = StageQueue(queue_path)
    run = queue.run(campaign_id)
    if run.get("state") == "running":
        queue.interrupt_run(campaign_id)
    attempts = queue.list_attempts(campaign_id)
    reconciler = DockerReconciler()
    outcomes = []
    for attempt in attempts:
        try:
            job = queue.get_job(campaign_id, attempt["unit_id"])
            evidence = reconciler.reconcile(
                campaign_id=campaign_id,
                job_id=job["job_id"],
                attempt_id=attempt["attempt_id"],
                generation=attempt["attempt_number"],
            ).as_evidence()
        except Exception as error:  # Convert backend errors to explicit incomplete cleanup evidence.
            evidence = {
                "cleanup_confirmed": False,
                "reason_code": "outer_reconciliation_failed",
                "error_type": type(error).__name__,
                "resource_ids": [],
                "removed_ids": [],
                "source": "docker_label_reconciliation",
            }
        result = attempt.get("result") or {}
        if (
            attempt.get("status") == "unknown"
            and not result.get("reconciliation")
            and (
                result.get("requires_reconciliation")
                or result.get("cleanup_confirmed") is False
                or result.get("reason_code") == "dispatcher_executor_error"
            )
        ):
            try:
                queue.record_reconciliation(
                    campaign_id, attempt["unit_id"], attempt["attempt_id"], evidence,
                )
            except Exception as error:
                evidence["queue_record_error_type"] = type(error).__name__
        outcomes.append({
            "unit_id": attempt.get("unit_id"),
            "attempt_id": attempt.get("attempt_id"),
            "generation": attempt.get("attempt_number"),
            "previous_status": attempt.get("status"),
            "cleanup_confirmed": evidence.get("cleanup_confirmed") is True,
            "reason_code": evidence.get("reason_code"),
            "resource_ids": evidence.get("resource_ids", []),
            "removed_ids": evidence.get("removed_ids", []),
            "evidence": evidence,
        })
    refreshed_run = queue.run(campaign_id)
    report = {
        "schema_version": 1,
        "kind": "phase4_p403_linux_outer_cleanup",
        "status": "confirmed" if outcomes and all(item["cleanup_confirmed"] for item in outcomes) else "unconfirmed" if outcomes else "not_started",
        "campaign_id": campaign_id,
        "parent_manifest_sha256": binding["parent_manifest_sha256"],
        "executor_manifest_sha256": binding["executor_manifest_sha256"],
        "queue_path": queue_path.as_posix(),
        "queue_state": refreshed_run.get("state"),
        "attempts_reconciled": len(outcomes),
        "cleanup_confirmed": all(item["cleanup_confirmed"] for item in outcomes) if outcomes else None,
        "attempts": outcomes,
    }
    report["report_sha256"] = object_sha256(report)
    return report
