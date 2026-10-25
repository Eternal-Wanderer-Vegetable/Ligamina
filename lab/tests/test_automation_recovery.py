from __future__ import annotations

import copy
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lab.automation.recovery import (
    RecoveryPilotError,
    _locked_input_inventory,
    _project_worker_stage_result,
    _verify_pilot_worker_images,
    build_linux_pilot_manifest,
    validate_linux_pilot_manifest,
)
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
        self.assertEqual(manifest["worker_limits"]["timeout_seconds"], 240)
        self.assertEqual(manifest["probe_semantics_version"], 2)
        self.assertTrue(all(row["identity"]["platform"] == "linux" for row in manifest["selection"]))

    def test_worker_timeout_is_part_of_the_campaign_identity(self) -> None:
        source = ROOT / "outputs/acceptance/phase3-p301-recovery-manifest-20261020.json"
        self.assertNotEqual(
            build_linux_pilot_manifest(source, worker_timeout_seconds=120)["campaign_id"],
            build_linux_pilot_manifest(source, worker_timeout_seconds=240)["campaign_id"],
        )

    def test_probe_semantics_version_is_part_of_the_campaign_identity(self) -> None:
        source = ROOT / "outputs/acceptance/phase3-p301-recovery-manifest-20261020.json"
        self.assertNotEqual(
            build_linux_pilot_manifest(source, probe_semantics_version=1)["campaign_id"],
            build_linux_pilot_manifest(source, probe_semantics_version=2)["campaign_id"],
        )
        self.assertNotEqual(
            build_linux_pilot_manifest(source, probe_semantics_version=2)["campaign_id"],
            build_linux_pilot_manifest(source, probe_semantics_version=3)["campaign_id"],
        )

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

    def test_archive_size_overrides_are_bound_to_a_new_pilot_identity(self) -> None:
        recovery_manifest_path = ROOT / "outputs/acceptance/phase3-p301-recovery-manifest-20261020.json"
        base = build_linux_pilot_manifest(recovery_manifest_path)
        overrides = {
            "schema_version": 1,
            "base_artifact_lock_sha256": base["source_identity"]["artifact_lock_sha256"],
            "entries": [
                {
                    "registry_entry_id": row["identity"]["registry_entry_id"],
                    "artifact_sha256": row["identity"]["artifact_sha256"],
                    "cache_path": f"sha256/{row['identity']['artifact_sha256'][:2]}/{row['identity']['artifact_sha256']}",
                    "metadata_size_bytes": 100,
                    "archive_size_bytes": 50,
                }
                for row in base["selection"]
            ],
        }
        with tempfile.TemporaryDirectory() as directory, patch(
            "lab.automation.recovery._build_archive_size_overrides", return_value=overrides,
        ):
            frozen = build_linux_pilot_manifest(recovery_manifest_path, archive_cache_root=Path(directory))
        self.assertNotEqual(base["campaign_id"], frozen["campaign_id"])
        self.assertEqual(validate_linux_pilot_manifest(frozen)["status"], "valid")
        changed = copy.deepcopy(frozen)
        changed["archive_size_overrides"]["entries"][0]["archive_size_bytes"] += 1
        with self.assertRaisesRegex(RecoveryPilotError, "manifest hash"):
            validate_linux_pilot_manifest(changed)

    def test_non_cleanup_stage_does_not_report_cleanup_as_unconfirmed(self) -> None:
        probe = {
            "worker": {
                "cleanup_confirmed": True,
                "protocol_report": {
                    "native_load": {"status": "passed", "reason_code": None},
                    "cleanup": {"status": "passed", "reason_code": None},
                },
            },
        }
        native = _project_worker_stage_result("native", probe, "a" * 64)
        self.assertEqual(native["status"], "passed")
        self.assertNotIn("cleanup_confirmed", native)
        cleanup = _project_worker_stage_result("cleanup", probe, "a" * 64)
        self.assertTrue(cleanup["cleanup_confirmed"])

    def test_cleanup_stage_requires_worker_cleanup_evidence(self) -> None:
        probe = {
            "worker": {
                "cleanup_confirmed": False,
                "protocol_report": {"cleanup": {"status": "passed", "reason_code": None}},
            },
        }
        cleanup = _project_worker_stage_result("cleanup", probe, "b" * 64)
        self.assertEqual(cleanup["status"], "unknown")
        self.assertEqual(cleanup["reason_code"], "worker_cleanup_unconfirmed")
        self.assertFalse(cleanup["cleanup_confirmed"])

    def test_malformed_worker_probe_remains_unknown_instead_of_aborting_projection(self) -> None:
        missing = _project_worker_stage_result("native", None, "c" * 64)
        self.assertEqual(missing["status"], "unknown")
        self.assertEqual(missing["reason_code"], "worker_probe_missing")
        malformed = _project_worker_stage_result(
            "native", {"worker": {"protocol_report": {"native_load": "invalid"}}}, "d" * 64,
        )
        self.assertEqual(malformed["status"], "unknown")
        self.assertEqual(malformed["reason_code"], "native_evidence_missing")

    @patch("lab.automation.recovery.shutil.which", return_value="docker")
    @patch(
        "lab.automation.recovery.subprocess.run",
        side_effect=[SimpleNamespace(returncode=0, stdout="sha256:" + "a" * 64), SimpleNamespace(returncode=0, stdout="sha256:" + "b" * 64)],
    )
    def test_pilot_worker_image_preflight_checks_exact_digest_ids(self, _run, _which) -> None:
        inventory = _verify_pilot_worker_images({
            "nonebot": "nonebot@sha256:" + "a" * 64,
            "koishi": "koishi@sha256:" + "b" * 64,
        })
        self.assertEqual([item["status"] for item in inventory], ["present", "present"])

    @patch("lab.automation.recovery.shutil.which", return_value="docker")
    @patch(
        "lab.automation.recovery.subprocess.run",
        return_value=SimpleNamespace(returncode=1, stdout=""),
    )
    def test_missing_pilot_worker_image_fails_before_dispatch(self, _run, _which) -> None:
        with self.assertRaisesRegex(RecoveryPilotError, "unavailable or mismatched"):
            _verify_pilot_worker_images({
                "nonebot": "nonebot@sha256:" + "a" * 64,
                "koishi": "koishi@sha256:" + "b" * 64,
            })

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
