"""Resolve registry-advertised releases and cache their verified artifacts.

This module reads package metadata and archives only. It never installs a
package or invokes a package build backend. Source distributions remain
explicitly marked for a later OS-worker-only build step.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import gzip
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from packaging.tags import compatible_tags, cpython_tags, parse_tag
from packaging.utils import parse_wheel_filename
from packaging.version import InvalidVersion, Version

from .catalog.snapshot import CatalogError, pretty_json_bytes, strict_json_loads, verify_snapshot


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE_ROOT = ROOT.parent / "Ligamina-artifact-cache"
SCHEMA_VERSION = 1
MAX_METADATA_BYTES = 32 * 1024 * 1024
MAX_ARTIFACT_BYTES = 1024 * 1024 * 1024
MAX_ATTEMPTS = 3
HEADER_ALLOWLIST = ("ETag", "Last-Modified", "Date", "Retry-After")
PLATFORM_NAMES = ("windows-x86_64", "linux-x86_64")


class ArtifactError(RuntimeError):
    """A lock or cache failed integrity validation."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _retry_delay(value: str | None, attempt: int) -> float:
    if value:
        try:
            return max(0.0, min(float(value), 300.0))
        except ValueError:
            pass
    return float(min(2 ** (attempt - 1), 30))


def _safe_host(url: str, allowed_hosts: set[str]) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in allowed_hosts:
        raise ArtifactError(f"registry URL is outside the allowed HTTPS hosts: {url}")
    return parsed.hostname


def _cached_metadata_path(cache_root: Path, url: str) -> Path:
    return cache_root / "metadata" / "by-url" / f"{sha256_bytes(url.encode('utf-8'))}.json.gz"


def fetch_json(url: str, cache_root: Path, *, refresh: bool = False) -> tuple[Any, dict[str, Any]]:
    """Fetch exact release metadata, retaining a compressed response for resume."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in {"pypi.org", "registry.npmjs.org"}:
        raise ArtifactError(f"metadata URL is not an official HTTPS endpoint: {url}")
    body_path = _cached_metadata_path(cache_root, url)
    receipt_path = body_path.with_suffix(".receipt.json")
    if not refresh and body_path.is_file() and receipt_path.is_file():
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            raw = gzip.decompress(body_path.read_bytes())
            digest = sha256_bytes(raw)
            if receipt.get("response_sha256") == digest:
                return strict_json_loads(raw, url), {
                    **receipt,
                    "cache_hit": True,
                }
        except (OSError, EOFError, gzip.BadGzipFile, json.JSONDecodeError, ValueError):
            pass

    request = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "Ligamina-artifact-lock/0.1",
    })
    last_error: BaseException | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        retry_after = None
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                final_url = response.geturl()
                if urllib.parse.urlparse(final_url).hostname != parsed.hostname:
                    raise ArtifactError(f"metadata redirected to another host: {url}")
                if getattr(response, "status", 200) != 200:
                    raise ArtifactError(f"metadata request returned HTTP {response.status}: {url}")
                raw = response.read(MAX_METADATA_BYTES + 1)
                if not raw:
                    raise ArtifactError(f"metadata response was empty: {url}")
                if len(raw) > MAX_METADATA_BYTES:
                    raise ArtifactError(f"metadata response exceeded {MAX_METADATA_BYTES} bytes: {url}")
                try:
                    value = strict_json_loads(raw, url)
                except CatalogError as error:
                    raise ArtifactError(f"metadata_invalid:{error}") from error
                receipt = {
                    "requested_url": url,
                    "final_url": final_url,
                    "http_status": 200,
                    "fetched_at_utc": utc_now(),
                    "attempts": attempt,
                    "response_bytes": len(raw),
                    "response_sha256": sha256_bytes(raw),
                    "response_headers": {
                        name: response.headers.get(name)
                        for name in HEADER_ALLOWLIST
                        if response.headers.get(name) is not None
                    },
                    "cache_hit": False,
                }
                body_path.parent.mkdir(parents=True, exist_ok=True)
                compressed = gzip.compress(raw, mtime=0)
                with tempfile.NamedTemporaryFile(dir=body_path.parent, delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(compressed)
                os.replace(temporary, body_path)
                receipt_path.write_bytes(pretty_json_bytes({key: value for key, value in receipt.items() if key != "cache_hit"}))
                return value, receipt
        except urllib.error.HTTPError as error:
            last_error = error
            retry_after = error.headers.get("Retry-After") if error.headers else None
            if error.code not in (429, 500, 502, 503, 504) or attempt == MAX_ATTEMPTS:
                raise ArtifactError(f"metadata HTTP {error.code}: {url}") from error
        except ArtifactError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
            last_error = error
            if attempt == MAX_ATTEMPTS:
                raise ArtifactError(f"metadata fetch failed after {attempt} attempts: {url}: {error}") from error
        time.sleep(_retry_delay(retry_after, attempt))
    raise ArtifactError(f"metadata fetch failed: {url}: {last_error}")


def _target_tags(platform: str) -> dict[str, int]:
    if platform == "windows-x86_64":
        platforms = ["win_amd64"]
    elif platform == "linux-x86_64":
        platforms = [
            "manylinux_2_40_x86_64", "manylinux_2_39_x86_64", "manylinux_2_38_x86_64",
            "manylinux_2_37_x86_64", "manylinux_2_36_x86_64", "manylinux_2_35_x86_64",
            "manylinux_2_34_x86_64", "manylinux_2_33_x86_64", "manylinux_2_32_x86_64",
            "manylinux_2_31_x86_64", "manylinux_2_30_x86_64", "manylinux_2_29_x86_64",
            "manylinux_2_28_x86_64", "manylinux_2_27_x86_64", "manylinux_2_26_x86_64",
            "manylinux_2_25_x86_64", "manylinux_2_24_x86_64", "manylinux_2_23_x86_64",
            "manylinux_2_22_x86_64", "manylinux_2_21_x86_64", "manylinux_2_20_x86_64",
            "manylinux_2_19_x86_64", "manylinux_2_18_x86_64", "manylinux_2_17_x86_64",
            "manylinux2014_x86_64", "manylinux_2_12_x86_64", "manylinux2010_x86_64",
            "manylinux_2_5_x86_64", "manylinux1_x86_64", "linux_x86_64",
        ]
    else:
        raise ValueError(f"unsupported target platform: {platform}")
    tags = list(cpython_tags((3, 12), abis=["cp312", "abi3", "none"], platforms=platforms))
    tags.extend(compatible_tags((3, 12), interpreter="cp312", platforms=platforms))
    result: dict[str, int] = {}
    for index, tag in enumerate(tags):
        result.setdefault(str(tag), index)
    return result


_TARGET_TAGS = {platform: _target_tags(platform) for platform in PLATFORM_NAMES}


def _compatible_python_file(file_record: dict[str, Any], platform: str) -> tuple[int, str] | None:
    filename = file_record.get("filename")
    if not isinstance(filename, str) or not filename.endswith(".whl"):
        return None
    try:
        _, _, _, wheel_tags = parse_wheel_filename(filename)
    except (ValueError, InvalidVersion):
        return None
    priorities = [priority for tag in wheel_tags if (priority := _TARGET_TAGS[platform].get(str(tag))) is not None]
    return (min(priorities), filename) if priorities else None


def _requires_python_allows(requires_python: Any, target: str = "3.12.14") -> bool:
    if not requires_python:
        return True
    try:
        from packaging.specifiers import SpecifierSet

        return SpecifierSet(str(requires_python)).contains(target, prereleases=True)
    except Exception:
        return False


def _choose_pypi_file(release: dict[str, Any], platform: str) -> tuple[dict[str, Any] | None, str | None]:
    files = release.get("urls")
    if not isinstance(files, list) or not files:
        return None, "withdrawn"
    eligible = [item for item in files if isinstance(item, dict) and item.get("yanked") is not True]
    if not eligible:
        return None, "yanked"
    eligible = [item for item in eligible if _requires_python_allows(item.get("requires_python"))]
    if not eligible:
        return None, "requires_python_mismatch"
    wheels = [
        (match[0], item)
        for item in eligible
        if (match := _compatible_python_file(item, platform)) is not None
    ]
    if wheels:
        wheels.sort(key=lambda pair: (pair[0], str(pair[1].get("filename", ""))))
        return wheels[0][1], None
    sdists = [item for item in eligible if item.get("packagetype") == "sdist"]
    if sdists:
        sdists.sort(key=lambda item: (str(item.get("filename", "")), str(item.get("url", ""))))
        return sdists[0], "source_build_required"
    return None, "no_compatible_distribution"


def _sri_digests(integrity: Any) -> list[dict[str, str]]:
    if not isinstance(integrity, str):
        return []
    supported = {"sha512": "sha512", "sha384": "sha384", "sha256": "sha256"}
    result = []
    for token in integrity.split():
        if "?" in token:
            token = token.split("?", 1)[0]
        algorithm, separator, encoded = token.partition("-")
        if not separator or algorithm not in supported:
            continue
        try:
            digest = base64.b64decode(encoded, validate=True)
        except (ValueError, base64.binascii.Error):
            continue
        result.append({"algorithm": supported[algorithm], "hex": digest.hex()})
    return result


def _verify_upstream_digest(path: Path, expected: list[dict[str, str]]) -> dict[str, str]:
    computed = {
        "sha256": hashlib.sha256(),
        "sha384": hashlib.sha384(),
        "sha512": hashlib.sha512(),
    }
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            for digest in computed.values():
                digest.update(chunk)
    values = {name: digest.hexdigest() for name, digest in computed.items()}
    if not expected:
        raise ArtifactError("release metadata has no supported SHA-256/SHA-384/SHA-512 digest")
    if not any(values[item["algorithm"]] == item["hex"] for item in expected):
        raise ArtifactError("downloaded bytes do not match the registry-provided digest")
    return values


def download_artifact(
    artifact: dict[str, Any], cache_root: Path, *, allowed_hosts: set[str]
) -> tuple[dict[str, Any] | None, str | None]:
    url = artifact.get("url")
    if not isinstance(url, str):
        return None, "missing_artifact_url"
    try:
        host = _safe_host(url, allowed_hosts)
    except ArtifactError:
        return None, "registry_host_mismatch"
    expected = artifact.get("upstream_digests")
    if not isinstance(expected, list) or not expected:
        return None, "missing_upstream_digest"
    primary = expected[0]
    cache_path = cache_root / "sha256" / str(primary["hex"])[:2] / str(primary["hex"])
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    if cache_path.is_file():
        try:
            verified = _verify_upstream_digest(cache_path, expected)
        except ArtifactError:
            return None, "cache_hash_mismatch"
        return {**artifact, "cache_path": cache_path.relative_to(cache_root).as_posix(), "computed_digests": verified}, None

    request = urllib.request.Request(url, headers={"User-Agent": "Ligamina-artifact-lock/0.1"})
    temp_path: Path | None = None
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            if urllib.parse.urlparse(response.geturl()).hostname != host:
                return None, "artifact_redirect_host_mismatch"
            if getattr(response, "status", 200) != 200:
                return None, f"artifact_http_{response.status}"
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > MAX_ARTIFACT_BYTES:
                return None, "artifact_exceeds_size_limit"
            with tempfile.NamedTemporaryFile(dir=cache_path.parent, delete=False) as stream:
                temp_path = Path(stream.name)
                copied = 0
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    copied += len(chunk)
                    if copied > MAX_ARTIFACT_BYTES:
                        return None, "artifact_exceeds_size_limit"
                    stream.write(chunk)
        if temp_path is None or temp_path.stat().st_size == 0:
            return None, "empty_artifact"
        verified = _verify_upstream_digest(temp_path, expected)
        if artifact.get("sha256") is not None and verified["sha256"] != artifact["sha256"]:
            return None, "artifact_hash_mismatch"
        os.replace(temp_path, cache_path)
        temp_path = None
        return {
            **artifact,
            "cache_path": cache_path.relative_to(cache_root).as_posix(),
            "computed_digests": verified,
            "size_bytes": cache_path.stat().st_size,
        }, None
    except urllib.error.HTTPError as error:
        return None, f"artifact_http_{error.code}"
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
        return None, f"artifact_download_failed:{type(error).__name__}"
    except ArtifactError:
        return None, "artifact_hash_mismatch"
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _pypi_metadata_url(package: str, version: str) -> str:
    project = urllib.parse.quote(package, safe="")
    release = urllib.parse.quote(version, safe="")
    return f"https://pypi.org/pypi/{project}/{release}/json"


def _npm_metadata_url(package: str, version: str) -> str:
    package_path = urllib.parse.quote(package, safe="@")
    release = urllib.parse.quote(version, safe="")
    return f"https://registry.npmjs.org/{package_path}/{release}"


def _release_status(version: Any) -> str | None:
    if not isinstance(version, str) or not version.strip():
        return "version_missing"
    try:
        parsed = Version(version)
    except InvalidVersion:
        return "invalid_advertised_version"
    if parsed.is_prerelease or parsed.is_devrelease:
        return "prerelease_advertised"
    return None


def _metadata_failure_status(error: BaseException) -> tuple[str, str]:
    message = str(error)
    if "metadata HTTP 404" in message:
        return "release_unavailable", "release_unavailable"
    if "metadata HTTP 410" in message:
        return "release_withdrawn", "release_withdrawn"
    if "metadata_invalid:" in message:
        return "metadata_invalid", "metadata_invalid"
    return "metadata_unavailable", "metadata_unavailable"


def _npm_release_status(version: Any) -> str | None:
    if not isinstance(version, str) or not version.strip():
        return "version_missing"
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?", version):
        return "invalid_advertised_version"
    if "-" in version.split("+", 1)[0]:
        return "prerelease_advertised"
    return None


def _nonebot_entry(entry: dict[str, Any], cache_root: Path, refresh: bool) -> dict[str, Any]:
    version = entry.get("advertised_version")
    status = _release_status(version)
    base = {
        "registry_entry_id": entry["registry_entry_id"],
        "ecosystem": "nonebot",
        "package": entry["package"],
        "advertised_version": version,
        "source_metadata": None,
        "artifacts": [],
        "status": status or "unresolved",
        "reason_code": status,
        "build_policy": "sdist_backend_os_worker_only",
        "requirements": [],
        "requires_python": None,
    }
    if status:
        return base
    url = _pypi_metadata_url(str(entry["package"]), str(version))
    try:
        document, receipt = fetch_json(url, cache_root, refresh=refresh)
    except ArtifactError as error:
        status, reason_code = _metadata_failure_status(error)
        base.update({"status": status, "reason_code": reason_code, "metadata_error": str(error)})
        return base
    info = document.get("info") if isinstance(document, dict) else None
    if not isinstance(info, dict):
        base.update({"status": "metadata_invalid", "reason_code": "metadata_invalid", "source_metadata": receipt})
        return base
    actual_version = info.get("version")
    try:
        same_version = Version(str(actual_version)) == Version(str(version))
    except InvalidVersion:
        same_version = False
    if not same_version:
        base.update({"status": "version_mismatch", "reason_code": "version_mismatch", "source_metadata": receipt})
        return base
    base["source_metadata"] = receipt
    base["distribution_name"] = info.get("name")
    base["requires_python"] = info.get("requires_python")
    base["requirements"] = info.get("requires_dist") or []
    if not _requires_python_allows(info.get("requires_python")):
        base.update({"status": "requires_python_mismatch", "reason_code": "requires_python_mismatch"})
        return base
    for platform in PLATFORM_NAMES:
        file_record, file_reason = _choose_pypi_file(document, platform)
        if file_record is None:
            base["artifacts"].append({"platform": platform, "status": file_reason or "no_artifact", "artifact": None})
            continue
        digest = file_record.get("digests", {}).get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            base["artifacts"].append({"platform": platform, "status": "missing_upstream_digest", "artifact": None})
            continue
        selected = {
            "platform": platform,
            "filename": file_record.get("filename"),
            "packagetype": file_record.get("packagetype"),
            "url": file_record.get("url"),
            "upstream_digests": [{"algorithm": "sha256", "hex": digest}],
            "sha256": digest,
            "size_bytes": file_record.get("size"),
            "requires_python": file_record.get("requires_python"),
            "yanked": bool(file_record.get("yanked")),
            "build_required": file_reason == "source_build_required",
        }
        downloaded, error = download_artifact(selected, cache_root, allowed_hosts={"files.pythonhosted.org"})
        base["artifacts"].append({
            "platform": platform,
            "status": error or ("source_build_required" if file_reason else "locked"),
            "reason_code": error or file_reason,
            "artifact": downloaded,
        })
    locked_count = sum(item["artifact"] is not None for item in base["artifacts"])
    if locked_count == len(PLATFORM_NAMES):
        base["status"] = "source_build_required" if any(item["status"] == "source_build_required" for item in base["artifacts"]) else "locked"
        base["reason_code"] = "source_build_required" if base["status"] == "source_build_required" else None
    elif locked_count:
        base["status"] = "partially_available"
        base["reason_code"] = "platform_artifact_missing"
    else:
        reasons = [item["status"] for item in base["artifacts"]]
        terminal = reasons[0] if len(set(reasons)) == 1 else "platform_artifact_missing"
        base["status"] = terminal
        base["reason_code"] = terminal
    return base


def _koishi_entry(entry: dict[str, Any], cache_root: Path, refresh: bool) -> dict[str, Any]:
    version = entry.get("advertised_version")
    base = {
        "registry_entry_id": entry["registry_entry_id"],
        "ecosystem": "koishi",
        "package": entry["package"],
        "advertised_version": version,
        "source_metadata": None,
        "artifacts": [],
        "status": "unresolved",
        "reason_code": None,
        "build_policy": "npm_lifecycle_scripts_disabled",
        "dependencies": {},
        "optional_dependencies": {},
        "peer_dependencies": {},
        "engines": {},
    }
    status = _npm_release_status(version)
    if status:
        base.update({"status": status, "reason_code": status})
        return base
    url = _npm_metadata_url(str(entry["package"]), version)
    try:
        document, receipt = fetch_json(url, cache_root, refresh=refresh)
    except ArtifactError as error:
        status, reason_code = _metadata_failure_status(error)
        base.update({"status": status, "reason_code": reason_code, "metadata_error": str(error)})
        return base
    if not isinstance(document, dict) or document.get("name") != entry.get("package") or document.get("version") != version:
        base.update({"status": "version_mismatch", "reason_code": "version_mismatch", "source_metadata": receipt})
        return base
    dist = document.get("dist")
    if not isinstance(dist, dict):
        base.update({"status": "missing_distribution", "reason_code": "missing_distribution", "source_metadata": receipt})
        return base
    base["source_metadata"] = receipt
    base["dependencies"] = document.get("dependencies") or {}
    base["optional_dependencies"] = document.get("optionalDependencies") or {}
    base["peer_dependencies"] = document.get("peerDependencies") or {}
    base["peer_dependencies_meta"] = document.get("peerDependenciesMeta") or {}
    base["engines"] = document.get("engines") or {}
    tarball = dist.get("tarball")
    expected = _sri_digests(dist.get("integrity"))
    if not expected:
        sha1 = dist.get("shasum")
        if isinstance(sha1, str) and re.fullmatch(r"[0-9a-f]{40}", sha1):
            base.update({"status": "weak_digest_only", "reason_code": "weak_digest_only", "source_metadata": receipt})
            return base
        base.update({"status": "missing_upstream_digest", "reason_code": "missing_upstream_digest"})
        return base
    preferred = next((item for item in expected if item["algorithm"] == "sha512"), expected[0])
    selected = {
        "platform": "all",
        "filename": urllib.parse.urlparse(str(tarball)).path.rsplit("/", 1)[-1],
        "packagetype": "npm-tarball",
        "url": tarball,
        "upstream_digests": expected,
        "sha256": preferred["hex"] if preferred["algorithm"] == "sha256" else None,
        "size_bytes": dist.get("unpackedSize"),
        "build_required": False,
    }
    downloaded, error = download_artifact(selected, cache_root, allowed_hosts={"registry.npmjs.org"})
    if downloaded is None:
        base.update({"status": error or "artifact_unavailable", "reason_code": error or "artifact_unavailable"})
        base["artifacts"].append({"platform": "all", "status": base["status"], "reason_code": base["reason_code"], "artifact": None})
        return base
    base["artifacts"].append({"platform": "all", "status": "locked", "reason_code": None, "artifact": downloaded})
    base["status"] = "deprecated_available" if document.get("deprecated") else "locked"
    base["reason_code"] = "deprecated_release" if document.get("deprecated") else None
    base["deprecated"] = document.get("deprecated")
    return base


def _resolve_entry(entry: dict[str, Any], cache_root: Path, refresh: bool) -> dict[str, Any]:
    try:
        if entry["ecosystem"] == "nonebot":
            return _nonebot_entry(entry, cache_root, refresh)
        if entry["ecosystem"] == "koishi":
            return _koishi_entry(entry, cache_root, refresh)
    except Exception as error:
        return {
            "registry_entry_id": entry.get("registry_entry_id"),
            "ecosystem": entry.get("ecosystem"),
            "package": entry.get("package"),
            "advertised_version": entry.get("advertised_version"),
            "source_metadata": None,
            "artifacts": [],
            "status": "resolver_error",
            "reason_code": "resolver_error",
            "error_type": type(error).__name__,
            "error": str(error),
        }
    return {
        "registry_entry_id": entry.get("registry_entry_id"),
        "ecosystem": entry.get("ecosystem"),
        "package": entry.get("package"),
        "advertised_version": entry.get("advertised_version"),
        "source_metadata": None,
        "artifacts": [],
        "status": "unsupported_ecosystem",
        "reason_code": "unsupported_ecosystem",
    }


def _write_lock(snapshot_dir: Path, lock: dict[str, Any]) -> Path:
    target = snapshot_dir / "artifacts.lock.json"
    data = pretty_json_bytes(lock)
    temporary = target.with_suffix(".json.tmp")
    temporary.write_bytes(data)
    os.replace(temporary, target)
    checksum = target.with_name("artifacts.lock.sha256")
    checksum.write_text(f"{sha256_bytes(data)}  artifacts.lock.json\n", encoding="ascii")
    return target


def resolve_snapshot(
    snapshot_dir: Path,
    cache_root: Path = DEFAULT_CACHE_ROOT,
    *,
    workers: int = 8,
    refresh_metadata: bool = False,
) -> dict[str, Any]:
    snapshot_dir = snapshot_dir.resolve(strict=True)
    cache_root = cache_root.expanduser().absolute()
    if workers < 1 or workers > 32:
        raise ValueError("workers must be between 1 and 32")
    verify_snapshot(snapshot_dir)
    catalog = json.loads((snapshot_dir / "catalog.json").read_text(encoding="utf-8"))
    entries = catalog["entries"]
    cache_root.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any] | None] = [None] * len(entries)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(_resolve_entry, entry, cache_root, refresh_metadata): index
            for index, entry in enumerate(entries)
        }
        for future in concurrent.futures.as_completed(future_map):
            results[future_map[future]] = future.result()
    resolved = [item for item in results if item is not None]
    statuses = Counter(item["status"] for item in resolved)
    lock = {
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": catalog["snapshot_id"],
        "created_at_utc": utc_now(),
        "source_catalog_sha256": sha256_file(snapshot_dir / "catalog.json"),
        "resolution_policy": {
            "version_source": "exact catalog.advertised_version; no fallback or substitution",
            "python_profile": "NoneBot 2.5.0 / OneBot adapter 2.4.6 / CPython 3.12.14 / x86_64 Windows and Linux",
            "koishi_profile": "Koishi core 4.18.11 / Node 24.20.0 / x86_64 Windows and Linux",
            "stable_release_policy": "prerelease and dev releases are explicit terminals; advertised versions are not silently replaced",
            "source_build_policy": "source archives may be locked, but build backends are never invoked here; S2-04 OS Worker is required",
            "cache_layout": "sha256/<first-two-hex>/<upstream-primary-digest>",
            "metadata_cache_layout": "metadata/by-url/<sha256-url>.json.gz",
            "maximum_artifact_bytes": MAX_ARTIFACT_BYTES,
        },
        "counts": {
            "registry_entries": len(entries),
            "resolved_entries": len(resolved),
            "status": dict(sorted(statuses.items())),
            "unresolved_entries": sum(item["status"] in {"unresolved", "resolver_error"} for item in resolved),
            "locked_artifacts": sum(
                artifact.get("artifact") is not None
                for item in resolved
                for artifact in item.get("artifacts", [])
            ),
        },
        "entries": resolved,
    }
    path = _write_lock(snapshot_dir, lock)
    return {
        "lock_path": str(path),
        "snapshot_id": catalog["snapshot_id"],
        "registry_entries": len(entries),
        "resolved_entries": len(resolved),
        "statuses": dict(sorted(statuses.items())),
        "locked_artifacts": lock["counts"]["locked_artifacts"],
        "cache_root": str(cache_root),
    }


def verify_lock(snapshot_dir: Path, cache_root: Path = DEFAULT_CACHE_ROOT) -> dict[str, Any]:
    snapshot_dir = snapshot_dir.resolve(strict=True)
    cache_root = cache_root.expanduser().absolute()
    verify_snapshot(snapshot_dir)
    lock_path = snapshot_dir / "artifacts.lock.json"
    checksum_path = snapshot_dir / "artifacts.lock.sha256"
    if not lock_path.is_file() or not checksum_path.is_file():
        raise ArtifactError("artifact lock or checksum is missing")
    expected_file_hash = checksum_path.read_text(encoding="ascii").split()[0]
    if sha256_file(lock_path) != expected_file_hash:
        raise ArtifactError("artifact lock checksum mismatch")
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    catalog = json.loads((snapshot_dir / "catalog.json").read_text(encoding="utf-8"))
    expected_ids = [entry["registry_entry_id"] for entry in catalog["entries"]]
    actual_ids = [entry.get("registry_entry_id") for entry in lock.get("entries", [])]
    if lock.get("snapshot_id") != catalog.get("snapshot_id") or actual_ids != expected_ids:
        raise ArtifactError("artifact lock does not preserve the exact catalog denominator and order")
    checked = 0
    failures: list[str] = []
    for entry in lock["entries"]:
        for record in entry.get("artifacts", []):
            artifact = record.get("artifact")
            if not isinstance(artifact, dict):
                continue
            relative = Path(str(artifact.get("cache_path", "")))
            if relative.is_absolute() or ".." in relative.parts:
                failures.append(f"{entry['registry_entry_id']}:unsafe_cache_path")
                continue
            path = cache_root / relative
            expected_sha = artifact.get("computed_digests", {}).get("sha256")
            if not path.is_file() or sha256_file(path) != expected_sha:
                failures.append(f"{entry['registry_entry_id']}:cache_hash_mismatch")
                continue
            try:
                _verify_upstream_digest(path, artifact.get("upstream_digests", []))
            except ArtifactError:
                failures.append(f"{entry['registry_entry_id']}:upstream_hash_mismatch")
                continue
            checked += 1
    if failures:
        raise ArtifactError(f"{len(failures)} cached artifact(s) failed verification: {failures[:10]}")
    return {
        "status": "passed",
        "snapshot_id": lock["snapshot_id"],
        "registry_entries": len(actual_ids),
        "checked_artifacts": checked,
        "lock_sha256": expected_file_hash,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    resolve_parser = subparsers.add_parser("resolve", help="lock advertised versions and cache registry artifacts")
    resolve_parser.add_argument("snapshot", type=Path)
    resolve_parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    resolve_parser.add_argument("--workers", type=int, default=8)
    resolve_parser.add_argument("--refresh-metadata", action="store_true")
    verify_parser = subparsers.add_parser("verify", help="verify the complete lock and cached artifact hashes")
    verify_parser.add_argument("snapshot", type=Path)
    verify_parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    options = parser.parse_args(argv)
    try:
        if options.command == "resolve":
            result = resolve_snapshot(
                options.snapshot,
                options.cache_root,
                workers=options.workers,
                refresh_metadata=options.refresh_metadata,
            )
        else:
            result = verify_lock(options.snapshot, options.cache_root)
    except (ArtifactError, OSError, ValueError) as error:
        print(f"artifact error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
