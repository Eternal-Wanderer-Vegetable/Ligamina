"""Generate hash-locked per-entry dependency profiles without running scripts."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from .artifacts import (
    DEFAULT_CACHE_ROOT,
    ArtifactError,
    canonical_json_bytes,
    pretty_json_bytes,
    sha256_bytes,
    sha256_file,
    verify_lock,
)
from .catalog.snapshot import verify_snapshot


ROOT = Path(__file__).resolve().parents[1]
NONEBOT_BASE = ("nonebot2[fastapi]==2.5.0", "nonebot-adapter-onebot==2.4.6", "jsonschema==4.25.1")
KOISHI_BASE = ("@koishijs/core@4.18.11",)
PLATFORMS = {
    "windows-x86_64": "windows",
    "linux-x86_64": "linux",
}
PROFILES_RESOLVER_VERSION = 2


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _store_lock(cache_root: Path, ecosystem: str, raw: bytes) -> dict[str, Any]:
    digest = sha256_bytes(raw)
    relative = Path("profile-locks") / ecosystem / "sha256" / digest[:2] / digest
    path = cache_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and sha256_file(path) != digest:
        raise ArtifactError(f"profile cache corruption at {path}")
    if not path.exists():
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
        os.replace(temporary, path)
    return {"sha256": digest, "cache_path": relative.as_posix(), "size_bytes": len(raw)}


def _dependency_count(lock_text: str) -> int:
    count = 0
    for line in lock_text.splitlines():
        value = line.strip().rstrip("\\").strip()
        if not value or value.startswith(("#", "--hash=")):
            continue
        try:
            Requirement(value)
        except InvalidRequirement:
            continue
        count += 1
    return count


def _direct_requirement_block(lock_text: str, name: str) -> str | None:
    target = canonicalize_name(name)
    blocks: list[str] = []
    current: list[str] = []
    for line in lock_text.splitlines():
        starts_requirement = bool(line and not line[0].isspace() and not line.startswith("#"))
        if starts_requirement:
            if current:
                blocks.append("\n".join(current))
            current = [line]
        elif current:
            current.append(line)
    if current:
        blocks.append("\n".join(current))
    for block in blocks:
        first = block.splitlines()[0].strip().rstrip("\\").strip()
        try:
            requirement = Requirement(first)
        except InvalidRequirement:
            continue
        if canonicalize_name(requirement.name) == target:
            return block
    return None


def _python_failure(stderr: str, stdout: str) -> tuple[str, str]:
    combined = f"{stderr}\n{stdout}".lower()
    if "no solution found" in combined or "resolutionimpossible" in combined or "conflict" in combined:
        if "requires-python" in combined or "python version" in combined:
            return "requires_python_mismatch", "The pinned Python profile cannot satisfy the resolved metadata."
        return "dependency_conflict", "The fixed host and plugin requirements have no common solution."
    if "no build" in combined or "source distribution" in combined or "sdist" in combined:
        return "dependency_source_build_required", "A dependency needs a source build; only S2-04 OS Workers may run it."
    if "no wheels" in combined or "no compatible" in combined:
        return "no_compatible_distribution", "No compatible binary distribution exists for this profile."
    if "not found" in combined or "404" in combined:
        return "dependency_unavailable", "A pinned dependency is unavailable from the configured registry."
    return "profile_resolution_failed", "The package manager failed without a more specific classification."


def _npm_failure(stdout: str, stderr: str) -> str:
    combined = f"{stdout}\n{stderr}"
    lowered = combined.lower()
    if "eresolve" in lowered and "peer" in lowered:
        return "peer_dependency_conflict"
    if "ebadengine" in lowered or "not compatible with your version of node" in lowered:
        return "requires_node_mismatch"
    if "eunsupportedprotocol" in lowered or "workspace:" in lowered:
        return "unsupported_workspace_dependency"
    if "e404" in lowered:
        return "dependency_package_unavailable"
    if "etarget" in lowered:
        return "dependency_version_unavailable"
    return "profile_resolution_failed"


def _resolve_python_profile(
    entry: dict[str, Any],
    artifact_entry: dict[str, Any],
    platform_id: str,
    cache_root: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    distribution_name = artifact_entry.get("distribution_name") or entry.get("package")
    version = entry.get("advertised_version")
    platform_record = next((item for item in artifact_entry.get("artifacts", []) if item.get("platform") == platform_id), None)
    artifact = platform_record.get("artifact") if platform_record else None
    if not isinstance(artifact, dict):
        entry_status = artifact_entry.get("status", "artifact_unavailable")
        if entry_status == "metadata_unavailable":
            entry_status = "unknown"
        return {
            "status": platform_record.get("status", entry_status) if platform_record else entry_status,
            "reason_code": (platform_record.get("reason_code") or platform_record.get("status")) if platform_record else artifact_entry.get("reason_code"),
            "profile_id": f"nonebot-2.5.0-onebot-2.4.6-py3.12.14-{platform_id}",
            "dependency_lock": None,
        }
    if artifact.get("build_required"):
        return {
            "status": "source_build_required",
            "reason_code": "source_build_required",
            "profile_id": f"nonebot-2.5.0-onebot-2.4.6-py3.12.14-{platform_id}",
            "artifact_sha256": artifact.get("sha256"),
            "dependency_lock": None,
        }
    if artifact.get("packagetype") != "bdist_wheel":
        return {
            "status": "source_build_required",
            "reason_code": "source_build_required",
            "profile_id": f"nonebot-2.5.0-onebot-2.4.6-py3.12.14-{platform_id}",
            "artifact_sha256": artifact.get("sha256"),
            "dependency_lock": None,
        }
    cache_path = cache_root / str(artifact["cache_path"])
    if not cache_path.is_file() or sha256_file(cache_path) != artifact.get("computed_digests", {}).get("sha256"):
        return {
            "status": "cache_hash_mismatch",
            "reason_code": "cache_hash_mismatch",
            "profile_id": f"nonebot-2.5.0-onebot-2.4.6-py3.12.14-{platform_id}",
            "artifact_sha256": artifact.get("sha256"),
            "dependency_lock": None,
        }

    uv = shutil.which("uv")
    if not uv:
        return {"status": "tool_unavailable", "reason_code": "uv_unavailable", "profile_id": f"nonebot-2.5.0-onebot-2.4.6-py3.12.14-{platform_id}", "dependency_lock": None}
    with tempfile.TemporaryDirectory(prefix="ligamina-profile-python-") as temporary_dir:
        temporary = Path(temporary_dir)
        input_file = temporary / "requirements.in"
        output_file = temporary / "requirements.txt"
        direct_file = temporary / str(artifact.get("filename") or "plugin.whl")
        shutil.copyfile(cache_path, direct_file)
        requirements = [*NONEBOT_BASE, f"{distribution_name}=={version}"]
        input_file.write_text("\n".join(requirements) + "\n", encoding="utf-8")
        command = [
            uv, "pip", "compile", str(input_file),
            "--output-file", str(output_file),
            "--generate-hashes", "--no-build",
            "--python-version", "3.12.14",
            "--python-platform", PLATFORMS[platform_id],
            "--resolution", "highest", "--prerelease", "disallow",
            "--default-index", "https://pypi.org/simple",
        ]
        try:
            child_env = {
                key: os.environ[key]
                for key in ("PATH", "SystemRoot", "WINDIR", "TEMP", "TMP")
                if key in os.environ
            }
            child_env.update({"UV_CACHE_DIR": str(cache_root / "uv-cache"), "UV_NO_PROGRESS": "1"})
            result = subprocess.run(command, capture_output=True, text=True, timeout=timeout_seconds, check=False, env=child_env)
        except subprocess.TimeoutExpired:
            return {"status": "unknown", "reason_code": "profile_resolution_timeout", "profile_id": f"nonebot-2.5.0-onebot-2.4.6-py3.12.14-{platform_id}", "dependency_lock": None}
        if result.returncode != 0 or not output_file.is_file():
            reason, _ = _python_failure(result.stderr, result.stdout)
            return {
                "status": reason,
                "reason_code": reason,
                "profile_id": f"nonebot-2.5.0-onebot-2.4.6-py3.12.14-{platform_id}",
                "artifact_sha256": artifact.get("sha256"),
                "diagnostic_sha256": sha256_bytes((result.stdout + "\n" + result.stderr).encode("utf-8", errors="replace")),
            }
        lock_text = output_file.read_text(encoding="utf-8")
        direct_block = _direct_requirement_block(lock_text, str(distribution_name))
        required_hash = artifact.get("sha256")
        if direct_block is None:
            return {"status": "artifact_not_in_dependency_lock", "reason_code": "artifact_not_in_dependency_lock", "profile_id": f"nonebot-2.5.0-onebot-2.4.6-py3.12.14-{platform_id}", "dependency_lock": None}
        if required_hash and required_hash not in direct_block:
            return {"status": "artifact_hash_mismatch", "reason_code": "artifact_hash_mismatch", "profile_id": f"nonebot-2.5.0-onebot-2.4.6-py3.12.14-{platform_id}", "dependency_lock": None}
        if not _python_lock_has_hashes(lock_text):
            return {"status": "dependency_integrity_missing", "reason_code": "dependency_integrity_missing", "profile_id": f"nonebot-2.5.0-onebot-2.4.6-py3.12.14-{platform_id}", "dependency_lock": None}
        raw = output_file.read_bytes()
        stored = _store_lock(cache_root, "nonebot", raw)
        return {
            "status": "locked",
            "reason_code": None,
            "profile_id": f"nonebot-2.5.0-onebot-2.4.6-py3.12.14-{platform_id}",
            "artifact_sha256": required_hash,
            "dependency_lock": stored,
            "dependency_count": _dependency_count(lock_text),
            "resolver": "uv pip compile --generate-hashes --no-build",
        }


def _npm_command() -> list[str] | None:
    node = shutil.which("node")
    npm_cli_candidates = [
        Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "nodejs" / "node_modules" / "npm" / "bin" / "npm-cli.js",
        Path(shutil.which("npm") or "").parent / "node_modules" / "npm" / "bin" / "npm-cli.js",
    ]
    npm_cli = next((path for path in npm_cli_candidates if path.is_file()), None)
    return [node, str(npm_cli)] if node and npm_cli else None


def _resolve_koishi_profile(
    entry: dict[str, Any],
    artifact_entry: dict[str, Any],
    cache_root: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    version = entry.get("advertised_version")
    platform_record = next((item for item in artifact_entry.get("artifacts", []) if item.get("platform") == "all"), None)
    artifact = platform_record.get("artifact") if platform_record else None
    profile_id = "koishi-core-4.18.11-node-24.20.0-x86_64-portable-lock"
    if not isinstance(artifact, dict):
        entry_status = artifact_entry.get("status", "artifact_unavailable")
        if entry_status == "metadata_unavailable":
            entry_status = "unknown"
        return {
            "status": platform_record.get("status", entry_status) if platform_record else entry_status,
            "reason_code": (platform_record.get("reason_code") or platform_record.get("status")) if platform_record else artifact_entry.get("reason_code"),
            "profile_id": profile_id,
            "dependency_lock": None,
        }
    cache_path = cache_root / str(artifact["cache_path"])
    if not cache_path.is_file() or sha256_file(cache_path) != artifact.get("computed_digests", {}).get("sha256"):
        return {"status": "cache_hash_mismatch", "reason_code": "cache_hash_mismatch", "profile_id": profile_id, "dependency_lock": None}
    npm_command = _npm_command()
    if not npm_command:
        return {"status": "tool_unavailable", "reason_code": "npm_unavailable", "profile_id": profile_id, "dependency_lock": None}
    with tempfile.TemporaryDirectory(prefix="ligamina-profile-npm-") as temporary_dir:
        temporary = Path(temporary_dir)
        (temporary / "package.json").write_bytes(pretty_json_bytes({
            "name": "ligamina-catalog-profile",
            "version": "0.0.0",
            "private": True,
            "dependencies": {
                "@koishijs/core": "4.18.11",
                str(entry["package"]): str(version),
            },
        }))
        empty_user_config = temporary / "empty.npmrc"
        empty_user_config.write_text("registry=https://registry.npmjs.org/\n", encoding="utf-8")
        empty_global_config = temporary / "empty-global.npmrc"
        empty_global_config.write_text("registry=https://registry.npmjs.org/\n", encoding="utf-8")
        npm_cache = cache_root / "npm-cache"
        npm_cache.mkdir(parents=True, exist_ok=True)
        command = [
            *npm_command, "install", "--package-lock-only", "--ignore-scripts",
            "--strict-peer-deps", "--engine-strict", "--no-audit", "--no-fund", "--lockfile-version=3",
            "--registry=https://registry.npmjs.org/", f"--userconfig={empty_user_config}",
            f"--globalconfig={empty_global_config}", f"--cache={npm_cache}",
        ]
        try:
            child_env = {
                key: os.environ[key]
                for key in ("PATH", "SystemRoot", "WINDIR", "TEMP", "TMP")
                if key in os.environ
            }
            child_env.update({"NPM_CONFIG_REGISTRY": "https://registry.npmjs.org/", "NPM_CONFIG_CACHE": str(npm_cache)})
            result = subprocess.run(command, cwd=temporary, capture_output=True, text=True, timeout=timeout_seconds, check=False, env=child_env)
        except subprocess.TimeoutExpired:
            return {"status": "unknown", "reason_code": "profile_resolution_timeout", "profile_id": profile_id, "dependency_lock": None}
        lock_path = temporary / "package-lock.json"
        if result.returncode != 0 or not lock_path.is_file():
            combined = f"{result.stdout}\n{result.stderr}"
            reason = _npm_failure(result.stdout, result.stderr)
            return {
                "status": reason,
                "reason_code": reason,
                "profile_id": profile_id,
                "diagnostic_sha256": sha256_bytes(combined.encode("utf-8", errors="replace")),
            }
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        package_key = f"node_modules/{entry['package']}"
        locked_plugin = lock.get("packages", {}).get(package_key, {})
        if locked_plugin.get("version") != version:
            return {"status": "version_mismatch", "reason_code": "version_mismatch", "profile_id": profile_id, "dependency_lock": None}
        upstream = artifact.get("upstream_digests", [])
        locked_integrity = locked_plugin.get("integrity")
        if not isinstance(locked_integrity, str) or not _integrity_matches(locked_integrity, upstream):
            return {"status": "artifact_hash_mismatch", "reason_code": "artifact_hash_mismatch", "profile_id": profile_id, "dependency_lock": None}
        if not _npm_lock_has_integrities(lock):
            return {"status": "dependency_integrity_missing", "reason_code": "dependency_integrity_missing", "profile_id": profile_id, "dependency_lock": None}
        raw = lock_path.read_bytes()
        stored = _store_lock(cache_root, "koishi", raw)
        return {
            "status": "locked",
            "reason_code": None,
            "profile_id": profile_id,
            "artifact_sha256": artifact.get("computed_digests", {}).get("sha256"),
            "dependency_lock": stored,
            "dependency_count": max(0, len(lock.get("packages", {})) - 1),
            "resolver": "npm install --package-lock-only --ignore-scripts --strict-peer-deps --engine-strict",
            "portability": "one npm lock shared by x86_64 Windows/Linux; target Worker performs npm ci with scripts disabled",
        }


def _integrity_matches(value: str, expected: list[dict[str, str]]) -> bool:
    for digest in expected:
        encoded = __import__("base64").b64encode(bytes.fromhex(digest["hex"])).decode("ascii")
        if f"{digest['algorithm']}-{encoded}" in value.split():
            return True
    return False


def _python_lock_has_hashes(lock_text: str) -> bool:
    blocks: list[str] = []
    current: list[str] = []
    for line in lock_text.splitlines():
        starts_requirement = bool(line and not line[0].isspace() and not line.startswith("#"))
        if starts_requirement:
            if current:
                blocks.append("\n".join(current))
            current = [line]
        elif current:
            current.append(line)
    if current:
        blocks.append("\n".join(current))
    if not blocks:
        return False
    for block in blocks:
        first = block.splitlines()[0].strip().rstrip("\\").strip()
        try:
            Requirement(first)
        except InvalidRequirement:
            return False
        if not re.search(r"--hash=sha256:[0-9a-f]{64}", block):
            return False
    return True


def _npm_lock_has_integrities(lock: dict[str, Any]) -> bool:
    packages = lock.get("packages")
    if not isinstance(packages, dict) or not packages:
        return False
    for package_path, record in packages.items():
        if package_path == "" or not isinstance(record, dict) or record.get("link") or record.get("inBundle"):
            continue
        resolved = record.get("resolved")
        if isinstance(resolved, str) and resolved.startswith("https://"):
            integrity = record.get("integrity")
            if not isinstance(integrity, str) or not re.search(r"(?:^|\s)(?:sha512|sha384|sha256)-[A-Za-z0-9+/=]+", integrity):
                return False
        elif isinstance(resolved, str) and resolved.startswith("git+") and re.search(r"#[0-9a-f]{40,64}$", resolved):
            continue
        else:
            return False
    return True


def resolve_profiles(
    snapshot_dir: Path,
    cache_root: Path = DEFAULT_CACHE_ROOT,
    *,
    timeout_seconds: int = 600,
    workers: int = 8,
) -> dict[str, Any]:
    snapshot_dir = snapshot_dir.resolve(strict=True)
    cache_root = cache_root.expanduser().absolute()
    verify_snapshot(snapshot_dir)
    artifacts_lock_path = snapshot_dir / "artifacts.lock.json"
    if not artifacts_lock_path.is_file():
        raise ArtifactError("artifacts.lock.json must be generated before dependency profiles")
    artifacts_lock = json.loads(artifacts_lock_path.read_text(encoding="utf-8"))
    artifacts_lock_sha256 = sha256_file(artifacts_lock_path)
    catalog = json.loads((snapshot_dir / "catalog.json").read_text(encoding="utf-8"))
    artifact_entries = {item["registry_entry_id"]: item for item in artifacts_lock.get("entries", [])}
    if list(artifact_entries) != [item["registry_entry_id"] for item in catalog["entries"]]:
        raise ArtifactError("artifact lock denominator/order does not match the source catalog")
    if workers < 1 or workers > 16:
        raise ValueError("workers must be between 1 and 16")

    def resolve_entry(entry: dict[str, Any]) -> dict[str, Any]:
        artifact_entry = artifact_entries[entry["registry_entry_id"]]
        if entry["ecosystem"] == "nonebot":
            platform_profiles = {
                platform: _resolve_python_profile(entry, artifact_entry, platform, cache_root, timeout_seconds)
                for platform in PLATFORMS
            }
        else:
            profile = _resolve_koishi_profile(entry, artifact_entry, cache_root, timeout_seconds)
            platform_profiles = {platform: dict(profile) for platform in PLATFORMS}
        return {
            "registry_entry_id": entry["registry_entry_id"],
            "ecosystem": entry["ecosystem"],
            "package": entry["package"],
            "advertised_version": entry["advertised_version"],
            "profiles": platform_profiles,
        }

    journal = cache_root / "progress" / catalog["snapshot_id"] / "profiles-resolve.ndjson"
    journal.parent.mkdir(parents=True, exist_ok=True)
    reusable: dict[str, dict[str, Any]] = {}
    if journal.is_file():
        for line in journal.read_bytes().splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (record.get("resolver_version") != PROFILES_RESOLVER_VERSION
                    or record.get("snapshot_id") != catalog["snapshot_id"]
                    or record.get("artifacts_lock_sha256") != artifacts_lock_sha256):
                continue
            result = record.get("result")
            if isinstance(result, dict) and _profile_result_reusable(result, cache_root):
                reusable[result["registry_entry_id"]] = result

    results_by_id = dict(reusable)
    remaining = [entry for entry in catalog["entries"] if entry["registry_entry_id"] not in results_by_id]
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(resolve_entry, entry): entry for entry in remaining}
        completed_since_flush = 0
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            results_by_id[result["registry_entry_id"]] = result
            record = {
                "resolver_version": PROFILES_RESOLVER_VERSION,
                "snapshot_id": catalog["snapshot_id"],
                "artifacts_lock_sha256": artifacts_lock_sha256,
                "completed_at_utc": _utc_now(),
                "result": result,
            }
            with journal.open("ab") as stream:
                stream.write(canonical_json_bytes(record) + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
            completed_since_flush += 1
            if completed_since_flush % 100 == 0:
                print(f"profile progress: {len(results_by_id)}/{len(catalog['entries'])}", file=sys.stderr, flush=True)
    results = [results_by_id[entry["registry_entry_id"]] for entry in catalog["entries"]]
    statuses = Counter(
        profile["status"]
        for entry in results
        for profile in entry["profiles"].values()
    )
    lock = {
        "schema_version": 1,
        "resolver_version": PROFILES_RESOLVER_VERSION,
        "snapshot_id": catalog["snapshot_id"],
        "created_at_utc": _utc_now(),
        "source_catalog_sha256": sha256_file(snapshot_dir / "catalog.json"),
        "artifacts_lock_sha256": sha256_file(artifacts_lock_path),
        "profiles": {
            "nonebot": {"host": "NoneBot 2.5.0", "adapter": "nonebot-adapter-onebot 2.4.6", "python": "3.12.14", "architectures": ["x86_64"], "resolver": "uv", "source_build": "OS Worker only"},
            "koishi": {"host": "@koishijs/core 4.18.11", "node": "24.20.0", "architectures": ["x86_64"], "resolver": "npm 12.0.2", "lifecycle_scripts": "disabled"},
        },
        "counts": {
            "registry_entries": len(results),
            "profile_units": sum(len(entry["profiles"]) for entry in results),
            "status": dict(sorted(statuses.items())),
        },
        "entries": results,
    }
    path = snapshot_dir / "profiles.lock.json"
    data = pretty_json_bytes(lock)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)
    path.with_name("profiles.lock.sha256").write_text(f"{sha256_bytes(data)}  profiles.lock.json\n", encoding="ascii")
    return {
        "lock_path": str(path),
        "snapshot_id": catalog["snapshot_id"],
        "registry_entries": len(results),
        "profile_units": lock["counts"]["profile_units"],
        "statuses": dict(sorted(statuses.items())),
        "cache_root": str(cache_root),
        "resume_journal": str(journal),
    }


def _profile_result_reusable(result: dict[str, Any], cache_root: Path) -> bool:
    stable_terminal_statuses = {
        "locked", "source_build_required", "dependency_conflict", "peer_dependency_conflict",
        "requires_python_mismatch", "requires_node_mismatch", "dependency_source_build_required",
        "no_compatible_distribution", "release_unavailable", "release_withdrawn",
        "prerelease_advertised", "invalid_advertised_version", "version_missing", "yanked",
        "withdrawn", "missing_distribution", "missing_upstream_digest", "weak_digest_only",
        "metadata_invalid", "unsupported_ecosystem", "dependency_package_unavailable",
        "dependency_version_unavailable", "unsupported_workspace_dependency",
    }
    profiles = result.get("profiles")
    if not isinstance(profiles, dict) or set(profiles) != set(PLATFORMS):
        return False
    for profile in profiles.values():
        if not isinstance(profile, dict) or profile.get("status") not in stable_terminal_statuses:
            return False
        lock = profile.get("dependency_lock")
        if lock:
            relative = Path(str(lock.get("cache_path", "")))
            if relative.is_absolute() or ".." in relative.parts:
                return False
            path = cache_root / relative
            if not path.is_file() or sha256_file(path) != lock.get("sha256"):
                return False
    return True


def verify_profiles(snapshot_dir: Path, cache_root: Path = DEFAULT_CACHE_ROOT) -> dict[str, Any]:
    snapshot_dir = snapshot_dir.resolve(strict=True)
    cache_root = cache_root.expanduser().absolute()
    verify_snapshot(snapshot_dir)
    verify_lock(snapshot_dir, cache_root)
    path = snapshot_dir / "profiles.lock.json"
    checksum = path.with_name("profiles.lock.sha256")
    if not path.is_file() or not checksum.is_file():
        raise ArtifactError("profiles lock or checksum is missing")
    expected_hash = checksum.read_text(encoding="ascii").split()[0]
    if sha256_file(path) != expected_hash:
        raise ArtifactError("profiles lock checksum mismatch")
    lock = json.loads(path.read_text(encoding="utf-8"))
    catalog = json.loads((snapshot_dir / "catalog.json").read_text(encoding="utf-8"))
    artifacts_lock_path = snapshot_dir / "artifacts.lock.json"
    artifacts_lock = json.loads(artifacts_lock_path.read_text(encoding="utf-8"))
    artifact_entries = {entry["registry_entry_id"]: entry for entry in artifacts_lock.get("entries", [])}
    if lock.get("artifacts_lock_sha256") != sha256_file(artifacts_lock_path):
        raise ArtifactError("profiles lock was generated from a different artifact lock")
    expected_ids = [entry["registry_entry_id"] for entry in catalog["entries"]]
    actual_ids = [entry.get("registry_entry_id") for entry in lock.get("entries", [])]
    if actual_ids != expected_ids or lock.get("snapshot_id") != catalog.get("snapshot_id"):
        raise ArtifactError("profiles lock does not preserve catalog denominator and order")
    if lock.get("resolver_version") != PROFILES_RESOLVER_VERSION:
        raise ArtifactError("profiles lock resolver version is stale")
    expected_units = len(expected_ids) * len(PLATFORMS)
    if lock.get("counts", {}).get("profile_units") != expected_units:
        raise ArtifactError("profiles lock profile-unit denominator is incomplete")
    checked = 0
    checked_unique: set[str] = set()
    profile_statuses: Counter[str] = Counter()
    for entry in lock["entries"]:
        if set(entry.get("profiles", {})) != set(PLATFORMS):
            raise ArtifactError(f"profile platform denominator is incomplete: {entry['registry_entry_id']}")
        artifact_entry = artifact_entries.get(entry["registry_entry_id"])
        if artifact_entry is None:
            raise ArtifactError(f"artifact lock entry is missing: {entry['registry_entry_id']}")
        for platform, profile in entry.get("profiles", {}).items():
            profile_statuses[profile.get("status", "missing_status")] += 1
            dependency_lock = profile.get("dependency_lock")
            if profile.get("status") != "locked":
                if dependency_lock:
                    raise ArtifactError(f"non-locked profile must not carry a usable dependency lock: {entry['registry_entry_id']}")
                continue
            if not dependency_lock:
                raise ArtifactError(f"locked profile has no dependency lock: {entry['registry_entry_id']}")
            relative = Path(dependency_lock["cache_path"])
            if relative.is_absolute() or ".." in relative.parts:
                raise ArtifactError("profile cache path escapes the configured cache root")
            cached = cache_root / relative
            if not cached.is_file() or sha256_file(cached) != dependency_lock.get("sha256"):
                raise ArtifactError(f"profile lock cache hash mismatch: {entry['registry_entry_id']}")
            checked += 1
            checked_unique.add(dependency_lock["sha256"])
            if entry["ecosystem"] == "nonebot":
                lock_text = cached.read_text(encoding="utf-8")
                if not _python_lock_has_hashes(lock_text):
                    raise ArtifactError(f"Python dependency lock is missing package hashes: {entry['registry_entry_id']}")
                expected_base = {
                    "nonebot2": "2.5.0",
                    "nonebot-adapter-onebot": "2.4.6",
                    "jsonschema": "4.25.1",
                }
                for package, version in expected_base.items():
                    block = _direct_requirement_block(lock_text, package)
                    if not block or str(Requirement(block.splitlines()[0].strip().rstrip("\\").strip()).specifier) != f"=={version}":
                        raise ArtifactError(f"Python host baseline drifted in lock {entry['registry_entry_id']}: {package}")
                distribution_name = artifact_entry.get("distribution_name") or entry["package"]
                block = _direct_requirement_block(lock_text, str(distribution_name))
                artifact_record = next((record for record in artifact_entry.get("artifacts", []) if record.get("platform") == platform), None)
                artifact = artifact_record.get("artifact") if artifact_record else None
                if not block or not artifact or artifact.get("sha256") not in block:
                    raise ArtifactError(f"Python direct artifact hash does not match profile lock: {entry['registry_entry_id']}")
                requirement = Requirement(block.splitlines()[0].strip().rstrip("\\").strip())
                if str(requirement.specifier) != f"=={entry['advertised_version']}":
                    raise ArtifactError(f"Python plugin version drifted in profile lock: {entry['registry_entry_id']}")
            elif entry["ecosystem"] == "koishi":
                npm_lock = json.loads(cached.read_text(encoding="utf-8"))
                if not _npm_lock_has_integrities(npm_lock):
                    raise ArtifactError(f"npm dependency lock is missing strong package integrity: {entry['registry_entry_id']}")
                packages = npm_lock.get("packages", {})
                root_dependencies = packages.get("", {}).get("dependencies", {})
                if root_dependencies.get("@koishijs/core") != "4.18.11":
                    raise ArtifactError(f"Koishi host baseline drifted in lock: {entry['registry_entry_id']}")
                package_key = f"node_modules/{entry['package']}"
                package_record = packages.get(package_key, {})
                if package_record.get("version") != entry["advertised_version"]:
                    raise ArtifactError(f"Koishi plugin version drifted in lock: {entry['registry_entry_id']}")
                artifact_record = next((record for record in artifact_entry.get("artifacts", []) if record.get("platform") == "all"), None)
                artifact = artifact_record.get("artifact") if artifact_record else None
                if not artifact or not _integrity_matches(str(package_record.get("integrity", "")), artifact.get("upstream_digests", [])):
                    raise ArtifactError(f"Koishi direct artifact integrity does not match profile lock: {entry['registry_entry_id']}")
    if dict(sorted(profile_statuses.items())) != lock.get("counts", {}).get("status"):
        raise ArtifactError("profile status counts do not match the full result matrix")
    return {
        "status": "passed",
        "snapshot_id": lock["snapshot_id"],
        "registry_entries": len(actual_ids),
        "profile_units": lock.get("counts", {}).get("profile_units"),
        "checked_dependency_locks": checked,
        "unique_dependency_locks": len(checked_unique),
        "profile_statuses": dict(sorted(profile_statuses.items())),
        "lock_sha256": expected_hash,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    resolve_parser = subparsers.add_parser("resolve", help="build per-entry host dependency profiles")
    resolve_parser.add_argument("snapshot", type=Path)
    resolve_parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    resolve_parser.add_argument("--timeout-seconds", type=int, default=600)
    resolve_parser.add_argument("--workers", type=int, default=8)
    verify_parser = subparsers.add_parser("verify", help="verify dependency-lock cache hashes and denominators")
    verify_parser.add_argument("snapshot", type=Path)
    verify_parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    options = parser.parse_args(argv)
    try:
        if options.command == "resolve":
            result = resolve_profiles(options.snapshot, options.cache_root, timeout_seconds=options.timeout_seconds, workers=options.workers)
        else:
            result = verify_profiles(options.snapshot, options.cache_root)
    except (ArtifactError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"profile error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
