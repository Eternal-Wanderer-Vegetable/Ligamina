"""Fetch, freeze, normalize, compare, and verify official plugin catalogs."""

from __future__ import annotations

import argparse
import email.utils
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_ROOT = ROOT / "catalogs"
MAX_DOCUMENT_BYTES = 32 * 1024 * 1024
SOURCE_DEFINITIONS = {
    "nonebot_registry": {
        "url": "https://registry.nonebot.dev/plugins.json",
        "filename": "nonebot-plugins.json",
        "ecosystem": "nonebot",
    },
    "koishi_registry": {
        "url": "https://registry.koishi.chat/index.json",
        "filename": "koishi-index.json",
        "ecosystem": "koishi",
    },
    "koishi_official_plugins_page": {
        "url": "https://koishi.chat/zh-CN/plugins/",
        "filename": "koishi-official-plugins.html",
        "ecosystem": "koishi",
    },
}
HEADER_ALLOWLIST = (
    "ETag", "Last-Modified", "Date", "Age", "Via", "Content-Length",
    "X-GitHub-Request-Id", "X-Served-By", "X-Cache",
)


class CatalogError(RuntimeError):
    """A source or frozen snapshot failed integrity/shape validation."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def pretty_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def stable_entry_id(ecosystem: str, identity: dict[str, Any], occurrence: int) -> str:
    prefix = "nb" if ecosystem == "nonebot" else "ko"
    digest = hashlib.sha256(canonical_json_bytes(identity)).hexdigest()[:24]
    suffix = f"-dup-{occurrence + 1:04d}" if occurrence else ""
    return f"{prefix}-{digest}{suffix}"


def strict_json_loads(raw: bytes, label: str) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-standard JSON constant {value}")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON object key {key!r}")
            result[key] = value
        return result

    try:
        return json.loads(raw.decode("utf-8"), parse_constant=reject_constant, object_pairs_hook=unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise CatalogError(f"{label} is not valid strict UTF-8 JSON: {error}") from error


def _retry_after_seconds(value: str | None, attempt: int) -> float:
    if value:
        try:
            delay = float(value)
            if delay >= 0:
                return delay
        except ValueError:
            try:
                deadline = email.utils.parsedate_to_datetime(value)
                if deadline.tzinfo is None:
                    deadline = deadline.replace(tzinfo=timezone.utc)
                return max(0.0, (deadline - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
    return float(min(2 ** (attempt - 1), 30))


def fetch_document(
    source_key: str,
    url: str,
    *,
    timeout: float = 30,
    max_attempts: int = 3,
    opener: Callable[..., Any] = urllib.request.urlopen,
    sleeper: Callable[[float], None] = time.sleep,
) -> tuple[bytes, dict[str, Any]]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise CatalogError(f"catalog source must use HTTPS: {url}")
    if timeout <= 0 or max_attempts < 1:
        raise CatalogError("timeout and max_attempts must be positive")
    request = urllib.request.Request(url, headers={
        "Accept": "application/json, text/html;q=0.9",
        "User-Agent": "Ligamina-catalog-snapshot/0.1 (+https://github.com/Eternal-Wanderer-Vegetable/Ligamina)",
    })
    last_error: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        retry_header = None
        try:
            with opener(request, timeout=timeout) as response:
                status = getattr(response, "status", 200)
                final_url = response.geturl()
                if status != 200:
                    raise CatalogError(f"{source_key} returned HTTP {status}")
                if urllib.parse.urlparse(final_url).scheme != "https":
                    raise CatalogError(f"{source_key} redirected to a non-HTTPS URL")
                raw = response.read(MAX_DOCUMENT_BYTES + 1)
                if len(raw) > MAX_DOCUMENT_BYTES:
                    raise CatalogError(f"{source_key} exceeded the {MAX_DOCUMENT_BYTES}-byte capture limit")
                if not raw:
                    raise CatalogError(f"{source_key} returned an empty document")
                headers = response.headers
                metadata = {
                    "source_key": source_key,
                    "requested_url": url,
                    "final_url": final_url,
                    "http_status": status,
                    "fetched_at_utc": utc_now(),
                    "response_headers": {
                        name: headers.get(name)
                        for name in HEADER_ALLOWLIST
                        if headers.get(name) is not None
                    },
                    "attempts": attempt,
                    "byte_length": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }
                return raw, metadata
        except urllib.error.HTTPError as error:
            last_error = error
            retry_header = error.headers.get("Retry-After") if error.headers else None
            retryable = error.code == 429 or 500 <= error.code <= 599
            if not retryable or attempt >= max_attempts:
                raise CatalogError(f"{source_key} returned HTTP {error.code}; no catalog was frozen") from error
        except CatalogError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last_error = error
            if attempt >= max_attempts:
                raise CatalogError(f"{source_key} fetch failed after {attempt} attempts: {error}") from error

        delay = _retry_after_seconds(retry_header, attempt)
        if delay > 300:
            raise CatalogError(f"{source_key} Retry-After is {delay:.0f}s; refusing an early retry") from last_error
        sleeper(delay)

    raise CatalogError(f"{source_key} fetch failed: {last_error}")


def derive_snapshot_id(sources: dict[str, dict[str, Any]]) -> str:
    material = [
        {"source_key": key, "url": item.get("final_url") or item.get("requested_url"), "sha256": item["sha256"]}
        for key, item in sorted(sources.items())
    ]
    return "p2-" + hashlib.sha256(canonical_json_bytes(material)).hexdigest()[:20]


def _source_summaries(
    documents: dict[str, tuple[bytes, dict[str, Any]]],
    normalized_counts: dict[str, dict[str, int]],
) -> list[dict[str, Any]]:
    result = []
    for source_key, (raw, metadata) in sorted(documents.items()):
        source = dict(metadata)
        source["raw_path"] = f"raw/{SOURCE_DEFINITIONS[source_key]['filename']}"
        source["byte_length"] = len(raw)
        source["sha256"] = hashlib.sha256(raw).hexdigest()
        if source_key in normalized_counts:
            source["entry_count"] = normalized_counts[source_key].get("registry_entries")
        elif source_key == "koishi_official_plugins_page":
            source["listed_package_count"] = normalized_counts["koishi_registry"].get("official_page_packages")
        result.append(source)
    return result


def compare_catalogs(
    previous: dict[str, Any] | None,
    current_entries: list[dict[str, Any]],
    previous_sources: list[dict[str, Any]] | None,
    current_sources: list[dict[str, Any]],
    current_snapshot_id: str,
) -> dict[str, Any]:
    old_entries = {entry["registry_entry_id"]: entry for entry in (previous or {}).get("entries", [])}
    new_entries = {entry["registry_entry_id"]: entry for entry in current_entries}
    added = sorted(new_entries.keys() - old_entries.keys()) if previous else []
    removed = sorted(old_entries.keys() - new_entries.keys())
    changed = [
        {"registry_entry_id": key,
         "previous_record_sha256": old_entries[key]["source_record_sha256"],
         "current_record_sha256": new_entries[key]["source_record_sha256"]}
        for key in sorted(old_entries.keys() & new_entries.keys())
        if old_entries[key]["source_record_sha256"] != new_entries[key]["source_record_sha256"]
    ]
    old_sources = {item["source_key"]: item for item in (previous_sources or [])}
    new_sources = {item["source_key"]: item for item in current_sources}
    source_changes = []
    for key in sorted(old_sources.keys() | new_sources.keys()):
        old = old_sources.get(key)
        new = new_sources.get(key)
        if old is None or new is None:
            source_changes.append({
                "source_key": key,
                "change": "added" if old is None else "removed",
                "previous_requested_url": old.get("requested_url") if old else None,
                "current_requested_url": new.get("requested_url") if new else None,
                "previous_url": old.get("final_url") if old else None,
                "current_url": new.get("final_url") if new else None,
                "previous_sha256": old.get("sha256") if old else None,
                "current_sha256": new.get("sha256") if new else None,
            })
        elif (old.get("requested_url") != new.get("requested_url")
              or old.get("final_url") != new.get("final_url")
              or old.get("sha256") != new.get("sha256")):
            url_changed = (
                old.get("requested_url") != new.get("requested_url")
                or old.get("final_url") != new.get("final_url")
            )
            source_changes.append({
                "source_key": key,
                "change": "source_and_content_changed" if url_changed and old.get("sha256") != new.get("sha256")
                else "source_changed" if url_changed else "content_changed",
                "previous_requested_url": old.get("requested_url"),
                "current_requested_url": new.get("requested_url"),
                "previous_url": old.get("final_url"),
                "current_url": new.get("final_url"),
                "previous_sha256": old.get("sha256"),
                "current_sha256": new.get("sha256"),
            })
    return {
        "schema_version": 1,
        "snapshot_id": current_snapshot_id,
        "previous_snapshot_id": (previous or {}).get("snapshot_id"),
        "baseline": previous is None,
        "entry_delta": {
            "added": added,
            "removed": removed,
            "changed": changed,
            "detail_mode": "baseline_digest" if previous is None else "per_entry",
            "baseline_entry_count": len(new_entries) if previous is None else None,
            "baseline_entry_id_set_sha256": hashlib.sha256(
                canonical_json_bytes(sorted(new_entries))
            ).hexdigest() if previous is None else None,
        },
        "source_changes": source_changes,
        "counts": {
            "added": len(new_entries) if previous is None else len(added),
            "removed": len(removed),
            "changed": len(changed),
        },
    }


def _git_provenance() -> dict[str, Any]:
    def run(*args: str) -> str | None:
        try:
            result = subprocess.run(
                ["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=10, check=False
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    head = run("rev-parse", "HEAD")
    status = run("status", "--porcelain", "--untracked-files=normal")
    status_bytes = (status or "").encode("utf-8")
    return {
        "git_head": head,
        "git_dirty": bool(status),
        "git_status_sha256": hashlib.sha256(status_bytes).hexdigest(),
    }


def _load_previous(output_root: Path, exclude_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    if not output_root.is_dir():
        return None
    candidates: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for directory in output_root.iterdir():
        if not directory.is_dir() or directory.name == exclude_id or directory.name.startswith("."):
            continue
        catalog_path = directory / "catalog.json"
        provenance_path = directory / "provenance.json"
        if not catalog_path.exists() and not provenance_path.exists():
            continue
        verify_snapshot(directory)
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        candidates.append((catalog.get("created_at_utc", ""), catalog, provenance))
    if not candidates:
        return None
    _, catalog, provenance = max(candidates, key=lambda item: (item[0], item[1].get("snapshot_id", "")))
    return catalog, provenance


def _safe_relative_path(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise CatalogError(f"snapshot path escapes its directory: {relative}")
    return candidate


def verify_snapshot(directory: Path) -> dict[str, Any]:
    directory = directory.resolve()
    try:
        catalog = json.loads((directory / "catalog.json").read_text(encoding="utf-8"))
        provenance = json.loads((directory / "provenance.json").read_text(encoding="utf-8"))
        changes = json.loads((directory / "changes.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CatalogError(f"snapshot is missing valid catalog/provenance/changes files: {error}") from error
    if catalog.get("schema_version") != 1 or provenance.get("schema_version") != 1 or changes.get("schema_version") != 1:
        raise CatalogError("snapshot schema_version must be 1")
    if catalog.get("snapshot_id") != provenance.get("snapshot_id") or catalog.get("snapshot_id") != changes.get("snapshot_id"):
        raise CatalogError("snapshot IDs differ across catalog, provenance, and changes")
    sources = provenance.get("sources")
    if not isinstance(sources, list) or not sources:
        raise CatalogError("snapshot provenance has no sources")
    raw_by_key: dict[str, bytes] = {}
    for source in sources:
        if not isinstance(source, dict):
            raise CatalogError("snapshot provenance source must be an object")
        raw_path = _safe_relative_path(directory, source.get("raw_path", ""))
        try:
            raw = raw_path.read_bytes()
        except OSError as error:
            raise CatalogError(f"snapshot raw source is missing: {source.get('raw_path')}") from error
        if len(raw) != source.get("byte_length") or hashlib.sha256(raw).hexdigest() != source.get("sha256"):
            raise CatalogError(f"snapshot raw integrity mismatch: {source.get('source_key')}")
        raw_by_key[source["source_key"]] = raw
    if derive_snapshot_id({item["source_key"]: item for item in sources}) != catalog["snapshot_id"]:
        raise CatalogError("snapshot ID does not match source raw hashes")

    from .koishi import normalize_koishi, normalize_official_page
    from .nonebot import normalize_nonebot

    if set(raw_by_key) != set(SOURCE_DEFINITIONS):
        raise CatalogError("snapshot must contain the full required source set")
    nonebot_value = strict_json_loads(raw_by_key["nonebot_registry"], "NoneBot registry")
    koishi_value = strict_json_loads(raw_by_key["koishi_registry"], "Koishi registry")
    official_names = set(normalize_official_page(raw_by_key["koishi_official_plugins_page"]))
    nonebot_entries, nonebot_counts = normalize_nonebot(nonebot_value)
    koishi_entries, koishi_counts = normalize_koishi(koishi_value, official_names)
    expected_entries = nonebot_entries + koishi_entries
    if expected_entries != catalog.get("entries"):
        raise CatalogError("normalized entries do not match their frozen raw sources")
    registry_entry_ids = [entry.get("registry_entry_id") for entry in expected_entries]
    if len(set(registry_entry_ids)) != len(registry_entry_ids):
        raise CatalogError("normalized catalog contains duplicate registry_entry_id values")
    expected_counts = {
        "registry_entries": len(expected_entries),
        "by_ecosystem": {
            "nonebot": nonebot_counts["registry_entries"],
            "koishi": koishi_counts["registry_entries"],
        },
        "official_maintained": {
            "nonebot": nonebot_counts["official_maintained_entries"],
            "koishi": koishi_counts["official_maintained_entries"],
        },
        "duplicate_identity_entries": {
            "nonebot": nonebot_counts["duplicate_identity_entries"],
            "koishi": koishi_counts["duplicate_identity_entries"],
        },
        "duplicate_package_entries": {
            "nonebot": nonebot_counts["duplicate_package_entries"],
            "koishi": koishi_counts["duplicate_package_entries"],
        },
        "koishi_official_page_packages": koishi_counts["official_page_packages"],
        "koishi_official_page_packages_missing_from_registry": koishi_counts["official_page_packages_missing_from_registry"],
    }
    if catalog.get("counts") != expected_counts:
        raise CatalogError("catalog entry denominator does not match normalized entries")
    if changes.get("baseline"):
        expected_id_digest = hashlib.sha256(canonical_json_bytes(sorted(registry_entry_ids))).hexdigest()
        delta = changes.get("entry_delta", {})
        if (changes.get("previous_snapshot_id") is not None
                or delta.get("detail_mode") != "baseline_digest"
                or delta.get("baseline_entry_count") != len(expected_entries)
                or delta.get("baseline_entry_id_set_sha256") != expected_id_digest
                or changes.get("counts", {}).get("added") != len(expected_entries)):
            raise CatalogError("baseline entry denominator or ID digest is inconsistent")
    else:
        delta = changes.get("entry_delta", {})
        counts = changes.get("counts", {})
        if any(counts.get(kind) != len(delta.get(kind, [])) for kind in ("added", "removed", "changed")):
            raise CatalogError("catalog delta counts do not match the recorded entry changes")
    if hashlib.sha256(pretty_json_bytes(catalog)).hexdigest() != provenance.get("catalog_sha256"):
        raise CatalogError("normalized catalog SHA-256 mismatch")
    if hashlib.sha256(pretty_json_bytes(changes)).hexdigest() != provenance.get("changes_sha256"):
        raise CatalogError("changes SHA-256 mismatch")
    return {
        "snapshot_id": catalog["snapshot_id"],
        "registry_entries": len(expected_entries),
        "nonebot_entries": nonebot_counts["registry_entries"],
        "koishi_entries": koishi_counts["registry_entries"],
        "nonebot_official_maintained": nonebot_counts["official_maintained_entries"],
        "koishi_official_maintained": koishi_counts["official_maintained_entries"],
        "nonebot_duplicate_package_entries": nonebot_counts["duplicate_package_entries"],
        "koishi_duplicate_package_entries": koishi_counts["duplicate_package_entries"],
        "koishi_official_page_packages_missing_from_registry": koishi_counts["official_page_packages_missing_from_registry"],
        "raw_sources": len(sources),
        "integrity": "passed",
    }


def write_snapshot(
    documents: dict[str, tuple[bytes, dict[str, Any]]],
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    *,
    created_at_utc: str | None = None,
    git: dict[str, Any] | None = None,
) -> Path:
    required = set(SOURCE_DEFINITIONS)
    if set(documents) != required:
        raise CatalogError(f"snapshot requires exactly these sources: {', '.join(sorted(required))}")
    output_root = output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    sources_by_key = {}
    for key, (raw, metadata) in documents.items():
        if key not in SOURCE_DEFINITIONS:
            raise CatalogError(f"unknown catalog source: {key}")
        if not raw:
            raise CatalogError(f"{key} returned an empty document")
        source = dict(metadata)
        source.update({
            "source_key": key,
            "final_url": source.get("final_url") or SOURCE_DEFINITIONS[key]["url"],
            "sha256": hashlib.sha256(raw).hexdigest(),
            "byte_length": len(raw),
        })
        sources_by_key[key] = source

    from .koishi import normalize_koishi, normalize_official_page
    from .nonebot import normalize_nonebot

    nonebot_raw = documents["nonebot_registry"][0]
    koishi_raw = documents["koishi_registry"][0]
    official_raw = documents["koishi_official_plugins_page"][0]
    nonebot_value = strict_json_loads(nonebot_raw, "NoneBot registry")
    koishi_value = strict_json_loads(koishi_raw, "Koishi registry")
    official_names_ordered = normalize_official_page(official_raw)
    official_names = set(official_names_ordered)
    nonebot_entries, nonebot_counts = normalize_nonebot(nonebot_value)
    koishi_entries, koishi_counts = normalize_koishi(koishi_value, official_names)
    entries = nonebot_entries + koishi_entries
    created_at_utc = created_at_utc or utc_now()
    snapshot_id = derive_snapshot_id(sources_by_key)

    source_summaries = _source_summaries(documents, {
        "nonebot_registry": nonebot_counts,
        "koishi_registry": koishi_counts,
    })
    for source in source_summaries:
        if source["source_key"] == "nonebot_registry":
            source["upstream_revision"] = {
                "version": None,
                "commit": None,
                "evidence": "the public registry JSON does not expose an upstream commit or catalog version; HTTP validators are retained",
            }
        elif source["source_key"] == "koishi_registry":
            source["upstream_revision"] = {
                "version": koishi_value.get("version"),
                "time": koishi_value.get("time"),
                "force_time": koishi_value.get("forceTime"),
            }
    previous = _load_previous(output_root, snapshot_id)
    changes = compare_catalogs(
        previous[0] if previous else None,
        entries,
        previous[1].get("sources") if previous else None,
        source_summaries,
        snapshot_id,
    )
    catalog = {
        "schema_version": 1,
        "snapshot_id": snapshot_id,
        "created_at_utc": created_at_utc,
        "counts": {
            "registry_entries": len(entries),
            "by_ecosystem": {"nonebot": nonebot_counts["registry_entries"], "koishi": koishi_counts["registry_entries"]},
            "official_maintained": {
                "nonebot": nonebot_counts["official_maintained_entries"],
                "koishi": koishi_counts["official_maintained_entries"],
            },
            "duplicate_identity_entries": {
                "nonebot": nonebot_counts["duplicate_identity_entries"],
                "koishi": koishi_counts["duplicate_identity_entries"],
            },
            "duplicate_package_entries": {
                "nonebot": nonebot_counts["duplicate_package_entries"],
                "koishi": koishi_counts["duplicate_package_entries"],
            },
            "koishi_official_page_packages": koishi_counts["official_page_packages"],
            "koishi_official_page_packages_missing_from_registry": koishi_counts["official_page_packages_missing_from_registry"],
        },
        "official_maintenance_sources": {
            "nonebot": {"field": "is_official", "source_key": "nonebot_registry"},
            "koishi": {
                "source_key": "koishi_official_plugins_page",
                "packages": sorted(official_names),
                "parser": "npm-package-name-regex-v1",
            },
        },
        "sources": [
            {"source_key": source["source_key"], "url": source["final_url"], "sha256": source["sha256"], "raw_path": source["raw_path"]}
            for source in source_summaries
        ],
        "entries": entries,
    }
    target = output_root / snapshot_id
    if target.exists():
        result = verify_snapshot(target)
        if result["snapshot_id"] != snapshot_id:
            raise CatalogError(f"existing snapshot ID collision: {target}")
        return target

    with tempfile.TemporaryDirectory(prefix=".catalog-snapshot-", dir=output_root) as temporary:
        staging = Path(temporary)
        raw_directory = staging / "raw"
        raw_directory.mkdir()
        for key, (raw, _) in documents.items():
            (raw_directory / SOURCE_DEFINITIONS[key]["filename"]).write_bytes(raw)
        (staging / "catalog.json").write_bytes(pretty_json_bytes(catalog))
        (staging / "changes.json").write_bytes(pretty_json_bytes(changes))
        provenance = {
            "schema_version": 1,
            "snapshot_id": snapshot_id,
            "created_at_utc": created_at_utc,
            "git": git if git is not None else _git_provenance(),
            "sources": source_summaries,
            "previous_snapshot_id": changes.get("previous_snapshot_id"),
            "catalog_sha256": hashlib.sha256(pretty_json_bytes(catalog)).hexdigest(),
            "changes_sha256": hashlib.sha256(pretty_json_bytes(changes)).hexdigest(),
            "entry_identity_policy": "ecosystem identity plus occurrence ordinal; duplicate entries are preserved",
            "execution_scope": "metadata capture only; no package download or plugin execution",
        }
        (staging / "provenance.json").write_bytes(pretty_json_bytes(provenance))
        verify_snapshot(staging)
        os.replace(staging, target)
    return target


def capture(
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    *,
    source_urls: dict[str, str] | None = None,
    timeout: float = 30,
    max_attempts: int = 3,
) -> Path:
    urls = {key: value["url"] for key, value in SOURCE_DEFINITIONS.items()}
    if source_urls:
        unknown = set(source_urls) - set(urls)
        if unknown:
            raise CatalogError(f"unknown source URL override(s): {', '.join(sorted(unknown))}")
        urls.update(source_urls)
    documents = {
        key: fetch_document(key, url, timeout=timeout, max_attempts=max_attempts)
        for key, url in urls.items()
    }
    return write_snapshot(documents, output_root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    capture_parser = subparsers.add_parser("capture", help="fetch and freeze the complete official catalogs")
    capture_parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    capture_parser.add_argument("--nonebot-url", default=SOURCE_DEFINITIONS["nonebot_registry"]["url"])
    capture_parser.add_argument("--koishi-url", default=SOURCE_DEFINITIONS["koishi_registry"]["url"])
    capture_parser.add_argument("--koishi-official-page-url", default=SOURCE_DEFINITIONS["koishi_official_plugins_page"]["url"])
    capture_parser.add_argument("--timeout", type=float, default=30)
    capture_parser.add_argument("--max-attempts", type=int, default=3)
    verify_parser = subparsers.add_parser("verify", help="recompute raw hashes, normalization, and denominators offline")
    verify_parser.add_argument("snapshot", type=Path)
    options = parser.parse_args(argv)
    try:
        if options.command == "capture":
            path = capture(
                options.output_root,
                source_urls={
                    "nonebot_registry": options.nonebot_url,
                    "koishi_registry": options.koishi_url,
                    "koishi_official_plugins_page": options.koishi_official_page_url,
                },
                timeout=options.timeout,
                max_attempts=options.max_attempts,
            )
            print(json.dumps(verify_snapshot(path), ensure_ascii=False, indent=2))
        else:
            print(json.dumps(verify_snapshot(options.snapshot), ensure_ascii=False, indent=2))
    except (CatalogError, ValueError, OSError) as error:
        print(f"catalog error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
