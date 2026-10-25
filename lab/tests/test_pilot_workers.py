from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lab.pilot_workers import (
    _CatalogContext,
    PilotWorkerError,
    _integrity_values,
    _package_archive_ref,
    _prepare_npm_project,
    _write_probe,
    run_selected_koishi_pilot,
)


class KoishiPilotWorkerPreparationTests(unittest.TestCase):
    def test_integrity_parser_accepts_locked_sha512_and_rejects_bad_data(self) -> None:
        raw = b"locked npm archive"
        integrity = "sha512-" + base64.b64encode(hashlib.sha512(raw).digest()).decode("ascii")
        parsed = _integrity_values(integrity)
        self.assertEqual(parsed, [("sha512", hashlib.sha512(raw).digest())])
        with self.assertRaises(PilotWorkerError):
            _integrity_values("sha512-not-base64!")

    def test_npm_archive_url_must_stay_on_registry_host(self) -> None:
        digest = "sha512-" + base64.b64encode(hashlib.sha512(b"x").digest()).decode("ascii")
        algorithm, hex_digest, relative = _package_archive_ref(
            "https://registry.npmjs.org/example/-/example-1.0.0.tgz", digest, entry_path="node_modules/example"
        )
        self.assertEqual(algorithm, "sha512")
        self.assertEqual(hex_digest, hashlib.sha512(b"x").hexdigest())
        self.assertEqual(relative.as_posix(), f"dependency-archives/npm/sha512/{hex_digest}")
        with self.assertRaisesRegex(PilotWorkerError, "outside the allowlist"):
            _package_archive_ref("https://evil.example/example.tgz", digest, entry_path="node_modules/example")

    def test_profile_lock_is_localized_to_verified_file_tarballs(self) -> None:
        raw = b"locked package tarball"
        integrity = "sha512-" + base64.b64encode(hashlib.sha512(raw).digest()).decode("ascii")
        digest = hashlib.sha512(raw).hexdigest()
        relative = Path("dependency-archives/npm/sha512") / digest
        lock = {
            "lockfileVersion": 3,
            "packages": {
                "": {"name": "pilot", "version": "0.0.0", "dependencies": {"example": "1.0.0"}},
                "node_modules/example": {
                    "version": "1.0.0",
                    "resolved": "https://registry.npmjs.org/example/-/example-1.0.0.tgz",
                    "integrity": integrity,
                },
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "cache"
            project = Path(temporary) / "project"
            archive = cache / relative
            archive.parent.mkdir(parents=True)
            archive.write_bytes(raw)
            with patch("lab.pilot_workers._download_npm_archive", return_value=(archive, 0, True)):
                stats = _prepare_npm_project(lock, package_name="example", cache_root=cache, project_dir=project)

            localized = json.loads((project / "package-lock.json").read_text(encoding="utf-8"))
            record = localized["packages"]["node_modules/example"]
            self.assertEqual(record["resolved"], f"file:tarballs/sha512/{digest}.tgz")
            self.assertEqual(record["integrity"], integrity)
            self.assertEqual((project / "tarballs/sha512" / f"{digest}.tgz").read_bytes(), raw)
            self.assertEqual(stats["unique_archive_count"], 1)
            self.assertEqual(stats["archive_bytes"], len(raw))
            self.assertTrue(lock["packages"]["node_modules/example"]["resolved"].startswith("https://"))

    def test_probe_installs_offline_with_scripts_disabled_before_import(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            _write_probe(project, package_name="example", profile_lock_sha256="a" * 64)
            source = (project / "pilot-probe.mjs").read_text(encoding="utf-8")
            sampler_source = (project / "ligamina-scratch-sampler.mjs").read_text(encoding="utf-8")
        self.assertIn("'ci', '--ignore-scripts', '--offline'", source)
        self.assertIn("env: childEnv", source)
        self.assertIn("NODE_OPTIONS: `--require=${npmDiagnostics}`", source)
        self.assertIn("v8_heap_limit_bytes", source)
        self.assertIn("await import('@koishijs/core')", source)
        self.assertIn("loaded.inject ?? defaultExport?.inject ?? plugin.inject", source)
        self.assertIn("required_host_service_unavailable", source)
        self.assertLess(source.index("const npm = spawnSync"), source.index("await import('@koishijs/core')"))
        self.assertIn("missing_entry_bound_fixture", source)
        self.assertIn("ligamina-scratch-sampler.mjs", source)
        self.assertIn("peak_writable_scratch_bytes", sampler_source)

    def test_probe_reports_plugin_process_exit_as_native_load_failure(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is required to execute the generated Koishi probe")
        with tempfile.TemporaryDirectory(prefix="ligamina-process-exit-probe-") as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            _write_probe(project, package_name="process-exit-plugin", profile_lock_sha256="a" * 64)
            (project / "spec.json").write_text(json.dumps({
                "registry_entry_id": "ko-process-exit-fixture", "package": "process-exit-plugin",
                "version": "1.0.0", "profile_id": "test-profile", "profile_lock_sha256": "a" * 64,
                "artifact_sha256": "b" * 64, "install_timeout_ms": 30_000,
            }), encoding="utf-8")
            fake_npm = root / "fake-npm.mjs"
            fake_npm.write_text("process.stdout.write('offline install fixture passed')\n", encoding="utf-8")

            core = project / "node_modules" / "@koishijs" / "core"
            core.mkdir(parents=True)
            (core / "package.json").write_text(json.dumps({
                "name": "@koishijs/core", "type": "module", "exports": "./index.js",
            }), encoding="utf-8")
            (core / "index.js").write_text(
                "export const version = '4.18.11';\n"
                "export class Context { plugin(fn) { fn(this, {}) } async start() {} async stop() {} }\n",
                encoding="utf-8",
            )
            plugin = project / "node_modules" / "process-exit-plugin"
            plugin.mkdir(parents=True)
            (plugin / "package.json").write_text(json.dumps({
                "name": "process-exit-plugin", "version": "1.0.0", "main": "index.cjs",
            }), encoding="utf-8")
            (plugin / "index.cjs").write_text("process.exit(0)\nmodule.exports = function plugin() {}\n", encoding="utf-8")

            env = os.environ.copy()
            env.update({
                "LIGAMINA_SPEC": str(project / "spec.json"),
                "LIGAMINA_PROJECT": str(project),
                "LIGAMINA_NPM_COMMAND": node,
                "LIGAMINA_NPM_ARGS": json.dumps([str(fake_npm)]),
                "LIGAMINA_SCRATCH_ROOTS": json.dumps([str(project / "node_modules")]),
                "LIGAMINA_SCRATCH_SAMPLE_PATH": str(project / "ligamina-scratch-sample.json"),
                "LIGAMINA_SCRATCH_STOP_PATH": str(project / "ligamina-scratch-stop"),
            })
            completed = subprocess.run(
                [node, str(project / "pilot-probe.mjs")], capture_output=True, text=True, timeout=20, env=env,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        self.assertTrue(lines, "probe must emit its structured report")
        report = json.loads(lines[-1])
        self.assertEqual(report["dependency_install"]["status"], "passed")
        self.assertEqual(report["native_load"]["status"], "failed")
        self.assertEqual(report["native_load"]["reason_code"], "native_plugin_load_failed")
        self.assertEqual(report["native_load"]["error_name"], "PluginProcessExitError")
        self.assertEqual(report["native_load"]["requested_exit_code"], 0)

    def test_probe_exits_after_report_when_plugin_leaves_an_open_handle(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is required to execute the generated Koishi probe")
        with tempfile.TemporaryDirectory(prefix="ligamina-open-handle-probe-") as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            _write_probe(project, package_name="open-handle-plugin", profile_lock_sha256="a" * 64)
            (project / "spec.json").write_text(json.dumps({
                "registry_entry_id": "ko-open-handle-fixture", "package": "open-handle-plugin",
                "version": "1.0.0", "profile_id": "test-profile", "profile_lock_sha256": "a" * 64,
                "artifact_sha256": "b" * 64, "install_timeout_ms": 30_000,
            }), encoding="utf-8")
            fake_npm = root / "fake-npm.mjs"
            fake_npm.write_text("process.stdout.write('offline install fixture passed')\n", encoding="utf-8")

            core = project / "node_modules" / "@koishijs" / "core"
            core.mkdir(parents=True)
            (core / "package.json").write_text(json.dumps({
                "name": "@koishijs/core", "type": "module", "exports": "./index.js",
            }), encoding="utf-8")
            (core / "index.js").write_text(
                "export const version = '4.18.11';\n"
                "export class Context { plugin(fn) { fn(this, {}) } async start() {} async stop() {} }\n",
                encoding="utf-8",
            )
            plugin = project / "node_modules" / "open-handle-plugin"
            plugin.mkdir(parents=True)
            (plugin / "package.json").write_text(json.dumps({
                "name": "open-handle-plugin", "version": "1.0.0", "main": "index.cjs",
            }), encoding="utf-8")
            (plugin / "index.cjs").write_text(
                "module.exports = { apply: function plugin() { setInterval(() => {}, 1000) } }\n",
                encoding="utf-8",
            )

            env = os.environ.copy()
            env.update({
                "LIGAMINA_SPEC": str(project / "spec.json"),
                "LIGAMINA_PROJECT": str(project),
                "LIGAMINA_NPM_COMMAND": node,
                "LIGAMINA_NPM_ARGS": json.dumps([str(fake_npm)]),
                "LIGAMINA_SCRATCH_ROOTS": json.dumps([str(project / "node_modules")]),
                "LIGAMINA_SCRATCH_SAMPLE_PATH": str(project / "ligamina-scratch-sample.json"),
                "LIGAMINA_SCRATCH_STOP_PATH": str(project / "ligamina-scratch-stop"),
            })
            completed = subprocess.run(
                [node, str(project / "pilot-probe.mjs")], capture_output=True, text=True, timeout=4, env=env,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads([line for line in completed.stdout.splitlines() if line.strip()][-1])
        self.assertEqual(report["dependency_install"]["status"], "passed")
        self.assertEqual(report["native_load"]["status"], "passed", report["native_load"])
        self.assertEqual(report["cleanup"]["status"], "passed")

    def test_v3_probe_preserves_commonjs_inject_and_skips_missing_services(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is required to execute the generated Koishi probe")
        with tempfile.TemporaryDirectory(prefix="ligamina-koishi-inject-probe-") as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            _write_probe(project, package_name="inject-fixture", profile_lock_sha256="a" * 64)
            (project / "spec.json").write_text(json.dumps({
                "registry_entry_id": "ko-inject-fixture", "package": "inject-fixture",
                "version": "1.0.0", "profile_id": "test-profile", "profile_lock_sha256": "a" * 64,
                "artifact_sha256": "b" * 64, "install_timeout_ms": 30_000, "probe_semantics_version": 3,
            }), encoding="utf-8")
            fake_npm = root / "fake-npm.mjs"
            fake_npm.write_text("process.stdout.write('offline install fixture passed')\n", encoding="utf-8")

            core = project / "node_modules" / "@koishijs" / "core"
            core.mkdir(parents=True)
            (core / "package.json").write_text(json.dumps({
                "name": "@koishijs/core", "type": "module", "exports": "./index.js",
            }), encoding="utf-8")
            (core / "index.js").write_text(
                "export const version = '4.18.11';\n"
                "export class Context { get(name) { return name === 'database' ? {} : undefined } "
                "plugin(fn) { fn(this, {}) } async start() {} async stop() {} }\n",
                encoding="utf-8",
            )
            plugin = project / "node_modules" / "inject-fixture"
            plugin.mkdir(parents=True)
            (plugin / "package.json").write_text(json.dumps({
                "name": "inject-fixture", "version": "1.0.0", "main": "index.cjs",
            }), encoding="utf-8")
            (plugin / "index.cjs").write_text(
                "exports.inject = { required: ['database', 'cron'] };\n"
                "exports.apply = function apply() { require('node:fs').writeFileSync('plugin-invoked', 'yes') };\n",
                encoding="utf-8",
            )

            env = os.environ.copy()
            env.update({
                "LIGAMINA_SPEC": str(project / "spec.json"),
                "LIGAMINA_PROJECT": str(project),
                "LIGAMINA_NPM_COMMAND": node,
                "LIGAMINA_NPM_ARGS": json.dumps([str(fake_npm)]),
                "LIGAMINA_SCRATCH_ROOTS": json.dumps([str(project / "node_modules")]),
                "LIGAMINA_SCRATCH_SAMPLE_PATH": str(project / "ligamina-scratch-sample.json"),
                "LIGAMINA_SCRATCH_STOP_PATH": str(project / "ligamina-scratch-stop"),
            })
            completed = subprocess.run(
                [node, str(project / "pilot-probe.mjs")], capture_output=True, text=True, timeout=20, env=env,
                cwd=project,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        self.assertTrue(lines, "probe must emit its structured report")
        report = json.loads(lines[-1])
        self.assertEqual(report["probe_semantics_version"], 3)
        self.assertEqual(report["native_load"]["status"], "skipped")
        self.assertEqual(report["native_load"]["reason_code"], "required_host_service_unavailable")
        self.assertEqual(report["native_load"]["required_services"], ["database", "cron"])
        self.assertEqual(report["native_load"]["missing_services"], ["cron"])
        self.assertFalse(report["native_load"]["plugin_registered"])
        self.assertFalse((project / "plugin-invoked").exists(), "plugin callback must not run with missing required services")

    def test_selected_windows_batch_routes_to_windows_sandbox_probe(self) -> None:
        entry_id = "ko-test-windows"
        row = {
            "registry_entry_id": entry_id,
            "ecosystem": "koishi",
            "platform": "windows",
            "selected": True,
            "registry_quality": "valid",
            "artifact_status": "locked",
            "profile_status": "locked",
            "recipe_categories": ["message"],
        }
        manifest = {
            "kind": "phase2_stratified_pilot_manifest",
            "minimum_per_ecosystem": 1,
            "snapshot_id": "snapshot-test",
            "catalog_assignment_ledger": [row],
        }
        canonical = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        manifest["manifest_sha256"] = hashlib.sha256(canonical).hexdigest()
        context = _CatalogContext(
            snapshot_dir=Path("."),
            entries_by_id={entry_id: {"source_record": {"manifest": {}}}},
            units_by_id={entry_id: {"package": "koishi-plugin-test", "version": "1.0.0", "profile_id": "test", "recipe_categories": ["message"]}},
            artifacts_by_id={},
            profiles_by_id={},
            artifact_lock_raw=b"{}",
            profile_lock_raw=b"{}",
        )
        expected = {
            "result_id": f"catalog:{entry_id}|windows",
            "registry_entry_id": entry_id,
            "ecosystem": "koishi",
            "package": "koishi-plugin-test",
            "version": "1.0.0",
            "platform": "windows",
            "status": "skipped",
            "attempted": True,
        }
        with tempfile.TemporaryDirectory() as temporary:
            manifest_path = Path(temporary) / "manifest.json"
            checkpoint_path = Path(temporary) / "windows.checkpoint.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with patch("lab.pilot_workers._load_catalog_context", return_value=context), patch(
                "lab.pilot_workers.run_koishi_windows_load_probe", return_value=expected
            ) as windows_probe, patch(
                "lab.pilot_workers._windows_node_runtime",
                return_value=(Path("node.exe"), Path("npm"), {"node_version": "24.20.0"}),
            ):
                report = run_selected_koishi_pilot(
                    Path(temporary), manifest_path, cache_root=Path(temporary), timeout_seconds=30,
                    platform="windows", checkpoint_path=checkpoint_path,
                )
                windows_probe.assert_called_once()
                resumed = run_selected_koishi_pilot(
                    Path(temporary), manifest_path, cache_root=Path(temporary), timeout_seconds=30,
                    platform="windows", checkpoint_path=checkpoint_path, resume_from=checkpoint_path,
                )
                checkpoint_report = json.loads(checkpoint_path.read_text(encoding="utf-8"))

        self.assertEqual(report["kind"], "phase2_s209_koishi_windows_pilot_workers")
        self.assertEqual(report["selected_koishi_windows_entries"], 1)
        self.assertEqual(report["results"], [expected])
        self.assertEqual(resumed["results"], [expected])
        self.assertEqual(checkpoint_report["terminal_results"], 1)
        windows_probe.assert_called_once_with(
            Path(temporary), entry_id, cache_root=Path(temporary), timeout_seconds=30,
            memory_mb=16384, _context=context,
        )


if __name__ == "__main__":
    unittest.main()
