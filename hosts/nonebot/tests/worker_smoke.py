"""End-to-end stdio protocol probe for NoneBot Host lifecycle and gateway calls."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

HOST_DIR = Path(__file__).resolve().parents[1]


def send(process: subprocess.Popen[bytes], packet: dict) -> None:
    assert process.stdin is not None
    process.stdin.write((json.dumps(packet, separators=(",", ":")) + "\n").encode())
    process.stdin.flush()


def receive(process: subprocess.Popen[bytes]) -> dict:
    assert process.stdout is not None
    line = process.stdout.readline()
    if not line:
        stderr = process.stderr.read().decode(errors="replace") if process.stderr else ""
        raise AssertionError(f"Host closed stdout unexpectedly: {stderr}")
    packet = json.loads(line)
    assert packet.get("jsonrpc") == "2.0", packet
    return packet


def request(process: subprocess.Popen[bytes], method: str, params: dict, *, expect_error: bool = False):
    request_id = str(uuid.uuid4())
    send(process, {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
    while True:
        packet = receive(process)
        if packet.get("method") == "platform.execute":
            operation = packet["params"]
            if operation["operation"] == "get_message":
                result = {
                    "status": "succeeded",
                    "data": {
                        "target": operation["target"],
                        "message_id": "77",
                        "real_id": "77",
                        "actor_id": "9000",
                        "timestamp": "2026-10-10T01:02:03Z",
                        "message": [{"type": "text", "text": "previous"}],
                        "nickname": "Bot",
                    },
                }
            else:
                result = {"status": "succeeded", "platform_message_id": "501"}
            send(process, {"jsonrpc": "2.0", "id": packet["id"], "result": result})
            continue
        if packet.get("id") == request_id:
            if "error" in packet:
                if expect_error:
                    return packet["error"]
                raise AssertionError(f"Host request failed: {packet['error']}")
            return packet["result"]


def main() -> None:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(HOST_DIR)
    environment["LIGAMINA_NONEBOT_PLUGINS"] = "ligamina_nonebot.bridge_smoke_plugin"
    environment["LIGAMINA_NONEBOT_NICKNAMES"] = "Bot"
    process = subprocess.Popen(
        [sys.executable, "-m", "ligamina_nonebot.worker"],
        cwd=HOST_DIR,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        initialized = request(process, "host.initialize", {
            "host_id": "nonebot-worker-test",
            "epoch": 1,
            "protocol_version": "0.1",
        })
        assert initialized["ready"] is True
        assert initialized["protocol_version"] == "0.1"
        assert initialized["capabilities"] == [{
            "capability_id": "ligamina.nonebot.math.add",
            "input_schema": {
                "type": "object",
                "properties": {
                    "left": {"type": "integer", "minimum": -9007199254740991, "maximum": 9007199254740991},
                    "right": {"type": "integer", "minimum": -9007199254740991, "maximum": 9007199254740991},
                },
                "required": ["left", "right"],
                "additionalProperties": False,
            },
            "output_schema": {"type": "integer", "minimum": -9007199254740991, "maximum": 9007199254740991},
            "pure": True,
        }]
        assert request(process, "capability.invoke", {
            "capability_id": "ligamina.nonebot.math.add",
            "arguments": {"left": 19, "right": 23},
        }) == 42
        invalid = request(process, "capability.invoke", {
            "capability_id": "ligamina.nonebot.math.add",
            "arguments": {"left": "19", "right": 23},
        }, expect_error=True)
        assert invalid["data"]["code"] == "invalid_request"
        unsupported = request(process, "capability.invoke", {
            "capability_id": "ligamina.nonebot.unmapped",
            "arguments": {},
        }, expect_error=True)
        assert unsupported["data"]["code"] == "unsupported_capability"
        assert request(process, "event.dispatch", {
            "schema_version": "ligamina/0.1",
            "event_id": "worker-event-1",
            "trace_id": "worker-trace-1",
            "platform": "onebot.v11",
            "account_id": "9000",
            "conversation": {"kind": "group", "id": "42"},
            "actor_id": "23",
            "timestamp": "2026-10-10T01:02:03Z",
            "target_host": "nonebot-worker-test",
            "message": [
                {"type": "reply", "message_id": "77"},
                {"type": "mention", "user_id": "9000"},
                {"type": "text", "text": "ping"},
            ],
            "extensions": {"onebot.v11": {"message_id": "78"}},
        }) == {"accepted": True, "event_id": "worker-event-1"}
        assert request(process, "runtime.health", {})["ready"] is True
        assert request(process, "host.shutdown", {}) == {"stopping": True}
        assert process.stdin is not None
        process.stdin.close()
        assert process.wait(timeout=10) == 0
        print("NoneBot stdio worker: initialize, declared capability, dispatch, reverse platform.execute, health and stop passed")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if process.stderr:
            process.stderr.close()
        if process.stdout:
            process.stdout.close()
        if process.stdin:
            process.stdin.close()


if __name__ == "__main__":
    main()
