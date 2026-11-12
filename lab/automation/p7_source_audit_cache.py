"""Download only the hash-locked archives needed to validate P7 source audits."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from lab.artifacts import download_artifact
from lab.automation.behavior_v2 import ARTIFACT_CACHE, CATALOG_ROOT


ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATHS = (
    ROOT / "lab/automation/data/p7-nonebot-source-bound-context-v1.json",
    ROOT / "lab/automation/data/p7-koishi-source-bound-context-v1.json",
    ROOT / "lab/automation/data/p7-bilibili-image-https-v1.json",
    ROOT / "lab/automation/data/p7-local-candidate-stimuli-v1.json",
)
ALLOWED_HOSTS = {
    "nonebot": {"files.pythonhosted.org"},
    "koishi": {"registry.npmjs.org"},
}
PROFILE_LOCK_ROOT = ROOT / "lab/automation/data/p7-profile-locks"


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _root_path(relative: str, *, label: str) -> Path:
    path = (ROOT / relative).resolve(strict=True)
    if not path.is_relative_to(ROOT.resolve()):
        raise ValueError(f"{label} path escapes the repository")
    return path


def required_artifact_targets() -> list[dict[str, Any]]:
    """Resolve the exact artifact identities referenced by committed P7 audit reports."""
    identities: dict[str, dict[str, str]] = {}
    for manifest_path in MANIFEST_PATHS:
        manifest = _read_json(manifest_path)
        for ref_name in ("source_report", "supplemental_source_report", "source_audit"):
            reference = manifest.get(ref_name)
            if not isinstance(reference, dict):
                continue
            report_path = _root_path(str(reference.get("path", "")), label=ref_name)
            report_raw = report_path.read_bytes()
            if _sha256(report_raw) != reference.get("sha256"):
                raise ValueError(f"P7 source report hash changed: {report_path.relative_to(ROOT)}")
            report = json.loads(report_raw)
            if not isinstance(report, dict):
                raise ValueError(f"P7 source report is not an object: {report_path.relative_to(ROOT)}")
            rows = report.get("findings")
            if not isinstance(rows, list):
                rows = report.get("entries")
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError(f"malformed P7 source row in {report_path.relative_to(ROOT)}")
                identity_fields = ("registry_entry_id", "ecosystem", "package", "version", "artifact_sha256")
                if any(not isinstance(row.get(field), str) or not row[field].strip() for field in identity_fields):
                    raise ValueError(f"incomplete P7 source identity in {report_path.relative_to(ROOT)}")
                identity = {field: row[field] for field in identity_fields}
                digest = identity["artifact_sha256"]
                if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                    raise ValueError(f"invalid P7 artifact SHA-256 in {report_path.relative_to(ROOT)}")
                previous = identities.setdefault(identity["registry_entry_id"], identity)
                if previous != identity:
                    raise ValueError(f"conflicting P7 source identity: {identity['registry_entry_id']}")

    lock_path = CATALOG_ROOT / "artifacts.lock.json"
    lock_raw = lock_path.read_bytes()
    checksum_path = CATALOG_ROOT / "artifacts.lock.sha256"
    expected_lock_hash = checksum_path.read_text(encoding="ascii").split()[0]
    if _sha256(lock_raw) != expected_lock_hash:
        raise ValueError("catalog artifacts lock checksum mismatch")
    lock = json.loads(lock_raw)
    entries = {
        str(row.get("registry_entry_id")): row
        for row in lock.get("entries", [])
        if isinstance(row, dict)
    }

    targets: list[dict[str, Any]] = []
    for entry_id, identity in sorted(identities.items()):
        ecosystem = identity["ecosystem"]
        allowed_hosts = ALLOWED_HOSTS.get(ecosystem)
        if allowed_hosts is None:
            raise ValueError(f"unsupported P7 artifact ecosystem: {ecosystem}")
        lock_entry = entries.get(entry_id)
        if not isinstance(lock_entry, dict) or lock_entry.get("package") != identity["package"]:
            raise ValueError(f"P7 source artifact has no matching frozen catalog lock: {entry_id}")
        platform = "all" if ecosystem == "koishi" else "linux-x86_64"
        artifacts = [
            row.get("artifact")
            for row in lock_entry.get("artifacts", [])
            if isinstance(row, dict) and row.get("platform") == platform and isinstance(row.get("artifact"), dict)
        ]
        if len(artifacts) != 1:
            raise ValueError(f"P7 source artifact has no unique {platform} lock: {entry_id}")
        artifact = artifacts[0]
        locked_sha256 = artifact.get("computed_digests", {}).get("sha256")
        if locked_sha256 != identity["artifact_sha256"]:
            raise ValueError(f"P7 source artifact hash differs from the frozen catalog lock: {entry_id}")
        targets.append({
            **identity,
            "allowed_hosts": set(allowed_hosts),
            "artifact": {**artifact, "sha256": identity["artifact_sha256"], "registry_entry_id": entry_id},
        })
    return targets


def required_profile_lock_targets() -> list[dict[str, Any]]:
    """Resolve exact dependency locks for the source-audit artifacts from the frozen catalog."""
    profile_lock_path = CATALOG_ROOT / "profiles.lock.json"
    profile_lock_raw = profile_lock_path.read_bytes()
    checksum_path = CATALOG_ROOT / "profiles.lock.sha256"
    expected_profile_lock_hash = checksum_path.read_text(encoding="ascii").split()[0]
    if _sha256(profile_lock_raw) != expected_profile_lock_hash:
        raise ValueError("catalog profiles lock checksum mismatch")
    profile_lock = json.loads(profile_lock_raw)
    artifact_lock_hash = _sha256((CATALOG_ROOT / "artifacts.lock.json").read_bytes())
    if profile_lock.get("artifacts_lock_sha256") != artifact_lock_hash:
        raise ValueError("catalog profiles lock is bound to a different artifacts lock")
    entries = {
        str(row.get("registry_entry_id")): row
        for row in profile_lock.get("entries", [])
        if isinstance(row, dict)
    }

    targets: list[dict[str, Any]] = []
    for artifact_target in required_artifact_targets():
        entry_id = artifact_target["registry_entry_id"]
        entry = entries.get(entry_id)
        if not isinstance(entry, dict):
            raise ValueError(f"P7 source artifact has no frozen profile entry: {entry_id}")
        if (
            entry.get("package") != artifact_target["package"]
            or entry.get("ecosystem") != artifact_target["ecosystem"]
            or entry.get("advertised_version") != artifact_target["version"]
        ):
            raise ValueError(f"P7 source artifact differs from its frozen profile identity: {entry_id}")
        profile = entry.get("profiles", {}).get("linux-x86_64", {})
        lock = profile.get("dependency_lock") if isinstance(profile, dict) else None
        if profile.get("artifact_sha256") != artifact_target["artifact_sha256"] or not isinstance(lock, dict):
            raise ValueError(f"P7 source artifact has no exact Linux dependency lock: {entry_id}")
        digest = lock.get("sha256")
        expected_cache_path = Path("profile-locks") / artifact_target["ecosystem"] / "sha256" / str(digest)[:2] / str(digest)
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
            or lock.get("cache_path") != expected_cache_path.as_posix()
            or not isinstance(lock.get("size_bytes"), int)
            or lock["size_bytes"] <= 0
        ):
            raise ValueError(f"P7 source artifact has an invalid frozen Linux dependency lock: {entry_id}")
        source_path = PROFILE_LOCK_ROOT / digest
        raw = source_path.read_bytes()
        if len(raw) != lock["size_bytes"] or _sha256(raw) != digest:
            raise ValueError(f"vendored P7 dependency lock hash or size mismatch: {entry_id}")
        targets.append({
            "registry_entry_id": entry_id,
            "package": artifact_target["package"],
            "ecosystem": artifact_target["ecosystem"],
            "sha256": digest,
            "size_bytes": lock["size_bytes"],
            "cache_path": expected_cache_path.as_posix(),
            "path": source_path,
            "status": "verified_source_lock",
        })
    return targets


def _install_profile_lock(target: dict[str, Any], cache_root: Path) -> None:
    raw = target["path"].read_bytes()
    if len(raw) != target["size_bytes"] or _sha256(raw) != target["sha256"]:
        raise RuntimeError(f"vendored P7 dependency lock changed: {target['registry_entry_id']}")
    root = cache_root.resolve()
    destination = (root / target["cache_path"]).resolve()
    if not destination.is_relative_to(root):
        raise RuntimeError(f"P7 dependency lock cache path escapes its root: {target['registry_entry_id']}")
    if destination.is_file():
        existing = destination.read_bytes()
        if len(existing) != target["size_bytes"] or _sha256(existing) != target["sha256"]:
            raise RuntimeError(f"cached P7 dependency lock hash or size mismatch: {target['registry_entry_id']}")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(raw)
    try:
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_source_audit_cache(cache_root: Path = ARTIFACT_CACHE) -> list[dict[str, Any]]:
    """Fetch and verify every locked archive referenced by P7 source-bound test contracts."""
    cache_root = cache_root.expanduser().absolute()
    results: list[dict[str, Any]] = []
    for target in required_artifact_targets():
        resolved, error = download_artifact(
            target["artifact"], cache_root, allowed_hosts=set(target["allowed_hosts"]),
        )
        if error or not isinstance(resolved, dict):
            raise RuntimeError(f"{target['registry_entry_id']}: {error or 'artifact download returned no record'}")
        digest = resolved.get("computed_digests", {}).get("sha256")
        if digest != target["artifact_sha256"]:
            raise RuntimeError(f"{target['registry_entry_id']}: downloaded artifact SHA-256 mismatch")
        results.append({
            "registry_entry_id": target["registry_entry_id"],
            "package": target["package"],
            "version": target["version"],
            "artifact_sha256": digest,
            "size_bytes": resolved.get("size_bytes"),
            "status": "verified",
        })
    for target in required_profile_lock_targets():
        _install_profile_lock(target, cache_root)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-root", type=Path, default=ARTIFACT_CACHE)
    options = parser.parse_args(argv)
    try:
        results = prepare_source_audit_cache(options.cache_root)
        profile_locks = required_profile_lock_targets()
    except (OSError, ValueError, RuntimeError) as error:
        print(f"P7 source-audit cache preparation failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps({
        "status": "passed",
        "verified_artifacts": len(results),
        "artifacts": results,
        "verified_profile_locks": len(profile_locks),
        "profile_locks": [
            {key: target[key] for key in ("registry_entry_id", "package", "sha256", "size_bytes", "status")}
            for target in profile_locks
        ],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
