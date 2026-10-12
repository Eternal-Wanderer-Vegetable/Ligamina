from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from lab.native_runner import execute_harness, load_case, run_pair


ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "lab/test_workers/fixture_harness.py"
PROFILE = ROOT / "samples/profiles/nonebot-group-v0.1.json"
LOCKFILE = ROOT / "hosts/nonebot/uv.lock"


def fixture_command(*inject: str) -> list[str]:
    return [
        sys.executable,
        str(HARNESS),
        "--profile",
        str(PROFILE),
        "--lockfile",
        str(LOCKFILE),
        *inject,
    ]


class NativeRunnerTest(unittest.TestCase):
    def test_harness_gets_only_stimulus_and_logs_stay_out_of_json_stdout(self) -> None:
        case = load_case(ROOT / "samples/fixtures/event-routing/single-group-reply.json")
        script = (
            "import json, os, sys; "
            "payload=json.load(sys.stdin); "
            "assert 'expected' not in payload; "
            "print('harness diagnostic', file=sys.stderr); "
            "print(json.dumps({'status':'completed','mode':os.environ['LIGAMINA_RUN_MODE']}))"
        )
        result = execute_harness([sys.executable, "-c", script], case, "native", 5)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["mode"], "native")
        self.assertEqual(result["diagnostics"], "harness diagnostic\n")

    def test_context_is_passed_but_caller_secrets_are_not_inherited(self) -> None:
        case = load_case(ROOT / "samples/fixtures/event-routing/single-group-reply.json")
        case["context"] = {"artifact_hash": "frozen-digest"}
        script = (
            "import json, os, sys; payload=json.load(sys.stdin); "
            "assert payload['context']['artifact_hash']=='frozen-digest'; "
            "assert 'LIGAMINA_TEST_SECRET' not in os.environ; "
            "print(json.dumps({'status':'completed','context_seen':True}))"
        )
        with patch.dict("os.environ", {"LIGAMINA_TEST_SECRET": "must-not-cross"}):
            result = execute_harness([sys.executable, "-c", script], case, "native", 5)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["context_seen"])

    def test_missing_harness_is_reported_as_environment_missing(self) -> None:
        case = load_case(ROOT / "samples/fixtures/event-routing/single-group-reply.json")
        result = execute_harness([], case, "native", 5)
        self.assertEqual(result["status"], "environment_missing")

    def test_runner_executes_two_isolated_modes_and_accepts_the_fixture_case(self) -> None:
        case = load_case(ROOT / "samples/fixtures/event-routing/single-group-reply.json")
        command = fixture_command()
        report = run_pair(case, command, command, 5)
        self.assertEqual(report["native"]["mode"], "native")
        self.assertEqual(report["federated"]["mode"], "federated")
        self.assertEqual(report["comparison"]["status"], "passed", report["comparison"]["issues"])

    def test_runner_rejects_identical_wrong_behavior_and_single_side_mutations(self) -> None:
        case = load_case(ROOT / "samples/fixtures/event-routing/single-group-reply.json")
        same_fault = fixture_command("--inject", "wrong-recipient")
        report = run_pair(case, same_fault, same_fault, 5)
        self.assertEqual(report["comparison"]["status"], "failed")
        self.assertIn(
            "operation_expectation_failed",
            {issue["code"] for issue in report["comparison"]["issues"]},
        )

        native = fixture_command()
        federated = fixture_command("--inject", "leak-process")
        report = run_pair(case, native, federated, 5)
        self.assertEqual(report["comparison"]["status"], "failed")
        self.assertIn(
            "cleanup_expectation_failed",
            {issue["code"] for issue in report["comparison"]["issues"]},
        )


if __name__ == "__main__":
    unittest.main()
