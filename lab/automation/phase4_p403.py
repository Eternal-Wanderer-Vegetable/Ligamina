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


def _seal_p403_recovery_report(report: dict[str, Any]) -> dict[str, Any]:
    report.pop("report_sha256", None)
    report["report_sha256"] = object_sha256(report)
    return report


def _relative(path_value: str) -> Path:
    path = Path(path_value)
    return path if path.is_absolute() else ROOT / path


def _repository_relative(path: Path, label: str) -> str:
    resolved = Path(path).expanduser().resolve(strict=True)
    repository = ROOT.resolve(strict=True)
    if not resolved.is_relative_to(repository):
        raise Phase4P403Error(f"P4-03 {label} escapes the repository")
    return resolved.relative_to(repository).as_posix()


def _repository_file(path_value: Any, label: str) -> tuple[Path, str]:
    if not isinstance(path_value, str) or not path_value or Path(path_value).is_absolute():
        raise Phase4P403Error(f"P4-03 {label} must be a repository-relative path")
    path = (ROOT / path_value).resolve(strict=True)
    repository = ROOT.resolve(strict=True)
    if not path.is_relative_to(repository):
        raise Phase4P403Error(f"P4-03 {label} escapes the repository")
    return path, path.relative_to(repository).as_posix()


def _validate_p403_prior_linux_recovery(
    parent: dict[str, Any],
    parent_validation: dict[str, Any],
    previous_executor: dict[str, Any],
    previous_executor_validation: dict[str, Any],
    previous_report_path: Path,
    previous_controlled_report_path: Path,
    previous_executor_manifest_path: Path,
) -> dict[str, Any]:
    report = _json_load(previous_report_path)
    controlled_report = _json_load(previous_controlled_report_path)
    report_relative = _repository_relative(previous_report_path, "previous recovery report")
    executor_relative = _repository_relative(previous_executor_manifest_path, "previous executor manifest")
    controlled_evidence = controlled_report.get("evidence_sha256", {})
    if not isinstance(controlled_evidence, dict):
        raise Phase4P403Error("previous controlled report has no evidence hash map")
    report_file_sha256 = controlled_evidence.get(report_relative)
    executor_file_sha256 = controlled_evidence.get(executor_relative)
    previous_selection = {
        item["identity"]["registry_entry_id"]
        for item in previous_executor.get("selection", [])
        if isinstance(item, dict) and isinstance(item.get("identity"), dict)
    }
    previous_mode = (
        "linux_unknown_recovery_remaining"
        if isinstance(previous_executor.get("phase4_previous_recovery"), dict)
        else "linux_unknown_recovery"
    )
    if (
        not isinstance(report_file_sha256, str)
        or not _text_file_sha256_matches(previous_report_path, report_file_sha256)
        or controlled_report.get("kind") != "phase4_p403_controlled_run"
        or controlled_report.get("mode") != previous_mode
        or controlled_report.get("status") != "blocked"
        or controlled_report.get("exit_code") != 3
        or controlled_report.get("plugin_execution") != "dispatched"
        or not controlled_report.get("run_id")
    ):
        raise Phase4P403Error("previous controlled report does not bind the interrupted Linux recovery report")
    if controlled_evidence.get(executor_relative) != _file_sha256(previous_executor_manifest_path):
        raise Phase4P403Error("previous controlled report does not bind the original executor manifest bytes")
    if (
        report.get("kind") != "phase4_p403_linux_unknown_recovery"
        or report.get("status") != "interrupted"
        or report.get("queue_state") != "interrupted"
        or report.get("campaign_id") != previous_executor.get("campaign_id")
        or report.get("manifest_sha256") != previous_executor_validation.get("executor_manifest_sha256")
        or report.get("phase4_parent_manifest_sha256") != parent_validation.get("manifest_sha256")
        or report.get("phase4_executor_manifest_sha256") != previous_executor_validation.get("executor_manifest_sha256")
        or report.get("parent_unknown_count") != 39
        or not isinstance(report.get("denominator"), dict)
        or report["denominator"].get("expected_units") != len(previous_selection)
        or report.get("selected_parent_count", len(previous_selection)) != len(previous_selection)
    ):
        raise Phase4P403Error("previous P4-03 Linux recovery report does not match its interrupted executor selection")

    if executor_file_sha256 != _file_sha256(previous_executor_manifest_path):
        raise Phase4P403Error("previous controlled report does not bind the original executor manifest bytes")
    parent_rows = {
        row["registry_entry_id"]: row
        for row in parent.get("rows", [])
        if row.get("cohort") == "linux"
    }
    report_rows = report.get("plugin_reports")
    if not previous_selection or not isinstance(report_rows, list) or len(report_rows) != len(previous_selection):
        raise Phase4P403Error("previous P4-03 Linux recovery report must cover its exact nonempty executor selection")
    by_id: dict[str, dict[str, Any]] = {}
    not_run_ids: set[str] = set()
    attempted_ids: set[str] = set()
    for row in report_rows:
        if not isinstance(row, dict):
            raise Phase4P403Error("previous P4-03 recovery report contains a non-object row")
        entry_id = row.get("registry_entry_id")
        parent_row = parent_rows.get(entry_id)
        if not isinstance(entry_id, str) or entry_id not in previous_selection or entry_id in by_id or parent_row is None:
            raise Phase4P403Error("previous P4-03 recovery report has duplicate or unexpected parent identities")
        p4_parent = row.get("p4_parent", {})
        if (
            not isinstance(p4_parent, dict)
            or p4_parent.get("recovery_id") != parent_row.get("recovery_id")
            or p4_parent.get("parent_report_row_sha256") != parent_row.get("parent", {}).get("report_row_sha256")
            or p4_parent.get("parent_unknown_attempt_ids")
            != [attempt["attempt_id"] for attempt in parent_row.get("parent", {}).get("parent_unknown_attempts", [])]
        ):
            raise Phase4P403Error(f"previous P4-03 recovery report lost parent lineage for {entry_id}")
        status = row.get("recovery_status")
        attempts = row.get("attempts")
        if status not in {"recovered", "failed", "unknown", "not_run"} or not isinstance(attempts, list):
            raise Phase4P403Error(f"previous P4-03 recovery row has an invalid disposition for {entry_id}")
        if status == "not_run":
            if attempts:
                raise Phase4P403Error(f"previously not_run identity already has attempts: {entry_id}")
            not_run_ids.add(entry_id)
        else:
            if not attempts or any(
                not isinstance(attempt, dict) or attempt.get("attempt_number") != 1
                for attempt in attempts
            ):
                raise Phase4P403Error(f"previous recovery attempt budget is not one for {entry_id}")
            attempted_ids.add(entry_id)
        by_id[entry_id] = row

    if set(by_id) != previous_selection or len(attempted_ids) + len(not_run_ids) != len(previous_selection):
        raise Phase4P403Error("previous P4-03 Linux recovery report does not close the exact executor selection")
    if not not_run_ids:
        raise Phase4P403Error("previous P4-03 recovery report has no unattempted identities to select")
    if previous_mode == "linux_unknown_recovery" and (len(attempted_ids) != 14 or len(not_run_ids) != 25):
        raise Phase4P403Error("original P4-03 interrupted campaign must contain exactly 14 attempted and 25 not_run identities")
    counts = report.get("counts", {})
    if not isinstance(counts, dict):
        raise Phase4P403Error("previous P4-03 recovery report has no counts object")
    expected_counts = {
        "recovered": sum(row["recovery_status"] == "recovered" for row in report_rows),
        "plugin_or_stage_failed": sum(row["recovery_status"] == "failed" for row in report_rows),
        "unknown_or_cleanup_unconfirmed": sum(row["recovery_status"] == "unknown" for row in report_rows),
        "not_run": len(not_run_ids),
    }
    if any(counts.get(key) != value for key, value in expected_counts.items()):
        raise Phase4P403Error("previous P4-03 Linux recovery report counts do not match its row dispositions")
    return {
        "report": report,
        "report_file_sha256": report_file_sha256,
        "controlled_report": controlled_report,
        "controlled_report_file_sha256": _file_sha256(previous_controlled_report_path),
        "run_id": str(controlled_report["run_id"]),
        "campaign_id": report["campaign_id"],
        "not_run_ids": sorted(not_run_ids),
        "attempted_ids": sorted(attempted_ids),
    }


def _verified_p3_linux_worker_images(
    parent: dict[str, Any],
) -> tuple[dict[str, str], dict[str, Any], dict[str, Any]]:
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
    try:
        validate_linux_pilot_manifest(source_manifest)
    except RecoveryPilotError as error:
        raise Phase4P403Error(f"P3 Linux recovery source manifest is invalid: {error}") from error

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

    artifact_lock_path = ROOT / "catalogs/p2-41649744e057d61031d5/artifacts.lock.json"
    artifact_lock = _json_load(artifact_lock_path)
    artifact_lock_sha256 = _file_sha256(artifact_lock_path)
    source_identity = source_manifest.get("source_identity", {})
    override_contract = source_manifest.get("archive_size_overrides")
    override_rows = override_contract.get("entries") if isinstance(override_contract, dict) else None
    source_selection = {
        row["identity"]["registry_entry_id"]: row for row in source_manifest.get("selection", [])
        if isinstance(row, dict) and isinstance(row.get("identity"), dict)
    }
    overrides_by_id = {
        row.get("registry_entry_id"): row for row in override_rows if isinstance(row, dict)
    } if isinstance(override_rows, list) else {}
    if (
        not isinstance(override_contract, dict)
        or override_contract.get("schema_version") != 1
        or override_contract.get("base_artifact_lock_sha256") != artifact_lock_sha256
        or source_identity.get("artifact_lock_sha256") != artifact_lock_sha256
        or not isinstance(override_rows, list)
        or len(overrides_by_id) != len(override_rows)
        or set(overrides_by_id) != set(source_selection)
    ):
        raise Phase4P403Error("P3 Linux recovery archive-size overrides are not bound to the frozen artifact lock and selection")
    locked_artifacts = {row["registry_entry_id"]: row for row in artifact_lock.get("entries", [])}
    parent_linux_ids = {
        row["registry_entry_id"] for row in parent.get("rows", []) if row.get("cohort") == "linux"
    }
    p3_overrides = []
    for entry_id in sorted(parent_linux_ids):
        selection = source_selection.get(entry_id)
        override = overrides_by_id.get(entry_id)
        if selection is None or override is None:
            raise Phase4P403Error(f"P3 Linux archive-size override is missing for P4 parent {entry_id}")
        identity = selection["identity"]
        artifact_entry = locked_artifacts.get(entry_id, {})
        artifact_platform = "all" if identity.get("ecosystem") == "koishi" else "linux-x86_64"
        artifact_record = next(
            (item for item in artifact_entry.get("artifacts", []) if item.get("platform") == artifact_platform), {},
        )
        artifact = artifact_record.get("artifact") or {}
        cache_path = artifact.get("cache_path")
        metadata_size = artifact.get("size_bytes")
        if (
            identity.get("artifact_sha256") != artifact.get("computed_digests", {}).get("sha256")
            or override.get("artifact_sha256") != identity.get("artifact_sha256")
            or override.get("cache_path") != cache_path
            or override.get("metadata_size_bytes") != metadata_size
            or not isinstance(cache_path, str)
            or Path(cache_path).is_absolute()
            or ".." in Path(cache_path).parts
            or not isinstance(override.get("archive_size_bytes"), int)
            or override["archive_size_bytes"] < 1
        ):
            raise Phase4P403Error(f"P3 Linux archive-size override does not match frozen artifact identity for {entry_id}")
        p3_overrides.append(dict(override))

    binding = {
        "manifest_path": manifest_relative,
        "manifest_file_sha256": source["manifest_file_sha256"],
        "manifest_sha256": manifest_sha256,
        "report_path": report_relative,
        "report_file_sha256": source["report_file_sha256"],
        "campaign_id": source_report["campaign_id"],
    }
    p4_archive_size_overrides = {
        "schema_version": 1,
        "base_artifact_lock_sha256": artifact_lock_sha256,
        "entries": p3_overrides,
    }
    return dict(worker_images), binding, p4_archive_size_overrides


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
    worker_images, worker_image_source, archive_size_overrides = _verified_p3_linux_worker_images(parent)
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
        "archive_size_overrides": archive_size_overrides,
    })
    seed = object_sha256(core)
    executor_manifest = {"campaign_id": f"p4-p403-linux-{seed[:12]}", **core}
    executor_manifest["manifest_sha256"] = object_sha256(executor_manifest)
    try:
        validate_linux_pilot_manifest(executor_manifest)
    except RecoveryPilotError as error:
        raise Phase4P403Error(f"generated P4-03 Linux execution manifest is invalid: {error}") from error
    return executor_manifest


def build_p403_linux_remaining_executor_manifest(
    parent_manifest_path: Path,
    previous_executor_manifest_path: Path,
    previous_report_path: Path,
    previous_controlled_report_path: Path,
) -> dict[str, Any]:
    """Freeze only the parent identities left not_run by the prior interrupted campaign."""
    parent_manifest_path = Path(parent_manifest_path).expanduser().resolve(strict=True)
    previous_executor_manifest_path = Path(previous_executor_manifest_path).expanduser().resolve(strict=True)
    previous_report_path = Path(previous_report_path).expanduser().resolve(strict=True)
    previous_controlled_report_path = Path(previous_controlled_report_path).expanduser().resolve(strict=True)
    parent = _json_load(parent_manifest_path)
    parent_validation = validate_p403_recovery_manifest(parent_manifest_path)
    previous_executor = _json_load(previous_executor_manifest_path)
    previous_executor_validation = validate_p403_linux_executor_manifest(
        parent_manifest_path, previous_executor_manifest_path,
    )
    if previous_executor_validation.get("selected_units") != len(previous_executor.get("selection", [])):
        raise Phase4P403Error("remaining-selection source executor selection is invalid")
    prior = _validate_p403_prior_linux_recovery(
        parent, parent_validation, previous_executor, previous_executor_validation,
        previous_report_path, previous_controlled_report_path, previous_executor_manifest_path,
    )
    report_path = _repository_relative(previous_report_path, "previous recovery report")
    controlled_report_path = _repository_relative(previous_controlled_report_path, "previous controlled report")
    executor_path = _repository_relative(previous_executor_manifest_path, "previous executor manifest")
    parent_rows = {row["identity"]["registry_entry_id"]: row for row in previous_executor["selection"]}
    selected_ids = set(prior["not_run_ids"])
    selected = [parent_rows[entry_id] for entry_id in sorted(selected_ids)]

    core = {
        key: value
        for key, value in previous_executor.items()
        if key not in {"campaign_id", "manifest_sha256"}
    }
    core["selection"] = selected
    core["expected_units"] = len(selected)
    core["recovery_per_ecosystem"] = {
        ecosystem: sum(row["identity"]["ecosystem"] == ecosystem for row in selected)
        for ecosystem in ("nonebot", "koishi")
    }
    core["failed_controls_per_ecosystem"] = {"nonebot": 0, "koishi": 0}
    core["selection_algorithm"] = (
        "P4-03 exact not_run identities from the hash-bound interrupted campaign; "
        "exclude every identity with a prior P4-03 attempt"
    )
    core["host_service_profiles"] = _build_core_only_host_service_profiles(selected)
    overrides = dict(previous_executor["archive_size_overrides"])
    overrides["entries"] = [
        item for item in overrides["entries"]
        if item.get("registry_entry_id") in selected_ids
    ]
    core["archive_size_overrides"] = overrides
    core["phase4_previous_recovery"] = {
        "report_path": report_path,
        "report_file_sha256": prior["report_file_sha256"],
        "controlled_report_path": controlled_report_path,
        "controlled_report_file_sha256": prior["controlled_report_file_sha256"],
        "run_id": prior["run_id"],
        "campaign_id": prior["campaign_id"],
        "executor_manifest_path": executor_path,
        "executor_manifest_sha256": previous_executor_validation["executor_manifest_sha256"],
        "attempted_parent_count": len(prior["attempted_ids"]),
        "attempted_parent_ids_sha256": object_sha256(prior["attempted_ids"]),
        "not_run_parent_count": len(prior["not_run_ids"]),
        "not_run_parent_ids_sha256": object_sha256(prior["not_run_ids"]),
    }
    seed = object_sha256(core)
    executor_manifest = {"campaign_id": f"p4-p403-linux-remaining-{seed[:12]}", **core}
    executor_manifest["manifest_sha256"] = object_sha256(executor_manifest)
    try:
        validate_linux_pilot_manifest(executor_manifest)
    except RecoveryPilotError as error:
        raise Phase4P403Error(f"generated P4-03 remaining Linux manifest is invalid: {error}") from error
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
    selected = executor.get("selection", [])
    selected_ids_list = [
        item.get("identity", {}).get("registry_entry_id")
        for item in selected
        if isinstance(item, dict)
    ] if isinstance(selected, list) else []
    if len(selected_ids_list) != len(selected) or len(set(selected_ids_list)) != len(selected_ids_list):
        raise Phase4P403Error("P4-03 Linux executor selection has invalid or duplicate identities")
    selected_ids = set(selected_ids_list)
    worker_images, worker_image_source, archive_size_overrides = _verified_p3_linux_worker_images(parent)
    previous_binding = executor.get("phase4_previous_recovery")
    expected_overrides = archive_size_overrides
    if previous_binding is not None:
        expected_overrides = dict(archive_size_overrides)
        expected_overrides["entries"] = [
            item for item in archive_size_overrides["entries"]
            if item.get("registry_entry_id") in selected_ids
        ]
    if (
        executor.get("worker_images") != worker_images
        or executor.get("worker_image_source") != worker_image_source
        or executor.get("archive_size_overrides") != expected_overrides
    ):
        raise Phase4P403Error("P4-03 Linux executor Worker images or archive sizes are not bound to the verified P3 source run")
    parent_binding = executor.get("phase4_parent_manifest", {})
    if (
        parent_binding.get("file_sha256") != parent_validation["manifest_file_sha256"]
        or parent_binding.get("manifest_sha256") != parent_validation["manifest_sha256"]
        or parent_binding.get("closure_file_sha256") != parent.get("parent_closure", {}).get("file_sha256")
        or parent_binding.get("expected_linux_parent_count") != 39
    ):
        raise Phase4P403Error("P4-03 Linux executor manifest is not bound to the current parent manifest")
    parents = {row["registry_entry_id"]: row for row in parent["rows"] if row.get("cohort") == "linux"}
    if previous_binding is None:
        if len(selected) != 39 or selected_ids != set(parents):
            raise Phase4P403Error("P4-03 Linux executor selection is not the exact 39 parent identities")
    else:
        if not isinstance(previous_binding, dict):
            raise Phase4P403Error("P4-03 remaining executor previous-report binding is invalid")
        previous_executor_path, previous_executor_relative = _repository_file(
            previous_binding.get("executor_manifest_path"), "previous executor manifest",
        )
        previous_report_path, previous_report_relative = _repository_file(
            previous_binding.get("report_path"), "previous recovery report",
        )
        previous_controlled_report_path, previous_controlled_report_relative = _repository_file(
            previous_binding.get("controlled_report_path"), "previous controlled report",
        )
        previous_executor = _json_load(previous_executor_path)
        previous_executor_validation = validate_p403_linux_executor_manifest(
            parent_manifest_path, previous_executor_path,
        )
        if previous_executor_validation.get("selected_units") != len(previous_executor.get("selection", [])):
            raise Phase4P403Error("P4-03 remaining executor previous selection is invalid")
        prior = _validate_p403_prior_linux_recovery(
            parent, parent_validation, previous_executor, previous_executor_validation,
            previous_report_path, previous_controlled_report_path, previous_executor_path,
        )
        expected_previous_binding = {
            "report_path": previous_report_relative,
            "report_file_sha256": prior["report_file_sha256"],
            "controlled_report_path": previous_controlled_report_relative,
            "controlled_report_file_sha256": prior["controlled_report_file_sha256"],
            "run_id": prior["run_id"],
            "campaign_id": prior["campaign_id"],
            "executor_manifest_path": previous_executor_relative,
            "executor_manifest_sha256": previous_executor_validation["executor_manifest_sha256"],
            "attempted_parent_count": len(prior["attempted_ids"]),
            "attempted_parent_ids_sha256": object_sha256(prior["attempted_ids"]),
            "not_run_parent_count": len(prior["not_run_ids"]),
            "not_run_parent_ids_sha256": object_sha256(prior["not_run_ids"]),
        }
        if (
            previous_binding != expected_previous_binding
            or executor.get("selection_algorithm")
            != "P4-03 exact not_run identities from the hash-bound interrupted campaign; exclude every identity with a prior P4-03 attempt"
            or not selected_ids
            or selected_ids != set(prior["not_run_ids"])
            or not isinstance(executor.get("campaign_id"), str)
            or not executor["campaign_id"].startswith("p4-p403-linux-remaining-")
        ):
            raise Phase4P403Error("P4-03 remaining executor selection does not exactly match the prior not_run identities")
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
        "selected_units": len(selected),
        "ecosystem_counts": low_level["counts"],
        "previous_recovery_report_file_sha256": previous_binding.get("report_file_sha256") if isinstance(previous_binding, dict) else None,
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
        "selected_parent_count": len(report.get("plugin_reports", [])),
        "windows_parent_unknowns_not_dispatched": 14,
        "automation_gate": "blocked_pending_linux_results_windows_admission_and_runner_acceptance",
    })
    if isinstance(executor.get("phase4_previous_recovery"), dict):
        report["previous_recovery_report_file_sha256"] = executor["phase4_previous_recovery"]["report_file_sha256"]
    _seal_p403_recovery_report(report)
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
