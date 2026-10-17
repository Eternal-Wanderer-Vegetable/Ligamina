"""Hash-locked, offline Linux Worker dispatch for S2-09 catalog pilots.

The host only downloads and verifies registry tarballs. npm extraction and all
plugin imports happen inside the digest-pinned, network-disabled Docker Worker.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .artifacts import DEFAULT_CACHE_ROOT, sha256_file
from .catalog_run import load_catalog_units
from .workers import DockerWorker, WindowsSandboxWorker, WorkerLimits, WorkerSpec


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SNAPSHOT = ROOT / "catalogs/p2-41649744e057d61031d5"
DEFAULT_REPORT = ROOT / "outputs/acceptance/phase2-s209-koishi-linux-pilot-20261012.json"
RUNTIME_LOCK_PATH = ROOT / "lab/baselines/runtime-images.lock.json"
NPM_ALLOWED_HOST = "registry.npmjs.org"
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_FETCH_WORKERS = 8


class PilotWorkerError(ValueError):
    """The frozen entry, profile lock, or Worker output is inconsistent."""


@dataclass(frozen=True)
class _CatalogContext:
    snapshot_dir: Path
    entries_by_id: dict[str, dict[str, Any]]
    units_by_id: dict[str, dict[str, Any]]
    artifacts_by_id: dict[str, dict[str, Any]]
    profiles_by_id: dict[str, dict[str, Any]]
    artifact_lock_raw: bytes
    profile_lock_raw: bytes


def _read_json(path: Path) -> tuple[Any, bytes]:
    try:
        raw = Path(path).read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PilotWorkerError(f"cannot read JSON {path}: {error}") from error
    return value, raw


def _integrity_values(value: str) -> list[tuple[str, bytes]]:
    result = []
    for item in value.split():
        if "-" not in item:
            raise PilotWorkerError("npm lock integrity is malformed")
        algorithm, encoded = item.split("-", 1)
        if algorithm not in {"sha1", "sha256", "sha384", "sha512"}:
            raise PilotWorkerError(f"unsupported npm integrity algorithm: {algorithm}")
        try:
            digest = base64.b64decode(encoded, validate=True)
        except ValueError as error:
            raise PilotWorkerError("npm lock integrity is not valid base64") from error
        if len(digest) != hashlib.new(algorithm).digest_size:
            raise PilotWorkerError("npm lock integrity has the wrong digest length")
        result.append((algorithm, digest))
    if not result:
        raise PilotWorkerError("npm package has no integrity digest")
    return result


def _package_archive_ref(url: str, integrity: str, *, entry_path: str) -> tuple[str, str, Path]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != NPM_ALLOWED_HOST or parsed.username or parsed.password:
        raise PilotWorkerError(f"npm package URL is outside the allowlist: {url}")
    values = _integrity_values(integrity)
    # Prefer SHA-512 because the frozen profile resolver currently emits it.
    algorithm, digest = max(values, key=lambda item: hashlib.new(item[0]).digest_size)
    digest_hex = digest.hex()
    relative = Path("dependency-archives") / "npm" / algorithm / digest_hex
    return algorithm, digest_hex, relative


def _download_npm_archive(url: str, integrity: str, cache_root: Path, *, entry_path: str) -> tuple[Path, int, bool]:
    algorithm, expected_hex, relative = _package_archive_ref(url, integrity, entry_path=entry_path)
    destination = cache_root / relative
    if destination.is_file():
        actual = hashlib.new(algorithm, destination.read_bytes()).hexdigest()
        if actual != expected_hex:
            raise PilotWorkerError(f"cached npm archive integrity mismatch: {entry_path}")
        return destination, 0, True

    request = urllib.request.Request(url, headers={"User-Agent": "Ligamina-catalog-pilot/1.0", "Accept-Encoding": "identity"})
    last_error: Exception | None = None
    for attempt in range(1, 4):
        temporary: str | None = None
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                final = urllib.parse.urlparse(response.geturl())
                if final.scheme != "https" or final.hostname != NPM_ALLOWED_HOST:
                    raise PilotWorkerError("npm tarball redirected outside the registry allowlist")
                raw = response.read(MAX_ARCHIVE_BYTES + 1)
            if len(raw) > MAX_ARCHIVE_BYTES:
                raise PilotWorkerError(f"npm archive exceeds the S2-09 download limit: {entry_path}")
            actual = hashlib.new(algorithm, raw).hexdigest()
            if actual != expected_hex:
                raise PilotWorkerError(f"npm archive integrity mismatch: {entry_path}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(prefix=".download-", dir=destination.parent)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            return destination, len(raw), False
        except PilotWorkerError:
            raise
        except (OSError, urllib.error.URLError, TimeoutError) as error:
            last_error = error
            if attempt == 3:
                break
            time.sleep(0.25 * (2 ** (attempt - 1)))
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
    raise PilotWorkerError(f"could not download npm archive for {entry_path}: {last_error}")


def _load_catalog_context(snapshot_dir: Path, platform: str) -> _CatalogContext:
    snapshot_dir = Path(snapshot_dir).resolve(strict=True)
    catalog, units = load_catalog_units(snapshot_dir, (platform,))
    artifact_lock, artifact_raw = _read_json(snapshot_dir / "artifacts.lock.json")
    profile_lock, profile_raw = _read_json(snapshot_dir / "profiles.lock.json")
    return _CatalogContext(
        snapshot_dir=snapshot_dir,
        entries_by_id={item["registry_entry_id"]: item for item in catalog["entries"]},
        units_by_id={item["registry_entry_id"]: item for item in units},
        artifacts_by_id={item["registry_entry_id"]: item for item in artifact_lock["entries"]},
        profiles_by_id={item["registry_entry_id"]: item for item in profile_lock["entries"]},
        artifact_lock_raw=artifact_raw,
        profile_lock_raw=profile_raw,
    )


def _entry_inputs(
    snapshot_dir: Path,
    entry_id: str,
    platform: str,
    *,
    context: _CatalogContext | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], bytes, bytes]:
    context = context or _load_catalog_context(snapshot_dir, platform)
    if context.snapshot_dir != Path(snapshot_dir).resolve(strict=True):
        raise PilotWorkerError("catalog context belongs to a different snapshot directory")
    unit = context.units_by_id.get(entry_id)
    entry = context.entries_by_id.get(entry_id)
    if unit is None or entry is None:
        raise PilotWorkerError(f"entry is not in the frozen catalog snapshot: {entry_id}")
    if unit["ecosystem"] != "koishi":
        raise PilotWorkerError("S2-09 generic Node dispatch only accepts Koishi entries")
    if unit["artifact_status"] != "locked" or unit["profile_status"] != "locked":
        raise PilotWorkerError("Koishi pilot entry does not have a locked artifact and profile")

    artifact_entry = context.artifacts_by_id[entry_id]
    profile_entry = context.profiles_by_id[entry_id]
    platform_id = {"linux": "linux-x86_64", "windows": "windows-x86_64"}.get(platform)
    if platform_id is None:
        raise PilotWorkerError(f"unsupported Koishi pilot Worker platform: {platform}")
    profile = profile_entry["profiles"].get(platform_id, {})
    lock_ref = profile.get("dependency_lock")
    if not isinstance(lock_ref, dict) or not isinstance(lock_ref.get("cache_path"), str):
        raise PilotWorkerError("Koishi profile has no dependency lock")
    artifact_record = next((item for item in artifact_entry.get("artifacts", []) if item.get("platform") == "all"), None)
    artifact = (artifact_record or {}).get("artifact")
    if not isinstance(artifact, dict) or not isinstance(artifact.get("cache_path"), str):
        raise PilotWorkerError("Koishi catalog entry has no frozen package tarball")
    return unit, entry, artifact, lock_ref, context.artifact_lock_raw, context.profile_lock_raw


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _load_npm_lock(lock_path: Path, lock_ref: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
    lock, raw = _read_json(lock_path)
    if _sha256(raw) != lock_ref.get("sha256") or len(raw) != lock_ref.get("size_bytes"):
        raise PilotWorkerError("npm profile lock failed its frozen hash/size check")
    packages = lock.get("packages")
    if not isinstance(packages, dict) or not isinstance(packages.get(""), dict):
        raise PilotWorkerError("npm profile lock has no root package record")
    return lock, raw


def _prepare_npm_project(
    lock: dict[str, Any],
    *,
    package_name: str,
    cache_root: Path,
    project_dir: Path,
) -> dict[str, Any]:
    packages = lock["packages"]
    dependency_refs: dict[str, tuple[str, str, Path]] = {}
    for package_path, record in packages.items():
        if not package_path:
            continue
        if record.get("link") is True:
            raise PilotWorkerError(f"linked package is not supported in a frozen catalog lock: {package_path}")
        resolved = record.get("resolved")
        integrity = record.get("integrity")
        if not isinstance(resolved, str) or not isinstance(integrity, str):
            raise PilotWorkerError(f"npm lock package is missing resolved bytes or integrity: {package_path}")
        algorithm, digest, cache_relative = _package_archive_ref(resolved, integrity, entry_path=package_path)
        dependency_refs[package_path] = (algorithm, digest, cache_relative)

    unique = {value[2]: value for value in dependency_refs.values()}
    downloads = {"downloaded_bytes": 0, "downloaded_archives": 0, "cache_hits": 0, "archive_bytes": 0}
    fetched: dict[Path, Path] = {}

    def fetch(value: tuple[str, str, Path]) -> tuple[Path, int, bool]:
        algorithm, digest, relative = value
        # The matching URL/integrity pair is read from the immutable lock below.
        package_path = next(key for key, candidate in dependency_refs.items() if candidate == value)
        record = packages[package_path]
        return _download_npm_archive(record["resolved"], record["integrity"], cache_root, entry_path=package_path)

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_FETCH_WORKERS) as pool:
        future_values = list(zip(unique.keys(), pool.map(fetch, unique.values())))
    for relative, (path, downloaded_bytes, cached) in future_values:
        fetched[relative] = path
        downloads["downloaded_bytes"] += downloaded_bytes
        downloads["downloaded_archives"] += int(not cached)
        downloads["cache_hits"] += int(cached)
        downloads["archive_bytes"] += path.stat().st_size

    project_dir.mkdir(parents=True, exist_ok=True)
    root_record = dict(packages[""])
    package_json = {
        "name": root_record.get("name", "ligamina-catalog-pilot"),
        "version": root_record.get("version", "0.0.0"),
        "private": True,
        "dependencies": root_record.get("dependencies", {}),
    }
    if package_name not in package_json["dependencies"]:
        raise PilotWorkerError("pilot package is not an exact root dependency in the frozen npm lock")
    (project_dir / "package.json").write_text(json.dumps(package_json, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    localized_lock = json.loads(json.dumps(lock))
    localized_packages = localized_lock["packages"]
    tarball_root = project_dir / "tarballs"
    for package_path, (algorithm, digest, relative) in dependency_refs.items():
        archive = fetched[relative]
        target = tarball_root / algorithm / f"{digest}.tgz"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            try:
                os.link(archive, target)
            except OSError:
                shutil.copyfile(archive, target)
        resolved = target.relative_to(project_dir).as_posix()
        localized_packages[package_path]["resolved"] = f"file:{resolved}"
    (project_dir / "package-lock.json").write_text(
        json.dumps(localized_lock, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {
        **downloads,
        "locked_package_count": len(dependency_refs),
        "unique_archive_count": len(unique),
        "package_lock_sha256": _sha256(json.dumps(localized_lock, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"),
    }


def _write_probe(project_dir: Path, *, package_name: str, profile_lock_sha256: str) -> None:
    (project_dir / "ligamina-scratch-sampler.mjs").write_text(
        """import { existsSync } from 'node:fs'
import { readdir, stat, writeFile } from 'node:fs/promises'
import { join } from 'node:path'

const roots = JSON.parse(process.env.LIGAMINA_SCRATCH_ROOTS ?? '[]')
const samplePath = process.env.LIGAMINA_SCRATCH_SAMPLE_PATH
const stopPath = process.env.LIGAMINA_SCRATCH_STOP_PATH
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
async function sizeTree(root) {
  const pending = [root]
  const files = []
  while (pending.length) {
    const directory = pending.pop()
    let entries
    try { entries = await readdir(directory, { withFileTypes: true }) } catch { continue }
    for (const entry of entries) {
      if (entry.isSymbolicLink()) continue
      const path = join(directory, entry.name)
      if (entry.isDirectory()) pending.push(path)
      else files.push(path)
    }
  }
  let total = 0
  for (let index = 0; index < files.length; index += 512) {
    const sizes = await Promise.all(files.slice(index, index + 512).map(async (path) => {
      try { return (await stat(path)).size } catch { return 0 }
    }))
    total += sizes.reduce((sum, size) => sum + size, 0)
  }
  return total
}
let peak = 0
let samples = 0
do {
  let total = 0
  for (const root of roots) total += await sizeTree(root)
  peak = Math.max(peak, total)
  samples += 1
  try { await writeFile(samplePath, JSON.stringify({ peak_writable_scratch_bytes: peak, sample_count: samples })) } catch {}
  await pause(500)
} while (!existsSync(stopPath))
""",
        encoding="utf-8",
        newline="\n",
    )
    (project_dir / "pilot-probe.mjs").write_text(
        """import { spawn, spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { readFile, writeFile } from 'node:fs/promises'
import { join } from 'node:path'

const specPath = process.env.LIGAMINA_SPEC ?? '/input/spec.json'
const project = process.env.LIGAMINA_PROJECT ?? '/work/project'
const npmCommand = process.env.LIGAMINA_NPM_COMMAND ?? 'npm'
const npmPrefixArgs = JSON.parse(process.env.LIGAMINA_NPM_ARGS ?? '[]')
const npmCache = process.env.LIGAMINA_NPM_CACHE
const npmDiagnostics = join(project, 'ligamina-npm-diagnostics.cjs')
await writeFile(npmDiagnostics, "const v8 = require('node:v8'); process.stderr.write('LIGAMINA_NPM_RUNTIME=' + JSON.stringify({ heap_limit_bytes: v8.getHeapStatistics().heap_size_limit }) + String.fromCharCode(10))")
const scratchSampler = spawn(process.execPath, [join(project, 'ligamina-scratch-sampler.mjs')], { stdio: 'ignore', windowsHide: true })
const samplerExit = new Promise((resolve) => { scratchSampler.once('exit', resolve); scratchSampler.once('error', resolve) })
const stopScratchSampler = async () => {
  try { await writeFile(process.env.LIGAMINA_SCRATCH_STOP_PATH, 'stop') } catch {}
  const stopped = await Promise.race([samplerExit.then(() => true), new Promise((resolve) => setTimeout(() => resolve(false), 5000))])
  if (!stopped) scratchSampler.kill('SIGTERM')
  try { return JSON.parse(await readFile(process.env.LIGAMINA_SCRATCH_SAMPLE_PATH, 'utf8')) } catch { return null }
}
const npmArgs = [...npmPrefixArgs, 'ci', '--ignore-scripts', '--offline', '--no-audit', '--no-fund', '--maxsockets=1', '--logs-max=0']
if (npmCache) npmArgs.push('--cache', npmCache)
const spec = JSON.parse(await readFile(specPath, 'utf8'))
const childEnv = { ...process.env, NODE_OPTIONS: `--require=${npmDiagnostics}` }
const npm = spawnSync(npmCommand, npmArgs, {
  cwd: project, encoding: 'utf8', timeout: spec.install_timeout_ms, env: childEnv,
})
const tail = (value) => String(value ?? '').slice(-4096)
const digest = (value) => createHash('sha256').update(value ?? '').digest('hex')
const report = {
  schema_version: 1,
  task_id: 'S2-09',
  registry_entry_id: spec.registry_entry_id,
  package: spec.package,
  version: spec.version,
  profile_id: spec.profile_id,
  profile_lock_sha256: spec.profile_lock_sha256,
  artifact_sha256: spec.artifact_sha256,
  dependency_install: {
    status: npm.status === 0 ? 'passed' : (npm.error?.code === 'ETIMEDOUT' ? 'unknown' : 'failed'),
    exit_code: npm.status,
    reason_code: npm.status === 0 ? null : 'offline_npm_install_failed',
    stdout_bytes: Buffer.byteLength(npm.stdout ?? ''),
    stderr_bytes: Buffer.byteLength(npm.stderr ?? ''),
    stdout_sha256: digest(npm.stdout),
    stderr_sha256: digest(npm.stderr),
    stdout_tail: tail(npm.stdout),
    stderr_tail: tail(npm.stderr),
  },
  native_load: { status: 'skipped', reason_code: 'dependency_install_failed' },
  behavior: { status: 'skipped', reason_code: 'missing_entry_bound_fixture' },
  cleanup: { status: 'passed' },
  environment: { ecosystem: 'koishi', runtime_version: process.version.replace(/^v/, ''), v8_heap_limit_bytes: (await import('node:v8')).getHeapStatistics().heap_size_limit, host_node_options_ignored: true, npm_v8_heap_limit_bytes: null },
}
const npmRuntimeLine = String(npm.stderr ?? '').split(String.fromCharCode(10)).find((line) => line.startsWith('LIGAMINA_NPM_RUNTIME='))
if (npmRuntimeLine) {
  try { report.environment.npm_v8_heap_limit_bytes = JSON.parse(npmRuntimeLine.slice('LIGAMINA_NPM_RUNTIME='.length)).heap_limit_bytes } catch {}
}

if (npm.error || npm.status !== 0) {
  if (npm.error?.code === 'ETIMEDOUT') report.dependency_install.reason_code = 'dependency_install_timeout'
  report.resource_observations = await stopScratchSampler()
  console.log(JSON.stringify(report))
  process.exit(0)
}

let app
const originalProcessExit = process.exit
process.exit = (code = process.exitCode ?? 0) => {
  const error = new Error(`Plugin called process.exit(${String(code)}) during the native-load probe`)
  error.name = 'PluginProcessExitError'
  error.code = 'LIGAMINA_PLUGIN_PROCESS_EXIT'
  error.requestedExitCode = code
  throw error
}
try {
  const { Context, version } = await import('@koishijs/core')
  report.environment.framework_version = version
  const loaded = await import(spec.package)
  const plugin = loaded.apply ?? loaded.default?.apply ?? loaded.default ?? loaded
  if (typeof plugin !== 'function') {
    report.native_load = { status: 'skipped', reason_code: 'entry_loader_profile_unavailable', export_kind: typeof plugin }
  } else {
    app = new Context({ nickname: ['Ligamina Pilot'] })
    app.plugin(plugin, {})
    await app.start()
    report.native_load = { status: 'passed', reason_code: null, entry_module_imported: true, plugin_registered: true }
  }
} catch (error) {
  report.native_load = {
    status: 'failed',
    reason_code: 'native_plugin_load_failed',
    error_name: error?.name ?? 'Error',
    error_tail: String(error?.stack ?? error).slice(-4096),
    ...(error?.code === 'LIGAMINA_PLUGIN_PROCESS_EXIT' ? { requested_exit_code: error.requestedExitCode } : {}),
  }
} finally {
  process.exit = originalProcessExit
  try { await app?.stop() } catch (error) {
    report.cleanup = { status: 'failed', reason_code: 'native_plugin_cleanup_failed', error_tail: String(error?.stack ?? error).slice(-2048) }
  }
}

report.resource_observations = await stopScratchSampler()
console.log(JSON.stringify(report))
""",
        encoding="utf-8",
        newline="\n",
    )


def run_koishi_load_probe(
    snapshot_dir: Path,
    entry_id: str,
    *,
    cache_root: Path = DEFAULT_CACHE_ROOT,
    image_digest: str | None = None,
    timeout_seconds: int = 240,
    _context: _CatalogContext | None = None,
    platform: str = "linux",
) -> dict[str, Any]:
    """Install one exact locked Koishi profile offline and load its package in Docker."""
    snapshot_dir = Path(snapshot_dir).resolve(strict=True)
    cache_root = Path(cache_root).resolve(strict=True)
    unit, catalog_entry, artifact, lock_ref, artifact_lock_raw, profile_lock_raw = _entry_inputs(
        snapshot_dir, entry_id, platform, context=_context,
    )
    artifact_path = cache_root / artifact["cache_path"]
    if not artifact_path.is_file() or sha256_file(artifact_path) != (artifact.get("computed_digests") or {}).get("sha256"):
        raise PilotWorkerError("catalog plugin tarball failed its frozen artifact hash check")
    lock_path = cache_root / lock_ref["cache_path"]
    lock, lock_raw = _load_npm_lock(lock_path, lock_ref)
    image_lock, _ = _read_json(RUNTIME_LOCK_PATH)
    if image_lock.get("task_id") != "S2-05" or image_lock.get("platform") != "linux/amd64":
        raise PilotWorkerError("pinned Linux Worker image lock has the wrong identity")
    selected_image = image_digest or image_lock.get("profile_images", {}).get("koishi")
    if not isinstance(selected_image, str) or "@sha256:" not in selected_image:
        raise PilotWorkerError("Koishi Worker image must be digest pinned")

    entry_doc = unit
    package_name = entry_doc["package"]
    with tempfile.TemporaryDirectory(prefix="ligamina-pilot-koishi-", dir=cache_root) as temporary:
        bundle = Path(temporary)
        project_dir = bundle / "project"
        dependency_stats = _prepare_npm_project(lock, package_name=package_name, cache_root=cache_root, project_dir=project_dir)
        shutil.copyfile(artifact_path, project_dir / "catalog-plugin.tgz")
        artifact_sha = sha256_file(artifact_path)
        artifact_integrity = (artifact.get("computed_digests") or {}).get("sha512")
        direct_path = f"node_modules/{package_name}"
        direct_record = lock.get("packages", {}).get(direct_path, {})
        if not isinstance(artifact_integrity, str) or not any(
            algorithm == "sha512" and digest.hex() == artifact_integrity
            for algorithm, digest in _integrity_values(direct_record.get("integrity", ""))
        ):
            raise PilotWorkerError("catalog plugin bytes do not match the direct package in the frozen profile lock")
        spec = {
            "registry_entry_id": entry_id,
            "package": package_name,
            "version": unit.get("version"),
                "profile_id": entry_doc["profile_id"],
            "profile_lock_sha256": lock_ref["sha256"],
            "artifact_sha256": artifact_sha,
            "install_timeout_ms": min(timeout_seconds * 1000 - 5000, 20 * 60 * 1000),
        }
        (bundle / "spec.json").write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        _write_probe(project_dir, package_name=package_name, profile_lock_sha256=lock_ref["sha256"])
        bundle_bytes = sum(path.stat().st_size for path in bundle.rglob("*") if path.is_file())
        worker = DockerWorker(image_digest=selected_image, repository_root=ROOT)
        command = ("sh", "-c", "set -eu; cp -a /input/project /work/project; cp /input/spec.json /work/project/spec.json; cd /work/project; LIGAMINA_SCRATCH_ROOTS='[\"/work\",\"/tmp\"]' LIGAMINA_SCRATCH_SAMPLE_PATH=/work/project/ligamina-scratch-sample.json LIGAMINA_SCRATCH_STOP_PATH=/work/project/ligamina-scratch-stop node pilot-probe.mjs")
        # The spec is passed at /input because the mounted source stays read-only.
        result = worker.run(
            WorkerSpec(
                run_id="s209-koishi-pilot",
                entry_id=entry_id,
                command=command,
                input_dir=bundle,
                limits=WorkerLimits(timeout_seconds=timeout_seconds, memory_mb=4096, cpu_cores=2, process_limit=256),
            )
        )
        protocol = None
        for line in reversed(result.stdout.decode("utf-8", errors="replace").splitlines()):
            try:
                candidate = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict) and candidate.get("task_id") == "S2-09" and candidate.get("registry_entry_id") == entry_id:
                protocol = candidate
                break
        worker_passed = result.status == "passed" and result.cleanup_confirmed and isinstance(protocol, dict)
        if not worker_passed:
            overall_status = "unknown" if result.status == "unknown" else "failed"
            overall_reason = result.reason_code or "worker_report_invalid"
        else:
            install_status = protocol["dependency_install"]["status"]
            native_status = protocol["native_load"]["status"]
            cleanup_status = protocol["cleanup"]["status"]
            if "failed" in {install_status, native_status, cleanup_status}:
                overall_status = "failed"
            elif "unknown" in {install_status, native_status, cleanup_status}:
                overall_status = "unknown"
            elif native_status == "skipped" or install_status == "skipped":
                overall_status = "skipped"
            else:
                # The unit-level result remains skipped because L4 has no frozen,
                # entry-bound oracle even when its L1/L2 load path succeeds.
                overall_status = "skipped"
            overall_reason = next((
                protocol[key].get("reason_code")
                for key in ("dependency_install", "native_load", "cleanup")
                if protocol.get(key, {}).get("status") in {"failed", "skipped", "unknown"} and protocol[key].get("reason_code")
            ), "missing_entry_bound_fixture" if overall_status == "skipped" and native_status == "passed" else None)
        return {
            "schema_version": 1,
            "task_id": "S2-09",
            "result_id": f"catalog:{entry_id}|{platform}",
            "unit_type": "catalog_entry",
            "registry_entry_id": entry_id,
            "ecosystem": "koishi",
            "package": package_name,
            "version": unit.get("version"),
            "platform": platform,
            "status": overall_status,
            "reason_code": overall_reason,
            "attempted": True,
            "detail": f"Koishi dependency installation and native plugin load ran in the pinned network-disabled {platform} Worker; no entry-bound behavior fixture exists.",
            "profile_id": entry_doc["profile_id"],
            "profile_lock_sha256": lock_ref["sha256"],
            "profile_document_sha256": _sha256(profile_lock_raw),
            "artifact_lock_sha256": _sha256(artifact_lock_raw),
            "npm_lock_sha256": _sha256(lock_raw),
            "artifact_sha256": artifact_sha,
            "plugin_archive_bytes": artifact_path.stat().st_size,
            "dependency_archives": dependency_stats,
            "worker": {
                "status": result.status,
                "reason_code": result.reason_code,
                "exit_code": result.exit_code,
                "elapsed_seconds": result.elapsed_seconds,
                "peak_memory_bytes": result.peak_memory_bytes,
                "cleanup_confirmed": result.cleanup_confirmed,
                "stdout_sha256": result.stdout_sha256,
                "stderr_sha256": result.stderr_sha256,
                "stdout_tail": result.stdout.decode("utf-8", errors="replace")[-4096:],
                "stderr_tail": result.stderr.decode("utf-8", errors="replace")[-4096:],
                "output_truncated": result.output_truncated,
                "input_bytes": bundle_bytes,
                "image_digest": selected_image,
                "network": "none",
                "read_only_input": True,
                "npm_scripts": "disabled",
                "protocol_report": protocol,
            },
            "resource_observations": protocol.get("resource_observations") if isinstance(protocol, dict) else None,
        }


def _tree_sha256(directory: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in Path(directory).rglob("*") if item.is_file()):
        relative = path.relative_to(directory).as_posix().encode("utf-8")
        digest.update(relative + b"\0" + sha256_file(path).encode("ascii") + b"\n")
    return digest.hexdigest()


def _windows_node_runtime() -> tuple[Path, Path, dict[str, Any]]:
    node_path = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "nodejs" / "node.exe"
    npm_root = node_path.parent / "node_modules" / "npm"
    if not node_path.is_file() or not npm_root.is_dir():
        raise PilotWorkerError("a local Node.js 24.20.0 runtime and bundled npm are required for Windows Koishi pilot")
    try:
        node_version = subprocess.run(
            [str(node_path), "--version"], capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, TimeoutError) as error:
        raise PilotWorkerError(f"could not identify the Windows Node runtime: {error}") from error
    if node_version.returncode != 0 or node_version.stdout.strip() != "v24.20.0":
        raise PilotWorkerError(f"Windows Node runtime must be v24.20.0, got {node_version.stdout.strip()}")
    signature_command = (
        "Get-AuthenticodeSignature -LiteralPath '" + str(node_path).replace("'", "''")
        + "' | ForEach-Object { '{0}|{1}' -f $_.Status,$_.SignerCertificate.Subject }"
    )
    pwsh = shutil.which("pwsh")
    if not pwsh:
        raise PilotWorkerError("PowerShell 7 is required to verify the Windows Node Authenticode signature")
    try:
        signature = subprocess.run(
            [pwsh, "-NoProfile", "-NonInteractive", "-Command", signature_command],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, TimeoutError) as error:
        raise PilotWorkerError(f"could not verify the Windows Node Authenticode signature: {error}") from error
    signature_value = signature.stdout.strip().split("|", 1) if signature.returncode == 0 else []
    if len(signature_value) != 2 or signature_value[0] != "Valid" or "OpenJS Foundation" not in signature_value[1]:
        raise PilotWorkerError("Windows Node runtime lacks the expected valid OpenJS Foundation signature")
    return node_path, npm_root, {
        "node_version": node_version.stdout.strip().lstrip("v"),
        "node_sha256": sha256_file(node_path),
        "node_bytes": node_path.stat().st_size,
        "authenticode_status": signature_value[0],
        "authenticode_signer": signature_value[1],
        "npm_version": json.loads((npm_root / "package.json").read_text(encoding="utf-8")).get("version"),
        "npm_tree_sha256": _tree_sha256(npm_root),
    }


def run_koishi_windows_load_probe(
    snapshot_dir: Path,
    entry_id: str,
    *,
    cache_root: Path = DEFAULT_CACHE_ROOT,
    timeout_seconds: int = 300,
    _context: _CatalogContext | None = None,
) -> dict[str, Any]:
    """Install one portable Koishi profile and load its entry in Windows Sandbox."""
    snapshot_dir = Path(snapshot_dir).resolve(strict=True)
    cache_root = Path(cache_root).resolve(strict=True)
    context = _context or _load_catalog_context(snapshot_dir, "windows")
    unit, catalog_entry, artifact, lock_ref, artifact_lock_raw, profile_lock_raw = _entry_inputs(
        snapshot_dir, entry_id, "windows", context=context,
    )
    artifact_path = cache_root / artifact["cache_path"]
    if not artifact_path.is_file() or sha256_file(artifact_path) != (artifact.get("computed_digests") or {}).get("sha256"):
        raise PilotWorkerError("catalog plugin tarball failed its frozen artifact hash check")
    lock_path = cache_root / lock_ref["cache_path"]
    lock, lock_raw = _load_npm_lock(lock_path, lock_ref)
    node_path, npm_root, runtime = _windows_node_runtime()
    package_name = unit["package"]
    direct_record = lock.get("packages", {}).get(f"node_modules/{package_name}", {})
    artifact_sha = sha256_file(artifact_path)
    artifact_integrity = (artifact.get("computed_digests") or {}).get("sha512")
    if not isinstance(artifact_integrity, str) or not any(
        algorithm == "sha512" and digest.hex() == artifact_integrity
        for algorithm, digest in _integrity_values(direct_record.get("integrity", ""))
    ):
        raise PilotWorkerError("catalog plugin bytes do not match the direct package in the frozen profile lock")

    temp_root = cache_root / "s209-windows-worker-temp"
    temp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ligamina-pilot-koishi-windows-", dir=cache_root) as temporary:
        bundle = Path(temporary)
        project_dir = bundle / "project"
        dependency_stats = _prepare_npm_project(lock, package_name=package_name, cache_root=cache_root, project_dir=project_dir)
        shutil.copyfile(artifact_path, project_dir / "catalog-plugin.tgz")
        runtime_dir = bundle / "runtime"
        runtime_dir.mkdir()
        shutil.copy2(node_path, runtime_dir / "node.exe")
        shutil.copytree(npm_root, runtime_dir / "node_modules" / "npm", symlinks=False)
        spec = {
            "registry_entry_id": entry_id,
            "package": package_name,
            "version": unit.get("version"),
            "profile_id": unit["profile_id"],
            "profile_lock_sha256": lock_ref["sha256"],
            "artifact_sha256": artifact_sha,
            "install_timeout_ms": min(timeout_seconds * 1000 - 5000, 20 * 60 * 1000),
        }
        (bundle / "spec.json").write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        _write_probe(project_dir, package_name=package_name, profile_lock_sha256=lock_ref["sha256"])
        (bundle / "run-koishi-pilot.ps1").write_text(
            r"""$ErrorActionPreference = 'Stop'
$inputRoot = 'C:\Ligamina\Input'
$workspaceRoot = 'C:\Ligamina\Workspace'
$project = Join-Path $workspaceRoot 'project'
$runtime = Join-Path $workspaceRoot 'runtime'
Copy-Item -LiteralPath (Join-Path $inputRoot 'project') -Destination $project -Recurse -Force
Copy-Item -LiteralPath (Join-Path $inputRoot 'runtime') -Destination $runtime -Recurse -Force
$env:LIGAMINA_SPEC = Join-Path $inputRoot 'spec.json'
$env:LIGAMINA_PROJECT = $project
$env:LIGAMINA_NPM_COMMAND = Join-Path $runtime 'node.exe'
$npmCache = Join-Path $workspaceRoot 'Temp\npm-cache'
New-Item -ItemType Directory -Path $npmCache -Force | Out-Null
$env:LIGAMINA_NPM_CACHE = $npmCache
$env:LIGAMINA_SCRATCH_ROOTS = ConvertTo-Json -InputObject @($workspaceRoot) -Compress
$env:LIGAMINA_SCRATCH_SAMPLE_PATH = Join-Path $project 'ligamina-scratch-sample.json'
$env:LIGAMINA_SCRATCH_STOP_PATH = Join-Path $project 'ligamina-scratch-stop'
$env:NODE_OPTIONS = $null
$npmCli = Join-Path $runtime 'node_modules\npm\bin\npm-cli.js'
$env:LIGAMINA_NPM_ARGS = ConvertTo-Json -InputObject @($npmCli) -Compress
$env:PATH = "$runtime;$env:PATH"
& $env:LIGAMINA_NPM_COMMAND (Join-Path $project 'pilot-probe.mjs')
exit $LASTEXITCODE
""",
            encoding="utf-8",
            newline="\n",
        )
        bundle_bytes = sum(path.stat().st_size for path in bundle.rglob("*") if path.is_file())
        worker = WindowsSandboxWorker(repository_root=ROOT, temporary_root=temp_root)
        command = (
            r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
            "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", r"C:\Ligamina\Input\run-koishi-pilot.ps1",
        )
        result = worker.run(
            WorkerSpec(
                run_id="s209-koishi-windows-pilot",
                entry_id=entry_id,
                command=command,
                input_dir=bundle,
                limits=WorkerLimits(timeout_seconds=timeout_seconds, memory_mb=8192, cpu_cores=2, process_limit=256),
            )
        )
        protocol = None
        for line in reversed(result.stdout.decode("utf-8", errors="replace").splitlines()):
            try:
                candidate = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict) and candidate.get("task_id") == "S2-09" and candidate.get("registry_entry_id") == entry_id:
                protocol = candidate
                break
        worker_passed = result.status == "passed" and result.cleanup_confirmed and isinstance(protocol, dict)
        if not worker_passed:
            overall_status = "unknown" if result.status == "unknown" else "failed"
            overall_reason = result.reason_code or "worker_report_invalid"
        else:
            stage_statuses = [protocol[key].get("status") for key in ("dependency_install", "native_load", "cleanup")]
            if "failed" in stage_statuses:
                overall_status = "failed"
            elif "unknown" in stage_statuses:
                overall_status = "unknown"
            elif "skipped" in stage_statuses:
                overall_status = "skipped"
            else:
                overall_status = "skipped"
            overall_reason = next((
                protocol[key].get("reason_code") for key in ("dependency_install", "native_load", "cleanup")
                if protocol.get(key, {}).get("status") in {"failed", "skipped", "unknown"} and protocol[key].get("reason_code")
            ), "missing_entry_bound_fixture" if overall_status == "skipped" else None)
        return {
            "schema_version": 1,
            "task_id": "S2-09",
            "result_id": f"catalog:{entry_id}|windows",
            "unit_type": "catalog_entry",
            "registry_entry_id": entry_id,
            "ecosystem": "koishi",
            "package": package_name,
            "version": unit.get("version"),
            "platform": "windows",
            "status": overall_status,
            "reason_code": overall_reason,
            "attempted": True,
            "detail": "Koishi dependency installation and native plugin load ran in Windows Sandbox with Networking=Disabled and npm lifecycle scripts disabled; no entry-bound behavior fixture exists.",
            "profile_id": unit["profile_id"],
            "profile_lock_sha256": lock_ref["sha256"],
            "profile_document_sha256": _sha256(profile_lock_raw),
            "artifact_lock_sha256": _sha256(artifact_lock_raw),
            "npm_lock_sha256": _sha256(lock_raw),
            "artifact_sha256": artifact_sha,
            "plugin_archive_bytes": artifact_path.stat().st_size,
            "dependency_archives": dependency_stats,
            "windows_node_runtime": runtime,
            "worker": {
                "status": result.status,
                "reason_code": result.reason_code,
                "exit_code": result.exit_code,
                "elapsed_seconds": result.elapsed_seconds,
                "peak_memory_bytes": result.peak_memory_bytes,
                "memory_limit_mb": 8192,
                "cleanup_confirmed": result.cleanup_confirmed,
                "stdout_sha256": result.stdout_sha256,
                "stderr_sha256": result.stderr_sha256,
                "stdout_tail": result.stdout.decode("utf-8", errors="replace")[-4096:],
                "stderr_tail": result.stderr.decode("utf-8", errors="replace")[-4096:],
                "output_truncated": result.output_truncated,
                "input_bytes": bundle_bytes,
                "runtime_binary_sha256": runtime["node_sha256"],
                "network": "disabled by Windows Sandbox policy",
                "read_only_input": True,
                "npm_scripts": "disabled",
                "protocol_report": protocol,
            },
            "resource_observations": protocol.get("resource_observations") if isinstance(protocol, dict) else None,
        }


def run_selected_koishi_pilot(
    snapshot_dir: Path,
    manifest_path: Path,
    *,
    cache_root: Path = DEFAULT_CACHE_ROOT,
    timeout_seconds: int = 240,
    platform: str = "linux",
    checkpoint_path: Path | None = None,
    resume_from: Path | None = None,
) -> dict[str, Any]:
    """Attempt each selected, installable Koishi entry for one platform exactly once."""
    if platform not in {"linux", "windows"}:
        raise PilotWorkerError(f"unsupported Koishi pilot platform: {platform}")
    manifest, manifest_raw = _read_json(manifest_path)
    if not isinstance(manifest, dict) or manifest.get("kind") != "phase2_stratified_pilot_manifest":
        raise PilotWorkerError("S2-09 pilot manifest has the wrong identity")
    manifest_sha = manifest.get("manifest_sha256")
    payload = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if _sha256(canonical) != manifest_sha:
        raise PilotWorkerError("S2-09 pilot manifest failed its canonical hash check")
    ledger = manifest.get("catalog_assignment_ledger")
    if not isinstance(ledger, list):
        raise PilotWorkerError("S2-09 pilot manifest has no assignment ledger")
    selected = [row for row in ledger if row.get("selected") is True and row.get("ecosystem") == "koishi" and row.get("platform") == platform]
    ids = [row.get("registry_entry_id") for row in selected]
    if len(ids) != len(set(ids)) or len(selected) < int(manifest.get("minimum_per_ecosystem", 30)):
        raise PilotWorkerError(f"Koishi {platform} pilot cohort does not meet its unique minimum denominator")

    context = _load_catalog_context(snapshot_dir, platform)
    units = context.units_by_id
    runtime_details = _windows_node_runtime()[2] if platform == "windows" else "Node.js runtime pinned by the Docker image and profile lock"
    expected_ids = {f"catalog:{entry_id}|{platform}" for entry_id in ids}
    results: list[dict[str, Any]] = []
    resume_sha256 = None
    if resume_from is not None:
        prior, prior_raw = _read_json(Path(resume_from))
        expected_kind = f"phase2_s209_koishi_{platform}_pilot_workers"
        if not isinstance(prior, dict) or prior.get("task_id") != "S2-09" or prior.get("kind") != expected_kind:
            raise PilotWorkerError("Koishi pilot resume checkpoint has the wrong identity")
        if prior.get("pilot_manifest_sha256") != manifest_sha or prior.get(f"selected_koishi_{platform}_entries") != len(selected):
            raise PilotWorkerError("Koishi pilot resume checkpoint does not match this manifest and platform cohort")
        prior_results = prior.get("results")
        if not isinstance(prior_results, list) or any(not isinstance(item, dict) for item in prior_results):
            raise PilotWorkerError("Koishi pilot resume checkpoint has no result list")
        prior_ids = [item.get("result_id") for item in prior_results]
        if len(prior_ids) != len(set(prior_ids)) or not set(prior_ids).issubset(expected_ids):
            raise PilotWorkerError("Koishi pilot resume checkpoint has duplicate or unexpected result identities")
        if any(item.get("status") not in {"passed", "failed", "skipped", "unknown"} for item in prior_results):
            raise PilotWorkerError("Koishi pilot resume checkpoint contains a non-terminal result")
        if any(item.get("platform") != platform or item.get("ecosystem") != "koishi" for item in prior_results):
            raise PilotWorkerError("Koishi pilot resume checkpoint contains a result for a different platform or ecosystem")
        results.extend(prior_results)
        resume_sha256 = _sha256(prior_raw)
    completed_ids = {item["result_id"] for item in results}

    def make_report(*, final: bool) -> dict[str, Any]:
        result_ids = [item["result_id"] for item in results]
        counts = {status: sum(item.get("status") == status for item in results) for status in ("passed", "failed", "skipped", "unknown")}
        closed = len(results) == len(selected) and set(result_ids) == expected_ids and len(set(result_ids)) == len(selected)
        report = {
            "schema_version": 1,
            "date": "2026-10-12",
            "task_id": "S2-09",
            "kind": f"phase2_s209_koishi_{platform}_pilot_workers",
            "status": ("partial" if counts["unknown"] or counts["failed"] or counts["skipped"] else "completed") if final else "in-progress",
            "snapshot_id": manifest.get("snapshot_id"),
            "pilot_manifest_sha256": manifest_sha,
            "pilot_manifest_raw_sha256": _sha256(manifest_raw),
            f"selected_koishi_{platform}_entries": len(selected),
            "terminal_results": len(results),
            "closed_exactly_once": closed,
            "counts": counts,
            "results": results,
            "resume_checkpoint_sha256": resume_sha256,
            "execution_scope": {
                "worker_os": "Linux Docker" if platform == "linux" else "Windows Sandbox",
                "network": "none" if platform == "linux" else "disabled by Windows Sandbox policy",
                "plugin_install_scripts": "disabled",
                "selected_entry_behavior_fixtures": "missing; L4 remains skipped",
                "other_platform": "not run by this platform-specific entrypoint",
                "nonebot": "not run by this Koishi-only entrypoint",
                "runtime": runtime_details,
            },
        }
        if final and not closed:
            raise PilotWorkerError(f"Koishi {platform} pilot results do not close the selected denominator exactly once")
        return report

    def save_checkpoint() -> None:
        if checkpoint_path is None:
            return
        path = Path(checkpoint_path).expanduser().absolute()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(make_report(final=False), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)

    needs_profile = {"adapter", "storage", "services", "scheduler", "console"}
    save_checkpoint()
    for row in sorted(selected, key=lambda item: item["registry_entry_id"]):
        entry_id = row["registry_entry_id"]
        result_id = f"catalog:{entry_id}|{platform}"
        if result_id in completed_ids:
            continue
        unit = units[entry_id]
        source_record = context.entries_by_id[entry_id].get("source_record", {})
        manifest_record = source_record.get("manifest", {}) if isinstance(source_record, dict) else {}
        service_record = manifest_record.get("service", {}) if isinstance(manifest_record, dict) else {}
        required_services = service_record.get("required", []) if isinstance(service_record, dict) else []
        if not isinstance(required_services, list):
            required_services = []
        if row.get("registry_quality") == "known_failure":
            result = {
                "result_id": f"catalog:{entry_id}|{platform}", "registry_entry_id": entry_id, "ecosystem": "koishi",
                "package": row.get("package"), "version": row.get("version"), "platform": platform, "status": "skipped",
                "reason_code": "catalog_entry_invalid", "attempted": False, "stage": "prepare",
            }
        elif row.get("artifact_status") != "locked" or row.get("profile_status") != "locked":
            result = {
                "result_id": f"catalog:{entry_id}|{platform}", "registry_entry_id": entry_id, "ecosystem": "koishi",
                "package": row.get("package"), "version": row.get("version"), "platform": platform, "status": "skipped",
                "reason_code": row.get("profile_reason_code") or row.get("artifact_reason_code") or "profile_or_artifact_unavailable",
                "attempted": False, "stage": "prepare",
            }
        elif required_services or needs_profile.intersection(unit.get("recipe_categories", [])):
            result = {
                "result_id": f"catalog:{entry_id}|{platform}", "registry_entry_id": entry_id, "ecosystem": "koishi",
                "package": row.get("package"), "version": row.get("version"), "platform": platform, "status": "skipped",
                "reason_code": "entry_loader_profile_missing", "attempted": False, "stage": "prepare",
                "detail": "The entry requires a service/adapter/scheduler/console loader profile that is not frozen in S2-09.",
                "required_services": required_services,
                "recipe_categories": unit.get("recipe_categories", []),
            }
        else:
            try:
                probe = run_koishi_load_probe if platform == "linux" else run_koishi_windows_load_probe
                result = probe(snapshot_dir, entry_id, cache_root=cache_root, timeout_seconds=timeout_seconds, _context=context)
            except (PilotWorkerError, OSError, RuntimeError, StopIteration, ValueError) as error:
                result = {
                    "schema_version": 1, "task_id": "S2-09", "result_id": result_id,
                    "registry_entry_id": entry_id, "ecosystem": "koishi", "package": row.get("package"),
                    "version": row.get("version"), "platform": platform, "status": "unknown",
                    "reason_code": "worker_preparation_failed", "attempted": False, "detail": str(error),
                }
        results.append(result)
        completed_ids.add(result_id)
        save_checkpoint()

    result_ids = [item["result_id"] for item in results]
    if len(results) != len(selected) or len(set(result_ids)) != len(selected):
        raise PilotWorkerError(f"Koishi {platform} pilot results do not close the selected denominator exactly once")
    return make_report(final=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--entry-id")
    parser.add_argument("--platform", choices=("linux", "windows"), default="linux")
    parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--checkpoint-report", type=Path)
    parser.add_argument("--resume-from", type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=240)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    options = parse_args(argv)
    if options.report is not None:
        report_path = Path(options.report).expanduser().absolute()
    elif options.manifest:
        report_path = ROOT / f"outputs/acceptance/phase2-s209-koishi-{options.platform}-pilot-20261012.json"
    else:
        report_path = ROOT / f"outputs/acceptance/phase2-s209-koishi-{options.platform}-{options.entry_id}.json"
    try:
        if bool(options.manifest) == bool(options.entry_id):
            raise PilotWorkerError("provide exactly one of --manifest (selected cohort) or --entry-id")
        if options.manifest:
            result = run_selected_koishi_pilot(
                options.snapshot, options.manifest, cache_root=options.cache_root,
                timeout_seconds=options.timeout_seconds, platform=options.platform,
                checkpoint_path=options.checkpoint_report or report_path.with_name(report_path.stem + ".checkpoint.json"),
                resume_from=options.resume_from,
            )
        else:
            probe = run_koishi_load_probe if options.platform == "linux" else run_koishi_windows_load_probe
            result = probe(
                options.snapshot,
                options.entry_id,
                cache_root=options.cache_root,
                timeout_seconds=options.timeout_seconds,
            )
        report_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = report_path.with_suffix(report_path.suffix + ".tmp")
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(report_path)
    except (PilotWorkerError, OSError, StopIteration, ValueError) as error:
        print(f"pilot worker failed: {error}")
        return 2
    print(json.dumps({"task_id": "S2-09", "status": result["status"], "counts": result.get("counts"), "result_id": result.get("result_id"), "reason_code": result.get("reason_code"), "report": str(report_path)}))
    return 0 if result["status"] in {"passed", "failed", "skipped", "partial", "completed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
