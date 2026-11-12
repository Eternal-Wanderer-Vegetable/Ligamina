from __future__ import annotations

import unittest
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lab.automation.health import _linux_available_memory_bytes, collect_linux_health, require_current_admission
from lab.automation.selection import AutomationContractError
from lab.automation import __main__ as automation_cli
from deploy.lab.linux.probes.acceptance import _probe_spec


class LinuxAdmissionTests(unittest.TestCase):
    def test_benign_probe_worker_overrides_application_entrypoint(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p3-probe-spec-") as directory:
            spec = _probe_spec("probe-run", "probe-entry", ("python", "-c", "pass"), Path(directory))

        self.assertEqual(spec.entrypoint, "/usr/bin/env")

    def test_pinned_image_override_binds_probe_and_local_image_identity(self) -> None:
        worker_image = "ligamina-astrbot-runtime@sha256:" + "b" * 64
        with tempfile.TemporaryDirectory(prefix="p3-health-override-") as directory:
            probe_path = Path(directory) / "probe.json"
            probe_path.write_text(json.dumps({
                "passed": True,
                "worker_image": worker_image,
                "results": [{"passed": True, "cleanup_confirmed": True}],
            }), encoding="utf-8")
            with (
                patch("lab.automation.health.platform.system", return_value="Linux"),
                patch("lab.automation.health.shutil.which", return_value="docker"),
                patch("lab.automation.health._docker_facts", return_value={"available": True, "server_version": "29.1.3"}),
                patch("lab.automation.health.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="sha256:" + "b" * 64)),
                patch("lab.automation.health._linux_available_memory_bytes", return_value=32 * 1024**3),
                patch("lab.automation.health.shutil.disk_usage", return_value=SimpleNamespace(free=100 * 1024**3)),
            ):
                report = collect_linux_health(probe_report_path=probe_path, worker_image=worker_image)

        self.assertEqual(report["status"], "admitted")
        self.assertEqual(report["facts"]["image"]["locked_reference"], worker_image)
        self.assertEqual(report["facts"]["image"]["image_id"], "sha256:" + "b" * 64)
        self.assertEqual(report["facts"]["probe"]["worker_image"], worker_image)

    def test_worker_image_override_must_be_digest_pinned(self) -> None:
        with self.assertRaisesRegex(AutomationContractError, "digest-pinned"):
            collect_linux_health(worker_image="ligamina-astrbot-runtime:latest")

    def test_preflight_cli_passes_explicit_worker_image_to_admission(self) -> None:
        worker_image = "ligamina-astrbot-runtime@sha256:" + "c" * 64
        with tempfile.TemporaryDirectory(prefix="p3-cli-override-") as directory:
            output = Path(directory) / "admission.json"
            with patch("lab.automation.__main__.collect_linux_health", return_value={
                "status": "admitted", "reason_codes": [], "environment_fingerprint": "d" * 64,
            }) as collect:
                result = automation_cli.main([
                    "preflight", "--platform", "linux", "--worker-image", worker_image,
                    "--output", str(output),
                ])

        self.assertEqual(result, 0)
        collect.assert_called_once_with(probe_report_path=None, worker_image=worker_image)

    def test_missing_meminfo_is_reported_as_unavailable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p3-meminfo-test-") as directory:
            missing = Path(directory) / "meminfo"
            self.assertIsNone(_linux_available_memory_bytes(missing))

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
