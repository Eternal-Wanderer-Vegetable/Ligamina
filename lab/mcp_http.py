"""Minimal MCP Streamable HTTP client (stdlib only) for the network-disabled Worker.

Implements just what tool-surface tests need: initialize, tools/list and tools/call
against the local Core `/mcp` endpoint with a bearer token. Responses may arrive as
plain JSON or as a server-sent event stream; both are parsed.
"""

from __future__ import annotations

import itertools
import json
import threading
import urllib.error
import urllib.request
from typing import Any

PROTOCOL_VERSION = "2025-11-25"


class McpError(RuntimeError):
    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class McpHttpClient:
    def __init__(self, url: str, token: str, *, timeout: float = 60.0) -> None:
        self.url = url
        self.token = token
        self.timeout = timeout
        self.session_id: str | None = None
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def initialize(self) -> dict[str, Any]:
        result = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "ligamina-tool-surface-acceptance", "version": "0.1.0"},
        })
        if result.get("protocolVersion") != PROTOCOL_VERSION:
            raise McpError(f"server negotiated {result.get('protocolVersion')!r}")
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, expect_body=False)
        return result

    def list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            result = self._request("tools/list", {"cursor": cursor} if cursor else {})
            tools.extend(result.get("tools", []))
            cursor = result.get("nextCursor")
            if not cursor:
                return tools

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._request("tools/call", {"name": name, "arguments": arguments})

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            request_id = next(self._ids)
        response = self._post({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        for message in response:
            if message.get("id") != request_id:
                continue
            if "error" in message:
                error = message["error"]
                raise McpError(str(error.get("message")), code=error.get("code"))
            result = message.get("result")
            if not isinstance(result, dict):
                raise McpError(f"{method} returned a non-object result")
            return result
        raise McpError(f"{method} returned no response for request {request_id}")

    def _post(self, body: dict[str, Any], *, expect_body: bool = True) -> list[dict[str, Any]]:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        request = urllib.request.Request(
            self.url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                session = response.headers.get("Mcp-Session-Id")
                if session:
                    self.session_id = session
                content_type = response.headers.get("Content-Type", "")
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            raise McpError(f"HTTP {error.code}: {error.read()[:500]!r}", code=error.code) from error
        if not expect_body or not raw.strip():
            return []
        if "text/event-stream" in content_type:
            return _parse_sse(raw)
        value = json.loads(raw)
        return value if isinstance(value, list) else [value]


def _parse_sse(raw: str) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    data: list[str] = []
    for line in raw.splitlines() + [""]:
        if line.startswith("data:"):
            data.append(line[5:].lstrip())
        elif not line.strip() and data:
            payload = "\n".join(data).strip()
            data = []
            if payload:
                try:
                    value = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    messages.append(value)
    return messages
