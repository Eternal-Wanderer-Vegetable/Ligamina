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
PLATFORM_IDS = {platform: platform_id for platform_id, platform in PLATFORMS.items()}
PROFILES_RESOLVER_VERSION = 2
DEFAULT_PYTHON_VERSION = "3.12.14"
WINDOWS_PIP_BOOTSTRAP = {
    "version": "25.3",
    "filename": "pip-25.3-py3-none-any.whl",
    "metadata_url": "https://pypi.org/pypi/pip/25.3/json",
    "sha256": "9655943313a94722b7774661c21049070f6bbb0a1516bf02f7c8d5d9201514cd",
}


def _python_profile_id(version: str, platform_id: str) -> str:
    return f"nonebot-2.5.0-onebot-2.4.6-py{version}-{platform_id}"


def _merge_runtime_profiles(
    base_lock: dict[str, Any] | None,
    selected_runtime_profiles: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Keep runtime overrides from the base lock when resolving a partial cohort."""
    merged: dict[str, dict[str, Any]] = {}
    if base_lock is not None:
        profile_specs = base_lock.get("profiles")
        nonebot_spec = profile_specs.get("nonebot") if isinstance(profile_specs, dict) else None
        base_runtime_profiles = (
            nonebot_spec.get("runtime_profiles_by_entry", {})
            if isinstance(nonebot_spec, dict) else {}
        )
        if not isinstance(base_runtime_profiles, dict):
            raise ArtifactError("base profiles lock has an invalid Windows runtime override map")
        for entry_id, runtime in base_runtime_profiles.items():
            if not isinstance(entry_id, str) or not isinstance(runtime, dict):
                raise ArtifactError("base profiles lock has an invalid Windows runtime override entry")
            merged[entry_id] = dict(runtime)
    for entry_id, runtime in selected_runtime_profiles.items():
        if not isinstance(entry_id, str) or not isinstance(runtime, dict):
            raise ArtifactError("selected Windows runtime override is invalid")
        merged[entry_id] = dict(runtime)
    return merged


def _load_windows_runtime_admission(path: Path) -> dict[str, Any]:
    path = Path(path).expanduser().resolve(strict=True)
    raw = path.read_bytes()
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ArtifactError(f"Windows runtime admission report is invalid JSON: {path}") from error
    if (
        not isinstance(document, dict)
        or document.get("task_id") != "P3-04"
        or document.get("kind") != "phase3_worker_admission"
        or document.get("platform") != "windows"
        or document.get("status") != "admitted"
        or document.get("execution_scope") != "worker_runtime_profile_only_no_catalog_plugins"
    ):
        raise ArtifactError("Windows runtime admission report does not prove an admitted P3-04 runtime profile")
    runtime = document.get("runtime_profile")
    if not isinstance(runtime, dict):
        raise ArtifactError("Windows runtime admission report has no runtime profile")
    required = {
        "profile_id": "windows-cpython-3.12.10-official-embed-amd64-psf-signed",
        "version": "3.12.10",
        "architecture": "win_amd64",
        "distribution": "python.org embeddable package",
        "archive_url": "https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip",
        "archive_sha256": "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3",
        "executable_sha256": "4d6f5f81a4bca11191c4c7c6b43632694d0a4ce74e068619d8fdc161d469859a",
        "authenticode_status": "Valid",
        "signer": "Python Software Foundation",
    }
    if any(runtime.get(key) != value for key, value in required.items()):
        raise ArtifactError("Windows runtime admission report does not match the frozen signed CPython 3.12.10 profile")
    profile_core = {key: value for key, value in runtime.items() if key != "profile_sha256"}
    if runtime.get("profile_sha256") != sha256_bytes(canonical_json_bytes(profile_core)):
        raise ArtifactError("Windows runtime profile SHA-256 is invalid")
    capabilities = document.get("capabilities")
    if not isinstance(capabilities, dict) or capabilities.get("claim_allowed_for_profile") is not True:
        raise ArtifactError("Windows runtime admission does not allow claims for this exact profile")
    try:
        admission_path = path.relative_to(ROOT).as_posix()
    except ValueError:
        admission_path = path.as_posix()
    return {
        **{key: runtime[key] for key in required},
        "profile_sha256": runtime["profile_sha256"],
        "admission_report_path": admission_path,
        "admission_report_sha256": sha256_bytes(raw),
    }


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
    python_version: str = DEFAULT_PYTHON_VERSION,
    runtime_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    profile_id = _python_profile_id(python_version, platform_id)
    runtime_profile_id = runtime_profile.get("profile_id") if runtime_profile else None
    identity = {"profile_id": profile_id}
    if runtime_profile_id:
        identity["runtime_profile_id"] = runtime_profile_id
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
            **identity,
            "dependency_lock": None,
        }
    if artifact.get("build_required"):
        return {
            "status": "source_build_required",
            "reason_code": "source_build_required",
            **identity,
            "artifact_sha256": artifact.get("sha256"),
            "dependency_lock": None,
        }
    if artifact.get("packagetype") != "bdist_wheel":
        return {
            "status": "source_build_required",
            "reason_code": "source_build_required",
            **identity,
            "artifact_sha256": artifact.get("sha256"),
            "dependency_lock": None,
        }
    cache_path = cache_root / str(artifact["cache_path"])
    if not cache_path.is_file() or sha256_file(cache_path) != artifact.get("computed_digests", {}).get("sha256"):
        return {
            "status": "cache_hash_mismatch",
            "reason_code": "cache_hash_mismatch",
            **identity,
            "artifact_sha256": artifact.get("sha256"),
            "dependency_lock": None,
        }

    uv = shutil.which("uv")
    if not uv:
        return {"status": "tool_unavailable", "reason_code": "uv_unavailable", **identity, "dependency_lock": None}
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
            "--python-version", python_version,
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
            # Registry resolution runs on the trusted host; preserve an explicitly
            # configured outbound proxy without forwarding it to OS Workers.
            child_env.update({
                key: os.environ[key]
                for key in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy")
                if key in os.environ
            })
            result = subprocess.run(command, capture_output=True, text=True, timeout=timeout_seconds, check=False, env=child_env)
        except subprocess.TimeoutExpired:
            return {"status": "unknown", "reason_code": "profile_resolution_timeout", **identity, "dependency_lock": None}
        if result.returncode != 0 or not output_file.is_file():
            reason, _ = _python_failure(result.stderr, result.stdout)
            return {
                "status": reason,
                "reason_code": reason,
                **identity,
                "artifact_sha256": artifact.get("sha256"),
                "diagnostic_sha256": sha256_bytes((result.stdout + "\n" + result.stderr).encode("utf-8", errors="replace")),
            }
        lock_text = output_file.read_text(encoding="utf-8")
        direct_block = _direct_requirement_block(lock_text, str(distribution_name))
        required_hash = artifact.get("sha256")
        if direct_block is None:
            return {"status": "artifact_not_in_dependency_lock", "reason_code": "artifact_not_in_dependency_lock", **identity, "dependency_lock": None}
        if required_hash and required_hash not in direct_block:
            return {"status": "artifact_hash_mismatch", "reason_code": "artifact_hash_mismatch", **identity, "dependency_lock": None}
        if not _python_lock_has_hashes(lock_text):
            return {"status": "dependency_integrity_missing", "reason_code": "dependency_integrity_missing", **identity, "dependency_lock": None}
        raw = output_file.read_bytes()
        stored = _store_lock(cache_root, "nonebot", raw)
        return {
            "status": "locked",
            "reason_code": None,
            **identity,
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
            # Registry resolution runs on the trusted host; preserve an explicitly
            # configured outbound proxy without forwarding it to OS Workers.
            child_env.update({
                key: os.environ[key]
                for key in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy")
                if key in os.environ
            })
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
    windows_runtime_admission_report: Path | None = None,
    output_lock_path: Path | None = None,
    base_profile_lock_path: Path | None = None,
    platforms: tuple[str, ...] | None = None,
    entry_ids: tuple[str, ...] | None = None,
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
    selected_platform_names = tuple(("windows", "linux") if platforms is None else platforms)
    if not selected_platform_names or len(set(selected_platform_names)) != len(selected_platform_names):
        raise ValueError("platforms must select windows and/or linux once")
    if any(platform not in PLATFORM_IDS for platform in selected_platform_names):
        raise ValueError("platforms must select windows and/or linux")
    selected_platform_ids = tuple(PLATFORM_IDS[platform] for platform in selected_platform_names)
    runtime_profile = _load_windows_runtime_admission(windows_runtime_admission_report) if windows_runtime_admission_report else None
    if runtime_profile is not None and "windows-x86_64" not in selected_platform_ids:
        raise ValueError("a Windows runtime admission can only be used when resolving Windows profiles")
    python_versions = {platform_id: DEFAULT_PYTHON_VERSION for platform_id in PLATFORMS}
    catalog_entry_ids = [item["registry_entry_id"] for item in catalog["entries"]]
    selected_entry_ids = tuple(catalog_entry_ids if entry_ids is None else entry_ids)
    if not selected_entry_ids or len(set(selected_entry_ids)) != len(selected_entry_ids):
        raise ValueError("entry_ids must contain unique catalog entry identities")
    if set(selected_entry_ids) - set(catalog_entry_ids):
        raise ValueError("entry_ids contains an identity outside the frozen catalog")
    catalog_entries_by_id = {item["registry_entry_id"]: item for item in catalog["entries"]}
    runtime_profiles_by_entry = {}
    if runtime_profile is not None:
        runtime_profiles_by_entry = {
            entry_id: runtime_profile for entry_id in selected_entry_ids
            if catalog_entries_by_id[entry_id].get("ecosystem") == "nonebot"
        }

    base_lock: dict[str, Any] | None = None
    base_profiles_by_id: dict[str, dict[str, Any]] = {}
    if base_profile_lock_path is not None:
        base_path = Path(base_profile_lock_path).expanduser().resolve(strict=True)
        try:
            base_lock = json.loads(base_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise ArtifactError(f"base profiles lock is invalid JSON: {base_path}") from error
        base_checksum = base_path.with_name("profiles.lock.sha256") if base_path.name == "profiles.lock.json" else base_path.with_name(f"{base_path.name}.sha256")
        if not base_checksum.is_file():
            raise ArtifactError(f"base profiles lock checksum is missing: {base_checksum}")
        if sha256_file(base_path) != base_checksum.read_text(encoding="ascii").split()[0]:
            raise ArtifactError("base profiles lock checksum mismatch")
        if (
            not isinstance(base_lock, dict)
            or base_lock.get("snapshot_id") != catalog.get("snapshot_id")
            or base_lock.get("artifacts_lock_sha256") != artifacts_lock_sha256
            or base_lock.get("source_catalog_sha256") != sha256_file(snapshot_dir / "catalog.json")
        ):
            raise ArtifactError("base profiles lock does not match this snapshot and artifact lock")
        base_entries = base_lock.get("entries")
        if not isinstance(base_entries, list):
            raise ArtifactError("base profiles lock has no entry matrix")
        base_profiles_by_id = {item.get("registry_entry_id"): item for item in base_entries if isinstance(item, dict)}
        if list(base_profiles_by_id) != [item["registry_entry_id"] for item in catalog["entries"]]:
            raise ArtifactError("base profiles lock denominator/order does not match the source catalog")
        for item in base_entries:
            profiles = item.get("profiles")
            if not isinstance(profiles, dict) or set(profiles) != set(PLATFORMS):
                raise ArtifactError(f"base profiles lock has an incomplete platform matrix: {item['registry_entry_id']}")
    if base_lock is not None and set(selected_platform_ids) == set(PLATFORMS):
        raise ValueError("a base profiles lock is only useful when resolving a platform subset")
    if base_lock is None and (
        set(selected_platform_ids) != set(PLATFORMS) or set(selected_entry_ids) != set(catalog_entry_ids)
    ):
        raise ValueError("a base profiles lock is required when resolving only selected platforms or entries")
    if base_lock is not None and any(
        platform_id not in base_profiles_by_id[catalog_entry["registry_entry_id"]]["profiles"]
        for catalog_entry in catalog["entries"] for platform_id in set(PLATFORMS) - set(selected_platform_ids)
    ):
        raise ArtifactError("base profiles lock is missing a platform needed for the output matrix")
    runtime_profiles_by_entry = _merge_runtime_profiles(base_lock, runtime_profiles_by_entry)

    def expected_profile_identity(entry: dict[str, Any], platform_id: str) -> tuple[str, str | None]:
        if entry.get("ecosystem") == "nonebot":
            expected_runtime = runtime_profiles_by_entry.get(entry["registry_entry_id"]) if platform_id == "windows-x86_64" else None
            version = expected_runtime["version"] if expected_runtime else python_versions[platform_id]
            return _python_profile_id(version, platform_id), (
                expected_runtime.get("profile_id") if expected_runtime else None
            )
        return "koishi-core-4.18.11-node-24.20.0-x86_64-portable-lock", None

    def reusable_for_configuration(result: dict[str, Any]) -> bool:
        if not _profile_result_reusable(result, cache_root):
            return False
        entry = entries_by_id.get(result.get("registry_entry_id"))
        if entry is None:
            return False
        for platform_id in PLATFORMS:
            profile = result["profiles"].get(platform_id, {})
            profile_id, runtime_id = expected_profile_identity(entry, platform_id)
            if profile.get("profile_id") != profile_id or profile.get("runtime_profile_id") != runtime_id:
                return False
        return True

    entries_by_id = {entry["registry_entry_id"]: entry for entry in catalog["entries"]}

    def resolve_entry(entry: dict[str, Any]) -> dict[str, Any]:
        artifact_entry = artifact_entries[entry["registry_entry_id"]]
        base_profiles = base_profiles_by_id.get(entry["registry_entry_id"], {}).get("profiles", {})
        platform_profiles: dict[str, dict[str, Any]] = {}
        for platform_id in PLATFORMS:
            if platform_id not in selected_platform_ids or entry["registry_entry_id"] not in selected_entry_ids:
                if not base_profiles:
                    raise ArtifactError("a base profiles lock is required when resolving only selected catalog entries")
                platform_profiles[platform_id] = dict(base_profiles[platform_id])
            elif entry["ecosystem"] == "nonebot":
                platform_profiles[platform_id] = _resolve_python_profile(
                    entry, artifact_entry, platform_id, cache_root, timeout_seconds,
                    python_version=(runtime_profiles_by_entry.get(entry["registry_entry_id"], {}).get("version")
                                    if platform_id == "windows-x86_64" else None) or python_versions[platform_id],
                    runtime_profile=(runtime_profiles_by_entry.get(entry["registry_entry_id"])
                                     if platform_id == "windows-x86_64" else None),
                )
            else:
                platform_profiles[platform_id] = _resolve_koishi_profile(entry, artifact_entry, cache_root, timeout_seconds)
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
            if isinstance(result, dict) and reusable_for_configuration(result):
                reusable[result["registry_entry_id"]] = result

    results_by_id: dict[str, dict[str, Any]] = {}
    for entry in catalog["entries"]:
        entry_id = entry["registry_entry_id"]
        if base_lock is not None and entry_id not in selected_entry_ids:
            profiles = {platform_id: dict(base_profiles_by_id[entry_id]["profiles"][platform_id]) for platform_id in PLATFORMS}
        elif entry_id in reusable:
            profiles = {
                **({
                    platform_id: dict(base_profiles_by_id[entry_id]["profiles"][platform_id])
                    for platform_id in set(PLATFORMS) - set(selected_platform_ids)
                } if base_lock is not None else {}),
                **{
                    platform_id: dict(reusable[entry_id]["profiles"][platform_id])
                    for platform_id in selected_platform_ids
                },
            }
        else:
            continue
        results_by_id[entry_id] = {
            "registry_entry_id": entry_id,
            "ecosystem": entry["ecosystem"],
            "package": entry["package"],
            "advertised_version": entry["advertised_version"],
            "profiles": profiles,
        }
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
    nonebot_profile_spec: dict[str, Any] = {
        "host": "NoneBot 2.5.0", "adapter": "nonebot-adapter-onebot 2.4.6",
        "python": DEFAULT_PYTHON_VERSION, "architectures": ["x86_64"], "resolver": "uv",
        "source_build": "OS Worker only",
    }
    if any(version != DEFAULT_PYTHON_VERSION for version in python_versions.values()):
        nonebot_profile_spec["python_by_platform"] = dict(python_versions)
    if runtime_profiles_by_entry:
        nonebot_profile_spec["runtime_profiles_by_entry"] = runtime_profiles_by_entry
        nonebot_profile_spec["bootstrap_installer"] = WINDOWS_PIP_BOOTSTRAP
    lock = {
        "schema_version": 1,
        "resolver_version": PROFILES_RESOLVER_VERSION,
        "snapshot_id": catalog["snapshot_id"],
        "created_at_utc": _utc_now(),
        "source_catalog_sha256": sha256_file(snapshot_dir / "catalog.json"),
        "artifacts_lock_sha256": sha256_file(artifacts_lock_path),
        "profiles": {
            "nonebot": nonebot_profile_spec,
            "koishi": {"host": "@koishijs/core 4.18.11", "node": "24.20.0", "architectures": ["x86_64"], "resolver": "npm 12.0.2", "lifecycle_scripts": "disabled"},
        },
        "counts": {
            "registry_entries": len(results),
            "profile_units": sum(len(entry["profiles"]) for entry in results),
            "status": dict(sorted(statuses.items())),
        },
        "entries": results,
    }
    path = Path(output_lock_path).expanduser().absolute() if output_lock_path else snapshot_dir / "profiles.lock.json"
    checksum_path = path.with_name("profiles.lock.sha256") if path.name == "profiles.lock.json" else path.with_name(f"{path.name}.sha256")
    if output_lock_path is not None and (path.exists() or checksum_path.exists()):
        raise ArtifactError(f"refusing to overwrite an existing alternate profiles lock: {path}")
    data = pretty_json_bytes(lock)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)
    checksum_path.write_text(f"{sha256_bytes(data)}  {path.name}\n", encoding="ascii")
    return {
        "lock_path": str(path),
        "snapshot_id": catalog["snapshot_id"],
        "registry_entries": len(results),
        "profile_units": lock["counts"]["profile_units"],
        "statuses": dict(sorted(statuses.items())),
        "cache_root": str(cache_root),
        "selected_platforms": list(selected_platform_ids),
        "selected_entries": list(selected_entry_ids),
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


def verify_profiles(
    snapshot_dir: Path,
    cache_root: Path = DEFAULT_CACHE_ROOT,
    *,
    profile_lock_path: Path | None = None,
) -> dict[str, Any]:
    snapshot_dir = snapshot_dir.resolve(strict=True)
    cache_root = cache_root.expanduser().absolute()
    verify_snapshot(snapshot_dir)
    verify_lock(snapshot_dir, cache_root)
    path = Path(profile_lock_path).expanduser().resolve(strict=True) if profile_lock_path else snapshot_dir / "profiles.lock.json"
    checksum = path.with_name("profiles.lock.sha256") if path.name == "profiles.lock.json" else path.with_name(f"{path.name}.sha256")
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
    nonebot_spec = lock.get("profiles", {}).get("nonebot", {})
    default_version = nonebot_spec.get("python") if isinstance(nonebot_spec, dict) else None
    python_versions = nonebot_spec.get("python_by_platform") if isinstance(nonebot_spec, dict) else None
    if python_versions is None:
        if not isinstance(default_version, str):
            raise ArtifactError("profiles lock has no NoneBot Python version identity")
        python_versions = {platform_id: default_version for platform_id in PLATFORMS}
    if not isinstance(python_versions, dict) or set(python_versions) != set(PLATFORMS) or any(
        not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version)
        for version in python_versions.values()
    ):
        raise ArtifactError("profiles lock has an invalid per-platform Python runtime matrix")
    runtime_profiles_by_entry = nonebot_spec.get("runtime_profiles_by_entry", {}) if isinstance(nonebot_spec, dict) else {}
    if not isinstance(runtime_profiles_by_entry, dict) or set(runtime_profiles_by_entry) - set(expected_ids):
        raise ArtifactError("profiles lock has an invalid runtime profile map")
    if runtime_profiles_by_entry and nonebot_spec.get("bootstrap_installer") != WINDOWS_PIP_BOOTSTRAP:
        raise ArtifactError("profiles lock does not bind the frozen offline Windows bootstrap installer")
    if not runtime_profiles_by_entry and "bootstrap_installer" in nonebot_spec:
        raise ArtifactError("profiles lock has an unused Windows bootstrap installer identity")
    entries_by_id = {item["registry_entry_id"]: item for item in lock["entries"]}
    for entry_id, runtime_profile in runtime_profiles_by_entry.items():
        if not isinstance(runtime_profile, dict):
            raise ArtifactError("profiles lock runtime profile entry is invalid")
        report_path = Path(str(runtime_profile.get("admission_report_path", "")))
        if not report_path.is_absolute():
            report_path = ROOT / report_path
        verified_runtime = _load_windows_runtime_admission(report_path)
        if verified_runtime != runtime_profile:
            raise ArtifactError(f"profiles lock Windows runtime profile differs from P3-04 evidence: {entry_id}")
        entry_doc = entries_by_id.get(entry_id)
        if not isinstance(entry_doc, dict) or entry_doc.get("ecosystem") != "nonebot":
            raise ArtifactError(f"Windows runtime override targets a non-NoneBot entry: {entry_id}")
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
            if entry.get("ecosystem") == "nonebot":
                runtime_profile = runtime_profiles_by_entry.get(entry["registry_entry_id"]) if platform == "windows-x86_64" else None
                version = runtime_profile["version"] if runtime_profile else python_versions[platform]
                expected_profile_id = _python_profile_id(version, platform)
                expected_runtime_id = runtime_profile.get("profile_id") if runtime_profile else None
            else:
                expected_profile_id = "koishi-core-4.18.11-node-24.20.0-x86_64-portable-lock"
                expected_runtime_id = None
            if profile.get("profile_id") != expected_profile_id or profile.get("runtime_profile_id") != expected_runtime_id:
                raise ArtifactError(f"profile runtime identity does not match lock metadata: {entry['registry_entry_id']} / {platform}")
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
    resolve_parser.add_argument("--windows-runtime-admission-report", type=Path)
    resolve_parser.add_argument("--output-lock", type=Path)
    resolve_parser.add_argument("--base-lock", type=Path)
    resolve_parser.add_argument("--platform", choices=("windows", "linux"), action="append")
    resolve_parser.add_argument("--entry-id", action="append")
    verify_parser = subparsers.add_parser("verify", help="verify dependency-lock cache hashes and denominators")
    verify_parser.add_argument("snapshot", type=Path)
    verify_parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    verify_parser.add_argument("--profile-lock", type=Path)
    options = parser.parse_args(argv)
    try:
        if options.command == "resolve":
            result = resolve_profiles(
                options.snapshot, options.cache_root,
                timeout_seconds=options.timeout_seconds, workers=options.workers,
                windows_runtime_admission_report=options.windows_runtime_admission_report,
                output_lock_path=options.output_lock,
                base_profile_lock_path=options.base_lock,
                platforms=tuple(options.platform) if options.platform else None,
                entry_ids=tuple(options.entry_id) if options.entry_id else None,
            )
        else:
            result = verify_profiles(options.snapshot, options.cache_root, profile_lock_path=options.profile_lock)
    except (ArtifactError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"profile error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
