from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from lab.compare import compare_pair


FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "samples"
    / "fixtures"
    / "event-routing"
    / "single-group-reply.json"
)


class CompareTest(unittest.TestCase):
    def setUp(self) -> None:
        self.case = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.expected = copy.deepcopy(self.case["expected"])
        self.good = {
            "status": "completed",
            "environment": copy.deepcopy(self.expected["environment"]),
            **copy.deepcopy(self.expected),
        }

    def compare(self, native=None, federated=None):
        return compare_pair(self.case, native or self.good, federated or self.good)

    def test_matching_expected_results_pass_even_when_trace_ids_differ(self) -> None:
        native = copy.deepcopy(self.good)
        federated = copy.deepcopy(self.good)
        federated["events"][0]["trace_id"] = "trace-other-run"
        result = self.compare(native, federated)
        self.assertTrue(result.passed, result.issues)

    def test_both_paths_producing_the_same_wrong_recipient_still_fail(self) -> None:
        wrong = copy.deepcopy(self.good)
        wrong["operations"][0]["target"]["id"] = "wrong-room"
        result = self.compare(wrong, wrong)
        self.assertFalse(result.passed)
        self.assertIn("operation_expectation_failed", {issue.code for issue in result.issues})

    def test_wrong_recipient_on_one_side_fails_expectation_and_equivalence(self) -> None:
        federated = copy.deepcopy(self.good)
        federated["operations"][0]["target"]["id"] = "wrong-room"
        result = self.compare(self.good, federated)
        self.assertFalse(result.passed)
        self.assertIn("operation_expectation_failed", {issue.code for issue in result.issues})

    def test_duplicate_reply_and_lost_message_segment_are_detected(self) -> None:
        duplicate = copy.deepcopy(self.good)
        duplicate["operations"].append(copy.deepcopy(duplicate["operations"][0]))
        self.assertFalse(self.compare(duplicate, duplicate).passed)

        lost_segment = copy.deepcopy(self.good)
        lost_segment["events"][0]["message"].pop()
        self.assertFalse(self.compare(lost_segment, lost_segment).passed)

    def test_environment_mismatch_and_residual_host_state_fail(self) -> None:
        mismatched = copy.deepcopy(self.good)
        mismatched["environment"]["lockfile_sha256"] = "different-lock"
        result = self.compare(mismatched, self.good)
        self.assertFalse(result.passed)
        self.assertIn("environment_mismatch", {issue.code for issue in result.issues})

        leaked = copy.deepcopy(self.good)
        leaked["cleanup"]["live_processes"] = [1234]
        self.assertFalse(self.compare(leaked, leaked).passed)


if __name__ == "__main__":
    unittest.main()
