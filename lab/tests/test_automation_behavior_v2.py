from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from lab.automation.behavior_v2 import (
    SCHEMA_PATH,
    SUITE_PATH,
    BehaviorV2Error,
    build_phase4_suite,
    build_phase4_p402_suite,
    validate_phase4_suite,
)
from lab.automation.phase4_worker_side import _safe_diagnostic_summary
from lab.automation.phase4_campaign import _phase4_capability_results


class WorkerDiagnosticTests(unittest.TestCase):
    def test_summary_keeps_failure_context_and_scrubs_values_and_worker_paths(self) -> None:
        summary = _safe_diagnostic_summary(
            "Traceback (most recent call last):\n"
            "  File '/work/profile/private.py', line 12, in run\n"
            "RuntimeError: profile mismatch token=supersecret /work/profile/config.json "
            + "a" * 40
        )

        self.assertIn("RuntimeError: profile mismatch", summary)
        self.assertIn("<redacted-field>", summary)
        self.assertIn("<worker-path>", summary)
        self.assertNotIn("supersecret", summary)
        self.assertNotIn("a" * 40, summary)

    def test_structured_federated_failure_summary_omits_event_payload(self) -> None:
        summary = _safe_diagnostic_summary(
            'Traceback (most recent call last):\nRuntimeError: federated event did not complete: '
            '{"event_id":"event-1","status":"unknown",'
            '"error":"host request failed: request c-3 exceeded its deadline",'
            '"gateway_operation_count":0,"gateway_errors":["HTTP Error 404"]}'
        )

        self.assertIn("event_id=event-1", summary)
        self.assertIn("status=unknown", summary)
        self.assertIn("deadline", summary)
        self.assertIn("HTTP Error 404", summary)
        self.assertNotIn("message", summary)

    def test_node_failure_summary_keeps_exception_without_runtime_footer(self) -> None:
        summary = _safe_diagnostic_summary(
            "Error [ERR_MODULE_NOT_FOUND]: Cannot find package '/input/project/sample/artifact.bin'\n"
            "    at packageResolve (node:internal/modules/esm/resolve:800:9)\n"
            "Node.js v24.20.0"
        )

        self.assertIn("ERR_MODULE_NOT_FOUND", summary)
        self.assertNotIn("Node.js v24.20.0", summary)
        self.assertNotIn("/input/project", summary)


class CapabilityProbeAggregationTests(unittest.TestCase):
    def _attempts(self) -> list[tuple[str, dict, dict]]:
        suite, _ = build_phase4_suite()
        selected = [
            (entry, case)
            for entry in suite["entries"]
            for case in entry["cases"]
            if case.get("migration", {}).get("status") == "executable"
        ]
        attempts = []
        worker_index = 0
        for number in range(1, 4):
            for entry, case in selected:
                ecosystem = entry["ecosystem"]
                expected = [
                    {
                        "event_id": str(event["event_id"]),
                        "account_id": str(event["account_id"]),
                        "actor_id": str(event["actor_id"]),
                        "conversation": {"kind": str(event["conversation"]["kind"]), "id": str(event["conversation"]["id"])},
                        "timestamp": str(event["timestamp"]),
                    }
                    for event in case["stimulus"]["events"]
                ]
                attempt_id = f"{case['case_id']}-a{number}"
                attempt = {}
                for side in ("native", "federated"):
                    worker_id = f"worker-{worker_index}"
                    worker_index += 1
                    data_directory = "nonebot-data" if ecosystem == "nonebot" else ("native-koishi-data" if side == "native" else "koishi-data")
                    probe_file = (
                        "lab/baselines/nonebot/fixtures/phase4_probe_observer.py"
                        if ecosystem == "nonebot" else "lab/baselines/koishi/phase4_probe_plugin.mjs"
                    )
                    source_hash = hashlib.sha256((Path(__file__).resolve().parents[2] / probe_file).read_bytes()).hexdigest()
                    attempt[side] = {
                        "status": "completed",
                        "outer_worker": {"worker_id": worker_id, "cleanup_confirmed": True},
                        "capability_probe_observation": {
                            "status": "recorded",
                            "storage": {
                                "relative_path": f"{data_directory}/phase4-probes/observations.ndjson",
                                "file_sha256": "a" * 64,
                                "record_count": len(expected),
                                "probe_source_sha256": source_hash,
                            },
                            "events": copy.deepcopy(expected),
                        },
                    }
                attempts.append((attempt_id, case, attempt))
        return attempts

    def test_storage_and_clock_require_all_fresh_side_observations(self) -> None:
        result = _phase4_capability_results(self._attempts(), {"path": "service.json", "sha256": "b" * 64, "probe_id": "phase4-service-probe-v1"})

        self.assertEqual(result["storage"]["status"], "passed")
        self.assertEqual(result["storage"]["observed_side_runs"], 36)
        self.assertEqual(result["storage"]["record_count"], 54)
        self.assertEqual(result["storage"]["unique_worker_count"], 36)
        self.assertEqual(result["clock"]["status"], "passed")
        self.assertEqual(result["clock"]["matched_event_count"], 54)

    def test_scope_or_timestamp_mismatch_fails_clock_probe(self) -> None:
        attempts = self._attempts()
        attempts[0][2]["native"]["capability_probe_observation"]["events"][0]["actor_id"] = "wrong-actor"
        result = _phase4_capability_results(attempts, {"path": "service.json", "sha256": "b" * 64, "probe_id": "phase4-service-probe-v1"})

        self.assertEqual(result["storage"]["status"], "passed")
        self.assertEqual(result["clock"]["status"], "failed")


class BehaviorV2ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        cls.suite = json.loads(SUITE_PATH.read_text(encoding="utf-8"))
        cls.validator = Draft202012Validator(cls.schema, format_checker=FormatChecker())

    def _first_case(self) -> tuple[dict, dict]:
        suite = copy.deepcopy(self.suite)
        entry = next(row for row in suite["entries"] if row["registry_entry_id"] == "nb-16c80a826ca035f6c49ad69a")
        return suite, entry["cases"][0]

    def _assert_schema_invalid(self, suite: dict) -> None:
        self.assertTrue(list(self.validator.iter_errors(suite)))

    def test_golden_suite_preserves_all_v1_identities_and_is_explicitly_blocked(self) -> None:
        suite, _ = build_phase4_suite()
        result = validate_phase4_suite(SUITE_PATH, matrix_path=None, verify_artifacts=False)
        self.assertEqual(result["status"], "validated")
        self.assertEqual(result["p4_fixture_gate"], "blocked")
        self.assertEqual((result["entries"], result["cases"], result["executable_cases"], result["blocked_cases"]), (20, 60, 6, 54))
        self.assertEqual(suite["capability_probes"], self.suite["capability_probes"])

    def test_p402_four_sample_suite_preserves_identities_and_has_twelve_reviewed_cases(self) -> None:
        suite, matrix = build_phase4_p402_suite()
        with tempfile.TemporaryDirectory() as directory:
            suite_path = Path(directory) / "suite.json"
            matrix_path = Path(directory) / "matrix.json"
            suite_path.write_text(json.dumps(suite, ensure_ascii=False), encoding="utf-8")
            matrix_path.write_text(json.dumps(matrix, ensure_ascii=False), encoding="utf-8")
            result = validate_phase4_suite(suite_path, matrix_path, verify_artifacts=False)
        self.assertEqual(result["suite_id"], "phase4-linux-behavior-p402-v2r3")
        self.assertEqual((result["entries"], result["cases"]), (20, 60))
        self.assertEqual((result["executable_cases"], result["blocked_cases"]), (12, 48))
        help_ids = {"nb-0b37b4bb782b82c00e95a87f", "ko-fb5aa9ada6cacd62ec595c66"}
        help_entries = [entry for entry in suite["entries"] if entry["registry_entry_id"] in help_ids]
        self.assertEqual([case["migration"]["status"] for entry in help_entries for case in entry["cases"]], ["executable"] * 6)
        self.assertTrue(all(case["oracle"]["source_artifact_sha256"] == entry["artifact_sha256"] for entry in help_entries for case in entry["cases"]))

    def test_p402_help_config_is_limited_to_reviewed_values(self) -> None:
        suite, matrix = build_phase4_p402_suite()
        target = next(entry for entry in suite["entries"] if entry["registry_entry_id"] == "nb-0b37b4bb782b82c00e95a87f")
        target["cases"][0]["setup"]["plugin_config"]["arbitrary_import"] = "os"
        with tempfile.TemporaryDirectory() as directory:
            suite_path = Path(directory) / "suite.json"
            matrix_path = Path(directory) / "matrix.json"
            suite_path.write_text(json.dumps(suite, ensure_ascii=False), encoding="utf-8")
            matrix_path.write_text(json.dumps(matrix, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(BehaviorV2Error):
                validate_phase4_suite(suite_path, matrix_path, verify_artifacts=False)

    def test_reviewed_roll_candidate_has_session_interleaving_oracle_and_old_candidate_stays_blocked(self) -> None:
        suite, _ = build_phase4_suite()
        roll = next(row for row in suite["entries"] if row["registry_entry_id"] == "nb-16c80a826ca035f6c49ad69a")
        guess = next(row for row in suite["entries"] if row["registry_entry_id"] == "nb-5f9a0398ba3644f4256bbe83")
        isolation = next(case for case in roll["cases"] if case["class"] == "isolation")

        self.assertEqual(isolation["setup"]["plugin_config"], {})
        self.assertEqual(len(isolation["stimulus"]["events"]), 3)
        self.assertEqual(isolation["oracle"]["source_artifact_sha256"], roll["artifact_sha256"])
        self.assertEqual(isolation["oracle"]["assertions"][1]["path"], "plugin_observation.operations.0.params.message.0.text")
        self.assertTrue(all(case["migration"]["status"] == "blocked" for case in guess["cases"]))

        matrix = build_phase4_suite()[1]
        selection = matrix["candidate_selection_revision"]
        self.assertTrue(selection["authorized_by_user"])
        self.assertEqual(selection["previous_candidate"]["registry_entry_id"], "nb-5f9a0398ba3644f4256bbe83")
        self.assertEqual(selection["selected_candidate"]["registry_entry_id"], "nb-16c80a826ca035f6c49ad69a")

    def test_event_is_required_for_executable_case(self) -> None:
        suite, case = self._first_case()
        case["stimulus"].pop("events")
        self._assert_schema_invalid(suite)

    def test_adapter_is_required_for_executable_case(self) -> None:
        suite, case = self._first_case()
        case["adapter"] = None
        self._assert_schema_invalid(suite)

    def test_setup_is_required_for_executable_case(self) -> None:
        suite, case = self._first_case()
        case["setup"] = None
        self._assert_schema_invalid(suite)

    def test_oracle_is_required_for_executable_case(self) -> None:
        suite, case = self._first_case()
        case["oracle"] = None
        self._assert_schema_invalid(suite)

    def test_profile_hash_must_match_the_frozen_lock(self) -> None:
        suite = copy.deepcopy(self.suite)
        suite["entries"][0]["dependency_locks"]["linux-x86_64"]["dependency_lock_sha256"] = "0" * 64
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "suite.json"
            path.write_text(json.dumps(suite), encoding="utf-8")
            with self.assertRaisesRegex(BehaviorV2Error, "entry identity/source/profile changed"):
                validate_phase4_suite(path, matrix_path=None, verify_artifacts=False)

    def test_private_onebot_conversation_id_must_match_actor_id(self) -> None:
        suite, _ = build_phase4_suite()
        entry = next(row for row in suite["entries"] if row["registry_entry_id"] == "ko-163c3bd4cf87c72c24210251")
        case = next(row for row in entry["cases"] if row["case_id"] == "ko-koishijs-plugin-echo-isolation")
        private_event = next(event for event in case["stimulus"]["events"] if event["conversation"]["kind"] == "private")
        self.assertEqual(private_event["conversation"]["id"], private_event["actor_id"])
        self.assertEqual(case["setup"]["user_authority"][f"onebot.v11:{private_event['actor_id']}"], 3)

        private_event["actor_id"] = "24"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "suite.json"
            path.write_text(json.dumps(suite), encoding="utf-8")
            with self.assertRaisesRegex(BehaviorV2Error, "private OneBot conversation ID must equal actor ID"):
                validate_phase4_suite(path, matrix_path=None, verify_artifacts=False)


if __name__ == "__main__":
    unittest.main()
