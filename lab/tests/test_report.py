from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from lab.report import (
    build_run_report,
    main,
    redact_secrets,
    render_v1_report,
    report_exit_code,
    render_catalog_plan,
    render_summary_output,
    summarize_cases,
    summarize_report,
    validate_run_report,
)
from lab.result import summarize_results


def make_run_report(
    *,
    expected: list[str] | None = None,
    results: list[dict] | None = None,
    run_state: str = "completed",
    gate: str = "compatibility",
) -> dict:
    expected = expected if expected is not None else ["nb-1:windows:case-1"]
    results = results if results is not None else [{
        "schema_version": 1,
        "result_id": expected[0],
        "registry_entry_id": "nb-1",
        "ecosystem": "nonebot",
        "official_maintained": True,
        "package": "nonebot-plugin-example",
        "version": "1.2.3",
        "platform": "windows",
        "profile_id": "nonebot-2.4",
        "stage": "federated",
        "case_id": "message.echo",
        "attempt_id": "attempt-1",
        "status": "passed",
        "delivery_scope": "virtual",
    }]
    fields = {
        "suite": "catalog",
        "run_id": "report-test-001",
        "run_state": run_state,
        "created_at_utc": "2026-10-12T00:00:00Z",
        "catalog": {
            "schema_version": 1,
            "snapshot_id": "p2-fixture",
            "source_snapshot_sha256": "a" * 64,
            "fetched_at_utc": "2026-10-12T00:00:00Z",
            "entry_count": 1,
            "official_maintained_count": 1,
            "ecosystems": ["nonebot"],
            "filter_policy": "none",
        },
        "provenance": {
            "git_commit": "abcdef0123456789",
            "git_dirty": False,
            "framework": "nonebot",
            "runtime": "python-3.12.14",
            "os": "windows",
            "architecture": "x86_64",
            "profile_id": "nonebot-2.4",
            "dependency_lock_sha256": "b" * 64,
        },
    }
    if run_state == "completed":
        fields["finished_at_utc"] = "2026-10-12T00:01:00Z"
    return build_run_report(fields=fields, expected_result_ids=expected, results=results, gate=gate)


class ReportSummaryTests(unittest.TestCase):
    def test_skips_are_not_counted_as_executed_passes_and_unknowns_stay_visible(self) -> None:
        summary = summarize_cases(
            [
                {"status": "passed"},
                {"status": "failed"},
                {"status": "skipped", "reason": "not applicable"},
                {"status": "unknown", "reason": "no receipt"},
            ],
            expected_count=4,
        )
        self.assertEqual(summary["expected_denominator"], 4)
        self.assertEqual(summary["executed_denominator"], 3)
        self.assertEqual(summary["passed"], 1)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(summary["unknown"], 1)
        self.assertFalse(summary["gate_passed"])

    def test_missing_and_unexpected_cases_are_reported(self) -> None:
        missing = summarize_cases([{"status": "passed"}], expected_count=2)
        self.assertEqual(missing["missing"], 1)
        self.assertEqual(missing["coverage_rate"], 0.5)
        self.assertFalse(missing["gate_passed"])

        unexpected = summarize_cases([{"status": "passed"}, {"status": "passed"}], expected_count=1)
        self.assertEqual(unexpected["unexpected"], 1)
        self.assertFalse(unexpected["gate_passed"])

    def test_invalid_status_is_retained_as_unknown(self) -> None:
        summary = summarize_cases([{"status": "maybe"}], expected_count=1)
        self.assertEqual(summary["unknown"], 1)
        self.assertEqual(summary["invalid_statuses"], [{"index": 0, "status": "maybe"}])
        self.assertFalse(summary["gate_passed"])

    def test_report_projection_uses_the_same_case_summary(self) -> None:
        expected = summarize_cases([{"status": "passed"}], expected_count=1)
        projected = summarize_report({"suite": "stability", "status": "completed", "case_summary": expected})
        self.assertEqual(projected["case_summary"], expected)


class UnifiedReportTests(unittest.TestCase):
    def test_partial_run_keeps_denominator_and_completion_is_not_compatibility(self) -> None:
        report = make_run_report(
            expected=["a", "b"],
            results=[{
                "schema_version": 1, "result_id": "a", "registry_entry_id": "nb-1",
                "ecosystem": "nonebot", "package": "example", "version": "1.0",
                "platform": "windows", "profile_id": "p", "stage": "install",
                "case_id": "install", "attempt_id": "a1", "status": "failed",
                "reason_code": "install_failed", "failure_domain": "package",
                "delivery_scope": "virtual",
            }],
            run_state="running",
            gate="completion",
        )
        self.assertEqual(report["summary"]["expected_result_count"], 2)
        self.assertEqual(report["summary"]["accounted_result_count"], 1)
        self.assertEqual(report["summary"]["missing_result_ids"], ["b"])
        self.assertEqual(report["exit_code"], 1)
        self.assertFalse(report["gate_passed"])

        completed = make_run_report(
            expected=["a", "b"],
            results=[
                {**report["results"][0], "status": "failed", "reason_code": "install_failed"},
                {
                    "schema_version": 1, "result_id": "b", "registry_entry_id": "nb-2",
                    "ecosystem": "nonebot", "package": "example-two", "version": "1.0",
                    "platform": "windows", "profile_id": "p", "stage": "federated",
                    "case_id": "echo", "attempt_id": "b1", "status": "passed",
                    "delivery_scope": "virtual",
                },
            ],
            gate="completion",
        )
        self.assertEqual(completed["exit_code"], 0)
        self.assertFalse(completed["summary"]["gate_passed"])
        self.assertEqual(report_exit_code(completed, "compatibility"), 1)

    def test_gate_semantics_include_unknown_and_infrastructure_failures(self) -> None:
        row = make_run_report()["results"][0]
        package_failure = make_run_report(results=[{
            **row, "status": "failed", "reason_code": "plugin_error", "failure_domain": "package",
        }], gate="automation")
        self.assertEqual(package_failure["exit_code"], 0)

        infrastructure_failure = make_run_report(results=[{
            **row, "status": "failed", "reason_code": "worker_lost", "failure_domain": "infrastructure",
        }], gate="automation")
        self.assertEqual(infrastructure_failure["exit_code"], 1)

        interrupted = make_run_report(run_state="interrupted", gate="completion")
        self.assertEqual(interrupted["exit_code"], 3)

    def test_schema_and_recomputed_summary_are_both_required(self) -> None:
        report = make_run_report()
        validate_run_report(report)
        invalid_status = json.loads(json.dumps(report))
        invalid_status["results"][0]["status"] = "completed"
        with self.assertRaises(ValueError):
            validate_run_report(invalid_status)

        wrong_summary = json.loads(json.dumps(report))
        wrong_summary["summary"]["status_counts"]["passed"] = 99
        with self.assertRaisesRegex(ValueError, "summary does not match"):
            validate_run_report(wrong_summary)

        missing_denominator = json.loads(json.dumps(report))
        missing_denominator.pop("expected_result_ids")
        with self.assertRaises(ValueError):
            validate_run_report(missing_denominator)

    def test_json_text_and_ndjson_are_parseable_and_privacy_safe(self) -> None:
        report = make_run_report()
        report["results"][0]["reason"] = "password=very-secret-value " + ("x" * 5000)
        rendered = render_v1_report(report, "json")
        parsed = json.loads(rendered)
        self.assertEqual(parsed["summary"], summarize_results(report["expected_result_ids"], report["results"]))
        self.assertNotIn("very-secret-value", rendered)
        self.assertNotIn("private_token", rendered)
        self.assertLess(len(rendered), 5000)

        ndjson = render_v1_report(report, "ndjson")
        events = [json.loads(line) for line in ndjson.splitlines()]
        self.assertEqual([event["event"] for event in events], ["run", "matrix", "summary"])
        self.assertTrue(all(event["schema_version"] == 1 for event in events))
        self.assertIn("Gate compatibility", render_v1_report(report, "text"))

    def test_redaction_masks_fields_inline_secrets_and_bounds_strings(self) -> None:
        value = redact_secrets({
            "api_key": "secret",
            "message": "Bearer abc.def password=hunter2",
            "large": "z" * 5000,
        })
        self.assertEqual(value["api_key"], "[REDACTED]")
        self.assertNotIn("abc.def", value["message"])
        self.assertNotIn("hunter2", value["message"])
        self.assertIn("[truncated]", value["large"])

    def test_cli_exit_codes_and_stdout_contract(self) -> None:
        partial = make_run_report(
            expected=["a", "b"],
            results=[{**make_run_report()["results"][0], "result_id": "a"}],
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "partial.json"
            path.write_text(json.dumps(partial), encoding="utf-8")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main([str(path), "--format", "json", "--gate", "completion"])
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(stdout.getvalue())["summary"]["missing_count"], 1)

            path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
            stderr = io.StringIO()
            stdout = io.StringIO()
            with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(stdout):
                code = main([str(path)])
            self.assertEqual(code, 2)
            self.assertEqual(stdout.getvalue(), "")

    def test_all_three_gates_are_explicit_and_selectable(self) -> None:
        report = make_run_report()
        self.assertEqual(report_exit_code(report, "compatibility"), 0)
        self.assertEqual(report_exit_code(report, "completion"), 0)
        self.assertEqual(report_exit_code(report, "automation"), 0)
        with self.assertRaises(ValueError):
            report_exit_code(report, "unknown")

    def test_unknown_status_cli_input_is_a_schema_error_and_run_summary_formats(self) -> None:
        report = make_run_report()
        report["results"][0]["status"] = "completed"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = main([str(path)])
            self.assertEqual(code, 2)
            self.assertIn("schema", stderr.getvalue())

            report = make_run_report()
            report["suite"] = "unknown"
            path.write_text(json.dumps(report), encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()):
                code = main([str(path)])
            self.assertEqual(code, 2)

        event = json.loads(render_summary_output({"event": "catalog_plan", "selected": 3}, "ndjson"))
        self.assertEqual(event["event"], "catalog_plan")
        self.assertEqual(event["summary"]["selected"], 3)

    def test_catalog_plan_formats_keep_queued_jobs_out_of_compatibility_claims(self) -> None:
        plan = {
            "status": "planned", "run_id": "catalog-run", "run_state": "queued",
            "snapshot_id": "snapshot", "total_catalog_entries": 5690,
            "expected_units": 5690, "selected_units": 3, "expected_stage_jobs": 18,
            "job_counts": {"queued": 18, "leased": 0, "passed": 0, "failed": 0, "skipped": 0, "unknown": 0, "total": 18},
            "execution_started": False, "manifest": {
                "stages": ["a", "b", "c", "d", "e", "f"],
                "snapshot_entry_count": 5690, "selected_unit_count": 3, "expected_stage_count": 18,
            },
            "queue_path": "C:/private/path/ignored.sqlite3",
        }
        output, code = render_catalog_plan(plan, "json")
        parsed = json.loads(output)
        self.assertEqual(code, 0)
        self.assertFalse(parsed["execution_started"])
        self.assertEqual(parsed["job_counts"]["queued"], 18)
        self.assertNotIn("queue_path", parsed)
        self.assertIn("Plugin execution started: no", render_catalog_plan(plan, "text")[0])
        plan["execution_started"] = True
        with self.assertRaises(ValueError):
            render_catalog_plan(plan)


if __name__ == "__main__":
    unittest.main()
