from __future__ import annotations

import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from lab.automation.health import collect_linux_health, require_current_admission
from lab.automation.selection import AutomationContractError


class LinuxAdmissionTests(unittest.TestCase):
    @patch("lab.automation.health.platform.system", return_value="Linux")
    @patch("lab.automation.health.shutil.which", return_value=None)
    def test_missing_docker_backend_blocks_claims(self, _which, _system) -> None:
        report = collect_linux_health()
        self.assertEqual(report["status"], "blocked")
        self.assertIn("docker_cli_unavailable", report["reason_codes"])
        self.assertIn("benign_probe_report_missing", report["reason_codes"])
        self.assertFalse(report["capabilities"]["claim_allowed"])
        with self.assertRaisesRegex(AutomationContractError, "no successful admission"):
            require_current_admission(report, report["environment_fingerprint"])

    @patch("lab.automation.health.platform.system", return_value="Linux")
    @patch("lab.automation.health.shutil.which", return_value=None)
    def test_probe_report_does_not_pass_when_cleanup_is_unconfirmed(self, _which, _system) -> None:
        with tempfile.TemporaryDirectory(prefix="p3-health-test-") as directory:
            path = Path(directory) / "probe.json"
            path.write_text(json.dumps({"passed": True, "worker_image": "python@sha256:" + "a" * 64, "results": [{"passed": True, "cleanup_confirmed": False}]}), encoding="utf-8")
            report = collect_linux_health(probe_report_path=path)
        self.assertEqual(report["facts"]["probe"]["status"], "failed")
        self.assertIn("benign_probe_report_not_accepted", report["reason_codes"])

    def test_admission_fingerprint_must_match_current_environment(self) -> None:
        report = {"status": "admitted", "capabilities": {"claim_allowed": True}, "environment_fingerprint": "a" * 64}
        require_current_admission(report, "a" * 64)
        with self.assertRaisesRegex(AutomationContractError, "environment fingerprint changed"):
            require_current_admission(report, "b" * 64)


if __name__ == "__main__":
    unittest.main()
