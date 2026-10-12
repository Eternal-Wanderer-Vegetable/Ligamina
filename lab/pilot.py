"""Build a deterministic, stratified phase-two pilot manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable

try:
    from .catalog_run import load_catalog_units
    from .recipes import suggest_recipe_categories
except ImportError:  # supports `python -m lab.pilot` from the repository root
    from catalog_run import load_catalog_units
    from recipes import suggest_recipe_categories


ROOT = Path(__file__).resolve().parents[1]
PLATFORMS = ("windows", "linux")
PLATFORM_IDS = {"windows": "windows-x86_64", "linux": "linux-x86_64"}
DEFAULT_SNAPSHOT = ROOT / "catalogs/p2-41649744e057d61031d5"
DEFAULT_OUTPUT = ROOT / "outputs/acceptance/phase2-s209-pilot-manifest-20261012.json"
DEFAULT_LEGACY_MANIFEST = ROOT / "samples/third-party.lock.json"
DEFAULT_SUPPORT_AUDIT = ROOT / "outputs/acceptance/t18-final-audit-20261012.json"


class PilotError(ValueError):
    """Pilot inputs or cohort constraints are inconsistent."""


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _read_json(path: Path) -> tuple[Any, bytes]:
    try:
        raw = path.read_bytes()
        return json.loads(raw.decode("utf-8")), raw
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PilotError(f"cannot read JSON input {path}: {error}") from error


def _artifact_statuses(artifact_entry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        item.get("platform"): item
        for item in artifact_entry.get("artifacts", [])
        if isinstance(item, dict) and isinstance(item.get("platform"), str)
    }


def _entry_facts(
    entry: dict[str, Any],
    artifact_entry: dict[str, Any],
    profile_entry: dict[str, Any],
) -> dict[str, Any]:
    classification = suggest_recipe_categories(entry)
    categories = sorted(set(classification.get("categories", [])))
    artifact_by_platform = _artifact_statuses(artifact_entry)
    profiles = profile_entry.get("profiles", {})
    failures = []
    for platform_id in PLATFORM_IDS.values():
        artifact = artifact_by_platform.get(platform_id, {})
        profile = profiles.get(platform_id, {})
        if artifact.get("status") != "locked":
            failures.append(artifact.get("reason_code") or artifact.get("status") or "artifact_missing")
        if profile.get("status") != "locked":
            failures.append(profile.get("reason_code") or profile.get("status") or "profile_missing")

    source = entry.get("source_record", {})
    if entry.get("ecosystem") == "nonebot":
        registry_failure = source.get("valid") is False
    else:
        registry_failure = source.get("unsafe") is True

    package_metadata = source.get("package", {}) if isinstance(source.get("package"), dict) else {}
    manifest_metadata = source.get("manifest", {}) if isinstance(source.get("manifest"), dict) else {}
    manifest_description = manifest_metadata.get("description", {})
    if isinstance(manifest_description, dict):
        manifest_description = " ".join(str(value) for value in manifest_description.values())
    searchable = " ".join(str(value) for value in (
        entry.get("package"), source.get("shortname"), source.get("name"), source.get("desc"),
        source.get("category"), package_metadata.get("description"), manifest_description,
        *(package_metadata.get("keywords", []) if isinstance(package_metadata.get("keywords", []), list) else []),
        *(tag.get("label", "") for tag in source.get("tags", []) if isinstance(tag, dict)),
    )).casefold()
    message_candidate = "message" in categories or bool(re.search(
        r"(^|[^a-z])(chat|message|reply|command|session)([^a-z]|$)|群聊|消息|命令|问答", searchable,
    ))
    non_message_candidate = bool(set(categories) & {"adapter", "storage", "services", "scheduler", "console"}) or bool(
        re.search(r"(^|[^a-z])(adapter|driver|storage|database|service|scheduler|server|console|webui)([^a-z]|$)|数据库|存储|服务|定时|适配器|控制台", searchable)
    )

    return {
        "categories": categories,
        "unmapped_labels": sorted(set(classification.get("unmapped_labels", []))),
        "classification_reason_code": classification.get("reason_code"),
        "registry_failure": registry_failure,
        "registry_quality": "known_failure" if registry_failure else "not_marked_failed",
        "official_maintained": bool(entry.get("official_maintained")),
        "profile_or_artifact_failures": sorted(set(failures)),
        "profile_ready_both_os": not failures,
        "message_metadata_candidate": message_candidate,
        "non_message_metadata_candidate": non_message_candidate,
        # Recipe catalogs describe probe shapes; none is currently a frozen fixture for a
        # particular catalog entry. This is deliberately independent of category mapping.
        "behavior_fixture_status": "missing_fixture",
    }


def _order_key(snapshot_id: str, entry_id: str) -> bytes:
    return hashlib.sha256(f"{snapshot_id}\0{entry_id}".encode("utf-8")).digest()


def select_stratified_entries(
    entries: list[dict[str, Any]], facts_by_id: dict[str, dict[str, Any]], *, snapshot_id: str, minimum_per_ecosystem: int = 30,
) -> tuple[set[str], dict[str, list[str]], dict[str, dict[str, int]]]:
    """Select a stable cohort with explicit coverage for plan-required strata."""
    if minimum_per_ecosystem < 1:
        raise PilotError("minimum_per_ecosystem must be positive")

    selected: set[str] = set()
    reasons: dict[str, list[str]] = {}
    summaries: dict[str, dict[str, int]] = {}
    by_ecosystem: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        by_ecosystem.setdefault(entry["ecosystem"], []).append(entry)

    for ecosystem, cohort in sorted(by_ecosystem.items()):
        if len(cohort) < minimum_per_ecosystem:
            raise PilotError(f"{ecosystem} has only {len(cohort)} entries; needs {minimum_per_ecosystem}")
        ordered = sorted(cohort, key=lambda item: _order_key(snapshot_id, item["registry_entry_id"]))

        strata: list[tuple[str, Callable[[dict[str, Any], dict[str, Any]], bool], int]] = [
            ("official_maintained", lambda e, f: f["official_maintained"], 2),
            ("community", lambda e, f: not f["official_maintained"], 2),
            ("known_registry_failure", lambda e, f: f["registry_failure"], 1),
            ("known_lock_failure", lambda e, f: bool(f["profile_or_artifact_failures"]), 2),
            ("locked_both_os", lambda e, f: f["profile_ready_both_os"], 2),
            ("message_metadata_candidate", lambda e, f: f["message_metadata_candidate"], 1),
            ("non_message_metadata_candidate", lambda e, f: f["non_message_metadata_candidate"], 1),
            ("condition_missing_fixture", lambda e, f: f["behavior_fixture_status"] == "missing_fixture", 1),
            ("unmapped_category", lambda e, f: not f["categories"], 1),
        ]
        # NoneBot exposes application/library registry types; Koishi exposes registry
        # categories. Include one of each available type/category where the source has it.
        if ecosystem == "nonebot":
            type_values = sorted({str(e.get("source_record", {}).get("type")) for e in ordered if e.get("source_record", {}).get("type")})
            for value in type_values:
                strata.append((f"registry_type:{value}", lambda e, f, v=value: e.get("source_record", {}).get("type") == v, 1))
        else:
            category_values = sorted({str(e.get("source_record", {}).get("category")) for e in ordered if e.get("source_record", {}).get("category")})
            for value in category_values[:8]:
                strata.append((f"registry_category:{value}", lambda e, f, v=value: e.get("source_record", {}).get("category") == v, 1))

        for stratum, predicate, quota in strata:
            matching = [entry for entry in ordered if predicate(entry, facts_by_id[entry["registry_entry_id"]])]
            already = [entry for entry in matching if entry["registry_entry_id"] in selected]
            need = max(0, quota - len(already))
            for entry in matching:
                if not need:
                    break
                entry_id = entry["registry_entry_id"]
                if entry_id not in selected:
                    selected.add(entry_id)
                    reasons.setdefault(entry_id, []).append(stratum)
                    need -= 1
            if not matching and stratum in {
                "known_registry_failure", "known_lock_failure", "message_metadata_candidate", "non_message_metadata_candidate",
            }:
                continue
            # Existing selections count toward the stratum even when a later stratum found them.
            for entry in already:
                reasons.setdefault(entry["registry_entry_id"], []).append(stratum)

        for entry in ordered:
            if sum(item["registry_entry_id"] in selected for item in cohort) >= minimum_per_ecosystem:
                break
            entry_id = entry["registry_entry_id"]
            if entry_id not in selected:
                selected.add(entry_id)
                reasons.setdefault(entry_id, []).append("deterministic_fill")

        # The set contains prior ecosystems too, so count this ecosystem explicitly.
        selected_in_ecosystem = [entry for entry in cohort if entry["registry_entry_id"] in selected]
        if len(selected_in_ecosystem) < minimum_per_ecosystem:
            raise PilotError(f"stratification selected too few {ecosystem} entries")
        summary: dict[str, int] = {}
        for entry in selected_in_ecosystem:
            for reason in reasons.get(entry["registry_entry_id"], []):
                summary[reason] = summary.get(reason, 0) + 1
        summaries[ecosystem] = {"selected_entries": len(selected_in_ecosystem), **dict(sorted(summary.items()))}

    return selected, reasons, summaries


def _load_legacy_support(manifest_path: Path, audit_path: Path) -> tuple[list[dict[str, Any]], dict[str, str]]:
    manifest, manifest_bytes = _read_json(manifest_path)
    audit, audit_bytes = _read_json(audit_path)
    if not isinstance(manifest, dict) or not isinstance(audit, dict):
        raise PilotError("legacy manifest and support audit must be objects")
    samples = manifest.get("samples")
    if not isinstance(samples, list):
        raise PilotError("legacy manifest must contain a samples array")
    support = audit.get("supported_plugins")
    if not isinstance(support, list) or not support:
        raise PilotError("support audit does not declare exact supported plugin versions")
    expected = {
        (item.get("ecosystem"), item.get("package"), item.get("version"))
        for item in support if isinstance(item, dict)
    }
    candidates = [item for item in samples if isinstance(item, dict) and item.get("l4_candidate") is True]
    actual = {(item.get("ecosystem"), item.get("package"), item.get("version")) for item in candidates}
    if expected != actual or len(candidates) != len(expected):
        raise PilotError("legacy L4 candidates do not exactly match the current supported-plugin audit")
    for item in candidates:
        if not isinstance(item.get("fixture_path"), str) or not item["fixture_path"]:
            raise PilotError(f"legacy sample has no fixture path: {item.get('sample_id')}")
    return sorted(candidates, key=lambda item: item["sample_id"]), {
        "legacy_manifest_sha256": _sha256(manifest_bytes),
        "support_audit_sha256": _sha256(audit_bytes),
    }


def build_pilot_manifest(
    snapshot_dir: Path = DEFAULT_SNAPSHOT,
    *,
    legacy_manifest_path: Path = DEFAULT_LEGACY_MANIFEST,
    support_audit_path: Path = DEFAULT_SUPPORT_AUDIT,
    minimum_per_ecosystem: int = 30,
) -> dict[str, Any]:
    snapshot_dir = Path(snapshot_dir).expanduser().resolve(strict=True)
    catalog, units = load_catalog_units(snapshot_dir, PLATFORMS)
    artifact_lock, artifact_bytes = _read_json(snapshot_dir / "artifacts.lock.json")
    profile_lock, profile_bytes = _read_json(snapshot_dir / "profiles.lock.json")
    catalog_raw = (snapshot_dir / "catalog.json").read_bytes()
    artifacts_by_id = {item["registry_entry_id"]: item for item in artifact_lock["entries"]}
    profiles_by_id = {item["registry_entry_id"]: item for item in profile_lock["entries"]}
    entries = catalog["entries"]
    facts_by_id = {
        entry["registry_entry_id"]: _entry_facts(
            entry, artifacts_by_id[entry["registry_entry_id"]], profiles_by_id[entry["registry_entry_id"]],
        )
        for entry in entries
    }
    selected, selection_reasons, selection_summary = select_stratified_entries(
        entries, facts_by_id, snapshot_id=catalog["snapshot_id"], minimum_per_ecosystem=minimum_per_ecosystem,
    )
    unit_by_key = {(unit["registry_entry_id"], unit["platform"]): unit for unit in units}
    ledger = []
    for entry in entries:
        entry_id = entry["registry_entry_id"]
        facts = facts_by_id[entry_id]
        for platform in PLATFORMS:
            unit = unit_by_key[(entry_id, platform)]
            ledger.append({
                "registry_entry_id": entry_id,
                "ecosystem": entry["ecosystem"],
                "package": entry["package"],
                "version": entry.get("advertised_version"),
                "platform": platform,
                "selected": entry_id in selected,
                "selection_reasons": selection_reasons.get(entry_id, []),
                "artifact_status": unit["artifact_status"],
                "artifact_reason_code": unit["artifact_reason_code"],
                "profile_status": unit["profile_status"],
                "profile_reason_code": unit["profile_reason_code"],
                "recipe_categories": facts["categories"],
                "registry_quality": facts["registry_quality"],
                "behavior_fixture_status": facts["behavior_fixture_status"],
                "pilot_disposition": "selected_for_pilot" if entry_id in selected else "outside_pilot_cohort",
            })

    legacy_samples, legacy_hashes = _load_legacy_support(legacy_manifest_path, support_audit_path)
    legacy_units = [
        {
            "sample_id": item["sample_id"],
            "ecosystem": item["ecosystem"],
            "package": item["package"],
            "version": item["version"],
            "artifact_hash": item["artifact_hash"],
            "fixture_path": item["fixture_path"],
            "platforms": list(PLATFORMS),
            "unit_key": f"legacy:{item['sample_id']}@{item['version']}",
        }
        for item in legacy_samples
    ]
    expected_pilot_results = len(selected) * len(PLATFORMS) + len(legacy_units) * len(PLATFORMS)
    if len(ledger) != len(entries) * len(PLATFORMS):
        raise PilotError("full snapshot pilot assignment ledger does not close")
    if len({(item["registry_entry_id"], item["platform"]) for item in ledger}) != len(ledger):
        raise PilotError("full snapshot pilot assignment ledger contains duplicate entry/OS identities")

    manifest = {
        "schema_version": 1,
        "task_id": "S2-09",
        "kind": "phase2_stratified_pilot_manifest",
        "status": "planned",
        "snapshot_id": catalog["snapshot_id"],
        "catalog_entry_count": len(entries),
        "catalog_entry_os_denominator": len(entries) * len(PLATFORMS),
        "catalog_assignment_ledger_count": len(ledger),
        "minimum_per_ecosystem": minimum_per_ecosystem,
        "selection_method": {
            "seed": catalog["snapshot_id"],
            "ordering": "sha256(snapshot_id + NUL + registry_entry_id)",
            "behavior_strata": "message/non-message are metadata keyword/category heuristics only; they do not assign a runnable recipe or claim behavior",
            "fixture_policy": "no entry-specific fixtures are currently frozen; selected units retain missing_fixture until one is bound",
        },
        "selected_catalog_entry_count": len(selected),
        "selected_catalog_entry_os_count": len(selected) * len(PLATFORMS),
        "legacy_support_version_count": len(legacy_units),
        "legacy_support_os_count": len(legacy_units) * len(PLATFORMS),
        "expected_pilot_result_count": expected_pilot_results,
        "selection_by_ecosystem": selection_summary,
        "input_hashes": {
            "catalog_sha256": _sha256(catalog_raw),
            "artifacts_lock_sha256": _sha256(artifact_bytes),
            "profiles_lock_sha256": _sha256(profile_bytes),
            **legacy_hashes,
        },
        "execution_contract": {
            "catalog_plugins_executed": False,
            "legacy_support_plugins_executed": False,
            "windows_status": "not_started",
            "linux_status": "not_started",
            "unselected_catalog_rows_are_results": False,
            "pilot_results_must_be_terminal": True,
        },
        "catalog_assignment_ledger": ledger,
        "legacy_support_units": legacy_units,
    }
    manifest["manifest_sha256"] = _sha256(_canonical(manifest))
    return manifest


def write_manifest(manifest: dict[str, Any], output_path: Path) -> Path:
    path = Path(output_path).expanduser().absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) * percentile + 0.999999999) - 1)))
    return ordered[index]


def _resource_summary(values: list[float], *, units: str) -> dict[str, Any]:
    if not values:
        return {"sample_count": 0, "units": units, "p50": None, "p95": None, "min": None, "max": None}
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    p50 = ordered[midpoint] if len(ordered) % 2 else (ordered[midpoint - 1] + ordered[midpoint]) / 2
    return {
        "sample_count": len(ordered),
        "units": units,
        "p50": p50,
        "p95": _percentile(ordered, 0.95),
        "min": ordered[0],
        "max": ordered[-1],
    }


def build_acceptance_report(
    manifest_path: Path,
    legacy_report_path: Path,
    failed_attempt_path: Path,
    *,
    s205_source_path: Path = ROOT / "outputs/acceptance/phase2-s205-20261012.json",
    worker_linux_path: Path = ROOT / "outputs/acceptance/phase2-workers-linux-20261012.json",
    worker_windows_path: Path = ROOT / "outputs/acceptance/phase2-workers-windows-20261012.json",
    queue_acceptance_path: Path = ROOT / "outputs/acceptance/phase2-s207-20261012.json",
    budget_path: Path = ROOT / "config/lab/phase2-pilot-budget-v1.json",
    nonebot_catalog_pilot_report_path: Path = ROOT / "outputs/acceptance/phase2-s209-nonebot-linux-pilot-20261012.json",
    catalog_pilot_report_path: Path = ROOT / "outputs/acceptance/phase2-s209-koishi-linux-pilot-20261012.json",
    windows_catalog_pilot_report_path: Path = ROOT / "outputs/acceptance/phase2-s209-koishi-windows-pilot-20261012.json",
) -> dict[str, Any]:
    """Close the S2-09 pilot result denominator while preserving blocked and skipped states."""
    manifest, manifest_raw = _read_json(manifest_path)
    legacy_run, legacy_raw = _read_json(legacy_report_path)
    failed_attempt, failed_raw = _read_json(failed_attempt_path)
    s205_source, s205_raw = _read_json(s205_source_path)
    linux_worker, linux_worker_raw = _read_json(worker_linux_path)
    windows_worker, windows_worker_raw = _read_json(worker_windows_path)
    queue_acceptance, queue_raw = _read_json(queue_acceptance_path)
    budget, budget_raw = _read_json(budget_path)
    nonebot_catalog_pilot, nonebot_catalog_pilot_raw = _read_json(nonebot_catalog_pilot_report_path)
    catalog_pilot, catalog_pilot_raw = _read_json(catalog_pilot_report_path)
    windows_catalog_pilot, windows_catalog_pilot_raw = _read_json(windows_catalog_pilot_report_path)
    if not all(isinstance(value, dict) for value in (manifest, legacy_run, failed_attempt, s205_source, linux_worker, windows_worker, queue_acceptance, budget, nonebot_catalog_pilot, catalog_pilot, windows_catalog_pilot)):
        raise PilotError("S2-09 report inputs must all be JSON objects")

    expected_manifest_hash = manifest.get("manifest_sha256")
    hash_input = dict(manifest)
    hash_input.pop("manifest_sha256", None)
    if expected_manifest_hash != _sha256(_canonical(hash_input)):
        raise PilotError("pilot manifest hash mismatch")
    if manifest.get("kind") != "phase2_stratified_pilot_manifest" or manifest.get("status") != "planned":
        raise PilotError("pilot manifest is not a planned S2-09 manifest")
    if legacy_run.get("task_id") != "S2-09" or legacy_run.get("kind") != "phase2_s209_legacy_support_regression":
        raise PilotError("legacy regression report is not an S2-09 run")
    if legacy_run.get("status") != "completed":
        raise PilotError("all four historical support versions must complete their Linux pair run")
    if legacy_run.get("source_s205_report_sha256") != _sha256(s205_raw):
        raise PilotError("S2-05 source report changed since the S2-09 legacy run")
    if failed_attempt.get("task_id") != "S2-09" or failed_attempt.get("status") != "in-progress":
        raise PilotError("the preserved first-attempt failure report has an unexpected identity")
    if queue_acceptance.get("task_id") != "S2-07" or queue_acceptance.get("status") != "completed":
        raise PilotError("queue interruption/recovery evidence is not the accepted S2-07 report")
    if budget.get("task_id") != "S2-09" or budget.get("status") != "provisional":
        raise PilotError("pilot budget must remain provisional until both-OS full cohort measurements exist")
    if nonebot_catalog_pilot.get("task_id") != "S2-09" or nonebot_catalog_pilot.get("kind") != "phase2_s209_nonebot_linux_pilot_workers":
        raise PilotError("NoneBot Linux catalog pilot report has the wrong identity")
    if nonebot_catalog_pilot.get("pilot_manifest_sha256") != manifest.get("manifest_sha256"):
        raise PilotError("NoneBot Linux catalog pilot belongs to a different selection manifest")
    if nonebot_catalog_pilot.get("selected_nonebot_linux_entries") != manifest.get("minimum_per_ecosystem"):
        raise PilotError("NoneBot Linux catalog pilot did not close the minimum selected entry denominator")
    if nonebot_catalog_pilot.get("terminal_results") != nonebot_catalog_pilot.get("selected_nonebot_linux_entries") or nonebot_catalog_pilot.get("closed_exactly_once") is not True:
        raise PilotError("NoneBot Linux pilot results do not close exactly once")
    if catalog_pilot.get("task_id") != "S2-09" or catalog_pilot.get("kind") != "phase2_s209_koishi_linux_pilot_workers":
        raise PilotError("Koishi Linux catalog pilot report has the wrong identity")
    if catalog_pilot.get("pilot_manifest_sha256") != manifest.get("manifest_sha256"):
        raise PilotError("Koishi Linux catalog pilot belongs to a different selection manifest")
    if catalog_pilot.get("selected_koishi_linux_entries") != manifest.get("minimum_per_ecosystem"):
        raise PilotError("Koishi Linux catalog pilot did not close the minimum selected entry denominator")
    if catalog_pilot.get("terminal_results") != catalog_pilot.get("selected_koishi_linux_entries") or catalog_pilot.get("closed_exactly_once") is not True:
        raise PilotError("Koishi Linux catalog pilot results do not close exactly once")
    if windows_catalog_pilot.get("task_id") != "S2-09" or windows_catalog_pilot.get("kind") != "phase2_s209_koishi_windows_pilot_workers":
        raise PilotError("Koishi Windows catalog pilot report has the wrong identity")
    if windows_catalog_pilot.get("pilot_manifest_sha256") != manifest.get("manifest_sha256"):
        raise PilotError("Koishi Windows catalog pilot belongs to a different selection manifest")
    if windows_catalog_pilot.get("selected_koishi_windows_entries") != manifest.get("minimum_per_ecosystem"):
        raise PilotError("Koishi Windows catalog pilot did not close the minimum selected entry denominator")
    if windows_catalog_pilot.get("terminal_results") != windows_catalog_pilot.get("selected_koishi_windows_entries") or windows_catalog_pilot.get("closed_exactly_once") is not True:
        raise PilotError("Koishi Windows pilot results do not close exactly once")

    current_s205_hash = _sha256(Path(s205_source_path).read_bytes())
    if legacy_run.get("source_s205_report_sha256") != current_s205_hash:
        raise PilotError("S2-05 source acceptance report was modified")
    legacy_results = {
        item.get("sample_id"): item
        for item in legacy_run.get("implementation", {}).get("samples", [])
        if isinstance(item, dict)
    }
    legacy_units = manifest.get("legacy_support_units", [])
    expected_legacy_ids = {item.get("sample_id") for item in legacy_units}
    if len(legacy_results) != len(legacy_units) or set(legacy_results) != expected_legacy_ids:
        raise PilotError("legacy regression results do not exactly cover supported versions")

    catalog_pilot_results = {
        item.get("result_id"): item
        for item in [*nonebot_catalog_pilot.get("results", []), *catalog_pilot.get("results", []), *windows_catalog_pilot.get("results", [])]
        if isinstance(item, dict)
    }
    expected_catalog_result_ids = {
        f"catalog:{row['registry_entry_id']}|{row['platform']}"
        for row in manifest.get("catalog_assignment_ledger", [])
        if row.get("selected") is True and (row.get("ecosystem") == "koishi" or (row.get("ecosystem") == "nonebot" and row.get("platform") == "linux"))
    }
    if set(catalog_pilot_results) != expected_catalog_result_ids:
        raise PilotError("catalog pilot reports do not match the dispatched selected entry identities")

    results: list[dict[str, Any]] = []
    selected_rows = [row for row in manifest.get("catalog_assignment_ledger", []) if row.get("selected") is True]
    selected_entry_ids = {row["registry_entry_id"] for row in selected_rows}
    if len(selected_rows) != len(selected_entry_ids) * len(PLATFORMS):
        raise PilotError("selected catalog ledger does not include exactly one row per OS")
    for row in selected_rows:
        entry_id = row["registry_entry_id"]
        pilot_result = catalog_pilot_results.get(f"catalog:{entry_id}|{row['platform']}")
        if pilot_result is not None:
            status = pilot_result.get("status", "unknown")
            reason = pilot_result.get("reason_code")
            detail = pilot_result.get("detail") or f"{row['ecosystem']} {row['platform']} catalog worker recorded an entry-bound terminal state."
            blocker_evidence = None
            attempted = pilot_result.get("attempted") is True
            observed = pilot_result
        elif row.get("registry_quality") == "known_failure":
            status = "skipped"
            reason = "catalog_entry_invalid"
            detail = "The frozen registry marks this entry invalid or unsafe; no plugin code was dispatched."
            blocker_evidence = None
            attempted = False
            observed = None
        elif row.get("artifact_status") != "locked":
            status = "skipped"
            reason = row.get("artifact_reason_code") or row.get("artifact_status") or "artifact_unavailable"
            detail = f"The frozen release/artifact lock has no executable {row['platform']} artifact for this entry."
            blocker_evidence = None
            attempted = False
            observed = None
        elif row.get("profile_status") != "locked":
            status = "skipped"
            reason = row.get("profile_reason_code") or row.get("profile_status") or "profile_unavailable"
            detail = f"The frozen {row['platform']} dependency profile did not resolve to a complete lock."
            blocker_evidence = None
            attempted = False
            observed = None
        elif row["platform"] == "windows":
            status = "unknown"
            reason = "worker_runtime_blocked" if row["ecosystem"] == "nonebot" else "windows_koishi_worker_unverified"
            attempted = False
            observed = None
            if row["ecosystem"] == "nonebot":
                detail = "Windows Sandbox blocked the unsigned CPython runtime with App Control Win32 4551 before the plugin runner started."
                blocker_evidence = "outputs/acceptance/phase2-s205-windows-blocker-20261012.json"
            else:
                detail = "No Windows Koishi catalog runner result was available for this selected entry."
                blocker_evidence = None
        elif not row.get("recipe_categories"):
            status = "skipped"
            reason = "unmapped_category"
            detail = "No conservative behavior category or entry-specific fixture is available."
            blocker_evidence = None
            attempted = False
            observed = None
        else:
            status = "skipped"
            reason = "missing_fixture"
            detail = "Recipe shape exists, but no entry-bound behavior fixture is frozen; native/federated stages were not dispatched."
            blocker_evidence = None
            attempted = False
            observed = None
        results.append({
            "result_id": f"catalog:{entry_id}|{row['platform']}",
            "unit_type": "catalog_entry",
            "registry_entry_id": entry_id,
            "ecosystem": row["ecosystem"],
            "package": row["package"],
            "version": row.get("version"),
            "platform": row["platform"],
            "status": status,
            "reason_code": reason,
            "attempted": attempted,
            "detail": detail,
            "blocker_evidence": blocker_evidence,
            "artifact_status": row["artifact_status"],
            "profile_status": row["profile_status"],
            "selection_reasons": row["selection_reasons"],
            "observed": observed,
        })

    for legacy in legacy_units:
        sample = legacy_results[legacy["sample_id"]]
        for platform in PLATFORMS:
            if platform == "windows":
                status = "unknown"
                reason = "worker_runtime_blocked" if legacy["ecosystem"] == "nonebot" else "windows_node_worker_unverified"
                detail = (
                    "The Windows CPython worker was blocked by App Control (Win32 4551) before plugin start."
                    if legacy["ecosystem"] == "nonebot"
                    else "No exact Windows Koishi native/federated run was recorded; status stays unknown."
                )
                blocker_evidence = "outputs/acceptance/phase2-s205-windows-blocker-20261012.json" if legacy["ecosystem"] == "nonebot" else None
                observed = None
            else:
                pair = sample.get("pair") or {}
                worker = sample.get("worker") or {}
                comparison = pair.get("comparison", {}).get("status")
                status = "passed" if sample.get("status") == "passed" and comparison == "passed" and worker.get("cleanup_confirmed") is True else sample.get("status", "unknown")
                reason = None if status == "passed" else worker.get("reason_code") or sample.get("stage") or "legacy_pair_failed"
                detail = "Linux Native and Federated executions independently matched their oracle and pair comparison." if status == "passed" else "Legacy Linux pair did not meet the required oracle/cleanup contract."
                blocker_evidence = None
                observed = {
                    "sample_id": legacy["sample_id"],
                    "comparison_status": comparison,
                    "cleanup_confirmed": worker.get("cleanup_confirmed"),
                    "resource_observations": sample.get("resource_observations"),
                    "artifact_verification": sample.get("artifact_verification"),
                }
            results.append({
                "result_id": f"legacy:{legacy['sample_id']}@{legacy['version']}|{platform}",
                "unit_type": "legacy_support_version",
                "sample_id": legacy["sample_id"],
                "ecosystem": legacy["ecosystem"],
                "package": legacy["package"],
                "version": legacy["version"],
                "platform": platform,
                "status": status,
                "reason_code": reason,
                "attempted": platform == "linux",
                "detail": detail,
                "blocker_evidence": blocker_evidence,
                "observed": observed,
            })

    result_ids = [item["result_id"] for item in results]
    expected_count = manifest.get("expected_pilot_result_count")
    if len(results) != expected_count or len(set(result_ids)) != len(result_ids):
        raise PilotError("S2-09 pilot results do not close the declared denominator exactly once")
    if any(item["status"] not in {"passed", "failed", "skipped", "unknown"} for item in results):
        raise PilotError("S2-09 result contains a non-terminal status")

    counts: dict[str, int] = {status: sum(item["status"] == status for item in results) for status in ("passed", "failed", "skipped", "unknown")}
    matrix: dict[str, dict[str, dict[str, int]]] = {}
    for item in results:
        cell = matrix.setdefault(item["ecosystem"], {}).setdefault(item["platform"], {status: 0 for status in counts})
        cell[item["status"]] += 1

    legacy_measurements = [
        item.get("resource_observations", {})
        for sample in legacy_results.values()
        for item in [sample]
        if isinstance(sample.get("resource_observations"), dict)
    ]
    elapsed = [float(item["worker_elapsed_seconds"]) for item in legacy_measurements if isinstance(item.get("worker_elapsed_seconds"), (int, float))]
    memory = [float(item["peak_worker_memory_bytes"]) for item in legacy_measurements if isinstance(item.get("peak_worker_memory_bytes"), (int, float))]
    payload = [float(item["worker_input_bytes"]) for item in legacy_measurements if isinstance(item.get("worker_input_bytes"), (int, float))]
    archives = [float(sample["artifact_verification"]["size_bytes"]) for sample in legacy_results.values() if isinstance(sample.get("artifact_verification", {}).get("size_bytes"), (int, float))]

    def _file_sha(path: Path) -> str:
        return _sha256(Path(path).read_bytes())

    negative_scenarios = []
    for platform, worker_doc in (("linux", linux_worker), ("windows", windows_worker)):
        for item in worker_doc.get("results", []):
            if item.get("name") in {"nonzero-exit", "timeout", "output-overflow", "memory-limit", "worker-loss"}:
                negative_scenarios.append({
                    "platform": platform,
                    "name": item.get("name"),
                    "status": item.get("status"),
                    "reason_code": item.get("reason_code"),
                    "expected": item.get("expected"),
                })
    koishi_linux_worker_runs = [
        item for item in catalog_pilot.get("results", [])
        if isinstance(item, dict) and isinstance(item.get("worker"), dict)
    ]
    koishi_windows_worker_runs = [
        item for item in windows_catalog_pilot.get("results", [])
        if isinstance(item, dict) and isinstance(item.get("worker"), dict)
    ]
    nonebot_linux_worker_runs = [
        item for item in nonebot_catalog_pilot.get("results", [])
        if isinstance(item, dict) and isinstance(item.get("worker"), dict)
    ]
    koishi_linux_protocol_reports = [
        item["worker"].get("container_protocol_report") or item["worker"].get("protocol_report")
        for item in koishi_linux_worker_runs
        if isinstance(item["worker"].get("container_protocol_report") or item["worker"].get("protocol_report"), dict)
    ]
    koishi_windows_protocol_reports = [
        item["worker"].get("protocol_report") or item["worker"].get("container_protocol_report")
        for item in koishi_windows_worker_runs
        if isinstance(item["worker"].get("protocol_report") or item["worker"].get("container_protocol_report"), dict)
    ]
    nonebot_linux_protocol_reports = [
        item["worker"].get("protocol_report")
        for item in nonebot_linux_worker_runs
        if isinstance(item["worker"].get("protocol_report"), dict)
    ]

    def _stage_counts(stage_name: str, reports: list[dict[str, Any]]) -> dict[str, int]:
        return {
            status: sum(
                isinstance(report.get(stage_name), dict) and report[stage_name].get("status") == status
                for report in reports
            )
            for status in ("passed", "failed", "skipped", "unknown")
        }

    koishi_elapsed = [item["worker"]["elapsed_seconds"] for item in koishi_linux_worker_runs if isinstance(item["worker"].get("elapsed_seconds"), (int, float))]
    koishi_memory = [item["worker"]["peak_memory_bytes"] for item in koishi_linux_worker_runs if isinstance(item["worker"].get("peak_memory_bytes"), (int, float))]
    koishi_input = [item["worker"]["input_bytes"] for item in koishi_linux_worker_runs if isinstance(item["worker"].get("input_bytes"), (int, float))]
    koishi_windows_elapsed = [item["worker"]["elapsed_seconds"] for item in koishi_windows_worker_runs if isinstance(item["worker"].get("elapsed_seconds"), (int, float))]
    koishi_windows_input = [item["worker"]["input_bytes"] for item in koishi_windows_worker_runs if isinstance(item["worker"].get("input_bytes"), (int, float))]
    koishi_windows_memory = [item["worker"]["peak_memory_bytes"] for item in koishi_windows_worker_runs if isinstance(item["worker"].get("peak_memory_bytes"), (int, float))]
    nonebot_linux_elapsed = [item["worker"]["elapsed_seconds"] for item in nonebot_linux_worker_runs if isinstance(item["worker"].get("elapsed_seconds"), (int, float))]
    nonebot_linux_memory = [item["worker"]["peak_memory_bytes"] for item in nonebot_linux_worker_runs if isinstance(item["worker"].get("peak_memory_bytes"), (int, float))]
    nonebot_linux_input = [item["worker"]["input_bytes"] for item in nonebot_linux_worker_runs if isinstance(item["worker"].get("input_bytes"), (int, float))]
    nonebot_linux_scratch = [
        item["resource_observations"]["peak_writable_scratch_bytes"] for item in nonebot_catalog_pilot.get("results", [])
        if isinstance(item, dict) and isinstance(item.get("resource_observations"), dict)
        and isinstance(item["resource_observations"].get("peak_writable_scratch_bytes"), (int, float))
    ]
    koishi_linux_scratch = [
        item["resource_observations"]["peak_writable_scratch_bytes"] for item in catalog_pilot.get("results", [])
        if isinstance(item, dict) and isinstance(item.get("resource_observations"), dict)
        and isinstance(item["resource_observations"].get("peak_writable_scratch_bytes"), (int, float))
    ]
    koishi_windows_scratch = [
        item["resource_observations"]["peak_writable_scratch_bytes"] for item in windows_catalog_pilot.get("results", [])
        if isinstance(item, dict) and isinstance(item.get("resource_observations"), dict)
        and isinstance(item["resource_observations"].get("peak_writable_scratch_bytes"), (int, float))
    ]
    nonebot_linux_dependency_archives = [
        item["dependency_archives"]["archive_bytes"] for item in nonebot_linux_worker_runs
        if isinstance(item.get("dependency_archives", {}).get("archive_bytes"), (int, float))
    ]
    nonebot_linux_plugin_archives = [item["plugin_archive_bytes"] for item in nonebot_linux_worker_runs if isinstance(item.get("plugin_archive_bytes"), (int, float))]
    koishi_linux_dependency_archives = [
        item["dependency_archives"]["archive_bytes"] for item in koishi_linux_worker_runs
        if isinstance(item.get("dependency_archives", {}).get("archive_bytes"), (int, float))
    ]
    koishi_windows_dependency_archives = [
        item["dependency_archives"]["archive_bytes"] for item in koishi_windows_worker_runs
        if isinstance(item.get("dependency_archives", {}).get("archive_bytes"), (int, float))
    ]
    koishi_linux_plugin_archives = [item["plugin_archive_bytes"] for item in koishi_linux_worker_runs if isinstance(item.get("plugin_archive_bytes"), (int, float))]
    koishi_windows_plugin_archives = [item["plugin_archive_bytes"] for item in koishi_windows_worker_runs if isinstance(item.get("plugin_archive_bytes"), (int, float))]
    dependency_download_bytes = sum(
        item.get("dependency_archives", {}).get("downloaded_bytes", 0)
        + item.get("dependency_archives", {}).get("metadata_download_bytes", 0)
        for item in [*nonebot_catalog_pilot.get("results", []), *catalog_pilot.get("results", []), *windows_catalog_pilot.get("results", [])]
        if isinstance(item, dict) and isinstance(item.get("dependency_archives"), dict)
    )
    return {
        "schema_version": 1,
        "date": "2026-10-12",
        "task_id": "S2-09",
        "kind": "phase2_pilot_acceptance",
        "status": "acceptance-pending",
        "pilot_run_status": "completed",
        "readiness": {
            "pilot_denominator": "passed",
            "negative_worker_checks": "passed" if negative_scenarios else "unknown",
            "interruption_recovery": "passed" if queue_acceptance.get("verification", {}).get("interruption_and_recovery") else "accepted_in_s207",
            "resource_budget": "provisional",
            "behavior_compatibility": "not_evaluated_without_entry_bound_fixtures",
            "windows_nonebot": "blocked_by_app_control_4551",
            "full_census": "not_started",
        },
        "pilot_manifest_path": str(Path(manifest_path).as_posix()),
        "pilot_manifest_sha256": manifest["manifest_sha256"],
        "catalog_snapshot_id": manifest["snapshot_id"],
        "denominator": {
            "full_catalog_entry_os": manifest["catalog_entry_os_denominator"],
            "full_catalog_assignment_rows": manifest["catalog_assignment_ledger_count"],
            "selected_catalog_entries": manifest["selected_catalog_entry_count"],
            "selected_catalog_entry_os": manifest["selected_catalog_entry_os_count"],
            "legacy_support_versions": manifest["legacy_support_version_count"],
            "expected_pilot_results": expected_count,
            "terminal_pilot_results": len(results),
            "closed_exactly_once": len(results) == expected_count and len(set(result_ids)) == expected_count,
        },
        "counts": counts,
        "matrix": matrix,
        "results": results,
        "worker_readiness": {
            "linux_catalog_dispatch": "partial_nonebot_koishi_l2_pilot",
            "nonebot_linux_pilot": {
                "expected_selected_entries": nonebot_catalog_pilot.get("selected_nonebot_linux_entries"),
                "terminal_results": nonebot_catalog_pilot.get("terminal_results"),
                "closed_exactly_once": nonebot_catalog_pilot.get("closed_exactly_once"),
                "unit_status_counts": nonebot_catalog_pilot.get("counts"),
                "attempted_workers": len(nonebot_linux_worker_runs),
                "dependency_install": _stage_counts("dependency_install", nonebot_linux_protocol_reports),
                "native_load": _stage_counts("native_load", nonebot_linux_protocol_reports),
                "cleanup": _stage_counts("cleanup", nonebot_linux_protocol_reports),
                "behavior": _stage_counts("behavior", nonebot_linux_protocol_reports),
                "runtime": nonebot_catalog_pilot.get("execution_scope", {}).get("profile_image"),
            },
            "koishi_linux_pilot": {
                "expected_selected_entries": catalog_pilot.get("selected_koishi_linux_entries"),
                "terminal_results": catalog_pilot.get("terminal_results"),
                "closed_exactly_once": catalog_pilot.get("closed_exactly_once"),
                "unit_status_counts": catalog_pilot.get("counts"),
                "attempted_workers": len(koishi_linux_worker_runs),
                "dependency_install": _stage_counts("dependency_install", koishi_linux_protocol_reports),
                "native_load": _stage_counts("native_load", koishi_linux_protocol_reports),
                "cleanup": _stage_counts("cleanup", koishi_linux_protocol_reports),
                "behavior": _stage_counts("behavior", koishi_linux_protocol_reports),
            },
            "koishi_windows_pilot": {
                "expected_selected_entries": windows_catalog_pilot.get("selected_koishi_windows_entries"),
                "terminal_results": windows_catalog_pilot.get("terminal_results"),
                "closed_exactly_once": windows_catalog_pilot.get("closed_exactly_once"),
                "unit_status_counts": windows_catalog_pilot.get("counts"),
                "attempted_workers": len(koishi_windows_worker_runs),
                "dependency_install": _stage_counts("dependency_install", koishi_windows_protocol_reports),
                "native_load": _stage_counts("native_load", koishi_windows_protocol_reports),
                "cleanup": _stage_counts("cleanup", koishi_windows_protocol_reports),
                "behavior": _stage_counts("behavior", koishi_windows_protocol_reports),
                "runtime": windows_catalog_pilot.get("execution_scope", {}).get("runtime"),
            },
            "linux_legacy_support_pairs": "passed" if counts["passed"] == 4 else "failed",
            "windows_nonebot": "blocked_by_app_control_4551",
            "windows_koishi": "partial_l2_pilot",
            "full_census_started": False,
        },
        "negative_scenarios": {
            "status": "passed" if negative_scenarios else "unknown",
            "worker_probes": negative_scenarios,
            "worker_reports": {
                "linux": {"path": str(Path(worker_linux_path).as_posix()), "sha256": _file_sha(worker_linux_path)},
                "windows": {"path": str(Path(worker_windows_path).as_posix()), "sha256": _file_sha(worker_windows_path)},
            },
            "interruption_recovery": {
                "status": "passed" if queue_acceptance.get("verification", {}).get("interruption_and_recovery") else "accepted_in_s207",
                "evidence": str(Path(queue_acceptance_path).as_posix()),
                "sha256": _file_sha(queue_acceptance_path),
                "scope": "S2-07 queue/reopen/recovery acceptance; S2-09 has not yet added a catalog plugin executor to the queue.",
            },
        },
        "resource_measurements": {
            "status": "partial",
            "sample_count": len(legacy_measurements) + len(nonebot_linux_worker_runs) + len(koishi_linux_worker_runs) + len(koishi_windows_worker_runs),
            "scope": "Four legacy support pairs plus attempted NoneBot Linux and Koishi Linux/Windows catalog pilot entries; Windows Job Object memory and Sandbox scratch are measured for Koishi, while NoneBot Windows remains blocked before worker start.",
            "worker_elapsed_seconds": _resource_summary(elapsed, units="seconds"),
            "peak_worker_memory_bytes": _resource_summary(memory, units="bytes; Docker stats sampled every 250ms"),
            "read_only_worker_input_bytes": _resource_summary(payload, units="bytes"),
            "plugin_archive_download_bytes": _resource_summary(archives, units="bytes; exact plugin archives only"),
            "koishi_linux_plugin_archive_bytes": _resource_summary(koishi_linux_plugin_archives, units="bytes; exact frozen plugin tarballs"),
            "koishi_linux_worker_elapsed_seconds": _resource_summary(koishi_elapsed, units="seconds"),
            "koishi_linux_peak_worker_memory_bytes": _resource_summary(koishi_memory, units="bytes; Docker stats sampled every 250ms"),
            "koishi_linux_worker_input_bytes": _resource_summary(koishi_input, units="bytes; localized package-lock and verified tarballs"),
            "koishi_linux_dependency_archive_bytes_per_entry": _resource_summary(koishi_linux_dependency_archives, units="bytes; verified lock tarballs staged per entry"),
            "koishi_windows_plugin_archive_bytes": _resource_summary(koishi_windows_plugin_archives, units="bytes; exact frozen plugin tarballs"),
            "koishi_windows_dependency_archive_bytes_per_entry": _resource_summary(koishi_windows_dependency_archives, units="bytes; verified lock tarballs staged per entry"),
            "koishi_windows_worker_elapsed_seconds": _resource_summary(koishi_windows_elapsed, units="seconds"),
            "koishi_windows_peak_worker_memory_bytes": _resource_summary(koishi_windows_memory, units="bytes; Windows Job Object peak memory"),
            "koishi_windows_worker_input_bytes": _resource_summary(koishi_windows_input, units="bytes; Node runtime, localized package-lock and verified tarballs"),
            "koishi_linux_peak_writable_scratch_bytes": _resource_summary(koishi_linux_scratch, units="bytes; Worker tmpfs sampled every 500ms inside Node pilot"),
            "koishi_windows_peak_writable_scratch_bytes": _resource_summary(koishi_windows_scratch, units="bytes; Sandbox Workspace sampled every 500ms inside Node pilot"),
            "nonebot_linux_plugin_archive_bytes": _resource_summary(nonebot_linux_plugin_archives, units="bytes; exact frozen plugin wheels"),
            "nonebot_linux_dependency_archive_bytes_per_entry": _resource_summary(nonebot_linux_dependency_archives, units="bytes; verified lock wheelhouse closure"),
            "nonebot_linux_worker_elapsed_seconds": _resource_summary(nonebot_linux_elapsed, units="seconds"),
            "nonebot_linux_peak_worker_memory_bytes": _resource_summary(nonebot_linux_memory, units="bytes; Docker stats sampled every 250ms"),
            "nonebot_linux_worker_input_bytes": _resource_summary(nonebot_linux_input, units="bytes; verified wheelhouse and lock"),
            "dependency_download_bytes": dependency_download_bytes,
            "peak_writable_scratch_bytes": _resource_summary(nonebot_linux_scratch, units="bytes; sampled every 200ms inside Linux Worker"),
            "note": "Linux catalog dispatch verifies every wheel against the frozen SHA-256 lock before staging; dependency_download_bytes includes uncached wheel archives and PyPI release metadata. NoneBot and Koishi scratch are sampled inside their isolated Workers, and Windows Koishi memory is read from the Job Object peak counter. Windows NoneBot is still blocked by App Control, so the full-catalog budget remains provisional.",
        },
        "resource_budget": {
            "path": str(Path(budget_path).as_posix()),
            "sha256": _sha256(budget_raw),
            "status": budget.get("status"),
            "limits": budget,
        },
        "previous_attempt": {
            "path": str(Path(failed_attempt_path).as_posix()),
            "sha256": _sha256(failed_raw),
            "status": failed_attempt.get("status"),
            "root_cause": "S2-08 introduced lab.recipes imports into the paired comparator, while S2-05's copied worker module closure omitted recipes.py; the bundle was fixed and all four pairs passed on rerun.",
        },
        "source_hashes": {
            "legacy_run_sha256": _sha256(legacy_raw),
            "s205_source_sha256": _sha256(s205_raw),
            "pilot_manifest_sha256": _sha256(manifest_raw),
            "worker_linux_sha256": _sha256(linux_worker_raw),
            "worker_windows_sha256": _sha256(windows_worker_raw),
            "queue_acceptance_sha256": _sha256(queue_raw),
            "budget_sha256": _sha256(budget_raw),
            "nonebot_linux_catalog_pilot_sha256": _sha256(nonebot_catalog_pilot_raw),
            "koishi_linux_catalog_pilot_sha256": _sha256(catalog_pilot_raw),
            "koishi_windows_catalog_pilot_sha256": _sha256(windows_catalog_pilot_raw),
        },
        "known_blockers": [
            f"The selected Linux NoneBot and Linux/Windows Koishi cohorts close their pilot denominators: attempted NoneBot Linux {len(nonebot_linux_worker_runs)}, Koishi Linux {len(koishi_linux_worker_runs)}, and Koishi Windows {len(koishi_windows_worker_runs)} workers. Entry-bound behavior remains skipped without fixtures.",
            "Windows Sandbox App Control Win32 4551 still blocks CPython before plugin start; signed Node.js enabled a partial Koishi L1/L2 pilot. Windows behavior compatibility remains unverified.",
            "Writable scratch and Windows Job Object memory are measured for Koishi pilot workers; Windows NoneBot has no Worker samples because App Control blocks CPython, so the full resource budget remains provisional.",
        ],
        "next": "Retain the entry-bound behavior fixture gap and App Control unknowns as gates for broader automation; use the measured Linux and Koishi resource distributions to prepare the S2-10 run budget.",
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--legacy-manifest", type=Path, default=DEFAULT_LEGACY_MANIFEST)
    parser.add_argument("--support-audit", type=Path, default=DEFAULT_SUPPORT_AUDIT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--minimum-per-ecosystem", type=int, default=30)
    parser.add_argument("--legacy-report", type=Path)
    parser.add_argument("--failed-attempt", type=Path, default=ROOT / "outputs/acceptance/phase2-s209-legacy-linux-attempt1-20261012.json")
    parser.add_argument("--nonebot-catalog-report", type=Path, default=ROOT / "outputs/acceptance/phase2-s209-nonebot-linux-pilot-20261012.json")
    parser.add_argument("--catalog-report", type=Path, default=ROOT / "outputs/acceptance/phase2-s209-koishi-linux-pilot-20261012.json")
    parser.add_argument("--windows-catalog-report", type=Path, default=ROOT / "outputs/acceptance/phase2-s209-koishi-windows-pilot-20261012.json")
    parser.add_argument("--acceptance-output", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    options = parse_args(argv)
    try:
        manifest = build_pilot_manifest(
            options.snapshot,
            legacy_manifest_path=options.legacy_manifest,
            support_audit_path=options.support_audit,
            minimum_per_ecosystem=options.minimum_per_ecosystem,
        )
        output = write_manifest(manifest, options.output)
        acceptance = None
        if options.acceptance_output:
            if not options.legacy_report:
                raise PilotError("--acceptance-output requires --legacy-report")
            acceptance = build_acceptance_report(
                output,
                options.legacy_report,
                options.failed_attempt,
                nonebot_catalog_pilot_report_path=options.nonebot_catalog_report,
                catalog_pilot_report_path=options.catalog_report,
                windows_catalog_pilot_report_path=options.windows_catalog_report,
            )
            acceptance_path = Path(options.acceptance_output).expanduser().absolute()
            acceptance_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = acceptance_path.with_suffix(acceptance_path.suffix + ".tmp")
            temporary.write_text(json.dumps(acceptance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(acceptance_path)
    except (PilotError, OSError) as error:
        print(f"pilot manifest failed: {error}")
        return 2
    print(json.dumps({
        "task_id": "S2-09",
        "status": manifest["status"],
        "manifest_sha256": manifest["manifest_sha256"],
        "catalog_entry_os_denominator": manifest["catalog_entry_os_denominator"],
        "selected_catalog_entry_os_count": manifest["selected_catalog_entry_os_count"],
        "expected_pilot_result_count": manifest["expected_pilot_result_count"],
        "selection_by_ecosystem": manifest["selection_by_ecosystem"],
        "output": str(output),
        "acceptance_output": str(acceptance_path) if options.acceptance_output else None,
        "acceptance_status": acceptance["status"] if options.acceptance_output else None,
        "terminal_counts": acceptance["counts"] if options.acceptance_output else None,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
