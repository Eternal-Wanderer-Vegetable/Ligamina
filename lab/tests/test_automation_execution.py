from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from lab.automation.execution import BehaviorExecutionError, run_behavior_pair


def fake_runner(*, bad_oracle: bool = False, missing_route: bool = False, bad_binding: bool = False, bad_cleanup: bool = False) -> list[str]:
    script = r'''
import json, pathlib, sys
request = json.load(sys.stdin)
execution = request["context"]["execution"]
workspace = pathlib.Path(execution["workspace"])
assert pathlib.Path.cwd().samefile(workspace)
marker = workspace / "state.json"
assert not marker.exists(), "workspace was reused across paths"
marker.write_text(json.dumps({"mode": execution["mode"]}))
mode = execution["mode"]
observation = {"response_or_operation_count": 0 if "bad-oracle" in sys.argv else 1, "recipient": "wrong-room" if "bad-oracle" in sys.argv else "group-1", "scope_leak_count": 0}
binding = dict(request["context"]["binding"])
if "bad-binding" in sys.argv:
  binding["registry_entry_id"] = "other-entry"
result = {
  "status": "completed",
  "binding": binding,
  "plugin_observation": observation,
  "cleanup": {"worker_cleanup_confirmed": "bad-cleanup" not in sys.argv, "active_capability_count": 0, "live_processes": []},
}
if mode == "native":
  result["native_trace"] = {"bridge_adapter_imported": False}
elif "missing-route" not in sys.argv:
  result["federated_trace"] = {"core_host_route": "passed", "host_pid_reaped": True, "gateway_operation_count": 0 if "bad-oracle" in sys.argv else 1}
print(json.dumps(result))
'''
    args = [sys.executable, "-c", script]
    for enabled, value in (
        (bad_oracle, "bad-oracle"),
        (missing_route, "missing-route"),
        (bad_binding, "bad-binding"),
        (bad_cleanup, "bad-cleanup"),
    ):
        if enabled:
            args.append(value)
    return args


def fake_v2_runner(*, wrong_content: bool = False) -> list[str]:
    script = r'''
import json, sys
request = json.load(sys.stdin)
context = request["context"]
assert "oracle" not in context
fixture = context["behavior_fixture"]
assert fixture["adapter"]["id"] == "nonebot.onebot.v11"
assert fixture["setup"]["plugin_config"] == {"mode": "fixture"}
observation = {
  "response_or_operation_count": 1,
  "operations": [{"params": {"message": [{"type": "text", "text": "wrong" if "wrong-content" in sys.argv else "right"}]}}],
}
result = {
  "status": "completed",
  "binding": context["binding"],
  "plugin_observation": observation,
  "cleanup": {"worker_cleanup_confirmed": True, "active_capability_count": 0, "live_processes": []},
}
if context["execution"]["mode"] == "native":
  result["native_trace"] = {"bridge_adapter_imported": False}
else:
  result["federated_trace"] = {"core_host_route": "passed", "host_pid_reaped": True, "gateway_operation_count": 1}
print(json.dumps(result))
'''
    args = [sys.executable, "-c", script]
    if wrong_content:
        args.append("wrong-content")
    return args


def fixture_case() -> tuple[dict, dict]:
    entry = {
        "registry_entry_id": "nb-example",
        "ecosystem": "nonebot",
        "package": "example-plugin",
        "version": "1.0.0",
        "artifact_sha256": "a" * 64,
        "dependency_locks": {
            "linux-x86_64": {
                "status": "eligible_not_run",
                "profile_id": "nonebot-linux",
                "dependency_lock_sha256": "b" * 64,
            }
        },
    }
    case = {
        "case_id": "example-positive",
        "class": "positive",
        "stimuli": [{"input": "fixture", "fixture_clock": "2026-10-20T07:00:00Z", "random_seed": 3, "session_scope": "session-a"}],
        "assertions": [
            {"path": "plugin_observation.response_or_operation_count", "operator": "equals", "value": 1},
            {"path": "plugin_observation.recipient", "operator": "equals", "value": "group-1"},
            {"path": "plugin_observation.scope_leak_count", "operator": "equals", "value": 0},
        ],
        "oracle": {"member": "plugin.py", "symbol": "handle", "review_status": "reviewed"},
    }
    return entry, case


class AutomationExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.entry, self.case = fixture_case()

    def test_pair_checks_binding_oracle_cleanup_independent_workspaces_and_core_host_trace(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ligamina-execution-test-") as directory:
            root = Path(directory)
            report = run_behavior_pair(
                self.entry, self.case, "linux-x86_64", fake_runner(), fake_runner(),
                fixture_context={"config": {"mode": "test"}}, workspace_root=root,
            )
            self.assertEqual(report["status"], "passed", report["comparison"]["issues"])
            self.assertTrue((root / "native/state.json").is_file())
            self.assertTrue((root / "federated/state.json").is_file())
            self.assertNotEqual((root / "native").resolve(), (root / "federated").resolve())
            self.assertEqual(report["federated"]["federated_trace"]["core_host_route"], "passed")

    def test_identical_wrong_behavior_on_both_paths_fails_independent_oracle(self) -> None:
        report = run_behavior_pair(
            self.entry, self.case, "linux-x86_64", fake_runner(bad_oracle=True), fake_runner(bad_oracle=True),
        )
        self.assertEqual(report["status"], "failed")
        self.assertEqual({item["code"] for item in report["comparison"]["issues"]}, {"oracle_mismatch"})

    def test_federated_result_without_core_host_trace_fails_closed(self) -> None:
        report = run_behavior_pair(
            self.entry, self.case, "linux-x86_64", fake_runner(), fake_runner(missing_route=True),
        )
        self.assertEqual(report["status"], "failed")
        self.assertIn("core_host_route_unproven", {item["code"] for item in report["comparison"]["issues"]})

    def test_missing_expected_case_fails_before_any_harness_starts(self) -> None:
        case = dict(self.case, assertions=[])
        with self.assertRaisesRegex(BehaviorExecutionError, "no independent expected assertions"):
            run_behavior_pair(self.entry, case, "linux-x86_64", fake_runner(), fake_runner())

    def test_fixture_context_cannot_forward_oracle_material_to_runner(self) -> None:
        with self.assertRaisesRegex(BehaviorExecutionError, "cannot contain expected oracle data"):
            run_behavior_pair(
                self.entry, self.case, "linux-x86_64", fake_runner(), fake_runner(),
                fixture_context={"nested": {"expected": {"response": "secret oracle"}}},
            )

    def test_wrong_binding_and_unconfirmed_cleanup_fail(self) -> None:
        wrong_binding = run_behavior_pair(
            self.entry, self.case, "linux-x86_64", fake_runner(bad_binding=True), fake_runner(),
        )
        self.assertIn("identity_mismatch", {item["code"] for item in wrong_binding["comparison"]["issues"]})
        unclean = run_behavior_pair(
            self.entry, self.case, "linux-x86_64", fake_runner(), fake_runner(bad_cleanup=True),
        )
        self.assertIn("cleanup_unconfirmed", {item["code"] for item in unclean["comparison"]["issues"]})

    def test_v2_fixture_forwards_dispatch_contract_but_not_oracle_and_checks_both_sides(self) -> None:
        case = {
            "case_id": "v2-example-positive",
            "class": "positive",
            "migration": {"status": "executable"},
            "adapter": {"id": "nonebot.onebot.v11"},
            "setup": {"plugin_config": {"mode": "fixture"}},
            "stimulus": {"events": [{"event_id": "event-1"}]},
            "determinism": {"clock": "2026-10-26T00:00:00Z", "random_seed": 1},
            "observation": {"projection": "message_operations"},
            "oracle": {"assertions": [
                {"path": "plugin_observation.response_or_operation_count", "operator": "equals", "value": 1},
                {"path": "plugin_observation.operations.0.params.message.0.text", "operator": "equals", "value": "right"},
            ]},
        }
        passed = run_behavior_pair(self.entry, case, "linux-x86_64", fake_v2_runner(), fake_v2_runner())
        self.assertEqual(passed["status"], "passed", passed["comparison"]["issues"])
        failed = run_behavior_pair(self.entry, case, "linux-x86_64", fake_v2_runner(wrong_content=True), fake_v2_runner(wrong_content=True))
        self.assertEqual(failed["status"], "failed")
        self.assertIn("oracle_mismatch", {issue["code"] for issue in failed["comparison"]["issues"]})


if __name__ == "__main__":
    unittest.main()
