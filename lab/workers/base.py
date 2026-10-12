"""Shared worker request, limits, and result types."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

WorkerStatus = Literal["passed", "failed", "unknown"]


@dataclass(frozen=True)
class WorkerLimits:
    timeout_seconds: int = 120
    memory_mb: int = 4096
    cpu_cores: float = 2.0
    process_limit: int = 256
    output_limit_bytes: int = 4 * 1024 * 1024

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.memory_mb < 256:
            raise ValueError("memory_mb must be at least 256")
        if self.cpu_cores <= 0:
            raise ValueError("cpu_cores must be positive")
        if self.process_limit < 1:
            raise ValueError("process_limit must be positive")
        if self.output_limit_bytes < 1024:
            raise ValueError("output_limit_bytes must be at least 1024")


@dataclass(frozen=True)
class WorkerSpec:
    run_id: str
    entry_id: str
    command: tuple[str, ...]
    input_dir: Path
    limits: WorkerLimits = WorkerLimits()

    def __post_init__(self) -> None:
        if not self.run_id or not self.entry_id:
            raise ValueError("run_id and entry_id are required")
        if not self.command or any(not isinstance(arg, str) for arg in self.command):
            raise ValueError("command must be a non-empty string argument list")
        if not self.input_dir.is_dir():
            raise ValueError("input_dir must be an existing directory")


@dataclass(frozen=True)
class WorkerResult:
    status: WorkerStatus
    reason_code: str | None
    exit_code: int | None
    elapsed_seconds: float
    stdout: bytes
    stderr: bytes
    stdout_sha256: str
    stderr_sha256: str
    output_truncated: bool
    cleanup_confirmed: bool
    worker_id: str


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def ensure_external_directory(path: Path, *, forbidden_root: Path) -> Path:
    """Resolve a directory and reject repository paths from worker mappings."""
    requested = path.expanduser().absolute()
    current = requested
    while current != current.parent:
        if current.is_symlink() or current.is_junction():
            raise ValueError(f"worker input path must not traverse a link: {current}")
        current = current.parent
    resolved = path.expanduser().resolve(strict=True)
    root = forbidden_root.expanduser().resolve(strict=True)
    if resolved == root or root in resolved.parents:
        raise ValueError(f"worker input must not be the repository or a child of it: {resolved}")
    if resolved.is_symlink() or resolved.is_junction():
        raise ValueError(f"worker input must not be a link: {resolved}")
    for child in resolved.rglob("*"):
        if child.is_symlink() or child.is_junction():
            raise ValueError(f"worker input tree must not contain links: {child}")
    return resolved
