"""AB-05 Worker side: call one frozen AstrBot plugin's commands as MCP tools through the real Core.

Runs inside a network-disabled Docker Worker built from astrbot-tool-runtime.Dockerfile:
  /input/plugin/<dir>            the plugin tree from the frozen archive
  /input/wheelhouse, requirements.txt, constraints.txt   offline dependencies (as in AB-04)
  /input/project/hosts/astrbot   the Ligamina AstrBot Host (+ /input/project/contracts)
  /input/side/                   this file, astrbot_census_side.py (offline install), mcp_http.py
  /input/request.json            {"case_id", "host_id", "tool_calls"}: the stimulus only
The Core daemon (/usr/local/bin/ligamina-cli) supervises the AstrBot Host; a virtual
Gateway is leased and must stay idle because tool sessions complete inside Core.
Prints exactly one JSON object on stdout. No Oracle is present in the Worker.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

PROTOCOL_OUT = sys.stdout
sys.stdout = sys.stderr

INPUT = Path("/input")
WORK = Path("/work")
CORE = "/usr/local/bin/ligamina-cli"
PYTHON = "/opt/astrbot/venv/bin/python"
GATEWAY_ACCOUNT = "astrbot-ab05-bot"
sys.path.insert(0, str(INPUT / "side"))


def emit(result: dict[str, Any]) -> None:
    PROTOCOL_OUT.write(json.dumps(result, ensure_ascii=False) + "\n")
    PROTOCOL_OUT.flush()


def tool_name_part(value: str) -> str:
    """Mirror of ligamina_core::commands::tool_name_part."""
    sanitized = "".join(char if char.isascii() and (char.isalnum() or char in "_-.") else "_" for char in value)
    if sanitized == value:
        return sanitized
    return f"{sanitized}-{hashlib.sha256(value.encode('utf-8')).hexdigest()[:8]}"


def api(base: str, method: str, path: str, token: str, body: Any = None, expected: tuple[int, ...] = (200,),
        timeout: float = 10) -> Any:
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Authorization": f"Bearer {token}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    if status not in expected:
        raise RuntimeError(f"Core HTTP {method} {path} returned {status}: {raw[:500]!r}")
    return json.loads(raw) if raw else None


def run_tool_steps(client: Any, host_id: str, steps: list[list[dict[str, Any]]]) -> list[list[dict[str, Any]]]:
    from mcp_http import McpError

    recorded: list[list[dict[str, Any]]] = []
    for step in steps:
        outcomes: list[dict[str, Any] | None] = [None] * len(step)

        def call(index: int, spec: dict[str, Any]) -> None:
            name = f"cmd.{tool_name_part(host_id)}.{tool_name_part(str(spec['command_id']))}"
            record: dict[str, Any] = {"command_id": spec["command_id"], "tool": name, "arguments": spec.get("arguments", {})}
            try:
                response = client.call_tool(name, spec.get("arguments", {}))
                structured = response.get("structuredContent") or {}
                record.update({
                    "is_error": response.get("isError") is True,
                    "replies": [message.get("text", "") for message in structured.get("messages", [])],
                    "segments": [message.get("segments", []) for message in structured.get("messages", [])],
                    "refused_operations": structured.get("refused_operations", []),
                    "dispatch": structured.get("dispatch"),
                    "error_text": None if structured else "".join(
                        block.get("text", "") for block in response.get("content", []) if isinstance(block, dict))[:500],
                })
            except McpError as error:
                record.update({"is_error": True, "protocol_error": str(error)[:500], "replies": [], "refused_operations": []})
            outcomes[index] = record

        threads = [threading.Thread(target=call, args=(index, spec), daemon=True) for index, spec in enumerate(step)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=90)
        recorded.append([outcome or {"command_id": None, "is_error": True, "protocol_error": "call did not finish"}
                         for outcome in outcomes])
    return recorded


def run(request: dict[str, Any], install_result: dict[str, Any], plugin_dir: Path) -> dict[str, Any]:
    from mcp_http import McpHttpClient

    host_id = request["host_id"]
    core_dir = WORK / "core"
    core_dir.mkdir(parents=True, exist_ok=True)
    client_id, client_token, admin_token = "ab05-client", secrets.token_hex(32), secrets.token_hex(32)
    registration_token = secrets.token_hex(32)
    python_path = [str(INPUT / "project" / "hosts" / "astrbot")]
    if install_result.get("status") == "installed":
        python_path.insert(0, str(WORK / "site"))
    config = {
        "listen": "127.0.0.1:0", "database_path": str(core_dir / "core.sqlite"), "admin_token": admin_token,
        "mcp_client_id": client_id,
        "clients": [{"id": client_id, "token": client_token, "capabilities": [], "operation_grants": [
            {"id": "ab05-client-send", "account_id": GATEWAY_ACCOUNT, "operations": ["send_message"], "targets": None}]}],
        "gateways": [{"id": "ab05-gateway", "client_id": client_id, "account_id": GATEWAY_ACCOUNT,
                      "registration_token": registration_token}],
        "hosts": [{
            "id": host_id, "enabled": True, "program": PYTHON, "args": ["-m", "ligamina_astrbot.worker"],
            "current_dir": str(WORK), "max_restarts": 0, "restart_backoff_ms": 100,
            "startup_timeout_ms": 180000, "shutdown_timeout_ms": 10000,
            "environment": {
                "PYTHONPATH": os.pathsep.join(python_path),
                "LIGAMINA_ASTRBOT_PLUGINS": str(plugin_dir),
                "LIGAMINA_ASTRBOT_DATA_DIR": str(WORK / "astrbot"),
                "HOME": str(WORK), "ASTRBOT_DISABLE_METRICS": "1", "PYTHONDONTWRITEBYTECODE": "1",
            },
            # A grant toward the Gateway's account: a send escaping the tool session could reach it.
            "operation_grants": [{"id": "ab05-host-send", "client_id": client_id, "account_id": GATEWAY_ACCOUNT,
                                  "operations": ["send_message"], "targets": None}],
        }],
        "router": {"max_pending_per_conversation": 16, "max_outstanding_events": 64, "max_tracked_conversations": 64,
                   "max_concurrent_conversations": 8, "processing_timeout_ms": 30000},
        "command_tools": {"enabled": True, "allow": [f"cmd.{tool_name_part(host_id)}.*"]},
    }
    config_path = core_dir / "daemon.json"
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    process = subprocess.Popen([CORE, "daemon", "--config", str(config_path)], cwd=str(WORK),
                               env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(WORK)},
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding="utf-8", errors="replace", bufsize=1)
    stdout_lines: queue.Queue[str] = queue.Queue()
    stderr_lines: queue.Queue[str] = queue.Queue()

    def pump(stream: Any, sink: queue.Queue[str]) -> None:
        for line in stream:
            sink.put(line)

    threading.Thread(target=pump, args=(process.stdout, stdout_lines), daemon=True).start()
    threading.Thread(target=pump, args=(process.stderr, stderr_lines), daemon=True).start()
    result: dict[str, Any] = {"status": "failed", "host_id": host_id, "install": install_result,
                              "gateway_operations": [], "gateway_errors": []}
    stop = threading.Event()
    base = None
    try:
        ready, deadline = None, time.monotonic() + 60
        while ready is None and time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"Core exited during startup with code {process.returncode}")
            try:
                packet = json.loads(stdout_lines.get(timeout=0.25))
            except (queue.Empty, json.JSONDecodeError):
                continue
            if isinstance(packet, dict) and packet.get("ready") is True:
                ready = packet
        if ready is None:
            raise TimeoutError("Core daemon did not announce readiness")
        base = f"http://{ready['listen']}"
        host_status, deadline = None, time.monotonic() + 200
        while time.monotonic() < deadline:
            status = api(base, "GET", "/v1/status", admin_token)
            host_status = next((item for item in status.get("hosts", []) if item.get("id") == host_id), None)
            if host_status and host_status.get("state") == "active":
                break
            time.sleep(0.5)
        if not host_status or host_status.get("state") != "active":
            raise RuntimeError(f"AstrBot Host did not become active: {host_status}")
        result["host_status"] = {key: host_status.get(key) for key in ("state", "pid", "generation")}
        lease = api(base, "POST", "/v1/gateways/ab05-gateway/lease", registration_token, {"ttl_ms": 300000})

        def gateway_loop() -> None:
            while not stop.is_set():
                try:
                    operation = api(base, "POST", "/v1/gateways/ab05-gateway/actions/claim", lease["token"], lease, timeout=5)
                except BaseException as error:  # noqa: BLE001 - recorded as evidence
                    result["gateway_errors"].append(f"{type(error).__name__}: {error}"[:300])
                    return
                if operation is None:
                    stop.wait(0.05)
                    continue
                # Never acknowledged: any claimed operation is a leak out of the tool session.
                result["gateway_operations"].append({"operation_id": operation.get("key", {}).get("operation_id"),
                                                     "kind": operation.get("operation_payload", {}).get("kind")})

        gateway = threading.Thread(target=gateway_loop, daemon=True)
        gateway.start()
        client = McpHttpClient(f"{base}/mcp", client_token)
        initialize = client.initialize()
        prefix = f"cmd.{tool_name_part(host_id)}."
        listed: list[str] = []
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            listed = sorted(tool["name"] for tool in client.list_tools() if tool.get("name", "").startswith(prefix))
            if listed:
                break
            time.sleep(0.5)
        result["tool_calls"] = {
            "protocol_version": initialize.get("protocolVersion"),
            "listed_command_tools": listed,
            "steps": run_tool_steps(client, host_id, request["tool_calls"]),
        }
        stop.wait(1.0)  # let a leaked operation, if any, reach the Gateway before it stops polling
        result["status"] = "completed"
    except Exception as error:  # noqa: BLE001 - the Worker reports, the orchestrator judges
        result["diagnostic"] = f"{type(error).__name__}: {str(error)[:500]}"
    finally:
        stop.set()
        if process.poll() is None:
            try:
                if base:
                    api(base, "POST", "/v1/shutdown", admin_token, expected=(202,))
            except Exception as error:  # noqa: BLE001
                result["shutdown_request_error"] = f"{type(error).__name__}: {error}"[:300]
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                result["core_killed"] = True
        result["core_exit_code"] = process.returncode
        pid = (result.get("host_status") or {}).get("pid")
        if pid:
            try:
                os.kill(int(pid), 0)
                alive = Path(f"/proc/{int(pid)}/stat").read_text().split()[2] != "Z"
            except (ProcessLookupError, OSError):
                alive = False
            result["host_alive_after_shutdown"] = alive
        result["gateway_operation_count"] = len(result["gateway_operations"])
        result["core_stderr_tail"] = "".join(list(stderr_lines.queue))[-3000:]
    return result


def main() -> None:
    request = json.loads((INPUT / "request.json").read_text(encoding="utf-8"))
    if set(request) - {"case_id", "host_id", "tool_calls"}:
        emit({"status": "failed", "diagnostic": f"unexpected request keys: {sorted(set(request) - {'case_id', 'host_id', 'tool_calls'})}"})
        return
    plugin_dirs = [path for path in (INPUT / "plugin").iterdir() if path.is_dir()]
    if len(plugin_dirs) != 1:
        emit({"status": "failed", "diagnostic": "expected exactly one plugin directory"})
        return
    from astrbot_census_side import install

    install_result = install()
    if install_result["status"] == "failed":
        emit({"status": "failed", "diagnostic": "dependency_install_failed", "install": install_result})
        return
    emit(run(request, install_result, plugin_dirs[0]))


if __name__ == "__main__":
    try:
        main()
    finally:
        sys.stderr.flush()
        os._exit(0)
