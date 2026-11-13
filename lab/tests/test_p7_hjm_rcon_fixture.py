from __future__ import annotations

import socket
import struct
import unittest
import hashlib
import json

from lab.automation.p7_hjm_rcon_fixture import (
    HjmRconFixture,
    build_diagnostic_selection,
    profile_binding,
    validate_diagnostic_selection,
    validate_bound_profile,
)
from lab.automation import p7_differential_campaign


HJM_CANDIDATES = (
    {
        "ecosystem": "koishi",
        "registry_entry_id": "ko-68ea4b6fd60ea7b3a207c402",
        "package": "koishi-plugin-hjm-server-query",
        "version": "1.0.3",
        "artifact_sha256": "70407f95613efa823ef643c1314b723afcebaec1489e78f396b240a3cc3aa541",
    },
    {
        "ecosystem": "koishi",
        "registry_entry_id": "ko-fcd188bbb6d1c2f93bd5b727",
        "package": "koishi-plugin-hjm-server-query-plus",
        "version": "1.1.1",
        "artifact_sha256": "9154c5fbf078958d86c5b1263bcf8e482434e5f7e26b8dea7d44aff9c59ae2f6",
    },
)


def _packet(packet_id: int, packet_type: int, body: str) -> bytes:
    payload = struct.pack("<ii", packet_id, packet_type) + body.encode("utf-8") + b"\0\0"
    return struct.pack("<i", len(payload)) + payload


def _read_packet(sock: socket.socket) -> tuple[int, int, str]:
    def read_exactly(size: int) -> bytes:
        parts = bytearray()
        while len(parts) < size:
            chunk = sock.recv(size - len(parts))
            if not chunk:
                raise AssertionError("fixture closed an incomplete RCON packet")
            parts.extend(chunk)
        return bytes(parts)

    length = struct.unpack("<i", read_exactly(4))[0]
    payload = read_exactly(length)
    packet_id, packet_type = struct.unpack("<ii", payload[:8])
    return packet_id, packet_type, payload[8:-2].decode("utf-8")


class P7HjmRconFixtureTests(unittest.TestCase):
    def test_profiles_bind_only_the_two_exact_locked_hjm_artifacts(self) -> None:
        bindings = [profile_binding(candidate) for candidate in HJM_CANDIDATES]

        self.assertTrue(all(binding is not None for binding in bindings))
        for candidate, binding in zip(HJM_CANDIDATES, bindings, strict=True):
            assert binding is not None
            self.assertEqual(binding["registry_entry_id"], candidate["registry_entry_id"])
            self.assertEqual(binding["artifact_sha256"], candidate["artifact_sha256"])
            self.assertEqual(binding["fixture_address"], "127.0.0.1")
            self.assertEqual(binding["synthetic_authority"], 4)
            self.assertEqual(len(binding["command_contexts"]), 7)
            validate_bound_profile(
                binding,
                {
                    "sample_id": f"p7-{candidate['registry_entry_id']}",
                    "package": candidate["package"],
                    "version": candidate["version"],
                    "_artifact_hash": candidate["artifact_sha256"],
                },
            )

        self.assertIsNone(profile_binding({"ecosystem": "koishi", "registry_entry_id": "ko-unbound"}))
        wrong_identity = {**HJM_CANDIDATES[0], "artifact_sha256": "0" * 64}
        with self.assertRaisesRegex(ValueError, "artifact identity mismatch"):
            profile_binding(wrong_identity)

    def test_diagnostic_selection_keeps_only_the_two_hashed_hjm_entries(self) -> None:
        selection = build_diagnostic_selection()

        self.assertEqual(selection["diagnostic_scope"]["kind"], "p7_source_bound_hjm_rcon_diagnostic")
        self.assertEqual(selection["selection"]["counts"], {"koishi": 2, "nonebot": 0})
        self.assertEqual(
            {row["entry"]["registry_entry_id"] for row in validate_diagnostic_selection(selection)["samples"]},
            {candidate["registry_entry_id"] for candidate in HJM_CANDIDATES},
        )

        tampered = json.loads(json.dumps(selection))
        tampered["samples"].append(tampered["samples"][0])
        with self.assertRaisesRegex(ValueError, "exact hash-bound 2-entry profile"):
            validate_diagnostic_selection(tampered)

        candidates = p7_differential_campaign.select_candidates(
            selection,
            allow_hjm_rcon_diagnostic=True,
        )
        self.assertEqual(
            {candidate["registry_entry_id"] for candidate in candidates},
            {candidate["registry_entry_id"] for candidate in HJM_CANDIDATES},
        )

    def test_rcon_fixture_authenticates_and_records_categories_without_payloads(self) -> None:
        profile = profile_binding(HJM_CANDIDATES[0])
        assert profile is not None
        fixture = HjmRconFixture(profile, port_override=0)
        fixture.start()
        secret_command = "say PRIVATE_SYNTHETIC_MARKER"
        try:
            with socket.create_connection(("127.0.0.1", fixture.bound_port), timeout=2) as client:
                client.settimeout(2)
                password = profile["plugin_config"]["servers"][0]["rconPassword"]
                client.sendall(_packet(11, 3, password))
                packets = [_read_packet(client), _read_packet(client)]
                self.assertEqual(packets[-1][:2], (11, 2))

                client.sendall(_packet(12, 2, secret_command))
                response = _read_packet(client)
                self.assertEqual(response[:2], (12, 0))
                self.assertTrue(response[2])
        finally:
            fixture.close()

        evidence = fixture.evidence()
        self.assertEqual(evidence["bind_address"], "127.0.0.1")
        self.assertEqual(evidence["network_mode"], "none")
        self.assertEqual(evidence["request_count"], 1)
        self.assertEqual(evidence["command_category_counts"], {"say": 1})
        serialized = str(evidence)
        self.assertNotIn(secret_command, serialized)
        self.assertNotIn(profile["plugin_config"]["servers"][0]["rconPassword"], serialized)

    def test_rcon_fixture_rejects_invalid_listener_addresses(self) -> None:
        profile = profile_binding(HJM_CANDIDATES[0])
        assert profile is not None
        profile["fixture_address"] = "0.0.0.0"
        payload = {key: value for key, value in profile.items() if key != "profile_sha256"}
        profile["profile_sha256"] = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        with self.assertRaisesRegex(ValueError, "does not match the installed sample artifact"):
            HjmRconFixture(profile)


if __name__ == "__main__":
    unittest.main()
