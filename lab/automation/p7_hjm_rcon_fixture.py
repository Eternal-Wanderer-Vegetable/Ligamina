"""Hash-bound synthetic RCON profile and Worker-local Source RCON fixture for P7."""

from __future__ import annotations

import copy
import hashlib
import json
import socketserver
import struct
import tarfile
import threading
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PROFILE_MANIFEST_PATH = ROOT / "lab/automation/data/p7-hjm-rcon-v1.json"
EXPECTED_PROFILE_MANIFEST_SHA256 = "d38346a07ab9a97967ad83c1f0ee0afb46a0f6ab1252fa2184a5680ca2854eba"
SOURCE_SELECTION_PATH = ROOT / "outputs/acceptance/p7-p704-independent-pilot-selection-20261111.json"
DIAGNOSTIC_SELECTION_PATH = ROOT / "outputs/acceptance/p7-p704-hjm-rcon-diagnostic-selection-20261112.json"
EXPECTED_IDENTITIES = {
    "ko-68ea4b6fd60ea7b3a207c402": {
        "package": "koishi-plugin-hjm-server-query",
        "version": "1.0.3",
        "artifact_sha256": "70407f95613efa823ef643c1314b723afcebaec1489e78f396b240a3cc3aa541",
    },
    "ko-fcd188bbb6d1c2f93bd5b727": {
        "package": "koishi-plugin-hjm-server-query-plus",
        "version": "1.1.1",
        "artifact_sha256": "9154c5fbf078958d86c5b1263bcf8e482434e5f7e26b8dea7d44aff9c59ae2f6",
    },
}


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _safe_repo_file(relative_path: str) -> Path:
    path = (ROOT / relative_path).resolve(strict=True)
    if not path.is_relative_to(ROOT.resolve()):
        raise ValueError("P7 HJM evidence path escapes the repository")
    return path


def _artifact_member_bytes(archive_path: Path, member_path: str) -> bytes:
    with tarfile.open(archive_path, "r:*") as archive:
        matches = [name for name in archive.getnames() if name.endswith(member_path)]
        if len(matches) != 1:
            raise ValueError(f"P7 HJM source archive member is missing or ambiguous: {member_path}")
        member = archive.extractfile(matches[0])
        if member is None:
            raise ValueError(f"P7 HJM source archive member is not a file: {member_path}")
        return member.read()


def validate_profile_manifest(*, verify_artifacts: bool = True) -> dict[str, Any]:
    """Validate the manifest, bound source review, selection, pilot inventory, and artifacts."""
    raw = PROFILE_MANIFEST_PATH.read_bytes()
    if _sha256(raw) != EXPECTED_PROFILE_MANIFEST_SHA256:
        raise ValueError("P7 HJM RCON profile manifest hash changed")
    document = json.loads(raw)
    if not isinstance(document, dict) or document.get("kind") != "p7_hjm_synthetic_rcon_profiles":
        raise ValueError("P7 HJM RCON profile manifest has an unexpected kind")
    if (
        document.get("execution_scope") != "linux_worker_only_network_disabled"
        or document.get("authority_scope") != "synthetic_test_actor_only"
        or document.get("fixture_address") != "127.0.0.1"
        or document.get("network_mode") != "none"
        or document.get("synthetic_authority") != 4
    ):
        raise ValueError("P7 HJM RCON profile exceeds its synthetic loopback authority scope")

    source_report_ref = document.get("source_report", {})
    source_report_path = _safe_repo_file(str(source_report_ref.get("path", "")))
    source_report_raw = source_report_path.read_bytes()
    if _sha256(source_report_raw) != source_report_ref.get("sha256"):
        raise ValueError("P7 HJM source report SHA-256 changed")
    source_report = json.loads(source_report_raw)
    if source_report.get("kind") != "p7_p704_hjm_rcon_source_audit":
        raise ValueError("P7 HJM source report has an unexpected kind")

    for key in ("selection", "baseline_pilot"):
        reference = document.get(key, {})
        report_reference = source_report.get(key, {})
        if reference != report_reference:
            raise ValueError(f"P7 HJM {key} reference differs between manifest and source report")
        evidence_path = _safe_repo_file(str(reference.get("path", "")))
        if _sha256(evidence_path.read_bytes()) != reference.get("sha256"):
            raise ValueError(f"P7 HJM {key} evidence SHA-256 changed")
    selection = _read_json(_safe_repo_file(document["selection"]["path"]))
    pilot = _read_json(_safe_repo_file(document["baseline_pilot"]["path"]))
    findings = {
        row.get("registry_entry_id"): row
        for row in source_report.get("findings", [])
        if isinstance(row, dict)
    }
    if verify_artifacts:
        from lab.automation.behavior_v2 import ARTIFACT_CACHE, CATALOG_ROOT

        catalog_root = CATALOG_ROOT
        artifact_cache = ARTIFACT_CACHE
    else:
        catalog_root = ROOT / "catalogs/p2-41649744e057d61031d5"
        artifact_cache = None
    catalog = _read_json(catalog_root / "artifacts.lock.json")
    lock_entries = {row.get("registry_entry_id"): row for row in catalog.get("entries", []) if isinstance(row, dict)}
    seen: set[str] = set()
    for profile in document.get("profiles", []):
        entry_id = profile.get("registry_entry_id")
        if entry_id in seen:
            raise ValueError(f"duplicate P7 HJM profile: {entry_id}")
        seen.add(entry_id)
        expected_identity = EXPECTED_IDENTITIES.get(entry_id)
        finding = findings.get(entry_id)
        if not isinstance(expected_identity, dict) or not isinstance(finding, dict):
            raise ValueError(f"P7 HJM profile is not one of the two reviewed artifacts: {entry_id}")
        identity = {key: profile.get(key) for key in ("package", "version", "artifact_sha256")}
        if identity != expected_identity or any(finding.get(key) != value for key, value in expected_identity.items()):
            raise ValueError(f"P7 HJM profile identity differs from its exact source audit: {entry_id}")
        selected_row = next((
            row for row in selection.get("samples", [])
            if isinstance(row, dict) and row.get("entry", {}).get("registry_entry_id") == entry_id
        ), None)
        selected = selected_row.get("entry") if isinstance(selected_row, dict) else None
        if (
            not isinstance(selected, dict)
            or selected_row.get("ecosystem") != "koishi"
            or any(selected.get(key) != value for key, value in {
                "registry_entry_id": entry_id,
                **expected_identity,
            }.items())
        ):
            raise ValueError(f"P7 HJM profile is absent from its exact locked selection: {entry_id}")
        pilot_entry = next((row for row in pilot.get("entries", []) if row.get("registry_entry_id") == entry_id), None)
        pilot_command_ids = sorted(
            row.get("command_id") for row in (pilot_entry or {}).get("commands", {}).get("items", [])
            if isinstance(row, dict) and isinstance(row.get("command_id"), str)
        )
        if profile.get("command_ids") != pilot_command_ids or finding.get("command_ids") != pilot_command_ids:
            raise ValueError(f"P7 HJM command inventory differs from the locked pilot: {entry_id}")
        source_files = finding.get("source_files", [])
        source_by_path = {row.get("path"): row for row in source_files if isinstance(row, dict)}
        for source in profile.get("source_evidence", []):
            audited = source_by_path.get(source.get("path"))
            if (
                not isinstance(audited, dict)
                or source.get("sha256") != audited.get("sha256")
                or source.get("lines") != audited.get("lines")
            ):
                raise ValueError(f"P7 HJM source evidence differs from its hash-bound audit: {entry_id}")
        if len(source_by_path) != len(profile.get("source_evidence", [])):
            raise ValueError(f"P7 HJM source evidence is incomplete: {entry_id}")
        _validate_plugin_config(document, profile)

        lock_entry = lock_entries.get(entry_id)
        artifacts = [
            item.get("artifact") for item in (lock_entry or {}).get("artifacts", [])
            if item.get("platform") == "all" and isinstance(item.get("artifact"), dict)
        ]
        if len(artifacts) != 1 or artifacts[0].get("computed_digests", {}).get("sha256") != expected_identity["artifact_sha256"]:
            raise ValueError(f"P7 HJM artifact differs from its frozen catalog lock: {entry_id}")
        if verify_artifacts:
            assert artifact_cache is not None
            archive_path = (artifact_cache / artifacts[0]["cache_path"]).resolve(strict=True)
            if _sha256(archive_path.read_bytes()) != expected_identity["artifact_sha256"]:
                raise ValueError(f"P7 HJM locked artifact bytes changed: {entry_id}")
            for source in profile["source_evidence"]:
                if _sha256(_artifact_member_bytes(archive_path, source["path"])) != source["sha256"]:
                    raise ValueError(f"P7 HJM source member hash differs from its artifact: {entry_id}:{source['path']}")
    if seen != set(EXPECTED_IDENTITIES):
        raise ValueError("P7 HJM profile manifest must contain exactly the two locked artifacts")
    return copy.deepcopy(document)


def _validate_plugin_config(document: dict[str, Any], profile: dict[str, Any]) -> None:
    servers = profile.get("plugin_config", {}).get("servers")
    if profile.get("synthetic_authority") != document.get("synthetic_authority") or not isinstance(servers, list) or len(servers) != 2:
        raise ValueError("P7 HJM config must use the shared synthetic authority and two local servers")
    for server in servers:
        if (
            server.get("rconHost") != document["fixture_address"]
            or server.get("rconPort") != document["fixture_port"]
            or server.get("rconPassword") != "p7-hjm-synthetic-rcon-v1"
            or server.get("gameHost") != "127.0.0.1"
        ):
            raise ValueError("P7 HJM plugin config must point only to the synthetic local fixture")
    if profile["package"] == "koishi-plugin-hjm-server-query":
        if profile["plugin_config"].get("commandName") != "hjm" or any(server.get("enabled") is not True for server in servers):
            raise ValueError("P7 HJM 1.0.3 config differs from its reviewed schema")
    elif any(server.get("alias") != "" for server in servers):
        raise ValueError("P7 HJM Plus config must keep command aliases disabled")


def _binding(document: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any] | None:
    if candidate.get("ecosystem") != "koishi":
        return None
    entry_id = candidate.get("registry_entry_id")
    profile = next((row for row in document["profiles"] if row["registry_entry_id"] == entry_id), None)
    if profile is None:
        return None
    identity = {key: candidate.get(key) for key in ("package", "version", "artifact_sha256")}
    expected = {key: profile[key] for key in ("package", "version", "artifact_sha256")}
    if identity != expected:
        raise ValueError(f"P7 HJM RCON artifact identity mismatch: {entry_id}")
    command_contexts = {
        command_id: {
            "conversation_kind": "group",
            "mention_bot": False,
            "authorization": "plugin_admin",
            "actor_id": document["synthetic_actor_id"],
        }
        for command_id in profile["command_ids"]
    }
    payload = {
        "profile_id": document["profile_id"],
        "execution_scope": document["execution_scope"],
        "authority_scope": document["authority_scope"],
        "manifest_sha256": _sha256(PROFILE_MANIFEST_PATH.read_bytes()),
        "registry_entry_id": entry_id,
        **expected,
        "synthetic_actor_id": document["synthetic_actor_id"],
        "synthetic_authority": profile["synthetic_authority"],
        "plugin_config": copy.deepcopy(profile["plugin_config"]),
        "command_contexts": command_contexts,
        "source_evidence": copy.deepcopy(profile["source_evidence"]),
        "fixture_address": document["fixture_address"],
        "fixture_port": document["fixture_port"],
        "network_mode": document["network_mode"],
        "source_report_sha256": document["source_report"]["sha256"],
    }
    return {**payload, "profile_sha256": _sha256(_canonical(payload))}


def profile_binding(candidate: dict[str, Any]) -> dict[str, Any] | None:
    """Bind only the two exact HJM artifacts and verify their source bytes before use."""
    if candidate.get("ecosystem") != "koishi" or candidate.get("registry_entry_id") not in EXPECTED_IDENTITIES:
        return None
    document = validate_profile_manifest(verify_artifacts=True)
    return _binding(document, candidate)


def profile_manifest_sha256() -> str:
    validate_profile_manifest(verify_artifacts=True)
    return _sha256(PROFILE_MANIFEST_PATH.read_bytes())


def profile_entry_ids() -> set[str]:
    return set(EXPECTED_IDENTITIES)


def build_diagnostic_selection() -> dict[str, Any]:
    """Build the exact two-artifact HJM diagnostic from the frozen 30+30 selection."""
    document = _read_json(SOURCE_SELECTION_PATH)
    manifest = validate_profile_manifest(verify_artifacts=True)
    selected_ids = set(EXPECTED_IDENTITIES)
    rows = [
        copy.deepcopy(row) for row in document.get("samples", [])
        if row.get("entry", {}).get("registry_entry_id") in selected_ids
    ]
    if {row["entry"]["registry_entry_id"] for row in rows} != selected_ids or len(rows) != len(selected_ids):
        raise ValueError("frozen P7 selection does not contain exactly the two reviewed HJM artifacts")
    source_hash = _sha256(SOURCE_SELECTION_PATH.read_bytes())
    result = copy.deepcopy(document)
    result["diagnostic_scope"] = {
        "kind": "p7_source_bound_hjm_rcon_diagnostic",
        "source_selection_path": SOURCE_SELECTION_PATH.relative_to(ROOT).as_posix(),
        "source_selection_sha256": source_hash,
        "profile_manifest_sha256": _sha256(PROFILE_MANIFEST_PATH.read_bytes()),
    }
    result["selection"]["counts"] = {"koishi": 2, "nonebot": 0}
    result["selection"]["per_ecosystem_target"] = None
    result["samples"] = rows
    return result


def validate_diagnostic_selection(selection: dict[str, Any]) -> dict[str, Any]:
    """Reject additions, substitutions, or profile drift in the bounded HJM selection."""
    expected = build_diagnostic_selection()
    expected_scope = expected["diagnostic_scope"]
    if (
        selection.get("kind") != "p7_p701_static_inventory_sample"
        or selection.get("diagnostic_scope") != expected_scope
        or selection.get("selection") != expected["selection"]
        or selection.get("execution") != expected["execution"]
        or selection.get("samples") != expected["samples"]
    ):
        raise ValueError("HJM diagnostic selection differs from the exact hash-bound 2-entry profile")
    return copy.deepcopy(selection)


def validate_bound_profile(profile: Any, sample: Any) -> dict[str, Any]:
    """Revalidate the serialized profile inside an isolated Worker without external cache access."""
    if not isinstance(profile, dict) or not isinstance(sample, dict):
        raise ValueError("P7 HJM RCON profile and sample must be objects")
    payload = {key: value for key, value in profile.items() if key != "profile_sha256"}
    if not isinstance(profile.get("profile_sha256"), str) or _sha256(_canonical(payload)) != profile["profile_sha256"]:
        raise ValueError("P7 HJM RCON profile hash mismatch")
    raw = PROFILE_MANIFEST_PATH.read_bytes()
    if _sha256(raw) != EXPECTED_PROFILE_MANIFEST_SHA256:
        raise ValueError("P7 HJM RCON profile manifest hash changed inside Worker")
    document = json.loads(raw)
    sample_id = str(sample.get("sample_id", "")).removeprefix("p7-")
    expected = _binding(document, {
        "ecosystem": sample.get("ecosystem", "koishi"),
        "registry_entry_id": sample_id,
        "package": sample.get("package"),
        "version": sample.get("version"),
        "artifact_sha256": sample.get("_artifact_hash"),
    })
    if expected is None or expected != profile:
        raise ValueError("P7 HJM RCON profile does not match the installed sample artifact")
    return copy.deepcopy(profile)


def _packet(packet_id: int, packet_type: int, body: str) -> bytes:
    payload = struct.pack("<ii", packet_id, packet_type) + body.encode("utf-8") + b"\0\0"
    return struct.pack("<i", len(payload)) + payload


def _read_exactly(stream: Any, size: int) -> bytes | None:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = stream.read(size - len(chunks))
        if not chunk:
            return None
        chunks.extend(chunk)
    return bytes(chunks)


def _command_category(command: str) -> str:
    parts = command.strip().split(None, 1)
    if not parts:
        return "empty"
    head = parts[0].casefold()
    if head == "listgamemodeproperties":
        property_name = parts[1].strip().casefold() if len(parts) > 1 else "unknown"
        return {
            "serverhostname": "server_hostname",
            "numplayers": "player_count",
            "optionsstring": "server_options",
        }.get(property_name, "server_property")
    if head in {"listplayers", "listbans", "banid", "kick", "unban", "say"}:
        return head
    return "command"


class HjmRconFixture:
    """Serve deterministic synthetic Source RCON responses on Worker loopback only."""

    def __init__(self, profile: dict[str, Any], *, port_override: int | None = None) -> None:
        sample = {
            "sample_id": f"p7-{profile.get('registry_entry_id', '')}",
            "ecosystem": "koishi",
            "package": profile.get("package"),
            "version": profile.get("version"),
            "_artifact_hash": profile.get("artifact_sha256"),
        }
        self.profile = validate_bound_profile(profile, sample)
        if port_override is not None and (
            isinstance(port_override, bool) or not isinstance(port_override, int) or not 0 <= port_override <= 65535
        ):
            raise ValueError("P7 HJM RCON port override must be an integer from 0 through 65535")
        self.port = self.profile["fixture_port"] if port_override is None else port_override
        self.password = self.profile["plugin_config"]["servers"][0]["rconPassword"]
        self._lock = threading.Lock()
        self._command_counts: Counter[str] = Counter()
        self._request_count = 0
        self._authenticated_connections = 0
        self._unmatched_requests = 0
        self._server: socketserver.ThreadingTCPServer | None = None
        self._thread: threading.Thread | None = None
        self._closed = False

    @property
    def bound_port(self) -> int:
        if self._server is None:
            raise RuntimeError("P7 HJM RCON fixture has not started")
        return int(self._server.server_address[1])

    def start(self) -> None:
        if self._server is not None:
            raise RuntimeError("P7 HJM RCON fixture has already started")
        fixture = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                authenticated = False
                while True:
                    length_raw = _read_exactly(self.rfile, 4)
                    if length_raw is None:
                        return
                    length = struct.unpack("<i", length_raw)[0]
                    if length < 10 or length > 8192:
                        return
                    packet = _read_exactly(self.rfile, length)
                    if packet is None:
                        return
                    request_id, packet_type = struct.unpack("<ii", packet[:8])
                    body_raw = packet[8:-2]
                    if packet[-2:] != b"\0\0":
                        return
                    body = body_raw.decode("utf-8", errors="replace")
                    if packet_type == 3:
                        authenticated = body == fixture.password
                        if authenticated:
                            with fixture._lock:
                                fixture._authenticated_connections += 1
                            self.wfile.write(_packet(request_id, 0, ""))
                            self.wfile.write(_packet(request_id, 2, ""))
                        else:
                            self.wfile.write(_packet(-1, 2, ""))
                            return
                    elif packet_type == 2 and authenticated:
                        category = _command_category(body)
                        response = fixture._response(category, body)
                        with fixture._lock:
                            fixture._request_count += 1
                            fixture._command_counts[category] += 1
                        self.wfile.write(_packet(request_id, 0, response))
                    else:
                        with fixture._lock:
                            fixture._unmatched_requests += 1
                        return

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = False
            daemon_threads = True
            request_queue_size = 16

        self._server = Server((self.profile["fixture_address"], self.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, name="p7-hjm-rcon", daemon=True)
        self._thread.start()

    def _response(self, category: str, command: str) -> str:
        if category == "server_hostname":
            return "ServerHostname=P7 Synthetic Insurgency Server"
        if category == "player_count":
            return "NumPlayers=1"
        if category == "server_options":
            return "OptionsString=Scenario_P7_Lobby?MaxPlayers=8?Lighting=Day"
        if category == "listplayers":
            return "1|P7 Synthetic Player|SteamNWI:76561198000000001||0"
        if category == "listbans":
            return "76561198000000001 0 (P7 synthetic fixture)"
        digest = _sha256(command.encode("utf-8"))[:12]
        return f"P7 synthetic {category} response {digest}"

    def evidence(self) -> dict[str, Any]:
        with self._lock:
            counts = dict(sorted(self._command_counts.items()))
            request_count = self._request_count
            authenticated = self._authenticated_connections
            unmatched = self._unmatched_requests
        return {
            "profile_id": self.profile["profile_id"],
            "profile_sha256": self.profile["profile_sha256"],
            "network_mode": "none",
            "bind_address": self.profile["fixture_address"],
            "bound_port": self.bound_port if self._server is not None else self.port,
            "request_count": request_count,
            "authenticated_connections": authenticated,
            "command_category_counts": counts,
            "unmatched_request_count": unmatched,
            "cleanup_confirmed": self._closed,
        }

    def close(self) -> None:
        if self._server is None:
            self._closed = True
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
            if self._thread.is_alive():
                raise RuntimeError("P7 HJM RCON fixture server did not stop")
        self._server = None
        self._thread = None
        self._closed = True
