"""Strict fixture-based checks before native/federated equivalence comparison."""

from __future__ import annotations

import copy
import json
from dataclasses import asdict, dataclass
from typing import Any

try:
    from lab.recipes import missing_fixture_prerequisite, validate_behavior_fixture
except ModuleNotFoundError as error:  # supports the documented `python lab/run.py` entry point
    if error.name not in {"lab", "lab.recipes"}:
        raise
    from recipes import missing_fixture_prerequisite, validate_behavior_fixture


@dataclass(frozen=True)
class Issue:
    code: str
    message: str
    side: str | None = None


@dataclass(frozen=True)
class PairComparison:
    passed: bool
    issues: tuple[Issue, ...]
    normalized_native: dict[str, Any] | None
    normalized_federated: dict[str, Any] | None

    def as_json(self) -> dict[str, Any]:
        return {
            "status": "passed" if self.passed else "failed",
            "issues": [asdict(issue) for issue in self.issues],
            "normalized_native": self.normalized_native,
            "normalized_federated": self.normalized_federated,
        }


@dataclass(frozen=True)
class RecipeComparison:
    status: str
    passed: bool
    reason_code: str | None
    issues: tuple[Issue, ...]
    normalized_native: dict[str, Any] | None
    normalized_federated: dict[str, Any] | None

    def as_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "passed": self.passed,
            "reason_code": self.reason_code,
            "issues": [asdict(issue) for issue in self.issues],
            "normalized_native": self.normalized_native,
            "normalized_federated": self.normalized_federated,
        }


def compare_pair(
    case: dict[str, Any],
    native: dict[str, Any],
    federated: dict[str, Any],
) -> PairComparison:
    """Check each path against the fixture, then compare semantic output."""
    issues: list[Issue] = []
    expected = case["expected"]
    normalized: dict[str, dict[str, Any]] = {}

    for side, result in (("native", native), ("federated", federated)):
        if result.get("status") != "completed":
            issues.append(Issue("run_not_completed", "runner did not complete", side))
            continue
        issues.extend(_validate_environment(expected["environment"], result.get("environment"), side))
        semantic, side_issues = _validate_behavior(expected, result, side)
        issues.extend(side_issues)
        if semantic is not None:
            normalized[side] = semantic

    if "native" in normalized and "federated" in normalized:
        if normalized["native"] != normalized["federated"]:
            issues.append(Issue("behavior_mismatch", "native and federated outputs differ"))

    return PairComparison(
        passed=not issues,
        issues=tuple(issues),
        normalized_native=normalized.get("native"),
        normalized_federated=normalized.get("federated"),
    )


def compare_recipe_pair(
    recipe: dict[str, Any],
    fixture: dict[str, Any] | None,
    native: dict[str, Any],
    federated: dict[str, Any],
    *,
    expected_binding: dict[str, Any] | None,
) -> RecipeComparison:
    """Check both paths against a recipe oracle and the same frozen binding."""
    if fixture is None:
        return RecipeComparison("skipped", False, "missing_fixture", (), None, None)
    fixture_errors = validate_behavior_fixture(recipe, fixture)
    if fixture_errors:
        issue = Issue("invalid_fixture", "; ".join(fixture_errors))
        return RecipeComparison("failed", False, "invalid_fixture", (issue,), None, None)
    missing_prerequisite = missing_fixture_prerequisite(fixture)
    if missing_prerequisite:
        reason_code, _ = missing_prerequisite
        return RecipeComparison("skipped", False, reason_code, (), None, None)
    if expected_binding is None:
        issue = Issue("expected_binding_missing", "comparison requires the binding produced by prepare_recipe")
        return RecipeComparison("failed", False, "expected_binding_missing", (issue,), None, None)

    issues: list[Issue] = []
    normalized: dict[str, dict[str, Any]] = {}
    expected = fixture["expected"]
    for side, result in (("native", native), ("federated", federated)):
        if not isinstance(result, dict) or result.get("status") != "completed":
            issues.append(Issue("run_not_completed", "recipe runner did not complete", side))
            continue
        if result.get("binding") != expected_binding:
            issues.append(Issue("binding_mismatch", "entry/profile/config/services binding differs from frozen inputs", side))
        observed = result.get("observed")
        if not isinstance(observed, dict):
            issues.append(Issue("observations_missing", "runner omitted recipe observations", side))
            continue
        side_values: dict[str, Any] = {}
        for observable in recipe["observables"]:
            if observable not in observed:
                issues.append(Issue("observation_missing", f"runner omitted {observable}", side))
                continue
            side_values[observable] = observed[observable]
            if _canonical(observed[observable]) != _canonical(expected[observable]):
                issues.append(Issue("oracle_mismatch", f"{observable} differs from the independent fixture oracle", side))
        if all(observable in side_values for observable in recipe["observables"]):
            normalized[side] = side_values

    if "native" in normalized and "federated" in normalized and normalized["native"] != normalized["federated"]:
        issues.append(Issue("behavior_mismatch", "native and federated recipe observations differ"))
    return RecipeComparison(
        "passed" if not issues else "failed",
        not issues,
        None if not issues else "recipe_comparison_failed",
        tuple(issues),
        normalized.get("native"),
        normalized.get("federated"),
    )


def _validate_environment(
    expected: dict[str, Any], actual: Any, side: str
) -> list[Issue]:
    if not isinstance(actual, dict):
        return [Issue("environment_missing", "runner omitted environment fingerprint", side)]
    issues = []
    required = ("ecosystem", "framework_version", "runtime_version", "lockfile_sha256", "profile_sha256")
    keys = tuple(dict.fromkeys((*required, *expected.keys())))
    for key in keys:
        actual_value = actual.get(key)
        expected_value = expected.get(key)
        same = (
            isinstance(actual_value, str)
            and isinstance(expected_value, str)
            and actual_value.casefold() == expected_value.casefold()
            if key.endswith("_sha256")
            else actual_value == expected_value
        )
        if not same:
            issues.append(Issue("environment_mismatch", f"environment field {key} differs from fixture", side))
    return issues


def _validate_behavior(
    expected: dict[str, Any], result: dict[str, Any], side: str
) -> tuple[dict[str, Any] | None, list[Issue]]:
    issues: list[Issue] = []
    actual_events = result.get("events")
    expected_events = expected.get("events")
    if not isinstance(actual_events, list) or _normalize_events(actual_events) != _normalize_events(expected_events):
        issues.append(Issue("event_expectation_failed", "observed event set differs from the fixture", side))

    actual_operations = result.get("operations")
    expected_operations = expected.get("operations")
    if not isinstance(actual_operations, list) or _canonical(actual_operations) != _canonical(expected_operations):
        issues.append(Issue("operation_expectation_failed", "operation count, target, or payload differs from the fixture", side))

    actual_receipts = result.get("operation_receipts")
    expected_receipts = expected.get("operation_receipts")
    if not isinstance(actual_receipts, list) or _canonical(actual_receipts) != _canonical(expected_receipts):
        issues.append(Issue("receipt_expectation_failed", "platform result receipts differ from the fixture", side))

    cleanup = result.get("cleanup")
    expected_cleanup = expected.get("cleanup", {"active_capability_count": 0, "live_processes": []})
    if not isinstance(cleanup, dict) or _canonical(cleanup) != _canonical(expected_cleanup):
        issues.append(Issue("cleanup_expectation_failed", "Host or capability state remains after the case", side))

    if issues:
        return None, issues
    return {
        "events": _normalize_events(actual_events),
        "operations": _canonicalize(actual_operations),
        "operation_receipts": _canonicalize(actual_receipts),
        "cleanup": _canonicalize(cleanup),
    }, []


def _normalize_events(events: Any) -> Any:
    if not isinstance(events, list):
        return events
    normalized = []
    for event in events:
        if not isinstance(event, dict):
            normalized.append(event)
            continue
        semantic = copy.deepcopy(event)
        semantic.pop("trace_id", None)
        normalized.append(_canonicalize(semantic))
    return normalized


def _canonical(value: Any) -> str:
    return json.dumps(_canonicalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canonicalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _canonicalize(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonicalize(item) for item in value]
    return value
