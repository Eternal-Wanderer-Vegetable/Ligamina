from __future__ import annotations

import unittest


class P7LocalCandidateDiagnosticTests(unittest.TestCase):
    def test_hash_bound_json_accepts_git_normalized_line_endings(self) -> None:
        import hashlib
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        from lab.automation import p7_local_candidate_diagnostic as diagnostic

        raw = b'{"marker": "line endings"}\r\n'
        expected = hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "profile.json"
            path.write_bytes(raw)
            with patch.object(diagnostic, "ROOT", Path(temporary)):
                resolved, document = diagnostic._hash_bound_json(
                    {"path": "profile.json", "sha256": expected},
                    label="context profile",
                )

        self.assertEqual(resolved, path.resolve())
        self.assertEqual(document, {"marker": "line endings"})

    def test_selection_binds_only_the_two_exact_candidates_and_stimulus_manifest(self) -> None:
        from lab.automation.p7_local_candidate_diagnostic import build_selection_document

        document = build_selection_document()

        self.assertEqual(document["diagnostic_scope"]["kind"], "p7_source_bound_local_candidate_diagnostic")
        self.assertEqual(
            {sample["entry"]["registry_entry_id"] for sample in document["samples"]},
            {"ko-919bd13715bb892a7a8675e3", "nb-250c92c282d77dcf502b9dbf"},
        )
        self.assertEqual(document["selection"]["counts"], {"koishi": 1, "nonebot": 1})
        self.assertEqual(
            document["diagnostic_scope"]["stimulus_profile_sha256"],
            document["stimulus_profile"]["sha256"],
        )
        self.assertEqual(document["selection"]["source_matrix_sha256"], "40bc6559dc6c360857f919dc43f6060732041820cc1bd479f6449ad1bb15d787")

    def test_selection_rejects_a_tampered_locked_candidate(self) -> None:
        from lab.automation.p7_local_candidate_diagnostic import (
            build_selection_document,
            validate_selection_document,
        )

        document = build_selection_document()
        document["samples"][0]["entry"]["artifact_sha256"] = "0" * 64

        with self.assertRaisesRegex(ValueError, "sample identities differ"):
            validate_selection_document(document)

    def test_scenario_batch_applies_only_explicit_source_bound_inputs(self) -> None:
        from lab.automation.p7_differential_campaign import build_scenario_batch
        from lab.automation.p7_local_candidate_diagnostic import (
            stimulus_for_candidate,
            stimulus_profile_sha256,
        )

        candidate = {
            "ecosystem": "koishi",
            "registry_entry_id": "ko-919bd13715bb892a7a8675e3",
            "package": "@deepseaxx/koishi-plugin-subculture-id-tool",
            "version": "0.4.5",
            "artifact_sha256": "edb9f833306554a68fd4ca6371256762bfd71ccda93464482f28d3035a0ab452",
        }
        descriptors = [
            {"command_id": "subid", "invocation": "subid", "framework": "koishi", "arguments": [{"name": "keyword", "required": True, "type": "string"}]},
            {"command_id": "subid-list", "invocation": "subid-list", "framework": "koishi"},
        ]
        stimulus = stimulus_for_candidate(candidate)
        events, generation = build_scenario_batch(
            candidate,
            descriptors,
            seed=20261110,
            local_stimulus=stimulus,
            local_stimulus_profile_sha256=stimulus_profile_sha256(),
        )

        self.assertEqual(len(events), 2)
        self.assertEqual(generation["commands_valid"], 1)
        self.assertEqual(generation["local_stimulus_input_count"], 2)
        messages = ["".join(segment["text"] for segment in event["message"] if segment["type"] == "text") for event in events]
        self.assertTrue(any("雪芝麻糊" in message for message in messages))
        self.assertTrue(any("蓝色火焰" in message for message in messages))
        self.assertEqual(len({event["event_id"] for event in events}), 2)

    def test_bilichat_unsub_uses_the_bound_superuser_and_group_context(self) -> None:
        from lab.automation.p7_differential_campaign import build_scenario_batch
        from lab.automation.p7_local_candidate_diagnostic import (
            stimulus_for_candidate,
            stimulus_profile_sha256,
        )
        from lab.automation.p7_context_profiles import profile_binding

        candidate = {
            "ecosystem": "nonebot",
            "registry_entry_id": "nb-250c92c282d77dcf502b9dbf",
            "package": "nonebot_plugin_bilichat",
            "version": "6.5.0",
            "artifact_sha256": "2fd7ac90900b4039fb71765ebf53886f6ade3593eb466fed5e615d2d82e02bb0",
        }
        descriptor = {
            "command_id": "unsub",
            "invocation": "/.unsub",
            "framework": "nonebot",
            "arguments": [{"name": "keyword", "required": True, "type": "string"}],
        }
        stimulus = stimulus_for_candidate(candidate)
        events, generation = build_scenario_batch(
            candidate,
            [descriptor],
            seed=20261110,
            local_stimulus=stimulus,
            local_stimulus_profile_sha256=stimulus_profile_sha256(),
        )
        context = profile_binding(candidate)

        self.assertEqual(len(events), 2)
        self.assertEqual({row["command_id"] for row in generation["scenarios"]}, {"unsub"})
        self.assertEqual({event["actor_id"] for event in events}, {context["synthetic_superuser_id"]})
        self.assertTrue(all(event["conversation"]["kind"] == "group" for event in events))
        self.assertTrue(all(any(segment.get("type") == "mention" for segment in event["message"]) for event in events))
        messages = ["".join(segment.get("text", "") for segment in event["message"]) for event in events]
        self.assertTrue(any("all" in message for message in messages))
        self.assertTrue(any("p7-unmatched-subscription" in message for message in messages))

    def test_campaign_requires_explicit_local_candidate_mode(self) -> None:
        from lab.automation.p7_differential_campaign import select_candidates
        from lab.automation.p7_local_candidate_diagnostic import build_selection_document

        selection = build_selection_document()
        candidates = select_candidates(selection, allow_local_candidate_diagnostic=True)

        self.assertEqual(
            {candidate["registry_entry_id"] for candidate in candidates},
            {"ko-919bd13715bb892a7a8675e3", "nb-250c92c282d77dcf502b9dbf"},
        )

    def test_campaign_rejects_local_selection_without_the_explicit_gate(self) -> None:
        from lab.automation.p7_differential_campaign import select_candidates
        from lab.automation.p7_local_candidate_diagnostic import build_selection_document

        with self.assertRaisesRegex(ValueError, "below the requested per-ecosystem count"):
            select_candidates(build_selection_document())

    def test_source_bound_input_delta_is_reported_without_changing_cohort_gate(self) -> None:
        from lab.automation.p7_differential_campaign import _source_bound_input_adjudication

        scenarios = [
            {"scenario_id": "scn-a", "event_id": "event-a", "local_stimulus_cycle_index": 0, "local_stimulus_input_sha256": "a" * 64},
            {"scenario_id": "scn-b", "event_id": "event-b", "local_stimulus_cycle_index": 1, "local_stimulus_input_sha256": "b" * 64},
        ]
        rounds = [
            {"scenario_results": [
                {"scenario_id": "scn-a", "status": "equivalent", "native_one_observation_sha256": "1" * 64, "native_two_observation_sha256": "1" * 64, "federated_observation_sha256": "1" * 64},
                {"scenario_id": "scn-b", "status": "equivalent", "native_one_observation_sha256": "2" * 64, "native_two_observation_sha256": "2" * 64, "federated_observation_sha256": "2" * 64},
            ]}
            for _ in range(3)
        ]

        outcome = _source_bound_input_adjudication(scenarios, rounds)

        self.assertEqual(outcome["classification"], "input_sensitive_native_federated_equivalent")
        self.assertTrue(outcome["input_sensitive"])
        self.assertFalse(outcome["counts_toward_cohort_nontrivial_gate"])
        self.assertEqual([row["source_event_id"] for row in outcome["inputs"]], ["event-a", "event-b"])

    def test_source_bound_input_without_operations_remains_unknown(self) -> None:
        from lab.automation.p7_differential_campaign import _source_bound_input_adjudication

        scenarios = [
            {"scenario_id": "scn-a", "event_id": "event-a", "local_stimulus_cycle_index": 0, "local_stimulus_input_sha256": "a" * 64},
            {"scenario_id": "scn-b", "event_id": "event-b", "local_stimulus_cycle_index": 1, "local_stimulus_input_sha256": "b" * 64},
        ]
        rounds = [
            {"scenario_results": [
                {"scenario_id": "scn-a", "status": "unknown", "reason_code": "no_native_gateway_operation_observed"},
                {"scenario_id": "scn-b", "status": "unknown", "reason_code": "no_native_gateway_operation_observed"},
            ]}
            for _ in range(3)
        ]

        outcome = _source_bound_input_adjudication(scenarios, rounds)

        self.assertEqual(outcome["classification"], "unknown_missing_native_or_federated_observation")
        self.assertIsNone(outcome["input_sensitive"])
        self.assertFalse(outcome["input_sensitivity_assessed"])
        self.assertFalse(outcome["counts_toward_cohort_nontrivial_gate"])


if __name__ == "__main__":
    unittest.main()
