from __future__ import annotations

"""Diagnose the locked NoneBot native dependency in Windows Sandbox without loading a catalog plugin."""

import hashlib
import json
import os
import pathlib
import shutil
import sys
import tempfile
import textwrap
import zipfile
from datetime import datetime, timezone
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
CACHE_ROOT = pathlib.Path(r"E:\stella\Ligamina-artifact-cache")
SOURCE_CAMPAIGN = "p3-recovery-windows-pilot-v2-9666ebb4609f"
SOURCE_MANIFEST_SHA256 = "0796fad72d14f0e84cfd91b77e63177873a26510dfa3adee55c9a3298feed4fa"
SOURCE_MANIFEST_FILE_SHA256 = "c28dcdddc035df73841e42a811abd4b4fd4c89c6fd795b783ea0214dcf9f21d5"
SOURCE_PROFILE_LOCK_SHA256 = "e58651dfa5e46b3ebc8590b7ba89fd7aef86e8dd42d47580c9ed7fb536d880df"
SOURCE_EXECUTION_SHA256 = "5e1f02f68e65faecf8048a5fc722cf613dda33b55792ae1e0e09a6a35d95d976"
ENTRY_ID = "nb-0051899c326ef8ed00eff8ab"
PROFILE_DEPENDENCY_LOCK_SHA256 = "ee98bc178065b3dd364586832105b38feec6ad46a63dac2857f1038aacaf3ece"
PROFILE_LOCK_PATH = ROOT / "outputs/acceptance/phase3-p305-windows-python31210-profile-lock-pilot-v3-20261025.json"
MANIFEST_PATH = ROOT / "outputs/acceptance/phase3-p305-windows-pilot-manifest-v2-20261025.json"
SOURCE_REPORT_PATH = ROOT / "outputs/acceptance/phase3-p305-windows-pilot-v2-execution-20261025.json"
RUNTIME_ADMISSION_PATH = ROOT / "outputs/acceptance/phase3-p304-windows-admission-python31210-20261025.json"
OUTPUT_PATH = ROOT / "outputs/acceptance/phase3-p305-windows-native-dll-diagnostic-20261025.json"
RUNTIME_ARCHIVE_SHA256 = "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3"
PYTHON_EXECUTABLE_SHA256 = "4d6f5f81a4bca11191c4c7c6b43632694d0a4ce74e068619d8fdc161d469859a"


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_identity(path: pathlib.Path, expected: str, label: str) -> bytes:
    raw = path.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected:
        raise RuntimeError(f"{label} SHA-256 mismatch: {actual}")
    return raw


def cached_metadata_exists(package: str, version: str) -> bool:
    url = f"https://pypi.org/pypi/{package}/{version}/json"
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return (CACHE_ROOT / "pypi-wheel-metadata" / "sha256" / key[:2] / key).is_file()


def extract_wheel(wheel: pathlib.Path, destination: pathlib.Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(wheel) as archive:
        for info in archive.infolist():
            relative = pathlib.PurePosixPath(info.filename)
            if (
                not info.filename
                or "\\" in info.filename
                or relative.is_absolute()
                or any(part in {"", ".", ".."} or ":" in part for part in relative.parts)
            ):
                raise RuntimeError(f"unsafe wheel path: {info.filename!r}")
            target = destination.joinpath(*relative.parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)


def guest_driver_sources() -> tuple[str, str, str]:
    powershell = r"""$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch {}
$inputRoot = 'C:\Ligamina\Input'
$work = 'C:\Ligamina\Workspace\p305-native-dll-diagnostic'
$runtime = Join-Path $work 'runtime'
$site = Join-Path $work 'site-packages'
New-Item -ItemType Directory -Path $work -Force | Out-Null
New-Item -ItemType Directory -Path $site -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $inputRoot 'runtime') -Destination $runtime -Recurse -Force
$python = Join-Path $runtime 'python.exe'
$pth = Join-Path $runtime 'python312._pth'
if (-not (Test-Path -LiteralPath $python) -or -not (Test-Path -LiteralPath $pth)) { throw 'admitted CPython runtime layout is incomplete' }
@('python312.zip', '.', $site, 'import site') | Set-Content -LiteralPath $pth -Encoding ascii
$env:PYTHONNOUSERSITE = '1'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PIP_NO_INDEX = '1'
$env:PIP_DISABLE_PIP_VERSION_CHECK = '1'
$env:PIP_NO_INPUT = '1'
$output = & $python (Join-Path $inputRoot 'diagnostic_driver.py') $inputRoot $work 2>&1
$exitCode = $LASTEXITCODE
$output | ForEach-Object { Write-Output $_ }
exit $exitCode
"""
    driver = r'''from __future__ import annotations
import hashlib, json, shutil, subprocess, sys, zipfile
from pathlib import Path

root = Path(sys.argv[1]); work = Path(sys.argv[2]); python = work / "runtime" / "python.exe"
site = work / "site-packages"; wheelhouse = root / "wheelhouse"
pip_wheel = root / "pip-bootstrap.whl"; expected_pip = json.loads((root / "identity.json").read_text(encoding="utf-8"))["pip_sha256"]
def extract(path):
  with zipfile.ZipFile(path) as archive:
    for info in archive.infolist():
      rel = Path(*info.filename.split("/"))
      if not info.filename or "\\" in info.filename or rel.is_absolute() or any(p in {"", ".", ".."} or ":" in p for p in rel.parts):
        raise RuntimeError("unsafe wheel path")
      target = site / rel
      if info.is_dir(): target.mkdir(parents=True, exist_ok=True)
      else:
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(info) as src, target.open("xb") as dst: shutil.copyfileobj(src, dst, 1024*1024)
if hashlib.sha256(pip_wheel.read_bytes()).hexdigest() != expected_pip: raise RuntimeError("pip bootstrap hash mismatch")
extract(pip_wheel)
env = dict(__import__("os").environ); env.update({"PYTHONUTF8":"1", "PYTHONIOENCODING":"utf-8", "PYTHONNOUSERSITE":"1", "PIP_NO_INDEX":"1", "PIP_DISABLE_PIP_VERSION_CHECK":"1", "PIP_NO_INPUT":"1"})
install = subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "--no-cache-dir", "--no-index", "--find-links", str(wheelhouse), "--only-binary=:all:", "--require-hashes", "-r", str(root / "requirements.txt"), "--target", str(site)], capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=300, check=False)
report = {"task_id":"p305-windows-native-dll-diagnostic", "dependency_install":{"status":"passed" if install.returncode == 0 else "failed", "exit_code":install.returncode, "stdout_tail":install.stdout[-3000:], "stderr_tail":install.stderr[-3000:]}, "native_diagnostic":None}
if install.returncode == 0:
  probe = subprocess.run([str(python), str(root / "native_diagnostic.py"), str(work)], capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=60, check=False)
  report["native_diagnostic"] = {"exit_code":probe.returncode, "stdout_tail":probe.stdout[-6000:], "stderr_tail":probe.stderr[-3000:]}
print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
'''
    native = r'''from __future__ import annotations
import ctypes, hashlib, importlib, importlib.util, json, os, pathlib, sys
work = pathlib.Path(sys.argv[1]); runtime = pathlib.Path(sys.executable).resolve().parent
result = {"python_version":sys.version.split()[0], "python_executable":str(pathlib.Path(sys.executable).resolve()), "runtime_crt_files":[], "extension":None, "controls":{}, "pydantic_core_import":None}
for name in ("vcruntime140.dll", "vcruntime140_1.dll"):
  p = runtime / name
  result["runtime_crt_files"].append({"name":name,"present":p.is_file(),"sha256":hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None})
try:
  dll_cookie = os.add_dll_directory(str(runtime))
except Exception as exc:
  result["add_dll_directory_error"] = type(exc).__name__ + ": " + str(exc)
for name in ("_ctypes", "_ssl"):
  try:
    importlib.import_module(name); result["controls"][name] = {"loaded":True}
  except BaseException as exc:
    result["controls"][name] = {"loaded":False,"type":type(exc).__name__,"message":str(exc)}
try:
  ctypes.WinDLL("bcryptprimitives.dll", use_last_error=True); result["controls"]["bcryptprimitives.dll"]={"loaded":True}
except OSError as exc:
  result["controls"]["bcryptprimitives.dll"]={"loaded":False,"winerror":exc.winerror,"errno":exc.errno,"message":str(exc)}
try:
  spec = importlib.util.find_spec("pydantic_core")
  package = pathlib.Path(spec.origin).parent if spec and spec.origin else None
  files = sorted(package.glob("_pydantic_core*.pyd")) if package else []
  result["extension"]=[{"path":str(p),"size":p.stat().st_size,"sha256":hashlib.sha256(p.read_bytes()).hexdigest()} for p in files]
  try:
    import pydantic_core
    result["pydantic_core_import"]={"loaded":True,"version":pydantic_core.__version__}
  except BaseException as exc:
    result["pydantic_core_import"]={"loaded":False,"type":type(exc).__name__,"message":str(exc)}
  if files:
    try:
      ctypes.WinDLL(str(files[0]), use_last_error=True)
      result["direct_loadlibrary"]={"loaded":True}
    except OSError as exc:
      result["direct_loadlibrary"]={"loaded":False,"winerror":exc.winerror,"errno":exc.errno,"message":str(exc)}
except BaseException as exc:
  result["diagnostic_error"] = type(exc).__name__ + ": " + str(exc)
print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
'''
    return powershell, driver, native


def main() -> int:
    sys.path.insert(0, str(ROOT))
    from lab.pilot_nonebot import (
        WINDOWS_PIP_BOOTSTRAP,
        _download_wheel,
        _load_release_metadata,
        _read_lock_requirements,
        _select_locked_wheel,
        _extract_checked_zip,
    )
    from lab.workers import WindowsSandboxWorker, WorkerLimits, WorkerSpec

    manifest_raw = verify_identity(MANIFEST_PATH, SOURCE_MANIFEST_FILE_SHA256, "pilot manifest file")
    manifest = json.loads(manifest_raw)
    if manifest.get("manifest_sha256") != SOURCE_MANIFEST_SHA256 or manifest.get("campaign_id") != SOURCE_CAMPAIGN:
        raise RuntimeError("pilot manifest canonical identity mismatch")
    verify_identity(PROFILE_LOCK_PATH, SOURCE_PROFILE_LOCK_SHA256, "pilot profile lock")
    source_report_raw = verify_identity(SOURCE_REPORT_PATH, SOURCE_EXECUTION_SHA256, "pilot execution report")
    source_report = json.loads(source_report_raw)
    if source_report.get("manifest_sha256") != SOURCE_MANIFEST_SHA256:
        raise RuntimeError("pilot execution report references a different manifest")
    admission = json.loads(RUNTIME_ADMISSION_PATH.read_text(encoding="utf-8"))
    cache_lock = CACHE_ROOT / "profile-locks/nonebot/sha256/ee" / PROFILE_DEPENDENCY_LOCK_SHA256
    lock_raw = cache_lock.read_bytes()
    if hashlib.sha256(lock_raw).hexdigest() != PROFILE_DEPENDENCY_LOCK_SHA256:
        raise RuntimeError("profile dependency-lock hash mismatch")
    requirements = _read_lock_requirements(lock_raw.decode("utf-8"))
    selected = []
    for package in ("pydantic-core", "typing-extensions"):
        req = next(item for item in requirements if item["canonical_name"] == package)
        if not cached_metadata_exists(req["name"], req["version"]):
            raise RuntimeError(f"frozen PyPI metadata cache is missing for {package}=={req['version']}")
        metadata, receipt = _load_release_metadata(req["name"], req["version"], CACHE_ROOT)
        record = _select_locked_wheel(req, metadata, platform_id="windows-x86_64", python_version="3.12.10")
        wheel_path = CACHE_ROOT / "wheel-cache/sha256" / record["sha256"][:2] / record["sha256"]
        if not wheel_path.is_file() or sha256_file(wheel_path) != record["sha256"] or wheel_path.stat().st_size != record["size_bytes"]:
            raise RuntimeError(f"hash-locked wheel cache is unavailable or corrupt for {package}")
        selected.append({"requirement": req, "record": record, "path": wheel_path, "metadata": receipt})

    runtime_hash_path = CACHE_ROOT / "windows-runtime-cache/sha256" / RUNTIME_ARCHIVE_SHA256[:2] / RUNTIME_ARCHIVE_SHA256
    if not runtime_hash_path.is_file() or sha256_file(runtime_hash_path) != RUNTIME_ARCHIVE_SHA256:
        raise RuntimeError("admitted Windows runtime archive is not present in its hash-verified cache")

    temp_root = CACHE_ROOT / "p305-native-dll-diagnostic-temp"
    worker_temp = CACHE_ROOT / "p305-native-dll-diagnostic-worker-temp"
    temp_root.mkdir(parents=True, exist_ok=True)
    worker_temp.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="p305-native-dll-diag-", dir=temp_root) as temporary:
        bundle = pathlib.Path(temporary)
        runtime_dir = bundle / "runtime"
        extraction = _extract_checked_zip(runtime_hash_path, runtime_dir, max_expanded_bytes=64 * 1024 * 1024)
        python_path = runtime_dir / "python.exe"
        if not python_path.is_file() or sha256_file(python_path) != PYTHON_EXECUTABLE_SHA256:
            raise RuntimeError("admitted runtime executable hash mismatch")
        wheelhouse = bundle / "wheelhouse"
        wheelhouse.mkdir()
        for item in selected:
            shutil.copyfile(item["path"], wheelhouse / item["record"]["filename"])
            if sha256_file(wheelhouse / item["record"]["filename"]) != item["record"]["sha256"]:
                raise RuntimeError("staged dependency wheel hash mismatch")

        pip_req = {
            "name": WINDOWS_PIP_BOOTSTRAP["filename"].split("-")[0],
            "canonical_name": "pip",
            "version": WINDOWS_PIP_BOOTSTRAP["version"],
            "hashes": {WINDOWS_PIP_BOOTSTRAP["sha256"]},
        }
        pip_meta_path = CACHE_ROOT / "pypi-wheel-metadata/sha256" / hashlib.sha256(WINDOWS_PIP_BOOTSTRAP["metadata_url"].encode()).hexdigest()[:2] / hashlib.sha256(WINDOWS_PIP_BOOTSTRAP["metadata_url"].encode()).hexdigest()
        if not pip_meta_path.is_file():
            raise RuntimeError("frozen pip release metadata cache is missing")
        pip_meta, _ = _load_release_metadata("pip", WINDOWS_PIP_BOOTSTRAP["version"], CACHE_ROOT)
        pip_record = _select_locked_wheel(pip_req, pip_meta, platform_id="windows-x86_64", python_version="3.12.10", prefer_sha256=WINDOWS_PIP_BOOTSTRAP["sha256"])
        pip_path = CACHE_ROOT / "wheel-cache/sha256" / pip_record["sha256"][:2] / pip_record["sha256"]
        if not pip_path.is_file() or sha256_file(pip_path) != WINDOWS_PIP_BOOTSTRAP["sha256"]:
            raise RuntimeError("frozen pip bootstrap wheel cache is missing or corrupt")
        shutil.copyfile(pip_path, bundle / "pip-bootstrap.whl")
        requirements_text = "\n".join(
            f"{item['requirement']['name']}=={item['requirement']['version']} --hash=sha256:{item['record']['sha256']}"
            for item in selected
        ) + "\n"
        (bundle / "requirements.txt").write_text(requirements_text, encoding="utf-8", newline="\n")
        (bundle / "identity.json").write_text(json.dumps({"pip_sha256": WINDOWS_PIP_BOOTSTRAP["sha256"]}), encoding="utf-8")
        powershell, driver, native = guest_driver_sources()
        (bundle / "run-diagnostic.ps1").write_text(powershell, encoding="utf-8", newline="\n")
        (bundle / "diagnostic_driver.py").write_text(driver, encoding="utf-8", newline="\n")
        (bundle / "native_diagnostic.py").write_text(native, encoding="utf-8", newline="\n")

        worker = WindowsSandboxWorker(repository_root=ROOT, temporary_root=worker_temp)
        worker_result = worker.run(WorkerSpec(
            run_id="p305-windows-native-dll-diagnostic-20261025",
            entry_id="diagnostic:pydantic-core-2.50.1",
            command=(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", r"C:\Ligamina\Input\run-diagnostic.ps1"),
            input_dir=bundle,
            limits=WorkerLimits(timeout_seconds=600, memory_mb=4096, cpu_cores=1, process_limit=64),
        ))
        stdout = worker_result.stdout.decode("utf-8", errors="replace")
        guest_report: dict[str, Any] | None = None
        for line in reversed(stdout.splitlines()):
            try:
                candidate = json.loads(line)
            except json.JSONDecodeError:
                continue
            if candidate.get("task_id") == "p305-windows-native-dll-diagnostic":
                guest_report = candidate
                break
        extension_sha = None
        try:
            with zipfile.ZipFile(selected[0]["path"]) as archive:
                extension_name = next(name for name in archive.namelist() if name.endswith(".pyd"))
                extension_raw = archive.read(extension_name)
                extension_sha = hashlib.sha256(extension_raw).hexdigest()
        except (StopIteration, zipfile.BadZipFile):
            extension_name = None

        report = {
            "schema_version": 1,
            "kind": "phase3_p305_windows_native_dll_diagnostic",
            "date": "2026-10-25",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "task_id": "P3-05 diagnostic-only; no catalog unit executed",
            "source": {
                "campaign_id": SOURCE_CAMPAIGN,
                "manifest_sha256": SOURCE_MANIFEST_SHA256,
                "manifest_file_sha256": SOURCE_MANIFEST_FILE_SHA256,
                "execution_report_sha256": hashlib.sha256(source_report_raw).hexdigest(),
                "profile_lock_sha256": SOURCE_PROFILE_LOCK_SHA256,
                "dependency_lock_sha256": PROFILE_DEPENDENCY_LOCK_SHA256,
                "runtime_admission_report_sha256": hashlib.sha256(RUNTIME_ADMISSION_PATH.read_bytes()).hexdigest(),
            },
            "diagnostic_scope": {
                "catalog_plugin_execution": False,
                "catalog_unit_outcome_changed": False,
                "packages_installed": [{"name": item["requirement"]["name"], "version": item["requirement"]["version"], "wheel_filename": item["record"]["filename"], "wheel_sha256": item["record"]["sha256"], "wheel_size_bytes": item["record"]["size_bytes"]} for item in selected],
                "pydantic_core_extension": {"filename": extension_name, "sha256": extension_sha},
                "runtime_archive_sha256": RUNTIME_ARCHIVE_SHA256,
                "python_executable_sha256": PYTHON_EXECUTABLE_SHA256,
                "runtime_extracted_file_count": extraction.get("file_count"),
            },
            "worker": {
                "status": worker_result.status,
                "reason_code": worker_result.reason_code,
                "exit_code": worker_result.exit_code,
                "cleanup_confirmed": worker_result.cleanup_confirmed,
                "elapsed_seconds": worker_result.elapsed_seconds,
                "stdout_sha256": worker_result.stdout_sha256,
                "stderr_sha256": worker_result.stderr_sha256,
                "stdout_tail": stdout[-8000:],
                "stderr_tail": worker_result.stderr.decode("utf-8", errors="replace")[-4000:],
            },
            "guest_report": guest_report,
            "interpretation": "Diagnostic only. The existing ten NoneBot pilot outcomes remain unknown; this report does not revise the sealed campaign.",
        }
        OUTPUT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (OUTPUT_PATH.with_suffix(OUTPUT_PATH.suffix + ".sha256")).write_text(f"{sha256_file(OUTPUT_PATH)}  {OUTPUT_PATH.name}\n", encoding="ascii")
        print(json.dumps({"report": str(OUTPUT_PATH), "report_sha256": sha256_file(OUTPUT_PATH), "worker_status": worker_result.status, "cleanup_confirmed": worker_result.cleanup_confirmed, "guest_report": guest_report}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"diagnostic failed before report: {type(error).__name__}: {error}", file=sys.stderr)
        raise
