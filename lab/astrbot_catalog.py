"""Freeze the AstrBot plugin market into a hash-bound catalog snapshot (AB-03).

The AstrBot market lists plugins with a download URL and a commit SHA, but AstrBot
itself verifies no hash. This tool records the raw market bytes, downloads every
release archive into the content-addressed artifact cache, computes our own digests,
and statically reads each archive (metadata.yaml, requirements.txt, source markers)
without extracting it to disk or executing any plugin code.

Usage:
  python -m lab.astrbot_catalog freeze --output-root catalogs [--cache-root DIR] [--market-file FILE]
  python -m lab.astrbot_catalog verify catalogs/<snapshot-id> [--cache-root DIR]
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MARKET_URL = "https://api.soulter.top/astrbot/plugins"
DEFAULT_CACHE = ROOT.parent / "Ligamina-artifact-cache"
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_MEMBER_BYTES = 2 * 1024 * 1024
VOLATILE_FIELDS = ("download_count", "stars", "updated_at", "logo")
# Source markers with a concrete consequence for hosting on the Ligamina bridge.
MARKERS = {
    "platform_adapter_type": re.compile(r"platform_adapter_type\s*\("),
    "aiocqhttp_event": re.compile(r"AiocqhttpMessageEvent|aiocqhttp_message_event"),
    "llm_tool": re.compile(r"filter\.llm_tool\s*\(|@llm_tool\b"),
    "llm_hooks": re.compile(r"on_llm_request|on_llm_response|request_llm|llm_generate"),
    "command": re.compile(r"filter\.command(_group)?\s*\("),
    "network_client": re.compile(r"\b(aiohttp|httpx|requests)\b"),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _write_json(path: Path, value: Any) -> str:
    data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    path.with_suffix(path.suffix + ".sha256").write_text(f"{digest}  {path.name}\n", encoding="ascii")
    return digest


def _fetch(url: str, *, timeout: float = 60, limit: int = MAX_ARCHIVE_BYTES) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "Ligamina-catalog-freeze/1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        chunks, size = [], 0
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                raise ValueError(f"archive exceeds {limit} bytes")
            chunks.append(chunk)
    return b"".join(chunks)


def _cache_store(cache_root: Path, data: bytes) -> tuple[str, str]:
    digest = hashlib.sha256(data).hexdigest()
    relative = f"sha256/{digest[:2]}/{digest}"
    target = cache_root / relative
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
            handle.write(data)
            temporary = Path(handle.name)
        os.replace(temporary, target)
    return digest, relative


def _simple_yaml(text: str) -> dict[str, Any]:
    """Top-level `key: value` pairs and inline/flow lists only; enough for metadata.yaml."""
    result: dict[str, Any] = {}
    current_list: str | None = None
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0].rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if current_list and line.lstrip().startswith("- ") and raw.startswith((" ", "-")):
            result[current_list].append(line.lstrip()[2:].strip().strip("'\""))
            continue
        match = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", line)
        if not match:
            current_list = None
            continue
        key, value = match.group(1), match.group(2).strip()
        if value == "":
            result[key] = []
            current_list = key
        elif value.startswith("[") and value.endswith("]"):
            result[key] = [item.strip().strip("'\"") for item in value[1:-1].split(",") if item.strip()]
            current_list = None
        else:
            result[key] = value.strip("'\"")
            current_list = None
    return result


def inspect_archive(data: bytes) -> dict[str, Any]:
    """Static, in-memory reading of one release archive. Never extracts or executes."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return {"status": "invalid_archive", "reason": "not a zip archive"}
    names = [info.filename for info in archive.infolist() if not info.is_dir()]
    unsafe = [name for name in names if name.startswith("/") or ".." in PurePosixPath(name).parts]
    if unsafe:
        return {"status": "invalid_archive", "reason": "archive contains unsafe paths", "unsafe_paths": unsafe[:5]}
    candidates = sorted({str(PurePosixPath(name).parent) for name in names if PurePosixPath(name).name == "main.py"}, key=len)
    if not candidates:
        return {"status": "invalid_archive", "reason": "no main.py", "member_count": len(names)}
    root = candidates[0]
    prefix = "" if root == "." else root + "/"

    def read(member: str) -> str | None:
        try:
            info = archive.getinfo(prefix + member)
        except KeyError:
            return None
        if info.file_size > MAX_MEMBER_BYTES:
            return None
        return archive.read(info).decode("utf-8", errors="replace")

    metadata_text = read("metadata.yaml") or read("metadata.yml")
    metadata = _simple_yaml(metadata_text) if metadata_text else {}
    requirements_text = read("requirements.txt") or ""
    requirements = [line.strip() for line in requirements_text.splitlines() if line.strip() and not line.strip().startswith("#")]
    markers = {name: 0 for name in MARKERS}
    python_files = 0
    for name in names:
        if not name.startswith(prefix) or not name.endswith(".py"):
            continue
        info = archive.getinfo(name)
        if info.file_size > MAX_MEMBER_BYTES:
            continue
        python_files += 1
        source = archive.read(info).decode("utf-8", errors="replace")
        for marker, pattern in MARKERS.items():
            markers[marker] += len(pattern.findall(source))
    binary_payloads = [name for name in names if name.endswith((".so", ".pyd", ".dll", ".exe", ".dylib", ".node"))]
    return {
        "status": "inspected",
        "plugin_root": root,
        "member_count": len(names),
        "python_file_count": python_files,
        "metadata_present": metadata_text is not None,
        "metadata": {key: metadata[key] for key in ("name", "desc", "version", "author", "repo", "astrbot_version", "support_platforms") if key in metadata},
        "requirements": requirements[:200],
        "conf_schema_present": read("_conf_schema.json") is not None,
        "markers": markers,
        "binary_payloads": binary_payloads[:20],
    }


def classify(entry: dict[str, Any], inspection: dict[str, Any]) -> list[str]:
    """Hosting notes with a concrete consequence on the Ligamina bridge."""
    notes = []
    markers = inspection.get("markers", {})
    platforms = entry["source_record"].get("support_platforms") or inspection.get("metadata", {}).get("support_platforms") or []
    if markers.get("aiocqhttp_event") or markers.get("platform_adapter_type"):
        notes.append("platform_specific_code")
    if platforms and "ligamina_bridge" not in platforms:
        notes.append("declares_specific_platforms")
    if not markers.get("command"):
        notes.append("no_static_commands")
    if markers.get("llm_tool") or markers.get("llm_hooks"):
        notes.append("llm_dependent")
    if inspection.get("requirements"):
        notes.append("has_requirements")
    if inspection.get("conf_schema_present"):
        notes.append("needs_config")
    if inspection.get("binary_payloads"):
        notes.append("binary_payloads")
    return notes


def _git() -> dict[str, Any]:
    def run(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False).stdout.strip()
    status = run("status", "--porcelain")
    return {"git_head": run("rev-parse", "HEAD"), "git_dirty": bool(status), "git_status_sha256": hashlib.sha256(status.encode()).hexdigest()}


def freeze(output_root: Path, cache_root: Path, market_file: Path | None, workers: int) -> dict[str, Any]:
    started = _now()
    raw = market_file.read_bytes() if market_file else _fetch(MARKET_URL, limit=128 * 1024 * 1024)
    market = json.loads(raw)
    raw_sha = hashlib.sha256(raw).hexdigest()
    snapshot_id = f"ab-{raw_sha[:20]}"
    target = output_root / snapshot_id
    if target.exists():
        raise FileExistsError(f"refusing to overwrite an existing snapshot: {target}")
    keys = [key for key in market if not key.startswith("$")]
    entries = []
    for ordinal, key in enumerate(keys):
        record = market[key]
        identity = {field: record.get(field) for field in ("author", "name", "version", "repo", "commit_sha", "download_url")}
        entries.append({
            "registry_entry_id": "ab-" + hashlib.sha256(_canonical({"key": key, "identity": identity})).hexdigest()[:24],
            "ecosystem": "astrbot",
            "market_key": key,
            "package": record.get("name"),
            "advertised_version": record.get("version"),
            "source_ordinal": ordinal,
            "registry_identity": identity,
            "source_record": {k: v for k, v in record.items() if k not in VOLATILE_FIELDS},
        })

    def process(entry: dict[str, Any]) -> dict[str, Any]:
        url = entry["registry_identity"].get("download_url")
        if not url:
            return {"registry_entry_id": entry["registry_entry_id"], "status": "no_download_url"}
        last_error = ""
        for attempt in range(1, 4):
            try:
                data = _fetch(url)
                break
            except Exception as error:  # network/HTTP errors are recorded, not raised
                last_error = f"{type(error).__name__}: {str(error)[:200]}"
                time.sleep(2 * attempt)
        else:
            return {"registry_entry_id": entry["registry_entry_id"], "status": "download_failed", "error": last_error, "attempts": 3}
        digest, relative = _cache_store(cache_root, data)
        inspection = inspect_archive(data)
        status = "frozen" if inspection["status"] == "inspected" else inspection["status"]
        return {
            "registry_entry_id": entry["registry_entry_id"],
            "status": status,
            "artifact": {
                "url": url,
                "cache_path": relative,
                "size_bytes": len(data),
                "computed_digests": {
                    "sha256": digest,
                    "sha512": hashlib.sha512(data).hexdigest(),
                },
                "market_commit_sha": entry["registry_identity"].get("commit_sha"),
            },
            "inspection": inspection,
            "hosting_notes": classify(entry, inspection) if status == "frozen" else [],
        }

    by_id = {entry["registry_entry_id"]: entry for entry in entries}
    results: dict[str, dict[str, Any]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for index, result in enumerate(pool.map(process, entries), start=1):
            results[result["registry_entry_id"]] = result
            if index % 200 == 0:
                print(f"{index}/{len(entries)} archives processed", file=sys.stderr, flush=True)

    target.mkdir(parents=True)
    (target / "raw").mkdir()
    (target / "raw" / "market.json").write_bytes(raw)
    from collections import Counter

    status_counts = Counter(result["status"] for result in results.values())
    note_counts = Counter(note for result in results.values() for note in result.get("hosting_notes", []))
    catalog = {
        "schema_version": 1,
        "snapshot_id": snapshot_id,
        "ecosystem": "astrbot",
        "created_at_utc": started,
        "source": {"url": MARKET_URL if not market_file else f"file:{market_file.name}", "raw_path": "raw/market.json", "raw_sha256": raw_sha, "market_meta": market.get("$meta")},
        "entry_identity_policy": "market key plus author/name/version/repo/commit_sha/download_url; volatile counters excluded",
        "counts": {"entries": len(entries)},
        "entries": entries,
    }
    catalog_sha = _write_json(target / "catalog.json", catalog)
    lock = {
        "schema_version": 1,
        "snapshot_id": snapshot_id,
        "source_catalog_sha256": catalog_sha,
        "resolution_policy": "download the market download_url once; record our own sha256/sha512; AstrBot itself verifies no hash",
        "counts": {"entries": len(entries), "status": dict(sorted(status_counts.items())), "hosting_notes": dict(sorted(note_counts.items()))},
        "entries": [results[entry["registry_entry_id"]] for entry in entries],
    }
    lock_sha = _write_json(target / "artifacts.lock.json", lock)
    provenance = {
        "schema_version": 1,
        "snapshot_id": snapshot_id,
        "created_at_utc": started,
        "completed_at_utc": _now(),
        "catalog_sha256": catalog_sha,
        "artifacts_lock_sha256": lock_sha,
        "execution_scope": "metadata capture, archive download and static in-memory inspection only; no extraction to disk, no dependency installation, no plugin execution",
        "cache_root": "external content-addressed artifact cache (not in the repository)",
        "git": _git(),
    }
    _write_json(target / "provenance.json", provenance)
    return {"snapshot_id": snapshot_id, "path": str(target), "counts": lock["counts"]}


def verify(snapshot: Path, cache_root: Path) -> dict[str, Any]:
    catalog_bytes = (snapshot / "catalog.json").read_bytes()
    lock = json.loads((snapshot / "artifacts.lock.json").read_text(encoding="utf-8"))
    catalog = json.loads(catalog_bytes)
    problems = []
    if hashlib.sha256(catalog_bytes).hexdigest() != lock["source_catalog_sha256"]:
        problems.append("catalog sha256 differs from the artifacts lock")
    if hashlib.sha256((snapshot / "raw" / "market.json").read_bytes()).hexdigest() != catalog["source"]["raw_sha256"]:
        problems.append("raw market bytes differ from the catalog")
    if [e["registry_entry_id"] for e in catalog["entries"]] != [e["registry_entry_id"] for e in lock["entries"]]:
        problems.append("catalog and lock entries differ")
    checked = 0
    for entry in lock["entries"]:
        artifact = entry.get("artifact")
        if not artifact:
            continue
        path = cache_root / artifact["cache_path"]
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != artifact["computed_digests"]["sha256"]:
            problems.append(f"cache mismatch: {entry['registry_entry_id']}")
        checked += 1
    return {"status": "valid" if not problems else "invalid", "archives_checked": checked, "problems": problems[:20]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    f = sub.add_parser("freeze")
    f.add_argument("--output-root", type=Path, default=ROOT / "catalogs")
    f.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE)
    f.add_argument("--market-file", type=Path)
    f.add_argument("--workers", type=int, default=6)
    v = sub.add_parser("verify")
    v.add_argument("snapshot", type=Path)
    v.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE)
    options = parser.parse_args(argv)
    if options.command == "freeze":
        result = freeze(options.output_root, options.cache_root, options.market_file, options.workers)
    else:
        result = verify(options.snapshot, options.cache_root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status", "valid") == "valid" else 1


if __name__ == "__main__":
    raise SystemExit(main())
