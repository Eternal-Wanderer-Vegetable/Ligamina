from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from lab.automation import phase4_campaign
from lab.automation.p7_differential_campaign import (
    _command_summary,
    _discovery_skip_domain,
    _discovery_terminal_state,
    _event_from_scenario,
    _nontrivial_commands,
    _round_scenario_results,
    _write_final,
)
from lab.automation.phase4_worker_side import _scrub_result_diagnostics


def _observation(message: str, event_id: str = "evt-1") -> dict[str, object]:
    return {
        "status": "completed",
        "operations": [
            {
                "source_event_id": event_id,
                "account_id": "100000",
                "operation": "send_message",
                "target": {"kind": "private", "id": "200000"},
                "params": {"message": [{"type": "text", "text": message}]},
            }
        ],
    }


class DifferentialCampaignTests(unittest.TestCase):
    def test_koishi_scenario_maps_stable_message_id_to_platform_extension(self) -> None:
        scenario = {
            "scenario_id": "scn-example",
            "steps": [{
                "kind": "event",
                "event": {
                    "event_id": "event-example",
                    "platform": "onebot.v11",
                    "extensions": {"onebot.v11": {"message_id": "message-example"}},
                },
            }],
        }

        event = _event_from_scenario(scenario, ecosystem="koishi")

        self.assertEqual(event["platform"], "koishi")
        self.assertEqual(event["extensions"]["koishi"]["message_id"], "message-example")
        self.assertEqual(event["extensions"]["onebot.v11"]["message_id"], "message-example")

    def test_koishi_scenario_generates_message_id_when_native_extension_is_absent(self) -> None:
        scenario = {
            "scenario_id": "scn-example",
            "steps": [{
                "kind": "event",
                "event": {"event_id": "event-example", "extensions": {}},
            }],
        }

        event = _event_from_scenario(scenario, ecosystem="koishi")

        self.assertRegex(event["extensions"]["koishi"]["message_id"], r"^[0-9]+$")

    def test_generated_group_conversation_is_preserved_in_pilot_event(self) -> None:
        scenario = {
            "scenario_id": "scn-example",
            "actors": [{"actor_id": "actor-1", "account_id": "account-1"}],
            "conversations": [{
                "conversation_id": "group-1",
                "kind": "group",
                "actor_id": "actor-1",
                "account_id": "account-1",
            }],
            "controls": {"fixed_time_utc": "2026-11-10T00:00:00Z"},
            "steps": [{
                "kind": "tool_call",
                "host_id": "koishi",
                "actor_id": "actor-1",
                "conversation_id": "group-1",
                "invocation": "hello",
                "arguments": "world",
            }],
        }

        event = _event_from_scenario(scenario, ecosystem="koishi")

        self.assertEqual(event["conversation"], {"kind": "group", "id": "group-1"})

    def test_clean_discovery_failure_is_a_skipped_terminal_state(self) -> None:
        result = _discovery_terminal_state({
            "status": "failed",
            "profile_setup": {"status": "passed"},
            "outer_worker": {"cleanup_confirmed": True},
        })

        self.assertEqual(result, ("skipped", "native_discovery_failed"))

    def test_discovery_failure_without_confirmed_cleanup_stops_campaign(self) -> None:
        result = _discovery_terminal_state({
            "status": "failed",
            "profile_setup": {"status": "passed"},
            "outer_worker": {"cleanup_confirmed": False},
        })

        self.assertEqual(result, ("incomplete", "worker_cleanup_unconfirmed"))

    def test_completed_discovery_is_not_a_failure_state(self) -> None:
        result = _discovery_terminal_state({
            "status": "completed",
            "profile_setup": {"status": "passed"},
            "outer_worker": {"cleanup_confirmed": True},
        })

        self.assertIsNone(result)

    def test_database_migration_prerequisite_is_attributed_to_profile(self) -> None:
        result = _discovery_skip_domain({
            "profile_setup_status": "passed",
            "diagnostic_summary": "Please run nb orm upgrade: database migration is required",
        })

        self.assertEqual(result, "profile")

    def test_other_native_load_errors_remain_host_scoped(self) -> None:
        result = _discovery_skip_domain({
            "profile_setup_status": "passed",
            "diagnostic_summary": "plugin initialization failed before command discovery",
        })

        self.assertEqual(result, "native_host")

    def test_no_gateway_operation_is_unknown_instead_of_nondeterministic(self) -> None:
        scenarios = [{"scenario_id": "scn-one", "event_id": "evt-1", "command_id": "echo", "kind": "raw_text"}]
        empty = {"status": "completed", "operations": []}

        rows, _, _, _ = _round_scenario_results(scenarios, empty, empty, empty)

        self.assertEqual(rows[0]["status"], "unknown")
        self.assertEqual(rows[0]["native_self_status"], "unknown")
        self.assertEqual(rows[0]["reason_code"], "no_native_gateway_operation_observed")

    def test_native_variation_is_nondeterministic_but_federated_difference_is_non_equivalent(self) -> None:
        scenarios = [{"scenario_id": "scn-one", "event_id": "evt-1", "command_id": "echo", "kind": "raw_text"}]

        varied, _, _, _ = _round_scenario_results(
            scenarios, _observation("first"), _observation("second"), _observation("first")
        )
        mismatch, _, _, _ = _round_scenario_results(
            scenarios, _observation("same"), _observation("same"), _observation("different")
        )

        self.assertEqual(varied[0]["status"], "nondeterministic")
        self.assertEqual(mismatch[0]["status"], "non_equivalent")

    def test_nontrivial_requires_stable_observed_normal_and_edge_outputs(self) -> None:
        scenarios = [
            {"event_id": "normal", "command_id": "echo", "kind": "raw_text", "arguments_nonempty": True},
            {"event_id": "edge", "command_id": "echo", "kind": "raw_text", "arguments_nonempty": False},
        ]
        no_output = {"events": {"normal": [], "edge": []}}
        output_one = {"events": {"normal": [{"message": "ok"}], "edge": [{"message": "usage"}]}}
        output_two = json.loads(json.dumps(output_one))

        self.assertEqual(_nontrivial_commands(scenarios, [(no_output, no_output)]), {"echo": "not_assessed"})
        self.assertEqual(_nontrivial_commands(scenarios, [(output_one, output_two)]), {"echo": "nontrivial"})

    def test_command_consistency_requires_three_fresh_reruns(self) -> None:
        descriptor = {"command_id": "echo"}
        scenario = {"scenario_id": "scn-one", "command_id": "echo", "event_id": "evt-1", "kind": "raw_text", "arguments_nonempty": True}
        one_round = [[{"scenario_id": "scn-one", "command_id": "echo", "status": "equivalent"}]]

        summary = _command_summary([descriptor], [scenario], [], one_round, [])

        self.assertFalse(summary["items"][0]["consistent_across_three_fresh_reruns"])

    def test_final_report_sidecar_hash_matches_written_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            digest = _write_final(path, {"status": "completed"})
            raw = path.read_bytes()

            self.assertEqual(digest, hashlib.sha256(raw).hexdigest())
            self.assertEqual(path.with_suffix(".json.sha256").read_text(encoding="ascii").split()[0], digest)
            self.assertNotIn("report_sha256", json.loads(raw))

    def test_p7_source_bundle_includes_koishi_runner_fingerprint_module(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            phase4_campaign._copy_python_sources(project)

            self.assertTrue((project / "lab/baselines/koishi/artifact_fingerprint.mjs").is_file())
            self.assertTrue((project / "contracts/lab/v1/catalog.schema.json").is_file())
            self.assertTrue((project / "contracts/lab/v1/run.schema.json").is_file())

    def test_raw_diagnostic_is_replaced_with_scrubbed_summary_and_hash(self) -> None:
        result = _scrub_result_diagnostics({"diagnostic": "RuntimeError: password=private-value"})

        self.assertNotIn("diagnostic", result)
        self.assertNotIn("private-value", json.dumps(result))
        self.assertEqual(
            result["diagnostic_evidence"]["sha256"],
            hashlib.sha256(b"RuntimeError: password=private-value").hexdigest(),
        )
        self.assertEqual(result["diagnostic_summary"], "RuntimeError: <redacted-field>")


if __name__ == "__main__":
    unittest.main()
