"""Hash-bound, Worker-local HTTP fixtures for narrowly scoped P7 scenarios."""

from __future__ import annotations

import copy
import hashlib
import json
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
PROFILE_MANIFEST_PATH = ROOT / "lab/automation/data/p7-nonebot-warframe-api-loopback-v1.json"
EXPECTED_PROFILE_MANIFEST_SHA256 = "5ed90faffafbfbe31f3f9f59c5fe352d2e2e3a6248841b4a7553cf545d4c06e1"
EXPECTED_SOURCE_EVIDENCE = [
    {
        "path": "nonebot_plugin_warframe_mode/data_source.py",
        "sha256": "d59b32607e5aa13426e1c82ca7a0e3f4f59e1284672914e05c9348aa5832bfba",
        "lines": [3, 4, 5, 6, 7],
    },
    {
        "path": "nonebot_plugin_warframe_mode/__init__.py",
        "sha256": "0e8c9fc201839d1bcfb7e0f28731c9cd158e919b78f56625adc81d44b7937ed7",
        "lines": [395, 396, 408, 422, 423, 435, 449, 450],
    },
]


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def validate_profile_manifest() -> dict[str, Any]:
    """Validate the exact committed manifest and the source report it cites."""
    document = _read_json(PROFILE_MANIFEST_PATH)
    if document.get("source_evidence") != EXPECTED_SOURCE_EVIDENCE:
        raise ValueError("P7 loopback service source hashes differ from the reviewed wheel evidence")
    if _sha256(PROFILE_MANIFEST_PATH.read_bytes()) != EXPECTED_PROFILE_MANIFEST_SHA256:
        raise ValueError("P7 loopback service manifest hash differs from the reviewed profile")
    source_report = document["source_report"]
    report_path = ROOT / source_report["path"]
    if not report_path.is_file() or _sha256(report_path.read_bytes()) != source_report["sha256"]:
        raise ValueError("P7 loopback service source report hash changed")
    prefixes = [row["path_prefix"] for row in document["routes"]]
    if len(set(prefixes)) != len(prefixes):
        raise ValueError("P7 loopback service route prefixes must be unique")
    return document


def profile_manifest_sha256() -> str:
    """Return the exact bytes hash of the validated fixture manifest."""
    validate_profile_manifest()
    return _sha256(PROFILE_MANIFEST_PATH.read_bytes())


def profile_binding(candidate: dict[str, Any]) -> dict[str, Any] | None:
    """Bind the loopback fixture only to its reviewed package version and artifact."""
    if candidate.get("ecosystem") != "nonebot":
        return None
    document = validate_profile_manifest()
    entry_id = candidate.get("registry_entry_id")
    if entry_id != document["registry_entry_id"]:
        return None
    identity = {
        "registry_entry_id": entry_id,
        "package": candidate.get("package"),
        "version": candidate.get("version"),
        "artifact_sha256": candidate.get("artifact_sha256"),
    }
    expected = {key: document[key] for key in identity}
    if identity != expected:
        raise ValueError(f"P7 loopback fixture artifact identity mismatch: {entry_id}")
    payload = {
        **copy.deepcopy(document),
        "manifest_sha256": _sha256(PROFILE_MANIFEST_PATH.read_bytes()),
    }
    return {**payload, "profile_sha256": _sha256(_canonical(payload))}


def validate_bound_profile(profile: Any, sample: Any) -> dict[str, Any]:
    """Revalidate the profile hash and exact sample identity inside each Worker."""
    if not isinstance(profile, dict) or not isinstance(sample, dict):
        raise ValueError("P7 loopback service profile and sample must be objects")
    payload = {key: value for key, value in profile.items() if key != "profile_sha256"}
    if not isinstance(profile.get("profile_sha256"), str) or _sha256(_canonical(payload)) != profile["profile_sha256"]:
        raise ValueError("P7 loopback service profile hash mismatch")
    if profile.get("manifest_sha256") != profile_manifest_sha256():
        raise ValueError("P7 loopback service manifest hash mismatch")
    sample_id = str(sample.get("sample_id", "")).removeprefix("p7-")
    expected = profile_binding({
        "ecosystem": sample.get("ecosystem", "nonebot"),
        "registry_entry_id": sample_id,
        "package": sample.get("package"),
        "version": sample.get("version"),
        "artifact_sha256": sample.get("_artifact_hash"),
    })
    if expected is None or expected != profile:
        raise ValueError("P7 loopback service profile does not match the installed sample artifact")
    return copy.deepcopy(profile)


class LoopbackApiFixture:
    """Serve deterministic text responses on loopback and retain only route counts."""

    def __init__(self, profile: dict[str, Any], *, port_override: int | None = None) -> None:
        sample = {
            "sample_id": f"p7-{profile.get('registry_entry_id', '')}",
            "package": profile.get("package"),
            "version": profile.get("version"),
            "_artifact_hash": profile.get("artifact_sha256"),
        }
        self.profile = validate_bound_profile(profile, sample)
        if port_override is not None and (
            isinstance(port_override, bool) or not isinstance(port_override, int) or not 0 <= port_override <= 65535
        ):
            raise ValueError("loopback fixture port override must be an integer from 0 through 65535")
        self.port = self.profile["fixture_port"] if port_override is None else port_override
        self._lock = threading.Lock()
        self._route_counts: Counter[str] = Counter()
        self._unmatched_requests = 0
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def bound_port(self) -> int:
        if self._server is None:
            raise RuntimeError("loopback fixture has not started")
        return int(self._server.server_address[1])

    def start(self) -> None:
        if self._server is not None:
            raise RuntimeError("loopback fixture has already started")
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler callback name
                route_id = next((
                    route["id"] for route in fixture.profile["routes"]
                    if urlsplit(self.path).path.startswith(route["path_prefix"])
                ), None)
                body = b""
                if route_id is None:
                    status = 404
                    with fixture._lock:
                        fixture._unmatched_requests += 1
                else:
                    status = 200
                    with fixture._lock:
                        fixture._route_counts[route_id] += 1
                    body = f"{fixture.profile['response_body']}:{route_id}\n".encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                if body:
                    self.wfile.write(body)
                self.close_connection = True

            def log_message(self, _format: str, *_args: Any) -> None:
                # Request paths can contain generated query text; never write them to logs.
                return

        class Server(ThreadingHTTPServer):
            allow_reuse_address = False
            daemon_threads = True
            request_queue_size = 8

        self._server = Server((self.profile["fixture_address"], self.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, name="p7-loopback-api", daemon=True)
        self._thread.start()

    def evidence(self) -> dict[str, Any]:
        with self._lock:
            route_counts = dict(sorted(self._route_counts.items()))
            unmatched = self._unmatched_requests
        return {
            "profile_id": self.profile["profile_id"],
            "profile_sha256": self.profile["profile_sha256"],
            "network_mode": "none",
            "bind_address": self.profile["fixture_address"],
            "request_count": sum(route_counts.values()) + unmatched,
            "route_counts": route_counts,
            "unmatched_request_count": unmatched,
        }

    def close(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
            if self._thread.is_alive():
                raise RuntimeError("P7 loopback fixture server did not stop")
        self._server = None
        self._thread = None
