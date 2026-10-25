#!/usr/bin/env python3
"""Audit frozen Windows Koishi inputs and their offline cache without executing plugins."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lab.pilot_workers import _package_archive_ref  # noqa: E402


RECOVERY_MANIFEST = "outputs/acceptance/phase3-p301-recovery-manifest-20261020.json"
PILOT_MANIFEST = "outputs/acceptance/phase3-p305-windows-pilot-manifest-v2-20261025.json"
ARTIFACT_LOCK = "catalogs/p2-41649744e057d61031d5/artifacts.lock.json"
PROFILE_LOCK = "catalogs/p2-41649744e057d61031d5/profiles.lock.json"


def _read_json(path: Path) -> tuple[dict[str, Any], bytes]:
    raw = path.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value, raw


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _cache_path(cache_root: Path, relative: str) -> Path:
    path = Path(relative)
    return path if path.is_absolute() else cache_root / path


def _verified(path: Path, expected_algorithm: str, expected_digest: str) -> tuple[bool, int]:
    if not path.is_file():
        return False, 0
    digest = hashlib.new(expected_algorithm)
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest() == expected_digest, size


def audit(cache_root: Path, expected_recovery_sha: str, expected_pilot_sha: str) -> dict[str, Any]:
    recovery, recovery_raw = _read_json(ROOT / RECOVERY_MANIFEST)
    pilot, pilot_raw = _read_json(ROOT / PILOT_MANIFEST)
    artifacts, artifacts_raw = _read_json(ROOT / ARTIFACT_LOCK)
    profiles, profiles_raw = _read_json(ROOT / PROFILE_LOCK)

    if recovery.get("manifest_sha256") != expected_recovery_sha:
        raise ValueError("recovery manifest identity differs from the frozen expected SHA")
    if pilot.get("manifest_sha256") != expected_pilot_sha:
        raise ValueError("Windows pilot identity differs from the frozen expected SHA")

    closed_ids = {
        row["identity"]["registry_entry_id"]
        for row in pilot.get("selection", [])
        if row.get("identity", {}).get("platform") == "windows"
    }
    remaining = [
        row
        for row in recovery.get("selection", [])
        if row.get("identity", {}).get("platform") == "windows"
        and row.get("identity", {}).get("ecosystem") == "koishi"
        and row["identity"]["registry_entry_id"] not in closed_ids
    ]

    profile_entries = {
        entry["registry_entry_id"]: entry for entry in profiles.get("entries", [])
    }
    artifact_entries = {
        entry["registry_entry_id"]: entry for entry in artifacts.get("entries", [])
    }
    profile_refs: dict[str, dict[str, Any]] = {}
    unsupported: dict[tuple[str, str, str], dict[str, Any]] = {}
    archive_refs: dict[tuple[str, str], dict[str, Any]] = {}
    artifact_refs: dict[str, dict[str, Any]] = {}
    lifecycle_script_packages: dict[tuple[str, str, str], dict[str, Any]] = {}
    missing_profile_refs: list[str] = []
    invalid_profile_locks: list[str] = []
    missing_artifact_refs: list[str] = []
    invalid_artifact_refs: list[str] = []

    for row in remaining:
        identity = row["identity"]
        registry_id = identity["registry_entry_id"]
        package_id = f"{identity['package']}@{identity['version']}"
        profile_entry = profile_entries.get(registry_id, {})
        profile = profile_entry.get("profiles", {}).get("windows-x86_64", {})
        lock_ref = profile.get("dependency_lock")
        if profile.get("status") != "locked" or not isinstance(lock_ref, dict):
            missing_profile_refs.append(registry_id)
            continue

        lock_path = _cache_path(cache_root, lock_ref["cache_path"])
        if not lock_path.is_file():
            missing_profile_refs.append(registry_id)
            continue
        lock_raw = lock_path.read_bytes()
        lock_digest = _sha256(lock_raw)
        if len(lock_raw) != lock_ref.get("size_bytes") or lock_digest != lock_ref.get("sha256"):
            invalid_profile_locks.append(registry_id)
            continue
        profile_refs[lock_digest] = {"path": lock_path, "size_bytes": len(lock_raw)}
        lock = json.loads(lock_raw)
        for dependency_path, dependency in lock.get("packages", {}).items():
            if not dependency_path:
                continue
            if dependency.get("hasInstallScript") is True:
                lifecycle_script_packages[(registry_id, dependency_path, str(dependency.get("version")))] = {
                    "registry_entry_id": registry_id,
                    "plugin": package_id,
                    "dependency_path": dependency_path,
                    "version": dependency.get("version"),
                }
            url = dependency.get("resolved")
            integrity = dependency.get("integrity")
            if not isinstance(url, str) or not isinstance(integrity, str):
                unsupported[(registry_id, dependency_path, str(dependency.get("version")))] = {
                    "registry_entry_id": registry_id,
                    "plugin": package_id,
                    "dependency_path": dependency_path,
                    "version": dependency.get("version"),
                    "resolved": url,
                    "reason": "missing_resolved_url_or_integrity",
                    "profile_lock_sha256": lock_digest,
                }
                continue
            try:
                algorithm, digest, relative = _package_archive_ref(
                    url, integrity, entry_path=dependency_path
                )
            except Exception as error:
                unsupported[(registry_id, dependency_path, str(dependency.get("version")))] = {
                    "registry_entry_id": registry_id,
                    "plugin": package_id,
                    "dependency_path": dependency_path,
                    "version": dependency.get("version"),
                    "resolved": url,
                    "reason": str(error),
                    "profile_lock_sha256": lock_digest,
                }
                continue
            key = (algorithm, digest)
            ref = archive_refs.setdefault(
                key,
                {
                    "path": cache_root / relative,
                    "integrity": integrity,
                    "dependencies": [],
                },
            )
            ref["dependencies"].append(
                {"registry_entry_id": registry_id, "plugin": package_id, "path": dependency_path}
            )

        artifact_entry = artifact_entries.get(registry_id, {})
        matching_artifacts = [
            item
            for item in artifact_entry.get("artifacts", [])
            if item.get("status") == "locked"
            and item.get("platform") in ("all", "windows-x86_64")
        ]
        if not matching_artifacts:
            missing_artifact_refs.append(registry_id)
            continue
        for item in matching_artifacts:
            artifact = item.get("artifact", {})
            digests = artifact.get("computed_digests", {})
            digest = digests.get("sha256")
            cache_path = artifact.get("cache_path")
            if not isinstance(digest, str) or not isinstance(cache_path, str):
                invalid_artifact_refs.append(registry_id)
                continue
            artifact_refs[digest] = {
                "path": _cache_path(cache_root, cache_path),
                "registry_entry_id": registry_id,
                "filename": artifact.get("filename"),
            }

    missing_archives: list[dict[str, Any]] = []
    corrupt_archives: list[dict[str, Any]] = []
    verified_archive_bytes = 0
    for (algorithm, digest), ref in archive_refs.items():
        valid, size = _verified(ref["path"], algorithm, digest)
        if not ref["path"].is_file():
            missing_archives.append({"algorithm": algorithm, "digest": digest})
        elif not valid:
            corrupt_archives.append({"algorithm": algorithm, "digest": digest})
        else:
            verified_archive_bytes += size

    missing_artifacts: list[str] = []
    corrupt_artifacts: list[str] = []
    verified_artifact_bytes = 0
    for digest, ref in artifact_refs.items():
        valid, size = _verified(ref["path"], "sha256", digest)
        if not ref["path"].is_file():
            missing_artifacts.append(digest)
        elif not valid:
            corrupt_artifacts.append(digest)
        else:
            verified_artifact_bytes += size

    cache_audit = {
        "remaining_koishi_units": len(remaining),
        "closed_pilot_unit_ids_excluded": len(closed_ids),
        "frozen_dependency_locks_verified": len(profile_refs),
        "missing_dependency_locks": missing_profile_refs,
        "invalid_dependency_locks": invalid_profile_locks,
        "unique_dependency_archives": len(archive_refs),
        "dependency_archives_verified": len(archive_refs)
        - len(missing_archives)
        - len(corrupt_archives),
        "missing_dependency_archives": missing_archives,
        "corrupt_dependency_archives": corrupt_archives,
        "verified_dependency_archive_bytes": verified_archive_bytes,
        "unsupported_dependency_refs": list(unsupported.values()),
        "lifecycle_script_package_occurrences": len(lifecycle_script_packages),
        "lifecycle_script_package_names": sorted(
            {item["dependency_path"].rsplit("/", 1)[-1] for item in lifecycle_script_packages.values()}
        ),
        "sqlite3_lifecycle_rows": [
            item for item in lifecycle_script_packages.values()
            if item["dependency_path"].endswith("/sqlite3")
        ],
        "unique_plugin_artifacts": len(artifact_refs),
        "plugin_artifacts_verified": len(artifact_refs)
        - len(missing_artifacts)
        - len(corrupt_artifacts),
        "missing_plugin_artifacts_sha256": missing_artifacts,
        "corrupt_plugin_artifacts_sha256": corrupt_artifacts,
        "missing_plugin_artifact_refs": missing_artifact_refs,
        "invalid_plugin_artifact_refs": invalid_artifact_refs,
        "verified_plugin_artifact_bytes": verified_artifact_bytes,
    }
    return {
        "schema_version": 1,
        "kind": "phase3_p305_windows_koishi_cache_audit",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "offline cache audit only; no Windows Sandbox or catalog plugin execution",
        "inputs": {
            "recovery_manifest_path": RECOVERY_MANIFEST,
            "recovery_manifest_sha256": recovery.get("manifest_sha256"),
            "recovery_manifest_file_sha256": _sha256(recovery_raw),
            "pilot_manifest_path": PILOT_MANIFEST,
            "pilot_manifest_sha256": pilot.get("manifest_sha256"),
            "pilot_manifest_file_sha256": _sha256(pilot_raw),
            "artifact_lock_path": ARTIFACT_LOCK,
            "artifact_lock_file_sha256": _sha256(artifacts_raw),
            "profile_lock_path": PROFILE_LOCK,
            "profile_lock_file_sha256": _sha256(profiles_raw),
            "cache_root": str(cache_root.resolve()),
        },
        "cache_audit": cache_audit,
        "interpretation": (
            "A verified archive means cached bytes match the frozen lock integrity. "
            "It does not authorize or execute lifecycle scripts. Non-registry Git dependencies "
            "remain unsupported by the current offline Worker allowlist and require a separate "
            "frozen source/binary decision."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--expected-recovery-sha", default="f084778b6cfee271876ba6493e039fc2a288e0295a7a0fe9353f0d6f2074416e")
    parser.add_argument("--expected-pilot-sha", default="0796fad72d14f0e84cfd91b77e63177873a26510dfa3adee55c9a3298feed4fa")
    parser.add_argument("--report", type=Path, default=ROOT / "outputs/acceptance/phase3-p305-windows-koishi-cache-audit-20261025.json")
    args = parser.parse_args()

    report = audit(args.cache_root.resolve(), args.expected_recovery_sha, args.expected_pilot_sha)
    raw = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_bytes(raw)
    digest = _sha256(raw)
    args.report.with_suffix(args.report.suffix + ".sha256").write_text(
        f"{digest}  {args.report.name}\n", encoding="ascii"
    )
    print(json.dumps({"report": str(args.report), "report_sha256": digest, "cache_audit": report["cache_audit"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
