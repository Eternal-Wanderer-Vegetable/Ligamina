"""Validation for frozen, entry-bound behavior fixture suites."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SUITE_PATH = ROOT / "contracts/lab/automation/v1/fixtures/behavior-suite.phase3-v1.json"
SCHEMA_PATH = ROOT / "contracts/lab/automation/v1/behavior-suite.schema.json"
CATALOG_ROOT = ROOT / "catalogs/p2-41649744e057d61031d5"
LEGACY_MANIFEST = ROOT / "outputs/acceptance/phase2-s209-pilot-manifest-20261012.json"
PLATFORMS = ("linux-x86_64", "windows-x86_64")


class BehaviorSuiteError(ValueError):
    """The frozen behavior suite is incomplete or mismatched to its source locks."""


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BehaviorSuiteError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BehaviorSuiteError(f"expected JSON object: {path}")
    return value


def validate_behavior_suite(path: Path = SUITE_PATH) -> dict[str, Any]:
    """Check schema, catalog/artifact/profile identity, per-entry cases and legacy isolation."""
    from jsonschema import Draft202012Validator, FormatChecker

    suite = _read(path)
    schema = _read(SCHEMA_PATH)
    Draft202012Validator.check_schema(schema)
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(suite),
        key=lambda error: tuple(str(part) for part in error.path),
    )
    if errors:
        raise BehaviorSuiteError("behavior suite schema failed: " + "; ".join(error.message for error in errors))

    artifacts = _read(CATALOG_ROOT / "artifacts.lock.json")
    profiles = _read(CATALOG_ROOT / "profiles.lock.json")
    artifact_by_id = {row["registry_entry_id"]: row for row in artifacts["entries"]}
    profile_by_id = {row["registry_entry_id"]: row for row in profiles["entries"]}
    seen: set[str] = set()
    for entry in suite["entries"]:
        identity = entry["registry_entry_id"]
        if identity in seen:
            raise BehaviorSuiteError(f"duplicate catalog entry: {identity}")
        seen.add(identity)
        locked_artifact = artifact_by_id.get(identity)
        locked_profile = profile_by_id.get(identity)
        if locked_artifact is None or locked_profile is None:
            raise BehaviorSuiteError(f"entry is absent from frozen catalog locks: {identity}")
        if (entry["ecosystem"], entry["package"], entry["version"]) != (
            locked_artifact["ecosystem"], locked_artifact["package"], locked_artifact["advertised_version"]
        ):
            raise BehaviorSuiteError(f"entry identity differs from catalog lock: {identity}")
        frozen = next((item["artifact"] for item in locked_artifact["artifacts"] if item["status"] == "locked"), None)
        locked_sha256 = (frozen or {}).get("sha256") or (frozen or {}).get("computed_digests", {}).get("sha256")
        if frozen is None or entry["artifact_sha256"] != locked_sha256:
            raise BehaviorSuiteError(f"artifact digest differs from catalog lock: {identity}")
        if entry["source_review"]["reviewed_artifact_sha256"] != locked_sha256:
            raise BehaviorSuiteError(f"source review is not bound to frozen artifact: {identity}")
        for platform in PLATFORMS:
            lock = locked_profile["profiles"].get(platform, {})
            bound = entry["dependency_locks"][platform]
            if lock.get("status") != "locked":
                if bound["status"] != "static_skipped" or bound.get("reason_code") != lock.get("reason_code"):
                    raise BehaviorSuiteError(f"platform disposition is not backed by profile lock: {identity}/{platform}")
                continue
            dependency_lock = lock.get("dependency_lock", {}).get("sha256")
            if bound["status"] != "eligible_not_run" or bound["profile_id"] != lock.get("profile_id"):
                raise BehaviorSuiteError(f"platform profile identity mismatch: {identity}/{platform}")
            if bound["dependency_lock_sha256"] != dependency_lock or bound["artifact_sha256"] != locked_sha256:
                raise BehaviorSuiteError(f"platform hashes mismatch: {identity}/{platform}")
        classes = [case["class"] for case in entry["cases"]]
        if set(classes) != {"positive", "negative", "isolation"} or len(classes) != 3:
            raise BehaviorSuiteError(f"entry must have exactly one positive, negative and isolation case: {identity}")
        for case in entry["cases"]:
            for assertion in case["assertions"]:
                if assertion["operator"] == "in_range" and not (
                    isinstance(assertion.get("minimum"), (int, float))
                    and isinstance(assertion.get("maximum"), (int, float))
                    and assertion["minimum"] <= assertion["maximum"]
                ):
                    raise BehaviorSuiteError(f"invalid numeric range in {identity}/{case['case_id']}")

    legacy = _read(LEGACY_MANIFEST)["legacy_support_units"]
    by_key = {row["unit_key"]: row for row in legacy}
    if len(by_key) != 4:
        raise BehaviorSuiteError("historical manifest no longer has exactly four legacy regressions")
    for row in suite["legacy_regressions"]:
        original = by_key.get(row["unit_key"])
        if original is None or any(row[key] != original[key] for key in original):
            raise BehaviorSuiteError(f"legacy regression identity mismatch: {row['unit_key']}")
    return {
        "status": "passed",
        "suite_id": suite["suite_id"],
        "entries": len(suite["entries"]),
        "required_cases": sum(len(entry["cases"]) for entry in suite["entries"]),
        "platform_positions": sum(len(entry["platforms"]) for entry in suite["entries"]),
        "legacy_regressions": len(suite["legacy_regressions"]),
        "executed_platforms": [],
        "windows_tests_run": False,
    }
