"""Source-bound, Worker-local HTTPS fixture for one exact Bilibili image plugin."""

from __future__ import annotations

import base64
import argparse
import copy
import hashlib
import json
import ssl
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit


ROOT = Path(__file__).resolve().parents[2]
BILIBILI_ENTRY_ID = "nb-8cfb5e4d1632a591a9b8ec7a"
BILIBILI_ARTIFACT_SHA256 = "a9c165f99d4cf41a2364141050e4646a7223fab3c152aecc2f975739938fec20"
PROFILE_MANIFEST_PATH = ROOT / "lab/automation/data/p7-bilibili-image-https-v1.json"
EXPECTED_PROFILE_MANIFEST_SHA256 = "42a6d5af3e077240ee59ea173d7579111661d825eb9def795b900ae04259b91c"
BILIBILI_TLS_CERT_PATH = ROOT / "lab/automation/data/p7-bilibili-fixture-cert.pem"
BILIBILI_TLS_KEY_PATH = ROOT / "lab/automation/data/p7-bilibili-fixture-key.pem"
SOURCE_SELECTION_PATH = ROOT / "outputs/acceptance/p7-p704-independent-pilot-selection-20261111.json"
SOURCE_AUDIT_PATH = ROOT / "outputs/acceptance/p7-p704-nontrivial-unknown-source-audit-20261112.json"
COMMAND_IDS = ("B站封面", "专栏下载", "动态下载")
DEFAULT_SELECTION_OUTPUT = ROOT / "outputs/acceptance/p7-p704-bilibili-image-selection-20261112.json"


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _hash_bound_json(reference: dict[str, Any], *, label: str) -> tuple[Path, dict[str, Any]]:
    path = (ROOT / reference["path"]).resolve(strict=True)
    raw = path.read_bytes()
    if _sha256(raw) != reference.get("sha256"):
        raise ValueError(f"Bilibili fixture {label} SHA-256 changed")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"Bilibili fixture {label} must be a JSON object")
    return path, value


def validate_profile_manifest() -> dict[str, Any]:
    """Validate exact source, selection, route and TLS material before binding the fixture."""
    document = _read_json(PROFILE_MANIFEST_PATH)
    if _sha256(PROFILE_MANIFEST_PATH.read_bytes()) != EXPECTED_PROFILE_MANIFEST_SHA256:
        raise ValueError("Bilibili HTTPS fixture manifest hash changed")
    if (
        document.get("schema_version") != 1
        or document.get("kind") != "p7_bilibili_image_https_fixture"
        or document.get("execution_scope") != "linux_worker_only_network_disabled"
        or document.get("authority_scope") != "synthetic_bilibili_responses_only"
        or document.get("network_mode") != "none"
    ):
        raise ValueError("Bilibili HTTPS fixture scope is invalid")
    selection_path, selection = _hash_bound_json(document["source_selection"], label="selection")
    audit_path, audit = _hash_bound_json(document["source_audit"], label="source audit")
    if selection_path != SOURCE_SELECTION_PATH.resolve() or audit_path != SOURCE_AUDIT_PATH.resolve():
        raise ValueError("Bilibili HTTPS fixture is bound to unexpected source reports")
    sample = next((
        row for row in selection.get("samples", [])
        if isinstance(row, dict) and row.get("entry", {}).get("registry_entry_id") == BILIBILI_ENTRY_ID
    ), None)
    identity = sample.get("entry", {}) if isinstance(sample, dict) else {}
    if (
        sample is None
        or sample.get("ecosystem") != "nonebot"
        or identity.get("package") != document.get("package")
        or identity.get("version") != document.get("version")
        or identity.get("artifact_sha256") != document.get("artifact_sha256")
        or document.get("artifact_sha256") != BILIBILI_ARTIFACT_SHA256
    ):
        raise ValueError("Bilibili HTTPS fixture does not match its frozen selection artifact")
    audit_entry = next((
        row for row in audit.get("entries", [])
        if isinstance(row, dict) and row.get("registry_entry_id") == BILIBILI_ENTRY_ID
    ), None)
    if not isinstance(audit_entry, dict) or audit_entry.get("artifact_sha256") != BILIBILI_ARTIFACT_SHA256:
        raise ValueError("Bilibili HTTPS fixture source audit identity changed")
    audited_commands = {
        row.get("command_id"): row
        for row in audit_entry.get("commands", [])
        if isinstance(row, dict)
    }
    stimuli = document.get("stimuli")
    if not isinstance(stimuli, list) or [row.get("command_id") for row in stimuli] != list(COMMAND_IDS):
        raise ValueError("Bilibili HTTPS fixture must bind its exact three audited commands")
    for stimulus in stimuli:
        audit_command = audited_commands.get(stimulus["command_id"])
        if not isinstance(audit_command, dict) or audit_command.get("status") != "unknown":
            raise ValueError(f"Bilibili command is not an unknown source-audit row: {stimulus['command_id']}")
        expected_sources = {
            (row.get("path"), row.get("sha256"), tuple(row.get("lines", [])))
            for row in audit_command.get("source_refs", [])
            if isinstance(row, dict)
        }
        for source in stimulus.get("source_member_refs", []):
            key = (source.get("path"), source.get("sha256"), tuple(source.get("lines", [])))
            if key not in expected_sources:
                raise ValueError(f"Bilibili source lines differ from audit: {stimulus['command_id']}")
        cycle = stimulus.get("input_cycle")
        if not isinstance(cycle, list) or len(cycle) != 2 or any(not isinstance(item, str) or not item for item in cycle):
            raise ValueError(f"Bilibili stimulus needs two explicit inputs: {stimulus['command_id']}")

    for path_key, hash_key, expected_path in (
        ("tls_certificate_path", "tls_certificate_sha256", BILIBILI_TLS_CERT_PATH),
        ("tls_key_path", "tls_key_sha256", BILIBILI_TLS_KEY_PATH),
    ):
        referenced = ROOT / document[path_key]
        if referenced.resolve() != expected_path.resolve() or _sha256(referenced.read_bytes()) != document[hash_key]:
            raise ValueError(f"Bilibili fixture TLS material changed: {path_key}")
    aliases = document.get("host_aliases")
    expected_aliases = {"api.bilibili.com", "api.vc.bilibili.com", "www.bilibili.com", "i0.hdslb.com"}
    if not isinstance(aliases, list) or set(aliases) != expected_aliases or len(aliases) != len(expected_aliases):
        raise ValueError("Bilibili fixture host aliases changed")
    routes = document.get("routes")
    if not isinstance(routes, list) or len(routes) != 16:
        raise ValueError("Bilibili fixture route set changed")
    route_keys = set()
    for route in routes:
        if (
            not isinstance(route, dict)
            or route.get("method") != "GET"
            or route.get("host") not in expected_aliases
            or not isinstance(route.get("path"), str)
            or not isinstance(route.get("query"), dict)
            or route.get("status") != 200
            or not isinstance(route.get("content_type"), str)
            or ("body" in route) == ("body_base64" in route)
        ):
            raise ValueError("Bilibili fixture contains an invalid response route")
        key = (route["host"], route["path"], _canonical(route["query"]))
        if key in route_keys:
            raise ValueError("Bilibili fixture response routes are not unique")
        route_keys.add(key)
        if "body_base64" in route:
            try:
                base64.b64decode(route["body_base64"], validate=True)
            except Exception as error:
                raise ValueError("Bilibili fixture image body is not valid base64") from error
    return document


def profile_manifest_sha256() -> str:
    validate_profile_manifest()
    return _sha256(PROFILE_MANIFEST_PATH.read_bytes())


def profile_binding(candidate: dict[str, Any]) -> dict[str, Any] | None:
    """Bind synthetic HTTPS responses only to the exact locked Bilibili entry."""
    if candidate.get("ecosystem") != "nonebot" or candidate.get("registry_entry_id") != BILIBILI_ENTRY_ID:
        return None
    document = validate_profile_manifest()
    for key in ("registry_entry_id", "package", "version", "artifact_sha256"):
        if candidate.get(key) != document.get(key):
            raise ValueError(f"Bilibili HTTPS fixture artifact identity mismatch: {key}")
    payload = {**copy.deepcopy(document), "manifest_sha256": _sha256(PROFILE_MANIFEST_PATH.read_bytes())}
    return {**payload, "profile_sha256": _sha256(_canonical(payload))}


def validate_bound_profile(profile: Any, sample: Any) -> dict[str, Any]:
    """Revalidate profile hash and locked plugin identity inside the Worker."""
    if not isinstance(profile, dict) or not isinstance(sample, dict):
        raise ValueError("Bilibili HTTPS profile and sample must be objects")
    payload = {key: value for key, value in profile.items() if key != "profile_sha256"}
    if not isinstance(profile.get("profile_sha256"), str) or _sha256(_canonical(payload)) != profile["profile_sha256"]:
        raise ValueError("Bilibili HTTPS fixture profile hash mismatch")
    if profile.get("manifest_sha256") != profile_manifest_sha256():
        raise ValueError("Bilibili HTTPS fixture manifest hash mismatch")
    identity = {
        "registry_entry_id": str(sample.get("sample_id", "")).removeprefix("p7-"),
        "ecosystem": sample.get("ecosystem"),
        "package": sample.get("package"),
        "version": sample.get("version"),
        "artifact_sha256": sample.get("_artifact_hash"),
    }
    if profile_binding(identity) != profile:
        raise ValueError("Bilibili HTTPS profile does not match the installed sample artifact")
    return copy.deepcopy(profile)


def build_selection_document() -> dict[str, Any]:
    """Derive a one-entry diagnostic selection without changing the frozen 30+30 cohort."""
    profile = validate_profile_manifest()
    _, selection = _hash_bound_json(profile["source_selection"], label="selection")
    samples = [
        copy.deepcopy(row) for row in selection.get("samples", [])
        if isinstance(row, dict) and row.get("entry", {}).get("registry_entry_id") == BILIBILI_ENTRY_ID
    ]
    if len(samples) != 1:
        raise ValueError("frozen source selection must contain exactly one Bilibili artifact row")
    document = copy.deepcopy(selection)
    document["samples"] = samples
    document["selection"]["per_ecosystem_target"] = 1
    document["selection"]["counts"] = {"koishi": 0, "nonebot": 1}
    document["selection"]["rule"] = "Exact Bilibili entry filtered from the frozen hash-ranked P7-04 selection; diagnostic only."
    document["source_selection"] = copy.deepcopy(profile["source_selection"])
    document["diagnostic_scope"] = {
        "kind": "p7_source_bound_bilibili_image_diagnostic",
        "profile_manifest_path": PROFILE_MANIFEST_PATH.relative_to(ROOT).as_posix(),
        "profile_manifest_sha256": profile_manifest_sha256(),
        "source_audit_path": profile["source_audit"]["path"],
        "source_audit_sha256": profile["source_audit"]["sha256"],
    }
    validate_selection_document(document)
    return document


def validate_selection_document(document: dict[str, Any]) -> None:
    profile = validate_profile_manifest()
    scope = document.get("diagnostic_scope", {})
    if (
        document.get("kind") != "p7_p701_static_inventory_sample"
        or scope.get("kind") != "p7_source_bound_bilibili_image_diagnostic"
        or scope.get("profile_manifest_path") != PROFILE_MANIFEST_PATH.relative_to(ROOT).as_posix()
        or scope.get("profile_manifest_sha256") != profile_manifest_sha256()
        or scope.get("source_audit_path") != profile["source_audit"]["path"]
        or scope.get("source_audit_sha256") != profile["source_audit"]["sha256"]
        or document.get("source_selection") != profile["source_selection"]
        or document.get("selection", {}).get("counts") != {"koishi": 0, "nonebot": 1}
    ):
        raise ValueError("Bilibili diagnostic selection has an invalid frozen-source binding")
    _, source_selection = _hash_bound_json(profile["source_selection"], label="selection")
    expected = [
        row for row in source_selection.get("samples", [])
        if row.get("entry", {}).get("registry_entry_id") == BILIBILI_ENTRY_ID
    ]
    if document.get("samples") != expected:
        raise ValueError("Bilibili diagnostic selection entry differs from the frozen 30+30 source")


def stimulus_profile_sha256() -> str:
    validate_profile_manifest()
    return _sha256(PROFILE_MANIFEST_PATH.read_bytes())


def stimulus_for_command(command_id: str) -> dict[str, Any]:
    profile = validate_profile_manifest()
    row = next((item for item in profile["stimuli"] if item["command_id"] == command_id), None)
    if row is None:
        raise ValueError(f"Bilibili diagnostic command is not allowlisted: {command_id}")
    return {
        "ecosystem": "nonebot",
        "registry_entry_id": BILIBILI_ENTRY_ID,
        "package": profile["package"],
        "version": profile["version"],
        "artifact_sha256": profile["artifact_sha256"],
        **copy.deepcopy(row),
    }


class _BilibiliFixtureServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler], tls: ssl.SSLContext) -> None:
        super().__init__(address, handler)
        self._tls = tls

    def get_request(self):
        connection, address = super().get_request()
        return self._tls.wrap_socket(connection, server_side=True), address


class BilibiliImageHttpsFixture:
    """Serve the profile's HTTPS JSON, page and image responses on container loopback."""

    def __init__(self, profile: dict[str, Any], *, port_override: int | None = None) -> None:
        sample = {
            "sample_id": f"p7-{profile.get('registry_entry_id', '')}",
            "ecosystem": "nonebot",
            "package": profile.get("package"),
            "version": profile.get("version"),
            "_artifact_hash": profile.get("artifact_sha256"),
        }
        self.profile = validate_bound_profile(profile, sample)
        if port_override is not None and (
            isinstance(port_override, bool) or not isinstance(port_override, int) or not 0 <= port_override <= 65535
        ):
            raise ValueError("Bilibili fixture port override must be between 0 and 65535")
        self.port = self.profile["fixture_port"] if port_override is None else port_override
        self._lock = threading.Lock()
        self._route_counts: Counter[str] = Counter()
        self._unmatched = 0
        self._server: _BilibiliFixtureServer | None = None
        self._thread: threading.Thread | None = None
        self._route_lookup = {
            (row["host"], row["path"], _canonical(_query_values(row["query"]))): row
            for row in self.profile["routes"]
        }

    @property
    def bound_port(self) -> int:
        if self._server is None:
            raise RuntimeError("Bilibili HTTPS fixture has not started")
        return int(self._server.server_address[1])

    def start(self) -> None:
        if self._server is not None:
            raise RuntimeError("Bilibili HTTPS fixture is already started")
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(str(ROOT / self.profile["tls_certificate_path"]), str(ROOT / self.profile["tls_key_path"]))
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                fixture._serve(self)

            def log_message(self, format: str, *args: Any) -> None:
                return

        self._server = _BilibiliFixtureServer(
            (self.profile["fixture_address"], self.port), Handler, tls,
        )
        self._thread = threading.Thread(target=self._server.serve_forever, name="p7-bilibili-https", daemon=True)
        self._thread.start()

    def _serve(self, handler: BaseHTTPRequestHandler) -> None:
        host_header = handler.headers.get("Host", "").casefold()
        host = host_header.rsplit(":", 1)[0] if host_header.count(":") == 1 else host_header
        parsed = urlsplit(handler.path)
        query = _group_query(parse_qsl(parsed.query, keep_blank_values=True))
        route = self._route_lookup.get((host, parsed.path, _canonical(query)))
        if route is None:
            with self._lock:
                self._unmatched += 1
            handler.send_error(404)
            return
        body = (
            base64.b64decode(route["body_base64"], validate=True)
            if "body_base64" in route
            else route["body"].encode("utf-8")
        )
        with self._lock:
            self._route_counts[route["id"]] += 1
        handler.send_response(route["status"])
        handler.send_header("Content-Type", route["content_type"])
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        handler.wfile.write(body)

    def evidence(self) -> dict[str, Any]:
        with self._lock:
            counts = dict(sorted(self._route_counts.items()))
            unmatched = self._unmatched
        return {
            "profile_id": self.profile["profile_id"],
            "profile_sha256": self.profile["profile_sha256"],
            "network_mode": "none",
            "bind_address": self.profile["fixture_address"],
            "request_count": sum(counts.values()) + unmatched,
            "route_counts": counts,
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
                raise RuntimeError("Bilibili HTTPS fixture server did not stop")
        self._server = None
        self._thread = None


def _group_query(rows: list[tuple[str, str]]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for key, value in rows:
        grouped.setdefault(key, []).append(value)
    return grouped


def _query_values(query: dict[str, Any]) -> dict[str, list[str]]:
    return {
        str(key): [str(value)] if not isinstance(value, list) else [str(item) for item in value]
        for key, value in query.items()
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_SELECTION_OUTPUT)
    options = parser.parse_args(argv)
    output = options.output.expanduser().absolute()
    if output.exists():
        parser.error(f"refusing to overwrite an existing Bilibili diagnostic selection: {output}")
    selection = build_selection_document()
    output.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(selection, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    output.write_bytes(raw)
    print(json.dumps({
        "status": "created",
        "output": output.as_posix(),
        "sha256": _sha256(raw),
        "registry_entry_id": BILIBILI_ENTRY_ID,
        "counts_toward_cohort_gate": False,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
