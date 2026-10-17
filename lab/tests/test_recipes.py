from __future__ import annotations

import copy
import unittest

from lab.recipes import (
    ECOSYSTEMS,
    RECIPE_CATEGORIES,
    load_host_profile,
    load_recipe,
    prepare_recipe,
    suggest_recipe_categories,
    validate_recipe_catalog,
)
from lab.compare import compare_recipe_pair


_DEFAULT = object()


class RecipeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.entry = {
            "registry_entry_id": "nb-fixture-001",
            "ecosystem": "nonebot",
            "package": "example-plugin",
            "version": "1.2.3",
            "artifact_sha256": "a" * 64,
            "categories": ["message"],
        }
        self.profile = load_host_profile("nonebot")
        self.recipe = load_recipe("nonebot", "message")
        self.fixture = {
            "schema_version": 1,
            "fixture_id": "message-fixture-001",
            "category": "message",
            "values": {"event": {"text": "ping"}},
            "expected": {
                "events": [{"type": "message", "text": "ping"}],
                "operations": [{"target": "group:42", "text": "pong"}],
                "operation_receipts": [{"status": "accepted", "scope": "virtual"}],
                "cleanup": {"active_services": 0, "live_processes": []},
            },
        }
        self.config = {"fixture_id": "config-fixture-001", "values": {"prefix": "!"}}
        self.services = {}

    def prepared(self, *, recipe=None, config=_DEFAULT, services=_DEFAULT, fixture=_DEFAULT):
        return prepare_recipe(
            recipe or self.recipe,
            self.entry,
            self.profile,
            config=self.config if config is _DEFAULT else config,
            services=self.services if services is _DEFAULT else services,
            fixture=self.fixture if fixture is _DEFAULT else fixture,
        )

    def test_both_ecosystems_have_all_ten_schema_validated_recipes(self) -> None:
        result = validate_recipe_catalog()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["ecosystems"], {"nonebot": 10, "koishi": 10})
        for ecosystem in ECOSYSTEMS:
            self.assertEqual(
                {load_recipe(ecosystem, category)["category"] for category in RECIPE_CATEGORIES},
                set(RECIPE_CATEGORIES),
            )

    def test_binding_fingerprints_entry_artifact_profile_config_services_and_fixture(self) -> None:
        prepared = self.prepared()
        self.assertEqual(prepared["status"], "ready")
        binding = prepared["binding"]
        self.assertEqual(binding["registry_entry_id"], self.entry["registry_entry_id"])
        self.assertEqual(binding["artifact_sha256"], self.entry["artifact_sha256"])
        self.assertEqual(binding["profile_id"], "nonebot-lab-v1")
        self.assertEqual(binding["config_fixture_id"], self.config["fixture_id"])
        self.assertEqual(binding["behavior_fixture_id"], self.fixture["fixture_id"])
        self.assertEqual(binding["service_fixtures"], {})
        self.assertEqual(len(binding["binding_sha256"]), 64)
        self.assertNotIn("prefix", str(binding))

    def test_missing_behavior_configuration_and_service_fixtures_are_explicit_skips(self) -> None:
        missing_behavior = self.prepared(fixture=None)
        self.assertEqual((missing_behavior["status"], missing_behavior["reason_code"]), ("skipped", "missing_fixture"))

        config_recipe = load_recipe("nonebot", "config")
        config_fixture = {**copy.deepcopy(self.fixture), "category": "config", "fixture_id": "config-behavior", "expected": {
            "applied_config": {}, "loaded_config": {}, "cleanup": {"active_services": 0, "live_processes": []}
        }}
        missing_config = self.prepared(recipe=config_recipe, config=None, fixture=config_fixture)
        self.assertEqual((missing_config["status"], missing_config["reason_code"]), ("skipped", "missing_config_fixture"))

        storage_recipe = load_recipe("nonebot", "storage")
        storage_fixture = {
            "schema_version": 1, "fixture_id": "storage-fixture", "category": "storage", "values": {},
            "expected": {"state_before": {}, "state_after": {"key": "value"}, "state_after_restart": {"key": "value"}, "cleanup": {"active_services": 0, "live_processes": []}},
        }
        missing_service = self.prepared(recipe=storage_recipe, fixture=storage_fixture)
        self.assertEqual((missing_service["status"], missing_service["reason_code"]), ("skipped", "missing_service_fixture"))

    def test_incomplete_or_secret_fixture_fails_closed(self) -> None:
        incomplete = copy.deepcopy(self.fixture)
        del incomplete["expected"]["operations"]
        self.assertEqual(self.prepared(fixture=incomplete)["reason_code"], "invalid_fixture")

        secret_config = {"fixture_id": "config-secret", "values": {"api_token": "never persist"}}
        self.assertEqual(self.prepared(config=secret_config)["reason_code"], "invalid_config_fixture")

    def test_missing_random_time_or_external_credentials_are_skipped(self) -> None:
        random_fixture = copy.deepcopy(self.fixture)
        random_fixture["required_inputs"] = ["random_seed"]
        self.assertEqual(self.prepared(fixture=random_fixture)["reason_code"], "missing_random_fixture")

        time_fixture = copy.deepcopy(self.fixture)
        time_fixture["required_inputs"] = ["fixed_time_utc"]
        self.assertEqual(self.prepared(fixture=time_fixture)["reason_code"], "missing_time_fixture")

        credential_fixture = copy.deepcopy(self.fixture)
        credential_fixture["required_credentials"] = ["upstream_api"]
        self.assertEqual(self.prepared(fixture=credential_fixture)["reason_code"], "missing_credentials")

    def test_pair_oracle_rejects_both_sides_with_the_same_wrong_output(self) -> None:
        prepared = self.prepared()
        wrong = {
            "status": "completed",
            "binding": prepared["binding"],
            "observed": copy.deepcopy(self.fixture["expected"]),
        }
        wrong["observed"]["operations"][0]["target"] = "wrong-group"
        result = compare_recipe_pair(
            self.recipe, self.fixture, wrong, copy.deepcopy(wrong), expected_binding=prepared["binding"]
        )
        self.assertFalse(result.passed)
        self.assertEqual(sum(issue.code == "oracle_mismatch" for issue in result.issues), 2)

    def test_explicit_empty_response_can_pass_and_binding_mismatch_cannot(self) -> None:
        recipe = load_recipe("koishi", "console")
        profile = load_host_profile("koishi")
        entry = {**self.entry, "ecosystem": "koishi", "registry_entry_id": "ks-fixture-001"}
        fixture = {
            "schema_version": 1,
            "fixture_id": "console-fixture",
            "category": "console",
            "values": {"path": "/health"},
            "expected": {
                "http_routes": ["/health"],
                "http_responses": [],
                "assets": [],
                "cleanup": {"active_services": 0, "live_processes": []},
            },
        }
        service = {"fixture_id": "http-fixture", "values": {"responses": []}}
        prepared = prepare_recipe(recipe, entry, profile, config=None, services={"http": service}, fixture=fixture)
        self.assertEqual(prepared["status"], "ready")
        output = {"status": "completed", "binding": prepared["binding"], "observed": copy.deepcopy(fixture["expected"])}
        passed = compare_recipe_pair(recipe, fixture, output, copy.deepcopy(output), expected_binding=prepared["binding"])
        self.assertTrue(passed.passed)

        changed_binding = copy.deepcopy(output)
        changed_binding["binding"]["registry_entry_id"] = "another-entry"
        rejected = compare_recipe_pair(recipe, fixture, changed_binding, changed_binding, expected_binding=prepared["binding"])
        self.assertFalse(rejected.passed)
        self.assertIn("binding_mismatch", {issue.code for issue in rejected.issues})

    def test_category_mapping_keeps_unknown_metadata_visible(self) -> None:
        mapped = suggest_recipe_categories({"categories": ["command", "database", "mystery-tag"]})
        self.assertEqual(mapped["categories"], ["command", "storage"])
        self.assertEqual(mapped["unmapped_labels"], ["mystery-tag"])
        unmapped = suggest_recipe_categories({"source_record": {"tags": [{"label": "utility"}]}})
        self.assertEqual(unmapped["reason_code"], "unmapped_category")
        self.assertEqual(unmapped["unmapped_labels"], ["utility"])


if __name__ == "__main__":
    unittest.main()
