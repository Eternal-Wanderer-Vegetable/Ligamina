from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from lab.astrbot_campaign_execute import (
    AstrBotCampaignSpecFactory,
    compare_astrbot_observations,
    execute_campaign_once,
    project_astrbot_contracts,
)
from lab.astrbot_campaign_plan import differential_jobs_for_inventory
from lab.automation.reconcile import ReconciliationResult
from lab.queue import QueueError, StageQueue
from lab.workers.base import WorkerResult, WorkerSpec


def _observation(reply: str) -> dict:
    return {
        "status": "completed",
        "tool_calls": {"steps": [[{
            "command_id": "echo",
            "is_error": False,
            "replies": [reply],
        }]]},
        "gateway_operation_count": 0,
        "gateway_errors": [],
        "host_alive_after_shutdown": False,
    }


class AstrBotObservationComparisonTests(unittest.TestCase):
    def test_equivalent_observations_store_hashes_without_raw_plugin_replies(self) -> None:
        result = compare_astrbot_observations(
            _observation("native reply"),
            _observation("native reply"),
            _observation("native reply"),
        )

        self.assertEqual(result["category"], "equivalent")
        self.assertEqual(result["reason_code"], "stable_native_federated_equal")
        self.assertEqual(result["scenario_count"], 1)
        self.assertNotIn("native reply", json.dumps(result))
        self.assertEqual(len(result["observation_sha256"]["native_first"]), 64)

    def test_native_variance_and_stable_federated_mismatch_remain_distinct(self) -> None:
        unstable = compare_astrbot_observations(
            _observation("first"), _observation("second"), _observation("federated"),
        )
        mismatch = compare_astrbot_observations(
            _observation("native"), _observation("native"), _observation("federated"),
        )

        self.assertEqual(unstable["category"], "nondeterministic")
        self.assertEqual(mismatch["category"], "non_equivalent")

    def test_empty_command_output_stays_unknown_and_gateway_activity_is_non_equivalent(self) -> None:
        empty = _observation("")
        no_observation = compare_astrbot_observations(empty, empty, empty)
        leaked = _observation("same")
        leaked["gateway_operation_count"] = 1
        gateway_activity = compare_astrbot_observations(_observation("same"), _observation("same"), leaked)

        self.assertEqual(no_observation["category"], "unknown")
        self.assertEqual(no_observation["reason_code"], "no_meaningful_command_observation")
        self.assertEqual(gateway_activity["category"], "non_equivalent")
        self.assertEqual(gateway_activity["reason_code"], "federated_gateway_activity")

    def test_comparison_reports_bounded_per_side_error_and_silent_call_counts(self) -> None:
        refused = _observation("")
        refused["tool_calls"]["steps"][0][0]["is_error"] = True
        silent = _observation("")
        mismatch = compare_astrbot_observations(_observation("reply"), _observation("reply"), refused)
        incomplete = compare_astrbot_observations(silent, silent, _observation("reply"))

        self.assertEqual(mismatch["call_signals"], {
            "native_first": {"calls": 1, "errors": 0, "silent": 0},
            "native_second": {"calls": 1, "errors": 0, "silent": 0},
            "federated": {"calls": 1, "errors": 1, "silent": 0},
        })
        # Signals are also kept when a side cannot be projected, so an operator can see why.
        self.assertEqual(incomplete["call_signals"]["native_first"], {"calls": 1, "errors": 0, "silent": 1})
        self.assertNotIn("reply", json.dumps(mismatch["call_signals"]))
        self.assertIsNone(compare_astrbot_observations(None, None, None)["call_signals"]["federated"])

    def test_contract_projection_evaluates_observed_mcp_calls_and_keeps_unstimulated_contracts_unknown(self) -> None:
        descriptors = [{
            "command_id": "echo",
            "invocation": "echo",
            "framework": "astrbot",
            "arguments": [{"name": "text", "required": True, "type": "string"}],
        }]
        federated = _observation("reply")
        federated["tool_calls"]["listed_command_tools"] = ["cmd.astrbot-p709.echo"]
        federated["tool_calls"]["command_tool_schemas"] = [{
            "name": "cmd.astrbot-p709.echo",
            "input_schema": {"type": "object", "properties": {"arguments": {"type": "string"}},
                             "required": ["arguments"], "additionalProperties": False},
        }]
        federated["tool_calls"]["steps"] = [[{
            "command_id": "echo", "is_error": False, "response_observed": True,
            "replies": ["reply"], "elapsed_ms": 15,
        }]]

        contracts = project_astrbot_contracts(
            "echo", descriptors, federated, worker_cleanup_confirmed=True,
        )

        self.assertEqual(contracts["C7"]["status"], "passed")
        self.assertEqual(contracts["C7"]["evidence"]["listed_command_count"], 1)
        self.assertEqual(contracts["C7"]["evidence"]["call_count"], 1)
        self.assertNotIn("reply", json.dumps(contracts))
        self.assertTrue(all(
            contracts[contract_id]["status"] == "unknown"
            for contract_id in ("C1", "C2", "C3", "C4", "C5", "C6", "C8")
        ))

    def test_contract_projection_fails_c7_when_command_is_unlisted(self) -> None:
        descriptors = [{"command_id": "echo", "invocation": "echo", "framework": "astrbot"}]
        federated = _observation("reply")
        federated["tool_calls"]["listed_command_tools"] = []
        federated["tool_calls"]["command_tool_schemas"] = []
        federated["tool_calls"]["steps"] = [[{
            "command_id": "echo", "is_error": True, "response_observed": True,
            "replies": [], "elapsed_ms": 10,
        }]]

        contracts = project_astrbot_contracts(
            "echo", descriptors, federated, worker_cleanup_confirmed=True,
        )

        self.assertEqual(contracts["C7"]["status"], "failed")
        self.assertEqual(contracts["C7"]["reason_code"], "mcp_command_not_listed")

    def test_contract_projection_fails_c7_when_call_exceeds_deadline(self) -> None:
        descriptors = [{"command_id": "echo", "invocation": "echo", "framework": "astrbot"}]
        federated = _observation("reply")
        federated["tool_calls"]["listed_command_tools"] = ["cmd.astrbot-p709.echo"]
        federated["tool_calls"]["command_tool_schemas"] = [{
            "name": "cmd.astrbot-p709.echo", "input_schema": {"type": "object"},
        }]
        federated["tool_calls"]["steps"] = [[{
            "command_id": "echo", "is_error": True, "response_observed": True,
            "replies": [], "elapsed_ms": 90_001,
        }]]

        contracts = project_astrbot_contracts(
            "echo", descriptors, federated, worker_cleanup_confirmed=True,
        )

        self.assertEqual(contracts["C7"]["status"], "failed")
        self.assertEqual(contracts["C7"]["reason_code"], "command_call_deadline_exceeded")


class AstrBotCampaignExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="ligamina-astrbot-executor-")
        self.root = Path(self.temporary.name)
        self.input_dir = self.root / "worker-input"
        self.input_dir.mkdir()
        self.queue = StageQueue(self.root / "campaign.sqlite3")
        self.run_id = "p7-09-test-run"
        self.queue.create_run(self.run_id, "ab-test", "a" * 64, {"schema_version": 1}, fencing_required=True)
        self.queue.enqueue_jobs(self.run_id, [self._job()])

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _job() -> dict:
        return {
            "unit_id": "astrbot-diff-unit", "registry_entry_id": "ab-test-entry", "ecosystem": "astrbot",
            "platform": "linux", "profile_id": "astrbot-4.29.1-native-federated-tool",
            "stage": "differential", "case_id": "echo", "payload": {"scenario_count": 1},
            "max_attempts": 2,
        }

    def _specs(self, job: dict) -> list[WorkerSpec]:
        return [WorkerSpec(
            run_id=job["run_id"], entry_id=job["registry_entry_id"], command=(side,), input_dir=self.input_dir,
            attempt_id=job["attempt_id"], generation=job["generation"], job_id=job["job_id"],
        ) for side in ("native-first", "native-second", "federated")]

    def test_claimed_job_runs_native_twice_and_federated_then_finishes_with_fenced_comparison(self) -> None:
        worker = _FakeWorker([_worker_result("native reply")] * 3)

        result = execute_campaign_once(
            self.queue, self.run_id, "executor-1", worker, self._specs,
        )

        job = self.queue.get_job(self.run_id, "astrbot-diff-unit")
        attempt = self.queue.list_attempts(self.run_id)[0]
        self.assertEqual(result["status"], "finished")
        self.assertEqual([spec.command[0] for spec in worker.specs], ["native-first", "native-second", "federated"])
        self.assertTrue(all(spec.attempt_id == attempt["attempt_id"] for spec in worker.specs))
        self.assertTrue(all(spec.generation == 1 and spec.job_id == job["job_id"] for spec in worker.specs))
        self.assertEqual(job["status"], "passed")
        self.assertEqual(job["result"]["comparison"]["category"], "equivalent")
        self.assertNotIn("native reply", json.dumps(job["result"]))

    def test_finished_queue_result_contains_sanitized_c7_projection(self) -> None:
        native = _observation("native reply")
        federated = _observation("federated reply")
        federated["tool_calls"]["listed_command_tools"] = ["cmd.astrbot-p709.echo"]
        federated["tool_calls"]["command_tool_schemas"] = [{
            "name": "cmd.astrbot-p709.echo",
            "input_schema": {"type": "object", "properties": {"arguments": {"type": "string"}},
                             "additionalProperties": False},
        }]
        federated["tool_calls"]["steps"] = [[{
            "command_id": "echo", "is_error": False, "response_observed": True,
            "replies": ["federated reply"], "elapsed_ms": 24,
        }]]

        result = execute_campaign_once(
            self.queue, self.run_id, "executor-1",
            _FakeWorker([_worker_result(native), _worker_result(native), _worker_result(federated)]),
            _ContractFactory(self.input_dir),
        )

        saved = self.queue.get_job(self.run_id, "astrbot-diff-unit")["result"]
        self.assertEqual(result["status"], "finished")
        self.assertEqual(saved["contracts"]["C7"]["status"], "passed")
        self.assertEqual(saved["contract_status"], "partial")
        self.assertNotIn("input_schema", json.dumps(saved))
        self.assertNotIn("federated reply", json.dumps(saved))
        self.assertEqual(saved["contracts"]["C7"]["evidence"]["command_schema_sha256"], hashlib.sha256(
            json.dumps(
                {"type": "object", "properties": {"arguments": {"type": "string"}},
                 "additionalProperties": False},
                sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest())

    def test_unconfirmed_worker_cleanup_quarantines_job_until_reconciliation_confirms_cleanup(self) -> None:
        worker = _FakeWorker([_worker_result("ignored", cleanup=False)])

        first = execute_campaign_once(self.queue, self.run_id, "executor-1", worker, self._specs)

        self.assertEqual(first["status"], "blocked")
        self.assertEqual(self.queue.run(self.run_id)["state"], "interrupted")
        job = self.queue.get_job(self.run_id, "astrbot-diff-unit")
        self.assertEqual(job["status"], "unknown")
        self.assertTrue(job["result"]["requires_reconciliation"])
        self.assertEqual(len(worker.specs), 1)
        with self.assertRaisesRegex(QueueError, "resumed before workers"):
            self.queue.claim(self.run_id, "other-worker")

        recovered = execute_campaign_once(
            self.queue, self.run_id, "executor-2", _FakeWorker([_worker_result("same")] * 3), self._specs,
            reconciler=_ConfirmedReconciler(),
        )

        self.assertEqual(recovered["status"], "finished")
        self.assertEqual(self.queue.get_job(self.run_id, "astrbot-diff-unit")["status"], "passed")
        attempts = self.queue.list_attempts(self.run_id)
        self.assertEqual([item["status"] for item in attempts], ["unknown", "passed"])
        self.assertTrue(attempts[0]["result"]["reconciliation"]["cleanup_confirmed"])

    def test_restart_reconciles_expired_fenced_lease_before_reclaiming(self) -> None:
        abandoned = self.queue.claim(self.run_id, "crashed-executor", lease_seconds=1, now=10)
        self.queue.interrupt_run(self.run_id, now=10)

        result = execute_campaign_once(
            self.queue, self.run_id, "resumed-executor", _FakeWorker([_worker_result("same")] * 3), self._specs,
            reconciler=_ConfirmedReconciler(),
        )

        self.assertEqual(result["status"], "finished")
        attempts = self.queue.list_attempts(self.run_id)
        self.assertEqual([attempt["status"] for attempt in attempts], ["unknown", "passed"])
        self.assertEqual(attempts[0]["attempt_id"], abandoned["attempt_id"])
        self.assertEqual(attempts[0]["result"]["reason_code"], "worker_lost")
        self.assertTrue(attempts[0]["result"]["reconciliation"]["cleanup_confirmed"])

    def test_inventory_result_is_sanitized_and_followup_registration_is_restartable(self) -> None:
        run_id = "p7-09-inventory-test"
        self.queue.create_run(run_id, "ab-test", "b" * 64, {"schema_version": 1}, fencing_required=True)
        self.queue.enqueue_jobs(run_id, [{
            **self._job(), "unit_id": "astrbot-inventory-unit", "stage": "inventory",
            "case_id": "runtime-command-inventory-backfill", "profile_id": "astrbot-4.29.1-runtime-inventory",
            "payload": {"inventory_backfill": True},
        }])
        factory = _InventoryAndFollowupFactory(self.input_dir)
        raw_inventory = {
            "status": "passed",
            "load": {"commands": [{
                "command_id": "echo", "arguments": [{"name": "text", "required": True, "type": "string"}],
                "description": "private plugin metadata must not persist",
            }], "commands_truncated": False},
        }

        inventory = execute_campaign_once(
            self.queue, run_id, "executor-1", _FakeWorker([_worker_result(raw_inventory)]), factory,
        )
        saved_inventory = self.queue.get_job(run_id, "astrbot-inventory-unit")["result"]
        self.assertEqual(inventory["status"], "finished")
        self.assertNotIn("private plugin metadata", json.dumps(saved_inventory))
        self.assertEqual(self.queue.counts(run_id)["total"], 2)

        differential = execute_campaign_once(
            self.queue, run_id, "executor-2", _FakeWorker([_worker_result(_observation("same"))] * 3), factory,
        )
        self.assertEqual(differential["status"], "finished")
        self.assertEqual(self.queue.get_job(run_id, "astrbot-diff-backfilled")["result"]["comparison"]["category"], "equivalent")

    def test_spec_factory_replays_the_frozen_scenario_batch_for_native_and_federated(self) -> None:
        root = Path(self.temporary.name)
        cache = root / "cache"
        cache.mkdir()
        bundle_root = root / "bundles"
        bundle_root.mkdir()
        runtime_archive = root / "runtime.tar.gz"
        runtime_archive.write_bytes(b"native-runtime")
        runtime = {
            "archive_sha256": hashlib.sha256(runtime_archive.read_bytes()).hexdigest(),
            "manifest_sha256": "c" * 64,
            "site_packages_sha256": "d" * 64,
            "requirements_lock_sha256": "e" * 64,
        }
        artifact_sha = "f" * 64
        entry = {
            "registry_entry_id": "ab-factory-entry", "status": "frozen",
            "artifact": {"computed_digests": {"sha256": artifact_sha}},
        }
        lock = {"snapshot_id": "ab-factory", "entries": [entry]}
        descriptors = [{
            "command_id": "echo",
            "arguments": [{"name": "text", "required": True, "type": "string"}],
        }]
        inputs = {
            "snapshot_id": "ab-factory", "artifacts_lock_sha256": "a" * 64,
            "selection_sha256": "b" * 64, "worker_image": "astrbot@sha256:" + "1" * 64,
            "native_runtime": runtime, "admission_environment_sha256": "2" * 64, "max_scenarios": 16,
        }
        inventory_sha, jobs = differential_jobs_for_inventory(
            snapshot_id=inputs["snapshot_id"], artifacts_lock_sha256=inputs["artifacts_lock_sha256"],
            selection_sha256=inputs["selection_sha256"], worker_image=inputs["worker_image"],
            native_runtime=runtime, admission_environment_sha256=inputs["admission_environment_sha256"],
            registry_entry_id=entry["registry_entry_id"], artifact_sha256=artifact_sha, descriptors=descriptors,
        )
        job = {
            **jobs[0], "run_id": "p7-09-factory", "job_id": "9" * 64,
            "attempt_id": "attempt-factory", "generation": 1,
        }
        plan = {"run_id": job["run_id"], "inputs": inputs}
        census = {"results": [{
            "registry_entry_id": entry["registry_entry_id"],
            "artifact_sha256": artifact_sha,
            "status": "passed",
            "result": {"side": {"status": "passed", "load": {
                "commands": 1, "command_descriptors": descriptors, "commands_truncated": False,
            }}},
        }]}
        queue = StageQueue(root / "factory.sqlite3")
        queue.create_run(job["run_id"], "ab-factory", "3" * 64, {"schema_version": 1}, fencing_required=True)

        def prepare(entry_value: dict, lock_entry: dict, bundle: Path, cache_root: Path) -> dict:
            bundle.mkdir(parents=True)
            (bundle / "plugin" / "fake-plugin").mkdir(parents=True)
            (bundle / "side").mkdir()
            host_worker = bundle / "project/hosts/astrbot/ligamina_astrbot/worker.py"
            host_worker.parent.mkdir(parents=True)
            host_worker.write_text("# frozen host entry\n", encoding="utf-8")
            return {"plugin_dir": "fake-plugin"}

        with patch("lab.astrbot_tool_campaign.prepare", side_effect=prepare):
            factory = AstrBotCampaignSpecFactory(
                plan=plan, census=census, lock=lock, queue=queue,
                cache_root=cache, bundle_root=bundle_root, runtime_archive=runtime_archive,
                repository_root=Path(__file__).resolve().parents[2],
            )
            specs = factory(job)

        self.assertEqual([spec.command[-1] for spec in specs], [
            "/input/side/astrbot_native_tool_side.py", "/input/side/astrbot_native_tool_side.py",
            "/input/side/astrbot_tool_side.py",
        ])
        self.assertEqual([spec.attempt_id for spec in specs], ["attempt-factory"] * 3)
        self.assertEqual(len({spec.input_dir for spec in specs}), 1)
        self.assertFalse((specs[0].input_dir / "request.json").exists())
        native_request = json.loads(specs[0].stdin_data.decode("utf-8"))
        federated_request = json.loads(specs[2].stdin_data.decode("utf-8"))
        self.assertEqual(native_request["runtime_archive_sha256"], runtime["archive_sha256"])
        self.assertEqual(federated_request["host_id"], "astrbot-p709")
        self.assertEqual(inventory_sha, jobs[0]["payload"]["inventory_sha256"])


def _worker_result(value: Any, *, cleanup: bool = True) -> WorkerResult:
    payload = _observation(value) if isinstance(value, str) else value
    stdout = json.dumps(payload).encode("utf-8")
    stderr = b""
    return WorkerResult(
        status="passed" if cleanup else "unknown", reason_code=None if cleanup else "worker_cleanup_unconfirmed",
        exit_code=0, elapsed_seconds=0.1, stdout=stdout, stderr=stderr,
        stdout_sha256=hashlib.sha256(stdout).hexdigest(), stderr_sha256=hashlib.sha256(stderr).hexdigest(),
        output_truncated=False, cleanup_confirmed=cleanup, worker_id="fake-worker",
    )


class _FakeWorker:
    def __init__(self, results: list[WorkerResult]) -> None:
        self.results = list(results)
        self.specs: list[WorkerSpec] = []

    def run(self, spec: WorkerSpec) -> WorkerResult:
        self.specs.append(spec)
        return self.results.pop(0)


class _ConfirmedReconciler:
    def reconcile(self, **kwargs) -> ReconciliationResult:
        return ReconciliationResult(True, (), (), None)


class _InventoryAndFollowupFactory:
    def __init__(self, input_dir: Path) -> None:
        self.input_dir = input_dir

    def __call__(self, job: dict) -> list[WorkerSpec]:
        sides = ("inventory",) if job["stage"] == "inventory" else ("native-first", "native-second", "federated")
        return [WorkerSpec(
            run_id=job["run_id"], entry_id=job["registry_entry_id"], command=(side,), input_dir=self.input_dir,
            attempt_id=job["attempt_id"], generation=job["generation"], job_id=job["job_id"],
        ) for side in sides]

    def enqueue_followups(self, queue: StageQueue, run_id: str) -> dict[str, int]:
        inventory = queue.get_job(run_id, "astrbot-inventory-unit")
        if not inventory.get("result"):
            return {"created": 0, "existing": 0}
        try:
            queue.get_job(run_id, "astrbot-diff-backfilled")
        except QueueError:
            pass
        else:
            return {"created": 0, "existing": 1}
        followup = {
            "unit_id": "astrbot-diff-backfilled", "registry_entry_id": "ab-test-entry", "ecosystem": "astrbot",
            "platform": "linux", "profile_id": "astrbot-4.29.1-native-federated-tool",
            "stage": "differential", "case_id": "echo", "payload": {"scenario_count": 1},
        }
        return queue.enqueue_jobs(run_id, [followup])


class _ContractFactory:
    def __init__(self, input_dir: Path) -> None:
        self.input_dir = input_dir

    def __call__(self, job: dict) -> list[WorkerSpec]:
        return [WorkerSpec(
            run_id=job["run_id"], entry_id=job["registry_entry_id"], command=(side,), input_dir=self.input_dir,
            attempt_id=job["attempt_id"], generation=job["generation"], job_id=job["job_id"],
        ) for side in ("native-first", "native-second", "federated")]

    def descriptors_for_job(self, _job: dict) -> list[dict]:
        return [{"command_id": "echo", "invocation": "echo", "framework": "astrbot"}]


if __name__ == "__main__":
    unittest.main()
