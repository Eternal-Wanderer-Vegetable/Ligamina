"""Resolve exact artifact-bound Koishi admin contexts for P7 synthetic pilots."""

from __future__ import annotations

import copy
import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from lab.automation.p7_context_profile_audit import validate_source_audit_finding


ROOT = Path(__file__).resolve().parents[2]
PROFILE_DOCUMENT_PATH = ROOT / "lab/automation/data/p7-koishi-source-bound-context-v1.json"
PROFILE_SCHEMA_PATH = ROOT / "contracts/lab/automation/p7-koishi-context-profiles/v1.schema.json"


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def validate_profile_document(document: dict[str, Any] | None = None) -> dict[str, Any]:
    if document is None:
        return copy.deepcopy(_cached_profile_document())
    return _validate_profile_document(document)


@lru_cache(maxsize=1)
def _cached_profile_document() -> dict[str, Any]:
    return _validate_profile_document(None)


def _validate_profile_document(document: dict[str, Any] | None) -> dict[str, Any]:
    value = copy.deepcopy(document) if document is not None else _read_json(PROFILE_DOCUMENT_PATH)
    schema = _read_json(PROFILE_SCHEMA_PATH)
    Draft202012Validator.check_schema(schema)
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda error: list(error.path))
    if errors:
        first = errors[0]
        location = "/".join(str(part) for part in first.path) or "<root>"
        raise ValueError(f"invalid P7 Koishi context profile at {location}: {first.message}")

    report_ref = value["source_report"]
    report_path = (ROOT / report_ref["path"]).resolve(strict=True)
    report_raw = report_path.read_bytes()
    if _sha256(report_raw) != report_ref["sha256"]:
        raise ValueError("P7 Koishi context source report SHA-256 changed")
    report = json.loads(report_raw)
    if report.get("kind") != "p7_p704_source_bound_context_profile_audit":
        raise ValueError("P7 Koishi context source report has an unexpected kind")
    source_selection = report.get("source_selection", {})
    selection_path = (ROOT / source_selection.get("path", "")).resolve(strict=True)
    selection_raw = selection_path.read_bytes()
    if _sha256(selection_raw) != source_selection.get("sha256"):
        raise ValueError("P7 Koishi context source report selection SHA-256 changed")
    selection = json.loads(selection_raw)
    findings = {
        row.get("registry_entry_id"): row
        for row in report.get("findings", [])
        if isinstance(row, dict)
    }
    for finding in findings.values():
        validate_source_audit_finding(finding, selection)

    seen: set[str] = set()
    for profile in value["profiles"]:
        entry_id = profile["registry_entry_id"]
        if entry_id in seen:
            raise ValueError(f"duplicate P7 Koishi context profile registry entry: {entry_id}")
        seen.add(entry_id)
        finding = findings.get(entry_id)
        if not isinstance(finding, dict):
            raise ValueError(f"P7 Koishi context profile has no source audit finding: {entry_id}")
        identity = (profile["package"], profile["version"], profile["artifact_sha256"])
        finding_identity = (finding.get("package"), finding.get("version"), finding.get("artifact_sha256"))
        if identity != finding_identity or finding.get("ecosystem") != "koishi":
            raise ValueError(f"P7 Koishi context profile identity differs from its source audit: {entry_id}")
        if profile["command_ids"] != finding.get("command_ids"):
            raise ValueError(f"P7 Koishi context profile command set differs from its source audit: {entry_id}")
        if profile["plugin_config"] != {"adminId": [value["synthetic_actor_id"]]}:
            raise ValueError(f"P7 Koishi admin config must contain only the declared synthetic actor: {entry_id}")
        source_file_hashes = {
            row.get("path"): row.get("sha256")
            for row in finding.get("source_files", [])
            if isinstance(row, dict)
        }
        for source in profile["source_evidence"]:
            if source_file_hashes.get(source["path"]) != source["sha256"]:
                raise ValueError(f"P7 Koishi profile source hash is not bound to its locked artifact: {source['path']}")
    return value


def profile_manifest_sha256() -> str:
    validate_profile_document()
    return _sha256(PROFILE_DOCUMENT_PATH.read_bytes())


def profile_entry_ids() -> set[str]:
    return {row["registry_entry_id"] for row in validate_profile_document()["profiles"]}


def profile_binding(candidate: dict[str, Any]) -> dict[str, Any] | None:
    if candidate.get("ecosystem") != "koishi":
        return None
    document = validate_profile_document()
    entry_id = candidate.get("registry_entry_id")
    profile = next((row for row in document["profiles"] if row["registry_entry_id"] == entry_id), None)
    if profile is None:
        return None
    expected = {
        "package": profile["package"],
        "version": profile["version"],
        "artifact_sha256": profile["artifact_sha256"],
    }
    if any(candidate.get(key) != value for key, value in expected.items()):
        raise ValueError(f"P7 Koishi context profile artifact hash or identity mismatch: {entry_id}")

    command_contexts = {
        command_id: {
            "conversation_kind": "group",
            "mention_bot": False,
            "authorization": "plugin_admin",
            "actor_id": document["synthetic_actor_id"],
        }
        for command_id in profile["command_ids"]
    }
    payload = {
        "profile_id": document["profile_id"],
        "execution_scope": document["execution_scope"],
        "authority_scope": document["authority_scope"],
        "manifest_sha256": _sha256(PROFILE_DOCUMENT_PATH.read_bytes()),
        "registry_entry_id": entry_id,
        "package": profile["package"],
        "version": profile["version"],
        "artifact_sha256": profile["artifact_sha256"],
        "synthetic_actor_id": document["synthetic_actor_id"],
        "plugin_config": copy.deepcopy(profile["plugin_config"]),
        "command_contexts": command_contexts,
        "source_evidence": copy.deepcopy(profile["source_evidence"]),
    }
    return {**payload, "profile_sha256": _sha256(_canonical(payload))}


def profile_for_command(candidate: dict[str, Any], command_id: str) -> dict[str, Any] | None:
    binding = profile_binding(candidate)
    if binding is None:
        return None
    context = binding["command_contexts"].get(command_id)
    return copy.deepcopy(context) if context is not None else None
