from __future__ import annotations

import json
from pathlib import Path
import re
import tempfile
import unittest

from lab.astrbot_native_tool_campaign import (
    _replies,
    _oracle_equivalent,
    _runtime_archive_manifest,
    _runtime_manifest,
    _tree_sha256,
    _validate_federated_report,
)
from lab.astrbot_native_runtime import write_runtime_archive
from lab.astrbot_tool_campaign import CASES_PATH
from lab.astrbot_census import LOCK_FILE as ASTRBOT_LOCK_FILE

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = ROOT / "catalogs/ab-15937e90d26b7caf9d12"
FEDERATED = ROOT / "outputs/acceptance/ab05-astrbot-tool-linux-20261108-v2.json"


class AstrBotNativeToolCampaignTests(unittest.TestCase):
    def test_native_worker_does_not_import_ligamina_host_or_bridge(self):
        source = (ROOT / "lab/astrbot_native_tool_side.py").read_text(encoding="utf-8")
        self.assertNotIn("from ligamina_astrbot", source)
        self.assertNotIn("import ligamina_astrbot", source)
        self.assertIn('name="p7_native_test"', source)

    def test_reply_projection_preserves_event_step_and_call_order(self):
        observation = {"tool_calls": {"steps": [
            [{"replies": ["first"]}, {"replies": []}],
            [{"replies": ["second", "third"]}],
        ]}}
        expected = [[ ["first"], [] ], [["second", "third"]]]
        self.assertEqual(_replies(observation), expected)
        self.assertEqual(_replies(observation["tool_calls"]), expected)

    def test_oracle_equivalence_accepts_distinct_actor_tokens_only_for_declared_patterns(self):
        cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
        case = next(case for entry in cases["entries"] for case in entry["cases"]
                    if case["case_id"] == "ab05-gallery-authority")
        pattern = case["oracle"]["calls"][0][0]["replies"][0]["pattern"]
        native = [[[
            "您(ID: tool-actor-" + "a" * 32 + ")的权限不足以使用此指令。通过 /sid 获取 ID 并请管理员添加。",
        ], [
            "您(ID: tool-actor-" + "b" * 32 + ")的权限不足以使用此指令。通过 /sid 获取 ID 并请管理员添加。",
        ]]]
        federated = [[[
            "您(ID: tool-actor-" + "c" * 32 + ")的权限不足以使用此指令。通过 /sid 获取 ID 并请管理员添加。",
        ], [
            "您(ID: tool-actor-" + "d" * 32 + ")的权限不足以使用此指令。通过 /sid 获取 ID 并请管理员添加。",
        ]]]
        self.assertEqual(len(case["oracle"]["calls"][0]), 2)
        self.assertIsNotNone(re.fullmatch(pattern, native[0][0][0]))
        self.assertTrue(_oracle_equivalent(case, native, federated))
        federated[0][1][0] = "permission denied"
        self.assertFalse(_oracle_equivalent(case, native, federated))

    def test_federated_baseline_is_bound_to_ab05_cases_and_artifact_lock(self):
        cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
        lock_path = SNAPSHOT / "artifacts.lock.json"
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        report, digest = _validate_federated_report(FEDERATED, cases, lock, lock_path)
        self.assertEqual(report["gate"]["astrbot_tool_linux"], "passed_scoped")
        self.assertEqual(report["counts"]["positions"], 8)
        self.assertEqual(report["counts"]["attempts"], 24)
        self.assertEqual(len(digest), 64)

    def test_runtime_manifest_binds_lock_and_complete_site_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            site = Path(temporary)
            (site / "astrbot.dist-info").mkdir()
            (site / "astrbot.dist-info" / "METADATA").write_text("Name: astrbot\nVersion: 4.29.1\n", encoding="utf-8")
            manifest = {
                "schema_version": 1,
                "kind": "astrbot_native_test_runtime",
                "python_version": "3.12.14",
                "astrbot_version": "4.29.1",
                "requirements_lock_path": ASTRBOT_LOCK_FILE.as_posix(),
                "requirements_lock_sha256": __import__("hashlib").sha256(ASTRBOT_LOCK_FILE.read_bytes()).hexdigest(),
                "site_packages_sha256": _tree_sha256(site),
                "distribution_count": 1,
                "plugin_code_executed": False,
            }
            (site / "runtime-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual(_runtime_manifest(site)["astrbot_version"], "4.29.1")
            (site / "astrbot.dist-info" / "METADATA").write_text("Name: astrbot\nVersion: 4.29.1\nchanged\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "manifest is invalid"):
                _runtime_manifest(site)

    def test_runtime_archive_is_bound_to_manifest_and_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            site = root / "site-packages"
            metadata = site / "astrbot-4.29.1.dist-info"
            metadata.mkdir(parents=True)
            (metadata / "METADATA").write_text("Name: astrbot\nVersion: 4.29.1\n", encoding="utf-8")
            manifest = {
                "schema_version": 1,
                "kind": "astrbot_native_test_runtime",
                "python_version": "3.12.14",
                "astrbot_version": "4.29.1",
                "requirements_lock_path": ASTRBOT_LOCK_FILE.relative_to(ROOT).as_posix(),
                "requirements_lock_sha256": __import__("hashlib").sha256(ASTRBOT_LOCK_FILE.read_bytes()).hexdigest(),
                "site_packages_sha256": _tree_sha256(site),
                "distribution_count": 1,
                "plugin_code_executed": False,
            }
            (site / "runtime-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            archive = root / "runtime.tar.gz"
            write_runtime_archive(site, archive)
            observed, archive_sha256, manifest_sha256 = _runtime_archive_manifest(archive)
            self.assertEqual(observed["site_packages_sha256"], manifest["site_packages_sha256"])
            self.assertEqual(len(archive_sha256), 64)
            self.assertEqual(len(manifest_sha256), 64)
            metadata.joinpath("METADATA").write_text("Name: astrbot\nVersion: 9.9.9\n", encoding="utf-8")
            broken = root / "broken.tar.gz"
            write_runtime_archive(site, broken)
            with self.assertRaisesRegex(ValueError, "manifest, package metadata"):
                _runtime_archive_manifest(broken)


if __name__ == "__main__":
    unittest.main()
