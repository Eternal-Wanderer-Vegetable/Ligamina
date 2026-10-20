"""Docker backend with a read-only artifact mount and no host networking."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import BinaryIO

from .base import WorkerResult, WorkerSpec, digest_bytes, ensure_external_directory

_PINNED_IMAGE = re.compile(r"^[^\s@]+@sha256:[a-f0-9]{64}$")


class _BoundedCapture:
    def __init__(self, stream: BinaryIO, limit: int, overflow: threading.Event) -> None:
        self.stream = stream
        self.limit = limit
        self.overflow = overflow
        self.buffer = bytearray()
        self.total = 0

    @property
    def truncated(self) -> bool:
        return self.total > self.limit

    def drain(self) -> None:
        while True:
            chunk = self.stream.read(65536)
            if not chunk:
                return
            self.total += len(chunk)
            if self.total > self.limit:
                self.overflow.set()
            remaining = self.limit - len(self.buffer)
            if remaining > 0:
                self.buffer.extend(chunk[:remaining])


class DockerWorker:
    def __init__(self, *, image_digest: str, repository_root: Path) -> None:
        if not _PINNED_IMAGE.fullmatch(image_digest):
            raise ValueError("worker image must be pinned by sha256 digest")
        self.image_digest = image_digest
        self.repository_root = repository_root.resolve(strict=True)
        self.docker = shutil.which("docker") or shutil.which("docker.exe")
        if not self.docker:
            raise RuntimeError("Docker CLI is unavailable")

    def command(self, spec: WorkerSpec, *, worker_id: str) -> list[str]:
        artifact_dir = ensure_external_directory(spec.input_dir, forbidden_root=self.repository_root)
        if "," in str(artifact_dir):
            raise ValueError("Docker bind mount paths cannot contain commas")
        limits = spec.limits
        return [
            self.docker,
            "run",
            "--pull=never",
            "--name",
            worker_id,
            *self._resource_labels(spec),
            "--init",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--pids-limit",
            str(limits.process_limit),
            "--memory",
            f"{limits.memory_mb}m",
            "--memory-swap",
            f"{limits.memory_mb}m",
            "--cpus",
            str(limits.cpu_cores),
            "--user",
            "65532:65532",
            "--workdir",
            "/work",
            "--tmpfs",
            f"/work:rw,exec,nosuid,nodev,size={limits.memory_mb // 2}m,uid=65532,gid=65532,mode=0700",
            "--tmpfs",
            f"/tmp:rw,exec,nosuid,nodev,size={min(limits.memory_mb // 4, 1024)}m,uid=65532,gid=65532,mode=0700",
            "--mount",
            f"type=bind,src={artifact_dir},dst=/input,readonly",
            "--env",
            "HOME=/work",
            "--env",
            "TMPDIR=/tmp",
            "--env",
            "PATH=/usr/local/bin:/usr/bin:/bin",
            "--env",
            "LANG=C.UTF-8",
            self.image_digest,
            *spec.command,
        ]

    @staticmethod
    def _resource_labels(spec: WorkerSpec) -> list[str]:
        if spec.attempt_id is None:
            return []
        labels = {
            "io.ligamina.campaign": spec.run_id,
            "io.ligamina.job": spec.job_id or spec.entry_id,
            "io.ligamina.attempt": spec.attempt_id,
            "io.ligamina.generation": str(spec.generation),
        }
        return [argument for key, value in labels.items() for argument in ("--label", f"{key}={value}")]

    def run(self, spec: WorkerSpec) -> WorkerResult:
        worker_id = f"ligamina-{uuid.uuid4().hex[:16]}"
        command = self.command(spec, worker_id=worker_id)
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "SYSTEMDRIVE": os.environ.get("SYSTEMDRIVE", ""),
                    "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
                    "WINDIR": os.environ.get("WINDIR", ""),
                },
            )
        except OSError:
            return self._result(
                status="unknown",
                reason_code="worker_backend_unavailable",
                exit_code=None,
                started=started,
                stdout=b"",
                stderr=b"",
                truncated=False,
                cleanup_confirmed=False,
                worker_id=worker_id,
            )
        assert process.stdout is not None and process.stderr is not None
        overflow = threading.Event()
        stdout = _BoundedCapture(process.stdout, spec.limits.output_limit_bytes, overflow)
        stderr = _BoundedCapture(process.stderr, spec.limits.output_limit_bytes, overflow)
        threads = [
            threading.Thread(target=stdout.drain, daemon=True),
            threading.Thread(target=stderr.drain, daemon=True),
        ]
        for thread in threads:
            thread.start()

        stats_stop = threading.Event()
        peak_memory: list[int | None] = [None]

        def sample_memory() -> None:
            while not stats_stop.is_set():
                usage = self._container_memory_bytes(worker_id)
                if usage is not None:
                    peak_memory[0] = max(peak_memory[0] or 0, usage)
                stats_stop.wait(0.25)

        stats_thread = threading.Thread(target=sample_memory, daemon=True)
        stats_thread.start()

        timed_out = False
        output_overflow = False
        launch_error = False
        exit_code: int | None = None
        oom_killed: bool | None = None
        deadline = started + spec.limits.timeout_seconds
        while True:
            if overflow.is_set():
                output_overflow = True
                self._docker_cleanup("kill", worker_id)
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                self._docker_cleanup("kill", worker_id)
                break
            try:
                exit_code = process.wait(timeout=min(0.2, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
            except OSError:
                launch_error = True
                exit_code = None
                break

        try:
            if process.poll() is None:
                try:
                    exit_code = process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    exit_code = process.wait(timeout=5)
        except OSError:
            launch_error = True
            exit_code = None

        if exit_code == 137:
            oom_killed = self._container_oom_killed(worker_id)

        if timed_out or exit_code != 0:
            self._docker_cleanup("kill", worker_id)
        stats_stop.set()
        stats_thread.join(timeout=3)
        self._docker_cleanup("rm", "--force", worker_id)
        cleanup_confirmed = self._container_gone(worker_id)
        for thread in threads:
            thread.join(timeout=5)
        truncated = stdout.truncated or stderr.truncated
        if timed_out:
            status, reason_code = "unknown", "worker_timeout"
        elif launch_error:
            status, reason_code = "unknown", "worker_launch_failed"
        elif exit_code == 125:
            status, reason_code = "unknown", "worker_backend_unavailable"
        elif exit_code in (126, 127):
            status, reason_code = "unknown", "worker_command_unavailable"
        elif output_overflow or truncated:
            status, reason_code = "unknown", "worker_output_truncated"
        elif exit_code == 137 and oom_killed is False:
            status, reason_code = "unknown", "worker_lost"
        elif exit_code == 137:
            status, reason_code = "unknown", "worker_resource_limit"
        elif exit_code == 0:
            status, reason_code = "passed", None
        else:
            status, reason_code = "failed", "worker_nonzero_exit"
        if not cleanup_confirmed:
            status, reason_code = "unknown", "worker_cleanup_unconfirmed"
        out = bytes(stdout.buffer)
        err = bytes(stderr.buffer)
        return WorkerResult(
            status=status,
            reason_code=reason_code,
            exit_code=exit_code,
            elapsed_seconds=time.monotonic() - started,
            stdout=out,
            stderr=err,
            stdout_sha256=digest_bytes(out),
            stderr_sha256=digest_bytes(err),
            output_truncated=truncated,
            cleanup_confirmed=cleanup_confirmed,
            worker_id=worker_id,
            peak_memory_bytes=peak_memory[0],
        )

    @staticmethod
    def _result(
        *,
        status: str,
        reason_code: str | None,
        exit_code: int | None,
        started: float,
        stdout: bytes,
        stderr: bytes,
        truncated: bool,
        cleanup_confirmed: bool,
        worker_id: str,
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
            output_truncated=truncated,
            cleanup_confirmed=cleanup_confirmed,
            worker_id=worker_id,
        )

    def _container_gone(self, worker_id: str) -> bool:
        try:
            result = subprocess.run(
                [self.docker, "ps", "--all", "--quiet", "--filter", f"name=^{worker_id}$"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "SYSTEMDRIVE": os.environ.get("SYSTEMDRIVE", ""),
                    "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
                    "WINDIR": os.environ.get("WINDIR", ""),
                },
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0 and not result.stdout.strip()

    def _container_oom_killed(self, worker_id: str) -> bool | None:
        try:
            result = subprocess.run(
                [self.docker, "inspect", "--format", "{{.State.OOMKilled}}", worker_id],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "SYSTEMDRIVE": os.environ.get("SYSTEMDRIVE", ""),
                    "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
                    "WINDIR": os.environ.get("WINDIR", ""),
                },
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        output = result.stdout.decode(errors="replace").strip().casefold()
        if output == "true":
            return True
        if output == "false":
            return False
        return None

    def _container_memory_bytes(self, worker_id: str) -> int | None:
        try:
            result = subprocess.run(
                [self.docker, "stats", "--no-stream", "--format", "{{.MemUsage}}", worker_id],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=2,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "SYSTEMDRIVE": os.environ.get("SYSTEMDRIVE", ""),
                    "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
                    "WINDIR": os.environ.get("WINDIR", ""),
                },
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        text = result.stdout.decode("utf-8", errors="replace").strip().split("/", 1)[0].strip()
        match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*([kmgt]?i?b)", text, re.IGNORECASE)
        if not match:
            return None
        value = float(match.group(1))
        unit = match.group(2).casefold()
        scale = {
            "b": 1,
            "kb": 1000,
            "kib": 1024,
            "mb": 1000**2,
            "mib": 1024**2,
            "gb": 1000**3,
            "gib": 1024**3,
            "tb": 1000**4,
            "tib": 1024**4,
        }[unit]
        return int(value * scale)

    def _docker_cleanup(self, *arguments: str) -> None:
        try:
            subprocess.run(
                [self.docker, *arguments],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
