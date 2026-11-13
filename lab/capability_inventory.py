"""Static, non-executing discovery for source-bound plugin inventories.

Archives are read in place and never extracted or imported. The results are
heuristic clues; they are deliberately kept in a separate record from a Host's
runtime inventory.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import tarfile
import warnings
import zipfile
from pathlib import Path
from typing import Callable, Iterable

SCANNER_VERSION = "capability-inventory-static/2"
MAX_MEMBERS = 20_000
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
SOURCE_SUFFIXES = {".py", ".js", ".cjs", ".mjs", ".ts"}
JAVASCRIPT_CALLS = {
    "command": ("commands", "command"),
    "middleware": ("handlers", "middleware"),
    "on": ("handlers", "event_listener"),
    "inject": ("services", "service_injection"),
    "implements": ("services", "service_implementation"),
    "provide": ("services", "service_provision"),
    "cron": ("schedulers", "cron"),
    "setInterval": ("schedulers", "interval"),
    "setTimeout": ("schedulers", "timeout"),
}
PYTHON_CALLS = {
    "on_command": ("commands", "command"),
    "on_shell_command": ("commands", "shell_command"),
    "on_alconna": ("commands", "alconna_command"),
    "command": ("commands", "command"),
    "command_group": ("commands", "command_group"),
    "on_keyword": ("handlers", "keyword"),
    "on_regex": ("handlers", "regex"),
    "on_startswith": ("handlers", "startswith"),
    "on_endswith": ("handlers", "endswith"),
    "on_fullmatch": ("handlers", "fullmatch"),
    "on_message": ("handlers", "message"),
    "on_metaevent": ("handlers", "metaevent"),
    "on_notice": ("handlers", "notice"),
    "on_request": ("handlers", "request"),
    "on_type": ("handlers", "event_type"),
    "llm_tool": ("handlers", "llm_tool"),
    "register_llm_tool": ("handlers", "llm_tool"),
    "event_message_type": ("handlers", "event_message_type"),
    "require": ("services", "plugin_dependency"),
    "inject": ("services", "service_injection"),
    "implements": ("services", "service_implementation"),
    "scheduler": ("schedulers", "scheduler"),
    "scheduled_job": ("schedulers", "scheduled_job"),
    "cron": ("schedulers", "cron"),
    "setInterval": ("schedulers", "interval"),
    "setTimeout": ("schedulers", "timeout"),
}


def scan_archive(
    archive_path: str | Path,
    *,
    ecosystem: str,
    package: str,
    version: str,
    registry_entry_id: str,
    artifact_sha256: str,
) -> dict:
    """Scan a source directory, zip/wheel, or tar archive without executing it."""
    path = Path(archive_path)
    digest = _sha256(path)
    if digest != artifact_sha256:
        raise ValueError(f"artifact SHA-256 mismatch for {path}")
    collections: dict[str, dict] = {
        key: {"status": "observed", "items": []}
        for key in ("commands", "handlers", "services", "schedulers", "configuration")
    }
    limitations: list[str] = []
    count = 0
    total = 0
    for member_name, open_member, size in _members(path):
        count += 1
        if count > MAX_MEMBERS:
            limitations.append(f"Member scan stopped at {MAX_MEMBERS} entries.")
            break
        if size > MAX_FILE_BYTES or total + size > MAX_TOTAL_BYTES:
            limitations.append(f"Source budget skipped {member_name}.")
            continue
        suffix = Path(member_name).suffix.lower()
        if suffix not in SOURCE_SUFFIXES and Path(member_name).name != "_conf_schema.json":
            continue
        raw = open_member()
        total += len(raw)
        if len(raw) > MAX_FILE_BYTES:
            limitations.append(f"Source budget skipped {member_name}.")
            continue
        if Path(member_name).name == "_conf_schema.json":
            _add(collections, "configuration", "config_schema", member_name, member_name, 1)
            continue
        text = raw.decode("utf-8-sig", errors="replace")
        if suffix == ".py":
            with warnings.catch_warnings(record=True) as parse_warnings:
                warnings.simplefilter("always")
                try:
                    _scan_python(text, member_name, collections)
                except SyntaxError:
                    limitations.append(f"Python syntax could not be parsed in {member_name}.")
                for warning in parse_warnings:
                    if issubclass(warning.category, SyntaxWarning):
                        limitations.append(
                            f"Python syntax warning in {member_name}:{warning.lineno}: {warning.message}"
                        )
        elif suffix in {".js", ".cjs", ".mjs", ".ts"}:
            _scan_javascript(text, member_name, collections)
        if len(limitations) >= 32:
            limitations = limitations[:31] + ["Additional static scan limitations omitted."]
            break
    for collection in collections.values():
        _dedupe(collection["items"])
    return {
        "schema_version": 1,
        "ecosystem": ecosystem,
        "plugin": {
            "registry_entry_id": registry_entry_id,
            "package": package,
            "version": version,
            "artifact_sha256": artifact_sha256,
        },
        "source": {
            "kind": "static",
            "collector": SCANNER_VERSION,
            "confidence": "heuristic",
            "evidence": [{"reference": f"sha256:{artifact_sha256}"}],
        },
        "capabilities": collections,
        "limitations": limitations,
    }


def _members(path: Path) -> Iterable[tuple[str, Callable[[], bytes], int]]:
    if path.is_dir():
        for member in sorted(path.rglob("*")):
            if member.is_file() and not member.is_symlink():
                rel = member.relative_to(path).as_posix()
                if any(part in {"node_modules", "__pycache__", ".git", "tests", "test"} for part in Path(rel).parts):
                    continue
                yield rel, member.read_bytes, member.stat().st_size
        return
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                rel = info.filename
                if any(part in {"node_modules", "__pycache__", ".git", "tests", "test"} for part in Path(rel).parts):
                    continue
                yield rel, lambda name=rel: archive.read(name), info.file_size
        return
    with tarfile.open(path, mode="r:*") as archive:
        for info in archive.getmembers():
            if not info.isfile():
                continue
            rel = info.name
            if any(part in {"node_modules", "__pycache__", ".git", "tests", "test"} for part in Path(rel).parts):
                continue
            yield rel, lambda member=info: _read_tar_member(archive, member), info.size


def _read_tar_member(archive: tarfile.TarFile, member: tarfile.TarInfo) -> bytes:
    stream = archive.extractfile(member)
    return stream.read(MAX_FILE_BYTES + 1) if stream is not None else b""


def _scan_python(text: str, path: str, collections: dict[str, dict]) -> None:
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and any(_call_name(base).endswith("BaseModel") for base in node.bases):
            _add(collections, "configuration", "config_model", node.name, path, node.lineno)
        if not isinstance(node, ast.Call):
            continue
        call_name = _call_name(node.func).split(".")[-1]
        category_kind = PYTHON_CALLS.get(call_name)
        if category_kind is None:
            continue
        category, kind = category_kind
        name = _literal_name(node)
        attributes = {
            "call": call_name,
            "name_confidence": "literal" if name else "unknown",
        }
        _add(collections, category, kind, name or call_name, path, node.lineno, attributes)


def _scan_javascript(text: str, path: str, collections: dict[str, dict]) -> None:
    pattern = re.compile(
        r"(?:(?:\b(?:ctx|context|app)\s*\.\s*)(?P<context_call>command|middleware|on|inject|implements|provide|cron)"
        r"|(?P<global_call>setInterval|setTimeout))\s*\(\s*"
        r"(?:'(?P<single>((?:\\.|[^'\\])*))'|\"(?P<double>((?:\\.|[^\"\\])*))\")?",
    )
    for line_number, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith(("//", "*", "/*")):
            continue
        for match in pattern.finditer(line):
            call_name = match.group("context_call") or match.group("global_call")
            category, kind = JAVASCRIPT_CALLS[call_name]
            literal = match.group("single") if match.group("single") is not None else match.group("double")
            name = _unescape_javascript(literal) if literal is not None else call_name
            if call_name == "on" and not name.startswith(("message", "notice")):
                continue
            _add(
                collections,
                category,
                kind,
                name,
                path,
                line_number,
                {
                    "call": call_name,
                    "name_confidence": "literal" if literal is not None else "unknown",
                },
            )


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_call_name(node.value)}.{node.attr}"
    return ""


def _literal_name(node: ast.Call) -> str | None:
    values = list(node.args)
    if not values:
        values = [keyword.value for keyword in node.keywords if keyword.arg in {"cmd", "command", "name", "msg", "pattern", "regex"}]
    if not values:
        return None
    literals = _string_literals(values[0])
    return ",".join(literals[:8])[:256] or None


def _string_literals(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return [value for item in node.elts for value in _string_literals(item)]
    if isinstance(node, ast.Call):
        return [value for item in node.args for value in _string_literals(item)]
    return []


def _unescape_javascript(value: str) -> str:
    return re.sub(r"\\([\\'\"nrt])", lambda match: {"n": "\n", "r": "\r", "t": "\t"}.get(match.group(1), match.group(1)), value)[:256]


def _add(
    collections: dict[str, dict], category: str, kind: str, name: str,
    path: str, line: int, attributes: dict | None = None,
) -> None:
    if not name:
        return
    item = {
        "kind": kind,
        "name": str(name)[:256],
        "evidence": [{"reference": f"{path}:{line}", "path": path, "line": line}],
    }
    if attributes:
        item["attributes"] = attributes
    collections[category]["items"].append(item)


def _dedupe(items: list[dict]) -> None:
    seen: set[tuple[str, str, str]] = set()
    kept = []
    for item in items:
        evidence = item["evidence"][0]
        key = (item["kind"], item["name"], evidence["reference"])
        if key not in seen:
            seen.add(key)
            kept.append(item)
    items[:] = kept


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
