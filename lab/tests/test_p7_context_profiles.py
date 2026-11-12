from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from lab.automation import p7_differential_campaign as p7
from lab.automation.phase4_docker_side import _worker_host_environment
from lab.automation.p7_context_profile_diagnostic import build_selection_document
from lab.automation.p7_source_context_profile_diagnostic import build_selection_document as build_source_context_selection
from lab.automation.p7_context_profiles import (
    PROFILE_DOCUMENT_PATH,
    profile_binding,
    profile_for_command,
    validate_profile_document,
)
from lab.baselines.federated import _environment as _federated_environment
from lab.baselines.federated import _test_context_profile_sha256 as _federated_profile_sha256
from lab.baselines.nonebot.runner import _test_context_profile_sha256 as _native_profile_sha256
from lab.representative import _copy_nonebot_context_settings


ANTIINSULT = {
    "registry_entry_id": "nb-47f239e4b2ed71a255157441",
    "package": "nonebot_plugin_antiinsult",
    "version": "0.7.1",
    "artifact_sha256": "7562ca642a1e08fb1d0507e3178501f4d87cb790a0b1b5c2935d65455850fd95",
}
ANTIRECALL = {
    "registry_entry_id": "nb-ed10c5014456c4e265167187",
    "package": "nonebot_plugin_antirecall",
    "version": "0.1.4",
    "artifact_sha256": "f85b8d122a60d89cbcc7b5a6c095cd28b2eca5bab6e2286f0f286462bdd9023c",
}
BILICHAT = {
    "registry_entry_id": "nb-250c92c282d77dcf502b9dbf",
    "package": "nonebot_plugin_bilichat",
    "version": "6.5.0",
    "artifact_sha256": "2fd7ac90900b4039fb71765ebf53886f6ade3593eb466fed5e615d2d82e02bb0",
}
ANTIRECALL_PRIVATE_COMMANDS = (
    "停用防撤回私聊",
    "启用防撤回私聊",
    "查看防撤回私聊",
)
KAWAII_ROBOT = {
    "registry_entry_id": "nb-5729c6688b584271718b810f",
    "package": "nonebot_plugin_kawaii_robot",
    "version": "4.1.5",
    "artifact_sha256": "dc92f205b3b10091bac1ae6ea6b77be998b62ac7e20966a552a445086374d81a",
}
KOISHI_XUN = {
    "registry_entry_id": "ko-d1c6923c1c0ddff7a16a31bf",
    "package": "koishi-plugin-xun-jiangshanfuzhu",
    "version": "0.0.2",
    "artifact_sha256": "f50452569da7fae897dc35addd00d758f20dc6096611bfc26a770d4bd3dd4917",
}


def _candidate(identity: dict[str, str]) -> dict[str, object]:
    return {
        **identity,
        "ecosystem": "nonebot",
        "profile_lock_sha256": "a" * 64,
        "catalog_entry": {"entry_point": identity["package"]},
        "preload_plugins": [],
    }


def _descriptor(command_id: str) -> dict[str, str]:
    return {
        "command_id": command_id,
        "invocation": f"/{command_id}",
        "framework": "nonebot",
        "usage": f"/{command_id} [arguments]",
    }


class P7ContextProfileTests(unittest.TestCase):
    def test_unprofiled_koishi_context_keeps_native_fingerprint_contract(self) -> None:
        candidate = {
            "registry_entry_id": "ko-unprofiled-context-test",
            "ecosystem": "koishi",
            "package": "koishi-plugin-example",
            "version": "1.0.0",
            "artifact_sha256": "d" * 64,
            "profile_lock_sha256": "e" * 64,
            "catalog_entry": {},
        }
        context = p7._build_context(
            candidate,
            PROFILE_DOCUMENT_PATH.parent,
            {},
            {"events": []},
            mode="native",
            seed=1,
            discover_commands=True,
        )

        self.assertNotIn("test_context_profile_sha256", context["config"])
        self.assertEqual(
            set(context["expected_environment"]),
            {
                "ecosystem",
                "framework_version",
                "runtime_version",
                "lockfile_sha256",
                "profile_sha256",
                "artifact_hash",
                "config_sha256",
                "stimulus_sha256",
            },
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            artifact = root / "artifact.bin"
            artifact.write_bytes(b"offline koishi artifact")
            lockfile = root / "package-lock.json"
            lockfile.write_text(
                json.dumps({"packages": {"node_modules/@koishijs/core": {"version": "4.18.11"}}}),
                encoding="utf-8",
            )
            candidate["artifact_sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
            candidate["profile_lock_sha256"] = hashlib.sha256(lockfile.read_bytes()).hexdigest()
            context = p7._build_context(
                candidate,
                root,
                {},
                {"events": []},
                mode="federated",
                seed=1,
                discover_commands=True,
            )
            context["fingerprint_paths"] = {
                "artifact": str(artifact),
                "lockfile": str(lockfile),
                "profile": str(p7.ROOT / "samples/profiles/koishi-group-v0.1.json"),
            }
            context["node_executable"] = "node"
            with patch(
                "lab.baselines.federated.subprocess.run",
                return_value=CompletedProcess(
                    args=["node", "--version"],
                    returncode=0,
                    stdout="v24.20.0\n",
                    stderr="",
                ),
            ):
                observed = _federated_environment(context, {"stimulus": {"events": []}})

        self.assertNotIn("test_context_profile_sha256", observed)

    def test_diagnostic_selection_is_the_exact_profile_bound_subset(self) -> None:
        document = build_selection_document()

        self.assertEqual(document["execution"], "source_subset_only_no_plugins_or_workers_ran")
        self.assertEqual(document["selection"]["counts"], {"koishi": 0, "nonebot": 3})
        self.assertEqual(
            {sample["entry"]["registry_entry_id"] for sample in document["samples"]},
            {ANTIINSULT["registry_entry_id"], ANTIRECALL["registry_entry_id"], BILICHAT["registry_entry_id"]},
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            artifact_cache = Path(temp_dir)
            catalog: dict[str, dict[str, object]] = {}
            artifacts: dict[str, dict[str, object]] = {}
            profiles: dict[str, dict[str, object]] = {}
            cache_hashes: dict[str, str] = {}
            for sample in document["samples"]:
                identity = sample["entry"]
                entry_id = identity["registry_entry_id"]
                artifact_path = f"{entry_id}.whl"
                lock_path = f"{entry_id}.lock"
                (artifact_cache / artifact_path).write_bytes(b"test artifact placeholder")
                (artifact_cache / lock_path).write_bytes(b"test profile-lock placeholder")
                cache_hashes[artifact_path] = identity["artifact_sha256"]
                cache_hashes[lock_path] = hashlib.sha256(b"test profile-lock placeholder").hexdigest()
                catalog[entry_id] = {
                    "registry_entry_id": entry_id,
                    "package": identity["package"],
                    "version": identity["version"],
                }
                artifacts[entry_id] = {
                    "distribution_name": identity["package"],
                    "artifacts": [{
                        "platform": "linux-x86_64",
                        "artifact": {"cache_path": artifact_path, "filename": f"{identity['package']}.whl"},
                    }],
                }
                profiles[entry_id] = {
                    "profiles": {
                        "linux-x86_64": {
                            "dependency_lock": {
                                "cache_path": lock_path,
                                "sha256": cache_hashes[lock_path],
                                "profile_id": "test-profile",
                            }
                        }
                    }
                }
            with (
                patch.object(p7, "ARTIFACT_CACHE", artifact_cache),
                patch.object(p7, "_frozen_records", return_value=(catalog, artifacts, profiles)),
                patch.object(p7, "_sha256_file", side_effect=lambda path: cache_hashes[Path(path).name]),
            ):
                candidates = p7.select_candidates(document, allow_profile_subset=True)

        self.assertEqual(len(candidates), 3)
        self.assertEqual({candidate["ecosystem"] for candidate in candidates}, {"nonebot"})

    def test_source_context_selection_and_candidate_gate_are_exact(self) -> None:
        document = build_source_context_selection()
        expected_ids = {
            ANTIINSULT["registry_entry_id"],
            ANTIRECALL["registry_entry_id"],
            BILICHAT["registry_entry_id"],
            KAWAII_ROBOT["registry_entry_id"],
            KOISHI_XUN["registry_entry_id"],
        }
        self.assertEqual(document["diagnostic_scope"]["kind"], "p7_source_bound_context_profiles")
        self.assertEqual(document["selection"]["counts"], {"koishi": 1, "nonebot": 4})
        self.assertEqual(
            {sample["entry"]["registry_entry_id"] for sample in document["samples"]},
            expected_ids,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            artifact_cache = Path(temp_dir)
            catalog: dict[str, dict[str, object]] = {}
            artifacts: dict[str, dict[str, object]] = {}
            profiles: dict[str, dict[str, object]] = {}
            cache_hashes: dict[str, str] = {}
            for sample in document["samples"]:
                identity = sample["entry"]
                entry_id = identity["registry_entry_id"]
                artifact_path = f"{entry_id}.artifact"
                lock_path = f"{entry_id}.lock"
                (artifact_cache / artifact_path).write_bytes(b"locked artifact placeholder")
                (artifact_cache / lock_path).write_bytes(b"test profile-lock placeholder")
                cache_hashes[artifact_path] = identity["artifact_sha256"]
                cache_hashes[lock_path] = hashlib.sha256(b"test profile-lock placeholder").hexdigest()
                catalog[entry_id] = {
                    "registry_entry_id": entry_id,
                    "package": identity["package"],
                    "version": identity["version"],
                }
                artifact_platform = "all" if sample["ecosystem"] == "koishi" else "linux-x86_64"
                artifacts[entry_id] = {
                    "distribution_name": identity["package"],
                    "artifacts": [{
                        "platform": artifact_platform,
                        "artifact": {"cache_path": artifact_path, "filename": f"{identity['package']}.bin"},
                    }],
                }
                profiles[entry_id] = {
                    "profiles": {
                        "linux-x86_64": {
                            "dependency_lock": {
                                "cache_path": lock_path,
                                "sha256": cache_hashes[lock_path],
                                "profile_id": "test-profile",
                            }
                        }
                    }
                }
            with (
                patch.object(p7, "ARTIFACT_CACHE", artifact_cache),
                patch.object(p7, "_frozen_records", return_value=(catalog, artifacts, profiles)),
                patch.object(p7, "_sha256_file", side_effect=lambda path: cache_hashes[Path(path).name]),
            ):
                candidates = p7.select_candidates(document, allow_source_context_subset=True)
        self.assertEqual({candidate["registry_entry_id"] for candidate in candidates}, expected_ids)

    def test_profile_document_is_schema_valid_and_bound_to_exact_artifacts(self) -> None:
        document = validate_profile_document()

        self.assertEqual(document["profile_id"], "p7-nonebot-source-bound-context-v1")
        self.assertEqual(PROFILE_DOCUMENT_PATH.name, "p7-nonebot-source-bound-context-v1.json")
        self.assertEqual(
            profile_for_command(_candidate(BILICHAT), ".otp")["conversation_kind"],
            "private",
        )
        self.assertEqual(
            profile_for_command(_candidate(BILICHAT), ".check")["mention_bot"],
            True,
        )
        self.assertIsNone(profile_for_command(_candidate(BILICHAT), "unprofiled"))

        changed_artifact = _candidate({**BILICHAT, "artifact_sha256": "0" * 64})
        with self.assertRaisesRegex(ValueError, "artifact hash"):
            profile_for_command(changed_artifact, ".otp")

    def test_nonebot_private_and_superuser_profiles_match_the_declared_commands(self) -> None:
        antirecall = profile_binding(_candidate(ANTIRECALL))
        self.assertIsNotNone(antirecall)
        for command_id in ANTIRECALL_PRIVATE_COMMANDS:
            context = antirecall["command_contexts"][command_id]
            self.assertEqual(context["conversation_kind"], "private")
            self.assertEqual(context["authorization"], "superuser")
        private_events, _ = p7.build_scenario_batch(
            _candidate(ANTIRECALL),
            [_descriptor(command_id) for command_id in ANTIRECALL_PRIVATE_COMMANDS],
            seed=p7.FIXED_SEED,
        )
        for event in private_events:
            self.assertEqual(event["conversation"]["kind"], "private")
            self.assertEqual(event["conversation"]["id"], event["actor_id"])
            self.assertEqual(event["actor_id"], antirecall["synthetic_superuser_id"])

        kawaii = profile_binding(_candidate(KAWAII_ROBOT))
        self.assertIsNotNone(kawaii)
        command = kawaii["command_contexts"]["重载词库"]
        self.assertEqual(command["authorization"], "superuser")
        self.assertEqual(command["actor_id"], kawaii["synthetic_superuser_id"])
        kawaii_candidate = _candidate(KAWAII_ROBOT)
        kawaii_events, _ = p7.build_scenario_batch(
            kawaii_candidate,
            [_descriptor("重载词库")],
            seed=p7.FIXED_SEED,
        )
        for mode in ("native", "federated"):
            request = p7._build_request(
                kawaii_candidate,
                {"events": kawaii_events},
                {"core_cli_sha256": "b" * 64},
                mode=mode,
                seed=p7.FIXED_SEED,
                discover_commands=mode == "federated",
                case_id=f"kawaii-{mode}",
            )
            self.assertEqual(request["context"]["config"]["nonebot_config"], {
                "superusers": [kawaii["synthetic_superuser_id"]],
            })
            self.assertEqual(request["context"]["host_environment"]["SUPERUSERS"], '["900000002"]')

    def test_koishi_admin_profile_binds_same_synthetic_config_to_native_and_federated(self) -> None:
        candidate = {
            **KOISHI_XUN,
            "ecosystem": "koishi",
            "profile_lock_sha256": "a" * 64,
            "catalog_entry": {},
            "preload_plugins": [],
        }
        binding = profile_binding(candidate)
        self.assertIsNotNone(binding)
        command_id = "重置金钱"
        context_profile = binding["command_contexts"][command_id]
        self.assertEqual(context_profile["authorization"], "plugin_admin")
        self.assertEqual(context_profile["actor_id"], binding["synthetic_actor_id"])
        self.assertIn("购买", binding["command_contexts"])
        self.assertNotIn("购买 <itemId:string> <quantity:number>", binding["command_contexts"])

        events, generation = p7.build_scenario_batch(
            candidate,
            [_descriptor(command_id)],
            seed=p7.FIXED_SEED,
        )
        self.assertEqual(events[0]["actor_id"], binding["synthetic_actor_id"])
        purchase_events, _ = p7.build_scenario_batch(
            candidate,
            [_descriptor("购买")],
            seed=p7.FIXED_SEED,
        )
        self.assertTrue(purchase_events)
        self.assertTrue(all(event["actor_id"] == binding["synthetic_actor_id"] for event in purchase_events))
        native = p7._build_request(
            candidate,
            {"events": events},
            {"core_cli_sha256": "b" * 64},
            mode="native",
            seed=p7.FIXED_SEED,
            discover_commands=False,
            case_id="koishi-profile-native",
        )["context"]
        federated = p7._build_request(
            candidate,
            {"events": events},
            {"core_cli_sha256": "b" * 64},
            mode="federated",
            seed=p7.FIXED_SEED,
            discover_commands=True,
            case_id="koishi-profile-federated",
        )["context"]
        expected_config = {"adminId": [binding["synthetic_actor_id"]]}
        self.assertEqual(native["sample"]["fixture"]["plugin_config"], expected_config)
        self.assertEqual(federated["sample"]["fixture"]["plugin_config"], expected_config)
        self.assertEqual(native["test_context_profile"], federated["test_context_profile"])
        self.assertEqual(
            native["expected_environment"]["test_context_profile_sha256"],
            binding["profile_sha256"],
        )
        self.assertEqual(_federated_profile_sha256(federated), binding["profile_sha256"])
        worker_environment = _worker_host_environment(federated)
        self.assertNotIn("SUPERUSERS", worker_environment)
        self.assertEqual(
            federated["sample"]["fixture"]["plugin_config"],
            {"adminId": [binding["synthetic_actor_id"]]},
        )
        self.assertEqual(
            generation["test_context_profile_sha256"],
            native["expected_environment"]["test_context_profile_sha256"],
        )

        tampered = json.loads(json.dumps(federated))
        tampered["sample"]["fixture"]["plugin_config"]["adminId"] = ["real-user"]
        with self.assertRaisesRegex(ValueError, "source-bound profile"):
            _federated_profile_sha256(tampered)
        with self.assertRaisesRegex(ValueError, "source-bound profile"):
            _worker_host_environment(tampered)
        with self.assertRaisesRegex(ValueError, "source-bound profile"):
            _worker_host_environment(
                federated,
                {"events": [{"actor_id": "unprofiled-actor"}]},
            )

        changed_artifact = {**candidate, "artifact_sha256": "0" * 64}
        with self.assertRaisesRegex(ValueError, "artifact hash"):
            profile_binding(changed_artifact)

    def test_profile_drives_event_context_and_same_superuser_config_on_both_paths(self) -> None:
        candidate = _candidate(BILICHAT)
        events, generation = p7.build_scenario_batch(
            candidate,
            [_descriptor(".otp"), _descriptor(".check"), _descriptor(".unsub"), _descriptor("unprofiled")],
            seed=p7.FIXED_SEED,
        )
        by_command = {
            row["command_id"]: next(
                event for event in events if event["event_id"] == row["event_id"]
            )
            for row in generation["scenarios"]
            if row["kind"] == "raw_text"
        }

        private = by_command[".otp"]
        group = by_command[".check"]
        unsub = by_command[".unsub"]
        unchanged = by_command["unprofiled"]
        self.assertEqual(private["conversation"]["kind"], "private")
        self.assertEqual(private["conversation"]["id"], private["actor_id"])
        self.assertEqual(group["conversation"]["kind"], "group")
        self.assertTrue(any(segment.get("type") == "mention" for segment in group["message"]))
        self.assertEqual(unsub["actor_id"], private["actor_id"])
        self.assertEqual(unsub["conversation"]["kind"], "group")
        self.assertTrue(any(segment.get("type") == "mention" for segment in unsub["message"]))
        unsub_profile = profile_for_command(candidate, ".unsub")
        self.assertEqual(unsub_profile["authorization"], "superuser")
        self.assertTrue(any(
            source["path"] == "nonebot_plugin_bilichat/command/subs.py"
            and source["sha256"] == "dabc1249ae780ea07282914056829061f85931194429a2b29594d64227bd4e18"
            for source in profile_binding(candidate)["source_evidence"]
        ))
        self.assertNotEqual(unchanged["actor_id"], private["actor_id"])
        self.assertFalse(any(segment.get("type") == "mention" for segment in unchanged["message"]))

        stimulus = {"events": events}
        native = p7._build_request(
            candidate,
            stimulus,
            {"core_cli_sha256": "b" * 64},
            mode="native",
            seed=p7.FIXED_SEED,
            discover_commands=False,
            case_id="profile-native",
        )["context"]
        federated = p7._build_request(
            candidate,
            stimulus,
            {"core_cli_sha256": "b" * 64},
            mode="federated",
            seed=p7.FIXED_SEED,
            discover_commands=True,
            case_id="profile-federated",
        )["context"]

        native_superusers = native["config"]["nonebot_config"]["superusers"]
        federated_superusers = json.loads(federated["host_environment"]["SUPERUSERS"])
        self.assertEqual(native_superusers, [private["actor_id"]])
        self.assertEqual(federated_superusers, native_superusers)
        docker_environment = _worker_host_environment(federated)
        self.assertEqual(json.loads(docker_environment["SUPERUSERS"]), native_superusers)
        self.assertEqual(native["test_context_profile"], federated["test_context_profile"])
        self.assertEqual(
            _native_profile_sha256(native),
            _federated_profile_sha256(federated),
        )
        self.assertEqual(
            native["expected_environment"]["test_context_profile_sha256"],
            profile_binding(candidate)["profile_sha256"],
        )
        self.assertEqual(
            generation["test_context_profile_sha256"],
            native["expected_environment"]["test_context_profile_sha256"],
        )
        tampered = json.loads(json.dumps(native))
        tampered["test_context_profile"]["command_contexts"][".otp"]["authorization"] = "none"
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            _native_profile_sha256(tampered)
        with self.assertRaisesRegex(ValueError, "settings do not match"):
            _worker_host_environment({**federated, "host_environment": {}})

    def test_unmatched_package_has_no_authority_or_context_overrides(self) -> None:
        candidate = _candidate({
            "registry_entry_id": "nb-not-profiled",
            "package": "other-plugin",
            "version": "1.0.0",
            "artifact_sha256": "c" * 64,
        })

        self.assertIsNone(profile_binding(candidate))
        self.assertIsNone(profile_for_command(candidate, "anything"))

    def test_synthetic_superuser_setting_is_forwarded_to_host_as_validated_json(self) -> None:
        environment = {"LIGAMINA_NONEBOT_PLUGINS": "sample"}
        _copy_nonebot_context_settings(
            {"SUPERUSERS": '["900000002"]', "KEEP_OUT": "value"},
            environment,
        )

        self.assertEqual(environment["SUPERUSERS"], '["900000002"]')
        self.assertNotIn("KEEP_OUT", environment)
        with self.assertRaisesRegex(ValueError, "synthetic numeric IDs"):
            _copy_nonebot_context_settings({"SUPERUSERS": '["real-user"]'}, {})


if __name__ == "__main__":
    unittest.main()
