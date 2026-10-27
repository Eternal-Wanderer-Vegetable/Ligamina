from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from lab.automation.phase4_p403 import (
    Phase4P403Error,
    _seal_p403_recovery_report,
    _text_file_sha256_matches,
    build_p403_linux_executor_manifest,
    build_p403_linux_remaining_executor_manifest,
    reconcile_p403_linux_runner_state,
    validate_p403_linux_executor_manifest,
    validate_p403_recovery_manifest,
)
from lab.automation.selection import ROOT, object_sha256


MANIFEST = ROOT / "outputs/acceptance/phase4-p403-recovery-manifest-20261027.json"
EXECUTOR_MANIFEST = ROOT / "outputs/acceptance/phase4-p403-linux-execution-manifest-20261027-v3.json"
REMAINING_EXECUTOR_MANIFEST = ROOT / "outputs/acceptance/phase4-p403-linux-execution-manifest-20261027-v4-not-run.json"
INTERRUPTED_REPORT = ROOT / "outputs/acceptance/phase4-p403-linux-recovery-run-40479738802.json"
INTERRUPTED_CONTROLLED_REPORT = ROOT / "outputs/acceptance/phase4-p403-linux-controlled-run-40479738802.json"


class Phase4P403TextHashTests(unittest.TestCase):
    def test_parent_text_hash_accepts_line_ending_conversion_but_rejects_content_changes(self) -> None:
        lf_bytes = b'{"evidence":"same"}\n'
        crlf_digest = hashlib.sha256(lf_bytes.replace(b"\n", b"\r\n")).hexdigest()

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "parent.json"
            path.write_bytes(lf_bytes)
            self.assertTrue(_text_file_sha256_matches(path, crlf_digest))

            path.write_bytes(b'{"evidence":"changed"}\n')
            self.assertFalse(_text_file_sha256_matches(path, crlf_digest))

    def test_recovery_report_seal_excludes_a_stale_embedded_digest(self) -> None:
        report = {"kind": "phase4_p403_linux_unknown_recovery", "report_sha256": "stale"}
        _seal_p403_recovery_report(report)
        self.assertEqual(report["report_sha256"], object_sha256({"kind": report["kind"]}))


class Phase4P403RecoveryManifestTests(unittest.TestCase):
    def test_frozen_manifest_covers_exact_53_parent_unknowns_and_77_linux_attempts(self) -> None:
        report = validate_p403_recovery_manifest(MANIFEST)

        self.assertEqual(report["status"], "valid")
        self.assertEqual(report["total"], 53)
        self.assertEqual(report["by_cohort"], {"linux": 39, "windows_pilot": 10, "windows_preparation": 4})
        self.assertEqual(report["linux_unknown_attempts"], 77)

    def test_executor_inherits_images_from_the_verified_p3_linux_run(self) -> None:
        executor = build_p403_linux_executor_manifest(MANIFEST)

        self.assertEqual(executor["worker_images"], {
            "nonebot": "ligamina-s205-python-profile@sha256:fcb712608785413b1db90fd2d96031beb1e1a18588b89d6cacdde906ce42ab89",
            "koishi": "node@sha256:ba849c60be29959425b8734d57b8b4b7d56f98edd9504c9af091d5281095a71e",
        })
        self.assertEqual(executor["worker_image_source"], {
            "manifest_path": "outputs/acceptance/phase3-p305-linux-affected-v3-manifest-20261025.json",
            "manifest_file_sha256": "91798c872560306155a5e8330067062f95be3f6d7fee6b566156ebc9f9087cbe",
            "manifest_sha256": "961786350b7c0ebdb990650d051203ad82452fff72ac291402a7f46a517173c8",
            "report_path": "outputs/acceptance/phase3-p305-linux-affected-v3-execution-resume6-20261025.json",
            "report_file_sha256": "8b4119e6bc56f2f26f24fc858458aa7c2491f43b0aa42fbc30bc94fe1a19c5f5",
            "campaign_id": "p3-recovery-linux-842f71712e2c",
        })
        self.assertEqual(executor["archive_size_overrides"]["base_artifact_lock_sha256"], "8dcec5da7cd81b9ed7d0f4e8aead8e76ef15cd7e17dfcdc4fa6f7b8944b83d4a")
        self.assertEqual(len(executor["archive_size_overrides"]["entries"]), 39)
        self.assertEqual(executor["archive_size_overrides"]["entries"][0]["registry_entry_id"], "ko-0831a0963acd3839ef3cb5f4")
        self.assertEqual(executor["archive_size_overrides"]["entries"][0]["archive_size_bytes"], 4787)
        self.assertEqual(executor["expected_units"], 39)

    def test_executor_rejects_stale_worker_image_digest(self) -> None:
        data = json.loads(EXECUTOR_MANIFEST.read_text(encoding="utf-8"))
        data["worker_images"]["nonebot"] = "ligamina-s205-python-profile@sha256:88bc85522c681966347809c702e0ae7fcf6fba4d8860161e77d7aa877a04488e"
        data["manifest_sha256"] = object_sha256({key: value for key, value in data.items() if key != "manifest_sha256"})
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "executor.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(Phase4P403Error, "not bound to the verified P3 source run"):
                validate_p403_linux_executor_manifest(MANIFEST, path)

    def test_executor_rejects_archive_size_override_not_bound_to_p3_evidence(self) -> None:
        data = json.loads(EXECUTOR_MANIFEST.read_text(encoding="utf-8"))
        data["archive_size_overrides"]["entries"][0]["archive_size_bytes"] += 1
        data["manifest_sha256"] = object_sha256({key: value for key, value in data.items() if key != "manifest_sha256"})
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "executor.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(Phase4P403Error, "archive sizes are not bound"):
                validate_p403_linux_executor_manifest(MANIFEST, path)

    def test_missing_parent_unknown_cannot_be_rebalanced(self) -> None:
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        data["rows"].pop()
        data["manifest_sha256"] = object_sha256({key: value for key, value in data.items() if key != "manifest_sha256"})
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manifest.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(Phase4P403Error, "exactly 53 parent identities"):
                validate_p403_recovery_manifest(path)

    def test_parent_attempt_digest_cannot_be_replaced(self) -> None:
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        linux_row = next(row for row in data["rows"] if row["cohort"] == "linux")
        linux_row["parent"]["parent_unknown_attempts"][0]["attempt_record_sha256"] = "0" * 64
        data["manifest_sha256"] = object_sha256({key: value for key, value in data.items() if key != "manifest_sha256"})
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manifest.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(Phase4P403Error, "unknown attempt evidence hash changed"):
                validate_p403_recovery_manifest(path)

    def test_retry_budget_is_bounded_to_one_new_attempt(self) -> None:
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        row = next(row for row in data["rows"] if row["cohort"] == "linux")
        row["recovery"]["retry_budget"] = 2
        data["manifest_sha256"] = object_sha256({key: value for key, value in data.items() if key != "manifest_sha256"})
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manifest.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(Phase4P403Error, "retry budget/status invalid"):
                validate_p403_recovery_manifest(path)

    def test_linux_recovery_requires_exactly_one_bounded_new_attempt(self) -> None:
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        row = next(row for row in data["rows"] if row["cohort"] == "linux")
        row["recovery"]["retry_budget"] = 0
        data["manifest_sha256"] = object_sha256({key: value for key, value in data.items() if key != "manifest_sha256"})
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manifest.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(Phase4P403Error, "retry budget/status invalid"):
                validate_p403_recovery_manifest(path)

    def test_remaining_executor_selects_only_prior_not_run_identities(self) -> None:
        prior_report = json.loads(INTERRUPTED_REPORT.read_text(encoding="utf-8"))
        expected_not_run = {
            row["registry_entry_id"]
            for row in prior_report["plugin_reports"]
            if row["recovery_status"] == "not_run"
        }
        previously_attempted = {
            row["registry_entry_id"]
            for row in prior_report["plugin_reports"]
            if row["attempts"]
        }

        executor = build_p403_linux_remaining_executor_manifest(
            MANIFEST, EXECUTOR_MANIFEST, INTERRUPTED_REPORT, INTERRUPTED_CONTROLLED_REPORT,
        )
        selected = {row["identity"]["registry_entry_id"] for row in executor["selection"]}
        self.assertEqual(len(expected_not_run), 25)
        self.assertEqual(len(previously_attempted), 14)
        self.assertEqual(selected, expected_not_run)
        self.assertFalse(selected & previously_attempted)
        self.assertEqual(executor["expected_units"], 25)
        self.assertEqual(executor["phase4_previous_recovery"]["campaign_id"], "p4-p403-linux-1932fab55a8b")
        self.assertEqual(executor["phase4_previous_recovery"]["not_run_parent_count"], 25)
        self.assertEqual(executor["phase4_previous_recovery"]["run_id"], "40479738802")

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "remaining-executor.json"
            path.write_text(json.dumps(executor), encoding="utf-8")
            validation = validate_p403_linux_executor_manifest(MANIFEST, path)
        self.assertEqual(validation["status"], "valid")
        self.assertEqual(validation["selected_units"], 25)
        self.assertEqual(
            validation["previous_recovery_report_file_sha256"],
            executor["phase4_previous_recovery"]["report_file_sha256"],
        )

    def test_remaining_executor_rejects_previous_not_run_set_replacement(self) -> None:
        executor = build_p403_linux_remaining_executor_manifest(
            MANIFEST, EXECUTOR_MANIFEST, INTERRUPTED_REPORT, INTERRUPTED_CONTROLLED_REPORT,
        )
        executor["phase4_previous_recovery"]["not_run_parent_ids_sha256"] = "0" * 64
        executor["manifest_sha256"] = object_sha256({key: value for key, value in executor.items() if key != "manifest_sha256"})
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "remaining-executor.json"
            path.write_text(json.dumps(executor), encoding="utf-8")
            with self.assertRaisesRegex(Phase4P403Error, "does not exactly match the prior not_run identities"):
                validate_p403_linux_executor_manifest(MANIFEST, path)

    def test_remaining_executor_can_continue_a_later_interrupted_selection(self) -> None:
        parent = json.loads(MANIFEST.read_text(encoding="utf-8"))
        remaining_executor = json.loads(REMAINING_EXECUTOR_MANIFEST.read_text(encoding="utf-8"))
        source_report = json.loads(INTERRUPTED_REPORT.read_text(encoding="utf-8"))
        selected_ids = {
            row["identity"]["registry_entry_id"]
            for row in remaining_executor["selection"]
        }
        report_rows = [
            json.loads(json.dumps(row))
            for row in source_report["plugin_reports"]
            if row["registry_entry_id"] in selected_ids
        ]
        for row in report_rows:
            row["recovery_status"] = "not_run"
            row["attempts"] = []
        report = {
            "schema_version": 1,
            "kind": "phase4_p403_linux_unknown_recovery",
            "status": "interrupted",
            "queue_state": "interrupted",
            "campaign_id": remaining_executor["campaign_id"],
            "manifest_sha256": remaining_executor["manifest_sha256"],
            "phase4_parent_manifest_sha256": remaining_executor["phase4_parent_manifest"]["manifest_sha256"],
            "phase4_executor_manifest_sha256": remaining_executor["manifest_sha256"],
            "parent_unknown_count": 39,
            "selected_parent_count": len(selected_ids),
            "denominator": {"expected_units": len(selected_ids)},
            "counts": {
                "recovered": 0,
                "plugin_or_stage_failed": 0,
                "unknown_or_cleanup_unconfirmed": 0,
                "not_run": len(selected_ids),
            },
            "plugin_reports": report_rows,
        }
        _seal_p403_recovery_report(report)
        executor_relative = REMAINING_EXECUTOR_MANIFEST.relative_to(ROOT).as_posix()

        with tempfile.TemporaryDirectory(dir=ROOT, prefix=".p403-recursive-") as temporary:
            temporary_path = Path(temporary)
            report_path = temporary_path / "recovery.json"
            controlled_path = temporary_path / "controlled.json"
            report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
            report_relative = report_path.relative_to(ROOT).as_posix()
            controlled = {
                "schema_version": 1,
                "kind": "phase4_p403_controlled_run",
                "mode": "linux_unknown_recovery_remaining",
                "status": "blocked",
                "exit_code": 3,
                "plugin_execution": "dispatched",
                "run_id": "synthetic-later-interruption",
                "evidence_sha256": {
                    report_relative: hashlib.sha256(report_path.read_bytes()).hexdigest(),
                    executor_relative: hashlib.sha256(REMAINING_EXECUTOR_MANIFEST.read_bytes()).hexdigest(),
                },
            }
            controlled_path.write_text(json.dumps(controlled, ensure_ascii=False), encoding="utf-8")
            next_executor = build_p403_linux_remaining_executor_manifest(
                MANIFEST, REMAINING_EXECUTOR_MANIFEST, report_path, controlled_path,
            )
            next_path = temporary_path / "next-executor.json"
            next_path.write_text(json.dumps(next_executor), encoding="utf-8")
            validation = validate_p403_linux_executor_manifest(MANIFEST, next_path)

        self.assertEqual(validation["status"], "valid")
        self.assertEqual(validation["selected_units"], 25)
        self.assertNotEqual(next_executor["campaign_id"], remaining_executor["campaign_id"])
        self.assertEqual(
            {row["identity"]["registry_entry_id"] for row in next_executor["selection"]},
            selected_ids,
        )

    def test_outer_reconciliation_is_not_applicable_without_a_claimed_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = reconcile_p403_linux_runner_state(
                MANIFEST, EXECUTOR_MANIFEST, Path(temporary) / "queue",
            )

        self.assertEqual(report["status"], "not_started")
        self.assertIsNone(report["cleanup_confirmed"])
        self.assertEqual(report["attempts_reconciled"], 0)


if __name__ == "__main__":
    unittest.main()
