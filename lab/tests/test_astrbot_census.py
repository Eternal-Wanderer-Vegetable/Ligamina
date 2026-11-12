"""AB-04: deterministic stratified pilot selection and requirement-name parsing."""

from __future__ import annotations

import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import json
import hashlib
import tempfile
import zipfile
from types import SimpleNamespace
from unittest.mock import patch

from lab import astrbot_census as census
from lab.astrbot_census import _requirement_name, locked_names, main, metadata_dir_name, select_pilot


def _entry(entry_id: str, *notes: str, status: str = "frozen") -> dict:
    return {"registry_entry_id": entry_id, "status": status, "hosting_notes": list(notes)}


class AstrBotCensusSelectionTests(unittest.TestCase):
    @staticmethod
    def _full_census_fixture(root: Path) -> tuple[list[str], Path]:
        snapshot = root / "ab-test-snapshot"
        snapshot.mkdir()
        cache = root / "cache"
        archive = cache / "plugin.zip"
        archive.parent.mkdir()
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr("metadata.yaml", "name: astrbot_fixture\n")
            package.writestr("__init__.py", "# source fixture\n")
        archive_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
        entries = [
            {
                "registry_entry_id": entry_id,
                "status": status,
                "ecosystem": "astrbot",
                "hosting_notes": [],
                "artifact": {"cache_path": "plugin.zip", "computed_digests": {"sha256": archive_hash}},
                "inspection": {"plugin_root": ".", "requirements": []},
            }
            for entry_id, status in (("ab-01", "frozen"), ("ab-02", "frozen"), ("ab-03", "download_failed"))
        ]
        lock_path = snapshot / "artifacts.lock.json"
        lock_bytes = (json.dumps({"snapshot_id": "ab-test-snapshot", "entries": entries}) + "\n").encode("utf-8")
        lock_path.write_bytes(lock_bytes)
        (snapshot / "artifacts.lock.json.sha256").write_text(
            f"{hashlib.sha256(lock_bytes).hexdigest()}  artifacts.lock.json\n", encoding="ascii"
        )
        admission = root / "admission.json"
        admission.write_text(json.dumps({
            "status": "admitted",
            "capabilities": {"network_disabled": True, "cleanup_evidence_required": True},
        }), encoding="utf-8")
        output = root / "report.json"
        arguments = [
            "--snapshot", str(snapshot), "--admission", str(admission),
            "--image", "ligamina-astrbot-runtime@sha256:" + "a" * 64,
            "--bundle-root", str(root / "bundles"), "--cache-root", str(cache),
            "--output", str(output), "--queue-db", str(root / "queue.sqlite3"),
            "--full-census", "--workers", "1",
        ]
        return arguments, output

    def test_full_census_cli_plan_preserves_every_frozen_astrbot_identity(self) -> None:
        snapshot = Path(__file__).resolve().parents[2] / "catalogs" / "ab-15937e90d26b7caf9d12"
        output = StringIO()

        with redirect_stdout(output):
            status = main(["--snapshot", str(snapshot), "--full-census", "--plan-only"])

        plan = json.loads(output.getvalue())
        self.assertEqual(status, 0)
        self.assertEqual(plan["kind"], "astrbot_full_census_plan")
        self.assertEqual(plan["snapshot_id"], "ab-15937e90d26b7caf9d12")
        self.assertEqual(plan["denominator"]["frozen_entries"], 3265)
        self.assertEqual(plan["denominator"]["selected_entries"], 3265)
        self.assertEqual(plan["denominator"]["excluded_non_frozen"], 6)
        self.assertEqual(plan["selection"]["selected_ids_sha256"], plan["manifest"]["selection_sha256"])

    def test_full_census_cli_resume_reuses_terminal_entries_and_reconciles_interrupted_attempt(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ligamina-astrbot-full-census-") as temporary:
            root = Path(temporary)
            arguments, output = self._full_census_fixture(root)
            worker_entries: list[str] = []
            interrupted = False

            class FakeWorker:
                def __init__(self, **_kwargs):
                    pass

                def run(self, spec):
                    nonlocal interrupted
                    worker_entries.append(spec.entry_id)
                    if spec.entry_id == "ab-02" and not interrupted:
                        interrupted = True
                        raise KeyboardInterrupt
                    stdout = b'{"status":"passed","reason_code":null}\n'
                    return SimpleNamespace(
                        status="passed", reason_code=None, exit_code=0, elapsed_seconds=0.01,
                        stdout=stdout, stderr=b"", stdout_sha256=hashlib.sha256(stdout).hexdigest(),
                        stderr_sha256=hashlib.sha256(b"").hexdigest(), output_truncated=False,
                        cleanup_confirmed=True, worker_id="fake-worker", peak_memory_bytes=1024,
                    )

            class ReconciliationEvidence:
                def as_evidence(self):
                    return {"cleanup_confirmed": True, "resource_ids": [], "removed_ids": [], "source": "test-exact-label-scan"}

            class FakeReconciler:
                def __init__(self):
                    pass

                def reconcile(self, **_kwargs):
                    return ReconciliationEvidence()

            stdout = StringIO()
            with patch.object(census, "DockerWorker", FakeWorker), patch.object(census, "DockerReconciler", FakeReconciler), \
                    patch.object(census, "FULL_CENSUS_LEASE_SECONDS", 30), redirect_stdout(stdout):
                self.assertEqual(main(arguments), 130)
                first_report = json.loads(output.read_text(encoding="utf-8"))
                self.assertEqual(first_report["status"], "interrupted")
                self.assertEqual(first_report["denominator"]["selected_entries"], 2)
                self.assertEqual(first_report["denominator"]["terminal_entries"], 1)
                with patch.object(census.time, "time", return_value=10**12):
                    self.assertEqual(main([*arguments, "--resume"]), 0)

            report = json.loads(output.read_text(encoding="utf-8"))
            by_id = {row["registry_entry_id"]: row for row in report["results"]}
            attempts = {
                entry_id: [attempt for attempt in report["attempts"] if attempt["unit_id"] == entry_id]
                for entry_id in ("ab-01", "ab-02")
            }
            self.assertEqual(report["status"], "completed")
            self.assertEqual(report["denominator"]["selected_entries"], 2)
            self.assertEqual(report["denominator"]["terminal_entries"], 2)
            self.assertTrue(report["denominator"]["closed_exactly_once"])
            self.assertEqual(set(by_id), {"ab-01", "ab-02"})
            self.assertEqual(worker_entries, ["ab-01", "ab-02", "ab-02"])
            self.assertEqual([attempt["status"] for attempt in attempts["ab-02"]], ["unknown", "passed"])
            changed_image = list(arguments)
            changed_image[changed_image.index("--image") + 1] = "ligamina-astrbot-runtime@sha256:" + "b" * 64
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                self.assertEqual(main([*changed_image, "--resume"]), 2)
            self.assertEqual(worker_entries, ["ab-01", "ab-02", "ab-02"])

    def test_full_census_does_not_retry_an_interrupted_entry_without_cleanup_confirmation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ligamina-astrbot-cleanup-gate-") as temporary:
            root = Path(temporary)
            arguments, output = self._full_census_fixture(root)
            worker_entries: list[str] = []
            interrupted = False

            class FakeWorker:
                def __init__(self, **_kwargs):
                    pass

                def run(self, spec):
                    nonlocal interrupted
                    worker_entries.append(spec.entry_id)
                    if spec.entry_id == "ab-02" and not interrupted:
                        interrupted = True
                        raise KeyboardInterrupt
                    stdout = b'{"status":"passed","reason_code":null}\n'
                    return SimpleNamespace(
                        status="passed", reason_code=None, exit_code=0, elapsed_seconds=0.01,
                        stdout=stdout, stderr=b"", stdout_sha256=hashlib.sha256(stdout).hexdigest(),
                        stderr_sha256=hashlib.sha256(b"").hexdigest(), output_truncated=False,
                        cleanup_confirmed=True, worker_id="fake-worker", peak_memory_bytes=1024,
                    )

            class ReconciliationEvidence:
                def as_evidence(self):
                    return {"cleanup_confirmed": False, "resource_ids": ["still-running"], "removed_ids": [], "source": "test-exact-label-scan"}

            class FakeReconciler:
                def __init__(self):
                    pass

                def reconcile(self, **_kwargs):
                    return ReconciliationEvidence()

            with patch.object(census, "DockerWorker", FakeWorker), patch.object(census, "DockerReconciler", FakeReconciler), \
                    patch.object(census, "FULL_CENSUS_LEASE_SECONDS", 30), redirect_stdout(StringIO()):
                self.assertEqual(main(arguments), 130)
                with patch.object(census.time, "time", return_value=10**12):
                    self.assertEqual(main([*arguments, "--resume"]), 3)

            report = json.loads(output.read_text(encoding="utf-8"))
            interrupted_attempts = [attempt for attempt in report["attempts"] if attempt["unit_id"] == "ab-02"]
            self.assertEqual(report["status"], "blocked_cleanup")
            self.assertEqual(report["queue"]["state"], "interrupted")
            self.assertEqual(worker_entries, ["ab-01", "ab-02"])
            self.assertEqual([attempt["status"] for attempt in interrupted_attempts], ["unknown"])
            self.assertFalse(interrupted_attempts[0]["result"]["reconciliation"]["cleanup_confirmed"])
            self.assertEqual(report["denominator"]["selected_entries"], 2)
            self.assertEqual(report["denominator"]["terminal_entries"], 2)

    def test_full_census_reconciles_failed_plugin_attempt_when_worker_cleanup_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ligamina-astrbot-failed-cleanup-") as temporary:
            root = Path(temporary)
            arguments, output = self._full_census_fixture(root)
            worker_entries: list[str] = []

            class FakeWorker:
                def __init__(self, **_kwargs):
                    pass

                def run(self, spec):
                    worker_entries.append(spec.entry_id)
                    if spec.entry_id == "ab-01" and worker_entries.count("ab-01") == 1:
                        stdout = b'{"status":"failed","reason_code":"plugin_load_failed"}\n'
                        return SimpleNamespace(
                            status="passed", reason_code=None, exit_code=0, elapsed_seconds=0.01,
                            stdout=stdout, stderr=b"", stdout_sha256=hashlib.sha256(stdout).hexdigest(),
                            stderr_sha256=hashlib.sha256(b"").hexdigest(), output_truncated=False,
                            cleanup_confirmed=False, worker_id="fake-worker", peak_memory_bytes=1024,
                        )
                    stdout = b'{"status":"passed","reason_code":null}\n'
                    return SimpleNamespace(
                        status="passed", reason_code=None, exit_code=0, elapsed_seconds=0.01,
                        stdout=stdout, stderr=b"", stdout_sha256=hashlib.sha256(stdout).hexdigest(),
                        stderr_sha256=hashlib.sha256(b"").hexdigest(), output_truncated=False,
                        cleanup_confirmed=True, worker_id="fake-worker", peak_memory_bytes=1024,
                    )

            class ReconciliationEvidence:
                def as_evidence(self):
                    return {"cleanup_confirmed": True, "resource_ids": [], "removed_ids": [], "source": "test-exact-label-scan"}

            class FakeReconciler:
                def __init__(self):
                    pass

                def reconcile(self, **_kwargs):
                    return ReconciliationEvidence()

            with patch.object(census, "DockerWorker", FakeWorker), patch.object(census, "DockerReconciler", FakeReconciler), \
                    patch.object(census, "FULL_CENSUS_LEASE_SECONDS", 30), redirect_stdout(StringIO()):
                self.assertEqual(main(arguments), 3)
                first_report = json.loads(output.read_text(encoding="utf-8"))
                first_attempt = next(attempt for attempt in first_report["attempts"] if attempt["unit_id"] == "ab-01")
                self.assertEqual(first_attempt["status"], "unknown")
                self.assertTrue(first_attempt["result"]["requires_reconciliation"])
                with patch.object(census.time, "time", return_value=10**12):
                    self.assertEqual(main([*arguments, "--resume"]), 0)

            report = json.loads(output.read_text(encoding="utf-8"))
            attempts = [attempt for attempt in report["attempts"] if attempt["unit_id"] == "ab-01"]
            self.assertEqual(report["status"], "completed")
            self.assertEqual([attempt["status"] for attempt in attempts], ["unknown", "passed"])
            self.assertTrue(attempts[0]["result"]["reconciliation"]["cleanup_confirmed"])
            self.assertEqual(worker_entries, ["ab-01", "ab-01", "ab-02"])

    def test_full_census_report_summarizes_plugin_diagnostics_without_persisting_them(self) -> None:
        secret = "synthetic-token-do-not-store"
        summary = census._summarize_worker_side({
            "status": "failed",
            "reason_code": "plugin_load_failed",
            "diagnostic": secret,
            "install": {"status": "installed", "seconds": 1.5, "error_tail": secret},
            "load": {
                "seconds": 2.5,
                "loaded_plugins": [{"name": "fixture"}],
                "failed_plugins": {"fixture": {"error": secret, "traceback_tail": secret}},
                "commands": [{"command_id": "ping"}],
                "startup_sends": 0,
            },
        })

        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["reason_code"], "plugin_load_failed")
        self.assertEqual(summary["install"], {"status": "installed", "seconds": 1.5})
        self.assertEqual(summary["load"], {
            "seconds": 2.5, "loaded_plugins": 1, "failed_plugins": 1, "commands": 1, "startup_sends": 0,
        })
        self.assertNotIn(secret, json.dumps(summary))

    def test_selection_is_deterministic_stratified_and_skips_unfrozen_entries(self) -> None:
        lock = {"entries": [
            _entry("ab-04", "has_requirements"),
            _entry("ab-01"),
            _entry("ab-03", "platform_specific_code", "has_requirements"),
            _entry("ab-02"),
            _entry("ab-05", "llm_dependent"),
            _entry("ab-00", status="download_failed"),
        ]}
        chosen = select_pilot(lock, per_stratum=1)
        self.assertEqual([(stratum, entry["registry_entry_id"]) for stratum, entry in chosen], [
            ("plain", "ab-01"), ("requirements", "ab-04"), ("platform_specific", "ab-03"), ("llm_dependent", "ab-05"),
        ])
        self.assertEqual(chosen, select_pilot(lock, per_stratum=1))

    def test_requirement_names_are_normalized_and_runtime_packages_known(self) -> None:
        self.assertEqual(_requirement_name("Pillow>=9; python_version>'3'"), "pillow")
        self.assertEqual(_requirement_name("jinja2[i18n]==3"), "jinja2")
        self.assertEqual(_requirement_name("Some_Package.Name~=1"), "some-package-name")
        self.assertIn("astrbot", locked_names())
        self.assertIn("aiohttp", locked_names())

    def test_plugin_directory_follows_astrbot_metadata_name_rule(self) -> None:
        self.assertEqual(metadata_dir_name("name: astrbot_plugin_gallery\nversion: 2.1.0\n"), "astrbot_plugin_gallery")
        self.assertEqual(metadata_dir_name('desc: x\nname: "plugin_x"  # comment\n'), "plugin_x")
        self.assertIsNone(metadata_dir_name("  name: nested_key_only\n"))
        self.assertIsNone(metadata_dir_name("name: has-dash\n"))
        self.assertIsNone(metadata_dir_name("name: class\n"))
        self.assertIsNone(metadata_dir_name("version: 1\n"))
        self.assertEqual(metadata_dir_name("\ufeffname: astrbot_plugin_teamup\n"), "astrbot_plugin_teamup")


if __name__ == "__main__":
    unittest.main()
