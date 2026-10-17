"""Versioned catalog-run result contracts and shared result accounting."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource


_REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_DIR = _REPO_ROOT / "contracts" / "lab" / "v1"
DOCUMENT_KINDS = ("catalog", "run", "result", "summary")
STATUSES = ("passed", "failed", "skipped", "unknown")
_SCHEMAS = {
    kind: json.loads((CONTRACT_DIR / f"{kind}.schema.json").read_text(encoding="utf-8"))
    for kind in DOCUMENT_KINDS
}

_registry = Registry()
for _schema in _SCHEMAS.values():
    Draft202012Validator.check_schema(_schema)
    _registry = _registry.with_resource(_schema["$id"], Resource.from_contents(_schema))

_VALIDATORS = {
    kind: Draft202012Validator(schema, registry=_registry, format_checker=FormatChecker())
    for kind, schema in _SCHEMAS.items()
}


def validate_document(kind: str, document: Any) -> None:
    """Validate a document against one of the version 1 lab schemas."""
    try:
        validator = _VALIDATORS[kind]
    except KeyError as exc:
        raise ValueError(f"unknown lab document kind: {kind}") from exc
    errors = sorted(
        validator.iter_errors(document),
        key=lambda error: tuple(map(str, error.absolute_path)),
    )
    if errors:
        details = "; ".join(
            f"/{'/'.join(map(str, error.absolute_path))}: {error.message}" for error in errors
        )
        raise ValueError(f"invalid {kind} v1 document: {details}")


def normalize_legacy_status(
    status: object,
    *,
    reason_code: str | None = None,
    environment_missing_proven: bool = False,
) -> dict[str, str]:
    """Map a legacy case status to the v1 result status without guessing."""
    if status in STATUSES:
        normalized = str(status)
        if normalized == "passed":
            return {"status": normalized}
        return {"status": normalized, "reason_code": reason_code or f"legacy_{normalized}"}
    if status == "not_run":
        return {"status": "skipped", "reason_code": reason_code or "not_scheduled"}
    if status == "environment_missing":
        if environment_missing_proven:
            return {"status": "skipped", "reason_code": reason_code or "environment_missing"}
        return {"status": "unknown", "reason_code": "environment_missing_unproven"}
    if status == "completed":
        return {"status": "unknown", "reason_code": "completed_is_run_state"}
    if status is None:
        return {"status": "unknown", "reason_code": "legacy_status_missing"}
    return {"status": "unknown", "reason_code": "legacy_status_unrecognized"}


def summarize_results(
    expected_result_ids: Iterable[str], results: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Summarize one terminal row per expected result unit.

    Duplicate result IDs are reported and excluded after their first occurrence,
    so retries or duplicate dispatches cannot inflate pass rates. Attempts belong
    in separate result evidence; this function consumes the final row per unit.
    """
    expected = list(expected_result_ids)
    if any(not isinstance(result_id, str) or not result_id for result_id in expected):
        raise ValueError("expected result IDs must be non-empty strings")
    if len(set(expected)) != len(expected):
        raise ValueError("expected result IDs must be unique")

    expected_set = set(expected)
    seen: set[str] = set()
    duplicate_ids: list[str] = []
    unexpected_ids: list[str] = []
    counts = dict.fromkeys(STATUSES, 0)

    for index, result in enumerate(results):
        result_id = result.get("result_id")
        if not isinstance(result_id, str) or not result_id:
            raise ValueError(f"results[{index}].result_id must be a non-empty string")
        status = result.get("status")
        if status not in STATUSES:
            raise ValueError(f"results[{index}] has unknown v1 status: {status!r}")
        if result_id in seen:
            duplicate_ids.append(result_id)
            continue
        seen.add(result_id)
        if result_id not in expected_set:
            unexpected_ids.append(result_id)
            continue
        counts[status] += 1

    accounted_ids = expected_set & seen
    missing_ids = sorted(expected_set - seen)
    accounted = len(accounted_ids)
    expected_count = len(expected)
    executed = counts["passed"] + counts["failed"] + counts["unknown"]

    def rate(numerator: int, denominator: int) -> float | None:
        return numerator / denominator if denominator else None

    summary = {
        "schema_version": 1,
        "expected_result_count": expected_count,
        "accounted_result_count": accounted,
        "missing_count": len(missing_ids),
        "missing_result_ids": missing_ids,
        "unexpected_count": len(unexpected_ids),
        "unexpected_result_ids": sorted(unexpected_ids),
        "duplicate_count": len(duplicate_ids),
        "duplicate_result_ids": duplicate_ids,
        "status_counts": counts,
        "executed_denominator": executed,
        "entry_coverage": rate(accounted, expected_count),
        "stage_execution_coverage": rate(executed, expected_count),
        "executed_pass_rate": rate(counts["passed"], executed),
        "passed_over_expected_rate": rate(counts["passed"], expected_count),
        "gate_passed": (
            expected_count > 0
            and accounted == expected_count
            and not unexpected_ids
            and not duplicate_ids
            and counts["passed"] == expected_count
        ),
    }
    validate_document("summary", summary)
    return summary
