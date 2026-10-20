from __future__ import annotations

import unittest
import subprocess
import sys
import os
import stat
import tempfile
from pathlib import Path
from unittest.mock import patch


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

    def test_linux_profile_cleanup_removes_host_owned_staging_without_docker(self) -> None:
        from lab.baselines.run_s205 import _cleanup_linux_profile

        with tempfile.TemporaryDirectory(prefix="ligamina-s205-cleanup-") as directory:
            profile = Path(directory) / ".venv"
            nested = profile / "lib/python3.12/site-packages/sample"
            nested.mkdir(parents=True)
            file_path = nested / "marker.py"
            file_path.write_text("value = 1\n", encoding="utf-8")
            nested.chmod(stat.S_IRUSR | stat.S_IXUSR)
            profile.chmod(stat.S_IRUSR | stat.S_IXUSR)
            result = _cleanup_linux_profile({"ecosystem": "nonebot"}, Path(directory), {})

        self.assertEqual(result["status"], "passed")
        self.assertIn(".venv", result["removed"])

    def test_registry_proxy_is_scoped_to_host_resolver_environment(self) -> None:
        from lab.representative import _registry_environment

        with patch.dict("os.environ", {"HTTPS_PROXY": "http://proxy.invalid:8080"}, clear=True):
            input_environment = {"PATH": "/usr/bin"}
            resolver_environment = _registry_environment(input_environment)
        self.assertNotIn("HTTPS_PROXY", input_environment)
        self.assertEqual(resolver_environment["HTTPS_PROXY"], "http://proxy.invalid:8080")


if __name__ == "__main__":
    unittest.main()
