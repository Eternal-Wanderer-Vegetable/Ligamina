"""Bidirectional JSON-RPC/NDJSON stdio worker for the NoneBot Host."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from typing import Any

from .contracts import event_error_code
from .capabilities import CapabilityError, capability_descriptors, invoke_capability
from .lifecycle import HostLifecycle, configured_plugins

PROTOCOL_VERSION = "0.1"
MAX_FRAME_BYTES = 1024 * 1024


class Worker:
    def __init__(self) -> None:
        self._write_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._inbound: dict[str, asyncio.Task[Any]] = {}
        self._lifecycle: HostLifecycle | None = None
        self._host_id: str | None = None
        self._epoch: int | None = None
        self._ready = False
        self._draining = False
        self._stopping = False

    async def run(self) -> None:
        while True:
            line = await asyncio.to_thread(sys.stdin.buffer.readline, MAX_FRAME_BYTES + 1)
            if not line:
                break
            if len(line) > MAX_FRAME_BYTES or not line.endswith(b"\n"):
                print("Host received an oversized or unterminated NDJSON frame", file=sys.stderr, flush=True)
                break
            try:
                packet = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                print(f"Host received invalid JSON: {error}", file=sys.stderr, flush=True)
                break
            if not isinstance(packet, dict) or packet.get("jsonrpc") != "2.0":
                print("Host received a non-JSON-RPC packet", file=sys.stderr, flush=True)
                break
            if isinstance(packet.get("method"), str):
                await self._receive_request(packet)
            else:
                self._receive_response(packet)
            if self._stopping:
                break

        self._stopping = True
        for task in self._inbound.values():
            task.cancel()
        if self._inbound:
            await asyncio.gather(*self._inbound.values(), return_exceptions=True)
        if self._lifecycle is not None:
            await self._lifecycle.stop()
        self._fail_pending(EOFError("Core stdio closed"))

    async def _receive_request(self, packet: dict[str, Any]) -> None:
        method = packet["method"]
        params = packet.get("params") or {}
        if method == "request.cancel":
            request_id = params.get("request_id") if isinstance(params, dict) else None
            task = self._inbound.get(request_id) if isinstance(request_id, str) else None
            if task is not None:
                task.cancel()
            return
        request_id = packet.get("id")
        if not isinstance(request_id, str):
            return
        if method in {"runtime.stop", "host.shutdown"}:
            await self._handle_request(request_id, method, params)
            return
        task = asyncio.create_task(self._handle_request(request_id, method, params))
        self._inbound[request_id] = task
        task.add_done_callback(lambda _task, key=request_id: self._inbound.pop(key, None))

    async def _handle_request(self, request_id: str, method: str, params: Any) -> None:
        try:
            if not isinstance(params, dict):
                raise WorkerError("invalid_request", "params must be an object")
            result = await self._dispatch(method, params)
            await self._send({"jsonrpc": "2.0", "id": request_id, "result": result})
        except asyncio.CancelledError:
            raise
        except WorkerError as error:
            await self._send({
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32000, "message": error.message, "data": {"code": error.code}},
            })
        except Exception as error:
            print(f"Host request {method!r} failed: {error!r}", file=sys.stderr, flush=True)
            await self._send({
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32000, "message": str(error)},
            })
        if method in {"runtime.stop", "host.shutdown"}:
            self._stopping = True

    async def _dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method == "host.initialize":
            if params.get("protocol_version") != PROTOCOL_VERSION:
                raise WorkerError("protocol_mismatch", "unsupported Host protocol version")
            host_id = params.get("host_id")
            epoch = params.get("epoch")
            if not isinstance(host_id, str) or not host_id.strip() or not isinstance(epoch, int) or isinstance(epoch, bool):
                raise WorkerError("invalid_request", "host_id and integer epoch are required")
            if self._ready:
                raise WorkerError("already_initialized", "Host has already initialized")
            self._host_id, self._epoch = host_id, epoch
            self._lifecycle = HostLifecycle(
                host_id=host_id,
                platform_call=self._platform_call,
                service_call=self._service_call,
            )
            await self._lifecycle.start(configured_plugins())
            self._ready = True
            return {
                "ready": True,
                "protocol_version": PROTOCOL_VERSION,
                "capabilities": capability_descriptors(),
            }
        if method == "event.dispatch":
            self._require_ready()
            envelope = params.get("event", params)
            code = event_error_code(envelope)
            if code:
                raise WorkerError(code, "event envelope is invalid for this Host")
            assert self._lifecycle is not None and self._lifecycle.adapter is not None
            await self._lifecycle.adapter.dispatch(envelope)
            return {"accepted": True, "event_id": envelope["event_id"]}
        if method == "capability.invoke":
            self._require_ready()
            capability_id = params.get("capability_id")
            if not isinstance(capability_id, str):
                raise WorkerError("invalid_request", "capability_id must be a string")
            try:
                return invoke_capability(capability_id, params.get("arguments"))
            except CapabilityError as error:
                raise WorkerError(error.code, str(error)) from error
        if method == "runtime.health":
            return {
                "ready": self._ready and not self._stopping,
                "host_id": self._host_id,
                "epoch": self._epoch,
                "protocol_version": PROTOCOL_VERSION,
            }
        if method == "runtime.capabilities":
            self._require_ready()
            return {"capabilities": capability_descriptors()}
        if method in {"runtime.stop", "host.shutdown"}:
            self._draining = True
            current = asyncio.current_task()
            active = [task for task in self._inbound.values() if task is not current]
            if active:
                await asyncio.gather(*active, return_exceptions=True)
            if self._lifecycle is not None:
                await self._lifecycle.stop()
                self._ready = False
            return {"stopping": True}
        raise WorkerError("method_not_found", f"unsupported Host method: {method}")

    def _require_ready(self, *, allow_draining: bool = False) -> None:
        if not self._ready or self._stopping or (self._draining and not allow_draining):
            raise WorkerError("host_not_ready", "Host is not ready")

    async def _platform_call(self, operation: dict[str, Any]) -> dict[str, Any]:
        self._require_ready(allow_draining=True)
        request_id = f"platform-{uuid.uuid4()}"
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send({"jsonrpc": "2.0", "id": request_id, "method": "platform.execute", "params": operation})
            return await future
        finally:
            self._pending.pop(request_id, None)

    async def _service_call(self, invocation: dict[str, Any]) -> Any:
        self._require_ready()
        request_id = f"service-{uuid.uuid4()}"
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send({"jsonrpc": "2.0", "id": request_id, "method": "service.invoke", "params": invocation})
            return await future
        finally:
            self._pending.pop(request_id, None)

    def _receive_response(self, packet: dict[str, Any]) -> None:
        request_id = packet.get("id")
        if not isinstance(request_id, str):
            print("Host ignored response without a string id", file=sys.stderr, flush=True)
            return
        future = self._pending.get(request_id)
        if future is None or future.done():
            return
        if "error" in packet:
            error = packet["error"]
            future.set_exception(RuntimeError(str(error.get("message", "platform.execute failed"))))
        else:
            result = packet.get("result")
            if request_id.startswith("platform-") and not isinstance(result, dict):
                future.set_exception(RuntimeError("platform.execute result must be an object"))
            else:
                future.set_result(result)

    async def _send(self, packet: dict[str, Any]) -> None:
        frame = (json.dumps(packet, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        if len(frame) > MAX_FRAME_BYTES:
            raise WorkerError("frame_too_large", "outgoing NDJSON frame exceeds 1 MiB")
        async with self._write_lock:
            sys.stdout.buffer.write(frame)
            sys.stdout.buffer.flush()

    def _fail_pending(self, error: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)


class WorkerError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def main() -> None:
    asyncio.run(Worker().run())


if __name__ == "__main__":
    main()
