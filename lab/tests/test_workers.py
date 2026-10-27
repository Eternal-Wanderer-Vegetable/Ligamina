from __future__ import annotations

import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from lab.workers import DockerWorker, WorkerLimits, WorkerSpec
from lab.workers.windows import WindowsSandboxWorker, build_wsb_config


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
IMAGE_DIGEST = "registry.example/ligamina/worker@sha256:" + "a" * 64


class WorkerPolicyTests(unittest.TestCase):
    def docker_worker(self) -> DockerWorker:
        # These are policy unit tests; real backend availability is covered by admission probes.
        with patch("lab.workers.linux.shutil.which", return_value="/usr/bin/docker"):
            return DockerWorker(image_digest=IMAGE_DIGEST, repository_root=REPOSITORY_ROOT)

    def test_limits_reject_non_positive_and_unbounded_values(self) -> None:
        with self.assertRaises(ValueError):
            WorkerLimits(timeout_seconds=0)
        with self.assertRaises(ValueError):
            WorkerLimits(memory_mb=128)
        with self.assertRaises(ValueError):
            WorkerLimits(process_limit=0)

    def test_docker_command_is_digest_pinned_and_has_closed_boundaries(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ligamina-worker-input-") as directory:
            input_dir = Path(directory)
            spec = WorkerSpec("run-1", "entry-1", ("python", "-c", "print('ok')"), input_dir)
            worker = self.docker_worker()
            command = worker.command(spec, worker_id="ligamina-test")
            direct = WorkerSpec(
                "run-2", "entry-2", ("/usr/local/bin/python", "-c", "print('direct')"), input_dir,
                entrypoint="/usr/bin/env",
            )
            direct_command = worker.command(direct, worker_id="ligamina-direct")

        self.assertIn("--network=none", command)
        self.assertIn("--read-only", command)
        self.assertIn("--cap-drop=ALL", command)
        self.assertIn("--pids-limit", command)
        self.assertIn("--memory", command)
        self.assertIn("--cpus", command)
        self.assertIn("--pull=never", command)
        self.assertNotIn("--rm", command)
        self.assertIn("HOME=/work", command)
        work_tmpfs = command[command.index("--tmpfs") + 1]
        self.assertIn("uid=65532,gid=65532,mode=0700", work_tmpfs)
        mount = command[command.index("--mount") + 1]
        self.assertTrue(mount.endswith(",readonly"))
        self.assertNotIn(str(REPOSITORY_ROOT), mount)
        self.assertEqual(command[-3:], ["python", "-c", "print('ok')"])

        entrypoint_index = direct_command.index("--entrypoint")
        self.assertEqual(direct_command[entrypoint_index + 1], "/usr/bin/env")
        self.assertEqual(direct_command[-3:], ["/usr/local/bin/python", "-c", "print('direct')"])

    def test_docker_backend_rejects_unpinned_image_and_repository_input(self) -> None:
        with self.assertRaises(ValueError):
            DockerWorker(image_digest="registry.example/ligamina/worker:latest", repository_root=REPOSITORY_ROOT)
        worker = self.docker_worker()
        spec = WorkerSpec("run-1", "entry-1", ("echo", "x"), REPOSITORY_ROOT / "lab")
        with self.assertRaisesRegex(ValueError, "must not be the repository"):
            worker.command(spec, worker_id="ligamina-test")

    def test_docker_worker_parses_peak_memory_sample(self) -> None:
        worker = self.docker_worker()
        completed = CompletedProcess([], 0, stdout=b"14.25MiB / 4GiB\n", stderr=b"")
        with patch("lab.workers.linux.subprocess.run", return_value=completed):
            self.assertEqual(worker._container_memory_bytes("container"), int(14.25 * 1024 * 1024))

    def test_windows_sandbox_disables_host_channels_and_limits_memory(self) -> None:
        config = ET.fromstring(build_wsb_config(memory_mb=2048))

        settings = {child.tag: child.text for child in config}
        self.assertEqual(settings["Networking"], "Disabled")
        self.assertEqual(settings["VGpu"], "Disable")
        self.assertEqual(settings["ClipboardRedirection"], "Disable")
        self.assertEqual(settings["ProtectedClient"], "Enable")
        self.assertEqual(settings["MemoryInMB"], "2048")
        self.assertIsNone(config.find("MappedFolders"))
        self.assertIsNone(config.find("LogonCommand"))

    def test_windows_worker_marks_existing_workspace_children_low_integrity(self) -> None:
        source = (REPOSITORY_ROOT / "deploy/lab/windows/runner.ps1").read_text(encoding="utf-8")
        self.assertIn("$workspaceHome = Join-Path $spec.working_directory 'Home'", source)
        self.assertIn("/setintegritylevel Low /T", source)
        self.assertIn("QueryInformationJobObject", source)
        self.assertIn("PeakJobMemoryUsed", source)

    def test_windows_worker_result_preserves_job_peak_memory(self) -> None:
        result = WindowsSandboxWorker._make_result(
            status="passed", reason_code=None, exit_code=0, started=0,
            stdout=b"", stderr=b"", output_truncated=False,
            cleanup_confirmed=True, worker_id="worker-test", peak_memory_bytes=123456,
        )
        self.assertEqual(result.peak_memory_bytes, 123456)

    def test_windows_cli_keeps_system_drive_and_drops_unlisted_environment(self) -> None:
        with patch.dict(
            "os.environ",
            {"SYSTEMDRIVE": "C:", "SYSTEMROOT": r"C:\Windows", "LIGAMINA_HOST_SECRET": "hidden"},
            clear=True,
        ):
            environment = WindowsSandboxWorker._cli_env()

        self.assertEqual(environment["SYSTEMDRIVE"], "C:")
        self.assertEqual(environment["SYSTEMROOT"], r"C:\Windows")
        self.assertNotIn("LIGAMINA_HOST_SECRET", environment)


if __name__ == "__main__":
    unittest.main()
