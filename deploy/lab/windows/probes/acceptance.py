from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPOSITORY_ROOT))

from lab.workers import WorkerLimits, WorkerSpec  # noqa: E402
from lab.workers.windows import WindowsSandboxWorker  # noqa: E402

REPORT_PATH = REPOSITORY_ROOT / "outputs" / "acceptance" / "phase2-workers-windows-20261012.json"
CLI_PATH = shutil.which("wsb.exe")


class CapturingWorker(WindowsSandboxWorker):
    sandbox_id: str | None = None

    def _call_cli(self, arguments: list[str], *, timeout: float):
        if arguments and arguments[0] == "start":
            self.sandbox_id = arguments[arguments.index("--id") + 1]
        return super()._call_cli(arguments, timeout=timeout)


def run_result(
    name: str,
    worker: WindowsSandboxWorker,
    spec: WorkerSpec,
    expected_status: str,
    expected_reason: str | None,
    *,
    required_stdout: str | None = None,
    empty_stderr: bool = False,
) -> dict[str, Any]:
    result = worker.run(spec)
    record = {
        "name": name,
        "limits": {
            "timeout_seconds": spec.limits.timeout_seconds,
            "memory_mb": spec.limits.memory_mb,
            "cpu_cores": spec.limits.cpu_cores,
            "process_limit": spec.limits.process_limit,
            "output_limit_bytes": spec.limits.output_limit_bytes,
        },
        "status": result.status,
        "reason_code": result.reason_code,
        "exit_code": result.exit_code,
        "elapsed_seconds": round(result.elapsed_seconds, 3),
        "stdout": result.stdout.decode("mbcs", errors="replace"),
        "stderr": result.stderr.decode("mbcs", errors="replace"),
        "stdout_sha256": result.stdout_sha256,
        "stderr_sha256": result.stderr_sha256,
        "output_truncated": result.output_truncated,
        "cleanup_confirmed": result.cleanup_confirmed,
        "worker_id": result.worker_id,
        "expected": {"status": expected_status, "reason_code": expected_reason},
    }
    record["passed"] = (
        result.status == expected_status
        and result.reason_code == expected_reason
        and result.cleanup_confirmed
        and (required_stdout is None or required_stdout in record["stdout"])
        and (not empty_stderr or not record["stderr"])
    )
    return record


def main() -> int:
    if not CLI_PATH:
        raise RuntimeError("Windows Sandbox CLI is unavailable")
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="ligamina-windows-probes-") as temp_root_text:
        temp_root = Path(temp_root_text)
        artifact_dir = temp_root / "artifact"
        artifact_dir.mkdir()
        shutil.copy2(
            REPOSITORY_ROOT / "deploy" / "lab" / "windows" / "probes" / "access-boundaries.cmd",
            artifact_dir / "access-boundaries.cmd",
        )
        worker = WindowsSandboxWorker(repository_root=REPOSITORY_ROOT, temporary_root=temp_root)

        previous_secret = os.environ.get("LIGAMINA_HOST_SECRET_TEST")
        os.environ["LIGAMINA_HOST_SECRET_TEST"] = "must-not-enter-sandbox"
        try:
            results.append(
                run_result(
                    "filesystem-network-and-secret-boundaries",
                    worker,
                    WorkerSpec(
                        "s2-04-windows-boundary",
                        "windows-boundary",
                        ("cmd.exe", "/d", "/c", r"C:\Ligamina\Input\access-boundaries.cmd"),
                        artifact_dir,
                        WorkerLimits(timeout_seconds=20, memory_mb=2048, cpu_cores=1, process_limit=32),
                    ),
                    "passed",
                    None,
                )
            )
        finally:
            if previous_secret is None:
                os.environ.pop("LIGAMINA_HOST_SECRET_TEST", None)
            else:
                os.environ["LIGAMINA_HOST_SECRET_TEST"] = previous_secret

        tree_command = r'start /b C:\Windows\System32\ping.exe -n 60 127.0.0.1 > nul & echo tree-launched'
        results.append(
            run_result(
                "process-tree-cleanup",
                worker,
                WorkerSpec(
                    "s2-04-windows-tree",
                    "windows-process-tree",
                    ("cmd.exe", "/d", "/c", tree_command),
                    artifact_dir,
                    WorkerLimits(timeout_seconds=20, memory_mb=2048, cpu_cores=1, process_limit=16),
                ),
                "passed",
                None,
                required_stdout="tree-launched",
                empty_stderr=True,
            )
        )
        results.append(
            run_result(
                "nonzero-exit",
                worker,
                WorkerSpec(
                    "s2-04-windows-nonzero",
                    "windows-nonzero",
                    ("cmd.exe", "/d", "/c", "exit /b 17"),
                    artifact_dir,
                    WorkerLimits(timeout_seconds=20, memory_mb=2048, cpu_cores=1, process_limit=16),
                ),
                "failed",
                "worker_nonzero_exit",
            )
        )
        results.append(
            run_result(
                "sibling-after-failure",
                worker,
                WorkerSpec(
                    "s2-04-windows-sibling",
                    "windows-sibling",
                    ("cmd.exe", "/d", "/c", "echo sibling-ok"),
                    artifact_dir,
                    WorkerLimits(timeout_seconds=20, memory_mb=2048, cpu_cores=1, process_limit=16),
                ),
                "passed",
                None,
            )
        )
        results.append(
            run_result(
                "timeout",
                worker,
                WorkerSpec(
                    "s2-04-windows-timeout",
                    "windows-timeout",
                    ("cmd.exe", "/d", "/c", "ping.exe -n 30 127.0.0.1 > nul"),
                    artifact_dir,
                    WorkerLimits(timeout_seconds=3, memory_mb=2048, cpu_cores=1, process_limit=16),
                ),
                "unknown",
                "worker_timeout",
            )
        )
        overflow_command = "for /L %i in (1,1,20000) do @echo ligamina-output-overflow-probe"
        results.append(
            run_result(
                "output-overflow",
                worker,
                WorkerSpec(
                    "s2-04-windows-overflow",
                    "windows-overflow",
                    ("cmd.exe", "/d", "/c", overflow_command),
                    artifact_dir,
                    WorkerLimits(timeout_seconds=30, memory_mb=2048, cpu_cores=1, process_limit=16, output_limit_bytes=1024),
                ),
                "unknown",
                "worker_output_truncated",
            )
        )

        lost_worker = CapturingWorker(repository_root=REPOSITORY_ROOT, temporary_root=temp_root)
        lost_spec = WorkerSpec(
            "s2-04-windows-worker-loss",
            "windows-worker-loss",
            ("cmd.exe", "/d", "/c", "ping.exe -n 60 127.0.0.1 > nul"),
            artifact_dir,
            WorkerLimits(timeout_seconds=90, memory_mb=2048, cpu_cores=1, process_limit=16),
        )
        lost_records: list[dict[str, Any]] = []
        thread = threading.Thread(
            target=lambda: lost_records.append(run_result("worker-loss", lost_worker, lost_spec, "unknown", "worker_lost")),
            daemon=True,
        )
        thread.start()
        deadline = time.monotonic() + 180
        while lost_worker.sandbox_id is None and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.1)
        if lost_worker.sandbox_id is None:
            thread.join(timeout=180)
            raise RuntimeError("Windows Sandbox did not start")
        time.sleep(12)
        lost_worker._stop_sandbox(lost_worker.sandbox_id)
        thread.join(timeout=120)
        if thread.is_alive() or not lost_records:
            raise RuntimeError("worker did not return after sandbox loss")
        results.append(lost_records[0])

    report = {
        "schema_version": 1,
        "date": "2026-10-12",
        "backend": "Windows Sandbox CLI",
        "cli_path": CLI_PATH,
        "host_platform": platform.platform(),
        "results": results,
        "passed": all(result["passed"] for result in results),
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(REPORT_PATH), "passed": report["passed"], "results": results}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
