"""Run the production-style local Core + NoneBot + Koishi federated smoke suite."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
if sys.path and Path(sys.path[0]).resolve() == SCRIPT_DIR:
    sys.path.pop(0)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import queue
from lab.report import render_summary_output

try:
    from lab.native_runner import load_case, run_pair
except ModuleNotFoundError:  # supports the documented `python lab/run.py` entry point
    from native_runner import load_case, run_pair


NONEBOT_CAPABILITY = "ligamina.nonebot.math.add"
KOISHI_CAPABILITY = "ligamina.koishi.math.add"
CLIENT_ID = "ligamina-smoke-client"
ACCOUNT_ID = "9000"


class SmokeFailure(RuntimeError):
    pass


class Api:
    def __init__(self, report: dict[str, Any]):
        self.report = report
        self.lock = threading.Lock()

    def request(
        self,
        base_url: str,
        method: str,
        path: str,
        *,
        token: str,
        body: Any = None,
        expected: tuple[int, ...] = (200,),
        timeout: float = 10,
    ) -> tuple[int, Any]:
        encoded = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Authorization": f"Bearer {token}"}
        if encoded is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            base_url + path,
            data=encoded,
            headers=headers,
            method=method,
        )
        status = 0
        response_body: Any = None
        error_text = None
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = response.status
                raw = response.read()
                response_body = json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            status = error.code
            raw = error.read()
            try:
                response_body = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                response_body = raw.decode("utf-8", errors="replace")
            error_text = str(error)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            error_text = str(error)

        item = {
            "method": method,
            "path": path,
            "request": redact_config(body),
            "status": status,
            "response": redact_config(response_body),
        }
        if error_text:
            item["transport_error"] = error_text
        with self.lock:
            self.report["http_requests"].append(item)
        if status not in expected:
            raise SmokeFailure(
                f"HTTP {method} {path} returned {status}: {response_body or error_text}"
            )
        return status, response_body


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def now_ms() -> int:
    return int(time.time() * 1000)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tool_version(command: list[str]) -> str | None:
    if os.name == "nt" and command and command[0] == "npm":
        command = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", "npm.cmd", *command[1:]]
    try:
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    return (result.stdout or result.stderr).strip().splitlines()[0]


def environment_fingerprint(config: dict[str, Any] | None = None) -> dict[str, Any]:
    lockfiles = [
        "Cargo.lock",
        "hosts/nonebot/uv.lock",
        "hosts/koishi/package-lock.json",
        "clients/reference/package-lock.json",
    ]
    redacted = redact_config(config) if config is not None else None
    result: dict[str, Any] = {
        "os": platform.platform(),
        "system": platform.system(),
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "node": tool_version(["node", "--version"]),
        "npm": tool_version(["npm", "--version"]),
        "uv": tool_version(["uv", "--version"]),
        "cargo": tool_version(["cargo", "--version"]),
        "rustc": tool_version(["rustc", "--version"]),
        "lockfiles": {
            name: sha256(ROOT / name) for name in lockfiles if (ROOT / name).is_file()
        },
    }
    if redacted is not None:
        encoded = json.dumps(redacted, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        result["sanitized_config_sha256"] = hashlib.sha256(encoded).hexdigest()
    return result


def redact_config(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "<redacted>" if "token" in key.casefold() else redact_config(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_config(item) for item in value]
    return value


def run_command(
    report: dict[str, Any],
    label: str,
    command: list[str],
    *,
    cwd: Path = ROOT,
    timeout: int = 600,
) -> None:
    if os.name == "nt" and command and command[0] == "npm":
        command = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", "npm.cmd", *command[1:]]
    step: dict[str, Any] = {"label": label, "command": command, "cwd": str(cwd)}
    report["build_steps"].append(step)
    try:
        process = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as error:
        step.update({"status": "environment_missing", "error": str(error)})
        raise EnvironmentError(f"required tool is unavailable: {command[0]}") from error
    except subprocess.TimeoutExpired as error:
        step.update({"status": "failed", "error": f"timed out after {timeout}s"})
        raise SmokeFailure(f"{label} timed out after {timeout}s") from error
    step.update(
        {
            "status": "passed" if process.returncode == 0 else "failed",
            "returncode": process.returncode,
            "stdout_tail": process.stdout[-6000:],
            "stderr_tail": process.stderr[-6000:],
        }
    )
    if process.returncode:
        raise SmokeFailure(f"{label} failed with exit code {process.returncode}")


def available_platform() -> str:
    return "windows" if os.name == "nt" else "linux" if sys.platform.startswith("linux") else sys.platform


def assert_platform(requested: str) -> None:
    actual = available_platform()
    if actual != requested:
        raise EnvironmentError(f"requested platform {requested}, but this process runs on {actual}")
    npm_name = "npm.cmd" if os.name == "nt" else "npm"
    missing = [name for name in ("cargo", "node", npm_name, "uv") if shutil.which(name) is None]
    if missing:
        raise EnvironmentError(f"required tools are missing from PATH: {', '.join(missing)}")


def build_dependencies(report: dict[str, Any], offline: bool) -> None:
    run_command(
        report,
        "build Core daemon",
        ["cargo", "build", "-p", "ligamina-cli", "--locked", *( ["--offline"] if offline else [])],
        timeout=900,
    )
    for project, label in ((ROOT / "hosts/koishi", "install Koishi locked dependencies"),
                           (ROOT / "clients/reference", "install reference client locked dependencies")):
        run_command(report, label, ["npm", "ci", "--no-audit", "--fund=false", "--prefer-offline"], cwd=project)
    run_command(report, "build Koishi Host", ["npm", "run", "build"], cwd=ROOT / "hosts/koishi")
    run_command(report, "build reference MCP client", ["npm", "run", "build"], cwd=ROOT / "clients/reference")
    # Validate the locked Python profile before Core tries to launch a managed Host.
    run_command(
        report,
        "resolve locked NoneBot runtime",
        ["uv", "run", "--locked", "--project", str(ROOT / "hosts/nonebot"), "--python", "3.12.14", "python", "-c", "import nonebot; print(nonebot.__version__)"] ,
        timeout=900,
    )


def make_config(temp: Path, listen_port: int = 0) -> dict[str, Any]:
    nonebot_data = temp / "nonebot-data"
    koishi_data = temp / "koishi-data"
    nonebot_data.mkdir(parents=True, exist_ok=True)
    koishi_data.mkdir(parents=True, exist_ok=True)
    koishi_plugin = (ROOT / "hosts/koishi/dist/federated-smoke-plugin.js").resolve().as_uri()
    client_token = secrets.token_hex(32)
    admin_token = secrets.token_hex(32)
    registration_token = secrets.token_hex(32)
    operation_grant = {
        "id": "smoke-client-send-grant",
        "account_id": ACCOUNT_ID,
        "operations": ["send_message"],
        "targets": None,
    }
    host_grants = lambda host: [{
        "id": f"{host}-send-grant",
        "client_id": CLIENT_ID,
        "account_id": ACCOUNT_ID,
        "operations": ["send_message"],
        "targets": None,
    }]
    return {
        "listen": f"127.0.0.1:{listen_port}",
        "database_path": str(temp / "ligamina.sqlite"),
        "admin_token": admin_token,
        "mcp_client_id": CLIENT_ID,
        "clients": [{
            "id": CLIENT_ID,
            "token": client_token,
            "capabilities": [NONEBOT_CAPABILITY, KOISHI_CAPABILITY],
            "operation_grants": [operation_grant],
        }],
        "gateways": [{
            "id": "smoke-gateway",
            "client_id": CLIENT_ID,
            "account_id": ACCOUNT_ID,
            "registration_token": registration_token,
        }],
        "hosts": [{
            "id": "nonebot-smoke",
            "enabled": True,
            "program": shutil.which("uv") or "uv",
            "args": ["run", "--locked", "--project", str(ROOT / "hosts/nonebot"), "--python", "3.12.14", "python", "-m", "ligamina_nonebot.worker"],
            "current_dir": str(ROOT / "hosts/nonebot"),
            "environment": {
                "LIGAMINA_NONEBOT_PLUGINS": "ligamina_nonebot.federated_smoke_plugin",
                "LIGAMINA_NONEBOT_NICKNAMES": "Bot",
                "LIGAMINA_NONEBOT_DATA_DIR": str(nonebot_data),
            },
            "max_restarts": 0,
            "restart_backoff_ms": 100,
            "startup_timeout_ms": 60000,
            "shutdown_timeout_ms": 10000,
            "operation_grants": host_grants("nonebot-smoke"),
        }, {
            "id": "koishi-smoke",
            "enabled": True,
            "program": shutil.which("node") or "node",
            "args": ["dist/worker.js"],
            "current_dir": str(ROOT / "hosts/koishi"),
            "environment": {
                "LIGAMINA_KOISHI_PLUGINS": koishi_plugin,
                "LIGAMINA_KOISHI_DATA_DIR": str(koishi_data),
                "LIGAMINA_KOISHI_NICKNAMES": "Bot",
            },
            "max_restarts": 0,
            "restart_backoff_ms": 100,
            "startup_timeout_ms": 60000,
            "shutdown_timeout_ms": 10000,
            "operation_grants": host_grants("koishi-smoke"),
        }],
        "router": {
            "max_pending_per_conversation": 16,
            "max_outstanding_events": 64,
            "max_tracked_conversations": 64,
            "max_concurrent_conversations": 8,
            "processing_timeout_ms": 30000,
        },
        "_secrets": {
            "client_token": client_token,
            "admin_token": admin_token,
            "gateway_registration_token": registration_token,
        },
    }


def event_envelope(host: str, kind: str, conversation_id: str, actor_id: str, message_id: str) -> dict[str, Any]:
    event_id = f"g1-{host}-{kind}"
    return {
        "schema_version": "ligamina/0.1",
        "event_id": event_id,
        "trace_id": f"trace-{event_id}",
        "platform": "onebot.v11",
        "account_id": ACCOUNT_ID,
        "conversation": {"kind": kind, "id": conversation_id},
        "actor_id": actor_id,
        "timestamp": utc_now(),
        "target_host": host,
        "message": [{"type": "text", "text": f"smoke-{host}-{kind}"}],
        "extensions": {"onebot.v11": {"message_id": message_id, "sender": {"nickname": "Alice"}}},
    }


def wait_event(api: Api, base_url: str, token: str, event: dict[str, Any], timeout: float = 15) -> dict[str, Any]:
    query = urllib.parse.urlencode({
        "platform": event["platform"],
        "account_id": event["account_id"],
        "conversation_kind": event["conversation"]["kind"],
        "conversation_id": event["conversation"]["id"],
    })
    path = f"/v1/events/{urllib.parse.quote(event['event_id'], safe='')}?{query}"
    deadline = time.monotonic() + timeout
    latest = None
    while time.monotonic() < deadline:
        _, latest = api.request(base_url, "GET", path, token=token)
        if latest.get("status") in ("completed", "failed", "unknown"):
            return latest
        time.sleep(0.05)
    raise SmokeFailure(f"event {event['event_id']} did not finish: {latest}")


def start_reader(stream, output: queue.Queue[str]) -> None:
    for line in iter(stream.readline, ""):
        output.put(line)


def start_gateway(api: Api, base_url: str, registration_token: str, report: dict[str, Any]):
    stop = threading.Event()
    state: dict[str, Any] = {"credential": None, "error": None, "count": 0}
    _, credential = api.request(
        base_url,
        "POST",
        "/v1/gateways/smoke-gateway/lease",
        token=registration_token,
        body={"ttl_ms": 300000},
    )
    state["credential"] = credential

    def loop() -> None:
        try:
            while not stop.is_set():
                _, operation = api.request(
                    base_url,
                    "POST",
                    "/v1/gateways/smoke-gateway/actions/claim",
                    token=credential["token"],
                    body=credential,
                    timeout=5,
                )
                if operation is None:
                    stop.wait(0.05)
                    continue
                state["count"] += 1
                payload = operation["operation_payload"]
                # OneBot v11 models message IDs as numeric; a virtual gateway must
                # honor that contract or the native send call raises after dispatch.
                receipt_id = str(700000 + state["count"])
                receipt = {
                    "key": operation["key"],
                    "fingerprint": operation["fingerprint"],
                    "status": "succeeded",
                    "platform_message_id": receipt_id,
                    "data": None,
                    "error": None,
                    "observed_at_ms": now_ms(),
                    "evidence_ref": f"ligamina-smoke://gateway/{receipt_id}",
                }
                _, completed = api.request(
                    base_url,
                    "POST",
                    f"/v1/gateways/smoke-gateway/actions/{urllib.parse.quote(operation['key']['operation_id'], safe='')}/result",
                    token=credential["token"],
                    body={"credential": credential, "result": receipt},
                )
                with api.lock:
                    report["gateway_operations"].append({
                        "operation": operation,
                        "outgoing_message": payload.get("params", {}).get("message"),
                        "receipt_request": receipt,
                        "receipt_response": completed,
                    })
        except BaseException as error:  # retain worker errors for the main thread
            state["error"] = f"{type(error).__name__}: {error}"
            stop.set()

    thread = threading.Thread(target=loop, name="ligamina-smoke-gateway", daemon=True)
    thread.start()
    return stop, thread, state


def kill_host(platform_name: str, pid: int) -> dict[str, Any]:
    if platform_name == "windows":
        result = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True, timeout=15, check=False)
        return {"command": ["taskkill", "/PID", str(pid), "/T", "/F"], "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
    os.kill(pid, signal.SIGKILL)
    return {"signal": "SIGKILL", "pid": pid}


def process_alive(pid: int, platform_name: str) -> bool:
    if platform_name == "windows":
        import csv
        import io

        result = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=10, check=False)
        try:
            return any(row[1] == str(pid) for row in csv.reader(io.StringIO(result.stdout)) if len(row) > 1)
        except csv.Error:
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_for_host_status(api: Api, base_url: str, admin_token: str, host_id: str, state: str, timeout: float = 15) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    current = None
    while time.monotonic() < deadline:
        _, current = api.request(base_url, "GET", "/v1/status", token=admin_token)
        found = next((host for host in current["hosts"] if host["id"] == host_id), None)
        if found and found["state"] == state:
            return current
        time.sleep(0.1)
    raise SmokeFailure(f"Host {host_id} did not reach {state}: {current}")


def run_negative_differential() -> dict[str, Any]:
    case = load_case(ROOT / "samples/fixtures/event-routing/single-group-reply.json")
    profile = ROOT / "samples/profiles/nonebot-group-v0.1.json"
    harness = ROOT / "lab/test_workers/fixture_harness.py"
    lockfile = ROOT / "hosts/nonebot/uv.lock"
    base = [
        sys.executable,
        str(harness),
        "--profile", str(profile),
        "--lockfile", str(lockfile),
    ]
    comparison = run_pair(case, base, [*base, "--inject", "wrong-recipient"], 15)
    if comparison["comparison"]["status"] != "failed":
        raise SmokeFailure("differential runner failed to detect the injected wrong-recipient link")
    if "operation_expectation_failed" not in {
        issue["code"] for issue in comparison["comparison"]["issues"]
    }:
        raise SmokeFailure("wrong-recipient injection was rejected for an unexpected reason")
    return comparison


def run_smoke(platform_name: str, report_path: Path, offline: bool) -> tuple[int, dict[str, Any]]:
    report: dict[str, Any] = {
        "suite": "smoke",
        "platform": platform_name,
        "status": "running",
        "started_at": utc_now(),
        "build_steps": [],
        "http_requests": [],
        "events": [],
        "gateway_operations": [],
        "capability_invocations": [],
        "mcp_client": None,
        "failure_injection": None,
        "cleanup": {"host_pids": [], "remaining_pids": [], "daemon_exit_code": None},
        "error": None,
    }
    daemon: subprocess.Popen[str] | None = None
    daemon_stderr: queue.Queue[str] = queue.Queue()
    daemon_stdout: queue.Queue[str] = queue.Queue()
    gateway_stop: threading.Event | None = None
    gateway_thread: threading.Thread | None = None
    gateway_state: dict[str, Any] | None = None
    base_url: str | None = None
    admin_token = client_token = registration_token = ""
    host_pids: set[int] = set()
    temp = Path(tempfile.mkdtemp(prefix="ligamina-g1-smoke-"))
    api = Api(report)

    try:
        assert_platform(platform_name)
        build_dependencies(report, offline)
        config = make_config(temp)
        secrets_config = config.pop("_secrets")
        admin_token = secrets_config["admin_token"]
        client_token = secrets_config["client_token"]
        registration_token = secrets_config["gateway_registration_token"]
        report["environment"] = environment_fingerprint(config)
        config_path = temp / "daemon.json"
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        executable = ROOT / "target/debug/ligamina-cli.exe" if platform_name == "windows" else ROOT / "target/debug/ligamina-cli"
        if not executable.is_file():
            raise EnvironmentError(f"built CLI binary was not found at {executable}")
        daemon = subprocess.Popen(
            [str(executable), "daemon", "--config", str(config_path)],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert daemon.stdout is not None and daemon.stderr is not None
        threading.Thread(target=start_reader, args=(daemon.stdout, daemon_stdout), daemon=True).start()
        threading.Thread(target=start_reader, args=(daemon.stderr, daemon_stderr), daemon=True).start()
        ready = None
        ready_deadline = time.monotonic() + 100
        while time.monotonic() < ready_deadline:
            if daemon.poll() is not None:
                break
            try:
                line = daemon_stdout.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                report.setdefault("daemon_stdout", []).append(line.rstrip())
                continue
            if value.get("ready") is True:
                ready = value
                break
        if ready is None:
            details = []
            while not daemon_stderr.empty():
                details.append(daemon_stderr.get_nowait())
            report["daemon_stderr"] = "".join(details)[-12000:]
            if daemon.poll() is not None:
                raise SmokeFailure(f"Core daemon exited during startup: {daemon.returncode}")
            raise SmokeFailure("Core daemon did not announce readiness within 100 seconds")
        base_url = f"http://{ready['listen']}"
        report["daemon_ready"] = ready
        _, initial_status = api.request(base_url, "GET", "/v1/status", token=admin_token)
        report["initial_host_status"] = initial_status
        for host in initial_status["hosts"]:
            if host.get("state") != "active" or not host.get("pid"):
                raise SmokeFailure(f"Host did not become active: {host}")
            host_pids.add(int(host["pid"]))
        report["cleanup"]["host_pids"] = sorted(host_pids)

        gateway_stop, gateway_thread, gateway_state = start_gateway(api, base_url, registration_token, report)
        event_specs = [
            ("nonebot-smoke", "group", "42", "23", "1001"),
            ("nonebot-smoke", "private", "23", "23", "1002"),
            ("koishi-smoke", "group", "72", "24", "2001"),
            ("koishi-smoke", "private", "24", "24", "2002"),
        ]
        events = [event_envelope(*spec) for spec in event_specs]
        for event in events:
            _, accepted = api.request(base_url, "POST", "/v1/events", token=client_token, body=event, expected=(202,))
            record = wait_event(api, base_url, client_token, event)
            report["events"].append({"request": event, "accepted": accepted, "final": record})
            if record.get("status") != "completed":
                raise SmokeFailure(f"event was not completed: {record}")
        if gateway_state["error"]:
            raise SmokeFailure(f"virtual gateway failed: {gateway_state['error']}")

        operations_deadline = time.monotonic() + 10
        while time.monotonic() < operations_deadline:
            with api.lock:
                if len(report["gateway_operations"]) == 4:
                    break
            if gateway_state["error"]:
                raise SmokeFailure(f"virtual gateway failed: {gateway_state['error']}")
            time.sleep(0.025)

        expected_replies = {
            ("nonebot-smoke", "group"): "nonebot:group:smoke-nonebot-smoke-group",
            ("nonebot-smoke", "private"): "nonebot:private:smoke-nonebot-smoke-private",
            ("koishi-smoke", "group"): "koishi:group:smoke-koishi-smoke-group",
            ("koishi-smoke", "private"): "koishi:private:smoke-koishi-smoke-private",
        }
        actual_replies: dict[tuple[str, str], tuple[dict[str, Any], str]] = {}
        for entry in report["gateway_operations"]:
            operation = entry["operation"]
            host = operation["principal"].get("id") if isinstance(operation.get("principal"), dict) else ""
            outgoing = entry["outgoing_message"] or []
            text = "".join(segment.get("text", "") for segment in outgoing if segment.get("type") == "text")
            key = (host, operation["operation_payload"].get("target", {}).get("kind", ""))
            actual_replies[key] = (operation, text)
            if entry["receipt_response"].get("status") != "succeeded":
                raise SmokeFailure(f"gateway receipt did not complete: {entry['receipt_response']}")
        if len(report["gateway_operations"]) != 4:
            raise SmokeFailure(f"expected four gateway operations, observed {len(report['gateway_operations'])}")
        for key, expected_text in expected_replies.items():
            observed = actual_replies.get(key)
            if observed is None or observed[1] != expected_text:
                raise SmokeFailure(f"native plugin reply mismatch for {key}: {observed}")
            operation = observed[0]
            source = next(event for event in events if event["target_host"] == key[0] and event["conversation"]["kind"] == key[1])
            if operation["operation_payload"].get("target") != source["conversation"]:
                raise SmokeFailure(f"reply recipient mismatch for {key}: {operation['operation_payload']}")
            operation_key = operation["key"]
            status_query = urllib.parse.urlencode({
                "account_id": operation_key["account_id"],
                "operation": operation_key["operation"],
            })
            _, operation_status = api.request(
                base_url,
                "GET",
                f"/v1/operations/{urllib.parse.quote(operation_key['operation_id'], safe='')}?{status_query}",
                token=client_token,
            )
            if operation_status.get("status") != "succeeded":
                raise SmokeFailure(f"operation status was not completed: {operation_status}")
            report.setdefault("operation_statuses", []).append(operation_status)

        for capability in (NONEBOT_CAPABILITY, KOISHI_CAPABILITY):
            _, value = api.request(
                base_url,
                "POST",
                "/v1/invocations",
                token=client_token,
                body={"capability_id": capability, "arguments": {"left": 19, "right": 23}},
            )
            if value.get("result") != 42:
                raise SmokeFailure(f"HTTP capability call {capability} returned {value}")
            report["capability_invocations"].append({"transport": "http", "capability": capability, "response": value})

        mcp = subprocess.run(
            ["node", str(ROOT / "clients/reference/tests/mcp-http-smoke.mjs"), base_url, client_token, NONEBOT_CAPABILITY, KOISHI_CAPABILITY],
            cwd=ROOT / "clients/reference",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=45,
            check=False,
        )
        report["mcp_client"] = {
            "command": ["node", "clients/reference/tests/mcp-http-smoke.mjs", "<Core URL>", "<redacted>", NONEBOT_CAPABILITY, KOISHI_CAPABILITY],
            "returncode": mcp.returncode,
            "stdout": mcp.stdout[-6000:],
            "stderr": mcp.stderr[-6000:],
        }
        if mcp.returncode:
            raise SmokeFailure(f"independent MCP HTTP client failed with exit code {mcp.returncode}")

        crash_pid = next(int(host["pid"]) for host in initial_status["hosts"] if host["id"] == "nonebot-smoke")
        report["injected_host_crash"] = kill_host(platform_name, crash_pid)
        crashed_status = wait_for_host_status(api, base_url, admin_token, "nonebot-smoke", "failed")
        report["after_crash_status"] = crashed_status
        capabilities = None
        remaining: set[str] = set()
        revoke_deadline = time.monotonic() + 5
        while time.monotonic() < revoke_deadline:
            _, capabilities = api.request(base_url, "GET", "/v1/capabilities", token=client_token)
            remaining = {item["descriptor"]["capability_id"] for item in capabilities["capabilities"]}
            if NONEBOT_CAPABILITY not in remaining:
                break
            time.sleep(0.05)
        if NONEBOT_CAPABILITY in remaining or KOISHI_CAPABILITY not in remaining:
            raise SmokeFailure(f"capability generation revocation failed: {sorted(remaining)}")
        _, denied = api.request(
            base_url,
            "POST",
            "/v1/invocations",
            token=client_token,
            body={"capability_id": NONEBOT_CAPABILITY, "arguments": {"left": 1, "right": 2}},
            expected=(422,),
        )
        report["revoked_capability_attempt"] = denied
        _, surviving = api.request(
            base_url,
            "POST",
            "/v1/invocations",
            token=client_token,
            body={"capability_id": KOISHI_CAPABILITY, "arguments": {"left": 19, "right": 23}},
        )
        if surviving.get("result") != 42:
            raise SmokeFailure(f"remaining Host did not continue to serve capabilities: {surviving}")
        report["capability_invocations"].append({"transport": "http-after-crash", "capability": KOISHI_CAPABILITY, "response": surviving})

        report["failure_injection"] = run_negative_differential()
        report["failure_injection"]["runner_exit_code"] = 1
        if gateway_state["error"]:
            raise SmokeFailure(f"virtual gateway failed: {gateway_state['error']}")
        report["status"] = "passed"
        return_code = 0
    except EnvironmentError as error:
        report["status"] = "environment_missing"
        report["error"] = str(error)
        return_code = 2
    except BaseException as error:
        report["status"] = "failed"
        report["error"] = f"{type(error).__name__}: {error}"
        report["traceback"] = traceback.format_exc()[-12000:]
        return_code = 1
    finally:
        if gateway_stop:
            gateway_stop.set()
        if gateway_thread:
            gateway_thread.join(timeout=5)
        if gateway_state and gateway_state.get("error") and not report.get("error"):
            report["status"] = "failed"
            report["error"] = f"virtual gateway failed: {gateway_state['error']}"
            return_code = 1
        if daemon and daemon.poll() is None:
            if base_url:
                try:
                    api.request(base_url, "POST", "/v1/shutdown", token=admin_token, expected=(202,))
                except Exception as error:
                    report.setdefault("cleanup_errors", []).append(f"graceful shutdown request failed: {error}")
            try:
                daemon.wait(timeout=20)
            except subprocess.TimeoutExpired:
                daemon.terminate()
                try:
                    daemon.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    daemon.kill()
                    daemon.wait(timeout=5)
                report.setdefault("cleanup_errors", []).append("Core daemon required forced termination")
                if report["status"] == "passed":
                    report["status"] = "failed"
                    report["error"] = "Core daemon did not stop gracefully"
                    return_code = 1
        if daemon:
            report["cleanup"]["daemon_exit_code"] = daemon.returncode
            for stream_name, stream in (("stdout", daemon.stdout), ("stderr", daemon.stderr)):
                if stream:
                    tail = stream.read()
                    if tail:
                        report[f"daemon_{stream_name}_tail"] = tail[-12000:]
        report["cleanup"]["remaining_pids"] = sorted(pid for pid in host_pids if process_alive(pid, platform_name))
        if report["cleanup"]["remaining_pids"]:
            report["status"] = "failed"
            report["error"] = f"managed Host processes remain after Core shutdown: {report['cleanup']['remaining_pids']}"
            return_code = 1
        report["ended_at"] = utc_now()
        report["duration_seconds"] = round(
            (datetime.fromisoformat(report["ended_at"].replace("Z", "+00:00"))
             - datetime.fromisoformat(report["started_at"].replace("Z", "+00:00"))).total_seconds(),
            3,
        )
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        shutil.rmtree(temp, ignore_errors=True)
    return return_code, report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("smoke", "representative", "catalog"), required=True)
    parser.add_argument("--format", choices=("json", "text", "ndjson"), default="json")
    parser.add_argument("--platform", choices=("windows", "linux", "both"))
    parser.add_argument("--report", type=Path)
    parser.add_argument("--offline", action="store_true", help="require already cached dependencies")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--worker", choices=("isolated",))
    parser.add_argument("--sample-id", action="append", default=[])
    parser.add_argument("--snapshot", type=Path, help="verified catalog snapshot directory for --suite catalog")
    parser.add_argument("--queue-db", type=Path, help="durable catalog queue path (defaults under .work)")
    parser.add_argument("--run-id", help="reuse an explicit stable catalog run ID")
    parser.add_argument("--shard", default="0/1", help="stable catalog partition as index/count")
    parser.add_argument("--resume", action="store_true", help="recover expired leases and reopen a persisted catalog plan")
    options = parser.parse_args(argv)
    if options.suite == "catalog":
        if not options.snapshot:
            parser.error("--suite catalog requires --snapshot")
        if options.manifest or options.worker or options.sample_id or options.offline:
            parser.error("--suite catalog does not accept --manifest, --worker, --sample-id, or --offline")
        try:
            from lab.catalog_run import CatalogRunError, create_catalog_plan, parse_shard, write_plan_report
            from lab.queue import QueueError
        except ModuleNotFoundError as error:
            if error.name != "lab":
                raise
            sys.path.insert(0, str(ROOT))
            from lab.catalog_run import CatalogRunError, create_catalog_plan, parse_shard, write_plan_report
            from lab.queue import QueueError
        try:
            shard_index, shard_count = parse_shard(options.shard)
            platform_option = options.platform or "both"
            platforms = ("windows", "linux") if platform_option == "both" else (platform_option,)
            queue_db = options.queue_db or ROOT / ".work/catalog-queue.sqlite3"
            plan = create_catalog_plan(
                options.snapshot,
                queue_db,
                platforms=platforms,
                shard_index=shard_index,
                shard_count=shard_count,
                run_id=options.run_id,
                resume=options.resume,
            )
            report_path = options.report or ROOT / ".work/catalog-runs" / plan["run_id"] / "plan.json"
            report_path = write_plan_report(plan, report_path)
        except (CatalogRunError, QueueError, OSError) as error:
            parser.exit(2, f"catalog plan failed: {error}\n")
        summary = {
            "schema_version": 1,
            "event": "catalog_plan",
            "suite": "catalog",
            "status": plan["status"],
            "run_state": plan["run_state"],
            "run_id": plan["run_id"],
            "snapshot_id": plan["snapshot_id"],
            "expected_units": plan["expected_units"],
            "selected_units": plan["selected_units"],
            "expected_stage_jobs": plan["expected_stage_jobs"],
            "job_counts": plan["job_counts"],
            "execution_started": plan["execution_started"],
            "queue_db": plan["queue_path"],
            "report": str(report_path),
        }
        print(render_summary_output(summary, options.format))
        return 0
    if options.suite == "representative":
        if options.platform == "both":
            parser.error("--platform both is only valid for --suite catalog")
        if not options.manifest:
            parser.error("--suite representative requires --manifest")
        if options.worker != "isolated":
            parser.error("--suite representative requires --worker isolated")
        try:
            from lab.representative import run_samples
        except ModuleNotFoundError:
            from representative import run_samples
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        report_path = options.report or ROOT / "outputs/acceptance" / f"t13-representative-{stamp}.json"
        code, report = run_samples(options.manifest, report_path, set(options.sample_id) or None)
        summary = {
            "schema_version": 1,
            "event": "run_summary",
            "suite": report["suite"], "status": report["status"], "report": str(report_path.resolve()),
            "sample_count": len(report.get("samples", [])), "l4_pass_count": report.get("l4_pass_count"),
            "gate": report.get("gate"), "error": report.get("error"),
        }
        print(render_summary_output(summary, options.format))
        return code
    if options.platform == "both":
        parser.error("--platform both is only valid for --suite catalog")
    platform_option = options.platform or available_platform()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    report_path = options.report or ROOT / "outputs/acceptance" / f"t12-smoke-{platform_option}-{stamp}.json"
    code, report = run_smoke(platform_option, report_path, options.offline or os.environ.get("CARGO_NET_OFFLINE") == "true")
    summary = {
        "schema_version": 1,
        "event": "run_summary",
        "suite": report["suite"],
        "status": report["status"],
        "platform": report["platform"],
        "report": str(report_path.resolve()),
        "error": report.get("error"),
        "events": len(report.get("events", [])),
        "gateway_operations": len(report.get("gateway_operations", [])),
        "mcp_client": report.get("mcp_client", {}).get("returncode") if report.get("mcp_client") else None,
        "remaining_pids": report["cleanup"].get("remaining_pids"),
    }
    print(render_summary_output(summary, options.format))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
