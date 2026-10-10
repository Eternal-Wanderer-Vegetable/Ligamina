import json
from pathlib import Path

from ligamina_nonebot.contracts import (
    classify_operation_reuse,
    classify_operation_support,
    event_error_code,
    validate_event,
    validate_capability_descriptor,
    validate_operation,
)

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "contracts" / "v0.1" / "fixtures"
VECTORS = json.loads((FIXTURES / "vectors.json").read_text(encoding="utf-8"))


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_shared_event_schema_vectors():
    for vector in VECTORS["event_schema"]:
        value = fixture(vector["file"])
        assert (validate_event(value) == []) is vector["valid"], vector["name"]
        assert event_error_code(value) == vector["error_code"], vector["name"]


def test_shared_operation_schema_and_collision_vectors():
    for vector in VECTORS["operation_schema"]:
        if "file" in vector:
            value = fixture(vector["file"])
            assert (validate_operation(value) == []) is vector["valid"], vector["name"]
            if vector.get("outcome") == "unsupported_api":
                assert classify_operation_support(value["operation"]) == "unsupported_api"
        else:
            existing = fixture(vector["existing"])
            incoming = fixture(vector["incoming"])
            assert validate_operation(existing) == []
            assert validate_operation(incoming) == []
            assert classify_operation_reuse(existing, incoming) == vector["outcome"]

    assert classify_operation_reuse({"operation_id": "a"}, {"operation_id": "b"}) == "new"


def test_shared_capability_descriptor_vectors():
    for vector in VECTORS["capability_descriptor"]:
        value = fixture(vector["file"])
        assert (validate_capability_descriptor(value) == []) is vector["valid"], vector["name"]
