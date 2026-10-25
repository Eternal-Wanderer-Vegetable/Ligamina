"""Build durable catalog stage plans from frozen snapshot and profile locks."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

try:
    from .catalog.snapshot import CatalogError, SOURCE_DEFINITIONS, _safe_relative_path, derive_snapshot_id, pretty_json_bytes, strict_json_loads
    from .queue import QueueError, StageQueue
    from .recipes import suggest_recipe_categories
except ImportError:  # supports loading from the direct lab/run.py entry point
    from catalog.snapshot import CatalogError, SOURCE_DEFINITIONS, _safe_relative_path, derive_snapshot_id, pretty_json_bytes, strict_json_loads
    from queue import QueueError, StageQueue
    from recipes import suggest_recipe_categories


ROOT = Path(__file__).resolve().parents[1]
PLATFORM_IDS = {"windows": "windows-x86_64", "linux": "linux-x86_64"}
STAGES = ("prepare", "install", "native", "federated", "compare", "cleanup")


class CatalogRunError(ValueError):
    """Frozen inputs or catalog run options are inconsistent."""


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _read(path: Path) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CatalogRunError(f"cannot read frozen input {path}: {error}") from error
    if not isinstance(value, dict):
        raise CatalogRunError(f"frozen input must contain a JSON object: {path}")
    return value, raw


def _verify_snapshot_inputs(snapshot_dir: Path) -> dict[str, Any]:
    """Verify immutable snapshot hashes without repeating expensive normalization.

    The full raw-to-normalized verification is performed when the snapshot is
    frozen and recorded in its acceptance evidence. Every catalog shard still
    checks all raw source hashes, the derived snapshot ID, the normalized
    document hashes, and the catalog denominator before it creates work.
    """
    snapshot_dir = Path(snapshot_dir).resolve(strict=True)
    catalog, _ = _read(snapshot_dir / "catalog.json")
    provenance, _ = _read(snapshot_dir / "provenance.json")
    changes, _ = _read(snapshot_dir / "changes.json")
    if any(item.get("schema_version") != 1 for item in (catalog, provenance, changes)):
        raise CatalogRunError("snapshot schema_version must be 1")
    snapshot_id = catalog.get("snapshot_id")
    if not snapshot_id or any(item.get("snapshot_id") != snapshot_id for item in (provenance, changes)):
        raise CatalogRunError("snapshot IDs differ across catalog, provenance, and changes")

    sources = provenance.get("sources")
    if not isinstance(sources, list) or not sources:
        raise CatalogRunError("snapshot provenance has no sources")
    source_by_key: dict[str, dict[str, Any]] = {}
    for source in sources:
        if not isinstance(source, dict):
            raise CatalogRunError("snapshot provenance source must be an object")
        source_key = source.get("source_key")
        if source_key not in SOURCE_DEFINITIONS or source_key in source_by_key:
            raise CatalogRunError("snapshot provenance has an unknown or duplicate source key")
        raw_path = _safe_relative_path(snapshot_dir, source.get("raw_path", ""))
        try:
            raw = raw_path.read_bytes()
        except OSError as error:
            raise CatalogRunError(f"snapshot raw source is missing: {source.get('raw_path')}") from error
        if len(raw) != source.get("byte_length") or _sha256(raw) != source.get("sha256"):
            raise CatalogRunError(f"snapshot raw integrity mismatch: {source_key}")
        if source_key in ("nonebot_registry", "koishi_registry"):
            try:
                strict_json_loads(raw, source_key)
            except CatalogError as error:
                raise CatalogRunError(f"snapshot raw source is invalid: {source_key}: {error}") from error
        source_by_key[source_key] = source
    if set(source_by_key) != set(SOURCE_DEFINITIONS):
        raise CatalogRunError("snapshot must contain the full required source set")
    if derive_snapshot_id(source_by_key) != snapshot_id:
        raise CatalogRunError("snapshot ID does not match source raw hashes")
    if _sha256(pretty_json_bytes(catalog)) != provenance.get("catalog_sha256"):
        raise CatalogRunError("normalized catalog SHA-256 mismatch")
    if _sha256(pretty_json_bytes(changes)) != provenance.get("changes_sha256"):
        raise CatalogRunError("changes SHA-256 mismatch")

    entries = catalog.get("entries")
    if not isinstance(entries, list):
        raise CatalogRunError("frozen catalog entries must be a list")
    entry_ids = [entry.get("registry_entry_id") for entry in entries if isinstance(entry, dict)]
    if len(entry_ids) != len(entries) or any(not isinstance(entry_id, str) or not entry_id for entry_id in entry_ids):
        raise CatalogRunError("frozen catalog contains an invalid registry entry")
    if len(set(entry_ids)) != len(entry_ids):
        raise CatalogRunError("frozen catalog contains duplicate registry_entry_id values")
    counts = catalog.get("counts", {})
    if counts.get("registry_entries") != len(entries):
        raise CatalogRunError("frozen catalog denominator does not match its entries")
    by_ecosystem = counts.get("by_ecosystem", {})
    actual_by_ecosystem = {
        ecosystem: sum(entry.get("ecosystem") == ecosystem for entry in entries)
        for ecosystem in ("nonebot", "koishi")
    }
    if by_ecosystem != actual_by_ecosystem or sum(actual_by_ecosystem.values()) != len(entries):
        raise CatalogRunError("frozen catalog ecosystem counts do not match its entries")
    return {
        "snapshot_id": snapshot_id,
        "registry_entries": len(entries),
        "raw_sources": len(sources),
        "integrity": "raw-and-frozen-document-hashes-passed",
    }


def parse_shard(value: str) -> tuple[int, int]:
    try:
        index_text, count_text = value.split("/", 1)
        index, count = int(index_text), int(count_text)
    except (ValueError, AttributeError) as error:
        raise CatalogRunError("shard must use index/count, for example 0/4") from error
    if count < 1 or index < 0 or index >= count:
        raise CatalogRunError("shard index must be within a positive shard count")
    return index, count


def partition_units(units: Iterable[dict[str, Any]], shard_index: int, shard_count: int) -> list[dict[str, Any]]:
    if shard_count < 1 or shard_index < 0 or shard_index >= shard_count:
        raise CatalogRunError("invalid shard index/count")
    unit_list = list(units)
    unit_ids = [unit.get("unit_id") for unit in unit_list]
    if any(not isinstance(unit_id, str) or not unit_id for unit_id in unit_ids):
        raise CatalogRunError("every planned unit requires a stable unit_id")
    if len(set(unit_ids)) != len(unit_ids):
        raise CatalogRunError("planned unit IDs must be unique before sharding")
    return [
        unit
        for unit in unit_list
        if int.from_bytes(hashlib.sha256(unit["unit_id"].encode("utf-8")).digest()[:8], "big") % shard_count == shard_index
    ]


def build_stage_jobs(unit: dict[str, Any], *, max_attempts: int = 2) -> list[dict[str, Any]]:
    """Build one install-once DAG with independent native/federated paths and final cleanup."""
    required = ("unit_id", "registry_entry_id", "ecosystem", "platform", "profile_id", "case_id")
    if any(not isinstance(unit.get(key), str) or not unit.get(key) for key in required):
        raise CatalogRunError("catalog unit is missing a required identity field")
    base = unit["unit_id"]
    stage_ids = {stage: f"{base}|{stage}" for stage in STAGES}
    dependencies = {
        "prepare": [],
        "install": [stage_ids["prepare"]],
        "native": [stage_ids["install"]],
        "federated": [stage_ids["install"]],
        "compare": [stage_ids["native"], stage_ids["federated"]],
        "cleanup": [stage_ids["install"], stage_ids["native"], stage_ids["federated"], stage_ids["compare"]],
    }
    jobs = []
    for stage in STAGES:
        jobs.append(
            {
                "unit_id": stage_ids[stage],
                "registry_entry_id": unit["registry_entry_id"],
                "ecosystem": unit["ecosystem"],
                "platform": unit["platform"],
                "profile_id": unit["profile_id"],
                "stage": stage,
                "case_id": unit["case_id"],
                "dependencies": dependencies[stage],
                "max_attempts": max_attempts,
                "always_run": stage == "cleanup",
                "payload": {**unit, "stage": stage},
            }
        )
    return jobs


def _verify_locks(
    snapshot_dir: Path,
    catalog: dict[str, Any],
    artifact_lock: dict[str, Any],
    profile_lock: dict[str, Any],
    *,
    profile_lock_path: Path | None = None,
) -> None:
    expected_ids = [entry.get("registry_entry_id") for entry in catalog.get("entries", [])]
    for name, lock in (("artifact", artifact_lock), ("profile", profile_lock)):
        actual_ids = [entry.get("registry_entry_id") for entry in lock.get("entries", [])]
        if lock.get("snapshot_id") != catalog.get("snapshot_id") or actual_ids != expected_ids:
            raise CatalogRunError(f"{name} lock does not preserve the frozen catalog order and denominator")
    artifact_path = snapshot_dir / "artifacts.lock.json"
    profile_path = profile_lock_path or (snapshot_dir / "profiles.lock.json")
    checksum_paths = (
        (artifact_path, snapshot_dir / "artifacts.lock.sha256"),
        (
            profile_path,
            profile_path.with_name("profiles.lock.sha256") if profile_path.name == "profiles.lock.json"
            else profile_path.with_name(f"{profile_path.name}.sha256"),
        ),
    )
    for path, checksum in checksum_paths:
        try:
            expected = checksum.read_text(encoding="ascii").split()[0]
        except (OSError, IndexError) as error:
            raise CatalogRunError(f"missing or invalid checksum for {path.name}") from error
        if _sha256(path.read_bytes()) != expected:
            raise CatalogRunError(f"{path.name} checksum mismatch")
    if profile_lock.get("artifacts_lock_sha256") != _sha256(artifact_path.read_bytes()):
        raise CatalogRunError("profiles lock was generated from a different artifact lock")
    if profile_lock.get("counts", {}).get("profile_units") != len(expected_ids) * len(PLATFORM_IDS):
        raise CatalogRunError("profiles lock does not account for both OS profiles per entry")


def load_catalog_units(
    snapshot_dir: Path,
    platforms: Iterable[str],
    *,
    profile_lock_path: Path | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    snapshot_dir = Path(snapshot_dir).expanduser().resolve(strict=True)
    snapshot_summary = _verify_snapshot_inputs(snapshot_dir)
    catalog, catalog_bytes = _read(snapshot_dir / "catalog.json")
    artifact_lock, artifact_bytes = _read(snapshot_dir / "artifacts.lock.json")
    selected_profile_lock = Path(profile_lock_path).expanduser().resolve(strict=True) if profile_lock_path else snapshot_dir / "profiles.lock.json"
    profile_lock, profile_bytes = _read(selected_profile_lock)
    _verify_locks(snapshot_dir, catalog, artifact_lock, profile_lock, profile_lock_path=selected_profile_lock)
    if snapshot_summary["snapshot_id"] != catalog.get("snapshot_id"):
        raise CatalogRunError("verified snapshot identity differs from catalog file")
    selected_platforms = tuple(dict.fromkeys(platforms))
    if not selected_platforms or any(platform not in PLATFORM_IDS for platform in selected_platforms):
        raise CatalogRunError("platforms must contain windows and/or linux")

    artifacts_by_entry = {entry["registry_entry_id"]: entry for entry in artifact_lock["entries"]}
    profiles_by_entry = {entry["registry_entry_id"]: entry for entry in profile_lock["entries"]}
    catalog_sha256 = _sha256(catalog_bytes)
    artifact_lock_sha256 = _sha256(artifact_bytes)
    profile_lock_sha256 = _sha256(profile_bytes)
    units: list[dict[str, Any]] = []
    for entry in catalog["entries"]:
        entry_id = entry["registry_entry_id"]
        artifact_entry = artifacts_by_entry[entry_id]
        profile_entry = profiles_by_entry[entry_id]
        artifact_by_platform = {record.get("platform"): record for record in artifact_entry.get("artifacts", [])}
        classification = suggest_recipe_categories(entry)
        categories = classification["categories"]
        fixture_state = "missing_fixture" if categories else "unmapped_category"
        for platform in selected_platforms:
            platform_id = PLATFORM_IDS[platform]
            # Koishi publishes one platform-independent registry tarball while
            # its dependency profile is still resolved separately per OS.
            artifact_record = artifact_by_platform.get(platform_id) or artifact_by_platform.get("all", {})
            artifact = artifact_record.get("artifact") or {}
            profile = profile_entry.get("profiles", {}).get(platform_id, {})
            dependency_lock = profile.get("dependency_lock") or {}
            units.append(
                {
                    "unit_id": f"{entry_id}|{platform}|catalog",
                    "registry_entry_id": entry_id,
                    "ecosystem": entry["ecosystem"],
                    "package": entry["package"],
                    "version": entry.get("advertised_version"),
                    "platform": platform,
                    "profile_id": profile.get("profile_id") or f"unresolved-{entry['ecosystem']}-{platform}",
                    "profile_status": profile.get("status", "profile_missing"),
                    "profile_reason_code": profile.get("reason_code"),
                    "dependency_lock_sha256": dependency_lock.get("sha256"),
                    "artifact_status": artifact_record.get("status", artifact_entry.get("status", "artifact_missing")),
                    "artifact_reason_code": artifact_record.get("reason_code", artifact_entry.get("reason_code")),
                    "artifact_sha256": (artifact.get("computed_digests") or {}).get("sha256") or artifact.get("sha256"),
                    "case_id": "catalog",
                    "recipe_categories": categories,
                    "unmapped_category_labels": classification["unmapped_labels"],
                    "behavior_fixture_status": fixture_state,
                    "reason_code": classification["reason_code"] or "missing_fixture",
                    "source_snapshot_sha256": catalog_sha256,
                    "artifact_lock_sha256": artifact_lock_sha256,
                    "profile_lock_sha256": profile_lock_sha256,
                }
            )
    return catalog, units


def create_catalog_plan(
    snapshot_dir: Path,
    queue_path: Path,
    *,
    platforms: Iterable[str],
    shard_index: int = 0,
    shard_count: int = 1,
    run_id: str | None = None,
    resume: bool = False,
    profile_lock_path: Path | None = None,
) -> dict[str, Any]:
    selected_platforms = tuple(dict.fromkeys(platforms))
    catalog, all_units = load_catalog_units(snapshot_dir, selected_platforms, profile_lock_path=profile_lock_path)
    selected = partition_units(all_units, shard_index, shard_count)
    all_jobs = [job for unit in selected for job in build_stage_jobs(unit)]
    manifest = {
        "schema_version": 1,
        "snapshot_id": catalog["snapshot_id"],
        "snapshot_entry_count": len(catalog["entries"]),
        "platforms": sorted(selected_platforms),
        "shard_index": shard_index,
        "shard_count": shard_count,
        "source_snapshot_sha256": selected[0]["source_snapshot_sha256"] if selected else _sha256((Path(snapshot_dir) / "catalog.json").read_bytes()),
        "artifact_lock_sha256": selected[0]["artifact_lock_sha256"] if selected else _sha256((Path(snapshot_dir) / "artifacts.lock.json").read_bytes()),
        "profile_lock_sha256": selected[0]["profile_lock_sha256"] if selected else _sha256((Path(snapshot_dir) / "profiles.lock.json").read_bytes()),
        "unit_count": len(all_units),
        "selected_unit_count": len(selected),
        "expected_stage_count": len(all_jobs),
        "stages": list(STAGES),
        "retry_policy": {"default_max_attempts": 2, "worker_loss": "unknown attempt retained; requeue within attempt budget"},
    }
    manifest_sha256 = _sha256(_canonical(manifest))
    if run_id is None:
        platform_tag = "-".join(manifest["platforms"])
        run_id = f"catalog-{manifest_sha256[:12]}-{platform_tag}-s{shard_index}of{shard_count}"
    queue = StageQueue(queue_path)
    if resume:
        try:
            queue.run(run_id)
        except QueueError as error:
            raise CatalogRunError("cannot resume a catalog plan that has not been created") from error
    created = queue.create_run(run_id, catalog["snapshot_id"], manifest_sha256, manifest)
    job_counts = queue.enqueue_jobs(run_id, all_jobs)
    resumed = queue.resume_run(run_id) if resume else None
    run = queue.run(run_id)
    counts = queue.counts(run_id)
    return {
        "status": "resumed" if resume else "planned",
        "run_id": run_id,
        "run_created": created,
        "run_state": run["state"],
        "manifest_sha256": manifest_sha256,
        "snapshot_id": catalog["snapshot_id"],
        "total_catalog_entries": len(catalog["entries"]),
        "expected_units": len(all_units),
        "selected_units": len(selected),
        "expected_stage_jobs": len(all_jobs),
        "job_counts": counts,
        "enqueue": job_counts,
        "resume": resumed,
        "queue_path": str(Path(queue_path).expanduser().absolute()),
        "manifest": manifest,
        "execution_started": False,
    }


def write_plan_report(plan: dict[str, Any], path: Path) -> Path:
    path = Path(path).expanduser().absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--queue-db", type=Path, default=ROOT / ".work/catalog-queue.sqlite3")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--platform", choices=("windows", "linux", "both"), default="both")
    parser.add_argument("--shard", default="0/1", help="stable partition as index/count")
    parser.add_argument("--run-id")
    parser.add_argument("--resume", action="store_true")
    options = parser.parse_args(argv)
    options.shard_index, options.shard_count = parse_shard(options.shard)
    options.platforms = ("windows", "linux") if options.platform == "both" else (options.platform,)
    return options
