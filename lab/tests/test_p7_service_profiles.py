from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from lab.automation import p7_differential_campaign as p7
from lab.automation.phase4_worker_side import _koishi_database_provider_module, _safe_diagnostic_summary
from lab.automation.p7_service_profiles import (
    PROFILE_PATHS,
    _validate_nonebot_provider_lock,
    validate_profile_documents,
)


def candidate_for_profile(profile: dict[str, object], ecosystem: str) -> dict[str, object]:
    sample = profile["sample"]
    assert isinstance(sample, dict)
    providers = profile["providers"]
    assert isinstance(providers, list)
    provider_names = [row["module"] if ecosystem == "nonebot" else row["package"] for row in providers]
    return {
        "registry_entry_id": sample["registry_entry_id"],
        "ecosystem": ecosystem,
        "package": sample["package"],
        "version": "2.3.1" if ecosystem == "nonebot" else "1.0.3",
        "artifact_sha256": "a" * 64,
        "profile_lock_sha256": "b" * 64,
        "profile_id": profile["profile_id"],
        "catalog_entry": {"entry_point": "nonebot_plugin_picstatus"},
        "profile_document_path": PROFILE_PATHS[ecosystem],
        "service_profile_sha256": hashlib.sha256(PROFILE_PATHS[ecosystem].read_bytes()).hexdigest(),
        "service_profile": profile,
        "preload_plugins": provider_names,
        "required_services": sorted({service for row in providers for service in row["services"]}),
    }


class P7OfflineServiceProfileTests(unittest.TestCase):
    def test_profile_providers_are_bound_to_frozen_catalog_artifacts(self) -> None:
        profiles = validate_profile_documents()

        self.assertEqual(profiles["koishi"]["profile_id"], "p7-koishi-offline-services-v1")
        self.assertEqual(profiles["nonebot"]["profile_id"], "p7-nonebot-offline-services-v1")
        self.assertEqual(
            {service for provider in profiles["koishi"]["providers"] for service in provider["services"]},
            {"database", "cron", "localstorage", "http"},
        )
        self.assertEqual(
            profiles["koishi"]["startup_accounts"],
            [{"account_id": "p7-offline-account", "guild_ids": ["p7-offline-group"]}],
        )
        self.assertIn("offline error-path", profiles["koishi"]["network_scope"])

    def test_nonebot_profile_lock_contains_hash_locked_provider_wheels(self) -> None:
        profiles = validate_profile_documents()
        profile = profiles["nonebot"]
        lock_text = "\n\n".join(
            f"{provider['distribution']}=={provider['version']} \\\n    --hash=sha256:{provider['artifact_sha256']}"
            for provider in profile["providers"]
        ) + "\n"
        with tempfile.TemporaryDirectory() as temporary:
            lock_path = Path(temporary) / "requirements.txt"
            lock_path.write_text(lock_text, encoding="utf-8")
            _validate_nonebot_provider_lock(lock_path, profile["providers"])

        self.assertEqual(profile["sample"]["registry_entry_id"], "nb-031f347fab50945ebffe96d4")
        self.assertEqual(
            [provider["module"] for provider in profile["providers"]],
            ["nonebot_plugin_localstore", "nonebot_plugin_apscheduler"],
        )

    def test_service_context_fingerprint_includes_variant_profile(self) -> None:
        profiles = validate_profile_documents()
        candidate = candidate_for_profile(profiles["nonebot"], "nonebot")

        request = p7._build_request(
            candidate,
            {"events": []},
            {"core_cli_sha256": "a" * 64},
            mode="native",
            seed=p7.FIXED_SEED,
            discover_commands=False,
            case_id="p7-08-profile-fingerprint",
        )

        context = request["context"]
        self.assertEqual(context["profile_document"], "samples/profiles/p7-nonebot-offline-services-v1.json")
        self.assertEqual(context["expected_environment"]["profile_sha256"], candidate["service_profile_sha256"])
        self.assertEqual(context["config"]["preload_plugins"], candidate["preload_plugins"])
        self.assertEqual(context["execution"]["required_services"], ["localstore", "scheduler"])

    def test_structured_koishi_database_provider_resolves_to_its_locked_package(self) -> None:
        profiles = validate_profile_documents()
        service_profile = profiles["koishi"]

        module = _koishi_database_provider_module(
            service_profile,
            [provider["package"] for provider in service_profile["providers"]],
        )

        self.assertEqual(module, "@koishijs/plugin-database-memory")

    def test_node_stack_summary_uses_the_error_line_not_the_source_line(self) -> None:
        raw = """/worker/runner.mjs:1
  throw new TypeError(`Koishi plugin ${label} has no callable plugin export`)
        ^

TypeError: Koishi plugin @koishijs/plugin-http has no callable plugin export
    at registerPluginModule (/worker/runner.mjs:1:1)
Node.js v24.20.0"""

        self.assertEqual(
            _safe_diagnostic_summary(raw),
            "TypeError: Koishi plugin @koishijs/plugin-http has no callable plugin export",
        )


if __name__ == "__main__":
    unittest.main()
