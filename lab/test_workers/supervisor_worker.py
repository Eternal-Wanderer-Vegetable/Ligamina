"""Small lifecycle worker for local supervisor integration tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time


def send(packet: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(packet, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def call_core(method: str, params: dict[str, object]) -> object:
    request_id = "supervisor-worker-core-1"
    send(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
    )
    for line in sys.stdin:
        response = json.loads(line)
        if response.get("id") == request_id:
            if "error" in response:
                raise RuntimeError(response["error"].get("message", "Core request failed"))
            return response.get("result")
    raise EOFError("Core closed before replying to the worker request")


def main() -> None:
    print("supervisor worker diagnostic", file=sys.stderr, flush=True)
    for line in sys.stdin:
        try:
            request = json.loads(line)
        except json.JSONDecodeError as error:
            print(f"invalid request: {error}", file=sys.stderr, flush=True)
            return

        method = request.get("method")
        request_id = request.get("id")
        params = request.get("params") or {}
        if method == "host.initialize":
            ready = os.environ.get("LIGAMINA_TEST_INIT_FAIL") != "1"
            result = {"ready": ready, "protocol_version": "0.1"}
        elif method == "host.call-core":
            result = call_core("core.test", params)
        elif method == "host.echo":
            result = {"value": params.get("value")}
        elif method == "host.spawn-child":
            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(600)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            result = {"pid": child.pid}
        elif method == "host.crash":
            os._exit(17)
        elif method == "host.shutdown":
            delay = float(os.environ.get("LIGAMINA_TEST_SHUTDOWN_DELAY_SECONDS", "0"))
            if delay > 0:
                time.sleep(delay)
            if request_id is not None:
                send({"jsonrpc": "2.0", "id": request_id, "result": {"stopping": True}})
            return
        else:
            if request_id is not None:
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32601, "message": "method not found"},
                    }
                )
            continue

        if request_id is not None:
            send({"jsonrpc": "2.0", "id": request_id, "result": result})


if __name__ == "__main__":
    main()
