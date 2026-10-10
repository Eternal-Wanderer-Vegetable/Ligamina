"""Explicit pure capability declarations exposed by the NoneBot Host."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from jsonschema import Draft202012Validator

CAPABILITY_ID = "ligamina.nonebot.math.add"
_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "left": {"type": "integer", "minimum": -9007199254740991, "maximum": 9007199254740991},
        "right": {"type": "integer", "minimum": -9007199254740991, "maximum": 9007199254740991},
    },
    "required": ["left", "right"],
    "additionalProperties": False,
}
_OUTPUT_SCHEMA = {"type": "integer", "minimum": -9007199254740991, "maximum": 9007199254740991}
_DESCRIPTORS = [{
    "capability_id": CAPABILITY_ID,
    "input_schema": _INPUT_SCHEMA,
    "output_schema": _OUTPUT_SCHEMA,
    "pure": True,
}]
_VALIDATORS = {CAPABILITY_ID: Draft202012Validator(_INPUT_SCHEMA)}


class CapabilityError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def capability_descriptors() -> list[dict[str, Any]]:
    return deepcopy(_DESCRIPTORS)


def invoke_capability(capability_id: str, arguments: Any) -> int:
    validator = _VALIDATORS.get(capability_id)
    if validator is None:
        raise CapabilityError("unsupported_capability", f"unsupported capability: {capability_id}")
    errors = sorted(error.message for error in validator.iter_errors(arguments))
    if errors:
        raise CapabilityError("invalid_request", "; ".join(errors))
    result = arguments["left"] + arguments["right"]
    if not Draft202012Validator(_OUTPUT_SCHEMA).is_valid(result):
        raise CapabilityError("invalid_result", "capability result does not match its declared schema")
    return result
