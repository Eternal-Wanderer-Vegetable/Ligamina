"""Install and exercise locked third-party plugins in disposable Host profiles."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
if sys.path and Path(sys.path[0]).resolve() == SCRIPT_DIR:
    sys.path.pop(0)
import queue

HOST_VERSIONS = {
    "nonebot": {"nonebot2[fastapi]": "2.5.0", "nonebot-adapter-onebot": "2.4.6", "jsonschema": "4.25.1"},
    "koishi": {"@koishijs/core": "4.18.11", "@minatojs/driver-sqlite": "4.7.0"},
}
ECOSYSTEMS = ("nonebot", "koishi")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
SRI_SHA512_RE = re.compile(r"^sha512-[A-Za-z0-9+/]{86}==$")
SAMPLE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
PACKAGE_RE = re.compile(r"^(?:@[a-z0-9._-]+/)?[a-zA-Z0-9._-]+$")
CORE_CAPABILITY = "ligamina.nonebot.math.add"
ACCOUNT_ID = "9000"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def capture_version(command: list[str]) -> str | None:
    if os.name == "nt" and command and command[0] == "npm":
        command = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", "npm.cmd", *command[1:]]
    try:
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    return (result.stdout or result.stderr).strip().splitlines()[0]


def preserve_profile_artifacts(sample: dict[str, Any], work: Path, report_path: Path) -> dict[str, Any]:
    names = ["pyproject.toml", "uv.lock"] if sample["ecosystem"] == "nonebot" else ["package.json", "package-lock.json"]
    if sample["ecosystem"] == "nonebot" and (work / ".env.prod").is_file():
        names.append(".env.prod")
    destination_root = report_path.resolve().parent / f"{report_path.stem}-locks" / sample["sample_id"]
    artifacts = []
    for name in names:
        source = work / name
        if not source.is_file():
            continue
        destination = destination_root / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        artifacts.append({
            "path": destination.relative_to(ROOT).as_posix(),
            "sha256": digest(destination),
            "size_bytes": destination.stat().st_size,
        })
    expected_names = set(names)
    copied_names = {Path(item["path"]).name for item in artifacts}
    return {
        "status": "passed" if copied_names == expected_names else "incomplete",
        "artifacts": artifacts,
    }


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def validate_manifest(path: Path, selected: set[str] | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("manifest schema_version must be 1")
    samples = manifest.get("samples")
    if not isinstance(samples, list) or not samples:
        raise ValueError("manifest samples must be a non-empty array")

    ids: set[str] = set()
    coordinates: set[tuple[str, str, str]] = set()
    ecosystems = {"nonebot": 0, "koishi": 0}
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(samples):
        prefix = f"samples[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{prefix} must be an object")
        sample_id = item.get("sample_id")
        ecosystem = item.get("ecosystem")
        package = item.get("package")
        version = item.get("version")
        artifact_url = item.get("artifact_url")
        artifact_hash = item.get("artifact_hash", item.get("artifact_sha256"))
        artifact_hash_algorithm = item.get("artifact_hash_algorithm", "sha256")
        source_url = item.get("source_url")
        license_name = item.get("license")
        categories = item.get("category")
        entry = item.get("entry_module") if ecosystem == "nonebot" else item.get("entry_point")
        missing = [
            key for key, value in (
                ("sample_id", sample_id), ("ecosystem", ecosystem), ("package", package),
                ("version", version), ("artifact_url", artifact_url),
                ("artifact_hash", artifact_hash), ("artifact_hash_algorithm", artifact_hash_algorithm), ("source_url", source_url),
                ("license", license_name), ("category", categories),
                (("entry_module" if ecosystem == "nonebot" else "entry_point"), entry),
            ) if value in (None, "", [])
        ]
        if missing:
            raise ValueError(f"{prefix} is missing required fields: {', '.join(missing)}")
        if ecosystem not in ecosystems:
            raise ValueError(f"{prefix}.ecosystem must be nonebot or koishi")
        if not isinstance(sample_id, str) or not SAMPLE_ID_RE.fullmatch(sample_id):
            raise ValueError(f"{prefix}.sample_id is not a safe lower-case ID")
        if sample_id in ids:
            raise ValueError(f"duplicate sample_id: {sample_id}")
        ids.add(sample_id)
        if not all(isinstance(value, str) and value.strip() for value in (package, version, artifact_url, source_url, license_name, entry)):
            raise ValueError(f"{prefix} string fields must be non-empty")
        if not PACKAGE_RE.fullmatch(package):
            raise ValueError(f"{prefix}.package is not a safe package name")
        if not artifact_url.startswith("https://") or not source_url.startswith(("https://", "http://")):
            raise ValueError(f"{prefix} source and artifact URLs must use HTTP(S)")
        if artifact_hash_algorithm == "sha256":
            valid_hash = isinstance(artifact_hash, str) and bool(HASH_RE.fullmatch(artifact_hash))
        elif artifact_hash_algorithm == "sri-sha512":
            valid_hash = isinstance(artifact_hash, str) and bool(SRI_SHA512_RE.fullmatch(artifact_hash))
        else:
            valid_hash = False
        if not valid_hash:
            raise ValueError(f"{prefix} artifact hash must match sha256 hex or npm sha512 SRI")
        if not isinstance(categories, list) or not categories or not all(isinstance(value, str) and value for value in categories):
            raise ValueError(f"{prefix}.category must be a non-empty string array")
        preload_plugins = item.get("preload_plugins", [])
        if not isinstance(preload_plugins, list) or not all(
            isinstance(value, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", value)
            for value in preload_plugins
        ):
            raise ValueError(f"{prefix}.preload_plugins must be an array of importable plugin module names")
        if ecosystem != "nonebot" and preload_plugins:
            raise ValueError(f"{prefix}.preload_plugins is only supported for NoneBot samples")
        if len(set(preload_plugins)) != len(preload_plugins):
            raise ValueError(f"{prefix}.preload_plugins cannot contain duplicates")
        coordinate = (ecosystem, package.casefold(), version)
        if coordinate in coordinates:
            raise ValueError(f"duplicate ecosystem/package/version sample: {coordinate}")
        coordinates.add(coordinate)
        ecosystems[ecosystem] += 1
        if ecosystem == "nonebot" and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", entry):
            raise ValueError(f"{prefix}.entry_module must be an importable module name")
        item = dict(item)
        item["_entry"] = entry
        item["preload_plugins"] = preload_plugins
        item["_artifact_hash"] = artifact_hash
        item["_artifact_hash_algorithm"] = artifact_hash_algorithm
        normalized.append(item)

    if len(normalized) < 20 or any(count < 10 for count in ecosystems.values()):
        raise ValueError(
            "representative manifest requires at least 20 unique samples, including 10 per ecosystem "
            f"(found {len(normalized)} total, {ecosystems})"
        )
    if selected:
        unknown = selected - ids
        if unknown:
            raise ValueError(f"unknown sample IDs: {', '.join(sorted(unknown))}")
        normalized = [item for item in normalized if item["sample_id"] in selected]
    return manifest, normalized


def download_and_verify(sample: dict[str, Any], destination: Path) -> dict[str, Any]:
    request = urllib.request.Request(
        sample["artifact_url"], headers={"User-Agent": "Ligamina-representative-runner/0.1"}
    )
    hasher = hashlib.sha256()
    size = 0
    try:
        with urllib.request.urlopen(request, timeout=90) as response, destination.open("wb") as output:
            final_url = response.geturl()
            if not final_url.startswith("https://"):
                raise ValueError("artifact redirected to a non-HTTPS URL")
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > 512 * 1024 * 1024:
                    raise ValueError("artifact exceeds 512 MiB safety limit")
                hasher.update(chunk)
                output.write(chunk)
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return {"status": "failed", "error": f"artifact_download_failed: {error}"}
    actual_sha256 = hasher.hexdigest()
    if sample["_artifact_hash_algorithm"] == "sha256":
        actual_hash = actual_sha256
    else:
        import base64

        actual_hash = "sha512-" + base64.b64encode(_file_digest(destination, "sha512")).decode("ascii")
    return {
        "status": "passed" if actual_hash == sample["_artifact_hash"] else "failed",
        "artifact_url": sample["artifact_url"],
        "artifact_hash_algorithm": sample["_artifact_hash_algorithm"],
        "expected_artifact_hash": sample["_artifact_hash"],
        "actual_artifact_hash": actual_hash,
        "actual_sha256": actual_sha256,
        "size_bytes": size,
        "error": None if actual_hash == sample["_artifact_hash"] else "artifact_hash_mismatch",
    }


def discover_python_entry(sample: dict[str, Any], artifact: Path) -> str:
    entry = sample["_entry"]
    if entry != "discover":
        return entry
    with zipfile.ZipFile(artifact) as wheel:
        top_levels = [
            line.strip() for name in wheel.namelist() if name.endswith(".dist-info/top_level.txt")
            for line in wheel.read(name).decode("utf-8", errors="replace").splitlines() if line.strip()
        ]
        modules = sorted(set(top_levels))
        if not modules:
            distribution = re.sub(r"[-_.]+", "_", sample["package"]).casefold()
            roots = {
                name.split("/", 1)[0]
                for name in wheel.namelist()
                if "/" in name and not name.split("/", 1)[0].endswith((".dist-info", ".data"))
            }
            modules = sorted(
                root for root in roots
                if root.casefold() == distribution
                or root.casefold().startswith("nonebot_plugin_")
                or root.casefold().startswith("nonebotplugin")
            )
            modules.extend(sorted(
                name[:-3] for name in wheel.namelist()
                if "/" not in name and name.endswith(".py") and not name.startswith("_")
                and name[:-3].casefold() == distribution
            ))
            modules = sorted(set(modules))
        if len(modules) != 1:
            raise ValueError(f"cannot unambiguously discover one plugin module from wheel: {modules}")
        module = modules[0]
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", module):
            raise ValueError(f"wheel top_level entry is not a Python module name: {module!r}")
        return module


def _file_digest(path: Path, algorithm: str) -> bytes:
    hasher = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.digest()


def isolated_environment(work: Path, cache_root: Path | None = None) -> dict[str, str]:
    """Give package installers and plugin code a fresh home and no inherited credentials."""
    home = work / "isolated-home"
    temp = work / "isolated-tmp"
    cache = cache_root or work / "isolated-cache"
    for path in (home, temp, cache):
        path.mkdir(parents=True, exist_ok=True)
    keys = ["PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR"]
    env = {key: os.environ[key] for key in keys if os.environ.get(key)}
    env.update({"HOME": str(home), "TMPDIR": str(temp), "TMP": str(temp), "TEMP": str(temp), "CI": "1"})
    if os.name == "nt":
        env["USERPROFILE"] = str(home)
        for name in ("APPDATA", "LOCALAPPDATA"):
            profile_path = home / name.lower()
            profile_path.mkdir(parents=True, exist_ok=True)
            env[name] = str(profile_path)
    for key, value in os.environ.items():
        if key.casefold() in {"http_proxy", "https_proxy", "no_proxy"} and "@" not in value:
            env[key] = value
    env["UV_CACHE_DIR"] = str(cache / "uv")
    env["npm_config_cache"] = str(cache / "npm")
    return env


def _registry_environment(environment: dict[str, str]) -> dict[str, str]:
    """Pass host proxy settings only to trusted, scripts-disabled package resolution."""
    result = dict(environment)
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"):
        if key in os.environ:
            result[key] = os.environ[key]
    return result


def remove_temporary_tree(path: Path) -> dict[str, Any]:
    if not path.name.startswith("ligamina-representative-"):
        return {"status": "refused", "path": str(path), "error": "temporary directory name does not match the runner prefix"}
    last_error = None
    for attempt in range(10):
        try:
            shutil.rmtree(path)
            return {"status": "passed", "path": str(path), "attempts": attempt + 1}
        except FileNotFoundError:
            return {"status": "passed", "path": str(path), "attempts": attempt + 1}
        except OSError as error:
            last_error = f"{type(error).__name__}: {error}"
            time.sleep(0.25)
    return {"status": "failed", "path": str(path), "error": last_error}


def command_step(label: str, command: list[str], *, cwd: Path, timeout: int = 1200, env: dict[str, str] | None = None) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, check=False,
        )
    except FileNotFoundError as error:
        return {"label": label, "status": "environment_missing", "error": str(error), "command": command}
    except subprocess.TimeoutExpired:
        return {"label": label, "status": "failed", "error": f"timed out after {timeout}s", "command": command}
    return {
        "label": label,
        "status": "passed" if completed.returncode == 0 else "failed",
        "command": command,
        "returncode": completed.returncode,
        "stdout_tail": completed.stdout[-4000:],
        "stderr_tail": completed.stderr[-4000:],
    }


def install_sample(sample: dict[str, Any], work: Path, cache_root: Path | None = None) -> tuple[dict[str, Any], dict[str, str]]:
    ecosystem = sample["ecosystem"]
    entry = sample["_entry"]
    environment = isolated_environment(work, cache_root)
    if ecosystem == "nonebot":
        uv = shutil.which("uv") or "uv"
        (work / "pyproject.toml").write_text(
            "[project]\nname = \"ligamina-sample-" + sample["sample_id"] + "\"\nversion = \"0.0.0\"\n"
            "requires-python = \"==3.12.*\"\ndependencies = [\n"
            + ''.join(f'  "{name}=={version}",\n' for name, version in (
                ("nonebot2[fastapi]", "2.5.0"), ("nonebot-adapter-onebot", "2.4.6"), ("jsonschema", "4.25.1")
            ))
            + f'  "{sample["package"]} @ {sample["artifact_url"]}",\n'
            + "]\n\n[tool.uv]\npackage = false\n",
            encoding="utf-8",
        )
        registry_env = _registry_environment(environment)
        lock = command_step("resolve exact Python sample profile", [uv, "lock"], cwd=work, timeout=1200, env=registry_env)
        sync = command_step(
            "install locked Python sample profile",
            [uv, "sync", "--locked", "--no-dev", "--no-build", "--python", "3.12.14"], cwd=work, timeout=1200, env=registry_env,
        ) if lock["status"] == "passed" else {"label": "install locked Python sample profile", "status": "skipped"}
        if sync["status"] == "passed":
            python_path = work / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            environment.update({
                "PYTHONPATH": str(ROOT / "hosts/nonebot"),
                "LIGAMINA_NONEBOT_PLUGINS": ",".join([*sample.get("preload_plugins", []), entry]),
            })
            run = command_step(
                "verify installed Python package metadata",
                [str(python_path), "-c", "from importlib.metadata import version; import sys; print(version(sys.argv[1]))", sample["package"]],
                cwd=work, env=environment,
            )
            exact_version = run.get("stdout_tail", "").strip().splitlines()[-1:] == [sample["version"]]
            if run["status"] != "passed" or not exact_version:
                run["status"] = "failed"
                run["error"] = "installed distribution version differs from the manifest"
        else:
            run = {"label": "verify installed Python package metadata", "status": "skipped"}
        lock_path = work / "uv.lock"
        return {"status": "passed" if all(item["status"] == "passed" for item in (lock, sync, run)) else "failed",
                "steps": [lock, sync, run], "lock_sha256": digest(lock_path) if lock_path.is_file() else None}, environment

    package_json: dict[str, Any] = {
        "name": f"ligamina-sample-{sample['sample_id']}", "version": "0.0.0", "private": True,
        "type": "module", "engines": {"node": "24.20.0"},
        "dependencies": {
            **HOST_VERSIONS["koishi"], "@modelcontextprotocol/sdk": "1.32.1", "ajv": "8.20.0",
            "typescript": "5.9.3", sample["package"]: sample["artifact_url"],
        },
    }
    (work / "package.json").write_text(json.dumps(package_json, indent=2) + "\n", encoding="utf-8")
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm") or ("npm.cmd" if os.name == "nt" else "npm")
    npm_prefix = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", npm] if os.name == "nt" else [npm]
    registry_env = _registry_environment(environment)
    lock = command_step("resolve exact Node sample profile", [*npm_prefix, "install", "--package-lock-only", "--ignore-scripts", "--no-audit", "--fund=false"], cwd=work, timeout=1200, env=registry_env)
    sync = command_step(
        "install locked Node sample profile", [*npm_prefix, "ci", "--ignore-scripts", "--no-audit", "--fund=false"],
        cwd=work, timeout=1200, env=registry_env,
    ) if lock["status"] == "passed" else {"label": "install locked Node sample profile", "status": "skipped"}
    if sync["status"] == "passed":
        try:
            lock_data = json.loads((work / "package-lock.json").read_text(encoding="utf-8"))
            lock_entry = lock_data.get("packages", {}).get(f"node_modules/{sample['package']}", {})
            if lock_entry.get("version") != sample["version"] or lock_entry.get("resolved") != sample["artifact_url"] or lock_entry.get("integrity") != sample["_artifact_hash"]:
                raise ValueError("Node lock entry differs from manifest version, tarball URL, or integrity")
            worker_dir = work / "ligamina-koishi-host"
            worker_dir.mkdir()
            shutil.copytree(ROOT / "hosts/koishi/src", worker_dir / "src")
            shutil.copy2(ROOT / "hosts/koishi/tsconfig.json", worker_dir / "tsconfig.json")
            contract_target = work.parent / "contracts"
            if not contract_target.exists():
                shutil.copytree(ROOT / "contracts", contract_target)
            built = command_step(
                "build Koishi Host in isolated profile",
                [shutil.which("node") or "node", str(work / "node_modules/typescript/bin/tsc"), "--project", str(worker_dir / "tsconfig.json")],
                cwd=work, timeout=300, env=environment,
            )
            if built["status"] != "passed":
                raise RuntimeError("Koishi Host build failed: " + built.get("stderr_tail", ""))
            resolve_script = work / "resolve-plugin.mjs"
            resolve_script.write_text(
                "const p = process.argv[2]; console.log(import.meta.resolve(p));\n", encoding="utf-8"
            )
            resolved = subprocess.run(
                [shutil.which("node") or "node", str(resolve_script), sample["package"]], cwd=work,
                env=environment, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, check=False,
            )
            if resolved.returncode:
                run = {"label": "resolve Koishi plugin entry", "status": "failed", "stderr_tail": resolved.stderr[-4000:]}
            else:
                entry_url = resolved.stdout.strip().splitlines()[-1]
                environment.update({
                    "LIGAMINA_KOISHI_PLUGINS": entry_url,
                    "LIGAMINA_KOISHI_DATA_DIR": str(work / "data"),
                })
                run = {"label": "resolve Koishi plugin entry", "status": "passed", "entry_url": entry_url}
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
            run = {"label": "prepare Koishi isolated worker", "status": "failed", "error": str(error)}
    else:
        run = {"label": "resolve Koishi plugin entry", "status": "skipped"}
    lock_path = work / "package-lock.json"
    if sync["status"] == "passed":
        metadata = command_step(
            "verify installed Node package metadata",
            [shutil.which("node") or "node", "-e", f"console.log(require({json.dumps(str(work / 'node_modules' / sample['package'] / 'package.json'))}).version)"],
            cwd=work, env=environment,
        )
        exact_version = metadata.get("stdout_tail", "").strip().splitlines()[-1:] == [sample["version"]]
        if metadata["status"] != "passed" or not exact_version:
            metadata["status"] = "failed"
            metadata["error"] = "installed package version differs from the manifest"
    else:
        metadata = {"label": "verify installed Node package metadata", "status": "skipped"}
    return {"status": "passed" if all(item["status"] == "passed" for item in (lock, sync, metadata, run)) else "failed",
            "steps": [lock, sync, metadata, run], "lock_sha256": digest(lock_path) if lock_path.is_file() else None}, environment


class DirectWorker:
    """One-shot stdio worker driver; always reaps the process tree on return."""

    def __init__(self, sample: dict[str, Any], work: Path, environment: dict[str, str]):
        self.sample = sample
        self.work = work
        self.environment = environment
        self.process: subprocess.Popen[str] | None = None
        self.stdout: queue.Queue[str] = queue.Queue()
        self.stderr: queue.Queue[str] = queue.Queue()
        self.requests: list[dict[str, Any]] = []
        self.responses: list[dict[str, Any]] = []
        self.outgoing: list[dict[str, Any]] = []
        self._job_handle: Any = None
        self._readers: list[threading.Thread] = []

    @staticmethod
    def _read(stream: Any, target: queue.Queue[str]) -> None:
        for line in iter(stream.readline, ""):
            target.put(line)

    def start(self) -> None:
        if self.sample["ecosystem"] == "nonebot":
            python_path = Path(self.environment.get(
                "LIGAMINA_PYTHON_EXECUTABLE",
                str(self.work / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")),
            ))
            command = [str(python_path), "-m", "ligamina_nonebot.worker"]
            cwd = self.work
        else:
            command = [
                self.environment.get("LIGAMINA_NODE_EXECUTABLE", shutil.which("node") or "node"),
                str(self.work / "ligamina-koishi-host/dist/worker.js"),
            ]
            cwd = self.work
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        self.process = subprocess.Popen(
            command, cwd=cwd, env=self.environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1,
            start_new_session=(os.name != "nt"), creationflags=creationflags,
        )
        if os.name == "nt":
            self._attach_windows_job()
        assert self.process.stdout and self.process.stderr
        self._readers = [
            threading.Thread(target=self._read, args=(self.process.stdout, self.stdout), daemon=True),
            threading.Thread(target=self._read, args=(self.process.stderr, self.stderr), daemon=True),
        ]
        for reader in self._readers:
            reader.start()

    def _attach_windows_job(self) -> None:
        import ctypes
        from ctypes import wintypes

        assert self.process is not None
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, wintypes.INT, wintypes.LPVOID, wintypes.DWORD]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.ResumeThread.restype = wintypes.DWORD
        kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        class BasicLimit(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class ExtendedLimit(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimit), ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise ctypes.WinError(ctypes.get_last_error())
        info = ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            error = ctypes.WinError(ctypes.get_last_error())
            kernel32.CloseHandle(job)
            raise error
        if not kernel32.AssignProcessToJobObject(job, wintypes.HANDLE(self.process._handle)):
            error = ctypes.WinError(ctypes.get_last_error())
            kernel32.CloseHandle(job)
            self.process.kill()
            self.process.wait(timeout=5)
            raise error
        self._job_handle = job

    def _receive_for(self, request_id: str, timeout: float = 90) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process is None or self.process.poll() is not None:
                stderr_tail = "".join(list(self.stderr.queue))[-3000:]
                raise RuntimeError(f"worker exited early: {self.process.returncode if self.process else None}; stderr={stderr_tail!r}")
            try:
                line = self.stdout.get(timeout=min(0.2, max(0.01, deadline - time.monotonic())))
            except queue.Empty:
                continue
            try:
                packet = json.loads(line)
            except json.JSONDecodeError as error:
                raise RuntimeError(f"worker wrote non-JSON to stdout: {line[:300]!r}") from error
            if packet.get("method") in ("platform.execute", "service.invoke"):
                self.requests.append(packet)
                self.outgoing.append(packet)
                response = {"jsonrpc": "2.0", "id": packet.get("id")}
                if packet["method"] == "platform.execute":
                    response["result"] = {"status": "succeeded", "platform_message_id": str(800000 + len(self.outgoing)), "data": None}
                else:
                    response["error"] = {"code": -32000, "message": "representative fixture does not grant services"}
                assert self.process and self.process.stdin
                self.process.stdin.write(json.dumps(response) + "\n")
                self.process.stdin.flush()
                continue
            if packet.get("id") == request_id:
                self.responses.append(packet)
                return packet
            self.responses.append(packet)
        raise TimeoutError(f"worker did not answer {request_id} within {timeout}s")

    def request(self, request_id: str, method: str, params: dict[str, Any], timeout: float = 90) -> dict[str, Any]:
        if self.process is None or self.process.stdin is None:
            raise RuntimeError("worker has not started")
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.process.stdin.flush()
        return self._receive_for(request_id, timeout)

    def run(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {"initialize": None, "dispatches": [], "outgoing": self.outgoing}
        self.start()
        try:
            result["initialize"] = self.request("representative-init", "host.initialize", {
                "protocol_version": "0.1", "host_id": f"sample-{self.sample['sample_id']}", "epoch": 1,
            })
            if "result" in result["initialize"]:
                for index, event in enumerate(events):
                    result["dispatches"].append(self.request(
                        f"representative-event-{index + 1}", "event.dispatch", {"event": event}
                    ))
        finally:
            self.stop()
        result["outgoing"] = list(self.outgoing)
        result["stderr_tail"] = "".join(list(self.stderr.queue))[-6000:]
        return result

    def stop(self) -> None:
        process = self.process
        if process is None:
            return
        try:
            if process.poll() is None and process.stdin:
                try:
                    self.request("representative-stop", "runtime.stop", {}, timeout=10)
                except Exception:
                    pass
                try:
                    process.stdin.close()
                except OSError:
                    pass
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            if process.poll() is None:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=15, check=False)
            if os.name != "nt":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif self._job_handle is not None:
                import ctypes

                ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(self._job_handle)
                self._job_handle = None
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            for reader in self._readers:
                reader.join(timeout=2)
            if process.stdin and not process.stdin.closed:
                process.stdin.close()


def test_events(sample: dict[str, Any]) -> list[dict[str, Any]]:
    fixture = sample.get("fixture") or {}
    conversation = fixture.get("conversation") or {"kind": "group", "id": "4242"}
    texts = fixture.get("input_texts") or [fixture.get("input_text", "ligamina sample probe")]
    events = []
    for index, text in enumerate(texts):
        event_id = f"sample-{sample['sample_id']}-{index + 1}"
        events.append({
            "schema_version": "ligamina/0.1", "event_id": event_id, "trace_id": f"trace-{event_id}",
            "platform": "onebot.v11" if sample["ecosystem"] == "nonebot" else "koishi",
            "account_id": ACCOUNT_ID, "conversation": conversation,
            "actor_id": str(fixture.get("actor_id", "2323")), "timestamp": utc_now(),
            "target_host": f"sample-{sample['sample_id']}",
            "message": [{"type": "text", "text": str(text)}],
            "extensions": {"onebot.v11": {"message_id": str(80000 + index), "sender": {"nickname": "Ligamina"}}},
        })
    return events


def api_request(base_url: str, method: str, path: str, token: str, body: Any = None, expected: tuple[int, ...] = (200,), timeout: float = 10) -> Any:
    encoded = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Authorization": f"Bearer {token}"}
    if encoded is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base_url + path, data=encoded, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    if status not in expected:
        raise RuntimeError(f"Core HTTP {method} {path} returned {status}: {raw[:1000]!r}")
    return json.loads(raw) if raw else None


def run_federated(sample: dict[str, Any], work: Path, host_environment: dict[str, str], events: list[dict[str, Any]]) -> dict[str, Any]:
    configured_executable = host_environment.get("LIGAMINA_CORE_EXECUTABLE")
    executable = (
        Path(configured_executable)
        if configured_executable
        else ROOT / ("target/debug/ligamina-cli.exe" if os.name == "nt" else "target/debug/ligamina-cli")
    )
    if not executable.is_file():
        raise FileNotFoundError(f"Core daemon binary is missing: {executable}")
    sample_id = sample["sample_id"]
    host_id = f"sample-{sample_id}"
    client_id = "ligamina-sample-client"
    client_token = __import__("secrets").token_hex(32)
    admin_token = __import__("secrets").token_hex(32)
    registration_token = __import__("secrets").token_hex(32)
    operation_grant = {"id": f"{sample_id}-client-send", "account_id": ACCOUNT_ID, "operations": ["send_message"], "targets": None}
    host_grant = {
        "id": f"{sample_id}-host-send", "client_id": client_id, "account_id": ACCOUNT_ID,
        "operations": ["send_message"], "targets": None,
    }
    host_env = {
        "LIGAMINA_NONEBOT_PLUGINS": ",".join([*sample.get("preload_plugins", []), sample["_entry"]]),
        "LIGAMINA_NONEBOT_NICKNAMES": "Bot",
        "LIGAMINA_NONEBOT_DATA_DIR": str(work / "nonebot-data"),
        "LIGAMINA_KOISHI_PLUGINS": host_environment.get("LIGAMINA_KOISHI_PLUGINS", ""),
        "LIGAMINA_KOISHI_PLUGIN_CONFIG": host_environment.get("LIGAMINA_KOISHI_PLUGIN_CONFIG", "{}"),
        "LIGAMINA_KOISHI_NICKNAMES": "Bot",
        "LIGAMINA_KOISHI_DATA_DIR": str(work / "koishi-data"),
        "PYTHONPATH": host_environment.get("PYTHONPATH", str(ROOT / "hosts/nonebot")),
    }
    if sample["ecosystem"] == "nonebot":
        python_executable = host_environment.get("LIGAMINA_PYTHON_EXECUTABLE")
        if python_executable:
            program = python_executable
            args = ["-m", "ligamina_nonebot.worker"]
        else:
            program = shutil.which("uv") or "uv"
            args = ["run", "--locked", "--project", str(work), "--python", "3.12.14", "python", "-m", "ligamina_nonebot.worker"]
        current_dir = work
    else:
        program = host_environment.get("LIGAMINA_NODE_EXECUTABLE", shutil.which("node") or "node")
        args = [str(work / "ligamina-koishi-host/dist/worker.js")]
        current_dir = work
    config = {
        "listen": "127.0.0.1:0", "database_path": str(work / "core.sqlite"), "admin_token": admin_token,
        "mcp_client_id": client_id,
        "clients": [{"id": client_id, "token": client_token, "capabilities": [], "operation_grants": [operation_grant]}],
        "gateways": [{"id": "sample-gateway", "client_id": client_id, "account_id": ACCOUNT_ID, "registration_token": registration_token}],
        "hosts": [{
            "id": host_id, "enabled": True, "program": program, "args": args, "current_dir": str(current_dir),
            "environment": host_env, "max_restarts": 0, "restart_backoff_ms": 100,
            "startup_timeout_ms": 90000, "shutdown_timeout_ms": 10000, "operation_grants": [host_grant],
        }],
        "router": {"max_pending_per_conversation": 16, "max_outstanding_events": 64,
                   "max_tracked_conversations": 64, "max_concurrent_conversations": 8,
                   "processing_timeout_ms": 30000},
    }
    config_path = work / "daemon.json"
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    core_environment = isolated_environment(work / "core-environment", work / "shared-cache")
    process = subprocess.Popen(
        [str(executable), "daemon", "--config", str(config_path)], cwd=ROOT, env=core_environment,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    assert process.stdout is not None and process.stderr is not None
    stdout: queue.Queue[str] = queue.Queue()
    stderr: queue.Queue[str] = queue.Queue()
    threading.Thread(target=DirectWorker._read, args=(process.stdout, stdout), daemon=True).start()
    threading.Thread(target=DirectWorker._read, args=(process.stderr, stderr), daemon=True).start()
    result: dict[str, Any] = {"event_requests": events, "gateway_operations": [], "cleanup": {"host_pid": None, "host_alive_after_shutdown": None}}
    gateway_stop = threading.Event()
    gateway_errors: list[str] = []
    gateway_counter = 0
    gateway_lock = threading.Lock()
    try:
        ready = None
        deadline = time.monotonic() + 100
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"Core exited during startup with code {process.returncode}: {''.join(list(stderr.queue))[-4000:]}")
            try:
                line = stdout.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                packet = json.loads(line)
            except json.JSONDecodeError:
                continue
            if packet.get("ready") is True:
                ready = packet
                break
        if ready is None:
            raise TimeoutError("Core daemon did not announce readiness")
        base_url = f"http://{ready['listen']}"
        initial = api_request(base_url, "GET", "/v1/status", admin_token)
        host_status = next((item for item in initial.get("hosts", []) if item.get("id") == host_id), None)
        if not host_status or host_status.get("state") != "active":
            raise RuntimeError(f"sample Host did not become active: {host_status}")
        result["host_status"] = host_status
        result["cleanup"]["host_pid"] = host_status.get("pid")
        lease = api_request(base_url, "POST", "/v1/gateways/sample-gateway/lease", registration_token, {"ttl_ms": 300000})
        credential = lease["token"]

        def gateway_loop() -> None:
            nonlocal gateway_counter
            while not gateway_stop.is_set():
                try:
                    operation = api_request(
                        base_url, "POST", "/v1/gateways/sample-gateway/actions/claim", credential,
                        lease, timeout=5,
                    )
                    if operation is None:
                        gateway_stop.wait(0.05)
                        continue
                    with gateway_lock:
                        gateway_counter += 1
                        counter = gateway_counter
                    # Numeric strings satisfy the OneBot v11 adapter's native ID model
                    # while remaining valid opaque Satori IDs for the Koishi profile.
                    receipt_id = str(800000 + counter)
                    receipt = {
                        "key": operation["key"], "fingerprint": operation["fingerprint"], "status": "succeeded",
                        "platform_message_id": receipt_id, "data": None, "error": None,
                        "observed_at_ms": int(time.time() * 1000), "evidence_ref": f"ligamina-sample://gateway/{receipt_id}",
                    }
                    completed = api_request(
                        base_url, "POST",
                        f"/v1/gateways/sample-gateway/actions/{urllib.parse.quote(operation['key']['operation_id'], safe='')}/result",
                        credential, {"credential": lease, "result": receipt},
                    )
                    result["gateway_operations"].append({
                        "operation": operation, "message": operation.get("operation_payload", {}).get("params", {}).get("message"),
                        "receipt": completed,
                    })
                except BaseException as error:
                    gateway_errors.append(f"{type(error).__name__}: {error}")
                    gateway_stop.set()

        gateway_thread = threading.Thread(target=gateway_loop, name="sample-gateway", daemon=True)
        gateway_thread.start()
        statuses = []
        for event in events:
            accepted = api_request(base_url, "POST", "/v1/events", client_token, event, expected=(202,))
            query = urllib.parse.urlencode({
                "platform": event["platform"], "account_id": event["account_id"],
                "conversation_kind": event["conversation"]["kind"], "conversation_id": event["conversation"]["id"],
            })
            path = f"/v1/events/{urllib.parse.quote(event['event_id'], safe='')}?{query}"
            deadline = time.monotonic() + 45
            status = None
            while time.monotonic() < deadline:
                status = api_request(base_url, "GET", path, client_token)
                if status.get("status") in ("completed", "failed", "unknown"):
                    break
                time.sleep(0.05)
            statuses.append({"accepted": accepted, "final": status})
            if not status or status.get("status") != "completed":
                raise RuntimeError(f"federated event did not complete: {status}")
        result["event_statuses"] = statuses
        wait_until = time.monotonic() + 3
        while time.monotonic() < wait_until and not gateway_errors:
            time.sleep(0.025)
        if gateway_errors:
            raise RuntimeError("virtual gateway failed: " + "; ".join(gateway_errors))
    finally:
        gateway_stop.set()
        thread = locals().get("gateway_thread")
        if thread:
            thread.join(timeout=5)
        if process.poll() is None:
            try:
                base_url_value = locals().get("base_url")
                if base_url_value:
                    api_request(base_url_value, "POST", "/v1/shutdown", admin_token, expected=(202,))
            except Exception as error:
                result["shutdown_request_error"] = f"{type(error).__name__}: {error}"
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        result["core_exit_code"] = process.returncode
        pid = result["cleanup"].get("host_pid")
        if pid:
            if os.name == "nt":
                check = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=10, check=False)
                result["cleanup"]["host_alive_after_shutdown"] = str(pid) in check.stdout
            else:
                try:
                    os.kill(int(pid), 0)
                    result["cleanup"]["host_alive_after_shutdown"] = True
                except (ProcessLookupError, OSError):
                    result["cleanup"]["host_alive_after_shutdown"] = False
        result["stderr_tail"] = "".join(list(stderr.queue))[-6000:]
        result["gateway_errors"] = gateway_errors
    return result


def normalized_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(
            str(segment.get("text", "")) for segment in value
            if isinstance(segment, dict) and segment.get("type") == "text"
        )
    return ""


def expected_texts(fixture: dict[str, Any]) -> list[str] | None:
    values = fixture.get("expected_outgoing_texts")
    if isinstance(values, list) and all(isinstance(value, str) for value in values):
        return values
    value = fixture.get("expected_outgoing_text")
    return [value] if isinstance(value, str) else None


def native_outgoing_messages(trace: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not trace:
        return []
    results = []
    for request in trace.get("outgoing", []):
        params = request.get("params", {})
        operation = params.get("operation_payload") if isinstance(params.get("operation_payload"), dict) else params
        payload = operation.get("params", {}) if isinstance(operation, dict) else {}
        message = payload.get("message") if isinstance(payload, dict) else None
        if message is not None:
            results.append({"text": normalized_text(message), "target": operation.get("target")})
    return results


def federated_outgoing_messages(trace: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not trace:
        return []
    results = []
    for record in trace.get("gateway_operations", []):
        operation = record.get("operation", {})
        payload = operation.get("operation_payload", {})
        results.append({
            "text": normalized_text(payload.get("params", {}).get("message")),
            "target": payload.get("target"),
        })
    return results


def run_samples(manifest_path: Path, report_path: Path, sample_ids: set[str] | None = None) -> tuple[int, dict[str, Any]]:
    started = utc_now()
    report: dict[str, Any] = {
        "suite": "representative", "status": "running", "worker": "isolated",
        "started_at": started, "platform": platform.platform(), "samples": [], "error": None,
        "runtime_versions": {
            "python": {"version": sys.version.splitlines()[0], "executable": sys.executable},
            "uv": capture_version(["uv", "--version"]),
            "node": capture_version(["node", "--version"]),
            "npm": capture_version(["npm", "--version"]),
            "rustc": capture_version(["rustc", "--version"]),
            "cargo": capture_version(["cargo", "--version"]),
        },
        "definition": {
            "L0": "source metadata and the registry-declared artifact digest were verified from downloaded bytes",
            "L1": "exact framework and plugin dependencies resolved and installed in a disposable profile",
            "L2": "unmodified plugin imported and initialized through its native framework Host; fixture event dispatched",
            "L3": "plugin receives the same fixture through the production Core federated route",
            "L4": "expected behavior and native/federated operation traces both match the sample fixture",
            "L5": "repeated lifecycle/resource stability; belongs to T14",
            "isolation_boundary": "disposable dependency profile, HOME, caches, credentials, process lifetime and data directory; not an OS security sandbox",
        },
    }
    try:
        manifest, samples = validate_manifest(manifest_path, sample_ids)
        report["manifest"] = {"path": str(manifest_path.resolve()), "sha256": digest(manifest_path), "sample_count": len(samples)}
        report["framework_versions"] = HOST_VERSIONS
        for sample in samples:
            fixture_path = sample.get("fixture_path")
            if fixture_path:
                resolved_fixture = (manifest_path.resolve().parent / fixture_path).resolve()
                if not resolved_fixture.is_relative_to((ROOT / "samples/fixtures/third-party").resolve()):
                    raise ValueError(f"fixture path for {sample['sample_id']} must remain under samples/fixtures/third-party")
                sample["fixture"] = json.loads(resolved_fixture.read_text(encoding="utf-8"))
                sample["_fixture_path"] = str(resolved_fixture)
        report["samples"] = []
        core_build = command_step("build production Core daemon", [shutil.which("cargo") or "cargo", "build", "-p", "ligamina-cli", "--locked"], cwd=ROOT, timeout=900)
        report["core_build"] = core_build
        if core_build["status"] != "passed":
            message = core_build.get("error") or core_build.get("stderr_tail") or "Core daemon build failed"
            if core_build["status"] == "environment_missing":
                raise FileNotFoundError(message)
            raise RuntimeError(message)
        with tempfile.TemporaryDirectory(prefix="ligamina-representative-", ignore_cleanup_errors=True) as temp_root:
            root = Path(temp_root)
            for sample in samples:
                record: dict[str, Any] = {
                    "sample_id": sample["sample_id"], "ecosystem": sample["ecosystem"],
                    "package": sample["package"], "version": sample["version"],
                    "license": sample["license"], "category": sample["category"],
                    "source_url": sample["source_url"], "artifact_hash_algorithm": sample["_artifact_hash_algorithm"],
                    "artifact_hash": sample["_artifact_hash"],
                    "preload_plugins": sample.get("preload_plugins", []),
                    "fixture_path": sample.get("_fixture_path"),
                    "reproduction_command": f"uv run --project lab python lab/run.py --suite representative --manifest samples/third-party.lock.json --worker isolated --sample-id {sample['sample_id']}",
                    "levels": {},
                }
                work = root / sample["sample_id"]
                work.mkdir()
                artifact = work / "artifact.bin"
                l0 = download_and_verify(sample, artifact)
                record["levels"]["L0"] = l0
                if l0["status"] != "passed":
                    record["failure_classification"] = ["artifact_download_or_integrity_failure"]
                    for level in ("L1", "L2", "L3", "L4", "L5"):
                        record["levels"][level] = {"status": "skipped", "reason": "L0 metadata/artifact verification failed"}
                    report["samples"].append(record)
                    continue
                try:
                    if sample["ecosystem"] == "nonebot":
                        sample["_entry"] = discover_python_entry(sample, artifact)
                        record["entry_module"] = sample["_entry"]
                    else:
                        record["entry_point"] = sample.get("entry_point", ".")
                    l1, environment = install_sample(sample, work, root / "shared-cache")
                except BaseException as error:
                    record["levels"]["L1"] = {
                        "status": "failed", "error": f"{type(error).__name__}: {error}",
                        "reproducibility_artifacts": preserve_profile_artifacts(sample, work, report_path),
                    }
                    record["failure_classification"] = ["dependency_resolution_installation_or_profile_failure"]
                    for level in ("L2", "L3", "L4", "L5"):
                        record["levels"][level] = {"status": "skipped", "reason": "L1 entry/profile setup failed"}
                    report["samples"].append(record)
                    continue
                record["levels"]["L1"] = l1
                if l1["status"] != "passed":
                    l1["reproducibility_artifacts"] = preserve_profile_artifacts(sample, work, report_path)
                    record["failure_classification"] = ["dependency_resolution_installation_or_profile_failure"]
                    for level in ("L2", "L3", "L4", "L5"):
                        record["levels"][level] = {"status": "skipped", "reason": "L1 dependency profile did not install"}
                    report["samples"].append(record)
                    continue
                events = test_events(sample)
                record["input_events"] = events
                fixture = sample.get("fixture") or {}
                if sample["ecosystem"] == "koishi" and fixture.get("plugin_config"):
                    module_url = environment.get("LIGAMINA_KOISHI_PLUGINS", "")
                    environment["LIGAMINA_KOISHI_PLUGIN_CONFIG"] = json.dumps({module_url: fixture["plugin_config"]}, ensure_ascii=False)
                if sample["ecosystem"] == "nonebot" and fixture.get("nonebot_config"):
                    custom_config = fixture["nonebot_config"]
                    if not isinstance(custom_config, dict) or any(
                        not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", key)
                        for key in custom_config
                    ):
                        raise ValueError(f"sample fixture {sample['sample_id']} has invalid NoneBot custom config")
                    dotenv_values = [
                        f"{key}={json.dumps(value, ensure_ascii=False, separators=(',', ':'))}"
                        for key, value in sorted(custom_config.items())
                    ]
                    (work / ".env.prod").write_text("\n".join(dotenv_values) + "\n", encoding="utf-8")
                for key, value in fixture.get("environment", {}).items():
                    if not isinstance(key, str) or not key.startswith("LIGAMINA_SAMPLE_") or not isinstance(value, str):
                        raise ValueError(f"sample fixture {sample['sample_id']} has an invalid isolated environment override")
                    environment[key] = value
                l1["reproducibility_artifacts"] = preserve_profile_artifacts(sample, work, report_path)
                native = None
                try:
                    native = DirectWorker(sample, work, environment).run(events)
                    initialized = native.get("initialize", {}).get("result", {}).get("ready") is True
                    dispatched = len(native.get("dispatches", [])) == len(events) and all(
                        item.get("result", {}).get("accepted") is True for item in native.get("dispatches", [])
                    )
                    l2_status = "passed" if initialized and dispatched else "failed"
                    record["levels"]["L2"] = {"status": l2_status, "native_trace": native}
                except BaseException as error:
                    record["levels"]["L2"] = {"status": "failed", "error": f"{type(error).__name__}: {error}", "traceback": traceback.format_exc()[-4000:]}
                    record["failure_classification"] = ["native_host_or_event_dispatch_failure"]
                federated = None
                if record["levels"]["L2"]["status"] == "passed":
                    try:
                        federated = run_federated(sample, work, environment, events)
                        result_status = "passed" if all(
                            item.get("final", {}).get("status") == "completed" for item in federated.get("event_statuses", [])
                        ) and federated.get("cleanup", {}).get("host_alive_after_shutdown") is False else "failed"
                        record["levels"]["L3"] = {"status": result_status, "federated_trace": federated}
                    except BaseException as error:
                        record["levels"]["L3"] = {"status": "failed", "error": f"{type(error).__name__}: {error}", "traceback": traceback.format_exc()[-4000:], "federated_trace": federated}
                        record["failure_classification"] = ["federated_core_route_or_lifecycle_failure"]
                else:
                    record["levels"]["L3"] = {"status": "skipped", "reason": "L2 native baseline did not complete"}
                fixture = sample.get("fixture") or {}
                expected = expected_texts(fixture)
                if record["levels"]["L3"]["status"] == "passed" and expected is not None:
                    native_messages = native_outgoing_messages(native)
                    federated_messages = federated_outgoing_messages(federated)
                    expected_target = fixture.get("expected_target") or fixture.get("conversation")
                    native_text_match = [entry["text"] for entry in native_messages] == expected
                    federated_text_match = [entry["text"] for entry in federated_messages] == expected
                    native_target_match = all(entry["target"] == expected_target for entry in native_messages)
                    federated_target_match = all(entry["target"] == expected_target for entry in federated_messages)
                    trace_parity = native_messages == federated_messages
                    reasons = []
                    if not native_text_match:
                        reasons.append("native_baseline_output_mismatch")
                    if not native_target_match:
                        reasons.append("native_baseline_target_mismatch")
                    if not federated_text_match:
                        reasons.append("federated_behavior_output_mismatch")
                    if not federated_target_match:
                        reasons.append("federated_behavior_target_mismatch")
                    if not trace_parity:
                        reasons.append("native_federated_behavior_divergence")
                    matches = not reasons
                    record["levels"]["L4"] = {
                        "status": "passed" if matches else "failed", "expected_outgoing_texts": expected,
                        "native_messages": native_messages, "federated_messages": federated_messages,
                        "expected_target": expected_target,
                        "native_baseline": {"text_match": native_text_match, "target_match": native_target_match},
                        "federated_behavior": {"text_match": federated_text_match, "target_match": federated_target_match},
                        "trace_parity": trace_parity,
                        "failure_classification": reasons,
                    }
                    if reasons:
                        record["failure_classification"] = reasons
                else:
                    record["levels"]["L4"] = {
                        "status": "not_run", "reason": "no L4 expected-output fixture" if expected is None else "L3 did not complete",
                        "expected_outgoing_texts": expected,
                    }
                record["levels"]["L5"] = {"status": "not_run", "reason": "repeated lifecycle/resource stability belongs to T14"}
                if "failure_classification" not in record and record["levels"]["L3"]["status"] != "passed":
                    record["failure_classification"] = [
                        "native_host_or_event_dispatch_failure"
                        if record["levels"]["L2"]["status"] == "failed"
                        else "federated_core_route_or_lifecycle_failure"
                    ]
                report["samples"].append(record)
        leftover_path = Path(temp_root)
        report["temporary_profile_cleanup"] = remove_temporary_tree(leftover_path) if leftover_path.exists() else {"status": "passed", "path": str(leftover_path), "already_removed": True}
        passed_l4 = {name: 0 for name in ECOSYSTEMS}
        message_l4 = {name: 0 for name in ECOSYSTEMS}
        for record in report["samples"]:
            if record.get("levels", {}).get("L4", {}).get("status") == "passed":
                ecosystem = record["ecosystem"]
                passed_l4[ecosystem] += 1
                if "message" in record.get("category", []):
                    message_l4[ecosystem] += 1
        report["l4_pass_count"] = passed_l4
        report["l4_message_pass_count"] = message_l4
        required_categories = {"command", "task", "dependency", "storage"}
        observed_categories = {category for record in report["samples"] for category in record.get("category", [])}
        report["category_coverage"] = {category: category in observed_categories for category in sorted(required_categories)}
        report["sample_failure_count"] = sum(
            1 for record in report["samples"]
            if any(level.get("status") == "failed" for level in record.get("levels", {}).values())
        )
        gate_passed = (
            all(passed_l4[name] >= 2 and message_l4[name] >= 1 for name in passed_l4)
            and all(report["category_coverage"].values())
            and len(report["samples"]) == len(samples)
            and report["temporary_profile_cleanup"]["status"] == "passed"
            and all(
                record.get("levels", {}).get("L4", {}).get("status") != "passed"
                or record.get("levels", {}).get("L1", {}).get("reproducibility_artifacts", {}).get("status") == "passed"
                for record in report["samples"]
            )
        )
        report["status"] = "completed"
        report["gate"] = "PASS" if gate_passed else "NOT READY"
        report["gate_reason"] = None if gate_passed else (
            "C11 requires at least two L4 behavior passes per ecosystem, with at least one passing message plugin per ecosystem. "
            "L0-L3 failures remain recorded per exact package version and are not included in the support list."
        )
        report["support_list"] = [
            {"ecosystem": record["ecosystem"], "package": record["package"], "version": record["version"], "fixture": record.get("fixture_path")}
            for record in report["samples"] if record.get("levels", {}).get("L4", {}).get("status") == "passed"
        ]
        return_code = 0
    except (OSError, ValueError, json.JSONDecodeError) as error:
        report["status"] = "environment_missing" if isinstance(error, FileNotFoundError) else "failed"
        report["error"] = f"{type(error).__name__}: {error}"
        report["traceback"] = traceback.format_exc()[-6000:]
        return_code = 2 if isinstance(error, FileNotFoundError) else 1
    report["ended_at"] = utc_now()
    report["report"] = str(report_path.resolve())
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return return_code, report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--worker", choices=("isolated",), required=True)
    parser.add_argument("--sample-id", action="append", default=[])
    parser.add_argument("--report", type=Path)
    options = parser.parse_args(argv)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    report_path = options.report or ROOT / "outputs/acceptance" / f"t13-representative-{stamp}.json"
    code, report = run_samples(options.manifest, report_path, set(options.sample_id) or None)
    summary = {
        "suite": report["suite"], "status": report["status"], "report": str(report_path.resolve()),
        "sample_count": len(report.get("samples", [])), "l4_pass_count": report.get("l4_pass_count"),
        "gate": report.get("gate"), "error": report.get("error"),
    }
    print(json.dumps(summary, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
