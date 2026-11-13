from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

from lab.native_runner import _harness_environment

ROOT = Path(__file__).resolve().parents[2]
PRELOAD = ROOT / "lab/baselines/seed_node_random.cjs"
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "Node.js is required for P7 Koishi Worker tests")
class NodeDeterminismTests(unittest.TestCase):
    def run_node(self, source: str, *, fixed_time: str | None) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["NODE_OPTIONS"] = f"--require={PRELOAD}"
        env["LIGAMINA_BEHAVIOR_RANDOM_SEED"] = "20261110"
        if fixed_time is None:
            env.pop("LIGAMINA_BEHAVIOR_FIXED_TIME_UTC", None)
        else:
            env["LIGAMINA_BEHAVIOR_FIXED_TIME_UTC"] = fixed_time
        return subprocess.run([NODE, "-e", source], capture_output=True, text=True, env=env, check=False)

    def test_fixed_clock_preserves_explicit_dates_and_controls_current_time(self) -> None:
        result = self.run_node(
            "console.log(JSON.stringify({now: Date.now(), current: new Date().toISOString(), "
            "explicit: new Date(0).toISOString(), callable: Date()}))",
            fixed_time="2026-10-26T00:00:00Z",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["now"], 1792972800000)
        self.assertEqual(value["current"], "2026-10-26T00:00:00.000Z")
        self.assertEqual(value["explicit"], "1970-01-01T00:00:00.000Z")
        self.assertTrue(value["callable"].startswith("Mon Oct 26 2026"))

    def test_invalid_fixed_clock_fails_closed(self) -> None:
        result = self.run_node("console.log('should not run')", fixed_time="not-a-time")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("LIGAMINA_BEHAVIOR_FIXED_TIME_UTC", result.stderr)

    def test_harness_environment_binds_the_scenario_fixed_clock(self) -> None:
        case = {
            "context": {
                "execution": {"random_seed": 7, "fixed_time_utc": "2026-10-26T00:00:00Z"},
                "sample": {"ecosystem": "koishi"},
            }
        }

        environment = _harness_environment("native", case)

        self.assertEqual(environment["LIGAMINA_BEHAVIOR_FIXED_TIME_UTC"], "2026-10-26T00:00:00Z")
        self.assertIn("seed_node_random.cjs", environment["NODE_OPTIONS"])


if __name__ == "__main__":
    unittest.main()
