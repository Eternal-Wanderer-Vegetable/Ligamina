from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from lab.automation.recovery import RecoveryPilotError, _locked_input_inventory, build_linux_pilot_manifest, validate_linux_pilot_manifest
from lab.automation.selection import ROOT


class RecoveryPilotManifestTests(unittest.TestCase):
    def test_linux_recovery_pilot_is_frozen_per_ecosystem_with_failed_controls(self) -> None:
        manifest_path = ROOT / "outputs/acceptance/phase3-p301-recovery-manifest-20261020.json"
        manifest = build_linux_pilot_manifest(manifest_path)
        result = validate_linux_pilot_manifest(manifest)
        self.assertEqual(result["selected_units"], 20)
        self.assertEqual(result["counts"]["nonebot"], {"affected_recovery": 8, "prior_plugin_failure_control": 2})
        self.assertEqual(result["counts"]["koishi"], {"affected_recovery": 8, "prior_plugin_failure_control": 2})
        self.assertEqual(manifest, build_linux_pilot_manifest(manifest_path))
        self.assertTrue(all(row["identity"]["platform"] == "linux" for row in manifest["selection"]))

    def test_manifest_rejects_tampered_identity_and_rebalanced_denominator(self) -> None:
        manifest = build_linux_pilot_manifest(ROOT / "outputs/acceptance/phase3-p301-recovery-manifest-20261020.json")
        changed_identity = copy.deepcopy(manifest)
        changed_identity["selection"][0]["identity"]["artifact_sha256"] = "0" * 64
        with self.assertRaisesRegex(RecoveryPilotError, "identity"):
            validate_linux_pilot_manifest(changed_identity)
        changed_denominator = copy.deepcopy(manifest)
        changed_denominator["expected_units"] += 1
        with self.assertRaisesRegex(RecoveryPilotError, "denominator"):
            validate_linux_pilot_manifest(changed_denominator)

    def test_missing_frozen_cache_inputs_are_reported_as_blockers(self) -> None:
        manifest = build_linux_pilot_manifest(ROOT / "outputs/acceptance/phase3-p301-recovery-manifest-20261020.json")
        with tempfile.TemporaryDirectory(prefix="ligamina-empty-cache-") as directory:
            result = _locked_input_inventory(
                manifest, ROOT / "catalogs/p2-41649744e057d61031d5", Path(directory),
            )
        self.assertFalse(result["ready"])
        self.assertEqual(result["ready_units"], 0)
        self.assertEqual(result["expected_units"], 20)
        self.assertTrue(all(not item["ready"] for item in result["units"]))


if __name__ == "__main__":
    unittest.main()
