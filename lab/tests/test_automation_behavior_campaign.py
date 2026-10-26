from __future__ import annotations

import unittest

from lab.automation.behavior_campaign import build_behavior_accounting_report


class BehaviorCampaignAccountingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.report = build_behavior_accounting_report()

    def test_full_double_platform_denominator_and_three_attempt_slots_are_accounted(self) -> None:
        self.assertEqual(self.report["scope"]["entries"], 20)
        self.assertEqual(self.report["scope"]["required_cases"], 60)
        self.assertEqual(self.report["scope"]["required_positions"], 120)
        self.assertEqual(self.report["scope"]["planned_attempt_slots"], 360)
        self.assertTrue(all(len(position["attempts"]) == 3 for position in self.report["positions"]))

    def test_unrunnable_positions_are_blocked_and_never_claim_worker_cleanup(self) -> None:
        accounting = self.report["accounting"]
        self.assertEqual(accounting["dependency_attributed_positions"], 120)
        self.assertEqual(accounting["dispatched_attempts"], 0)
        self.assertEqual(accounting["not_dispatched_attempts"], 360)
        self.assertEqual(accounting["worker_started_attempts"], 0)
        self.assertEqual(accounting["worker_cleanup_confirmed"], 0)
        self.assertEqual(accounting["worker_cleanup_not_applicable"], 360)
        self.assertEqual(accounting["missing_executable_fixture_positions"], 120)
        self.assertEqual(accounting["windows_positions_blocked_by_admission_scope"], 60)

    def test_report_keeps_behavior_compatibility_not_evaluated(self) -> None:
        self.assertEqual(self.report["status"], "accounted_with_dispatch_blockers")
        self.assertEqual(self.report["behavior_compatibility"], "not_evaluated")
        self.assertFalse(self.report["plugin_execution"])

    def test_all_frozen_profiles_and_dependency_blobs_are_resolved(self) -> None:
        self.assertEqual(self.report["accounting"]["profile_cache_checks"]["verified"], 120)
        self.assertEqual(self.report["accounting"]["profile_cache_checks"]["mismatch"], 0)
        self.assertEqual(self.report["accounting"]["profile_cache_checks"]["not_available"], 0)


if __name__ == "__main__":
    unittest.main()
