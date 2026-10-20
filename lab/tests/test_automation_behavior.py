from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from lab.automation.behavior import BehaviorSuiteError, SUITE_PATH, validate_behavior_suite


class BehaviorSuiteTests(unittest.TestCase):
    def test_frozen_suite_binds_twenty_entries_and_four_legacy_versions(self) -> None:
        result = validate_behavior_suite()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["entries"], 20)
        self.assertEqual(result["required_cases"], 60)
        self.assertEqual(result["legacy_regressions"], 4)
        self.assertEqual(result["executed_platforms"], [])
        self.assertFalse(result["windows_tests_run"])

    def test_duplicate_entry_and_removed_required_case_fail_closed(self) -> None:
        suite = json.loads(SUITE_PATH.read_text(encoding="utf-8"))
        suite["entries"][1]["registry_entry_id"] = suite["entries"][0]["registry_entry_id"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "suite.json"
            path.write_text(json.dumps(suite), encoding="utf-8")
            with self.assertRaisesRegex(BehaviorSuiteError, "duplicate catalog entry"):
                validate_behavior_suite(path)

    def test_missing_isolation_case_fails_schema_validation(self) -> None:
        suite = json.loads(SUITE_PATH.read_text(encoding="utf-8"))
        suite["entries"][0]["cases"] = [case for case in suite["entries"][0]["cases"] if case["class"] != "isolation"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "suite.json"
            path.write_text(json.dumps(suite), encoding="utf-8")
            with self.assertRaises(BehaviorSuiteError):
                validate_behavior_suite(path)

    def test_changed_artifact_binding_is_rejected(self) -> None:
        suite = json.loads(SUITE_PATH.read_text(encoding="utf-8"))
        suite["entries"][0]["source_review"]["reviewed_artifact_sha256"] = "0" * 64
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "suite.json"
            path.write_text(json.dumps(suite), encoding="utf-8")
            with self.assertRaisesRegex(BehaviorSuiteError, "source review is not bound"):
                validate_behavior_suite(path)


if __name__ == "__main__":
    unittest.main()
