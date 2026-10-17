"""Resumable S2-10 catalog census execution over the durable stage queue."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from .artifacts import DEFAULT_CACHE_ROOT
from .catalog_run import CatalogRunError, create_catalog_plan, load_catalog_units, partition_units
from .queue import QueueError, StageQueue

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SNAPSHOT = ROOT / "catalogs/p2-41649744e057d61031d5"
DEFAULT_QUEUE = ROOT / ".work/catalog-census/phase2-s210.sqlite3"
DEFAULT_AGGREGATE = ROOT / "outputs/acceptance/phase2-s210-20261012.json"
STAGES = ("prepare", "install", "native", "federated", "compare", "cleanup")
TERMINAL = ("passed", "failed", "skipped", "unknown")
NEEDS_LOADER_PROFILE = {"adapter", "storage", "services", "scheduler", "console"}
CENSUS_SHARD_COUNT = 256


class CensusError(ValueError):
    """Frozen census inputs or durable run state are inconsistent."""


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _write_json(path: Path, value: dict[str, Any]) -> Path:
    path = Path(path).expanduser().absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8") + b"\n")
    temporary.replace(path)
    return path


def _read_json(path: Path) -> tuple[dict[str, Any], bytes]:
    raw = Path(path).read_bytes()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CensusError(f"census report is not valid UTF-8 JSON: {path}") from error
    if not isinstance(value, dict):
        raise CensusError(f"census report root must be an object: {path}")
    return value, raw


def _probe_for_unit(
    snapshot_dir: Path,
    unit: dict[str, Any],
    *,
    cache_root: Path,
    nonebot_context: Any,
    koishi_context: Any,
    blocker_evidence_sha256: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    ecosystem, platform = unit["ecosystem"], unit["platform"]
    if ecosystem == "nonebot" and platform == "windows":
        return {
            "status": "unknown", "reason_code": "app_control_win32_4551", "attempted": False,
            "source_probe_task_id": "S2-09",
            "blocker_evidence_path": "outputs/acceptance/phase2-s209-20261012.json",
            "blocker_evidence_sha256": blocker_evidence_sha256,
            "detail": "Accepted Windows Sandbox CPython is blocked by App Control before the catalog package can start.",
        }
    if ecosystem == "nonebot":
        from .pilot_nonebot import run_nonebot_load_probe
        value = run_nonebot_load_probe(
            snapshot_dir, unit["registry_entry_id"], cache_root=cache_root,
            timeout_seconds=timeout_seconds, context=nonebot_context,
        )
    elif ecosystem == "koishi":
        from .pilot_workers import run_koishi_load_probe, run_koishi_windows_load_probe
        probe = run_koishi_load_probe if platform == "linux" else run_koishi_windows_load_probe
        value = probe(
            snapshot_dir, unit["registry_entry_id"], cache_root=cache_root,
            timeout_seconds=timeout_seconds, _context=koishi_context,
        )
    else:
        return {"status": "unknown", "reason_code": "unsupported_census_unit", "attempted": False}
    if not isinstance(value, dict):
        return {"status": "unknown", "reason_code": "worker_report_invalid", "attempted": True}
    result = dict(value)
    result["source_probe_task_id"] = result.get("task_id", "S2-09")
    result["task_id"] = "S2-10"
    result["census_probe"] = True
    return result


def _protocol(probe: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(probe, dict):
        return None
    worker = probe.get("worker")
    if isinstance(worker, dict) and isinstance(worker.get("protocol_report"), dict):
        return worker["protocol_report"]
    return probe.get("protocol_report") if isinstance(probe.get("protocol_report"), dict) else None


def _worker_stage(probe: dict[str, Any] | None, key: str) -> dict[str, Any]:
    protocol = _protocol(probe)
    source = protocol.get(key) if protocol else None
    if isinstance(source, dict):
        status = source.get("status")
        if status not in TERMINAL:
            status = "unknown"
        reason = source.get("reason_code") if isinstance(source.get("reason_code"), str) else None
        return {"status": status, "reason_code": reason or (None if status == "passed" else f"{key}_not_passed")}
    if key == "dependency_install" and isinstance(probe, dict):
        status = probe.get("status") if probe.get("status") in TERMINAL else "unknown"
        reason = probe.get("reason_code") if isinstance(probe.get("reason_code"), str) else None
        return {"status": status, "reason_code": reason or (None if status == "passed" else "worker_install_not_passed")}
    return {"status": "unknown", "reason_code": f"{key}_evidence_missing"}


def _compact_probe(value: Any) -> Any:
    """Keep hashes, protocol outcomes, and bounded diagnostic tails in reports."""
    if isinstance(value, dict):
        compact = {}
        for key, item in value.items():
            if key in {"stdout_tail", "stderr_tail"} and isinstance(item, str):
                compact[key] = item[-1024:]
            elif key in {"command", "environment", "env"}:
                continue
            else:
                compact[key] = _compact_probe(item)
        return compact
    if isinstance(value, list):
        return [_compact_probe(item) for item in value]
    if isinstance(value, str):
        return value[:4096]
    return value


def _terminal_result(queue: StageQueue, job: dict[str, Any], worker_id: str, result: dict[str, Any]) -> None:
    if result.get("status") not in TERMINAL:
        raise CensusError(f"non-terminal stage result for {job['unit_id']}")
    queue.finish(job["job_id"], worker_id, result)


def _handle_job(
    queue: StageQueue,
    job: dict[str, Any],
    *,
    worker_id: str,
    units_by_id: dict[str, dict[str, Any]],
    entries_by_id: dict[str, dict[str, Any]],
    snapshot_dir: Path,
    cache_root: Path,
    nonebot_context: Any,
    koishi_context: Any,
    blocker_evidence_sha256: str,
    timeout_seconds: int,
) -> None:
    entry_id, stage = job["registry_entry_id"], job["stage"]
    unit, entry = units_by_id[entry_id], entries_by_id[entry_id]
    payload = job["payload"]
    if stage == "prepare":
        if entry.get("registry_quality") == "known_failure":
            result = {"status": "skipped", "reason_code": "catalog_entry_invalid", "attempted": False}
        elif unit.get("artifact_status") != "locked":
            result = {"status": "skipped", "reason_code": unit.get("artifact_reason_code") or "artifact_unavailable", "attempted": False}
        elif unit.get("profile_status") != "locked":
            result = {"status": "skipped", "reason_code": unit.get("profile_reason_code") or "profile_unavailable", "attempted": False}
        elif not all(isinstance(unit.get(key), str) and len(unit[key]) == 64 for key in (
            "source_snapshot_sha256", "artifact_lock_sha256", "profile_lock_sha256", "artifact_sha256", "dependency_lock_sha256",
        )):
            result = {"status": "failed", "reason_code": "frozen_identity_incomplete", "attempted": True}
        else:
            result = {
                "status": "passed", "reason_code": None, "attempted": True,
                "source_snapshot_sha256": unit["source_snapshot_sha256"],
                "artifact_lock_sha256": unit["artifact_lock_sha256"],
                "profile_lock_sha256": unit["profile_lock_sha256"],
                "artifact_sha256": unit["artifact_sha256"],
                "dependency_lock_sha256": unit["dependency_lock_sha256"],
            }
        _terminal_result(queue, job, worker_id, result)
        return

    if stage == "install":
        try:
            source_record = entry.get("source_record") if isinstance(entry.get("source_record"), dict) else {}
            manifest = source_record.get("manifest") if isinstance(source_record.get("manifest"), dict) else {}
            service = manifest.get("service", {})
            required_services = service.get("required", []) if isinstance(service, dict) else []
            if unit["ecosystem"] == "koishi" and (
                required_services or NEEDS_LOADER_PROFILE.intersection(unit.get("recipe_categories", []))
            ):
                probe = {
                    "task_id": "S2-10", "status": "skipped", "reason_code": "entry_loader_profile_missing",
                    "attempted": False, "required_services": required_services,
                }
            else:
                probe = _probe_for_unit(
                    snapshot_dir, unit, cache_root=cache_root, nonebot_context=nonebot_context,
                    koishi_context=koishi_context, blocker_evidence_sha256=blocker_evidence_sha256,
                    timeout_seconds=timeout_seconds,
                )
            outcome = _worker_stage(probe, "dependency_install")
            if probe.get("reason_code") == "app_control_win32_4551":
                outcome = {"status": "unknown", "reason_code": "app_control_win32_4551"}
            elif probe.get("status") == "skipped" and probe.get("attempted") is False:
                outcome = {"status": "skipped", "reason_code": probe.get("reason_code") or "stage_not_reached"}
            result = {
                **outcome, "attempted": bool(probe.get("attempted", True)),
                "probe_result": probe, "probe_result_sha256": _sha256(_canonical(probe)),
            }
        except Exception as error:
            probe = {
                "task_id": "S2-10", "status": "unknown", "reason_code": "worker_preparation_failed",
                "attempted": False, "detail": f"{type(error).__name__}: {error}"[:1024],
            }
            result = {
                "status": "unknown", "reason_code": "worker_preparation_failed", "attempted": False,
                "probe_result": probe, "probe_result_sha256": _sha256(_canonical(probe)),
            }
        _terminal_result(queue, job, worker_id, result)
        return

    install_job = queue.get_job(job["run_id"], f"{payload['unit_id']}|install")
    install_result = install_job.get("result") or {}
    probe = install_result.get("probe_result") if isinstance(install_result, dict) else None
    if stage == "native":
        outcome = _worker_stage(probe, "native_load")
        outcome.update({
            "attempted": bool(isinstance(probe, dict) and probe.get("attempted", True)),
            "probe_result_sha256": install_result.get("probe_result_sha256"),
        })
    elif stage == "federated":
        outcome = {"status": "skipped", "reason_code": "entry_bound_federated_fixture_missing", "attempted": False}
    elif stage == "compare":
        outcome = {"status": "skipped", "reason_code": "entry_bound_expected_oracle_missing", "attempted": False}
    elif stage == "cleanup":
        if not isinstance(probe, dict) or not probe.get("attempted", True):
            outcome = {"status": "skipped", "reason_code": "no_worker_process_started", "attempted": False}
        else:
            worker = probe.get("worker")
            if isinstance(worker, dict) and worker.get("cleanup_confirmed") is False:
                outcome = {"status": "unknown", "reason_code": "worker_cleanup_unconfirmed"}
            elif not _protocol(probe) and isinstance(worker, dict) and worker.get("cleanup_confirmed") is True:
                outcome = {"status": "passed", "reason_code": "worker_cleanup_confirmed_without_protocol"}
            else:
                outcome = _worker_stage(probe, "cleanup")
            outcome.update({"attempted": True, "probe_result_sha256": install_result.get("probe_result_sha256")})
    else:
        outcome = {"status": "unknown", "reason_code": "unsupported_catalog_stage", "attempted": False}
    _terminal_result(queue, job, worker_id, outcome)


def _unit_rows(queue: StageQueue, run_id: str, selected: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    jobs = queue.list_jobs(run_id)
    attempts = queue.list_attempts(run_id)
    attempts_by_unit: dict[str, list[dict[str, Any]]] = {}
    for attempt in attempts:
        attempts_by_unit.setdefault(attempt["unit_id"], []).append(attempt)
    job_by_unit = {job["unit_id"]: job for job in jobs}
    rows: list[dict[str, Any]] = []
    for unit in selected:
        entry_id = unit["registry_entry_id"]
        prefix = f"{unit['unit_id']}|"
        unit_jobs = {stage: job_by_unit[prefix + stage] for stage in STAGES}
        stage_statuses = {stage: item["status"] for stage, item in unit_jobs.items()}
        statuses = set(stage_statuses.values())
        unit_status = (
            "unknown" if any(status not in TERMINAL for status in statuses)
            else "failed" if "failed" in statuses
            else "unknown" if "unknown" in statuses
            else "skipped" if "skipped" in statuses
            else "passed"
        )
        stage_hashes = {
            stage: _sha256(_canonical(item["result"])) if item.get("result") is not None else None
            for stage, item in unit_jobs.items()
        }
        install_result = unit_jobs["install"].get("result") or {}
        probe = install_result.get("probe_result") if isinstance(install_result, dict) else None
        worker = probe.get("worker") if isinstance(probe, dict) else None
        rows.append({
            "registry_entry_id": entry_id, "result_id": f"catalog:{entry_id}|{unit['platform']}",
            "ecosystem": unit["ecosystem"], "package": unit["package"], "version": unit.get("version"),
            "platform": unit["platform"], "unit_status": unit_status,
            "reason_code": next((((job_by_unit[prefix + stage].get("result") or {}).get("reason_code"))
                                  for stage in STAGES if (job_by_unit[prefix + stage].get("result") or {}).get("reason_code")), None),
            "profile_id": unit["profile_id"], "profile_status": unit["profile_status"],
            "profile_reason_code": unit.get("profile_reason_code"),
            "artifact_status": unit["artifact_status"], "artifact_reason_code": unit.get("artifact_reason_code"),
            "artifact_sha256": unit.get("artifact_sha256"), "dependency_lock_sha256": unit.get("dependency_lock_sha256"),
            "source_snapshot_sha256": unit["source_snapshot_sha256"],
            "artifact_lock_sha256": unit["artifact_lock_sha256"], "profile_lock_sha256": unit["profile_lock_sha256"],
            "behavior_fixture_status": unit["behavior_fixture_status"], "recipe_categories": unit["recipe_categories"],
            "stage_statuses": stage_statuses, "stage_result_sha256": stage_hashes,
            "attempts": {
                stage: [{
                    "attempt_id": attempt["attempt_id"], "attempt_number": attempt["attempt_number"],
                    "worker_id": attempt["worker_id"], "status": attempt["status"],
                    "started_at": attempt["started_at"], "ended_at": attempt["ended_at"],
                    "result_sha256": _sha256(_canonical(attempt["result"])) if attempt.get("result") is not None else None,
                } for attempt in attempts_by_unit.get(prefix + stage, [])]
                for stage in STAGES
            },
            "probe_evidence": _compact_probe(probe) if isinstance(probe, dict) else None,
            "cleanup_confirmed": worker.get("cleanup_confirmed") if isinstance(worker, dict) else None,
            "resource_observations": probe.get("resource_observations") if isinstance(probe, dict) else None,
        })
    counts = Counter(row["unit_status"] for row in rows)
    stage_counts = {
        stage: {status: sum(job["stage"] == stage and job["status"] == status for job in jobs) for status in TERMINAL}
        for stage in STAGES
    }
    return rows, {"unit_status": {status: counts.get(status, 0) for status in TERMINAL}, "stage_status": stage_counts}


def run_census_shard(
    snapshot_dir: Path,
    queue_path: Path,
    *,
    platform: str,
    shard_index: int,
    shard_count: int,
    report_path: Path,
    cache_root: Path = DEFAULT_CACHE_ROOT,
    resume: bool = False,
    timeout_seconds: int = 1200,
    lease_seconds: int = 1800,
) -> dict[str, Any]:
    if platform not in {"windows", "linux"}:
        raise CensusError("platform must be windows or linux")
    snapshot_dir = Path(snapshot_dir).expanduser().resolve(strict=True)
    cache_root = Path(cache_root).expanduser().resolve()
    cache_root.mkdir(parents=True, exist_ok=True)
    plan = create_catalog_plan(
        snapshot_dir, queue_path, platforms=(platform,), shard_index=shard_index,
        shard_count=shard_count, resume=resume,
    )
    catalog, all_units = load_catalog_units(snapshot_dir, (platform,))
    selected = partition_units(all_units, shard_index, shard_count)
    queue = StageQueue(queue_path)
    queue_path = Path(queue_path).expanduser().absolute()
    entries = {item["registry_entry_id"]: item for item in catalog["entries"]}
    units = {item["registry_entry_id"]: item for item in selected}
    from .pilot_nonebot import _catalog_context
    from .pilot_workers import _load_catalog_context
    nonebot_context = (
        _catalog_context(snapshot_dir)
        if platform == "linux" and any(item["ecosystem"] == "nonebot" for item in selected)
        else None
    )
    koishi_context = (
        _load_catalog_context(snapshot_dir, platform)
        if any(item["ecosystem"] == "koishi" for item in selected)
        else None
    )
    blocker_path = ROOT / "outputs/acceptance/phase2-s209-20261012.json"
    blocker_sha = _sha256(blocker_path.read_bytes())
    worker_id = f"s210-{platform}-shard-{shard_index}-of-{shard_count}"

    while True:
        job = queue.claim(plan["run_id"], worker_id, lease_seconds=lease_seconds)
        if job is None:
            break
        try:
            _handle_job(
                queue, job, worker_id=worker_id, units_by_id=units, entries_by_id=entries,
                snapshot_dir=snapshot_dir, cache_root=cache_root, nonebot_context=nonebot_context,
                koishi_context=koishi_context, blocker_evidence_sha256=blocker_sha,
                timeout_seconds=timeout_seconds,
            )
        except Exception as error:
            queue.finish(job["job_id"], worker_id, {
                "status": "unknown", "reason_code": "census_dispatch_exception",
                "detail": f"{type(error).__name__}: {error}"[:1024],
            })

    jobs = queue.list_jobs(plan["run_id"])
    counts = queue.counts(plan["run_id"])
    rows, summaries = _unit_rows(queue, plan["run_id"], selected)
    terminal_jobs = sum(counts[status] for status in TERMINAL)
    exact_rows = len(rows) == len(selected) and len({row["result_id"] for row in rows}) == len(selected)
    report = {
        "schema_version": 1, "date": "2026-10-12", "task_id": "S2-10",
        "kind": "phase2_s210_census_shard",
        "status": "completed" if terminal_jobs == counts["total"] and exact_rows else "in-progress",
        "snapshot_id": catalog["snapshot_id"], "run_id": plan["run_id"],
        "run_manifest_sha256": plan["manifest_sha256"],
        "source_hashes": {
            "catalog_sha256": plan["manifest"]["source_snapshot_sha256"],
            "artifact_lock_sha256": plan["manifest"]["artifact_lock_sha256"],
            "profile_lock_sha256": plan["manifest"]["profile_lock_sha256"],
            "s209_blocker_evidence_sha256": blocker_sha,
        },
        "platform": platform, "shard": {"index": shard_index, "count": shard_count},
        "denominator": {
            "full_snapshot_entries": len(catalog["entries"]),
            "full_entry_os_units": 2 * len(catalog["entries"]),
            "selected_units": len(selected), "terminal_units": len(rows),
            "closed_exactly_once": exact_rows,
            "expected_stage_jobs": plan["expected_stage_jobs"],
            "terminal_stage_jobs": terminal_jobs, "all_selected_stages_terminal": terminal_jobs == counts["total"],
            "full_census_closed": False,
        },
        "queue": {
            "path": str(queue_path), "state": queue.run(plan["run_id"])["state"],
            "counts": counts, "attempt_count": len(queue.list_attempts(plan["run_id"])),
            "journal_event_count": len(queue.journal(plan["run_id"])),
        },
        "counts": summaries["unit_status"], "stage_counts": summaries["stage_status"],
        "plugin_reports": rows,
        "evidence_manifest": [
            {"result_id": row["result_id"], "registry_entry_id": row["registry_entry_id"],
             "artifact_sha256": row["artifact_sha256"], "profile_lock_sha256": row["profile_lock_sha256"],
             "stage_result_sha256": row["stage_result_sha256"], "cleanup_confirmed": row["cleanup_confirmed"]}
            for row in rows
        ],
    }
    report["report_sha256"] = _sha256(_canonical(report))
    _write_json(report_path, report)
    return report


def aggregate_census(snapshot_dir: Path, shard_reports: Iterable[Path], output: Path) -> dict[str, Any]:
    snapshot_dir = Path(snapshot_dir).expanduser().resolve(strict=True)
    catalog, expected_units = load_catalog_units(snapshot_dir, ("linux", "windows"))
    expected_by_id = {unit["unit_id"]: unit for unit in expected_units}
    rows_by_id: dict[str, dict[str, Any]] = {}
    shard_hashes: list[dict[str, str]] = []
    seen_shards: set[tuple[str, int, int]] = set()
    shard_counts: dict[str, set[int]] = {"linux": set(), "windows": set()}
    for report_path in shard_reports:
        report, raw = _read_json(report_path)
        platform, shard = report.get("platform"), report.get("shard", {})
        if report.get("task_id") != "S2-10" or report.get("kind") != "phase2_s210_census_shard":
            raise CensusError(f"not an S2-10 census shard report: {report_path}")
        if report.get("snapshot_id") != catalog["snapshot_id"] or report.get("status") != "completed":
            raise CensusError(f"incomplete or wrong-snapshot census shard: {report_path}")
        index, count = shard.get("index"), shard.get("count")
        if platform not in shard_counts or not isinstance(index, int) or not isinstance(count, int) or count < 1 or not 0 <= index < count:
            raise CensusError(f"invalid shard identity: {report_path}")
        shard_key = (platform, index, count)
        if shard_key in seen_shards:
            raise CensusError(f"duplicate census shard: {shard_key}")
        seen_shards.add(shard_key)
        shard_counts[platform].add(count)
        canonical = {key: value for key, value in report.items() if key != "report_sha256"}
        if _sha256(_canonical(canonical)) != report.get("report_sha256"):
            raise CensusError(f"census shard hash mismatch: {report_path}")
        shard_hashes.append({"path": Path(report_path).as_posix(), "sha256": _sha256(raw)})
        for row in report.get("plugin_reports", []):
            unit_id = f"{row.get('registry_entry_id')}|{platform}|catalog"
            if unit_id not in expected_by_id:
                raise CensusError(f"unexpected census unit: {unit_id}")
            if unit_id in rows_by_id:
                raise CensusError(f"duplicate census unit: {unit_id}")
            unit = expected_by_id[unit_id]
            for key in ("artifact_sha256", "profile_lock_sha256", "source_snapshot_sha256", "artifact_lock_sha256"):
                if row.get(key) != unit.get(key):
                    raise CensusError(f"census identity mismatch for {unit_id}: {key}")
            shard_units = partition_units([unit], index, count)
            if not shard_units or unit_id != shard_units[0]["unit_id"]:
                raise CensusError(f"unit is assigned to the wrong shard: {unit_id}")
            rows_by_id[unit_id] = row

    missing = sorted(set(expected_by_id) - set(rows_by_id))
    observed_shard_counts = set().union(*shard_counts.values())
    inconsistencies = []
    if len(observed_shard_counts) > 1:
        inconsistencies.append("Linux and Windows reports use inconsistent shard counts")
    expected_shard_count = next(iter(observed_shard_counts)) if len(observed_shard_counts) == 1 else CENSUS_SHARD_COUNT
    missing_shards = [
        f"{platform}:{index}/{expected_shard_count}"
        for platform in ("linux", "windows")
        for index in range(expected_shard_count)
        if (platform, index, expected_shard_count) not in seen_shards
    ]

    per_plugin: dict[str, dict[str, Any]] = {}
    for row in rows_by_id.values():
        plugin = per_plugin.setdefault(row["registry_entry_id"], {
            "registry_entry_id": row["registry_entry_id"], "ecosystem": row["ecosystem"],
            "package": row["package"], "version": row.get("version"), "platforms": {},
        })
        if row["platform"] in plugin["platforms"]:
            raise CensusError(f"duplicate platform result: {row['registry_entry_id']} {row['platform']}")
        plugin["platforms"][row["platform"]] = row
    counts = Counter(row["unit_status"] for row in rows_by_id.values())
    closed = (
        not missing and not missing_shards and not inconsistencies
        and len(rows_by_id) == len(expected_by_id)
    )
    report = {
        "schema_version": 1, "date": "2026-10-12", "task_id": "S2-10",
        "kind": "phase2_s210_census_acceptance", "status": "census-closed" if closed else "census-incomplete",
        "snapshot_id": catalog["snapshot_id"],
        "denominator": {
            "snapshot_entries": len(catalog["entries"]), "expected_entry_os_units": len(expected_by_id),
            "terminal_units": len(rows_by_id), "closed_exactly_once": closed,
            "missing_unit_count": len(missing), "duplicate_units": 0,
            "missing_shard_count": len(missing_shards), "missing_shards": missing_shards[:100],
        },
        "counts": {status: counts.get(status, 0) for status in TERMINAL},
        "readiness": {"p2_census": "passed" if closed else "pending", "p2_automation": "pending",
                      "p2_behavior": "not-evaluated", "p2_field": "not-run"},
        "known_blockers": inconsistencies, "missing_unit_ids": missing[:100],
        "per_plugin_reports": per_plugin,
        "evidence_manifest": {
            "shard_reports": shard_hashes,
            "source_snapshot_sha256": expected_units[0]["source_snapshot_sha256"],
            "artifact_lock_sha256": expected_units[0]["artifact_lock_sha256"],
            "profile_lock_sha256": expected_units[0]["profile_lock_sha256"],
        },
    }
    report["report_sha256"] = _sha256(_canonical(report))
    _write_json(output, report)
    return report


def parse_shard(value: str) -> tuple[int, int]:
    try:
        index_text, count_text = value.split("/", 1)
        index, count = int(index_text), int(count_text)
    except (ValueError, AttributeError) as error:
        raise CensusError("shard must use index/count, for example 0/64") from error
    if count < 1 or index < 0 or index >= count:
        raise CensusError("shard index must be within a positive shard count")
    return index, count


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--queue-db", type=Path, default=DEFAULT_QUEUE)
    parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    parser.add_argument("--platform", choices=("linux", "windows"))
    parser.add_argument("--shard", help="stable partition as index/count")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=1200)
    parser.add_argument("--aggregate", nargs="+", type=Path, help="aggregate completed census shard reports")
    parser.add_argument("--aggregate-output", type=Path, default=DEFAULT_AGGREGATE)
    options = parser.parse_args(argv)
    if options.aggregate:
        if options.platform is not None or options.shard is not None:
            parser.error("--aggregate cannot be combined with --platform or --shard")
        return options
    if options.platform is None or options.shard is None:
        parser.error("--platform and --shard are required for census execution")
    options.shard_index, options.shard_count = parse_shard(options.shard)
    return options


def main(argv: list[str] | None = None) -> int:
    options = parse_args(argv)
    try:
        if options.aggregate:
            report = aggregate_census(options.snapshot, options.aggregate, options.aggregate_output)
            print(json.dumps({"report": str(options.aggregate_output), "status": report["status"],
                              "denominator": report["denominator"]}, ensure_ascii=False))
            return 0 if report["denominator"]["closed_exactly_once"] else 3
        report_path = options.report or ROOT / (
            f"outputs/acceptance/phase2-s210-census-{options.platform}-shard"
            f"{options.shard_index}of{options.shard_count}.json"
        )
        report = run_census_shard(
            options.snapshot, options.queue_db, platform=options.platform,
            shard_index=options.shard_index, shard_count=options.shard_count,
            report_path=report_path, cache_root=options.cache_root, resume=options.resume,
            timeout_seconds=options.timeout_seconds,
        )
        print(json.dumps({"report": str(report_path), "run_id": report["run_id"], "status": report["status"],
                          "denominator": report["denominator"], "counts": report["counts"],
                          "stage_counts": report["stage_counts"]}, ensure_ascii=False))
        return 0 if report["status"] == "completed" else 3
    except (CensusError, CatalogRunError, QueueError, OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
        print(f"S2-10 census failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
