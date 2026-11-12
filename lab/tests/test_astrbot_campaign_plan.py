from __future__ import annotations

import hashlib
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from lab.astrbot_campaign_plan import plan_astrbot_command_campaign, register_campaign_plan
from lab.astrbot_census import LOCK_FILE
from lab.queue import StageQueue


def _sha(char: str) -> str:
    return char * 64


class AstrBotCampaignPlanTests(unittest.TestCase):
    @staticmethod
    def _plan(lock, census, lock_sha, report_char="c", *, admission_age_seconds=0,
              admission_hash_char="1", admitted_image=None, docker_server_version="29.1.3",
              memory_available_bytes=16_000_000_000):
        worker_image = "ligamina-astrbot-tool-runtime@sha256:" + "d" * 64
        admission = {
            "status": "admitted", "platform": "linux",
            "collected_at_epoch": int(time.time()) - admission_age_seconds,
            "capabilities": {"network_disabled": True, "cleanup_evidence_required": True},
            "facts": {
                "image": {"locked_reference": admitted_image or worker_image},
                "docker": {"server_version": docker_server_version},
                "resources": {"memory_available_bytes": memory_available_bytes},
            },
        }
        native_runtime = {
            "archive_sha256": _sha("e"), "manifest_sha256": _sha("f"),
            "site_packages_sha256": _sha("0"),
            "requirements_lock_sha256": hashlib.sha256(LOCK_FILE.read_bytes()).hexdigest(),
        }
        return plan_astrbot_command_campaign(
            lock, census, artifact_lock_sha256=lock_sha, census_report_sha256=_sha(report_char),
            worker_image=worker_image, admission=admission, admission_sha256=_sha(admission_hash_char),
            native_runtime=native_runtime,
        )

    def _fixture(self):
        entries = [
            {"registry_entry_id": f"ab-{index:02d}", "status": "frozen",
             "artifact": {"computed_digests": {"sha256": _sha(str(index))}}}
            for index in range(1, 6)
        ]
        entries.append({"registry_entry_id": "ab-unfrozen", "status": "download_failed"})
        lock = {"snapshot_id": "ab-test", "entries": entries}
        lock_sha = _sha("a")
        selection_sha = hashlib.sha256(b'["ab-01","ab-02","ab-03","ab-04","ab-05"]').hexdigest()
        census = {
            "schema_version": 1,
            "kind": "astrbot_linux_install_load_census",
            "status": "completed",
            "run_id": "census-run",
            "snapshot_id": "ab-test",
            "manifest": {
                "snapshot_id": "ab-test",
                "artifacts_lock_sha256": lock_sha,
                "selection_sha256": selection_sha,
                "selected_entries": 5,
            },
            "denominator": {
                "snapshot_entries": 6,
                "frozen_entries": 5,
                "selected_entries": 5,
                "terminal_entries": 5,
                "closed_exactly_once": True,
            },
            "results": [
                {"registry_entry_id": "ab-01", "artifact_sha256": _sha("1"), "status": "passed",
                 "result": {"side": {"status": "passed", "load": {
                     "commands": 1,
                     "command_descriptors": [{
                         "command_id": "echo",
                         "arguments": [{"name": "text", "required": True, "type": "string"}],
                         "description": "must never enter a command plan",
                     }],
                     "commands_truncated": False,
                 }}}},
                {"registry_entry_id": "ab-02", "artifact_sha256": _sha("2"), "status": "passed",
                 "result": {"side": {"status": "passed", "load": {"commands": 0}}}},
                {"registry_entry_id": "ab-03", "artifact_sha256": _sha("3"), "status": "failed",
                 "reason_code": "plugin_load_failed", "result": {"side": {"status": "failed"}}},
                {"registry_entry_id": "ab-04", "artifact_sha256": _sha("4"), "status": "unknown",
                 "reason_code": "worker_lost", "result": {"status": "unknown"}},
                {"registry_entry_id": "ab-05", "artifact_sha256": _sha("5"), "status": "skipped",
                 "reason_code": "dependency_unresolvable", "result": {"side": {"status": "skipped"}}},
            ],
        }
        return lock, census, lock_sha, selection_sha

    def test_plan_preserves_census_denominator_and_only_queues_passing_observed_commands(self) -> None:
        lock, census, lock_sha, selection_sha = self._fixture()

        plan = self._plan(lock, census, lock_sha)

        self.assertEqual(plan["denominator"], {
            "snapshot_entries": 6, "frozen_entries": 5, "selected_entries": 5,
        })
        self.assertEqual(plan["inputs"]["selection_sha256"], selection_sha)
        self.assertEqual(plan["counts"], {
            "load_passed": 2, "load_failed": 1, "load_skipped": 1, "load_unknown": 1,
            "inventory_backfill_required": 1, "inventory_backfill_jobs": 1,
            "inventory_truncated": 0, "observed_commands": 1,
            "differential_jobs": 1, "contract_status": "not_evaluated",
        })
        self.assertEqual([job["stage"] for job in plan["jobs"]], ["differential", "inventory"])
        self.assertEqual(plan["jobs"][0]["registry_entry_id"], "ab-01")
        self.assertNotIn("description", str(plan))
        self.assertEqual(plan["unresolved_inventory"][0]["registry_entry_id"], "ab-02")
        self.assertEqual(plan["dispositions"]["ab-04"]["status"], "unknown")
        self.assertEqual(plan["dispositions"]["ab-03"]["reason_code"], "plugin_load_failed")

    def test_queue_registration_is_fenced_and_idempotent(self) -> None:
        lock, census, lock_sha, _ = self._fixture()
        plan = self._plan(lock, census, lock_sha)
        with tempfile.TemporaryDirectory(prefix="ligamina-astrbot-campaign-plan-") as temporary:
            queue_path = Path(temporary) / "campaign.sqlite3"
            first = register_campaign_plan(str(queue_path), plan)
            second = register_campaign_plan(str(queue_path), plan)
            queue = StageQueue(queue_path)

            self.assertEqual(first["created"], 2)
            self.assertEqual(second["created"], 0)
            self.assertEqual(second["existing"], 2)
            self.assertEqual(queue.counts(plan["run_id"])["total"], 2)
            self.assertTrue(queue.run(plan["run_id"])["fencing_required"])

    def test_queue_registration_rejects_repository_local_state(self) -> None:
        lock, census, lock_sha, _ = self._fixture()
        plan = self._plan(lock, census, lock_sha)
        repository_queue = Path(__file__).resolve().parents[2] / ".test-astrbot-campaign.sqlite3"

        with self.assertRaisesRegex(ValueError, "outside the repository"):
            register_campaign_plan(str(repository_queue), plan)

    def test_plan_key_is_incremental_for_unchanged_frozen_inputs(self) -> None:
        lock, census, lock_sha, _ = self._fixture()
        first = self._plan(lock, census, lock_sha, report_char="c", admission_hash_char="1")
        second = self._plan(lock, census, lock_sha, report_char="e", admission_age_seconds=10,
                            admission_hash_char="2", memory_available_bytes=8_000_000_000)

        self.assertEqual(first["run_id"], second["run_id"])
        self.assertEqual(first["manifest_sha256"], second["manifest_sha256"])
        self.assertNotEqual(first["inputs"]["census_report_sha256"], second["inputs"]["census_report_sha256"])
        self.assertNotEqual(first["inputs"]["admission_sha256"], second["inputs"]["admission_sha256"])
        self.assertEqual(first["inputs"]["admission_environment_sha256"],
                         second["inputs"]["admission_environment_sha256"])
        with tempfile.TemporaryDirectory(prefix="ligamina-astrbot-campaign-refresh-") as temporary:
            queue_path = Path(temporary) / "campaign.sqlite3"
            register_campaign_plan(str(queue_path), first)
            resumed = register_campaign_plan(str(queue_path), second)
            self.assertEqual(resumed["created"], 0)
            self.assertEqual(resumed["existing"], 2)

    def test_plan_identity_changes_when_admitted_docker_environment_changes(self) -> None:
        lock, census, lock_sha, _ = self._fixture()
        first = self._plan(lock, census, lock_sha, docker_server_version="29.1.3")
        second = self._plan(lock, census, lock_sha, docker_server_version="29.2.0")

        self.assertNotEqual(first["inputs"]["admission_environment_sha256"],
                            second["inputs"]["admission_environment_sha256"])
        self.assertNotEqual(first["run_id"], second["run_id"])

    def test_plan_identity_changes_when_campaign_worker_sources_change(self) -> None:
        lock, census, lock_sha, _ = self._fixture()
        with patch("lab.astrbot_campaign_plan._campaign_source_identity", return_value={"sha256": _sha("a"), "files": {}}):
            first = self._plan(lock, census, lock_sha)
        with patch("lab.astrbot_campaign_plan._campaign_source_identity", return_value={"sha256": _sha("b"), "files": {}}):
            second = self._plan(lock, census, lock_sha)

        self.assertNotEqual(first["inputs"]["campaign_sources_sha256"], second["inputs"]["campaign_sources_sha256"])
        self.assertNotEqual(first["run_id"], second["run_id"])

    def test_in_progress_census_cannot_be_dispatched(self) -> None:
        lock, census, lock_sha, _ = self._fixture()
        census["status"] = "in_progress"

        with self.assertRaisesRegex(ValueError, "completed"):
            self._plan(lock, census, lock_sha)

    def test_stale_admission_cannot_register_campaign_work(self) -> None:
        lock, census, lock_sha, _ = self._fixture()

        with self.assertRaisesRegex(ValueError, "fresh admitted Linux report"):
            self._plan(lock, census, lock_sha, admission_age_seconds=1801)

    def test_admission_for_another_worker_image_cannot_register_campaign_work(self) -> None:
        lock, census, lock_sha, _ = self._fixture()
        wrong_image = "ligamina-astrbot-tool-runtime@sha256:" + "a" * 64

        with self.assertRaisesRegex(ValueError, "fresh admitted Linux report"):
            self._plan(lock, census, lock_sha, admitted_image=wrong_image)

    def test_plan_fails_closed_on_duplicate_or_artifact_mismatched_census_rows(self) -> None:
        lock, census, lock_sha, _ = self._fixture()
        census["results"].append(dict(census["results"][0]))
        with self.assertRaisesRegex(ValueError, "exactly once"):
            self._plan(lock, census, lock_sha)

        lock, census, lock_sha, _ = self._fixture()
        census["results"][0]["artifact_sha256"] = _sha("f")
        with self.assertRaisesRegex(ValueError, "artifact hash"):
            self._plan(lock, census, lock_sha)


if __name__ == "__main__":
    unittest.main()
