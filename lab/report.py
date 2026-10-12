"""Build auditable summaries without treating skipped or unknown cases as passes."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from lab.result import summarize_results, validate_document


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


_SECRET_KEY = re.compile(r"(?i)(token|password|secret|authorization|cookie|credential|api[_-]?key|private[_-]?key)")
_INLINE_SECRET = re.compile(
    r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]+|"
    r"\b(password|token|secret|api[_-]?key)\s*([=:])\s*([^\s,;]+)"
)
_URL_CREDENTIALS = re.compile(r"(?i)(https?://)[^/@:\s]+:[^/@\s]+@")
SUPPORTED_FORMATS = ("json", "text", "ndjson")
GATES = ("completion", "automation", "compatibility")


def _redact_string(value: str, *, limit: int = 4096) -> str:
    value = _URL_CREDENTIALS.sub(r"\1[REDACTED]@", value)
    value = _INLINE_SECRET.sub(lambda match: (
        f"{match.group(1)}[REDACTED]" if match.group(1)
        else f"{match.group(2)}{match.group(3)}[REDACTED]"
    ), value)
    if len(value) > limit:
        return value[:limit] + "…[truncated]"
    return value


def redact_secrets(value: Any) -> Any:
    """Return a JSON-safe copy with credential fields and common inline secrets removed."""
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if _SECRET_KEY.search(str(key)) else redact_secrets(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, str):
        return _redact_string(value)
    return value


def _computed_summary(report: dict[str, Any], *, require_expected_ids: bool = True) -> dict[str, Any]:
    expected_ids = report.get("expected_result_ids")
    if expected_ids is None:
        if require_expected_ids:
            raise ValueError("run report must include expected_result_ids so its denominator can be recomputed")
        return report["summary"]
    if not isinstance(expected_ids, list):
        raise ValueError("expected_result_ids must be a list")
    computed = summarize_results(expected_ids, report["results"])
    if computed != report["summary"]:
        raise ValueError("run summary does not match expected_result_ids and results")
    return computed


def _gate_passed(report: dict[str, Any], gate: str) -> bool:
    summary = report["summary"]
    fully_accounted = (
        report["run_state"] == "completed"
        and summary["expected_result_count"] > 0
        and summary["accounted_result_count"] == summary["expected_result_count"]
        and summary["missing_count"] == 0
        and summary["unexpected_count"] == 0
        and summary["duplicate_count"] == 0
    )
    if gate == "completion":
        return fully_accounted
    if gate == "automation":
        return (
            fully_accounted
            and summary["status_counts"]["unknown"] == 0
            and not any(result.get("reason_code") == "not_scheduled" for result in report["results"])
            and not any(
                result.get("status") == "failed"
                and result.get("failure_domain") in {"harness", "infrastructure"}
                for result in report["results"]
            )
        )
    if gate == "compatibility":
        return fully_accounted and bool(summary.get("gate_passed"))
    raise ValueError(f"unknown report gate: {gate}")


def report_exit_code(report: dict[str, Any], gate: str | None = None) -> int:
    selected_gate = gate or report.get("gate") or "compatibility"
    if selected_gate not in GATES:
        raise ValueError(f"unknown report gate: {selected_gate}")
    if report["run_state"] == "interrupted":
        return 3
    return 0 if _gate_passed(report, selected_gate) else 1


def validate_run_report(report: dict[str, Any], *, require_expected_ids: bool = True) -> None:
    """Validate v1 schema, recompute all result accounting, and check recorded gate/exit state."""
    validate_document("run", report)
    _computed_summary(report, require_expected_ids=require_expected_ids)
    gate = report.get("gate")
    if gate:
        passed = _gate_passed(report, gate)
        if report.get("gate_passed") is not passed:
            raise ValueError("recorded gate_passed does not match the selected report gate")
        if report["exit_code"] != report_exit_code(report, gate):
            raise ValueError("recorded exit_code does not match run state and selected gate")


def build_run_report(
    *,
    fields: dict[str, Any],
    expected_result_ids: list[str],
    results: list[dict[str, Any]],
    gate: str = "compatibility",
) -> dict[str, Any]:
    """Build a v1 report, deriving its summary and gate result from final rows."""
    if gate not in GATES:
        raise ValueError(f"unknown report gate: {gate}")
    report = {
        **fields,
        "schema_version": 1,
        "expected_result_ids": expected_result_ids,
        "results": results,
        "summary": summarize_results(expected_result_ids, results),
        "gate": gate,
    }
    report["gate_passed"] = _gate_passed(report, gate)
    report["exit_code"] = report_exit_code(report, gate)
    report = redact_secrets(report)
    validate_run_report(report)
    return report


def report_matrix(results: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Count observed terminal rows by ecosystem, official subset, OS, and stage."""
    dimensions = ("ecosystem", "official_maintained", "platform", "stage")
    matrix: dict[str, list[dict[str, Any]]] = {}
    for dimension in dimensions:
        groups: dict[str, dict[str, Any]] = {}
        for result in results:
            raw = result.get(dimension)
            label = "unknown" if raw is None else str(raw).lower() if isinstance(raw, bool) else str(raw)
            row = groups.setdefault(label, {"observed_count": 0, "status_counts": {status: 0 for status in ("passed", "failed", "skipped", "unknown")}})
            row["observed_count"] += 1
            row["status_counts"][result["status"]] += 1
        matrix[dimension] = [{"value": value, **groups[value]} for value in sorted(groups)]
    return matrix


def report_projection(report: dict[str, Any], *, gate: str | None = None) -> dict[str, Any]:
    selected_gate = gate or report.get("gate") or "compatibility"
    validate_run_report(report)
    projected = {
        "schema_version": 1,
        "suite": report["suite"],
        "run_id": report["run_id"],
        "run_state": report["run_state"],
        "gate": selected_gate,
        "gate_passed": _gate_passed(report, selected_gate),
        "exit_code": report_exit_code(report, selected_gate),
        "catalog": {
            "snapshot_id": report["catalog"]["snapshot_id"],
            "entry_count": report["catalog"]["entry_count"],
            "official_maintained_count": report["catalog"]["official_maintained_count"],
            "ecosystems": report["catalog"]["ecosystems"],
        },
        "summary": report["summary"],
        "matrix": report_matrix(report["results"]),
    }
    return redact_secrets(projected)


def render_v1_report(report: dict[str, Any], output_format: str = "json", *, gate: str | None = None) -> str:
    if output_format not in SUPPORTED_FORMATS:
        raise ValueError(f"unsupported report format: {output_format}")
    projection = report_projection(report, gate=gate)
    if output_format == "json":
        return json.dumps(projection, ensure_ascii=False, indent=2)
    if output_format == "ndjson":
        events = [
            {"schema_version": 1, "event": "run", "run_id": projection["run_id"], "run_state": projection["run_state"], "gate": projection["gate"], "exit_code": projection["exit_code"]},
            {"schema_version": 1, "event": "matrix", "run_id": projection["run_id"], "matrix": projection["matrix"]},
            {"schema_version": 1, "event": "summary", "run_id": projection["run_id"], "summary": projection["summary"]},
        ]
        return "\n".join(json.dumps(event, ensure_ascii=False, separators=(",", ":")) for event in events)

    summary = projection["summary"]
    counts = summary["status_counts"]
    rates = "n/a" if summary["executed_pass_rate"] is None else f"{summary['executed_pass_rate']:.1%}"
    lines = [
        f"Run {projection['run_id']} ({projection['suite']}) — {projection['run_state']}",
        f"Gate {projection['gate']}: {'PASS' if projection['gate_passed'] else 'NOT READY'} (exit {projection['exit_code']})",
        f"Catalog {projection['catalog']['snapshot_id']}: {projection['catalog']['entry_count']} entries; {projection['catalog']['official_maintained_count']} officially maintained",
        f"Results: {summary['accounted_result_count']}/{summary['expected_result_count']} accounted; {summary['missing_count']} missing; {summary['unexpected_count']} unexpected; {summary['duplicate_count']} duplicate",
        f"passed={counts['passed']} failed={counts['failed']} skipped={counts['skipped']} unknown={counts['unknown']} executed_pass_rate={rates}",
    ]
    for dimension, rows in projection["matrix"].items():
        if rows:
            lines.append(f"{dimension}: " + ", ".join(f"{row['value']}={row['observed_count']}" for row in rows))
    return "\n".join(lines)


def render_summary_output(summary: dict[str, Any], output_format: str = "json") -> str:
    """Render a short command summary with the shared privacy and format rules."""
    if output_format not in SUPPORTED_FORMATS:
        raise ValueError(f"unsupported report format: {output_format}")
    safe = redact_secrets(summary)
    if output_format == "json":
        return json.dumps(safe, ensure_ascii=False, separators=(",", ":"))
    if output_format == "ndjson":
        event = safe.pop("event", "summary")
        return json.dumps({"schema_version": 1, "event": event, "summary": safe}, ensure_ascii=False, separators=(",", ":"))
    return "\n".join(f"{key}: {value}" for key, value in safe.items())


def render_catalog_plan(report: dict[str, Any], output_format: str = "json") -> tuple[str, int]:
    """Render a scheduler plan without implying that any plugin work ran."""
    if output_format not in SUPPORTED_FORMATS:
        raise ValueError(f"unsupported report format: {output_format}")
    manifest = report.get("manifest")
    counts = report.get("job_counts")
    stages = manifest.get("stages") if isinstance(manifest, dict) else None
    if (
        report.get("status") not in {"planned", "resumed"}
        or not isinstance(report.get("run_id"), str)
        or not isinstance(report.get("snapshot_id"), str)
        or type(report.get("selected_units")) is not int
        or type(report.get("expected_stage_jobs")) is not int
        or not isinstance(counts, dict)
        or not isinstance(stages, list)
        or not stages
        or not all(isinstance(stage, str) and stage for stage in stages)
        or report.get("execution_started") is not False
        or type(report.get("total_catalog_entries")) is not int
        or report["total_catalog_entries"] < 0
        or type(report.get("expected_units")) is not int
        or report["expected_units"] < report["selected_units"]
        or report["selected_units"] < 0
        or set(counts) != {"queued", "leased", "passed", "failed", "skipped", "unknown", "total"}
        or any(type(counts[status]) is not int or counts[status] < 0 for status in counts)
        or manifest.get("snapshot_entry_count") != report["total_catalog_entries"]
        or manifest.get("selected_unit_count") != report["selected_units"]
        or manifest.get("expected_stage_count") != report["expected_stage_jobs"]
        or report["expected_stage_jobs"] != report["selected_units"] * len(stages)
        or counts.get("total") != report["expected_stage_jobs"]
        or sum(counts.get(status, 0) for status in ("queued", "leased", "passed", "failed", "skipped", "unknown")) != counts.get("total")
    ):
        raise ValueError("invalid catalog plan report")
    projection = redact_secrets({
        "schema_version": 1,
        "event": "catalog_plan",
        "suite": "catalog",
        "status": report["status"],
        "run_id": report["run_id"],
        "run_state": report.get("run_state"),
        "snapshot_id": report["snapshot_id"],
        "total_catalog_entries": report.get("total_catalog_entries"),
        "expected_units": report.get("expected_units"),
        "selected_units": report["selected_units"],
        "expected_stage_jobs": report["expected_stage_jobs"],
        "job_counts": counts,
        "execution_started": False,
    })
    if output_format == "json":
        return json.dumps(projection, ensure_ascii=False, indent=2), 0
    if output_format == "ndjson":
        event = {key: value for key, value in projection.items() if key != "event"}
        return json.dumps({"schema_version": 1, "event": "catalog_plan", "summary": event}, ensure_ascii=False, separators=(",", ":")), 0
    lines = [
        f"Catalog plan {projection['run_id']} ({projection['status']})",
        f"Snapshot {projection['snapshot_id']}: {projection['total_catalog_entries']} entries",
        f"Units {projection['selected_units']} selected / {projection['expected_units']} catalog units; {projection['expected_stage_jobs']} stage jobs",
        "Jobs: " + ", ".join(f"{key}={value}" for key, value in sorted(counts.items()) if key != "total"),
        "Plugin execution started: no",
    ]
    return "\n".join(lines), 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="JSON report to summarize")
    parser.add_argument("--format", choices=SUPPORTED_FORMATS, default="json")
    parser.add_argument("--gate", choices=GATES, help="gate used to derive the report exit code")
    options = parser.parse_args(argv)

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        report = json.loads(options.report.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        print("could not read a valid report JSON document", file=sys.stderr)
        return 2
    if not isinstance(report, dict):
        print("report root must be a JSON object", file=sys.stderr)
        return 2
    if "schema_version" in report:
        try:
            validate_run_report(report)
            rendered = render_v1_report(report, options.format, gate=options.gate)
            code = report_exit_code(report, options.gate)
        except (KeyError, TypeError, ValueError):
            print("report failed schema, denominator, or gate validation", file=sys.stderr)
            return 2
    else:
        if isinstance(report.get("manifest"), dict):
            if options.gate:
                parser.error("--gate cannot be used with a catalog plan report")
            try:
                rendered, code = render_catalog_plan(report, options.format)
            except (KeyError, TypeError, ValueError):
                print("report failed plan validation", file=sys.stderr)
                return 2
            print(rendered)
            return code
        if options.gate:
            parser.error("--gate requires a versioned lab v1 run report")
        summary = redact_secrets(summarize_report(report))
        if options.format == "json":
            rendered = json.dumps(summary, ensure_ascii=False, indent=2)
        elif options.format == "text":
            rendered = "\n".join(f"{key}: {value}" for key, value in summary.items())
        else:
            rendered = "\n".join(
                json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                for event in (
                    {"schema_version": 1, "event": "legacy_summary", "suite": summary.get("suite"), "status": summary.get("status")},
                    {"schema_version": 1, "event": "summary", "summary": summary},
                )
            )
        code = 0
    print(rendered)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
