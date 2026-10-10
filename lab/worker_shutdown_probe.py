"""Check that both native Workers exit after acknowledging Core shutdown."""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def read_lines(stream: Any, target: queue.Queue[str]) -> None:
    for line in iter(stream.readline, ""):
        target.put(line)


def read_response(
    output: queue.Queue[str],
    request_id: str,
    timeout: float,
    process: subprocess.Popen[str],
    errors: queue.Queue[str],
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"worker exited with {process.returncode} before {request_id}; stderr={''.join(list(errors.queue))[-3000:]}"
            )
        try:
            packet = json.loads(output.get(timeout=min(0.2, deadline - time.monotonic())))
        except queue.Empty:
            continue
        if packet.get("id") == request_id:
            return packet
    raise TimeoutError(f"worker did not answer {request_id} within {timeout} seconds")


def run_worker(command: list[str], cwd: Path, environment: dict[str, str]) -> tuple[float, int, str]:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    assert process.stdin and process.stdout and process.stderr
    output: queue.Queue[str] = queue.Queue()
    errors: queue.Queue[str] = queue.Queue()
    threading.Thread(target=read_lines, args=(process.stdout, output), daemon=True).start()
    threading.Thread(target=read_lines, args=(process.stderr, errors), daemon=True).start()
    try:
        process.stdin.write(json.dumps({
            "jsonrpc": "2.0",
            "id": "initialize",
            "method": "host.initialize",
            "params": {"protocol_version": "0.1", "host_id": "shutdown-test", "epoch": 1},
        }) + "\n")
        process.stdin.flush()
        initialized = read_response(output, "initialize", timeout=30, process=process, errors=errors)
        if initialized.get("error") or initialized.get("result", {}).get("ready") is not True:
            raise AssertionError(f"worker did not initialize: {initialized}")

        started = time.monotonic()
        process.stdin.write(json.dumps({
            "jsonrpc": "2.0",
            "id": "shutdown",
            "method": "host.shutdown",
            "params": {},
        }) + "\n")
        process.stdin.flush()
        response = read_response(output, "shutdown", timeout=5, process=process, errors=errors)
        if response.get("error") or response.get("result", {}).get("stopping") is not True:
            raise AssertionError(f"worker did not acknowledge shutdown: {response}")
        process.wait(timeout=5)
        return time.monotonic() - started, process.returncode, "".join(list(errors.queue))[-3000:]
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def main() -> int:
    uv = shutil.which("uv")
    node = shutil.which("node")
    if not uv or not node:
        raise SystemExit("uv and node must be on PATH")
    environment = os.environ.copy()
    environment["PATH"] = str(Path(node).parent) + os.pathsep + environment.get("PATH", "")
    with tempfile.TemporaryDirectory(prefix="ligamina-worker-shutdown-") as temp:
        data_root = Path(temp)
        nonebot_environment = environment.copy()
        nonebot_environment.update({
            "LIGAMINA_NONEBOT_PLUGINS": "ligamina_nonebot.federated_smoke_plugin",
            "LIGAMINA_NONEBOT_NICKNAMES": "Bot",
            "LIGAMINA_NONEBOT_DATA_DIR": str(data_root / "nonebot-data"),
        })
        nonebot_command = [
            uv, "run", "--locked", "--project", str(ROOT / "hosts/nonebot"),
            "--python", "3.12.14", "python", "-m", "ligamina_nonebot.worker",
        ]
        nonebot_seconds, nonebot_code, nonebot_logs = run_worker(
            nonebot_command, ROOT / "hosts/nonebot", nonebot_environment
        )

        koishi_environment = environment.copy()
        koishi_environment.update({
            "LIGAMINA_KOISHI_PLUGINS": (ROOT / "hosts/koishi/dist/federated-smoke-plugin.js").as_uri(),
            "LIGAMINA_KOISHI_DATA_DIR": str(data_root / "koishi-data"),
            "LIGAMINA_KOISHI_NICKNAMES": "Bot",
        })
        koishi_command = [node, str(ROOT / "hosts/koishi/dist/worker.js")]
        koishi_seconds, koishi_code, koishi_logs = run_worker(
            koishi_command, ROOT / "hosts/koishi", koishi_environment
        )
    print(json.dumps({
        "nonebot": {"shutdown_seconds": round(nonebot_seconds, 3), "exit_code": nonebot_code, "stderr_tail": nonebot_logs},
        "koishi": {"shutdown_seconds": round(koishi_seconds, 3), "exit_code": koishi_code, "stderr_tail": koishi_logs},
    }, ensure_ascii=False))
    if nonebot_code or koishi_code or nonebot_seconds > 5 or koishi_seconds > 5:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
