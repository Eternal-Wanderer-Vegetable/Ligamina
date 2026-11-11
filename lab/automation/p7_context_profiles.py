"""Resolve narrowly scoped, source-bound synthetic contexts for P7 NoneBot pilots."""

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
PROFILE_DOCUMENT_PATH = ROOT / "lab/automation/data/p7-nonebot-source-bound-context-v1.json"
PROFILE_SCHEMA_PATH = ROOT / "contracts/lab/automation/p7-nonebot-context-profiles/v1.schema.json"


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
    """Validate the profile schema, source report hash, and exact command/artifact binds."""
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
        raise ValueError(f"invalid P7 NoneBot context profile at {location}: {first.message}")

    profile_ids: set[str] = set()
    report_ref = value["source_report"]
    report_path = (ROOT / report_ref["path"]).resolve(strict=True)
    report_raw = report_path.read_bytes()
    if _sha256(report_raw) != report_ref["sha256"]:
        raise ValueError("P7 context profile source report SHA-256 changed")
    report = json.loads(report_raw)
    findings = report.get("source_bound_findings", [])
    finding_by_id = {row.get("registry_entry_id"): row for row in findings if isinstance(row, dict)}
    audit_ref = value["supplemental_source_report"]
    audit_path = (ROOT / audit_ref["path"]).resolve(strict=True)
    audit_raw = audit_path.read_bytes()
    if _sha256(audit_raw) != audit_ref["sha256"]:
        raise ValueError("P7 context profile supplemental source audit SHA-256 changed")
    source_audit = json.loads(audit_raw)
    if source_audit.get("kind") != "p7_p704_source_bound_context_profile_audit":
        raise ValueError("P7 context profile supplemental source audit has an unexpected kind")
    selection_ref = source_audit.get("source_selection", {})
    selection_path = (ROOT / selection_ref.get("path", "")).resolve(strict=True)
    selection_raw = selection_path.read_bytes()
    if _sha256(selection_raw) != selection_ref.get("sha256"):
        raise ValueError("P7 context profile source audit selection SHA-256 changed")
    source_selection = json.loads(selection_raw)
    audit_findings = source_audit.get("findings", [])
    audit_finding_by_id = {
        row.get("registry_entry_id"): row
        for row in audit_findings
        if isinstance(row, dict)
    }
    for audit_finding in audit_finding_by_id.values():
        validate_source_audit_finding(audit_finding, source_selection)

    for profile in value["profiles"]:
        entry_id = profile["registry_entry_id"]
        if entry_id in profile_ids:
            raise ValueError(f"duplicate P7 context profile registry entry: {entry_id}")
        profile_ids.add(entry_id)
        finding = finding_by_id.get(entry_id)
        supplemental_finding = audit_finding_by_id.get(entry_id)
        if finding is None:
            finding = supplemental_finding
        if not isinstance(finding, dict):
            raise ValueError(f"P7 context profile has no source-bound finding: {entry_id}")
        identity = (profile["package"], profile["version"], profile["artifact_sha256"])
        finding_identity = (finding.get("package"), finding.get("version"), finding.get("artifact_sha256"))
        if identity != finding_identity:
            raise ValueError(f"P7 context profile identity differs from its source report: {entry_id}")
        source_commands = (
            {
                row.get("command_id")
                for row in finding.get("commands_with_native_no_operation", [])
                if isinstance(row, dict)
            }
            if entry_id in finding_by_id
            else set(finding.get("command_ids", []))
        )
        if set(profile["command_ids"]) != source_commands:
            raise ValueError(f"P7 context profile command set differs from its source report: {entry_id}")
        overrides = [row["command_id"] for row in profile["command_overrides"]]
        if len(overrides) != len(set(overrides)):
            raise ValueError(f"duplicate P7 context profile command override: {entry_id}")
        if not set(overrides).issubset(source_commands):
            raise ValueError(f"P7 context profile override names an unbound command: {entry_id}")
        source_file_hashes = (
            {
                row.get("package_source"): row.get("package_source_sha256")
                for source in finding.get("wheel", {}).get("source_files", [])
                for row in [source]
                if isinstance(row, dict)
            }
            if entry_id in finding_by_id
            else {
                row.get("path"): row.get("sha256")
                for row in finding.get("source_files", [])
                if isinstance(row, dict)
            }
        )
        for source in profile["source_evidence"]:
            if source_file_hashes.get(source["path"]) != source["sha256"]:
                raise ValueError(f"P7 context profile source hash is not bound to its locked wheel: {source['path']}")
    return value


def profile_manifest_sha256() -> str:
    """Return the exact file hash for the validated profile manifest."""
    validate_profile_document()
    return _sha256(PROFILE_DOCUMENT_PATH.read_bytes())


def context_profile_entry_ids() -> set[str]:
    """Return exact registry identities bound by either ecosystem profile manifest."""
    from lab.automation.p7_koishi_context_profiles import profile_entry_ids as koishi_profile_entry_ids

    return {
        *(row["registry_entry_id"] for row in validate_profile_document()["profiles"]),
        *koishi_profile_entry_ids(),
    }


def context_profile_manifest_sha256() -> str:
    """Hash the validated NoneBot and Koishi profile-manifest set as one campaign input."""
    return _sha256(_canonical(context_profile_manifest_hashes()))


def context_profile_manifest_hashes() -> dict[str, str]:
    """Return per-ecosystem hashes for the validated context-profile manifests."""
    from lab.automation.p7_koishi_context_profiles import profile_manifest_sha256 as koishi_profile_manifest_sha256

    return {
        "nonebot": profile_manifest_sha256(),
        "koishi": koishi_profile_manifest_sha256(),
    }


def profile_binding(candidate: dict[str, Any]) -> dict[str, Any] | None:
    """Build an immutable-by-hash context binding for one exact catalog artifact."""
    if candidate.get("ecosystem") == "koishi":
        from lab.automation.p7_koishi_context_profiles import profile_binding as koishi_profile_binding

        return koishi_profile_binding(candidate)
    if candidate.get("ecosystem") != "nonebot":
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
        raise ValueError(f"P7 context profile artifact hash or identity mismatch: {entry_id}")

    overrides = {row["command_id"]: row["context"] for row in profile["command_overrides"]}
    command_contexts: dict[str, dict[str, Any]] = {}
    for command_id in profile["command_ids"]:
        context = {**profile["default_context"], **overrides.get(command_id, {})}
        context["actor_id"] = (
            document["synthetic_superuser_id"]
            if context["authorization"] == "superuser"
            else document["synthetic_actor_id"]
        )
        context["bot_account_id"] = document["bot_account_id"]
        command_contexts[command_id] = context

    payload = {
        "profile_id": document["profile_id"],
        "execution_scope": document["execution_scope"],
        "authority_scope": document["authority_scope"],
        "event_envelope_role_claims": document["event_envelope_role_claims"],
        "manifest_sha256": _sha256(PROFILE_DOCUMENT_PATH.read_bytes()),
        "registry_entry_id": entry_id,
        "package": profile["package"],
        "version": profile["version"],
        "artifact_sha256": profile["artifact_sha256"],
        "synthetic_actor_id": document["synthetic_actor_id"],
        "synthetic_superuser_id": document["synthetic_superuser_id"],
        "bot_account_id": document["bot_account_id"],
        "command_contexts": command_contexts,
        "source_evidence": copy.deepcopy(profile["source_evidence"]),
    }
    return {**payload, "profile_sha256": _sha256(_canonical(payload))}


def profile_for_command(candidate: dict[str, Any], command_id: str) -> dict[str, Any] | None:
    """Return the resolved synthetic context for a command, if its exact artifact is bound."""
    binding = profile_binding(candidate)
    if binding is None:
        return None
    context = binding["command_contexts"].get(command_id)
    return copy.deepcopy(context) if context is not None else None

