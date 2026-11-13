from __future__ import annotations

import json
from pathlib import Path
import unittest

from hypothesis import strategies as st
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from lab.scenario_generator import (
    ScenarioGenerationError,
    _Factor,
    compare_repeated_observations,
    generate_scenarios,
    pairwise_cases,
    parameter_strategy,
    shrink_failing_example,
    type_domain,
)

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "contracts/lab/scenario/v1"
SCHEMA = json.loads((CONTRACTS / "scenario.schema.json").read_text(encoding="utf-8"))
BATCH_SCHEMA = json.loads((CONTRACTS / "scenario-batch.schema.json").read_text(encoding="utf-8"))
INVENTORY_SHA = "a" * 64


def _descriptor(framework: str = "koishi") -> dict:
    return {
        "framework": framework,
        "command_id": "greet",
        "invocation": "greet",
        "usage": "greet <name> [--count N]",
        "description": "Say `hello`",
        "aliases": ["hello"],
        "arguments": [
            {"name": "name", "type": "text", "required": True},
            {"name": "count", "type": "integer", "required": False},
        ],
        "options": [
            {"name": "verbose", "type": "boolean", "syntax": "--verbose"},
            {"name": "mode", "type": "text", "enum": ["short", "long"]},
        ],
    }


class ScenarioGeneratorTests(unittest.TestCase):
    def test_positive_fixture_and_generated_batch_validate_negative_fixture_rejected(self):
        scenario_validator = Draft202012Validator(SCHEMA)
        positive = json.loads((CONTRACTS / "fixtures/positive.json").read_text(encoding="utf-8"))
        negative = json.loads((CONTRACTS / "fixtures/negative.json").read_text(encoding="utf-8"))
        self.assertEqual(list(scenario_validator.iter_errors(positive)), [])
        self.assertTrue(list(scenario_validator.iter_errors(negative)))

        batch = generate_scenarios(
            inventory_sha256=INVENTORY_SHA,
            descriptor=_descriptor(),
            host_id="koishi",
            seed=17,
        ).to_dict()
        registry = Registry().with_resource(SCHEMA["$id"], Resource.from_contents(SCHEMA))
        validator = Draft202012Validator(BATCH_SCHEMA, registry=registry)
        self.assertEqual(list(validator.iter_errors(batch)), [])
        for scenario in batch["scenarios"]:
            self.assertEqual(list(scenario_validator.iter_errors(scenario)), [])

    def test_generation_is_stable_for_same_inputs_and_changes_for_new_seed(self):
        args = {
            "inventory_sha256": INVENTORY_SHA,
            "descriptor": _descriptor(),
            "host_id": "koishi",
        }
        first = generate_scenarios(**args, seed=17).to_dict()
        again = generate_scenarios(**args, seed=17).to_dict()
        other_seed = generate_scenarios(**args, seed=18).to_dict()
        self.assertEqual(first, again)
        self.assertNotEqual(first["scenarios"], other_seed["scenarios"])
        self.assertLessEqual(len(first["scenarios"]), 16)

    def test_type_domains_include_boolean_integer_and_text_boundaries(self):
        boolean = type_domain({"type": "boolean"})
        integer = type_domain({"type": "integer", "minimum": 0, "maximum": 9})
        text = type_domain({"type": "text"})
        enum = type_domain({"type": "text", "enum": ["short", "long"]})
        self.assertEqual(boolean, [False, True])
        self.assertTrue({0, -1, 1, 9, -(2**63), 2**63 - 1}.issubset(set(integer)))
        self.assertIn("中文🙂", text)
        self.assertIn("", text)
        self.assertIn(" leading", text)
        self.assertIn("trailing ", text)
        self.assertIn("x" * 2048, text)
        self.assertEqual(enum, ["short", "long"])
        boolean_strategy = parameter_strategy({"type": "boolean"})
        self.assertIs(shrink_failing_example(boolean_strategy, lambda value: value is True), True)
        self.assertIs(shrink_failing_example(boolean_strategy, lambda value: value is False), False)

    def test_pairwise_coverage_and_bounded_incomplete_report(self):
        factors = [
            _Factor(key=f"f{index}", parameter={}, values=[0, 1], role="argument")
            for index in range(3)
        ]
        complete = pairwise_cases(factors, max_rows=8, seed=9)
        bounded = pairwise_cases(factors, max_rows=2, seed=9)
        self.assertTrue(complete.complete)
        self.assertEqual((complete.covered_pairs, complete.total_pairs), (12, 12))
        self.assertFalse(bounded.complete)
        self.assertEqual(len(bounded.rows), 2)

    def test_shrinking_finds_smallest_failing_integer_deterministically(self):
        failing = lambda value: value >= 5
        self.assertEqual(shrink_failing_example(st.integers(min_value=0, max_value=100), failing, seed=23), 5)
        self.assertEqual(shrink_failing_example(st.integers(min_value=0, max_value=100), failing, seed=23), 5)

    def test_alias_uses_event_input_with_fixed_actor_and_offline_controls(self):
        batch = generate_scenarios(
            inventory_sha256=INVENTORY_SHA,
            descriptor=_descriptor(),
            host_id="koishi",
            seed=17,
        )
        alias = next(item for item in batch.scenarios if item["case"]["kind"] == "alias")
        step = alias["steps"][0]
        self.assertEqual(step["kind"], "event")
        self.assertEqual(step["event"]["message"][0]["text"].split()[0], "hello")
        self.assertEqual(step["event"]["actor_id"], alias["actors"][0]["actor_id"])
        self.assertEqual(alias["controls"]["network"], "offline")
        self.assertEqual(alias["controls"]["fixed_time_utc"], "2026-10-26T00:00:00Z")

    def test_group_profile_context_is_bound_to_generated_scenarios(self):
        batch = generate_scenarios(
            inventory_sha256=INVENTORY_SHA,
            descriptor=_descriptor(),
            host_id="koishi",
            seed=17,
            conversation_kind="group",
        )

        self.assertEqual(batch.to_dict()["generator"]["version"], "3")
        for scenario in batch.scenarios:
            self.assertEqual(scenario["conversations"][0]["kind"], "group")
            self.assertTrue(scenario["conversations"][0]["conversation_id"].startswith("group-"))
            step = scenario["steps"][0]
            if step["kind"] == "event":
                self.assertEqual(step["event"]["conversation"]["kind"], "group")

    def test_invalid_conversation_kind_is_rejected(self):
        with self.assertRaises(ScenarioGenerationError):
            generate_scenarios(
                inventory_sha256=INVENTORY_SHA,
                descriptor=_descriptor(),
                host_id="koishi",
                seed=17,
                conversation_kind="channel",
            )

    def test_nonebot_untyped_command_uses_raw_text_and_fixed_controls(self):
        descriptor = {
            "framework": "nonebot",
            "command_id": "echo",
            "invocation": "/echo",
            "usage": "/echo <text>",
            "arguments": [],
            "options": [],
            "aliases": [],
        }
        batch = generate_scenarios(
            inventory_sha256=INVENTORY_SHA,
            descriptor=descriptor,
            host_id="nonebot",
            seed=7,
        )
        self.assertTrue(all(item["case"]["kind"] == "raw_text" for item in batch.scenarios))
        self.assertTrue(all("raw_text" in item["steps"][0]["inputs"] for item in batch.scenarios))
        self.assertIn("", [item["steps"][0]["inputs"]["raw_text"] for item in batch.scenarios])

    def test_raw_text_empty_boundary_is_reserved_before_scenario_cap(self):
        descriptor = {
            "framework": "nonebot",
            "command_id": "echo",
            "invocation": "/echo",
            "usage": "/echo " + " ".join(f"value{index}" for index in range(64)),
            "arguments": [],
            "options": [],
            "aliases": [],
        }
        batch = generate_scenarios(
            inventory_sha256=INVENTORY_SHA,
            descriptor=descriptor,
            host_id="nonebot",
            seed=7,
            max_scenarios=16,
        )

        self.assertLessEqual(len(batch.scenarios), 16)
        self.assertTrue(any(item["steps"][0]["arguments"] == "" for item in batch.scenarios))
        self.assertTrue(any(item["steps"][0]["arguments"].strip() for item in batch.scenarios))

    def test_typed_command_gets_empty_input_when_budget_allows(self):
        descriptor = {
            "framework": "nonebot",
            "command_id": "optional",
            "invocation": "/optional",
            "arguments": [{"name": "text", "type": "text", "required": False}],
            "options": [],
            "aliases": [],
        }
        batch = generate_scenarios(
            inventory_sha256=INVENTORY_SHA,
            descriptor=descriptor,
            host_id="nonebot",
            seed=19,
            max_scenarios=4,
        )

        self.assertTrue(any(item["steps"][0]["arguments"] == "" for item in batch.scenarios))
        self.assertTrue(any(item["steps"][0]["arguments"].strip() for item in batch.scenarios))

    def test_volatile_only_repetitions_are_unknown_and_stable_payload_compares(self):
        only_timestamps = compare_repeated_observations(
            {"timestamp": "2026-11-10T10:00:00Z"},
            {"timestamp": "2026-11-10T10:00:01Z"},
        )
        self.assertEqual((only_timestamps["status"], only_timestamps["reason"]), ("unknown", "nondeterministic"))

        stable = compare_repeated_observations(
            {"message": "ok 2026-11-10T10:00:00Z", "event_id": "123"},
            {"message": "ok 2026-11-10T10:00:01Z", "event_id": "456"},
        )
        self.assertEqual(stable["status"], "stable")
        self.assertIn("<volatile:timestamp>", stable["normalized_first"]["message"])

    def test_invalid_hash_seed_budget_and_framework_are_rejected(self):
        base = {
            "inventory_sha256": INVENTORY_SHA,
            "descriptor": _descriptor(),
            "host_id": "koishi",
        }
        with self.assertRaises(ScenarioGenerationError):
            generate_scenarios(**{**base, "inventory_sha256": "bad"}, seed=1)
        with self.assertRaises(ScenarioGenerationError):
            generate_scenarios(**base, seed=-1)
        with self.assertRaises(ScenarioGenerationError):
            generate_scenarios(**base, seed=1, max_scenarios=17)
        with self.assertRaises(ScenarioGenerationError):
            generate_scenarios(**{**base, "descriptor": {**_descriptor(), "framework": "unknown"}}, seed=1)


if __name__ == "__main__":
    unittest.main()
