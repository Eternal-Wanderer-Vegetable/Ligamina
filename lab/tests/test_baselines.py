from __future__ import annotations

import unittest
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class StockBaselineSourceTests(unittest.TestCase):
    def test_s205_worker_closure_includes_recipe_helpers(self) -> None:
        from lab.baselines.run_s205 import NATIVE_FILES

        self.assertIn("recipes.py", NATIVE_FILES)

    def test_recipe_helpers_import_without_lab_schema_dependencies(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-S", "-c", "import sys; sys.path.insert(0, 'lab'); import recipes"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

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
