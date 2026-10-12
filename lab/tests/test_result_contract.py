from __future__ import annotations

import json
import unittest

from lab.result import CONTRACT_DIR, DOCUMENT_KINDS, normalize_legacy_status, summarize_results, validate_document


class LabResultContractTests(unittest.TestCase):
    def fixture(self, name: str) -> dict:
        return json.loads((CONTRACT_DIR / "fixtures" / name).read_text(encoding="utf-8"))

    def test_shared_golden_vectors_validate_in_python(self) -> None:
        vectors = self.fixture("vectors.json")
        self.assertEqual(vectors["schema_version"], 1)
        for kind in DOCUMENT_KINDS:
            for vector in vectors[kind]:
                value = self.fixture(vector["file"])
                if vector["valid"]:
                    validate_document(kind, value)
                else:
                    with self.assertRaises(ValueError, msg=vector["name"]):
                        validate_document(kind, value)

    def test_completed_run_state_does_not_turn_case_into_passed(self) -> None:
        run = self.fixture("run.completed.json")
        self.assertEqual(run["run_state"], "completed")
        self.assertEqual(run["results"][0]["status"], "passed")
        normalized = normalize_legacy_status("completed")
        self.assertEqual(normalized, {"status": "unknown", "reason_code": "completed_is_run_state"})

    def test_legacy_status_mapping_is_evidence_bounded(self) -> None:
        self.assertEqual(normalize_legacy_status("not_run"), {"status": "skipped", "reason_code": "not_scheduled"})
        self.assertEqual(
            normalize_legacy_status("environment_missing"),
            {"status": "unknown", "reason_code": "environment_missing_unproven"},
        )
        self.assertEqual(
            normalize_legacy_status("environment_missing", environment_missing_proven=True),
            {"status": "skipped", "reason_code": "environment_missing"},
        )
        self.assertEqual(
            normalize_legacy_status("maybe"),
            {"status": "unknown", "reason_code": "legacy_status_unrecognized"},
        )

    def test_summary_reports_missing_duplicate_unexpected_and_each_denominator(self) -> None:
        summary = summarize_results(
            ["a", "b", "c", "d"],
            [
                {"result_id": "a", "status": "passed"},
                {"result_id": "b", "status": "failed"},
                {"result_id": "c", "status": "skipped"},
                {"result_id": "c", "status": "passed"},
                {"result_id": "outside", "status": "passed"},
            ],
        )
        self.assertEqual(summary["status_counts"], {"passed": 1, "failed": 1, "skipped": 1, "unknown": 0})
        self.assertEqual(summary["missing_result_ids"], ["d"])
        self.assertEqual(summary["duplicate_result_ids"], ["c"])
        self.assertEqual(summary["unexpected_result_ids"], ["outside"])
        self.assertEqual(summary["executed_denominator"], 2)
        self.assertEqual(summary["entry_coverage"], 0.75)
        self.assertEqual(summary["stage_execution_coverage"], 0.5)
        self.assertEqual(summary["executed_pass_rate"], 0.5)
        self.assertEqual(summary["passed_over_expected_rate"], 0.25)
        self.assertFalse(summary["gate_passed"])
        self.assertEqual(summary, self.fixture("summary.mixed.json"))

    def test_zero_denominators_are_null_and_unknown_status_is_rejected(self) -> None:
        summary = summarize_results([], [])
        self.assertIsNone(summary["entry_coverage"])
        self.assertIsNone(summary["stage_execution_coverage"])
        self.assertIsNone(summary["executed_pass_rate"])
        self.assertIsNone(summary["passed_over_expected_rate"])
        with self.assertRaisesRegex(ValueError, "unknown v1 status"):
            summarize_results(["a"], [{"result_id": "a", "status": "completed"}])


if __name__ == "__main__":
    unittest.main()
