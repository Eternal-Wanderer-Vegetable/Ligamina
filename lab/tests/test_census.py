from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lab.catalog_run import build_stage_jobs
from lab.census import _handle_job, _unit_rows, aggregate_census
from lab.queue import StageQueue


def _canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _unit(platform: str = "linux") -> dict:
    return {
        "unit_id": f"nb-fixture|{platform}|catalog",
        "registry_entry_id": "nb-fixture",
        "ecosystem": "nonebot",
        "package": "nonebot-plugin-fixture",
        "version": "1.0.0",
        "platform": platform,
        "profile_id": "fixture-profile",
        "profile_status": "locked",
        "profile_reason_code": None,
        "dependency_lock_sha256": "b" * 64,
        "artifact_status": "locked",
        "artifact_reason_code": None,
        "artifact_sha256": "a" * 64,
        "case_id": "catalog",
        "recipe_categories": [],
        "unmapped_category_labels": ["other"],
        "behavior_fixture_status": "unmapped_category",
        "source_snapshot_sha256": "c" * 64,
        "artifact_lock_sha256": "d" * 64,
        "profile_lock_sha256": "e" * 64,
    }


class CensusQueueTests(unittest.TestCase):
    def _run_unit(self, platform: str, probe: dict | None = None) -> tuple[list[dict], dict]:
        with tempfile.TemporaryDirectory(prefix="ligamina-census-") as temporary:
            queue_path = Path(temporary) / "queue.sqlite3"
            queue = StageQueue(queue_path)
            unit = _unit(platform)
            manifest = {"snapshot_id": "snapshot", "unit_id": unit["unit_id"]}
            queue.create_run("census-test", "snapshot", "f" * 64, manifest)
            queue.enqueue_jobs("census-test", build_stage_jobs(unit))
            entries = {"nb-fixture": {"registry_quality": "valid", "source_record": {}}}
            units = {"nb-fixture": unit}
            worker_id = "census-test-worker"
            while True:
                job = queue.claim("census-test", worker_id, lease_seconds=60)
                if job is None:
                    break
                if job["stage"] == "install" and probe is not None:
                    with patch("lab.census._probe_for_unit", return_value=probe):
                        _handle_job(
                            queue, job, worker_id=worker_id, units_by_id=units, entries_by_id=entries,
                            snapshot_dir=Path(temporary), cache_root=Path(temporary), nonebot_context=None,
                            koishi_context=None, blocker_evidence_sha256="1" * 64, timeout_seconds=10,
                        )
                else:
                    _handle_job(
                        queue, job, worker_id=worker_id, units_by_id=units, entries_by_id=entries,
                        snapshot_dir=Path(temporary), cache_root=Path(temporary), nonebot_context=None,
                        koishi_context=None, blocker_evidence_sha256="1" * 64, timeout_seconds=10,
                    )
            return _unit_rows(queue, "census-test", [unit])

    def test_census_stores_native_install_and_cleanup_separately(self) -> None:
        protocol = {
            "dependency_install": {"status": "passed", "reason_code": None},
            "native_load": {"status": "passed", "reason_code": None},
            "cleanup": {"status": "passed", "reason_code": None},
        }
        probe = {
            "task_id": "S2-10", "status": "skipped", "reason_code": "missing_entry_bound_fixture",
            "attempted": True, "worker": {"status": "passed", "cleanup_confirmed": True},
            "protocol_report": protocol,
        }
        rows, summary = self._run_unit("linux", probe)

        self.assertEqual(rows[0]["unit_status"], "skipped")
        self.assertEqual(rows[0]["stage_statuses"], {
            "prepare": "passed", "install": "passed", "native": "passed",
            "federated": "skipped", "compare": "skipped", "cleanup": "passed",
        })
        self.assertTrue(rows[0]["cleanup_confirmed"])
        self.assertEqual(summary["unit_status"]["skipped"], 1)

    def test_windows_nonebot_app_control_is_unknown_and_does_not_claim_cleanup(self) -> None:
        rows, _ = self._run_unit("windows")

        self.assertEqual(rows[0]["unit_status"], "unknown")
        self.assertEqual(rows[0]["stage_statuses"]["install"], "unknown")
        self.assertEqual(rows[0]["stage_statuses"]["cleanup"], "skipped")
        self.assertIsNone(rows[0]["cleanup_confirmed"])
        self.assertEqual(rows[0]["probe_evidence"]["reason_code"], "app_control_win32_4551")


class CensusAggregateTests(unittest.TestCase):
    def test_aggregate_closes_both_os_and_keeps_per_plugin_report(self) -> None:
        units = [_unit("linux"), _unit("windows")]
        catalog = {"snapshot_id": "snapshot", "entries": [{"registry_entry_id": "nb-fixture"}]}
        reports = []
        with tempfile.TemporaryDirectory(prefix="ligamina-census-aggregate-") as temporary:
            root = Path(temporary)
            for platform in ("linux", "windows"):
                unit = next(item for item in units if item["platform"] == platform)
                row = {
                    "registry_entry_id": unit["registry_entry_id"],
                    "result_id": f"catalog:{unit['registry_entry_id']}|{platform}",
                    "ecosystem": unit["ecosystem"], "package": unit["package"], "version": unit["version"],
                    "platform": platform, "unit_status": "skipped",
                    "artifact_sha256": unit["artifact_sha256"],
                    "profile_lock_sha256": unit["profile_lock_sha256"],
                    "source_snapshot_sha256": unit["source_snapshot_sha256"],
                    "artifact_lock_sha256": unit["artifact_lock_sha256"],
                    "stage_statuses": {stage: "skipped" for stage in ("prepare", "install", "native", "federated", "compare", "cleanup")},
                }
                report = {
                    "task_id": "S2-10", "kind": "phase2_s210_census_shard", "status": "completed",
                    "snapshot_id": "snapshot", "platform": platform, "shard": {"index": 0, "count": 1},
                    "plugin_reports": [row],
                }
                report["report_sha256"] = _sha256(_canonical(report))
                path = root / f"{platform}.json"
                path.write_bytes(json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8") + b"\n")
                reports.append(path)

            output = root / "aggregate.json"
            with patch("lab.census.load_catalog_units", return_value=(catalog, units)):
                aggregated = aggregate_census(root, reports, output)
                partial = aggregate_census(root, reports[:1], root / "partial.json")

        self.assertEqual(aggregated["status"], "census-closed")
        self.assertTrue(aggregated["readiness"]["p2_census"] == "passed")
        self.assertEqual(aggregated["denominator"]["terminal_units"], 2)
        self.assertEqual(set(aggregated["per_plugin_reports"]["nb-fixture"]["platforms"]), {"linux", "windows"})
        self.assertEqual(partial["status"], "census-incomplete")
        self.assertEqual(partial["denominator"]["missing_unit_count"], 1)
        self.assertEqual(partial["denominator"]["missing_shard_count"], 1)


if __name__ == "__main__":
    unittest.main()
