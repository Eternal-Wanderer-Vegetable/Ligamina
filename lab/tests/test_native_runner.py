from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lab.baselines.nonebot.runner import NativeOneBotApiRecorder, _probe_observation
from lab.native_runner import execute_harness, load_case, run_pair


ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "lab/test_workers/fixture_harness.py"
PROFILE = ROOT / "samples/profiles/nonebot-group-v0.1.json"
LOCKFILE = ROOT / "hosts/nonebot/uv.lock"


def fixture_command(*inject: str) -> list[str]:
    return [
        sys.executable,
        str(HARNESS),
        "--profile",
        str(PROFILE),
        "--lockfile",
        str(LOCKFILE),
        *inject,
    ]


class NativeRunnerTests(unittest.TestCase):
    def test_probe_observation_returns_bound_source_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            path = work / "nonebot-data" / "phase4-probes" / "observations.ndjson"
            path.parent.mkdir(parents=True)
            row = {
                "message_id": "message-1",
                "account_id": "9000",
                "actor_id": "42",
                "conversation": {"kind": "group", "id": "77"},
                "timestamp": 123,
            }
            path.write_text(json.dumps(row) + "\n", encoding="utf-8")
            events = [{
                "event_id": "event-1",
                "extensions": {"onebot.v11": {"message_id": "message-1"}},
            }]

            result = _probe_observation(work, events)

        self.assertEqual(result["status"], "recorded")
        self.assertEqual(result["storage"]["record_count"], 1)
        self.assertEqual(result["events"], [{
            "event_id": "event-1",
            "account_id": "9000",
            "actor_id": "42",
            "conversation": {"kind": "group", "id": "77"},
            "timestamp": 123,
        }])

    def test_harness_environment_includes_only_source_bound_fixture_ca(self) -> None:
        from lab.automation.p7_bilibili_image_diagnostic import (
            BILIBILI_ENTRY_ID,
            profile_binding,
        )
        from lab.native_runner import _harness_environment

        sample = {
            "sample_id": f"p7-{BILIBILI_ENTRY_ID}",
            "ecosystem": "nonebot",
            "package": "nonebot-plugin-bilibili-image",
            "version": "0.0.2.1",
            "_artifact_hash": "a9c165f99d4cf41a2364141050e4646a7223fab3c152aecc2f975739938fec20",
        }
        profile = profile_binding({
            "ecosystem": sample["ecosystem"],
            "registry_entry_id": BILIBILI_ENTRY_ID,
            "package": sample["package"],
            "version": sample["version"],
            "artifact_sha256": sample["_artifact_hash"],
        })
        environment = _harness_environment("native", {"context": {
            "sample": sample,
            "bilibili_https_fixture_profile": profile,
        }})

        self.assertEqual(
            environment["SSL_CERT_FILE"],
            "/input/project/" + profile["tls_certificate_path"],
        )
        self.assertNotIn("HTTPS_PROXY", environment)

    def test_harness_gets_only_stimulus_and_logs_stay_out_of_json_stdout(self) -> None:
        case = load_case(ROOT / "samples/fixtures/event-routing/single-group-reply.json")
        script = (
            "import json, os, sys; "
            "payload=json.load(sys.stdin); "
            "assert 'expected' not in payload; "
            "print('harness diagnostic', file=sys.stderr); "
            "print(json.dumps({'status':'completed','mode':os.environ['LIGAMINA_RUN_MODE']}))"
        )
        result = execute_harness([sys.executable, "-c", script], case, "native", 5)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["mode"], "native")
        self.assertEqual(result["diagnostics"], "harness diagnostic\n")

    def test_context_is_passed_but_caller_secrets_are_not_inherited(self) -> None:
        case = load_case(ROOT / "samples/fixtures/event-routing/single-group-reply.json")
        case["context"] = {"artifact_hash": "frozen-digest"}
        script = (
            "import json, os, sys; payload=json.load(sys.stdin); "
            "assert payload['context']['artifact_hash']=='frozen-digest'; "
            "assert 'LIGAMINA_TEST_SECRET' not in os.environ; "
            "print(json.dumps({'status':'completed','context_seen':True}))"
        )
        with patch.dict("os.environ", {"LIGAMINA_TEST_SECRET": "must-not-cross"}):
            result = execute_harness([sys.executable, "-c", script], case, "native", 5)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["context_seen"])

    def test_missing_harness_is_reported_as_environment_missing(self) -> None:
        case = load_case(ROOT / "samples/fixtures/event-routing/single-group-reply.json")
        result = execute_harness([], case, "native", 5)
        self.assertEqual(result["status"], "environment_missing")

    def test_runner_executes_two_isolated_modes_and_accepts_the_fixture_case(self) -> None:
        case = load_case(ROOT / "samples/fixtures/event-routing/single-group-reply.json")
        command = fixture_command()
        report = run_pair(case, command, command, 5)
        self.assertEqual(report["native"]["mode"], "native")
        self.assertEqual(report["federated"]["mode"], "federated")
        self.assertEqual(report["comparison"]["status"], "passed", report["comparison"]["issues"])

    def test_runner_rejects_identical_wrong_behavior_and_single_side_mutations(self) -> None:
        case = load_case(ROOT / "samples/fixtures/event-routing/single-group-reply.json")
        same_fault = fixture_command("--inject", "wrong-recipient")
        report = run_pair(case, same_fault, same_fault, 5)
        self.assertEqual(report["comparison"]["status"], "failed")
        self.assertIn(
            "operation_expectation_failed",
            {issue["code"] for issue in report["comparison"]["issues"]},
        )

        native = fixture_command()
        federated = fixture_command("--inject", "leak-process")
        report = run_pair(case, native, federated, 5)
        self.assertEqual(report["comparison"]["status"], "failed")
        self.assertIn(
            "cleanup_expectation_failed",
            {issue["code"] for issue in report["comparison"]["issues"]},
        )

    def test_timeout_preserves_bounded_stderr_for_worker_diagnostics(self) -> None:
        result = execute_harness(
            [sys.executable, "-c", "import sys,time; print('P7-stage-marker', file=sys.stderr, flush=True); time.sleep(2)"],
            {"case_id": "timeout-diagnostics", "stimulus": {"events": []}, "context": {}},
            "native",
            0.1,
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["diagnostic"], "harness timed out")
        self.assertEqual(result["stderr"], "P7-stage-marker\n")


class NativeOneBotApiRecorderTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_forward_api_projects_native_message_segments(self) -> None:
        operations: list[dict] = []
        recorder = NativeOneBotApiRecorder(
            operations=operations,
            receipts=[],
            api_inputs=[],
            source_event_id=lambda: "event-message-segments",
            capture_source_event_id=True,
        )

        class MessageSegment:
            def __init__(self, segment_type: str, data: dict) -> None:
                self.type = segment_type
                self.data = data

        await recorder(
            SimpleNamespace(self_id="9000"),
            "send_group_forward_msg",
            group_id=42,
            messages=[{
                "type": "node",
                "data": {
                    "name": "Fixture Bot",
                    "uin": "9000",
                    "content": [
                        MessageSegment("text", {"text": "fixture title"}),
                        MessageSegment("image", {"file": "https://i0.hdslb.com/fixture/cover-a.jpg"}),
                    ],
                },
            }],
        )

        self.assertEqual(operations[0]["source_event_id"], "event-message-segments")
        self.assertEqual(operations[0]["params"]["message"], [{
            "type": "forward_node",
            "user_id": "9000",
            "nickname": "Fixture Bot",
            "content": [
                {"type": "text", "text": "fixture title"},
                {"type": "image_ref", "resource_ref": "https://i0.hdslb.com/fixture/cover-a.jpg"},
            ],
        }])

    async def test_group_forward_api_records_ordered_typed_nodes(self) -> None:
        operations: list[dict] = []
        receipts: list[dict] = []
        recorder = NativeOneBotApiRecorder(
            operations=operations,
            receipts=receipts,
            api_inputs=[],
            source_event_id=lambda: "event-forward",
            capture_source_event_id=True,
        )

        result = await recorder(
            SimpleNamespace(self_id="9000"),
            "send_group_forward_msg",
            group_id=42,
            messages=[
                {"type": "node", "data": {"name": "Alpha", "uin": "9000", "content": "first"}},
                {
                    "type": "node",
                    "data": {
                        "name": "Beta",
                        "uin": "9012",
                        "content": [
                            {"type": "text", "data": {"text": "second"}},
                            {"type": "at", "data": {"qq": "23"}},
                        ],
                    },
                },
            ],
        )

        self.assertEqual(result, {"message_id": "virtual-message-1"})
        self.assertEqual(operations, [{
            "operation_id": "operation-1",
            "account_id": "9000",
            "operation": "send_message",
            "target": {"kind": "group", "id": "42"},
            "params": {"message": [
                {"type": "forward_node", "user_id": "9000", "nickname": "Alpha", "content": "first"},
                {
                    "type": "forward_node",
                    "user_id": "9012",
                    "nickname": "Beta",
                    "content": [
                        {"type": "text", "text": "second"},
                        {"type": "mention", "user_id": "23"},
                    ],
                },
            ]},
            "source_event_id": "event-forward",
        }])
        self.assertEqual(receipts, [{
            "operation_id": "operation-1",
            "status": "succeeded",
            "target": {"kind": "group", "id": "42"},
            "account_id": "9000",
            "source_event_id": "event-forward",
        }])

    async def test_invalid_forward_inputs_fail_without_recording_operations(self) -> None:
        operations: list[dict] = []
        receipts: list[dict] = []
        recorder = NativeOneBotApiRecorder(
            operations=operations,
            receipts=receipts,
            api_inputs=[],
            source_event_id=lambda: "event-forward",
            capture_source_event_id=True,
        )
        bot = SimpleNamespace(self_id="9000")
        valid_node = {"type": "node", "data": {"name": "Bot", "uin": "9000", "content": "x"}}
        invalid_calls = [
            ({"group_id": 42, "messages": []}, "non-empty messages"),
            ({"group_id": "not-numeric", "messages": [valid_node]}, "numeric OneBot ID"),
            ({"group_id": 42, "messages": [{"type": "node", "data": {"name": "Bot", "uin": "bad", "content": "x"}}]}, "numeric OneBot ID"),
            ({"group_id": 42, "messages": [{"type": "node", "data": {"name": "Bot", "uin": "9000", "content": [{"type": "video", "data": {}}]}}]}, "unsupported forward node content"),
            ({"messages": [valid_node]}, "requires group_id"),
        ]

        for arguments, expected_message in invalid_calls:
            with self.subTest(arguments=arguments):
                operations: list[dict] = []
                receipts: list[dict] = []
                recorder = NativeOneBotApiRecorder(
                    operations=operations,
                    receipts=receipts,
                    api_inputs=[],
                    source_event_id=lambda: "event-forward",
                    capture_source_event_id=True,
                )
                with self.assertRaisesRegex(RuntimeError, expected_message):
                    await recorder(bot, "send_group_forward_msg", **arguments)
                self.assertEqual(operations, [])
                self.assertEqual(receipts, [])


if __name__ == "__main__":
    unittest.main()
