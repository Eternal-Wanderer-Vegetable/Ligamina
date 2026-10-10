"""Cross-language protocol worker used by ligamina-core integration tests."""

from __future__ import annotations

import itertools
import json
import sys
import threading
import time
from typing import Any

_stdout_lock = threading.Lock()
_pending_lock = threading.Lock()
_pending: dict[str, tuple[threading.Event, dict[str, Any]]] = {}
_request_ids = itertools.count(1)


def send(packet: dict[str, Any]) -> None:
    frame = json.dumps(packet, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
    with _stdout_lock:
        sys.stdout.buffer.write(frame)
        sys.stdout.buffer.flush()


def call_core(method: str, params: dict[str, Any]) -> Any:
    request_id = f"h-{next(_request_ids)}"
    event = threading.Event()
    response: dict[str, Any] = {}
    with _pending_lock:
        _pending[request_id] = (event, response)
    send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
    if not event.wait(5):
        with _pending_lock:
            _pending.pop(request_id, None)
        raise TimeoutError(f"core did not answer {request_id}")
    if "error" in response:
        raise RuntimeError(response["error"].get("message", "core request failed"))
    return response.get("result")


def dispatch(packet: dict[str, Any]) -> None:
    request_id = packet["id"]
    method = packet["method"]
    params = packet.get("params") or {}
    if method == "host.echo":
        value = params.get("value")
        reverse = call_core("core.reverse", {"value": value})
        result = {"worker": "python", "echo": value, "reverse": reverse}
    elif method == "host.delay":
        time.sleep(float(params.get("milliseconds", 0)) / 1000)
        result = {"worker": "python", "done": True}
    elif method == "host.malformed":
        with _stdout_lock:
            sys.stdout.buffer.write(b"{not-json}\n")
            sys.stdout.buffer.flush()
        return
    elif method == "host.oversize":
        frame = json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "result": {"padding": "x" * (1024 * 1024 + 32)}},
            separators=(",", ":"),
        )
        with _stdout_lock:
            sys.stdout.write(frame + "\n")
            sys.stdout.flush()
        return
    else:
        send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "method not found"}})
        return
    send({"jsonrpc": "2.0", "id": request_id, "result": result})


def main() -> None:
    print("python worker diagnostic on stderr", file=sys.stderr, flush=True)
    for raw_line in sys.stdin.buffer:
        if not raw_line.endswith(b"\n"):
            print("incomplete frame", file=sys.stderr, flush=True)
            return
        try:
            packet = json.loads(raw_line)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            print(f"bad inbound frame: {error}", file=sys.stderr, flush=True)
            return
        if packet.get("jsonrpc") != "2.0":
            print("bad JSON-RPC version", file=sys.stderr, flush=True)
            return
        if isinstance(packet.get("method"), str):
            if "id" in packet:
                threading.Thread(target=dispatch, args=(packet,), daemon=True).start()
            continue
        response_id = packet.get("id")
        with _pending_lock:
            pending = _pending.pop(response_id, None)
        if pending is not None:
            event, response = pending
            response.update(packet)
            event.set()


if __name__ == "__main__":
    main()
