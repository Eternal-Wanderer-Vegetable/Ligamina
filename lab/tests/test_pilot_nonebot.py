from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lab.pilot_nonebot import (
    NoneBotPilotError,
    _load_release_metadata,
    _pypi_json_url,
    _read_lock_requirements,
    _select_locked_wheel,
    _write_runner,
    run_selected_nonebot_pilot,
)


class NoneBotPilotLockTests(unittest.TestCase):
    def test_release_metadata_accepts_pep440_equivalent_version_spelling(self) -> None:
        requested_version = "13.0.3.0"
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            url = _pypi_json_url("cuda-toolkit", requested_version)
            key = hashlib.sha256(url.encode("utf-8")).hexdigest()
            path = cache / "pypi-wheel-metadata" / "sha256" / key[:2] / key
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"info": {"name": "cuda_toolkit", "version": "13.0.3"}, "urls": []}), encoding="utf-8")

            document, receipt = _load_release_metadata("cuda-toolkit", requested_version, cache)

        self.assertEqual(document["info"]["version"], "13.0.3")
        self.assertTrue(receipt["cache_hit"])

    def test_profile_requirements_are_exact_and_hash_locked(self) -> None:
        lock = """demo-pkg==1.2.3 \\
    --hash=sha256:{digest}
    # via direct
""".format(digest="a" * 64)
        parsed = _read_lock_requirements(lock)
        self.assertEqual(parsed[0]["canonical_name"], "demo-pkg")
        self.assertEqual(parsed[0]["version"], "1.2.3")
        self.assertEqual(parsed[0]["hashes"], {"a" * 64})
        with self.assertRaisesRegex(NoneBotPilotError, "exact PyPI pin"):
            _read_lock_requirements("demo-pkg>=1.2\n    --hash=sha256:" + "a" * 64)
        with self.assertRaisesRegex(NoneBotPilotError, "SHA-256 allowlist"):
            _read_lock_requirements("demo-pkg==1.2.3")

    def test_release_wheel_must_match_profile_hash_and_linux_tags(self) -> None:
        digest = "a" * 64
        requirement = {"name": "demo-pkg", "canonical_name": "demo-pkg", "version": "1.2.3", "hashes": {digest}}
        release = {"urls": [{
            "filename": "demo_pkg-1.2.3-py3-none-any.whl", "packagetype": "bdist_wheel",
            "digests": {"sha256": digest}, "size": 5, "url": "https://files.pythonhosted.org/packages/demo.whl",
            "requires_python": ">=3.9", "yanked": False,
        }]}
        selected = _select_locked_wheel(requirement, release)
        self.assertEqual(selected["sha256"], digest)
        with self.assertRaisesRegex(NoneBotPilotError, "dependency_wheel_unavailable"):
            _select_locked_wheel({**requirement, "hashes": {"b" * 64}}, release)
        release["urls"][0]["url"] = "https://evil.example/demo.whl"
        with self.assertRaisesRegex(NoneBotPilotError, "outside the allowlist"):
            _select_locked_wheel(requirement, release)

    def test_worker_runner_is_offline_hash_only_and_collects_scratch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _write_runner(Path(temporary))
            source = (Path(temporary) / "runner.py").read_text(encoding="utf-8")
            native = (Path(temporary) / "native_probe.py").read_text(encoding="utf-8")
        for expected in (
            "--no-index", "--require-hashes", "--only-binary=:all:", "peak_writable_scratch_bytes",
            "missing_config_fixture", "missing_credentials", "[redacted]", "output_overflow",
        ):
            self.assertIn(expected, source)
        self.assertIn("nonebot.load_plugin(entry)", native)
        self.assertIn("get_loaded_plugins()", native)


class NoneBotPilotResumeTests(unittest.TestCase):
    def test_selected_batch_checkpoint_resumes_without_redispatch(self) -> None:
        entry_id = "nb-test-entry"
        manifest = {
            "kind": "phase2_stratified_pilot_manifest",
            "minimum_per_ecosystem": 1,
            "snapshot_id": "snapshot-test",
            "catalog_assignment_ledger": [{
                "registry_entry_id": entry_id, "ecosystem": "nonebot", "package": "demo-pkg",
                "version": "1.0.0", "platform": "linux", "selected": True,
                "artifact_status": "locked", "profile_status": "locked", "registry_quality": "not_marked_failed",
            }],
        }
        manifest["manifest_sha256"] = hashlib.sha256(json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")).hexdigest()
        context = (
            {entry_id: {"ecosystem": "nonebot", "source_record": {"valid": True}}},
            {entry_id: {"profile_id": "profile-test"}},
            {}, {}, b"{}", b"{}", b"{}",
        )
        expected = {
            "result_id": f"catalog:{entry_id}|linux", "registry_entry_id": entry_id,
            "ecosystem": "nonebot", "package": "demo-pkg", "version": "1.0.0",
            "platform": "linux", "status": "skipped", "reason_code": "missing_entry_bound_fixture", "attempted": True, "attempt": 1,
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot = root / "snapshot"
            snapshot.mkdir()
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            checkpoint = root / "checkpoint.json"
            with patch("lab.pilot_nonebot._catalog_context", return_value=context), patch(
                "lab.pilot_nonebot.run_nonebot_load_probe", return_value=expected,
            ) as probe:
                first = run_selected_nonebot_pilot(
                    snapshot, manifest_path, cache_root=root, timeout_seconds=30, checkpoint_path=checkpoint,
                )
                resumed = run_selected_nonebot_pilot(
                    snapshot, manifest_path, cache_root=root, timeout_seconds=30,
                    checkpoint_path=checkpoint, resume_from=checkpoint,
                )
                probe.assert_called_once_with(snapshot.resolve(), entry_id, cache_root=root.resolve(), timeout_seconds=30, context=context)
        self.assertTrue(first["closed_exactly_once"])
        self.assertEqual(first["results"], [expected])
        self.assertEqual(resumed["results"], [expected])

    def test_resume_marks_inflight_worker_unknown_then_retries_with_new_attempt(self) -> None:
        entry_id = "nb-test-recovery"
        manifest = {
            "kind": "phase2_stratified_pilot_manifest", "minimum_per_ecosystem": 1,
            "snapshot_id": "snapshot-recovery",
            "catalog_assignment_ledger": [{
                "registry_entry_id": entry_id, "ecosystem": "nonebot", "package": "demo-pkg",
                "version": "1.0.0", "platform": "linux", "selected": True,
                "artifact_status": "locked", "profile_status": "locked", "registry_quality": "not_marked_failed",
            }],
        }
        manifest["manifest_sha256"] = hashlib.sha256(json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")).hexdigest()
        context = (
            {entry_id: {"ecosystem": "nonebot", "source_record": {"valid": True}}},
            {entry_id: {"profile_id": "profile-test"}}, {}, {}, b"{}", b"{}", b"{}",
        )
        result = {
            "result_id": f"catalog:{entry_id}|linux", "registry_entry_id": entry_id,
            "ecosystem": "nonebot", "package": "demo-pkg", "version": "1.0.0",
            "platform": "linux", "status": "skipped", "reason_code": "missing_entry_bound_fixture", "attempted": True,
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot = root / "snapshot"
            snapshot.mkdir()
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            checkpoint = root / "checkpoint.json"
            checkpoint.write_text(json.dumps({
                "task_id": "S2-09", "kind": "phase2_s209_nonebot_linux_pilot_workers",
                "pilot_manifest_sha256": manifest["manifest_sha256"], "selected_nonebot_linux_entries": 1,
                "results": [], "attempt_journal": [], "active_attempt": {
                    "result_id": f"catalog:{entry_id}|linux", "registry_entry_id": entry_id,
                    "ecosystem": "nonebot", "platform": "linux", "attempt": 1,
                    "status": "running", "stage": "prepare_and_worker", "started_at": "2026-10-12T00:00:00Z",
                },
            }), encoding="utf-8")
            with patch("lab.pilot_nonebot._catalog_context", return_value=context), patch(
                "lab.pilot_nonebot.run_nonebot_load_probe", return_value=result,
            ) as probe:
                report = run_selected_nonebot_pilot(
                    snapshot, manifest_path, cache_root=root, timeout_seconds=30,
                    checkpoint_path=checkpoint, resume_from=checkpoint,
                )
                probe.assert_called_once()
        self.assertEqual(report["results"][0]["attempt"], 2)
        self.assertEqual(report["attempt_journal"][0]["status"], "unknown")
        self.assertEqual(report["attempt_journal"][0]["reason_code"], "worker_lost_after_restart")
        self.assertEqual(report["attempt_journal"][0]["recovered_on_resume"], True)
        self.assertEqual(report["attempt_journal"][1]["status"], "skipped")
        self.assertTrue(report["closed_exactly_once"])


if __name__ == "__main__":
    unittest.main()
