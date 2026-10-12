from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class StockBaselineSourceTests(unittest.TestCase):
    def test_both_native_host_profiles_are_present(self) -> None:
        profiles = ROOT / "samples/profiles"
        self.assertTrue((profiles / "nonebot-group-v0.1.json").is_file())
        self.assertTrue((profiles / "koishi-group-v0.1.json").is_file())

    def test_native_baseline_sources_do_not_import_ligamina_bridge(self) -> None:
        nonebot = (ROOT / "lab/baselines/nonebot/runner.py").read_text(encoding="utf-8")
        koishi = (ROOT / "lab/baselines/koishi/runner.mjs").read_text(encoding="utf-8")
        self.assertNotIn("from ligamina_nonebot", nonebot)
        self.assertNotIn("BridgeAdapter", nonebot)
        self.assertNotIn("from './adapter", koishi)
        self.assertNotIn("BridgeAdapter", koishi)

    def test_worker_bootstrap_uses_read_only_input_and_writable_workspace(self) -> None:
        bootstrap = (ROOT / "lab/baselines/windows/bootstrap.ps1").read_text(encoding="utf-8")
        self.assertIn("C:\\Ligamina\\Input\\project", bootstrap)
        self.assertIn("C:\\Ligamina\\Workspace\\project", bootstrap)
        self.assertIn("paired_worker.py", bootstrap)


if __name__ == "__main__":
    unittest.main()
