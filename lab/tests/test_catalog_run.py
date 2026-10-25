from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lab.catalog.snapshot import SOURCE_DEFINITIONS, derive_snapshot_id, pretty_json_bytes
from lab.catalog_run import CatalogRunError, _verify_snapshot_inputs, create_catalog_plan, load_catalog_units, parse_shard
from lab.queue import QueueError, StageQueue


def _write_json(path: Path, value: dict) -> bytes:
    raw = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    path.write_bytes(raw)
    return raw


def _write_verified_snapshot_inputs(root: Path) -> None:
    raw_dir = root / "raw"
    raw_dir.mkdir(exist_ok=True)
    raw_values = {
        "nonebot_registry": b"[]",
        "koishi_registry": b"{}",
        "koishi_official_plugins_page": b"<html></html>",
    }
    source_list = []
    for key, definition in SOURCE_DEFINITIONS.items():
        raw = raw_values[key]
        raw_path = f"raw/{definition['filename']}"
        (root / raw_path).write_bytes(raw)
        source_list.append({
            "source_key": key,
            "requested_url": definition["url"],
            "final_url": definition["url"],
            "raw_path": raw_path,
            "byte_length": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        })
    snapshot_id = derive_snapshot_id({source["source_key"]: source for source in source_list})
    catalog = {
        "schema_version": 1,
        "snapshot_id": snapshot_id,
        "entries": [{"registry_entry_id": "nb-fixture-001", "ecosystem": "nonebot"}],
        "counts": {"registry_entries": 1, "by_ecosystem": {"nonebot": 1, "koishi": 0}},
    }
    changes = {"schema_version": 1, "snapshot_id": snapshot_id}
    provenance = {
        "schema_version": 1,
        "snapshot_id": snapshot_id,
        "sources": source_list,
        "catalog_sha256": hashlib.sha256(pretty_json_bytes(catalog)).hexdigest(),
        "changes_sha256": hashlib.sha256(pretty_json_bytes(changes)).hexdigest(),
    }
    for name, value in (("catalog.json", catalog), ("changes.json", changes), ("provenance.json", provenance)):
        (root / name).write_bytes(pretty_json_bytes(value))


class CatalogRunPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="ligamina-catalog-plan-")
        self.root = Path(self.temporary.name) / "snapshot"
        self.root.mkdir()
        entry = {
            "registry_entry_id": "nb-fixture-001",
            "ecosystem": "nonebot",
            "package": "example-plugin",
            "advertised_version": "1.2.3",
            "source_record": {"tags": [{"label": "command"}, {"label": "unclear-topic"}]},
        }
        self.catalog = {"snapshot_id": "snapshot-fixture", "entries": [entry]}
        self.catalog_bytes = _write_json(self.root / "catalog.json", self.catalog)
        artifact = {
            "sha256": "a" * 64,
            "computed_digests": {"sha256": "a" * 64},
            "build_required": False,
        }
        self.artifact_lock = {
            "snapshot_id": "snapshot-fixture",
            "entries": [{
                "registry_entry_id": "nb-fixture-001",
                "status": "locked",
                "artifacts": [
                    {"platform": "windows-x86_64", "status": "locked", "artifact": artifact},
                    {"platform": "linux-x86_64", "status": "locked", "artifact": artifact},
                ],
            }],
        }
        artifact_bytes = _write_json(self.root / "artifacts.lock.json", self.artifact_lock)
        (self.root / "artifacts.lock.sha256").write_text(hashlib.sha256(artifact_bytes).hexdigest() + "  artifacts.lock.json\n", encoding="ascii")
        self.profile_lock = {
            "snapshot_id": "snapshot-fixture",
            "artifacts_lock_sha256": hashlib.sha256(artifact_bytes).hexdigest(),
            "counts": {"profile_units": 2},
            "entries": [{
                "registry_entry_id": "nb-fixture-001",
                "profiles": {
                    platform: {
                        "status": "locked",
                        "profile_id": f"nonebot-profile-{platform}",
                        "dependency_lock": {"sha256": ("b" if platform.startswith("windows") else "c") * 64},
                    }
                    for platform in ("windows-x86_64", "linux-x86_64")
                },
            }],
        }
        profile_bytes = _write_json(self.root / "profiles.lock.json", self.profile_lock)
        (self.root / "profiles.lock.sha256").write_text(hashlib.sha256(profile_bytes).hexdigest() + "  profiles.lock.json\n", encoding="ascii")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_load_catalog_units_binds_both_os_locks_and_unmapped_labels(self) -> None:
        with patch("lab.catalog_run._verify_snapshot_inputs", return_value={"snapshot_id": "snapshot-fixture"}):
            catalog, units = load_catalog_units(self.root, ("windows", "linux"))
        self.assertEqual(catalog["snapshot_id"], "snapshot-fixture")
        self.assertEqual(len(units), 2)
        self.assertEqual({unit["platform"] for unit in units}, {"windows", "linux"})
        self.assertEqual({tuple(unit["recipe_categories"]) for unit in units}, {("command",)})
        self.assertTrue(all(unit["behavior_fixture_status"] == "missing_fixture" for unit in units))
        self.assertTrue(all(unit["unmapped_category_labels"] == ["unclear-topic"] for unit in units))
        self.assertEqual(len({unit["dependency_lock_sha256"] for unit in units}), 2)
        self.assertTrue(all(unit["artifact_sha256"] == "a" * 64 for unit in units))

    def test_load_catalog_units_can_bind_a_separate_profile_lock_without_mutating_snapshot(self) -> None:
        original_sha = hashlib.sha256((self.root / "profiles.lock.json").read_bytes()).hexdigest()
        alternate = Path(self.temporary.name) / "profiles-py31210.json"
        value = json.loads((self.root / "profiles.lock.json").read_text(encoding="utf-8"))
        value["entries"][0]["profiles"]["windows-x86_64"]["profile_id"] = "nonebot-profile-py31210-windows"
        raw = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        alternate.write_bytes(raw)
        alternate.with_name(f"{alternate.name}.sha256").write_text(
            hashlib.sha256(raw).hexdigest() + f"  {alternate.name}\n", encoding="ascii",
        )

        with patch("lab.catalog_run._verify_snapshot_inputs", return_value={"snapshot_id": "snapshot-fixture"}):
            _, units = load_catalog_units(self.root, ("windows",), profile_lock_path=alternate)

        self.assertEqual(len(units), 1)
        self.assertEqual(units[0]["profile_id"], "nonebot-profile-py31210-windows")
        self.assertEqual(units[0]["profile_lock_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(hashlib.sha256((self.root / "profiles.lock.json").read_bytes()).hexdigest(), original_sha)

    def test_platform_independent_koishi_tarball_is_bound_to_both_os_units(self) -> None:
        self.catalog["entries"][0]["ecosystem"] = "koishi"
        _write_json(self.root / "catalog.json", self.catalog)
        self.artifact_lock["entries"][0]["artifacts"] = [
            {"platform": "all", "status": "locked", "artifact": {
                "sha256": "a" * 64,
                "computed_digests": {"sha256": "a" * 64},
                "build_required": False,
            }},
        ]
        artifact_bytes = _write_json(self.root / "artifacts.lock.json", self.artifact_lock)
        artifact_sha = hashlib.sha256(artifact_bytes).hexdigest()
        (self.root / "artifacts.lock.sha256").write_text(artifact_sha + "  artifacts.lock.json\n", encoding="ascii")
        self.profile_lock["artifacts_lock_sha256"] = artifact_sha
        profile_bytes = _write_json(self.root / "profiles.lock.json", self.profile_lock)
        (self.root / "profiles.lock.sha256").write_text(hashlib.sha256(profile_bytes).hexdigest() + "  profiles.lock.json\n", encoding="ascii")

        with patch("lab.catalog_run._verify_snapshot_inputs", return_value={"snapshot_id": "snapshot-fixture"}):
            _, units = load_catalog_units(self.root, ("windows", "linux"))

        self.assertEqual(len(units), 2)
        self.assertTrue(all(unit["artifact_status"] == "locked" for unit in units))
        self.assertTrue(all(unit["artifact_sha256"] == "a" * 64 for unit in units))

    def test_catalog_plan_persists_expected_stage_graph_and_resumes_idempotently(self) -> None:
        queue_path = Path(self.temporary.name) / "queue.sqlite3"
        with patch("lab.catalog_run._verify_snapshot_inputs", return_value={"snapshot_id": "snapshot-fixture"}):
            first = create_catalog_plan(self.root, queue_path, platforms=("linux",), run_id="test-run")
            second = create_catalog_plan(self.root, queue_path, platforms=("linux",), run_id="test-run", resume=True)
        self.assertEqual(first["status"], "planned")
        self.assertFalse(first["execution_started"])
        self.assertTrue(first["run_created"])
        self.assertEqual(first["expected_units"], 1)
        self.assertEqual(first["expected_stage_jobs"], 6)
        self.assertEqual(first["enqueue"], {"created": 6, "existing": 0})
        self.assertEqual(second["enqueue"], {"created": 0, "existing": 6})
        self.assertEqual(second["resume"]["state"], "running")
        queue = StageQueue(queue_path)
        self.assertEqual(queue.counts("test-run")["total"], 6)
        self.assertEqual(queue.run("test-run")["manifest_sha256"], first["manifest_sha256"])

    def test_existing_run_identity_cannot_reuse_different_profile_cache_inputs(self) -> None:
        queue_path = Path(self.temporary.name) / "cache-identity.sqlite3"
        with patch("lab.catalog_run._verify_snapshot_inputs", return_value={"snapshot_id": "snapshot-fixture"}):
            first = create_catalog_plan(self.root, queue_path, platforms=("linux",), run_id="cache-identity")
            self.profile_lock["entries"][0]["profiles"]["linux-x86_64"]["dependency_lock"]["sha256"] = "d" * 64
            profile_bytes = _write_json(self.root / "profiles.lock.json", self.profile_lock)
            (self.root / "profiles.lock.sha256").write_text(hashlib.sha256(profile_bytes).hexdigest(), encoding="ascii")
            with self.assertRaisesRegex(QueueError, "different frozen manifest"):
                create_catalog_plan(self.root, queue_path, platforms=("linux",), run_id="cache-identity")
        self.assertNotEqual(first["manifest"]["profile_lock_sha256"], hashlib.sha256(profile_bytes).hexdigest())

    def test_snapshot_lock_mismatch_and_resume_of_missing_run_fail_closed(self) -> None:
        with patch("lab.catalog_run._verify_snapshot_inputs", return_value={"snapshot_id": "snapshot-fixture"}):
            self.profile_lock["artifacts_lock_sha256"] = "0" * 64
            profile_bytes = _write_json(self.root / "profiles.lock.json", self.profile_lock)
            (self.root / "profiles.lock.sha256").write_text(hashlib.sha256(profile_bytes).hexdigest(), encoding="ascii")
            with self.assertRaises(CatalogRunError):
                load_catalog_units(self.root, ("linux",))
            self.profile_lock["artifacts_lock_sha256"] = hashlib.sha256((self.root / "artifacts.lock.json").read_bytes()).hexdigest()
            profile_bytes = _write_json(self.root / "profiles.lock.json", self.profile_lock)
            (self.root / "profiles.lock.sha256").write_text(hashlib.sha256(profile_bytes).hexdigest(), encoding="ascii")
            with self.assertRaises(CatalogRunError):
                create_catalog_plan(
                    self.root,
                    Path(self.temporary.name) / "missing-run.sqlite3",
                    platforms=("linux",),
                    run_id="absent",
                    resume=True,
                )

    def test_fast_snapshot_preflight_checks_raw_and_frozen_hashes_and_denominator(self) -> None:
        _write_verified_snapshot_inputs(self.root)
        verified = _verify_snapshot_inputs(self.root)
        self.assertEqual(verified["integrity"], "raw-and-frozen-document-hashes-passed")
        self.assertEqual(verified["registry_entries"], 1)

        raw_path = self.root / "raw" / SOURCE_DEFINITIONS["nonebot_registry"]["filename"]
        raw_path.write_bytes(b"[{}]")
        with self.assertRaisesRegex(CatalogRunError, "raw integrity mismatch"):
            _verify_snapshot_inputs(self.root)

    def test_shard_syntax_rejects_out_of_range_and_non_integer_values(self) -> None:
        self.assertEqual(parse_shard("2/5"), (2, 5))
        for invalid in ("5/5", "0/0", "x/4", "-1/2"):
            with self.subTest(invalid=invalid), self.assertRaises(CatalogRunError):
                parse_shard(invalid)


if __name__ == "__main__":
    unittest.main()
