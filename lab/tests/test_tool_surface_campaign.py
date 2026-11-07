"""TOOL-04 orchestrator: case binding to the frozen parent suite and Oracle evaluation."""

from __future__ import annotations

import copy
import unittest

from lab.automation.behavior_v2 import SUITE_PATH, _read
from lab.automation.tool_surface_campaign import CASES_PATH, evaluate, validate_cases

HOST = "sample-phase4-ko-163c3bd4cf87c72c24210251-positive"


def _passing_result(replies: list[str]) -> dict:
    return {
        "status": "completed",
        "outer_worker": {"cleanup_confirmed": True},
        "federated_trace": {"host_pid_reaped": True},
        "plugin_observation": {
            "gateway_operation_count": 0,
            "tool_calls": {
                "listed_command_tools": [f"cmd.{HOST}.echo"],
                "steps": [[{"is_error": False, "dispatch": "completed", "replies": replies, "refused_operations": []}]],
            },
        },
    }


class ToolSurfaceCampaignTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cases = _read(CASES_PATH)
        self.suite = _read(SUITE_PATH)
        self.global_oracle = self.cases["global_oracle"]
        self.echo_positive = self.cases["entries"][0]["cases"][0]

    def test_cases_bind_to_frozen_parent_oracles(self) -> None:
        result = validate_cases(self.cases, self.suite)
        self.assertEqual(result["status"], "valid")
        self.assertEqual(sum(case["cited_parent_texts"] for case in result["cases"]), 6)

    def test_drifted_expected_text_is_rejected(self) -> None:
        drifted = copy.deepcopy(self.cases)
        drifted["entries"][0]["cases"][0]["oracle"]["calls"][0][0]["replies"] = ["beta"]
        with self.assertRaisesRegex(ValueError, "does not match"):
            validate_cases(drifted, self.suite)

    def test_passing_attempt_has_no_issues(self) -> None:
        self.assertEqual(evaluate(self.echo_positive, "echo", HOST, _passing_result(["alpha"]), self.global_oracle), [])

    def test_wrong_reply_gateway_use_and_unconfirmed_cleanup_are_reported(self) -> None:
        result = _passing_result(["beta"])
        result["plugin_observation"]["gateway_operation_count"] = 1
        result["outer_worker"]["cleanup_confirmed"] = False
        issues = evaluate(self.echo_positive, "echo", HOST, result, self.global_oracle)
        self.assertIn("worker_cleanup_unconfirmed", issues)
        self.assertIn("gateway_operations:1", issues)
        self.assertTrue(any(issue.startswith("step0.call0:replies") for issue in issues))

    def test_tool_errors_and_missing_tools_fail_the_attempt(self) -> None:
        result = _passing_result(["alpha"])
        result["plugin_observation"]["tool_calls"]["listed_command_tools"] = []
        result["plugin_observation"]["tool_calls"]["steps"][0][0].update({"is_error": True, "protocol_error": "boom"})
        issues = evaluate(self.echo_positive, "echo", HOST, result, self.global_oracle)
        self.assertTrue(any(issue.startswith("tool_not_listed") for issue in issues))
        self.assertTrue(any(issue.startswith("step0.call0:error") for issue in issues))

    def test_forbidden_text_and_refusals_are_caught_in_isolation(self) -> None:
        isolation = self.cases["entries"][0]["cases"][2]
        result = _passing_result([])
        result["plugin_observation"]["tool_calls"]["steps"] = [[
            {"is_error": False, "dispatch": "completed", "replies": ["group-message"], "refused_operations": []},
            {"is_error": False, "dispatch": "completed", "replies": ["private-message"], "refused_operations": []},
            {"is_error": False, "dispatch": "completed", "replies": ["leaked-message"], "refused_operations": [{"operation": "send_message"}]},
        ]]
        issues = evaluate(isolation, "echo", HOST, result, self.global_oracle)
        self.assertIn("step0.call2:forbidden_text_delivered", issues)
        self.assertIn("step0.call2:refused_operations:1", issues)

    def test_failed_side_stops_evaluation_with_its_reason(self) -> None:
        issues = evaluate(
            self.echo_positive, "echo", HOST,
            {"status": "failed", "diagnostic": "setup failed", "outer_worker": {"cleanup_confirmed": True}},
            self.global_oracle,
        )
        self.assertEqual(len(issues), 1)
        self.assertTrue(issues[0].startswith("side_status:failed"))


if __name__ == "__main__":
    unittest.main()
