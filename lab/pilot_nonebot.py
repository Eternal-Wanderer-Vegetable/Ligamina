"""Run hash-locked NoneBot L1/L2 catalog pilot entries in Linux Docker Workers."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import zipfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import InvalidVersion, Version

from .artifacts import DEFAULT_CACHE_ROOT, _compatible_python_file, _requires_python_allows, sha256_file
from .baselines.run_s205 import RUNTIME_LOCK_PATH
from .catalog_run import load_catalog_units
from .pilot_workers import PilotWorkerError, _read_json, _sha256
from .workers import DockerWorker, WindowsSandboxWorker, WorkerLimits, WorkerSpec
from .profiles import WINDOWS_PIP_BOOTSTRAP, _load_windows_runtime_admission, _python_profile_id


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SNAPSHOT = ROOT / "catalogs/p2-41649744e057d61031d5"
DEFAULT_MANIFEST = ROOT / "outputs/acceptance/phase2-s209-pilot-manifest-20261012.json"
DEFAULT_REPORT = ROOT / "outputs/acceptance/phase2-s209-nonebot-linux-pilot-20261012.json"
_HASH_RE = re.compile(r"--hash=sha256:([0-9a-f]{64})\b")
_MAX_FETCH_WORKERS = 4
_MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
_MAX_METADATA_BYTES = 32 * 1024 * 1024
_MAX_WINDOWS_RUNTIME_ARCHIVE_BYTES = 64 * 1024 * 1024
_MAX_WINDOWS_RUNTIME_EXPANDED_BYTES = 256 * 1024 * 1024


class NoneBotPilotError(RuntimeError):
    """Raised when frozen NoneBot pilot inputs cannot be trusted or executed."""


def _reclassify_windows_app_control_load(protocol: dict[str, Any]) -> dict[str, Any]:
    native = protocol.get("native_load")
    if not isinstance(native, dict) or native.get("status") != "failed":
        return protocol
    diagnostic = "\n".join(str(native.get(key, "")) for key in ("stdout_tail", "stderr_tail")).casefold()
    explicit_policy_markers = (
        "application control policy blocked", "application control policy has blocked",
        "应用程序控制策略", "app control policy", "win32 4551",
    )
    extension_block_markers = ("dll load failed while importing _pydantic_core", "dll load failed while importing pydantic_core")
    if not any(marker in diagnostic for marker in (*explicit_policy_markers, *extension_block_markers)):
        return protocol
    result = dict(protocol)
    result["native_load"] = {
        **native,
        "status": "unknown",
        "reason_code": "app_control_win32_4551" if any(marker in diagnostic for marker in explicit_policy_markers) else "windows_native_extension_load_unattributed",
        "worker_reported_status": native.get("status"),
        "worker_reported_reason_code": native.get("reason_code"),
        "policy_diagnostic_detected": any(marker in diagnostic for marker in explicit_policy_markers),
    }
    return result


def _read_lock_requirements(text: str) -> list[dict[str, Any]]:
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in text.splitlines():
        starts = bool(line and not line[0].isspace() and not line.startswith("#") and not line.startswith("--"))
        if starts:
            if current:
                blocks.append(current)
            current = [line]
        elif current:
            current.append(line)
    if current:
        blocks.append(current)

    requirements: list[dict[str, Any]] = []
    seen: set[str] = set()
    for block in blocks:
        head = block[0].strip().rstrip("\\").strip()
        try:
            parsed = Requirement(head)
        except InvalidRequirement as error:
            raise NoneBotPilotError(f"invalid requirement in frozen profile lock: {head!r}") from error
        exact = [specifier for specifier in parsed.specifier if specifier.operator in {"==", "==="}]
        if len(exact) != 1 or parsed.url:
            raise NoneBotPilotError(f"profile requirement is not an exact PyPI pin: {head!r}")
        try:
            version = Version(exact[0].version)
        except InvalidVersion as error:
            raise NoneBotPilotError(f"profile requirement has an invalid exact version: {head!r}") from error
        if parsed.marker is not None:
            raise NoneBotPilotError(f"platform profile contains an unresolved requirement marker: {head!r}")
        hashes = {match.group(1) for line in block for match in [_HASH_RE.search(line)] if match}
        if not hashes:
            raise NoneBotPilotError(f"profile requirement has no SHA-256 allowlist: {head!r}")
        name = canonicalize_name(parsed.name)
        if name in seen:
            raise NoneBotPilotError(f"profile lock contains duplicate normalized requirement: {name}")
        seen.add(name)
        requirements.append({"name": parsed.name, "canonical_name": name, "version": str(version), "hashes": hashes})
    if not requirements:
        raise NoneBotPilotError("frozen profile lock contains no requirements")
    return requirements


def _pypi_json_url(name: str, version: str) -> str:
    package = urllib.parse.quote(canonicalize_name(name), safe="-._")
    release = urllib.parse.quote(version, safe="-._!+*'()")
    return f"https://pypi.org/pypi/{package}/{release}/json"


def _load_release_metadata(name: str, version: str, cache_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    url = _pypi_json_url(name, version)
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    path = cache_root / "pypi-wheel-metadata" / "sha256" / key[:2] / key
    fetched = False
    if path.is_file():
        raw = path.read_bytes()
        if len(raw) > _MAX_METADATA_BYTES:
            raise NoneBotPilotError(f"cached PyPI metadata is too large for {name}=={version}")
    else:
        request = urllib.request.Request(url, headers={"User-Agent": "Ligamina-catalog-pilot/1.0", "Accept-Encoding": "identity"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                final_host = urllib.parse.urlparse(response.geturl()).hostname
                if final_host != "pypi.org":
                    raise NoneBotPilotError("PyPI JSON metadata redirected outside pypi.org")
                raw = response.read(_MAX_METADATA_BYTES + 1)
        except (OSError, urllib.error.URLError, TimeoutError) as error:
            raise NoneBotPilotError(f"PyPI release metadata unavailable for {name}=={version}: {error}") from error
        if len(raw) > _MAX_METADATA_BYTES:
            raise NoneBotPilotError(f"PyPI metadata exceeds the bounded response size for {name}=={version}")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(raw)
        temporary.replace(path)
        fetched = True
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as error:
        raise NoneBotPilotError(f"invalid PyPI JSON for {name}=={version}") from error
    info = document.get("info", {}) if isinstance(document, dict) else {}
    try:
        metadata_version_matches = isinstance(info.get("version"), str) and Version(info["version"]) == Version(version)
    except InvalidVersion:
        metadata_version_matches = False
    if (not isinstance(document, dict) or not metadata_version_matches
            or canonicalize_name(str(info.get("name", ""))) != canonicalize_name(name)):
        raise NoneBotPilotError(f"PyPI returned an unexpected release identity for {name}=={version}")
    return document, {"url_sha256": key, "response_sha256": _sha256(raw), "response_bytes": len(raw), "cache_hit": not fetched}


def _select_locked_wheel(
    requirement: dict[str, Any],
    document: dict[str, Any],
    *,
    platform_id: str = "linux-x86_64",
    python_version: str = "3.12.14",
    prefer_sha256: str | None = None,
) -> dict[str, Any]:
    files = document.get("urls")
    if not isinstance(files, list):
        raise NoneBotPilotError(f"PyPI response has no files list for {requirement['name']}=={requirement['version']}")
    matches: list[tuple[int, str, dict[str, Any]]] = []
    sdist_hash_matches = False
    for record in files:
        if not isinstance(record, dict):
            continue
        filename = record.get("filename")
        digest = (record.get("digests") or {}).get("sha256")
        if not isinstance(digest, str) or digest not in requirement["hashes"]:
            continue
        if record.get("packagetype") == "sdist":
            sdist_hash_matches = True
            continue
        if record.get("yanked") is True or not _requires_python_allows(record.get("requires_python"), python_version):
            continue
        compatible = _compatible_python_file(record, platform_id)
        if compatible is None:
            continue
        priority, wheel_filename = compatible
        if not isinstance(filename, str) or filename != wheel_filename:
            continue
        try:
            wheel_name, wheel_version, _, _ = parse_wheel_filename(filename)
        except (ValueError, InvalidVersion):
            continue
        if canonicalize_name(wheel_name) != requirement["canonical_name"] or wheel_version != Version(requirement["version"]):
            continue
        matches.append((priority, filename, record))
    if prefer_sha256:
        preferred = [item for item in matches if (item[2].get("digests") or {}).get("sha256") == prefer_sha256]
        if preferred:
            matches = preferred
    if not matches:
        reason = "dependency_source_build_required" if sdist_hash_matches else "dependency_wheel_unavailable"
        raise NoneBotPilotError(f"{reason}: no hash-locked CPython {python_version} {platform_id} wheel for {requirement['name']}=={requirement['version']}")
    matches.sort(key=lambda item: (item[0], item[1]))
    record = matches[0][2]
    digest = (record.get("digests") or {}).get("sha256")
    size = record.get("size")
    url = record.get("url")
    parsed_url = urllib.parse.urlparse(url or "")
    if parsed_url.scheme != "https" or parsed_url.hostname != "files.pythonhosted.org":
        raise NoneBotPilotError(f"PyPI wheel URL is outside the allowlist for {requirement['name']}=={requirement['version']}")
    if not isinstance(size, int) or size < 0 or size > _MAX_ARCHIVE_BYTES:
        raise NoneBotPilotError(f"invalid or oversized wheel metadata for {requirement['name']}=={requirement['version']}")
    return {"filename": record["filename"], "sha256": digest, "size_bytes": size, "url": url}


def _download_wheel(record: dict[str, Any], cache_root: Path) -> tuple[Path, int, bool]:
    digest = record["sha256"]
    path = cache_root / "wheel-cache" / "sha256" / digest[:2] / digest
    if path.is_file():
        if sha256_file(path) != digest or path.stat().st_size != record["size_bytes"]:
            raise NoneBotPilotError(f"content-addressed wheel cache failed integrity check: {digest}")
        return path, 0, True
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    request = urllib.request.Request(record["url"], headers={"User-Agent": "Ligamina-catalog-pilot/1.0", "Accept-Encoding": "identity"})
    written = 0
    digest_stream = hashlib.sha256()
    try:
        with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as output:
            final_host = urllib.parse.urlparse(response.geturl()).hostname
            if final_host != "files.pythonhosted.org":
                raise NoneBotPilotError("PyPI wheel download redirected outside files.pythonhosted.org")
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > record["size_bytes"] or written > _MAX_ARCHIVE_BYTES:
                    raise NoneBotPilotError(f"wheel exceeded the frozen size bound: {record['filename']}")
                digest_stream.update(chunk)
                output.write(chunk)
        if written != record["size_bytes"] or digest_stream.hexdigest() != digest:
            raise NoneBotPilotError(f"downloaded wheel failed frozen size/SHA-256 verification: {record['filename']}")
        try:
            temporary.replace(path)
        except FileExistsError:
            temporary.unlink(missing_ok=True)
            if sha256_file(path) != digest:
                raise NoneBotPilotError(f"concurrent wheel cache write failed integrity check: {digest}")
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return path, written, False


def _prepare_wheelhouse(
    lock_text: str,
    *,
    cache_root: Path,
    wheelhouse: Path,
    direct_artifact: dict[str, Any],
    direct_artifact_path: Path,
    platform_id: str = "linux-x86_64",
    python_version: str = "3.12.14",
) -> dict[str, Any]:
    requirements = _read_lock_requirements(lock_text)
    direct_requirement = next((item for item in requirements if item["canonical_name"] == canonicalize_name(direct_artifact["distribution_name"])), None)
    if direct_requirement is None or direct_artifact["version"] != direct_requirement["version"]:
        raise NoneBotPilotError("catalog artifact does not match a direct exact requirement in its frozen profile lock")
    artifact_sha = direct_artifact["sha256"]
    if artifact_sha not in direct_requirement["hashes"] or sha256_file(direct_artifact_path) != artifact_sha:
        raise NoneBotPilotError("catalog artifact is not bound by the direct profile lock SHA-256")

    resolved_records: list[dict[str, Any]] = []
    metadata_receipts: dict[str, dict[str, Any]] = {}
    for requirement in requirements:
        if requirement is direct_requirement:
            resolved_records.append({
                **requirement,
                "filename": direct_artifact["filename"],
                "sha256": artifact_sha,
                "size_bytes": direct_artifact_path.stat().st_size,
                "source": "frozen_catalog_artifact",
            })
            continue
        metadata, receipt = _load_release_metadata(requirement["name"], requirement["version"], cache_root)
        metadata_receipts[f"{requirement['canonical_name']}=={requirement['version']}"] = receipt
        selected = _select_locked_wheel(
            requirement, metadata, platform_id=platform_id, python_version=python_version,
        )
        resolved_records.append({**requirement, **selected, "source": "pypi_hash_locked_wheel"})

    wheelhouse.mkdir(parents=True, exist_ok=True)
    downloads = {"downloaded_bytes": 0, "downloaded_archives": 0, "cache_hits": 0, "archive_bytes": 0}
    sources: dict[str, Path] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=_MAX_FETCH_WORKERS) as pool:
        futures = {
            pool.submit(_download_wheel, record, cache_root): record
            for record in resolved_records if record.get("source") != "frozen_catalog_artifact"
        }
        for future, record in [(future, futures[future]) for future in futures]:
            source, transferred, cached = future.result()
            sources[record["sha256"]] = source
            downloads["downloaded_bytes"] += transferred
            downloads["downloaded_archives"] += int(not cached)
            downloads["cache_hits"] += int(cached)
    sources[artifact_sha] = direct_artifact_path
    downloads["cache_hits"] += 1

    for record in resolved_records:
        target = wheelhouse / record["filename"]
        source = sources[record["sha256"]]
        if target.exists():
            if sha256_file(target) != record["sha256"]:
                raise NoneBotPilotError(f"wheelhouse filename collision with differing hash: {record['filename']}")
            continue
        try:
            os.link(source, target)
        except OSError:
            shutil.copyfile(source, target)
        if sha256_file(target) != record["sha256"]:
            raise NoneBotPilotError(f"staged wheel failed SHA-256 check: {record['filename']}")
        downloads["archive_bytes"] += target.stat().st_size
    return {
        **downloads,
        "locked_package_count": len(requirements),
        "unique_archive_count": len({record["sha256"] for record in resolved_records}),
        "profile_lock_sha256": hashlib.sha256(lock_text.encode("utf-8")).hexdigest(),
        "wheel_manifest_sha256": _sha256(json.dumps([
            {"filename": record["filename"], "sha256": record["sha256"], "size_bytes": record["size_bytes"]}
            for record in resolved_records
        ], sort_keys=True, separators=(",", ":")).encode("utf-8")),
        "metadata_response_count": len(metadata_receipts),
        "metadata_download_bytes": sum(item["response_bytes"] for item in metadata_receipts.values() if not item["cache_hit"]),
        "metadata_cache_hits": sum(1 for item in metadata_receipts.values() if item["cache_hit"]),
        "metadata_response_sha256": _sha256(json.dumps(metadata_receipts, sort_keys=True, separators=(",", ":")).encode("utf-8")),
    }


def _write_runner(bundle: Path) -> None:
    runner = bundle / "runner.py"
    runner.write_text(
        """from __future__ import annotations
import hashlib, json, os, re, shutil, subprocess, sys, threading, time, types
from pathlib import Path

work = Path('/work')
venv = work / 'venv'
spec = json.loads(Path('/input/spec.json').read_text(encoding='utf-8'))
os.environ.update({
  'LIGAMINA_ENTRY_ID': spec['registry_entry_id'], 'LIGAMINA_PACKAGE': spec['package'],
  'LIGAMINA_VERSION': spec['version'], 'LIGAMINA_PROFILE_ID': spec['profile_id'],
  'LIGAMINA_PROFILE_LOCK_SHA256': spec['profile_lock_sha256'], 'LIGAMINA_ARTIFACT_SHA256': spec['artifact_sha256'],
  'LIGAMINA_ENTRY_POINT': spec['entry_point'],
})
report = {
  'schema_version': 1, 'task_id': 'S2-09', 'registry_entry_id': os.environ['LIGAMINA_ENTRY_ID'],
  'package': os.environ['LIGAMINA_PACKAGE'], 'version': os.environ['LIGAMINA_VERSION'],
  'profile_id': os.environ['LIGAMINA_PROFILE_ID'], 'profile_lock_sha256': os.environ['LIGAMINA_PROFILE_LOCK_SHA256'],
  'artifact_sha256': os.environ['LIGAMINA_ARTIFACT_SHA256'],
  'dependency_install': {'status': 'unknown', 'reason_code': None},
  'native_load': {'status': 'skipped', 'reason_code': 'dependency_install_not_passed'},
  'cleanup': {'status': 'unknown', 'reason_code': 'worker_cleanup_unconfirmed'},
  'resource_observations': {'peak_writable_scratch_bytes': 0},
}
stop = threading.Event()
peak = [0]
def measure_tree(root):
  total = 0
  try:
    for base, dirs, files in os.walk(root, followlinks=False):
      dirs[:] = [d for d in dirs if not (Path(base) / d).is_symlink()]
      for name in files:
        path = Path(base) / name
        try:
          if not path.is_symlink(): total += path.stat().st_size
        except OSError: pass
  except OSError: pass
  return total
def sample():
  while not stop.is_set():
    peak[0] = max(peak[0], measure_tree(work))
    stop.wait(0.2)
thread = threading.Thread(target=sample, daemon=True); thread.start()
def run(argv, timeout=900):
  started = time.monotonic()
  try:
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={
      'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': '/work/home', 'TMPDIR': '/tmp', 'TEMP': '/tmp', 'TMP': '/tmp',
      'LANG': 'C.UTF-8', 'PIP_NO_INPUT': '1', 'PIP_DISABLE_PIP_VERSION_CHECK': '1', 'PIP_CONFIG_FILE': '/dev/null',
      'PYTHONNOUSERSITE': '1', 'PYTHONDONTWRITEBYTECODE': '1',
    })
  except OSError as error:
    return types.SimpleNamespace(returncode=127, stdout=b'', stderr=str(error).encode(), timed_out=False, output_overflow=False), time.monotonic() - started
  output = {'stdout': bytearray(), 'stderr': bytearray(), 'tail': {'stdout': bytearray(), 'stderr': bytearray()}, 'total': {'stdout': 0, 'stderr': 0}, 'hash': {'stdout': hashlib.sha256(), 'stderr': hashlib.sha256()}}
  overflow = threading.Event()
  def drain(name, stream):
    while True:
      chunk = stream.read(65536)
      if not chunk: return
      output['total'][name] += len(chunk)
      output['hash'][name].update(chunk)
      output['tail'][name].extend(chunk)
      if len(output['tail'][name]) > 4096: del output['tail'][name][:-4096]
      room = 1024 * 1024 - len(output[name])
      if room > 0: output[name].extend(chunk[:room])
      if output['total'][name] > 1024 * 1024: overflow.set()
  readers = [threading.Thread(target=drain, args=('stdout', process.stdout), daemon=True), threading.Thread(target=drain, args=('stderr', process.stderr), daemon=True)]
  for reader in readers: reader.start()
  deadline = started + timeout
  timed_out = False
  while process.poll() is None:
    if overflow.is_set(): process.kill(); break
    if time.monotonic() >= deadline: timed_out = True; process.kill(); break
    time.sleep(0.05)
  try: returncode = process.wait(timeout=5)
  except subprocess.TimeoutExpired: process.kill(); returncode = process.wait(timeout=5)
  for reader in readers: reader.join(timeout=5)
  result = types.SimpleNamespace(returncode=None if timed_out or overflow.is_set() else returncode,
    stdout=bytes(output['stdout']), stderr=bytes(output['stderr']), stdout_total=output['total']['stdout'],
    stderr_total=output['total']['stderr'], stdout_sha256=output['hash']['stdout'].hexdigest(),
    stderr_sha256=output['hash']['stderr'].hexdigest(), stdout_tail=bytes(output['tail']['stdout']),
    stderr_tail=bytes(output['tail']['stderr']), timed_out=timed_out, output_overflow=overflow.is_set())
  return result, time.monotonic() - started
def diagnostic(result):
  out = result.stdout or b''; err = result.stderr or b''
  def safe_tail(raw):
    text = raw.decode('utf-8','replace')[-4096:]
    text = re.sub(r'(?i)(api[_-]?key|access[_-]?token|token|secret|password)(\\s*[:=]\\s*)[^\\s,;]+', r'\\1\\2[redacted]', text)
    text = re.sub(r'(?i)bearer\\s+[a-z0-9._~+/-]+=*', 'Bearer [redacted]', text)
    return text
  return {'exit_code': result.returncode, 'stdout_bytes': getattr(result,'stdout_total',len(out)), 'stderr_bytes': getattr(result,'stderr_total',len(err)),
    'stdout_sha256': getattr(result,'stdout_sha256',hashlib.sha256(out).hexdigest()), 'stderr_sha256': getattr(result,'stderr_sha256',hashlib.sha256(err).hexdigest()),
    'stdout_tail': safe_tail(getattr(result,'stdout_tail',out[-4096:])), 'stderr_tail': safe_tail(getattr(result,'stderr_tail',err[-4096:])),
    'timed_out': getattr(result,'timed_out',False), 'output_overflow': getattr(result,'output_overflow',False)}
try:
  os.environ.update({'HOME':'/work/home','XDG_CACHE_HOME':'/work/cache','TMPDIR':'/tmp','TEMP':'/tmp','TMP':'/tmp'})
  Path('/work/home').mkdir(parents=True, exist_ok=True)
  result, elapsed = run(['python', '-m', 'venv', str(venv)], timeout=120)
  report['environment_prepare'] = {**diagnostic(result), 'elapsed_seconds': round(elapsed, 3)}
  if result.returncode == 0:
    python = str(venv / 'bin' / 'python')
    install, elapsed = run([python, '-m', 'pip', 'install', '--disable-pip-version-check', '--no-input', '--no-cache-dir', '--no-index', '--find-links=/input/wheelhouse', '--only-binary=:all:', '--require-hashes', '-r', '/input/requirements.txt'], timeout=1200)
    report['dependency_install'] = {**diagnostic(install), 'status': 'passed' if install.returncode == 0 else ('unknown' if install.returncode is None else 'failed'), 'reason_code': None if install.returncode == 0 else ('dependency_install_output_limit' if install.output_overflow else 'dependency_install_timeout' if install.timed_out else 'dependency_install_failed'), 'elapsed_seconds': round(elapsed, 3)}
    if install.returncode == 0:
      probe, elapsed = run([python, '/input/native_probe.py', os.environ['LIGAMINA_ENTRY_POINT']], timeout=120)
      combined = ((probe.stdout or b'') + b'\\n' + (probe.stderr or b'')).decode('utf-8','replace')
      skip_reason = None
      if probe.returncode != 0 and not probe.output_overflow and not probe.timed_out:
        if 'ValidationError' in combined and 'Field required' in combined:
          skip_reason = 'missing_config_fixture'
        elif '请配置' in combined and re.search(r'(?i)(key|token|secret|密钥|凭证)', combined):
          skip_reason = 'missing_credentials'
      native_status = 'passed' if probe.returncode == 0 else 'skipped' if skip_reason else 'unknown' if probe.returncode is None else 'failed'
      native_reason = None if probe.returncode == 0 else skip_reason or ('native_load_output_limit' if probe.output_overflow else 'native_load_timeout' if probe.timed_out else 'native_plugin_load_failed')
      report['native_load'] = {**diagnostic(probe), 'status': native_status, 'reason_code': native_reason, 'elapsed_seconds': round(elapsed, 3)}
      try:
        native = json.loads((probe.stdout or b'').decode('utf-8','replace').splitlines()[-1]) if probe.stdout else {}
        if isinstance(native, dict) and native.get('task_id') == 'S2-09': report['native_load'].update(native)
      except (json.JSONDecodeError, IndexError): pass
  else:
    report['dependency_install'] = {'status':'unknown' if result.returncode is None else 'failed','reason_code':'venv_prepare_timeout' if result.returncode is None else 'venv_prepare_failed',**diagnostic(result),'elapsed_seconds':round(elapsed,3)}
finally:
  try:
    if venv.exists(): shutil.rmtree(venv)
    shutil.rmtree('/work/home', ignore_errors=True)
    shutil.rmtree('/work/cache', ignore_errors=True)
    report['cleanup'] = {'status':'passed'}
  except OSError as error:
    report['cleanup'] = {'status':'failed','reason_code':'worker_scratch_cleanup_failed','error':str(error)[:1024]}
  stop.set(); thread.join(timeout=2)
  peak[0] = max(peak[0], measure_tree(work))
  report['resource_observations']['peak_writable_scratch_bytes'] = peak[0]
print(json.dumps(report, ensure_ascii=False, separators=(',',':')))
""",
        encoding="utf-8",
        newline="\n",
    )
    probe = bundle / "native_probe.py"
    probe.write_text(
        """import json, sys\nimport nonebot\nfrom nonebot.adapters.onebot.v11 import Adapter\n\nentry = sys.argv[1]\nnonebot.init(driver='~fastapi', nickname={'Bot'})\ndriver = nonebot.get_driver()\ndriver.register_adapter(Adapter)\nplugin = nonebot.load_plugin(entry)\nif plugin is None:\n    raise RuntimeError(f'NoneBot did not load {entry!r}')\nloaded = {item.name: item for item in nonebot.get_loaded_plugins()}\nif entry not in loaded and getattr(plugin, 'name', None) not in loaded:\n    raise RuntimeError(f'NoneBot did not register the loaded plugin {entry!r}')\nprint(json.dumps({'schema_version':1,'task_id':'S2-09','entry_point':entry,'entry_module_imported':True,'plugin_registered':True,'loaded_plugin_count':len(loaded)},separators=(',',':')))\n""",
        encoding="utf-8",
        newline="\n",
    )


def _download_windows_runtime(runtime_profile: dict[str, Any], cache_root: Path) -> Path:
    url = runtime_profile.get("archive_url")
    digest = runtime_profile.get("archive_sha256")
    parsed = urllib.parse.urlparse(url or "")
    if parsed.scheme != "https" or parsed.hostname != "www.python.org" or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise NoneBotPilotError("admitted Windows Python runtime has an invalid archive identity")
    path = cache_root / "windows-runtime-cache" / "sha256" / digest[:2] / digest
    if path.is_file():
        if path.stat().st_size > _MAX_WINDOWS_RUNTIME_ARCHIVE_BYTES or sha256_file(path) != digest:
            raise NoneBotPilotError("cached Windows Python runtime failed its frozen size/SHA-256 check")
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    written = 0
    digest_stream = hashlib.sha256()
    request = urllib.request.Request(url, headers={"User-Agent": "Ligamina-catalog-pilot/1.0", "Accept-Encoding": "identity"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as output:
            if urllib.parse.urlparse(response.geturl()).hostname != "www.python.org":
                raise NoneBotPilotError("Python runtime download redirected outside www.python.org")
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > _MAX_WINDOWS_RUNTIME_ARCHIVE_BYTES:
                    raise NoneBotPilotError("Windows Python runtime archive exceeded its frozen download bound")
                digest_stream.update(chunk)
                output.write(chunk)
        if not written or digest_stream.hexdigest() != digest:
            raise NoneBotPilotError("downloaded Windows Python runtime failed its frozen SHA-256 check")
        try:
            temporary.replace(path)
        except FileExistsError:
            temporary.unlink(missing_ok=True)
            if not path.is_file() or sha256_file(path) != digest:
                raise NoneBotPilotError("concurrent Windows runtime cache write failed integrity check")
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return path


def _extract_checked_zip(archive_path: Path, destination: Path, *, max_expanded_bytes: int) -> dict[str, Any]:
    destination.mkdir(parents=True, exist_ok=False)
    seen: set[str] = set()
    expanded = 0
    file_count = 0
    try:
        with zipfile.ZipFile(archive_path) as archive:
            for info in archive.infolist():
                name = info.filename
                relative = PurePosixPath(name)
                key = relative.as_posix().casefold().rstrip("/")
                mode = info.external_attr >> 16
                if (
                    not name or "\\" in name or relative.is_absolute()
                    or any(part in {"", ".", ".."} or ":" in part for part in relative.parts)
                    or key in seen or stat.S_ISLNK(mode)
                ):
                    raise NoneBotPilotError("Windows runtime ZIP contains an unsafe or duplicate path")
                seen.add(key)
                target = destination.joinpath(*relative.parts)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                expanded += info.file_size
                if info.file_size < 0 or expanded > max_expanded_bytes:
                    raise NoneBotPilotError("Windows runtime ZIP exceeds its expanded size bound")
                target.parent.mkdir(parents=True, exist_ok=True)
                written = 0
                with archive.open(info, "r") as source, target.open("xb") as output:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > info.file_size or written > max_expanded_bytes:
                            raise NoneBotPilotError("Windows runtime ZIP member exceeded its declared size")
                        output.write(chunk)
                if written != info.file_size:
                    raise NoneBotPilotError("Windows runtime ZIP member length differs from its directory entry")
                file_count += 1
    except NoneBotPilotError:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError) as error:
        shutil.rmtree(destination, ignore_errors=True)
        raise NoneBotPilotError(f"Windows runtime ZIP could not be safely extracted: {error}") from error
    return {"file_count": file_count, "expanded_bytes": expanded}


def _verify_windows_python_signature(python_path: Path) -> dict[str, str]:
    pwsh = shutil.which("pwsh")
    if not pwsh:
        raise NoneBotPilotError("PowerShell 7 is required to verify the signed Windows Python runtime")
    command = (
        "$signature=Get-AuthenticodeSignature -LiteralPath '" + str(python_path).replace("'", "''")
        + "'; [pscustomobject]@{status=$signature.Status.ToString(); subject=$signature.SignerCertificate.Subject}|ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run([pwsh, "-NoProfile", "-NonInteractive", "-Command", command], capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise NoneBotPilotError(f"could not verify the Windows Python Authenticode signature: {error}") from error
    try:
        signature = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise NoneBotPilotError("Windows Python Authenticode verification returned invalid output") from error
    subject = signature.get("subject") if isinstance(signature, dict) else None
    status = signature.get("status") if isinstance(signature, dict) else None
    if result.returncode != 0 or status != "Valid" or not isinstance(subject, str) or "Python Software Foundation" not in subject:
        raise NoneBotPilotError("Windows Python executable does not have the admitted valid PSF signature")
    return {"status": status, "signer_subject": subject}


def _windows_probe_scripts(bundle: Path) -> None:
    (bundle / "native_probe.py").write_text(
        """import json, sys\nimport nonebot\nfrom nonebot.adapters.onebot.v11 import Adapter\n\nentry = sys.argv[1]\nnonebot.init(driver='~fastapi', nickname={'Bot'})\ndriver = nonebot.get_driver()\ndriver.register_adapter(Adapter)\nplugin = nonebot.load_plugin(entry)\nif plugin is None:\n    raise RuntimeError(f'NoneBot did not load {entry!r}')\nloaded = {item.name: item for item in nonebot.get_loaded_plugins()}\nif entry not in loaded and getattr(plugin, 'name', None) not in loaded:\n    raise RuntimeError(f'NoneBot did not register the loaded plugin {entry!r}')\nprint(json.dumps({'schema_version':1,'task_id':'S2-09','entry_point':entry,'entry_module_imported':True,'plugin_registered':True,'loaded_plugin_count':len(loaded)},separators=(',',':')))\n""",
        encoding="utf-8",
        newline="\n",
    )
    (bundle / "windows_driver.py").write_text(
        r'''from __future__ import annotations
import hashlib, json, os, shutil, subprocess, sys, zipfile
from pathlib import Path, PurePosixPath

input_root = Path(sys.argv[1])
work = Path(sys.argv[2])
spec = json.loads((input_root / "spec.json").read_text(encoding="utf-8"))
report = {
  "schema_version": 1, "task_id": "S2-09", "registry_entry_id": spec["registry_entry_id"],
  "package": spec["package"], "version": spec["version"], "profile_id": spec["profile_id"],
  "profile_lock_sha256": spec["profile_lock_sha256"], "artifact_sha256": spec["artifact_sha256"],
  "dependency_install": {"status": "unknown", "reason_code": "windows_probe_setup_failed"},
  "native_load": {"status": "skipped", "reason_code": "dependency_install_not_passed"},
  "resource_observations": {"peak_writable_scratch_bytes": 0},
}

def safe_extract_wheel(path: Path, destination: Path) -> None:
    seen = set()
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            relative = PurePosixPath(info.filename)
            key = relative.as_posix().casefold().rstrip("/")
            if not info.filename or "\\" in info.filename or relative.is_absolute() or any(part in {"", ".", ".."} or ":" in part for part in relative.parts) or key in seen:
                raise RuntimeError("pip wheel contains an unsafe or duplicate path")
            seen.add(key)
            target = destination.joinpath(*relative.parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output, 1024 * 1024)

def run(command, timeout):
    return subprocess.run(command, cwd=work, capture_output=True, text=True, errors="replace", timeout=timeout, check=False)

try:
    expected_pip = spec["pip_bootstrap_sha256"]
    pip_wheel = input_root / "pip-bootstrap.whl"
    if hashlib.sha256(pip_wheel.read_bytes()).hexdigest() != expected_pip:
        raise RuntimeError("offline pip bootstrap SHA-256 mismatch")
    site = work / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    safe_extract_wheel(pip_wheel, site)
    python = Path(sys.executable)
    wheelhouse = input_root / "wheelhouse"
    requirements = input_root / "requirements.txt"
    install = run([
      str(python), "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "--no-cache-dir",
      "--no-index", "--find-links", str(wheelhouse), "--only-binary=:all:", "--require-hashes",
      "-r", str(requirements), "--target", str(site),
    ], spec["install_timeout_seconds"])
    if install.returncode != 0:
        report["dependency_install"] = {"status": "failed", "reason_code": "dependency_install_failed", "exit_code": install.returncode,
          "stdout_tail": install.stdout[-4096:], "stderr_tail": install.stderr[-4096:]}
    else:
        report["dependency_install"] = {"status": "passed", "reason_code": None, "exit_code": 0,
          "stdout_sha256": hashlib.sha256(install.stdout.encode("utf-8", errors="replace")).hexdigest(),
          "stderr_sha256": hashlib.sha256(install.stderr.encode("utf-8", errors="replace")).hexdigest()}
        probe = run([str(python), str(input_root / "native_probe.py"), spec["entry_point"]], spec["native_timeout_seconds"])
        if probe.returncode != 0:
            report["native_load"] = {"status": "failed", "reason_code": "native_load_failed", "exit_code": probe.returncode,
              "stdout_tail": probe.stdout[-4096:], "stderr_tail": probe.stderr[-4096:]}
        else:
            try:
                result = None
                for line in reversed(probe.stdout.splitlines()):
                    if not line.startswith("{"):
                        continue
                    try:
                        result = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    break
                if result is None:
                    raise ValueError("native probe JSON report is missing")
            except (StopIteration, json.JSONDecodeError, ValueError):
                report["native_load"] = {"status": "unknown", "reason_code": "native_probe_report_missing",
                  "stdout_tail": probe.stdout[-4096:], "stderr_tail": probe.stderr[-4096:]}
            else:
                report["native_load"] = {"status": "passed", "reason_code": None, "exit_code": 0, "native_report": result,
                  "stderr_sha256": hashlib.sha256(probe.stderr.encode("utf-8", errors="replace")).hexdigest()}
except Exception as error:
    report["dependency_install"] = {"status": "unknown", "reason_code": "windows_probe_setup_failed", "error": (type(error).__name__ + ": " + str(error))[:1024]}
print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
''',
        encoding="utf-8",
        newline="\n",
    )
    (bundle / "run-nonebot-pilot.ps1").write_text(
        r'''$ErrorActionPreference = 'Stop'
$inputRoot = 'C:\Ligamina\Input'
$workspaceRoot = 'C:\Ligamina\Workspace'
$work = Join-Path $workspaceRoot 'p305-nonebot'
$runtime = Join-Path $work 'runtime'
New-Item -ItemType Directory -Path $work -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $inputRoot 'runtime') -Destination $runtime -Recurse -Force
$python = Join-Path $runtime 'python.exe'
$pth = Join-Path $runtime 'python312._pth'
if (-not (Test-Path -LiteralPath $python) -or -not (Test-Path -LiteralPath $pth)) { throw 'admitted CPython runtime layout is incomplete' }
@('python312.zip', '.', (Join-Path $work 'site-packages'), 'import site') | Set-Content -LiteralPath $pth -Encoding ascii
$env:PYTHONNOUSERSITE = '1'
$env:PIP_NO_INDEX = '1'
$env:PIP_DISABLE_PIP_VERSION_CHECK = '1'
$env:PIP_NO_INPUT = '1'
$driverOutput = & $python (Join-Path $inputRoot 'windows_driver.py') $inputRoot $work
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Output $driverOutput
exit 0
''',
        encoding="utf-8",
        newline="\n",
    )


def run_nonebot_windows_load_probe(
    snapshot_dir: Path,
    entry_id: str,
    *,
    profile_lock_path: Path,
    cache_root: Path = DEFAULT_CACHE_ROOT,
    timeout_seconds: int = 1200,
    context: tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], bytes, bytes, bytes] | None = None,
    worker_labels: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one hash-locked NoneBot install/load probe under the admitted Windows Sandbox profile."""
    snapshot_dir = Path(snapshot_dir).resolve(strict=True)
    profile_lock_path = Path(profile_lock_path).resolve(strict=True)
    cache_root = Path(cache_root).resolve(strict=True)
    entries, units, artifacts, profiles, artifact_lock_raw, profile_lock_raw, catalog_raw = context or _catalog_context(
        snapshot_dir, platform="windows", profile_lock_path=profile_lock_path,
    )
    entry = entries.get(entry_id)
    unit = units.get(entry_id)
    artifact_entry = artifacts.get(entry_id)
    profile_record = profiles.get(entry_id)
    profile = (profile_record or {}).get("profiles", {}).get("windows-x86_64", {})
    lock_ref = profile.get("dependency_lock")
    if not isinstance(entry, dict) or not isinstance(unit, dict) or entry.get("ecosystem") != "nonebot":
        raise NoneBotPilotError(f"entry is not a frozen Windows NoneBot catalog item: {entry_id}")
    if unit.get("artifact_status") != "locked" or unit.get("profile_status") != "locked" or profile.get("status") != "locked":
        raise NoneBotPilotError("Windows NoneBot pilot entry has no locked artifact and admitted profile")
    try:
        lock_document = json.loads(profile_lock_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise NoneBotPilotError("alternate Windows profile lock is invalid JSON") from error
    runtime_profiles = lock_document.get("profiles", {}).get("nonebot", {}).get("runtime_profiles_by_entry", {})
    runtime_profile = runtime_profiles.get(entry_id) if isinstance(runtime_profiles, dict) else None
    if not isinstance(runtime_profile, dict) or profile.get("runtime_profile_id") != runtime_profile.get("profile_id"):
        raise NoneBotPilotError("Windows profile lock does not bind an admitted runtime for this entry")
    admission_path = Path(runtime_profile["admission_report_path"])
    if not admission_path.is_absolute():
        admission_path = ROOT / admission_path
    verified_runtime = _load_windows_runtime_admission(admission_path)
    if verified_runtime != runtime_profile:
        raise NoneBotPilotError("Windows profile runtime identity differs from the P3-04 admission report")
    if profile.get("profile_id") != _python_profile_id("3.12.10", "windows-x86_64"):
        raise NoneBotPilotError("Windows profile ID does not encode the admitted CPython 3.12.10 runtime")
    if lock_document.get("profiles", {}).get("nonebot", {}).get("bootstrap_installer") != WINDOWS_PIP_BOOTSTRAP:
        raise NoneBotPilotError("Windows profile lock does not freeze the offline pip bootstrap identity")
    if not isinstance(artifact_entry, dict) or not isinstance(lock_ref, dict):
        raise NoneBotPilotError("frozen Windows artifact or dependency lock is missing")
    artifact_record = next((item for item in artifact_entry.get("artifacts", []) if item.get("platform") == "windows-x86_64"), None)
    artifact = artifact_record.get("artifact") if isinstance(artifact_record, dict) else None
    if not isinstance(artifact, dict) or profile.get("artifact_sha256") != (artifact.get("computed_digests") or {}).get("sha256"):
        raise NoneBotPilotError("Windows profile lock points to different direct artifact bytes")
    artifact_path = cache_root / artifact["cache_path"]
    if not artifact_path.is_file() or sha256_file(artifact_path) != artifact["computed_digests"]["sha256"]:
        raise NoneBotPilotError("Windows catalog wheel failed its frozen artifact SHA-256 check")
    lock_path = cache_root / lock_ref["cache_path"]
    lock_raw = lock_path.read_bytes()
    if len(lock_raw) != lock_ref.get("size_bytes") or _sha256(lock_raw) != lock_ref.get("sha256"):
        raise NoneBotPilotError("Windows dependency lock failed its frozen hash/size check")
    if artifact.get("build_required") or not artifact.get("filename", "").endswith(".whl"):
        raise NoneBotPilotError("Windows catalog pilot only installs frozen wheel artifacts")
    distribution_name = artifact_entry.get("distribution_name") or entry.get("package")
    version = entry.get("advertised_version")
    if not isinstance(distribution_name, str) or not isinstance(version, str):
        raise NoneBotPilotError("Windows catalog entry lacks exact distribution/version identity")
    entry_point = entry.get("entry_point") or entry.get("registry_identity", {}).get("module_name")
    if not isinstance(entry_point, str) or not entry_point.strip():
        raise NoneBotPilotError("Windows NoneBot registry entry has no importable entry point")
    if worker_labels and not all(key in worker_labels for key in ("campaign_id", "job_id", "attempt_id", "generation")):
        raise NoneBotPilotError("worker labels require campaign_id, job_id, attempt_id, and generation")

    runtime_archive = _download_windows_runtime(verified_runtime, cache_root)
    with tempfile.TemporaryDirectory(prefix="ligamina-pilot-nonebot-windows-", dir=cache_root) as temporary:
        bundle = Path(temporary)
        runtime_dir = bundle / "runtime"
        extraction = _extract_checked_zip(runtime_archive, runtime_dir, max_expanded_bytes=_MAX_WINDOWS_RUNTIME_EXPANDED_BYTES)
        python_path = runtime_dir / "python.exe"
        if not python_path.is_file() or sha256_file(python_path) != verified_runtime["executable_sha256"]:
            raise NoneBotPilotError("extracted Windows Python executable differs from the admitted runtime hash")
        if not (runtime_dir / "python312.zip").is_file() or not (runtime_dir / "python312._pth").is_file():
            raise NoneBotPilotError("admitted Windows Python archive has an incomplete embeddable runtime layout")
        signature = _verify_windows_python_signature(python_path)
        wheelhouse = bundle / "wheelhouse"
        dependency_stats = _prepare_wheelhouse(
            lock_raw.decode("utf-8"), cache_root=cache_root, wheelhouse=wheelhouse,
            direct_artifact={"distribution_name": distribution_name, "version": version, "filename": artifact["filename"], "sha256": artifact["computed_digests"]["sha256"]},
            direct_artifact_path=artifact_path,
            platform_id="windows-x86_64", python_version="3.12.10",
        )
        pip_metadata, pip_receipt = _load_release_metadata("pip", WINDOWS_PIP_BOOTSTRAP["version"], cache_root)
        pip_requirement = {
            "name": "pip", "canonical_name": "pip", "version": WINDOWS_PIP_BOOTSTRAP["version"],
            "hashes": {WINDOWS_PIP_BOOTSTRAP["sha256"]},
        }
        pip_record = _select_locked_wheel(
            pip_requirement, pip_metadata, platform_id="windows-x86_64", python_version="3.12.10",
            prefer_sha256=WINDOWS_PIP_BOOTSTRAP["sha256"],
        )
        if pip_record["filename"] != WINDOWS_PIP_BOOTSTRAP["filename"]:
            raise NoneBotPilotError("PyPI pip bootstrap wheel filename differs from the frozen profile identity")
        pip_path, pip_downloaded_bytes, pip_cache_hit = _download_wheel(pip_record, cache_root)
        shutil.copyfile(pip_path, bundle / "pip-bootstrap.whl")
        spec = {
            "registry_entry_id": entry_id, "package": entry.get("package"), "version": version,
            "profile_id": unit.get("profile_id"), "profile_lock_sha256": _sha256(profile_lock_raw),
            "artifact_sha256": artifact["computed_digests"]["sha256"], "entry_point": entry_point,
            "pip_bootstrap_sha256": WINDOWS_PIP_BOOTSTRAP["sha256"],
            "install_timeout_seconds": max(30, min(timeout_seconds - 30, 1200)),
            "native_timeout_seconds": max(30, min(timeout_seconds - 20, 600)),
        }
        (bundle / "requirements.txt").write_bytes(lock_raw)
        (bundle / "spec.json").write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        _windows_probe_scripts(bundle)
        bundle_bytes = sum(item.stat().st_size for item in bundle.rglob("*") if item.is_file())
        temp_root = cache_root / "s209-windows-worker-temp"
        temp_root.mkdir(parents=True, exist_ok=True)
        worker = WindowsSandboxWorker(repository_root=ROOT, temporary_root=temp_root)
        command = (
            r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
            "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", r"C:\Ligamina\Input\run-nonebot-pilot.ps1",
        )
        labels = worker_labels or {}
        result = worker.run(WorkerSpec(
            run_id=labels.get("campaign_id", "p305-nonebot-windows-pilot"), entry_id=entry_id,
            command=command, input_dir=bundle,
            limits=WorkerLimits(timeout_seconds=timeout_seconds, memory_mb=8192, cpu_cores=2, process_limit=256),
            attempt_id=labels.get("attempt_id"), generation=labels.get("generation"), job_id=labels.get("job_id"),
        ))
        protocol = None
        for line in reversed(result.stdout.decode("utf-8", errors="replace").splitlines()):
            try:
                candidate = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict) and candidate.get("task_id") == "S2-09" and candidate.get("registry_entry_id") == entry_id:
                protocol = candidate
                break
        if isinstance(protocol, dict):
            protocol = _reclassify_windows_app_control_load(protocol)
            protocol["cleanup"] = {
                "status": "passed" if result.cleanup_confirmed else "unknown",
                "reason_code": None if result.cleanup_confirmed else "worker_cleanup_unconfirmed",
                "evidence": "Windows Sandbox teardown and worker resource reconciliation",
            }
        if result.status != "passed" or not result.cleanup_confirmed or not isinstance(protocol, dict):
            overall_status = "unknown" if result.status == "unknown" else "failed"
            overall_reason = result.reason_code or "worker_report_invalid"
        else:
            stage_statuses = [protocol.get(key, {}).get("status") for key in ("dependency_install", "native_load", "cleanup")]
            if "failed" in stage_statuses:
                overall_status = "failed"
            elif "unknown" in stage_statuses:
                overall_status = "unknown"
            else:
                overall_status = "skipped"
            overall_reason = next((protocol[key].get("reason_code") for key in ("dependency_install", "native_load", "cleanup") if protocol.get(key, {}).get("reason_code")), "missing_entry_bound_fixture" if overall_status == "skipped" else None)
        return {
            "schema_version": 1, "task_id": "S2-09", "result_id": f"catalog:{entry_id}|windows",
            "unit_type": "catalog_entry", "registry_entry_id": entry_id, "ecosystem": "nonebot",
            "package": entry.get("package"), "version": version, "platform": "windows",
            "status": overall_status, "reason_code": overall_reason, "attempted": True,
            "detail": "NoneBot dependencies were installed offline with hashes and the native plugin was loaded using the P3-04 admitted PSF-signed CPython runtime inside Windows Sandbox; no entry-bound behavior fixture exists.",
            "profile_id": unit.get("profile_id"), "profile_lock_sha256": lock_ref["sha256"],
            "profile_document_sha256": _sha256(profile_lock_raw), "catalog_document_sha256": _sha256(catalog_raw),
            "artifact_lock_sha256": _sha256(artifact_lock_raw), "requirements_lock_sha256": _sha256(lock_raw),
            "artifact_sha256": artifact["computed_digests"]["sha256"], "plugin_archive_bytes": artifact_path.stat().st_size,
            "dependency_archives": dependency_stats,
            "windows_runtime": {**verified_runtime, "archive_bytes": runtime_archive.stat().st_size,
                "executable_sha256": sha256_file(python_path), "authenticode_status": signature["status"],
                "authenticode_signer": signature["signer_subject"], "extracted_file_count": extraction["file_count"],
                "expanded_bytes": extraction["expanded_bytes"]},
            "pip_bootstrap": {**WINDOWS_PIP_BOOTSTRAP, "wheel_size_bytes": pip_record["size_bytes"],
                "downloaded_bytes": pip_downloaded_bytes, "cache_hit": pip_cache_hit, "metadata_receipt": pip_receipt},
            "worker": {
                "status": result.status, "reason_code": result.reason_code, "exit_code": result.exit_code,
                "elapsed_seconds": result.elapsed_seconds, "peak_memory_bytes": result.peak_memory_bytes,
                "memory_limit_mb": 8192, "cleanup_confirmed": result.cleanup_confirmed,
                "stdout_sha256": result.stdout_sha256, "stderr_sha256": result.stderr_sha256,
                "stdout_tail": result.stdout.decode("utf-8", errors="replace")[-4096:],
                "stderr_tail": result.stderr.decode("utf-8", errors="replace")[-4096:],
                "output_truncated": result.output_truncated, "input_bytes": bundle_bytes,
                "runtime_archive_sha256": verified_runtime["archive_sha256"],
                "runtime_executable_sha256": verified_runtime["executable_sha256"],
                "network": "disabled by Windows Sandbox policy", "read_only_input": True,
                "package_manager": "pip offline --require-hashes --only-binary", "protocol_report": protocol,
            },
            "resource_observations": protocol.get("resource_observations") if isinstance(protocol, dict) else None,
        }


def _catalog_context(
    snapshot_dir: Path,
    *,
    platform: str = "linux",
    profile_lock_path: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], bytes, bytes, bytes]:
    catalog, units = load_catalog_units(snapshot_dir, (platform,), profile_lock_path=profile_lock_path)
    artifact_lock, artifact_raw = _read_json(snapshot_dir / "artifacts.lock.json")
    selected_profile_lock = Path(profile_lock_path) if profile_lock_path else snapshot_dir / "profiles.lock.json"
    profile_lock, profile_raw = _read_json(selected_profile_lock)
    catalog_raw = (snapshot_dir / "catalog.json").read_bytes()
    if not isinstance(catalog, dict) or not isinstance(units, list):
        raise NoneBotPilotError("frozen catalog snapshot did not validate")
    return (
        {item["registry_entry_id"]: item for item in catalog["entries"]},
        {item["registry_entry_id"]: item for item in units},
        {item["registry_entry_id"]: item for item in artifact_lock["entries"]},
        {item["registry_entry_id"]: item for item in profile_lock["entries"]},
        artifact_raw,
        profile_raw,
        catalog_raw,
    )


def run_nonebot_load_probe(
    snapshot_dir: Path,
    entry_id: str,
    *,
    cache_root: Path = DEFAULT_CACHE_ROOT,
    timeout_seconds: int = 1200,
    context: tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], bytes, bytes, bytes] | None = None,
    worker_labels: dict[str, Any] | None = None,
    image_digest: str | None = None,
) -> dict[str, Any]:
    """Install one exact locked NoneBot profile and verify native plugin registration offline."""
    snapshot_dir = Path(snapshot_dir).resolve(strict=True)
    cache_root = Path(cache_root).resolve(strict=True)
    entries, units, artifacts, profiles, artifact_lock_raw, profile_lock_raw, catalog_raw = context or _catalog_context(snapshot_dir)
    entry = entries.get(entry_id)
    unit = units.get(entry_id)
    artifact_entry = artifacts.get(entry_id)
    if not isinstance(entry, dict) or not isinstance(unit, dict) or entry.get("ecosystem") != "nonebot":
        raise NoneBotPilotError(f"entry is not a frozen NoneBot catalog item: {entry_id}")
    if unit.get("artifact_status") != "locked" or unit.get("profile_status") != "locked":
        raise NoneBotPilotError("NoneBot pilot entry has no locked Linux artifact and profile")
    if not isinstance(artifact_entry, dict):
        raise NoneBotPilotError("frozen artifact lock has no matching NoneBot entry")
    platform_record = next((item for item in artifact_entry.get("artifacts", []) if item.get("platform") == "linux-x86_64"), None)
    artifact = platform_record.get("artifact") if isinstance(platform_record, dict) else None
    profile_record = profiles.get(entry_id)
    profile = (profile_record or {}).get("profiles", {}).get("linux-x86_64", {})
    lock_ref = profile.get("dependency_lock")
    if not isinstance(artifact, dict) or not isinstance(lock_ref, dict) or profile.get("status") != "locked":
        raise NoneBotPilotError("frozen NoneBot artifact or dependency lock is missing")
    if profile.get("artifact_sha256") != (artifact.get("computed_digests") or {}).get("sha256"):
        raise NoneBotPilotError("NoneBot profile lock points to different direct artifact bytes")
    artifact_path = cache_root / artifact["cache_path"]
    if not artifact_path.is_file() or sha256_file(artifact_path) != (artifact.get("computed_digests") or {}).get("sha256"):
        raise NoneBotPilotError("NoneBot plugin wheel failed its frozen artifact SHA-256 check")
    lock_path = cache_root / lock_ref["cache_path"]
    lock_raw = lock_path.read_bytes()
    if len(lock_raw) != lock_ref.get("size_bytes") or _sha256(lock_raw) != lock_ref.get("sha256"):
        raise NoneBotPilotError("NoneBot dependency lock failed its frozen hash/size check")
    distribution_name = artifact_entry.get("distribution_name") or entry.get("package")
    version = entry.get("advertised_version")
    if artifact.get("build_required") or not artifact.get("filename", "").endswith(".whl"):
        raise NoneBotPilotError("S2-09 NoneBot catalog pilot only installs frozen wheel artifacts")
    if not isinstance(distribution_name, str) or not isinstance(version, str):
        raise NoneBotPilotError("NoneBot frozen entry lacks exact distribution/version identity")

    images, _ = _read_json(RUNTIME_LOCK_PATH)
    if images.get("task_id") != "S2-05" or images.get("platform") != "linux/amd64":
        raise NoneBotPilotError("pinned Linux Worker image lock has the wrong identity")
    image = image_digest or images.get("profile_images", {}).get("nonebot")
    if not isinstance(image, str) or "@sha256:" not in image:
        raise NoneBotPilotError("NoneBot Worker image must be pinned by sha256")

    entry_point = entry.get("entry_point") or entry.get("registry_identity", {}).get("module_name")
    if not isinstance(entry_point, str) or not entry_point.strip():
        raise NoneBotPilotError("NoneBot registry entry has no importable entry point")
    with tempfile.TemporaryDirectory(prefix="ligamina-pilot-nonebot-", dir=cache_root) as temporary:
        bundle = Path(temporary)
        wheelhouse = bundle / "wheelhouse"
        dependency_stats = _prepare_wheelhouse(
            lock_raw.decode("utf-8"), cache_root=cache_root, wheelhouse=wheelhouse,
            direct_artifact={"distribution_name": distribution_name, "version": version, "filename": artifact["filename"], "sha256": artifact["computed_digests"]["sha256"]},
            direct_artifact_path=artifact_path,
        )
        (bundle / "requirements.txt").write_bytes(lock_raw)
        (bundle / "spec.json").write_text(json.dumps({
            "registry_entry_id": entry_id, "package": entry.get("package"), "version": version,
            "profile_id": unit.get("profile_id"), "profile_lock_sha256": lock_ref["sha256"],
            "artifact_sha256": artifact["computed_digests"]["sha256"], "entry_point": entry_point,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        _write_runner(bundle)
        worker = DockerWorker(image_digest=image, repository_root=ROOT)
        command = ("python", "/input/runner.py")
        identity = worker_labels or {}
        if identity and not all(key in identity for key in ("campaign_id", "job_id", "attempt_id", "generation")):
            raise NoneBotPilotError("worker labels require campaign_id, job_id, attempt_id, and generation")
        result = worker.run(WorkerSpec(
            run_id=identity.get("campaign_id", "s209-nonebot-pilot"),
            entry_id=entry_id,
            command=command,
            input_dir=bundle,
            limits=WorkerLimits(timeout_seconds=timeout_seconds, memory_mb=4096, cpu_cores=2, process_limit=256),
            attempt_id=identity.get("attempt_id"),
            generation=identity.get("generation"),
            job_id=identity.get("job_id"),
        ))
        protocol = None
        for line in reversed(result.stdout.decode("utf-8", errors="replace").splitlines()):
            try:
                candidate = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict) and candidate.get("task_id") == "S2-09" and candidate.get("registry_entry_id") == entry_id:
                protocol = candidate
                break
        if result.status != "passed" or not result.cleanup_confirmed or not isinstance(protocol, dict):
            overall_status = "unknown" if result.status == "unknown" else "failed"
            overall_reason = result.reason_code or "worker_report_invalid"
        else:
            statuses = [protocol.get(key, {}).get("status") for key in ("dependency_install", "native_load", "cleanup")]
            if "failed" in statuses:
                overall_status, overall_reason = "failed", next((protocol.get(key, {}).get("reason_code") for key in ("dependency_install", "native_load", "cleanup") if protocol.get(key, {}).get("status") == "failed"), "nonebot_stage_failed")
            elif "unknown" in statuses:
                overall_status, overall_reason = "unknown", next((protocol.get(key, {}).get("reason_code") for key in ("dependency_install", "native_load", "cleanup") if protocol.get(key, {}).get("status") == "unknown"), "nonebot_stage_unknown")
            elif protocol.get("native_load", {}).get("status") == "skipped":
                overall_status, overall_reason = "skipped", protocol["native_load"].get("reason_code") or "native_load_skipped"
            elif "passed" in statuses:
                overall_status, overall_reason = "skipped", "missing_entry_bound_fixture"
            else:
                overall_status, overall_reason = "skipped", "nonebot_stages_not_reached"
        return {
            "schema_version": 1, "task_id": "S2-09", "result_id": f"catalog:{entry_id}|linux",
            "unit_type": "catalog_entry", "registry_entry_id": entry_id, "ecosystem": "nonebot",
            "package": entry.get("package"), "version": version, "platform": "linux",
            "status": overall_status, "reason_code": overall_reason, "attempted": True,
            "detail": "NoneBot locked wheel-only dependency installation and native plugin registration ran in the pinned network-disabled Linux Docker Worker; L4 behavior has no entry-bound fixture.",
            "profile_id": unit.get("profile_id"), "profile_lock_sha256": lock_ref["sha256"],
            "profile_document_sha256": _sha256(profile_lock_raw), "catalog_document_sha256": _sha256(catalog_raw),
            "artifact_lock_sha256": _sha256(artifact_lock_raw), "requirements_lock_sha256": _sha256(lock_raw),
            "artifact_sha256": artifact["computed_digests"]["sha256"], "plugin_archive_bytes": artifact_path.stat().st_size,
            "dependency_archives": dependency_stats,
            "worker": {
                "status": result.status, "reason_code": result.reason_code, "exit_code": result.exit_code,
                "elapsed_seconds": result.elapsed_seconds, "peak_memory_bytes": result.peak_memory_bytes,
                "cleanup_confirmed": result.cleanup_confirmed, "stdout_sha256": result.stdout_sha256,
                "stderr_sha256": result.stderr_sha256,
                "stdout_tail": result.stdout.decode("utf-8", errors="replace")[-4096:],
                "stderr_tail": result.stderr.decode("utf-8", errors="replace")[-4096:],
                "output_truncated": result.output_truncated, "input_bytes": sum(path.stat().st_size for path in bundle.rglob("*") if path.is_file()),
                "image_digest": image, "network": "none", "read_only_input": True,
                "build_backends": "disabled", "package_manager": "pip offline --require-hashes --only-binary",
                "protocol_report": protocol,
            },
            "resource_observations": protocol.get("resource_observations") if isinstance(protocol, dict) else None,
        }


def run_selected_nonebot_pilot(
    snapshot_dir: Path,
    manifest_path: Path,
    *,
    cache_root: Path = DEFAULT_CACHE_ROOT,
    timeout_seconds: int = 1200,
    checkpoint_path: Path | None = None,
    resume_from: Path | None = None,
) -> dict[str, Any]:
    """Attempt selected, installable NoneBot Linux entries exactly once with durable checkpoints."""
    manifest, manifest_raw = _read_json(manifest_path)
    if manifest.get("kind") != "phase2_stratified_pilot_manifest":
        raise NoneBotPilotError("S2-09 pilot manifest has the wrong identity")
    manifest_sha = manifest.get("manifest_sha256")
    payload = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if _sha256(canonical) != manifest_sha:
        raise NoneBotPilotError("S2-09 pilot manifest failed its canonical hash check")
    ledger = manifest.get("catalog_assignment_ledger")
    if not isinstance(ledger, list):
        raise NoneBotPilotError("S2-09 pilot manifest has no assignment ledger")
    selected = [row for row in ledger if row.get("selected") is True and row.get("ecosystem") == "nonebot" and row.get("platform") == "linux"]
    expected_ids = {f"catalog:{row['registry_entry_id']}|linux" for row in selected}
    if len(selected) < int(manifest.get("minimum_per_ecosystem", 30)) or len(expected_ids) != len(selected):
        raise NoneBotPilotError("NoneBot Linux pilot cohort does not meet its unique minimum denominator")
    snapshot_dir = Path(snapshot_dir).resolve(strict=True)
    cache_root = Path(cache_root).resolve(strict=True)
    context = _catalog_context(snapshot_dir)
    entries, units, artifacts, profiles, artifact_lock_raw, profile_lock_raw, catalog_raw = context
    results: list[dict[str, Any]] = []
    attempt_journal: list[dict[str, Any]] = []
    active_attempt: dict[str, Any] | None = None
    resume_sha = None
    if resume_from:
        prior, prior_raw = _read_json(resume_from)
        if prior.get("task_id") != "S2-09" or prior.get("kind") != "phase2_s209_nonebot_linux_pilot_workers":
            raise NoneBotPilotError("NoneBot resume checkpoint has the wrong identity")
        if prior.get("pilot_manifest_sha256") != manifest_sha or prior.get("selected_nonebot_linux_entries") != len(selected):
            raise NoneBotPilotError("NoneBot resume checkpoint does not match this manifest/cohort")
        prior_results = prior.get("results")
        prior_journal = prior.get("attempt_journal", [])
        prior_active = prior.get("active_attempt")
        if not isinstance(prior_results, list):
            raise NoneBotPilotError("NoneBot resume checkpoint has no result list")
        if not isinstance(prior_journal, list) or any(not isinstance(item, dict) for item in prior_journal):
            raise NoneBotPilotError("NoneBot resume checkpoint has an invalid attempt journal")
        prior_ids = [item.get("result_id") for item in prior_results if isinstance(item, dict)]
        if len(prior_ids) != len(prior_results) or len(prior_ids) != len(set(prior_ids)) or not set(prior_ids).issubset(expected_ids):
            raise NoneBotPilotError("NoneBot resume checkpoint contains duplicate/unexpected results")
        if any(item.get("status") not in {"passed", "failed", "skipped", "unknown"} for item in prior_results):
            raise NoneBotPilotError("NoneBot resume checkpoint contains a non-terminal result")
        if any(item.get("ecosystem") != "nonebot" or item.get("platform") != "linux" for item in prior_results):
            raise NoneBotPilotError("NoneBot resume checkpoint contains a mismatched result")
        results.extend(prior_results)
        attempt_journal.extend(prior_journal)
        if prior_active is not None:
            if (not isinstance(prior_active, dict) or prior_active.get("result_id") not in expected_ids
                    or prior_active.get("status") != "running" or prior_active.get("result_id") in prior_ids):
                raise NoneBotPilotError("NoneBot resume checkpoint has an invalid in-flight attempt")
            attempt_journal.append({
                **prior_active,
                "status": "unknown",
                "reason_code": "worker_lost_after_restart",
                "ended_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "recovered_on_resume": True,
            })
        resume_sha = _sha256(prior_raw)
    completed = {item["result_id"] for item in results}

    def report(final: bool) -> dict[str, Any]:
        ids = [item["result_id"] for item in results]
        counts = {status: sum(item.get("status") == status for item in results) for status in ("passed", "failed", "skipped", "unknown")}
        closed = len(results) == len(selected) and len(set(ids)) == len(selected) and set(ids) == expected_ids
        value = {
            "schema_version": 1, "date": "2026-10-12", "task_id": "S2-09",
            "kind": "phase2_s209_nonebot_linux_pilot_workers",
            "status": ("partial" if counts["failed"] or counts["unknown"] or counts["skipped"] else "completed") if final else "in-progress",
            "snapshot_id": manifest.get("snapshot_id"), "pilot_manifest_sha256": manifest_sha,
            "pilot_manifest_raw_sha256": _sha256(manifest_raw), "selected_nonebot_linux_entries": len(selected),
            "terminal_results": len(results), "closed_exactly_once": closed, "counts": counts,
            "results": results, "attempt_journal": attempt_journal, "active_attempt": active_attempt,
            "resume_checkpoint_sha256": resume_sha,
            "execution_scope": {
                "worker_os": "Linux Docker", "network": "none", "plugin_install_scripts": "not applicable; wheel-only install",
                "build_backends": "disabled", "selected_entry_behavior_fixtures": "missing; no L4 dispatch",
                "windows_nonebot": "blocked before CPython start by App Control Win32 4551",
                "profile_image": "digest pinned in lab/baselines/runtime-images.lock.json",
            },
        }
        if final and not closed:
            raise NoneBotPilotError("NoneBot Linux pilot does not close its selected denominator exactly once")
        return value

    def save_checkpoint() -> None:
        if checkpoint_path is None:
            return
        path = Path(checkpoint_path).expanduser().absolute()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(report(False), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)

    save_checkpoint()
    for row in sorted(selected, key=lambda item: item["registry_entry_id"]):
        entry_id = row["registry_entry_id"]
        result_id = f"catalog:{entry_id}|linux"
        if result_id in completed:
            continue
        attempt_no = 1 + sum(item.get("result_id") == result_id for item in attempt_journal)
        active_attempt = {
            "result_id": result_id,
            "registry_entry_id": entry_id,
            "ecosystem": "nonebot",
            "platform": "linux",
            "attempt": attempt_no,
            "status": "running",
            "stage": "prepare_and_worker",
            "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        save_checkpoint()
        entry = entries[entry_id]
        unit = units[entry_id]
        source = entry.get("source_record", {})
        if source.get("valid") is False:
            result = {"result_id": result_id, "registry_entry_id": entry_id, "ecosystem": "nonebot", "package": row.get("package"), "version": row.get("version"), "platform": "linux", "status": "skipped", "reason_code": "catalog_entry_invalid", "attempted": False, "stage": "prepare", "detail": "The frozen NoneBot registry entry is explicitly invalid."}
        elif row.get("artifact_status") != "locked" or row.get("profile_status") != "locked":
            result = {"result_id": result_id, "registry_entry_id": entry_id, "ecosystem": "nonebot", "package": row.get("package"), "version": row.get("version"), "platform": "linux", "status": "skipped", "reason_code": row.get("profile_reason_code") or row.get("artifact_reason_code") or "profile_or_artifact_unavailable", "attempted": False, "stage": "prepare"}
        else:
            try:
                result = run_nonebot_load_probe(snapshot_dir, entry_id, cache_root=cache_root, timeout_seconds=timeout_seconds, context=context)
            except (NoneBotPilotError, PilotWorkerError, OSError, RuntimeError, ValueError, KeyError, StopIteration) as error:
                result = {"schema_version": 1, "task_id": "S2-09", "result_id": result_id, "registry_entry_id": entry_id, "ecosystem": "nonebot", "package": row.get("package"), "version": row.get("version"), "platform": "linux", "status": "unknown", "reason_code": "worker_preparation_failed", "attempted": False, "stage": "prepare", "detail": str(error)}
        result["attempt"] = attempt_no
        attempt_journal.append({
            "result_id": result_id,
            "registry_entry_id": entry_id,
            "ecosystem": "nonebot",
            "platform": "linux",
            "attempt": attempt_no,
            "status": result.get("status", "unknown"),
            "reason_code": result.get("reason_code"),
            "stage": result.get("stage", "worker"),
            "started_at": active_attempt["started_at"],
            "ended_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        active_attempt = None
        results.append(result)
        completed.add(result_id)
        save_checkpoint()
    return report(True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--checkpoint-report", type=Path)
    parser.add_argument("--resume-from", type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=1200)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    options = parse_args(argv)
    checkpoint = options.checkpoint_report or options.report.with_name(options.report.stem + ".checkpoint.json")
    try:
        result = run_selected_nonebot_pilot(
            options.snapshot, options.manifest, cache_root=options.cache_root, timeout_seconds=options.timeout_seconds,
            checkpoint_path=checkpoint, resume_from=options.resume_from,
        )
        options.report.parent.mkdir(parents=True, exist_ok=True)
        options.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (NoneBotPilotError, PilotWorkerError, OSError, RuntimeError, ValueError, KeyError) as error:
        print(f"NoneBot pilot worker failed: {error}", file=__import__("sys").stderr)
        return 2
    print(json.dumps({"report": str(options.report), "status": result["status"], "terminal_results": result["terminal_results"], "counts": result["counts"], "closed_exactly_once": result["closed_exactly_once"]}, ensure_ascii=False))
    return 0 if result["closed_exactly_once"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
