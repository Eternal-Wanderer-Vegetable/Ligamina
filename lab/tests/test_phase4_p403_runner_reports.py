from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from lab.automation.phase4_p403_reports import reconcile_cancelled_p403_run


class Phase4P403RunnerCancellationReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.controlled = self.root / "controlled.json"
        self.self_test = self.root / "self-test.json"
        self.cleanup = self.root / "outer-cleanup.json"
        self.log = self.root / "runner.log"
        self.controlled.write_text(json.dumps({
            "schema_version": 1,
            "kind": "phase4_p403_controlled_run",
            "mode": "runner_contract_cancel",
            "status": "in-progress",
            "plugin_execution": "not_started",
        }), encoding="utf-8")
        self.cleanup.write_text(json.dumps({
            "schema_version": 1,
            "kind": "phase4_p403_runner_outer_cleanup",
            "cleanup_confirmed": True,
        }), encoding="utf-8")
        self.log.write_text(
            "P4-03 self-owned runner cancellation probe; cancel this workflow run.\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_cancelled_workflow_synthesizes_terminal_report_when_runner_trap_was_killed(self) -> None:
        result = reconcile_cancelled_p403_run(
            mode="runner_contract_cancel",
            job_status="cancelled",
            controlled_report_path=self.controlled,
            self_test_report_path=self.self_test,
            outer_cleanup_report_path=self.cleanup,
            log_path=self.log,
        )

        controlled = json.loads(self.controlled.read_text(encoding="utf-8"))
        self_test = json.loads(self.self_test.read_text(encoding="utf-8"))
        expected_report_sha = hashlib.sha256(json.dumps(
            {key: value for key, value in self_test.items() if key != "report_sha256"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        self.assertEqual(result["status"], "interrupted")
        self.assertTrue(result["probe_expectation_met"])
        self.assertEqual(controlled["status"], "interrupted")
        self.assertTrue(controlled["external_cancellation_observed"])
        self.assertEqual(self_test["status"], "interrupted")
        self.assertTrue(self_test["probe_started"])
        self.assertFalse(self_test["shell_exit_trap_reported"])
        self.assertTrue(self_test["probe_expectation_met"])
        self.assertEqual(self_test["report_sha256"], expected_report_sha)

    def test_cancellation_without_probe_start_or_confirmed_cleanup_does_not_pass(self) -> None:
        self.log.write_text("runner setup only\n", encoding="utf-8")
        self.cleanup.write_text(json.dumps({
            "schema_version": 1,
            "kind": "phase4_p403_runner_outer_cleanup",
            "cleanup_confirmed": False,
        }), encoding="utf-8")

        result = reconcile_cancelled_p403_run(
            mode="runner_contract_cancel",
            job_status="cancelled",
            controlled_report_path=self.controlled,
            self_test_report_path=self.self_test,
            outer_cleanup_report_path=self.cleanup,
            log_path=self.log,
        )

        self.assertFalse(result["probe_expectation_met"])
        self.assertFalse(json.loads(self.self_test.read_text(encoding="utf-8"))["probe_started"])

    def test_existing_nonterminal_self_test_report_is_closed_as_interrupted(self) -> None:
        self.self_test.write_text(json.dumps({
            "schema_version": 1,
            "kind": "phase4_p403_runner_contract_self_test",
            "mode": "runner_contract_cancel",
            "status": "in-progress",
            "plugin_execution": "not_started",
            "worker_claimed": False,
        }), encoding="utf-8")

        result = reconcile_cancelled_p403_run(
            mode="runner_contract_cancel",
            job_status="cancelled",
            controlled_report_path=self.controlled,
            self_test_report_path=self.self_test,
            outer_cleanup_report_path=self.cleanup,
            log_path=self.log,
        )

        self_test = json.loads(self.self_test.read_text(encoding="utf-8"))
        self.assertTrue(result["probe_expectation_met"])
        self.assertEqual(self_test["status"], "interrupted")
        self.assertFalse(self_test["shell_exit_trap_reported"])
        self.assertIsNone(self_test.get("exit_code"))

    def test_non_cancelled_finalizer_does_not_rewrite_runner_reports(self) -> None:
        before = self.controlled.read_bytes()
        result = reconcile_cancelled_p403_run(
            mode="runner_contract_cancel",
            job_status="success",
            controlled_report_path=self.controlled,
            self_test_report_path=self.self_test,
            outer_cleanup_report_path=self.cleanup,
            log_path=self.log,
        )

        self.assertEqual(result["status"], "not_cancelled")
        self.assertEqual(self.controlled.read_bytes(), before)
        self.assertFalse(self.self_test.exists())

    def test_module_entrypoint_matches_the_runner_finalizer_invocation(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "lab.automation.phase4_p403_reports",
                "runner_contract_cancel",
                "cancelled",
                str(self.controlled),
                str(self.self_test),
                str(self.cleanup),
                str(self.log),
            ],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["probe_expectation_met"])
        self.assertEqual(
            json.loads(self.self_test.read_text(encoding="utf-8"))["status"],
            "interrupted",
        )


if __name__ == "__main__":
    unittest.main()
