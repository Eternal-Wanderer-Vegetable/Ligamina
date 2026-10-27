"""Execute and validate the focused P4-01 service capability probe."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SERVICE_TEST = "native_nonebot_to_koishi_capability_call_uses_the_broker_and_current_generations"
SERVICE_SOURCES = (
    "hosts/nonebot/ligamina_nonebot/cross_host_smoke_plugin.py",
    "hosts/nonebot/ligamina_nonebot/adapter.py",
    "hosts/koishi/src/adapter.ts",
    "hosts/koishi/src/capabilities.ts",
    "crates/ligamina-core/src/broker.rs",
    "crates/ligamina-core/tests/host_session.rs",
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _version(command: list[str], *, cwd: Path = ROOT, env: dict[str, str] | None = None) -> str | None:
    try:
        result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    return (result.stdout or result.stderr).strip().splitlines()[0]


def _write_report(path: Path, report: dict[str, Any]) -> str:
    path = path.expanduser().absolute()
    if path.exists() or path.with_suffix(path.suffix + ".sha256").exists():
        raise FileExistsError(f"refusing to overwrite an existing P4 service probe report: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    path.write_bytes(data)
    digest = _sha256(data)
    path.with_suffix(path.suffix + ".sha256").write_text(f"{digest}  {path.name}\n", encoding="ascii")
    return digest


def run_service_capability_probe(output_path: Path, node_executable: Path | None = None) -> dict[str, Any]:
    node = node_executable.expanduser().resolve(strict=True) if node_executable else Path(shutil.which("node") or "node").resolve()
    env = os.environ.copy()
    if node.is_file() and node.parent.is_dir():
        env["PATH"] = str(node.parent) + os.pathsep + env.get("PATH", "")
    node_version = _version([str(node), "--version"], env=env)
    cargo_version = _version(["cargo", "--version"], env=env)
    rustc_version = _version(["rustc", "--version"], env=env)
    started = time.monotonic()
    test_result: dict[str, Any]
    if node_version != "v24.20.0" or cargo_version is None or rustc_version is None:
        test_result = {
            "status": "blocked",
            "reason": "pinned Node 24.20.0 and Rust toolchain are required for the real two-Host Core test",
            "command": ["cargo", "test", "--locked", "-p", "ligamina-core", "--test", "host_session", SERVICE_TEST, "--", "--exact", "--nocapture"],
            "exit_code": None,
        }
    else:
        command = [
            "cargo", "test", "--locked", "-p", "ligamina-core", "--test", "host_session",
            SERVICE_TEST, "--", "--exact", "--nocapture",
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=600,
                check=False,
            )
            output = completed.stdout + completed.stderr
            matched = f"test {SERVICE_TEST} ... ok" in output
            test_result = {
                "status": "passed" if completed.returncode == 0 and matched else "failed",
                "command": command,
                "exit_code": completed.returncode,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "expected_test_observed": matched,
                "stdout_sha256": _sha256(completed.stdout.encode("utf-8")),
                "stderr_sha256": _sha256(completed.stderr.encode("utf-8")),
                "output_bytes": len(output.encode("utf-8")),
            }
        except subprocess.TimeoutExpired as error:
            stdout = error.stdout.decode("utf-8", errors="replace") if isinstance(error.stdout, bytes) else error.stdout or ""
            stderr = error.stderr.decode("utf-8", errors="replace") if isinstance(error.stderr, bytes) else error.stderr or ""
            test_result = {
                "status": "unknown",
                "command": command,
                "exit_code": None,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "reason": "service integration probe exceeded its 600 second bound",
                "stdout_sha256": _sha256(stdout.encode("utf-8")),
                "stderr_sha256": _sha256(stderr.encode("utf-8")),
            }
    sources = {
        relative: _sha256((ROOT / relative).read_bytes())
        for relative in SERVICE_SOURCES
    }
    report = {
        "schema_version": 1,
        "kind": "phase4_p401_service_capability_probe",
        "probe_id": "phase4-service-probe-v1",
        "capability": "service",
        "required_hosts": ["native", "federated"],
        "status": test_result["status"],
        "platform": platform.system().lower(),
        "runtime": {
            "node": node_version,
            "cargo": cargo_version,
            "rustc": rustc_version,
        },
        "contract": {
            "route": "NoneBot Host service.invoke -> Rust CapabilityBroker -> Koishi Host ligamina.koishi.math.add",
            "expected_result": 42,
            "evidence": "A real NoneBot and Koishi worker pair runs under the Rust HostSession/Core Broker integration test; the caller emits cross-host:42 with the original trace.",
        },
        "test": test_result,
        "source_sha256": sources,
    }
    report["report_sha256"] = _write_report(output_path, report)
    report["output_path"] = str(output_path.expanduser().absolute())
    return report


def validate_service_probe_report(path: Path) -> dict[str, Any]:
    path = path.expanduser().resolve(strict=True)
    sidecar = path.with_suffix(path.suffix + ".sha256")
    expected = sidecar.read_text(encoding="ascii").split()[0]
    actual = _sha256(path.read_bytes())
    if expected != actual:
        raise ValueError("P4 service probe report SHA-256 sidecar mismatch")
    report = json.loads(path.read_text(encoding="utf-8"))
    test = report.get("test", {})
    expected_command = [
        "cargo", "test", "--locked", "-p", "ligamina-core", "--test", "host_session",
        SERVICE_TEST, "--", "--exact", "--nocapture",
    ]
    source_hashes = report.get("source_sha256", {})
    if (
        report.get("kind") != "phase4_p401_service_capability_probe"
        or report.get("probe_id") != "phase4-service-probe-v1"
        or report.get("capability") != "service"
        or report.get("required_hosts") != ["native", "federated"]
        or report.get("status") != "passed"
        or report.get("runtime", {}).get("node") != "v24.20.0"
        or test.get("status") != "passed"
        or test.get("command") != expected_command
        or test.get("expected_test_observed") is not True
        or test.get("exit_code") != 0
        or set(source_hashes) != set(SERVICE_SOURCES)
    ):
        raise ValueError("P4 service capability probe is not a successful, exact test report")
    for relative, digest in source_hashes.items():
        if _sha256((ROOT / relative).read_bytes()) != digest:
            raise ValueError(f"P4 service probe source changed since it was recorded: {relative}")
    return {"path": str(path), "sha256": actual, "probe_id": report["probe_id"], "status": "passed"}
