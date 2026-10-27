from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from lab.automation.phase4_p403 import (
    Phase4P403Error,
    _text_file_sha256_matches,
    build_p403_linux_executor_manifest,
    reconcile_p403_linux_runner_state,
    validate_p403_linux_executor_manifest,
    validate_p403_recovery_manifest,
)
from lab.automation.selection import ROOT, object_sha256


MANIFEST = ROOT / "outputs/acceptance/phase4-p403-recovery-manifest-20261027.json"
EXECUTOR_MANIFEST = ROOT / "outputs/acceptance/phase4-p403-linux-execution-manifest-20261027-v2.json"


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
