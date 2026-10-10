"""Validation helpers backed by the language-neutral v0.1 JSON Schemas."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CONTRACTS = _REPO_ROOT / "contracts" / "v0.1"
_EVENT_VALIDATOR = Draft202012Validator(
    json.loads((_CONTRACTS / "event-envelope.schema.json").read_text(encoding="utf-8"))
)
_OPERATION_VALIDATOR = Draft202012Validator(
    json.loads((_CONTRACTS / "platform-operation.schema.json").read_text(encoding="utf-8"))
)
_CAPABILITY_VALIDATOR = Draft202012Validator(
    json.loads((_CONTRACTS / "capability-descriptor.schema.json").read_text(encoding="utf-8"))
)
SUPPORTED_OPERATIONS = frozenset({"send_message", "get_message", "get_self_info"})


def validate_event(value: Any) -> list[str]:
    return sorted(error.message for error in _EVENT_VALIDATOR.iter_errors(value))


def event_error_code(value: Any) -> str | None:
    if isinstance(value, dict) and isinstance(value.get("schema_version"), str):
        if value["schema_version"] != "ligamina/0.1":
            return "protocol_mismatch"
    return "invalid_request" if validate_event(value) else None


def validate_operation(value: Any) -> list[str]:
    return sorted(error.message for error in _OPERATION_VALIDATOR.iter_errors(value))


def validate_capability_descriptor(value: Any) -> list[str]:
    return sorted(error.message for error in _CAPABILITY_VALIDATOR.iter_errors(value))


def classify_operation_reuse(existing: dict[str, Any], incoming: dict[str, Any]) -> str:
    if existing.get("operation_id") != incoming.get("operation_id"):
        return "new"
    payload_fields = ("account_id", "operation", "target", "params")
    if all(existing.get(field) == incoming.get(field) for field in payload_fields):
        return "idempotent"
    return "operation_conflict"


def classify_operation_support(operation: str) -> str:
    return "supported" if operation in SUPPORTED_OPERATIONS else "unsupported_api"
