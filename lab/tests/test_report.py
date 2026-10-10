from __future__ import annotations

import unittest

from lab.report import summarize_cases, summarize_report


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


if __name__ == "__main__":
    unittest.main()
