"""Build auditable summaries without treating skipped or unknown cases as passes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


CASE_STATUSES = ("passed", "failed", "skipped", "unknown")


def summarize_cases(cases: list[dict[str, Any]], expected_count: int | None = None) -> dict[str, Any]:
    counts = {status: 0 for status in CASE_STATUSES}
    invalid: list[dict[str, Any]] = []
    for index, case in enumerate(cases):
        status = case.get("status")
        if status not in counts:
            invalid.append({"index": index, "status": status})
            counts["unknown"] += 1
        else:
            counts[status] += 1
    expected = len(cases) if expected_count is None else max(0, expected_count)
    missing = max(0, expected - len(cases))
    unexpected = max(0, len(cases) - expected)
    executed = counts["passed"] + counts["failed"] + counts["unknown"]
    coverage_count = min(expected, len(cases))
    return {
        "expected_denominator": expected,
        "observed_count": len(cases),
        "coverage_count": coverage_count,
        "coverage_rate": round(coverage_count / expected, 6) if expected else 1.0,
        "executed_denominator": executed,
        "pass_rate": round(counts["passed"] / executed, 6) if executed else None,
        "passed": counts["passed"],
        "failed": counts["failed"],
        "skipped": counts["skipped"],
        "unknown": counts["unknown"],
        "missing": missing,
        "unexpected": unexpected,
        "invalid_statuses": invalid,
        "gate_passed": (
            counts["failed"] == 0
            and counts["unknown"] == 0
            and counts["skipped"] == 0
            and missing == 0
            and unexpected == 0
            and not invalid
        ),
    }


def summarize_report(report: dict[str, Any]) -> dict[str, Any]:
    if isinstance(report.get("case_summary"), dict):
        case_summary = report["case_summary"]
    elif isinstance(report.get("cases"), list):
        case_summary = summarize_cases(report["cases"], report.get("expected_cases"))
    else:
        case_summary = None
    return {
        "suite": report.get("suite"),
        "status": report.get("status"),
        "gate": report.get("gate"),
        "case_summary": case_summary,
        "resource_budget": report.get("resource_budget"),
        "recovery_budget": report.get("recovery_budget"),
        "cleanup": report.get("cleanup"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="JSON report to summarize")
    options = parser.parse_args(argv)
    try:
        report = json.loads(options.report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        parser.exit(2, f"could not read report: {error}\n")
    if not isinstance(report, dict):
        parser.exit(2, "report root must be a JSON object\n")
    print(json.dumps(summarize_report(report), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
