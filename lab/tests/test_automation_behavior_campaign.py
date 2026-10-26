from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from lab.automation.behavior_campaign import (
    ROOT,
    behavior_accounting_matches_frozen,
    build_behavior_accounting_report,
)


class BehaviorCampaignAccountingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.report = build_behavior_accounting_report()
        cls.frozen = json.loads((ROOT / "outputs/acceptance/phase3-p308-behavior-accounting-20261026.json").read_text(encoding="utf-8"))

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

    def test_profile_lock_identities_are_stable_when_cache_is_not_present(self) -> None:
        counts = self.report["accounting"]["profile_cache_checks"]
        self.assertEqual(counts["mismatch"], 0)
        self.assertEqual(counts["missing_or_invalid"], 0)
        self.assertEqual(counts["verified"] + counts["not_available"], 120)
        for position in self.report["positions"]:
            self.assertEqual(
                position["profile"]["dependency_lock_sha256"],
                position["profile"]["cache_verification"]["sha256"],
            )

    def test_clean_checkout_reuses_frozen_cache_verification_only_for_matching_lock(self) -> None:
        def unavailable(lock: dict) -> dict:
            dependency_lock = lock["dependency_lock"]
            return {
                "status": "not_available",
                "sha256": dependency_lock["sha256"],
                "size_bytes": dependency_lock["size_bytes"],
            }

        with patch("lab.automation.behavior_campaign._verified_cache_blob", side_effect=unavailable):
            rebuilt = build_behavior_accounting_report()
        self.assertEqual(rebuilt["accounting"]["profile_cache_checks"]["not_available"], 120)
        self.assertTrue(behavior_accounting_matches_frozen(rebuilt, self.frozen))

    def test_present_cache_mismatch_does_not_match_frozen_verification(self) -> None:
        def mismatch(lock: dict) -> dict:
            dependency_lock = lock["dependency_lock"]
            return {
                "status": "mismatch",
                "sha256": dependency_lock["sha256"],
                "size_bytes": dependency_lock["size_bytes"],
                "observed_sha256": "0" * 64,
                "observed_size_bytes": dependency_lock["size_bytes"],
            }

        with patch("lab.automation.behavior_campaign._verified_cache_blob", side_effect=mismatch):
            rebuilt = build_behavior_accounting_report()
        self.assertFalse(behavior_accounting_matches_frozen(rebuilt, self.frozen))


if __name__ == "__main__":
    unittest.main()
