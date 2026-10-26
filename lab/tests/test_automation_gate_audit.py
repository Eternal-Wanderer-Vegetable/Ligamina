from __future__ import annotations

import unittest

from lab.automation.gate_audit import build_gate_audit


class GateAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.report, cls.matrix = build_gate_audit()

    def test_denominators_are_independently_reconciled(self) -> None:
        denoms = self.report["denominators"]
        self.assertEqual(denoms["frozen_catalog_entries"], 5690)
        self.assertEqual(denoms["frozen_catalog_entry_os_positions"], 11380)
        self.assertEqual(denoms["p305_linux_unique_units"], 2206)
        self.assertEqual(denoms["p305_windows_unique_units"], 973)
        self.assertEqual(denoms["p305_cross_platform_unique_units"], 3179)
        self.assertEqual(denoms["p305_additive_http_reattributions_excluded"], 6)
        self.assertEqual(denoms["p308_behavior_positions"], 120)
        self.assertEqual(denoms["p308_planned_attempt_slots"], 360)
        self.assertEqual(denoms["p308_dispatched_attempts"], 0)

    def test_five_gates_are_separately_ruled_and_do_not_expand_support(self) -> None:
        statuses = {row["gate"]: row["status"] for row in self.report["gates"]}
        self.assertEqual(statuses, {
            "P3-ENGINE": "passed_scoped",
            "P3-RECOVERY": "blocked",
            "P3-AUTOMATION": "blocked",
            "P3-BEHAVIOR": "not_evaluated",
            "P3-FIELD": "not_run",
        })
        self.assertFalse(self.report["support_expansion"])
        self.assertEqual(self.report["plugin_compatibility"], "not_evaluated")

    def test_matrix_has_every_unique_affected_unit_and_terminal_status(self) -> None:
        self.assertEqual(self.report["matrix"]["rows"], 3179)
        for status in ("passed", "failed", "skipped", "unknown"):
            self.assertIn(f"| {status} |", self.matrix)
        self.assertIn("not_applicable_before_worker_start", self.matrix)

    def test_evidence_manifest_records_hashes_for_all_sources(self) -> None:
        manifest = self.report["evidence_manifest"]
        self.assertGreaterEqual(len(manifest), 20)
        self.assertTrue(all(len(row["file_sha256"]) == 64 for row in manifest))
        self.assertIn("phase3-p305-overall-closure-20261026.json", {row["path"].split("/")[-1] for row in manifest})


if __name__ == "__main__":
    unittest.main()
