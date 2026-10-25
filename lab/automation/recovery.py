"""Frozen Linux recovery pilot selection and execution on a new fenced campaign."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from lab.artifacts import DEFAULT_CACHE_ROOT, _verify_upstream_digest, sha256_file
from lab.automation.circuit_breaker import BackendCircuitBreaker, CircuitPolicy, CircuitOpen
from lab.automation.dispatcher import Dispatcher
from lab.automation.health import collect_linux_health
from lab.automation.reconcile import DockerReconciler, reconcile_expired_attempts
from lab.automation.selection import ROOT, AutomationContractError, object_sha256, sha256, validate_manifest
from lab.catalog_run import load_catalog_units
from lab.pilot_nonebot import _catalog_context, run_nonebot_load_probe
from lab.pilot_workers import _load_catalog_context, run_koishi_load_probe
from lab.queue import StageQueue


class RecoveryPilotError(ValueError):
    """A frozen pilot identity or execution report is invalid."""


def _verify_pilot_worker_images(worker_images: dict[str, str]) -> list[dict[str, str]]:
    docker = shutil.which("docker") or shutil.which("docker.exe")
    if not docker:
        raise RecoveryPilotError("Docker CLI is unavailable for pilot worker image preflight")
    results = []
    for ecosystem in ("nonebot", "koishi"):
        reference = worker_images.get(ecosystem)
        match = re.fullmatch(r"[^\s@]+@sha256:([a-f0-9]{64})", reference or "")
        if not match:
            raise RecoveryPilotError(f"{ecosystem} pilot image is not digest pinned")
        try:
            inspect = subprocess.run(
                [docker, "image", "inspect", reference, "--format", "{{.Id}}"],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RecoveryPilotError(f"{ecosystem} pilot image inspection failed: {type(error).__name__}") from error
        expected_id = f"sha256:{match.group(1)}"
        actual_id = inspect.stdout.strip()
        if inspect.returncode != 0 or actual_id != expected_id:
            raise RecoveryPilotError(f"{ecosystem} pilot image is unavailable or mismatched: {reference}")
        results.append({"ecosystem": ecosystem, "reference": reference, "image_id": actual_id, "status": "present"})
    return results


def _read_json(path: Path) -> tuple[dict[str, Any], bytes]:
    raw = path.read_bytes()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RecoveryPilotError(f"invalid UTF-8 JSON: {path}") from error
    if not isinstance(value, dict):
        raise RecoveryPilotError(f"expected an object: {path}")
    return value, raw


def _write_json(path: Path, value: dict[str, Any]) -> Path:
    path = Path(path).expanduser().absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    temporary.replace(path)
    return path


def _entry_identity(unit: dict[str, Any], snapshot_id: str) -> dict[str, Any]:
    return {
        "snapshot_id": snapshot_id,
        "registry_entry_id": unit["registry_entry_id"],
        "ecosystem": unit["ecosystem"],
        "package": unit["package"],
        "version": unit.get("version"),
        "platform": "linux",
        "profile_id": unit["profile_id"],
        "profile_lock_sha256": unit["profile_lock_sha256"],
        "artifact_sha256": unit["artifact_sha256"],
        "dependency_lock_sha256": unit["dependency_lock_sha256"],
    }


def _source_shard(entry: dict[str, Any], fallback_path: Path | None = None) -> dict[str, Any]:
    source_path = fallback_path or (ROOT / entry["source"]["shard_report_path"])
    raw = source_path.read_bytes()
    value, _ = _read_json(source_path)
    row = next((item for item in value.get("plugin_reports", []) if item.get("registry_entry_id") == entry["identity"]["registry_entry_id"]), None)
    if row is None:
        raise RecoveryPilotError(f"source shard has no row for {entry['identity']['registry_entry_id']}")
    return {
        "task_id": "S2-11",
        "report_path": source_path.relative_to(ROOT).as_posix(),
        "report_sha256": sha256(raw),
        "result_id": entry.get("result_id", f"catalog:{entry['identity']['registry_entry_id']}|linux"),
        "unit_status": row.get("unit_status"),
        "stage_statuses": row.get("stage_statuses", {}),
        "reason_code": row.get("reason_code"),
        "failure_domain": entry.get("source", {}).get("attribution_domain"),
        "cleanup_confirmed": row.get("cleanup_confirmed"),
    }


def build_linux_pilot_manifest(
    recovery_manifest_path: Path,
    snapshot_dir: Path = ROOT / "catalogs/p2-41649744e057d61031d5",
    reports_dir: Path = ROOT / "outputs/acceptance",
    *,
    recovery_per_ecosystem: int = 8,
    failed_controls_per_ecosystem: int = 2,
    worker_images: dict[str, str] | None = None,
    worker_policy_id: str = "s205-locked-profile-images-v1",
    archive_cache_root: Path | None = None,
    worker_timeout_seconds: int = 240,
    probe_semantics_version: int = 2,
) -> dict[str, Any]:
    """Freeze 8 affected units plus 2 historical plugin-failure controls per ecosystem."""
    source_manifest, source_raw = _read_json(recovery_manifest_path)
    validate_manifest(source_manifest, verify_source_files=True)
    catalog, all_units = load_catalog_units(snapshot_dir, ("linux",))
    snapshot_id = catalog["snapshot_id"]
    if snapshot_id != source_manifest["snapshot_id"]:
        raise RecoveryPilotError("recovery manifest and catalog snapshot differ")
    if worker_timeout_seconds < 30 or worker_timeout_seconds > 1200:
        raise RecoveryPilotError("pilot worker timeout must be between 30 and 1200 seconds")
    if probe_semantics_version not in {1, 2}:
        raise RecoveryPilotError("pilot probe semantics version is unsupported")
    units_by_entry = {item["registry_entry_id"]: item for item in all_units}
    selected_source = [
        row for row in source_manifest["selection"]
        if row["identity"]["platform"] == "linux"
        and row["identity"]["ecosystem"] in {"nonebot", "koishi"}
        and units_by_entry.get(row["identity"]["registry_entry_id"], {}).get("artifact_status") == "locked"
        and units_by_entry.get(row["identity"]["registry_entry_id"], {}).get("profile_status") == "locked"
    ]

    outputs: list[dict[str, Any]] = []
    for ecosystem in ("nonebot", "koishi"):
        candidates = [row for row in selected_source if row["identity"]["ecosystem"] == ecosystem]
        strata: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in candidates:
            domain = row.get("source", {}).get("attribution_domain") or "unclassified_infrastructure"
            strata[domain].append(row)
        for values in strata.values():
            values.sort(key=lambda item: item["identity"]["registry_entry_id"])
        recovery: list[dict[str, Any]] = []
        for domain in sorted(strata):
            if len(recovery) < recovery_per_ecosystem:
                recovery.append(strata[domain][0])
        already = {item["identity"]["registry_entry_id"] for item in recovery}
        for row in sorted(candidates, key=lambda item: item["identity"]["registry_entry_id"]):
            if len(recovery) >= recovery_per_ecosystem:
                break
            if row["identity"]["registry_entry_id"] not in already:
                recovery.append(row)
                already.add(row["identity"]["registry_entry_id"])
        if len(recovery) != recovery_per_ecosystem:
            raise RecoveryPilotError(f"not enough locked recovery candidates for {ecosystem}")
        for row in recovery:
            unit = units_by_entry[row["identity"]["registry_entry_id"]]
            outputs.append({
                "selection_kind": "affected_recovery",
                "identity": _entry_identity(unit, snapshot_id),
                "identity_sha256": object_sha256(_entry_identity(unit, snapshot_id)),
                "selection_reason": row.get("selection_reasons", []),
                "source": _source_shard(row),
            })

        failed: list[tuple[dict[str, Any], Path]] = []
        for report_path in sorted(reports_dir.glob("phase2-s211-rerun-census-linux-shard*of256-20261014.json")):
            report, _ = _read_json(report_path)
            if report.get("snapshot_id") != snapshot_id or report.get("platform") != "linux":
                continue
            for report_row in report.get("plugin_reports", []):
                if report_row.get("ecosystem") == ecosystem and report_row.get("unit_status") == "failed" and report_row.get("cleanup_confirmed") is True:
                    entry_id = report_row.get("registry_entry_id")
                    if (
                        entry_id in units_by_entry and entry_id not in already
                        and units_by_entry[entry_id].get("artifact_status") == "locked"
                        and units_by_entry[entry_id].get("profile_status") == "locked"
                    ):
                        failed.append((report_row, report_path))
        failed.sort(key=lambda pair: pair[0].get("registry_entry_id", ""))
        if len(failed) < failed_controls_per_ecosystem:
            raise RecoveryPilotError(f"not enough prior plugin-failure controls for {ecosystem}")
        for report_row, report_path in failed[:failed_controls_per_ecosystem]:
            unit = units_by_entry[report_row["registry_entry_id"]]
            control = {
                "selection_kind": "prior_plugin_failure_control",
                "identity": _entry_identity(unit, snapshot_id),
                "identity_sha256": object_sha256(_entry_identity(unit, snapshot_id)),
                "selection_reason": ["prior_native_failure_control"],
                "source": _source_shard({
                    "identity": _entry_identity(unit, snapshot_id),
                    "result_id": f"catalog:{unit['registry_entry_id']}|linux",
                }, fallback_path=report_path),
            }
            outputs.append(control)

    identities = [item["identity"]["registry_entry_id"] for item in outputs]
    if len(identities) != len(set(identities)):
        raise RecoveryPilotError("pilot selection contains duplicate registry entries")
    source_digest = sha256(source_raw)
    if worker_images is None:
        image_lock, _ = _read_json(ROOT / "lab/baselines/runtime-images.lock.json")
        worker_images = image_lock["profile_images"]
    for ecosystem in ("nonebot", "koishi"):
        image = worker_images.get(ecosystem)
        if not isinstance(image, str) or "@sha256:" not in image:
            raise RecoveryPilotError(f"{ecosystem} worker image must be digest pinned")
    archive_size_overrides = None
    if archive_cache_root is not None:
        archive_size_overrides = _build_archive_size_overrides(
            outputs, snapshot_dir=snapshot_dir, cache_root=archive_cache_root,
            expected_artifact_lock_sha256=source_manifest["source_identity"]["artifact_lock_sha256"],
        )
    core = {
        "schema_version": 1,
        "contract": "phase3-linux-recovery-pilot-v1",
        "purpose": "recovery",
        "snapshot_id": snapshot_id,
        "platform": "linux",
        "source_recovery_manifest_sha256": source_digest,
        "source_identity": source_manifest["source_identity"],
        "selection_algorithm": "per-ecosystem: attribution-domain-stratified recovery candidates, then entry-id order; 2 cleanup-confirmed native-failure controls",
        "expected_units": len(outputs),
        "recovery_per_ecosystem": recovery_per_ecosystem,
        "failed_controls_per_ecosystem": failed_controls_per_ecosystem,
        "stage_scope": ["prepare", "install", "native", "cleanup"],
        "stage_projection": "cleanup-evidence-on-cleanup-stage-only-v2",
        "probe_semantics_version": probe_semantics_version,
        "worker_limits": {"timeout_seconds": worker_timeout_seconds, "memory_mb": 4096, "cpu_cores": 2, "process_limit": 256},
        "worker_policy": worker_policy_id,
        "worker_images": worker_images,
        **({"archive_size_overrides": archive_size_overrides} if archive_size_overrides is not None else {}),
        "selection": outputs,
    }
    campaign_seed = object_sha256(core)
    manifest = {"campaign_id": f"p3-recovery-linux-{campaign_seed[:12]}", **core}
    manifest["manifest_sha256"] = object_sha256(manifest)
    return manifest


def validate_linux_pilot_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    if manifest.get("contract") != "phase3-linux-recovery-pilot-v1" or manifest.get("platform") != "linux":
        raise RecoveryPilotError("not a Linux recovery pilot manifest")
    rows = manifest.get("selection")
    if not isinstance(rows, list) or manifest.get("expected_units") != len(rows):
        raise RecoveryPilotError("pilot selection denominator mismatch")
    if manifest.get("probe_semantics_version", 1) not in {1, 2}:
        raise RecoveryPilotError("pilot probe semantics version is unsupported")
    worker_images = manifest.get("worker_images", {})
    if any(not isinstance(worker_images.get(ecosystem), str) or "@sha256:" not in worker_images[ecosystem] for ecosystem in ("nonebot", "koishi")):
        raise RecoveryPilotError("pilot worker image identities must be digest pinned")
    ids: set[str] = set()
    counts = {"nonebot": defaultdict(int), "koishi": defaultdict(int)}
    for row in rows:
        identity = row.get("identity", {})
        entry_id = identity.get("registry_entry_id")
        if identity.get("snapshot_id") != manifest.get("snapshot_id") or identity.get("platform") != "linux" or identity.get("ecosystem") not in counts:
            raise RecoveryPilotError("pilot selection identity mismatch")
        if entry_id in ids or row.get("identity_sha256") != object_sha256(identity):
            raise RecoveryPilotError("pilot selection contains duplicate or modified identity")
        ids.add(entry_id)
        counts[identity["ecosystem"]][row.get("selection_kind")] += 1
    overrides = manifest.get("archive_size_overrides")
    if overrides is not None:
        override_rows = overrides.get("entries") if isinstance(overrides, dict) else None
        if (
            not isinstance(overrides, dict)
            or manifest.get("stage_projection") != "cleanup-evidence-on-cleanup-stage-only-v2"
            or overrides.get("schema_version") != 1
            or not isinstance(overrides.get("base_artifact_lock_sha256"), str)
            or len(overrides["base_artifact_lock_sha256"]) != 64
            or not isinstance(override_rows, list)
            or len(override_rows) != len(rows)
        ):
            raise RecoveryPilotError("archive size override contract is invalid")
        by_id = {item.get("registry_entry_id"): item for item in override_rows if isinstance(item, dict)}
        if len(by_id) != len(override_rows) or set(by_id) != ids:
            raise RecoveryPilotError("archive size overrides do not cover the pilot selection")
        for row in rows:
            identity = row["identity"]
            override = by_id[identity["registry_entry_id"]]
            if (
                override.get("artifact_sha256") != identity.get("artifact_sha256")
                or not isinstance(override.get("cache_path"), str)
                or not override["cache_path"]
                or not isinstance(override.get("archive_size_bytes"), int)
                or override["archive_size_bytes"] < 1
                or not isinstance(override.get("metadata_size_bytes"), int)
                or override["metadata_size_bytes"] < 1
            ):
                raise RecoveryPilotError(f"archive size override identity is invalid for {identity['registry_entry_id']}")
    for ecosystem in counts:
        if counts[ecosystem]["affected_recovery"] != manifest.get("recovery_per_ecosystem") or counts[ecosystem]["prior_plugin_failure_control"] != manifest.get("failed_controls_per_ecosystem"):
            raise RecoveryPilotError(f"pilot selection strata incomplete for {ecosystem}")
    worker_limits = manifest.get("worker_limits")
    if worker_limits is not None and (
        not isinstance(worker_limits, dict)
        or not isinstance(worker_limits.get("timeout_seconds"), int)
        or not 30 <= worker_limits["timeout_seconds"] <= 1200
        or worker_limits.get("memory_mb") != 4096
        or worker_limits.get("cpu_cores") != 2
        or worker_limits.get("process_limit") != 256
    ):
        raise RecoveryPilotError("pilot worker limits are invalid")
    digest = object_sha256({key: value for key, value in manifest.items() if key != "manifest_sha256"})
    if manifest.get("manifest_sha256") != digest:
        raise RecoveryPilotError("pilot manifest hash mismatch")
    return {"status": "valid", "campaign_id": manifest["campaign_id"], "manifest_sha256": digest, "selected_units": len(rows), "counts": {key: dict(value) for key, value in counts.items()}}


def _build_archive_size_overrides(
    selection: list[dict[str, Any]], *, snapshot_dir: Path, cache_root: Path,
    expected_artifact_lock_sha256: str,
) -> dict[str, Any]:
    lock, lock_raw = _read_json(snapshot_dir / "artifacts.lock.json")
    lock_sha256 = sha256(lock_raw)
    if lock_sha256 != expected_artifact_lock_sha256:
        raise RecoveryPilotError("artifact lock differs from the frozen recovery source identity")
    cache_root = Path(cache_root).expanduser().resolve(strict=True)
    entries = {item["registry_entry_id"]: item for item in lock.get("entries", [])}
    overrides = []
    for selected in selection:
        identity = selected["identity"]
        entry_id = identity["registry_entry_id"]
        artifact_entry = entries.get(entry_id, {})
        artifact_platform = "all" if identity["ecosystem"] == "koishi" else "linux-x86_64"
        record = next((item for item in artifact_entry.get("artifacts", []) if item.get("platform") == artifact_platform), {})
        artifact = record.get("artifact") or {}
        relative = Path(str(artifact.get("cache_path", "")))
        if relative.is_absolute() or ".." in relative.parts:
            raise RecoveryPilotError(f"unsafe cache path for {entry_id}")
        path = (cache_root / relative).resolve(strict=True)
        if not path.is_file() or not path.is_relative_to(cache_root):
            raise RecoveryPilotError(f"locked archive is missing or escapes the cache for {entry_id}")
        expected_sha256 = artifact.get("computed_digests", {}).get("sha256")
        if expected_sha256 != identity.get("artifact_sha256") or sha256_file(path) != expected_sha256:
            raise RecoveryPilotError(f"locked archive SHA-256 mismatch for {entry_id}")
        try:
            _verify_upstream_digest(path, artifact.get("upstream_digests", []))
        except Exception as error:
            raise RecoveryPilotError(f"locked archive upstream digest mismatch for {entry_id}") from error
        metadata_size = artifact.get("size_bytes")
        if not isinstance(metadata_size, int) or metadata_size < 1:
            raise RecoveryPilotError(f"locked archive has no positive metadata size for {entry_id}")
        overrides.append({
            "registry_entry_id": entry_id,
            "artifact_sha256": expected_sha256,
            "cache_path": relative.as_posix(),
            "metadata_size_bytes": metadata_size,
            "archive_size_bytes": path.stat().st_size,
        })
    return {"schema_version": 1, "base_artifact_lock_sha256": lock_sha256, "entries": overrides}


def _recovery_jobs(unit: dict[str, Any]) -> list[dict[str, Any]]:
    prefix = unit["unit_id"]
    common = {
        "registry_entry_id": unit["registry_entry_id"], "ecosystem": unit["ecosystem"],
        "platform": "linux", "profile_id": unit["profile_id"], "payload": unit,
        "case_id": "linux-recovery-pilot", "max_attempts": 1,
    }
    return [
        {**common, "unit_id": f"{prefix}|prepare", "stage": "prepare"},
        {**common, "unit_id": f"{prefix}|install", "stage": "install", "dependencies": [f"{prefix}|prepare"]},
        {**common, "unit_id": f"{prefix}|native", "stage": "native", "dependencies": [f"{prefix}|install"]},
        {**common, "unit_id": f"{prefix}|cleanup", "stage": "cleanup", "dependencies": [f"{prefix}|install", f"{prefix}|native"], "always_run": True},
    ]


def _project_worker_stage_result(stage_name: str, probe: Any, probe_result_sha256: str | None) -> dict[str, Any]:
    if not isinstance(probe, dict):
        return {
            "status": "unknown", "reason_code": "worker_probe_missing", "attempted": True,
            "probe_result_sha256": probe_result_sha256,
            **({"cleanup_confirmed": False} if stage_name == "cleanup" else {}),
        }
    worker = probe.get("worker", {})
    worker = worker if isinstance(worker, dict) else {}
    protocol = worker.get("protocol_report")
    key = {"native": "native_load", "cleanup": "cleanup"}[stage_name]
    stage = protocol.get(key, {}) if isinstance(protocol, dict) else {}
    stage = stage if isinstance(stage, dict) else {}
    status = stage.get("status") if stage.get("status") in {"passed", "failed", "skipped", "unknown"} else "unknown"
    reason = stage.get("reason_code") or worker.get("reason_code") or (None if status == "passed" else f"{stage_name}_evidence_missing")
    result = {"status": status, "reason_code": reason, "attempted": True, "probe_result_sha256": probe_result_sha256}
    if stage_name == "cleanup":
        cleanup_confirmed = worker.get("cleanup_confirmed") is True
        result["cleanup_confirmed"] = cleanup_confirmed
        if not cleanup_confirmed:
            result["status"], result["reason_code"] = "unknown", "worker_cleanup_unconfirmed"
    return result


def _locked_input_inventory(manifest: dict[str, Any], snapshot_dir: Path, cache_root: Path) -> dict[str, Any]:
    artifacts, artifact_lock_raw = _read_json(snapshot_dir / "artifacts.lock.json")
    overrides_contract = manifest.get("archive_size_overrides")
    if overrides_contract is not None and overrides_contract.get("base_artifact_lock_sha256") != sha256(artifact_lock_raw):
        raise RecoveryPilotError("archive size overrides are bound to a different artifact lock")
    overrides = {
        item["registry_entry_id"]: item
        for item in (overrides_contract or {}).get("entries", [])
    }
    profiles, _ = _read_json(snapshot_dir / "profiles.lock.json")
    artifacts_by_id = {item["registry_entry_id"]: item for item in artifacts["entries"]}
    profiles_by_id = {item["registry_entry_id"]: item for item in profiles["entries"]}
    records = []
    for selection in manifest["selection"]:
        identity = selection["identity"]
        entry_id, ecosystem = identity["registry_entry_id"], identity["ecosystem"]
        artifact_entry = artifacts_by_id.get(entry_id, {})
        profile_entry = profiles_by_id.get(entry_id, {})
        platform_id = "linux-x86_64"
        artifact_platform = "all" if ecosystem == "koishi" else platform_id
        artifact_record = next((item for item in artifact_entry.get("artifacts", []) if item.get("platform") == artifact_platform), {})
        artifact = artifact_record.get("artifact") or {}
        override = overrides.get(entry_id)
        if override is not None and (
            override.get("artifact_sha256") != artifact.get("computed_digests", {}).get("sha256")
            or override.get("cache_path") != artifact.get("cache_path")
        ):
            raise RecoveryPilotError(f"archive size override does not match the locked artifact for {entry_id}")
        profile = profile_entry.get("profiles", {}).get(platform_id, {})
        lock_ref = profile.get("dependency_lock") or {}
        checks = []
        for name, relative, expected_sha, expected_size in (
            (
                "artifact", artifact.get("cache_path"), artifact.get("computed_digests", {}).get("sha256"),
                override.get("archive_size_bytes") if override is not None else artifact.get("size_bytes"),
            ),
            ("dependency_lock", lock_ref.get("cache_path"), lock_ref.get("sha256"), lock_ref.get("size_bytes")),
        ):
            path = cache_root / relative if isinstance(relative, str) else None
            exists = path is not None and path.is_file()
            hash_matches = bool(exists and expected_sha and sha256_file(path) == expected_sha)
            size_matches = bool(exists and (expected_size is None or path.stat().st_size == expected_size))
            checks.append({
                "name": name, "path": relative, "expected_sha256": expected_sha,
                "expected_size_bytes": expected_size,
                "metadata_size_bytes": override.get("metadata_size_bytes") if name == "artifact" and override is not None else expected_size,
                "actual_size_bytes": path.stat().st_size if exists else None,
                "size_basis": "sha256_verified_cache_archive" if name == "artifact" and override is not None else "frozen_lock",
                "exists": exists, "hash_matches": hash_matches, "size_matches": size_matches,
            })
        records.append({"registry_entry_id": entry_id, "ecosystem": ecosystem, "checks": checks, "ready": all(item["exists"] and item["hash_matches"] and item["size_matches"] for item in checks)})
    return {"ready": all(item["ready"] for item in records), "ready_units": sum(item["ready"] for item in records), "expected_units": len(records), "units": records}


def _checkpoint_report(manifest: dict[str, Any], queue: StageQueue | None, *, cache_inventory: dict[str, Any], admission_fingerprint: str | None, blocker: str) -> dict[str, Any]:
    queue_jobs = {job["unit_id"]: job for job in queue.list_jobs(manifest["campaign_id"])} if queue else {}
    attempts = queue.list_attempts(manifest["campaign_id"]) if queue else []
    rows = []
    for selected in manifest["selection"]:
        identity = selected["identity"]
        entry_id = identity["registry_entry_id"]
        prefix = f"{entry_id}|linux|catalog|"
        stages = {stage: queue_jobs.get(prefix + stage) for stage in ("prepare", "install", "native", "cleanup")}
        statuses = {stage: (job["status"] if job else "queued") for stage, job in stages.items()}
        results = {stage: ((job or {}).get("result") or {}) for stage, job in stages.items()}
        required = ("install", "native", "cleanup")
        not_run = any(statuses[stage] == "queued" for stage in required)
        unknown = any(statuses[stage] == "unknown" for stage in required)
        failed = any(statuses[stage] == "failed" for stage in required)
        cleanup = (results["cleanup"].get("reconciliation") or {}).get("cleanup_confirmed")
        if cleanup is None:
            cleanup_attempt = next(
                (item for item in attempts if item["unit_id"] == prefix + "cleanup" and (item.get("result") or {}).get("reconciliation")),
                None,
            )
            if cleanup_attempt is not None:
                cleanup = cleanup_attempt["result"]["reconciliation"].get("cleanup_confirmed")
        if cleanup is None:
            cleanup = results["cleanup"].get("cleanup_confirmed")
        recovery_status = "unknown" if unknown or (not not_run and cleanup is not True) else "not_run" if not_run else "failed" if failed else "recovered"
        previous = selected["source"]
        rows.append({
            "registry_entry_id": entry_id, "ecosystem": identity["ecosystem"], "package": identity["package"],
            "version": identity.get("version"), "platform": "linux", "selection_kind": selected["selection_kind"],
            "identity_sha256": selected["identity_sha256"], "source": previous,
            "old_to_new": {"old_stage_statuses": previous["stage_statuses"], "new_stage_statuses": statuses},
            "attempts": [item for item in attempts if item["unit_id"].startswith(prefix)],
            "cleanup_confirmed": cleanup if isinstance(cleanup, bool) else None,
            "recovery_status": recovery_status,
            "stage_result_sha256": {stage: object_sha256(results[stage]) if results[stage] else None for stage in stages},
        })
    terminal_units = sum(all(row["old_to_new"]["new_stage_statuses"][stage] in {"passed", "failed", "skipped", "unknown"} for stage in ("install", "native", "cleanup")) for row in rows)
    state = queue.run(manifest["campaign_id"])["state"] if queue else "not_created"
    return {
        "schema_version": 1, "kind": "phase3_linux_recovery_pilot", "campaign_id": manifest["campaign_id"],
        "manifest_sha256": manifest["manifest_sha256"], "snapshot_id": manifest["snapshot_id"], "platform": "linux",
        "admission_fingerprint": admission_fingerprint, "queue_state": state,
        "status": "interrupted" if queue and terminal_units else "blocked" if not queue else "incomplete",
        "execution_started": bool(attempts),
        "denominator": {"expected_units": len(rows), "terminal_units": terminal_units, "required_stage_positions": len(rows) * 3},
        "counts": {
            "recovered": sum(row["recovery_status"] == "recovered" for row in rows),
            "plugin_or_stage_failed": sum(row["recovery_status"] == "failed" for row in rows),
            "unknown_or_cleanup_unconfirmed": sum(row["recovery_status"] == "unknown" for row in rows),
            "not_run": sum(row["recovery_status"] == "not_run" for row in rows),
            "cleanup_confirmed": sum(row["cleanup_confirmed"] is True for row in rows),
        },
        "cache_inventory": cache_inventory,
        "blockers": [blocker],
        "gate": {"scope": "linux pilot only; not full P3-RECOVERY", "health": "blocked", "plugin_compatibility": "not_evaluated"},
        "plugin_reports": rows,
    }


def execute_linux_recovery_pilot(
    manifest_path: Path,
    admission_path: Path,
    *,
    snapshot_dir: Path = ROOT / "catalogs/p2-41649744e057d61031d5",
    cache_root: Path = DEFAULT_CACHE_ROOT,
    evidence_root: Path = Path("../Ligamina-automation-evidence"),
    timeout_seconds: int | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Run a frozen 20-unit Linux pilot in a new fenced queue and emit old→new rows."""
    manifest, manifest_raw = _read_json(manifest_path)
    validation = validate_linux_pilot_manifest(manifest)
    frozen_timeout = manifest.get("worker_limits", {}).get("timeout_seconds", 1200)
    if timeout_seconds is None:
        timeout_seconds = frozen_timeout
    elif timeout_seconds != frozen_timeout:
        raise RecoveryPilotError("execution timeout differs from the pilot manifest")
    at = time.time() if now is None else now
    admission, _ = _read_json(admission_path)
    if admission.get("status") != "admitted" or admission.get("platform") != "linux":
        raise RecoveryPilotError("Linux Worker admission is not admitted")
    if at - float(admission.get("collected_at_epoch", 0)) > 1800:
        raise RecoveryPilotError("Linux Worker admission is older than the 30-minute TTL")
    probe_path = Path(admission.get("facts", {}).get("probe", {}).get("report_path", ""))
    if not probe_path.is_file():
        raise RecoveryPilotError("admission probe report is unavailable")
    fresh_health = collect_linux_health(probe_report_path=probe_path)
    if fresh_health.get("status") != "admitted":
        raise RecoveryPilotError("current Linux Worker health is blocked: " + ",".join(fresh_health.get("reason_codes", [])))
    worker_image_inventory = _verify_pilot_worker_images(manifest["worker_images"])
    if object_sha256({key: value for key, value in manifest.items() if key != "manifest_sha256"}) != manifest["manifest_sha256"]:
        raise RecoveryPilotError("pilot manifest identity hash mismatch")

    catalog, units_all = load_catalog_units(snapshot_dir, ("linux",))
    units_by_entry = {item["registry_entry_id"]: item for item in units_all}
    entries = {item["registry_entry_id"]: item for item in catalog["entries"]}
    selected_units = []
    for row in manifest["selection"]:
        entry_id = row["identity"]["registry_entry_id"]
        unit = units_by_entry[entry_id]
        if row["identity"] != _entry_identity(unit, manifest["snapshot_id"]):
            raise RecoveryPilotError(f"frozen catalog identity changed for {entry_id}")
        if unit.get("artifact_status") != "locked" or unit.get("profile_status") != "locked":
            raise RecoveryPilotError(f"entry is not locked on Linux: {entry_id}")
        selected_units.append({**unit, "pilot_selection_kind": row["selection_kind"], "source": row["source"]})

    cache_inventory = _locked_input_inventory(manifest, snapshot_dir, cache_root)
    queue_path = ROOT / ".work" / "automation" / manifest["campaign_id"] / "linux" / "queue.sqlite3"
    campaign_dir = (evidence_root / manifest["campaign_id"] / "linux").resolve()
    if ROOT.resolve() == campaign_dir or ROOT.resolve() in campaign_dir.parents:
        raise RecoveryPilotError("automation evidence must be outside the repository")
    report_path = campaign_dir / "recovery-report.json"
    if report_path.exists() and cache_inventory["ready"]:
        raise RecoveryPilotError(f"refusing to overwrite sealed recovery report: {report_path}")
    if not cache_inventory["ready"]:
        existing_queue = StageQueue(queue_path) if queue_path.is_file() else None
        if existing_queue is not None:
            if existing_queue.run(manifest["campaign_id"])["state"] != "interrupted":
                existing_queue.interrupt_run(manifest["campaign_id"], now=at)
            reconciler = DockerReconciler()
            for attempt in existing_queue.list_attempts(manifest["campaign_id"]):
                result = attempt.get("result") or {}
                if attempt["status"] != "unknown" or not (
                    result.get("reason_code") == "dispatcher_executor_error"
                    or result.get("cleanup_confirmed") is False
                ) or result.get("reconciliation"):
                    continue
                job = existing_queue.get_job(manifest["campaign_id"], attempt["unit_id"])
                evidence = reconciler.reconcile(
                    campaign_id=manifest["campaign_id"], job_id=job["job_id"],
                    attempt_id=attempt["attempt_id"], generation=attempt["attempt_number"],
                ).as_evidence()
                existing_queue.record_reconciliation(
                    manifest["campaign_id"], attempt["unit_id"], attempt["attempt_id"], evidence, now=at,
                )
        checkpoint = _checkpoint_report(
            manifest, existing_queue, cache_inventory=cache_inventory,
            admission_fingerprint=fresh_health["environment_fingerprint"],
            blocker="frozen_artifact_or_dependency_lock_missing_from_local_cache",
        )
        checkpoint["queue_path"] = queue_path.as_posix() if existing_queue is not None else None
        checkpoint["report_sha256"] = object_sha256(checkpoint)
        campaign_dir.mkdir(parents=True, exist_ok=True)
        checkpoint_path = campaign_dir / f"recovery-checkpoint-{time.time_ns()}.json"
        _write_json(checkpoint_path, checkpoint)
        checkpoint["evidence_path"] = checkpoint_path.as_posix()
        return checkpoint
    queue = StageQueue(queue_path)
    manifest_sha = object_sha256(manifest)
    queue.create_run(manifest["campaign_id"], manifest["snapshot_id"], manifest_sha, manifest, fencing_required=True, now=at)
    all_jobs = [job for unit in selected_units for job in _recovery_jobs(unit)]
    queue.enqueue_jobs(manifest["campaign_id"], all_jobs, now=at)
    if queue.run(manifest["campaign_id"])["state"] == "interrupted":
        reconcile_expired_attempts(queue, manifest["campaign_id"], DockerReconciler(), now=at)
        queue.complete_reconciliation(manifest["campaign_id"], now=at)
        queue.resume_run(manifest["campaign_id"], now=at)

    from lab.automation.host_lock import HostLock
    lock = HostLock(ROOT / ".work" / "automation" / "linux-controller.lock")
    lock.acquire()
    breaker_cfg = json.loads((ROOT / "config/lab/phase3-automation-v1.json").read_text(encoding="utf-8"))["campaign_policy"]["circuit_breaker"]
    breaker = BackendCircuitBreaker(CircuitPolicy(
        consecutive_failures=breaker_cfg["consecutive_failures"], window_size=breaker_cfg["window_size"],
        window_failures=breaker_cfg["window_failures"], cooldown_seconds=breaker_cfg["cooldown_seconds"],
        half_open_passes=breaker_cfg["half_open_passes"],
        failed_recovery_windows_before_exit=breaker_cfg["failed_recovery_windows_before_exit"],
    ), clock=lambda: time.time())
    nonebot_context = _catalog_context(snapshot_dir)
    koishi_context = _load_catalog_context(snapshot_dir, "linux")
    probes: dict[str, dict[str, Any]] = {}

    def execute(job: dict[str, Any], cancelled) -> dict[str, Any]:
        unit = job["payload"]
        entry_id = unit["registry_entry_id"]
        if job["stage"] == "prepare":
            return {"status": "passed", "attempted": True, "identity_sha256": object_sha256(_entry_identity(unit, manifest["snapshot_id"]))}
        install_unit_id = f"{unit['unit_id']}|install"
        if job["stage"] == "install":
            labels = {"campaign_id": manifest["campaign_id"], "job_id": job["job_id"], "attempt_id": job["attempt_id"], "generation": job["generation"]}
            if unit["ecosystem"] == "nonebot":
                probe = run_nonebot_load_probe(snapshot_dir, entry_id, cache_root=cache_root, timeout_seconds=timeout_seconds, context=nonebot_context, worker_labels=labels, image_digest=manifest["worker_images"]["nonebot"])
            else:
                probe = run_koishi_load_probe(snapshot_dir, entry_id, cache_root=cache_root, timeout_seconds=timeout_seconds, _context=koishi_context, worker_labels=labels, image_digest=manifest["worker_images"]["koishi"])
            probes[entry_id] = probe
            worker = probe.get("worker", {})
            worker = worker if isinstance(worker, dict) else {}
            protocol = worker.get("protocol_report") if isinstance(worker, dict) else None
            install_stage = protocol.get("dependency_install", {}) if isinstance(protocol, dict) else {}
            status = install_stage.get("status")
            if status not in {"passed", "failed", "skipped", "unknown"}:
                status = "unknown"
            reason = install_stage.get("reason_code") or worker.get("reason_code") or (None if status == "passed" else "worker_install_not_passed")
            return {"status": status, "reason_code": reason, "attempted": True, "cleanup_confirmed": worker.get("cleanup_confirmed") is True, "probe_result": probe, "probe_result_sha256": object_sha256(probe)}
        install = queue.get_job(manifest["campaign_id"], install_unit_id).get("result") or {}
        probe = install.get("probe_result", {})
        return _project_worker_stage_result(job["stage"], probe, install.get("probe_result_sha256"))

    dispatcher = Dispatcher(
        queue, manifest["campaign_id"], f"p3-linux-{manifest['campaign_id'][-12:]}", execute, breaker,
        heartbeat_seconds=20, lease_seconds=120,
    )
    infrastructure_stop = False
    try:
        while True:
            try:
                outcome = dispatcher.dispatch_one()
            except CircuitOpen:
                queue.interrupt_run(manifest["campaign_id"])
                infrastructure_stop = True
                break
            if outcome is None:
                break
    finally:
        lock.release()

    rows = []
    jobs_by_unit = {item["unit_id"]: item for item in queue.list_jobs(manifest["campaign_id"])}
    attempts = queue.list_attempts(manifest["campaign_id"])
    for selected in selected_units:
        entry_id = selected["registry_entry_id"]
        prefix = f"{selected['unit_id']}|"
        stages = {stage: jobs_by_unit[prefix + stage] for stage in ("prepare", "install", "native", "cleanup")}
        previous = next(item["source"] for item in manifest["selection"] if item["identity"]["registry_entry_id"] == entry_id)
        new_statuses = {stage: item["status"] for stage, item in stages.items()}
        required = ("install", "native", "cleanup")
        unknown = any(new_statuses[stage] == "unknown" for stage in required)
        failed = any(new_statuses[stage] == "failed" for stage in required)
        not_run = any(new_statuses[stage] == "queued" for stage in required)
        cleanup_result = stages["cleanup"].get("result") or {}
        cleanup_confirmed = cleanup_result.get("cleanup_confirmed") is True
        rows.append({
            "registry_entry_id": entry_id, "ecosystem": selected["ecosystem"], "package": selected["package"],
            "version": selected.get("version"), "platform": "linux", "selection_kind": selected["pilot_selection_kind"],
            "identity_sha256": object_sha256(_entry_identity(selected, manifest["snapshot_id"])),
            "source": previous,
            "old_to_new": {"old_stage_statuses": previous["stage_statuses"], "new_stage_statuses": new_statuses},
            "attempts": [attempt for attempt in attempts if attempt["unit_id"].startswith(prefix)],
            "cleanup_confirmed": cleanup_confirmed,
            "recovery_status": "unknown" if unknown or (not not_run and not cleanup_confirmed) else "not_run" if not_run else "failed" if failed else "recovered",
            "stage_result_sha256": {stage: object_sha256(item["result"]) if item.get("result") is not None else None for stage, item in stages.items()},
        })
    state = queue.run(manifest["campaign_id"])["state"]
    report = {
        "schema_version": 1, "kind": "phase3_linux_recovery_pilot", "campaign_id": manifest["campaign_id"],
        "manifest_sha256": validation["manifest_sha256"], "snapshot_id": manifest["snapshot_id"],
        "platform": "linux", "admission_fingerprint": fresh_health["environment_fingerprint"],
        "worker_image_inventory": worker_image_inventory,
        "queue_path": queue_path.as_posix(), "queue_state": state,
        "status": "interrupted" if infrastructure_stop or state == "interrupted" else "completed" if state == "completed" else "incomplete",
        "denominator": {"expected_units": len(rows), "terminal_units": sum(all(row["old_to_new"]["new_stage_statuses"][stage] in {"passed", "failed", "skipped", "unknown"} for stage in ("install", "native", "cleanup")) for row in rows), "required_stage_positions": len(rows) * 3},
        "counts": {
            "recovered": sum(row["recovery_status"] == "recovered" for row in rows),
            "plugin_or_stage_failed": sum(row["recovery_status"] == "failed" for row in rows),
            "unknown_or_cleanup_unconfirmed": sum(row["recovery_status"] == "unknown" for row in rows),
            "not_run": sum(row["recovery_status"] == "not_run" for row in rows),
            "cleanup_confirmed": sum(row["cleanup_confirmed"] for row in rows),
            "infrastructure_blocker": breaker.state != "closed" or infrastructure_stop,
        },
        "gate": {"scope": "linux pilot only; not full P3-RECOVERY", "health": "passed" if breaker.state == "closed" and not infrastructure_stop and not any(row["recovery_status"] in {"unknown", "not_run"} for row in rows) else "blocked", "plugin_compatibility": "not_evaluated"},
        "plugin_reports": rows,
    }
    report["report_sha256"] = object_sha256(report)
    _write_json(report_path, report)
    return report
