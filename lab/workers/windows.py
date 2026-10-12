"""Windows Sandbox CLI backend with delayed, trusted output collection."""

from __future__ import annotations

import csv
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from .base import WorkerResult, WorkerSpec, digest_bytes, ensure_external_directory

_SANDBOX_ROOT = Path(r"C:\Ligamina")
_INTERNAL_ROOT = Path(r"C:\ProgramData\Ligamina")
_INTERNAL_OUTPUT = _INTERNAL_ROOT / "InternalOutput"
_OUTBOUND = _INTERNAL_ROOT / "Outbound"
_SANDBOX_VM_PROCESS_NAMES = {"vmmemwindowssandbox.exe", "vmwp.exe"}
_SANDBOX_RUN_LOCK = threading.Lock()


def build_wsb_config(*, memory_mb: int) -> str:
    """Create a Windows Sandbox CLI policy without host folders mounted at start."""
    if memory_mb < 256:
        raise ValueError("memory_mb must be at least 256")
    root = ET.Element("Configuration")
    for tag, value in (
        ("VGpu", "Disable"),
        ("Networking", "Disabled"),
        ("ClipboardRedirection", "Disable"),
        ("PrinterRedirection", "Disable"),
        ("AudioInput", "Disable"),
        ("VideoInput", "Disable"),
        ("ProtectedClient", "Enable"),
        ("MemoryInMB", str(memory_mb)),
    ):
        ET.SubElement(root, tag).text = value
    return ET.tostring(root, encoding="unicode", xml_declaration=True)


class WindowsSandboxWorker:
    def __init__(
        self,
        *,
        repository_root: Path,
        wsb_executable: str | None = None,
        temporary_root: Path | None = None,
        poll_interval: float = 0.1,
        runner_script_path: Path | None = None,
        collect_script_path: Path | None = None,
    ) -> None:
        self.repository_root = repository_root.resolve(strict=True)
        self.wsb_executable = wsb_executable or shutil.which("wsb.exe")
        if not self.wsb_executable:
            raise RuntimeError("Windows Sandbox CLI (wsb.exe) is unavailable")
        self.temporary_root = (
            ensure_external_directory(temporary_root, forbidden_root=self.repository_root)
            if temporary_root is not None
            else Path(tempfile.gettempdir()).resolve(strict=True)
        )
        self.poll_interval = poll_interval
        deploy_root = self.repository_root / "deploy" / "lab" / "windows"
        self.runner_script_path = runner_script_path or deploy_root / "runner.ps1"
        self.collect_script_path = collect_script_path or deploy_root / "collect-output.ps1"

    def run(self, spec: WorkerSpec) -> WorkerResult:
        artifact_dir = ensure_external_directory(spec.input_dir, forbidden_root=self.repository_root)
        with _SANDBOX_RUN_LOCK:
            return self._run_serialized(spec, artifact_dir)

    def _run_serialized(self, spec: WorkerSpec, artifact_dir: Path) -> WorkerResult:
        run_root = Path(tempfile.mkdtemp(prefix="ligamina-worker-", dir=self.temporary_root))
        control_dir = run_root / "control"
        output_dir = run_root / "output"
        control_dir.mkdir()
        output_dir.mkdir()
        worker_id = f"wsb-{uuid.uuid4().hex[:16]}"
        sandbox_id = str(uuid.uuid4())
        spec_path = control_dir / "worker.json"
        runner_path = control_dir / "runner.ps1"
        collect_path = control_dir / "collect-output.ps1"
        cli_stdout_path = run_root / "wsb.stdout"
        cli_stderr_path = run_root / "wsb.stderr"
        result_path = output_dir / "result.json"
        stdout_path = output_dir / "stdout.bin"
        stderr_path = output_dir / "stderr.bin"
        started = time.monotonic()
        baseline_processes = self._sandbox_vm_processes()
        start_attempted = False
        timed_out = False
        output_truncated = False
        status = "unknown"
        reason_code: str | None = "worker_launch_failed"
        exit_code: int | None = None
        result_doc: dict[str, Any] | None = None
        peak_memory_bytes: int | None = None
        stdout = b""
        stderr = b""
        cleanup_confirmed = False

        if baseline_processes is None:
            shutil.rmtree(run_root, ignore_errors=True)
            return self._make_result(
                status=status,
                reason_code="worker_process_inspection_unavailable",
                exit_code=None,
                started=started,
                stdout=stdout,
                stderr=stderr,
                output_truncated=False,
                cleanup_confirmed=True,
                worker_id=worker_id,
            )

        try:
            spec_path.write_text(
                json.dumps(
                    {
                        "command": list(spec.command),
                        "timeout_seconds": spec.limits.timeout_seconds,
                        "memory_mb": spec.limits.memory_mb,
                        "cpu_cores": spec.limits.cpu_cores,
                        "process_limit": spec.limits.process_limit,
                        "output_limit_bytes": spec.limits.output_limit_bytes,
                        "result_path": str(_INTERNAL_OUTPUT / "result.json"),
                        "stdout_path": str(_INTERNAL_OUTPUT / "stdout.bin"),
                        "stderr_path": str(_INTERNAL_OUTPUT / "stderr.bin"),
                        "input_path": str(_SANDBOX_ROOT / "HostInput"),
                        "guest_input_path": str(_SANDBOX_ROOT / "Input"),
                        "working_directory": str(_SANDBOX_ROOT / "Workspace"),
                        "outbound_path": str(_OUTBOUND),
                        "entry_id": spec.entry_id,
                        "run_id": spec.run_id,
                        "worker_id": worker_id,
                    },
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            runner_path.write_text(self.runner_script_path.read_text(encoding="utf-8"), encoding="utf-8")
            collect_path.write_text(self.collect_script_path.read_text(encoding="utf-8"), encoding="utf-8")

            start_attempted = True
            start = self._call_cli(
                ["start", "--id", sandbox_id, "--config", build_wsb_config(memory_mb=spec.limits.memory_mb), "--raw"],
                timeout=120,
            )
            if start.returncode != 0 or not self._start_response_matches(start.stdout, sandbox_id):
                reason_code = "worker_backend_unavailable"
                stderr = start.stderr[: spec.limits.output_limit_bytes]
                raise _StopWorker

            self._share_read_only(sandbox_id, artifact_dir, _SANDBOX_ROOT / "HostInput")
            self._share_read_only(sandbox_id, control_dir, _SANDBOX_ROOT / "Control")

            runner_command = (
                "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass "
                r"-File C:\Ligamina\Control\runner.ps1"
            )
            with cli_stdout_path.open("wb") as cli_stdout, cli_stderr_path.open("wb") as cli_stderr:
                runner_process = subprocess.Popen(
                    self._cli_argv(
                        [
                            "exec",
                            "--id",
                            sandbox_id,
                            "--command",
                            runner_command,
                            "--run-as",
                            "System",
                            "--working-directory",
                            r"C:\Windows\System32\WindowsPowerShell\v1.0",
                        ]
                    ),
                    stdin=subprocess.DEVNULL,
                    stdout=cli_stdout,
                    stderr=cli_stderr,
                    env=self._cli_env(),
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                try:
                    runner_process.wait(timeout=spec.limits.timeout_seconds + 90)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    reason_code = "worker_timeout"
                    self._stop_sandbox(sandbox_id)
                    runner_process.wait(timeout=15)

            if timed_out:
                raise _StopWorker

            if runner_process.returncode != 0:
                active_ids = self._running_sandbox_ids()
                if active_ids is None:
                    reason_code = "worker_status_unavailable"
                elif sandbox_id not in active_ids:
                    reason_code = "worker_lost"
                else:
                    reason_code = "worker_exec_failed"
                raise _StopWorker

            self._share_writable(sandbox_id, output_dir, _OUTBOUND)
            collect_command = (
                "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass "
                r"-File C:\Ligamina\Control\collect-output.ps1"
            )
            collect = self._call_cli(
                [
                    "exec",
                    "--id",
                    sandbox_id,
                    "--command",
                    collect_command,
                    "--run-as",
                    "System",
                    "--working-directory",
                    r"C:\Windows\System32\WindowsPowerShell\v1.0",
                ],
                timeout=60,
            )
            if collect.returncode != 0:
                reason_code = "sandbox_collect_failed"
                stderr = collect.stderr[: spec.limits.output_limit_bytes]
                raise _StopWorker

            result_doc = self._read_result(result_path)
            peak_value = result_doc.get("peak_memory_bytes") if isinstance(result_doc, dict) else None
            if isinstance(peak_value, int) and not isinstance(peak_value, bool) and peak_value >= 0:
                peak_memory_bytes = peak_value
            stdout = self._read_limited(stdout_path, spec.limits.output_limit_bytes)
            stderr = self._read_limited(stderr_path, spec.limits.output_limit_bytes)
            output_truncated = self._file_exceeds(stdout_path, spec.limits.output_limit_bytes) or self._file_exceeds(
                stderr_path, spec.limits.output_limit_bytes
            )
            if output_truncated or (result_doc and result_doc.get("output_truncated") is True):
                status, reason_code = "unknown", "worker_output_truncated"
                output_truncated = True
            elif result_doc is None:
                status, reason_code = "unknown", "sandbox_no_result"
            else:
                status = result_doc.get("status")
                reason_code = result_doc.get("reason_code")
                exit_code = result_doc.get("exit_code")
                if status not in {"passed", "failed", "unknown"}:
                    status, reason_code = "unknown", "sandbox_invalid_result"
                elif result_doc.get("cleanup_confirmed") is not True:
                    status, reason_code = "unknown", "worker_cleanup_unconfirmed"
        except _StopWorker:
            pass
        except subprocess.TimeoutExpired:
            timed_out = True
            status, reason_code = "unknown", "worker_timeout"
        except (OSError, ValueError, json.JSONDecodeError, TypeError):
            status, reason_code = "unknown", "worker_launch_failed"
        finally:
            if start_attempted:
                self._stop_sandbox(sandbox_id)
                cleanup_confirmed = self._wait_for_sandbox_cleanup(
                    sandbox_id,
                    baseline_processes,
                    timeout=45,
                )
            else:
                cleanup_confirmed = True
            if not cleanup_confirmed:
                status, reason_code = "unknown", "worker_cleanup_unconfirmed"
            if result_doc is None and not stderr:
                stderr = self._read_limited(cli_stderr_path, spec.limits.output_limit_bytes)
            shutil.rmtree(run_root, ignore_errors=True)

        return self._make_result(
            status=status,
            reason_code=reason_code,
            exit_code=exit_code,
            started=started,
            stdout=stdout,
            stderr=stderr,
            output_truncated=output_truncated,
            cleanup_confirmed=cleanup_confirmed,
            worker_id=worker_id,
            peak_memory_bytes=peak_memory_bytes,
        )

    def _share_read_only(self, sandbox_id: str, host_path: Path, sandbox_path: Path) -> None:
        result = self._call_cli(
            ["share", "--id", sandbox_id, "--host-path", str(host_path), "--sandbox-path", str(sandbox_path)],
            timeout=60,
        )
        if result.returncode != 0:
            raise OSError("Windows Sandbox read-only share failed")

    def _share_writable(self, sandbox_id: str, host_path: Path, sandbox_path: Path) -> None:
        result = self._call_cli(
            [
                "share",
                "--id",
                sandbox_id,
                "--host-path",
                str(host_path),
                "--sandbox-path",
                str(sandbox_path),
                "--allow-write",
            ],
            timeout=60,
        )
        if result.returncode != 0:
            raise OSError("Windows Sandbox delayed output share failed")

    def _stop_sandbox(self, sandbox_id: str) -> None:
        try:
            self._call_cli(["stop", "--id", sandbox_id], timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _wait_for_sandbox_cleanup(self, sandbox_id: str, baseline: set[int], *, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            ids = self._running_sandbox_ids()
            processes = self._sandbox_vm_processes()
            if ids is None or processes is None:
                return False
            if sandbox_id not in ids and processes.issubset(baseline):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(max(self.poll_interval, 0.5))

    def _running_sandbox_ids(self) -> set[str] | None:
        try:
            result = self._call_cli(["list", "--raw"], timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        try:
            document = json.loads(result.stdout.decode(errors="replace"))
        except json.JSONDecodeError:
            return None
        rows = document.get("WindowsSandboxEnvironments") if isinstance(document, dict) else None
        if not isinstance(rows, list):
            return None
        ids: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                return None
            identity = row.get("Id") or row.get("id")
            if not isinstance(identity, str):
                return None
            ids.add(identity)
        return ids

    def _sandbox_vm_processes(self) -> set[int] | None:
        try:
            result = subprocess.run(
                ["tasklist.exe", "/FO", "CSV", "/NH"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        pids: set[int] = set()
        for row in csv.reader(io.StringIO(result.stdout.decode(errors="replace"))):
            if len(row) < 2 or row[0].casefold() not in _SANDBOX_VM_PROCESS_NAMES:
                continue
            try:
                pids.add(int(row[1].replace(",", "")))
            except ValueError:
                return None
        return pids

    def _call_cli(self, arguments: list[str], *, timeout: float) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            self._cli_argv(arguments),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            env=self._cli_env(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def _cli_argv(self, arguments: list[str]) -> list[str]:
        return [self.wsb_executable, *arguments]

    @staticmethod
    def _cli_env() -> dict[str, str]:
        allowed = (
            "SYSTEMDRIVE",
            "SYSTEMROOT",
            "WINDIR",
            "PATH",
            "USERPROFILE",
            "LOCALAPPDATA",
            "APPDATA",
            "TEMP",
            "TMP",
        )
        return {key: os.environ[key] for key in allowed if key in os.environ}

    @staticmethod
    def _start_response_matches(stdout: bytes, sandbox_id: str) -> bool:
        try:
            document = json.loads(stdout.decode(errors="replace"))
        except json.JSONDecodeError:
            return False
        return isinstance(document, dict) and document.get("Id") == sandbox_id

    @staticmethod
    def _read_result(path: Path) -> dict[str, Any] | None:
        if not path.exists() or path.stat().st_size > 64 * 1024:
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _read_limited(path: Path, limit: int) -> bytes:
        try:
            with path.open("rb") as stream:
                return stream.read(limit + 1)[:limit]
        except OSError:
            return b""

    @staticmethod
    def _file_exceeds(path: Path, limit: int) -> bool:
        try:
            return path.stat().st_size > limit
        except OSError:
            return False

    @staticmethod
    def _make_result(
        *,
        status: str,
        reason_code: str | None,
        exit_code: int | None,
        started: float,
        stdout: bytes,
        stderr: bytes,
        output_truncated: bool,
        cleanup_confirmed: bool,
        worker_id: str,
        peak_memory_bytes: int | None = None,
    ) -> WorkerResult:
        return WorkerResult(
            status=status,  # type: ignore[arg-type]
            reason_code=reason_code,
            exit_code=exit_code,
            elapsed_seconds=time.monotonic() - started,
            stdout=stdout,
            stderr=stderr,
            stdout_sha256=digest_bytes(stdout),
            stderr_sha256=digest_bytes(stderr),
            output_truncated=output_truncated,
            cleanup_confirmed=cleanup_confirmed,
            worker_id=worker_id,
            peak_memory_bytes=peak_memory_bytes,
        )


class _StopWorker(Exception):
    pass
