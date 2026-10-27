"""Execute exactly one behavior side inside the network-disabled Docker Worker."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any

PROJECT = Path(__file__).resolve().parents[2]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from lab.native_runner import execute_harness


_SENSITIVE_VALUE = re.compile(
    r"(?i)(?:token|secret|password|authorization|api[_-]?key|cookie|credential)\s*[:=]\s*[^\s,;]+"
)
_LONG_VALUE = re.compile(r"(?<![A-Za-z0-9])[A-Fa-f0-9]{32,}(?![A-Za-z0-9])")


def _safe_diagnostic_summary(raw: str) -> str:
    """Keep the exception class and a bounded, scrubbed final line for failure triage."""
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if not lines:
        return "diagnostic output was empty"
    summary = lines[-1]
    if summary.startswith("Node.js v"):
        errors = [
            line
            for line in lines
            if re.search(r"\b(?:Error|TypeError|SyntaxError|ReferenceError|RangeError)(?:\b|\[)", line)
            and not line.startswith("Node.js v")
        ]
        summary = errors[0] if errors else (lines[-2] if len(lines) > 1 else summary)
    prefix = "RuntimeError: federated event did not complete: "
    if summary.startswith(prefix):
        try:
            detail = json.loads(summary[len(prefix):])
        except json.JSONDecodeError:
            detail = None
        if isinstance(detail, dict):
            summary = (
                f"federated event_id={detail.get('event_id')} status={detail.get('status')} "
                f"error={detail.get('error')} gateway_operations={detail.get('gateway_operation_count')} "
                f"gateway_errors={detail.get('gateway_errors', [])}"
            )
    summary = _SENSITIVE_VALUE.sub("<redacted-field>", summary)
    summary = _LONG_VALUE.sub("<redacted-value>", summary)
    summary = re.sub(r"/(?:work|input/project)/[^\s:'\"]+", "<worker-path>", summary)
    if re.search(r"(?i)(token|secret|password|authorization|api[_-]?key|cookie|credential)", summary):
        exception = re.match(r"([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception))\b", summary)
        if exception:
            summary = f"{exception.group(1)}: <redacted diagnostic>"
        else:
            summary = "<redacted diagnostic>"
    return summary[:320]


def _assert_oracle_absent(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key.casefold() in {"oracle", "assertions"}:
                raise ValueError("behavior Oracle material must stay outside the plugin Worker")
            _assert_oracle_absent(child)
    elif isinstance(value, list):
        for child in value:
            _assert_oracle_absent(child)


def _run(command: list[str], *, cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _prepare_profile(request: dict[str, Any]) -> dict[str, Any]:
    context = request["context"]
    ecosystem = context["sample"]["ecosystem"]
    profile = Path("/work/profile")
    profile.mkdir(parents=True, exist_ok=True)
    if ecosystem == "nonebot":
        site_packages = profile / "site-packages"
        site_packages.mkdir(parents=True, exist_ok=True)
        lock = Path("/input/project/sample/requirements.txt")
        wheelhouse = Path("/input/project/sample/wheelhouse")
        install = _run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--no-index",
                "--find-links",
                str(wheelhouse),
                "--require-hashes",
                "--no-deps",
                "--only-binary=:all:",
                "--target",
                str(site_packages),
                "-r",
                str(lock),
            ],
            cwd=profile,
            timeout=600,
        )
        if install.returncode:
            raise RuntimeError(f"locked offline Python profile install failed: {install.stderr[-2000:]}")
        context["site_packages"] = str(site_packages)
        context["host_environment"].update(
            {
                "LIGAMINA_PYTHON_EXECUTABLE": sys.executable,
                "LIGAMINA_PROFILE_SITE_PACKAGES": str(site_packages),
                "PYTHONPATH": f"/input/project/hosts/nonebot{os.pathsep}{site_packages}",
            }
        )
        return {
            "status": "passed",
            "kind": "python_require_hashes_offline",
            "lock_sha256": context["expected_environment"]["lockfile_sha256"],
            "wheel_count": sum(1 for path in wheelhouse.iterdir() if path.is_file()),
        }

    if ecosystem != "koishi":
        raise ValueError(f"unsupported Phase 4 ecosystem: {ecosystem!r}")
    archive = Path("/input/project/sample/npm-cache.tar.gz")
    if _sha256_file(archive) != context["npm_cache_sha256"]:
        raise RuntimeError("offline npm cache archive differs from its prepared asset report")
    with tarfile.open(archive, mode="r:gz") as stream:
        stream.extractall("/work", filter="data")
    shutil.copy2("/input/project/sample/package.json", profile / "package.json")
    shutil.copy2("/input/project/sample/package-lock.json", profile / "package-lock.json")
    shutil.copy2("/input/project/sample/artifact.bin", profile / "artifact.bin")
    cache = Path("/work/npm-cache")
    if not cache.is_dir():
        raise RuntimeError("offline npm cache archive has no npm-cache directory")
    install = _run(
        [
            "/usr/local/bin/node",
            "/usr/local/lib/node_modules/npm/bin/npm-cli.js",
            "ci",
            "--offline",
            "--ignore-scripts",
            "--no-audit",
            "--fund=false",
            "--strict-peer-deps",
            "--engine-strict",
            "--cache",
            str(cache),
        ],
        cwd=profile,
        timeout=600,
    )
    if install.returncode:
        raise RuntimeError(f"locked offline npm profile install failed: {install.stderr[-2000:]}")
    package_name = context["sample"]["package"]
    if not re.fullmatch(r"(?:@[a-z0-9._-]+/)?[a-z0-9._-]+", package_name):
        raise ValueError("frozen Koishi package name is outside the npm package-name allowlist")
    package_path = profile / "node_modules" / Path(*package_name.split("/")) / "package.json"
    if not package_path.is_file():
        raise RuntimeError(f"locked Koishi profile archive has no exact package: {package_name}")
    if not (profile / "node_modules/@koishijs/core/package.json").is_file():
        raise RuntimeError("locked Koishi profile archive has no framework package")

    host_root = Path("/opt/ligamina/hosts/koishi")
    host_worker = host_root / "dist/worker.js"
    host_lock = host_root / "package-lock.json"
    sqlite_driver = host_root / "node_modules/@minatojs/driver-sqlite"
    if (
        not host_worker.is_file()
        or not host_lock.is_file()
        or _sha256_file(host_lock) != context["host_lock_sha256"]
        or not sqlite_driver.is_dir()
    ):
        raise RuntimeError("pinned production Koishi Host assets are incomplete")
    (profile / "ligamina-koishi-host").symlink_to(host_root, target_is_directory=True)
    driver_target = profile / "node_modules/@minatojs/driver-sqlite"
    if driver_target.exists() or driver_target.is_symlink():
        if driver_target.is_symlink() or driver_target.is_dir():
            if driver_target.is_symlink():
                driver_target.unlink()
            else:
                shutil.rmtree(driver_target)
        else:
            driver_target.unlink()
    driver_target.parent.mkdir(parents=True, exist_ok=True)
    driver_target.symlink_to(sqlite_driver, target_is_directory=True)

    resolver = _run(
        [
            "/usr/local/bin/node",
            "--input-type=module",
            "-e",
            f"console.log(import.meta.resolve({json.dumps(package_name)}))",
        ],
        cwd=profile,
        timeout=15,
    )
    if resolver.returncode or not resolver.stdout.strip().startswith("file:"):
        raise RuntimeError(f"could not resolve the frozen Koishi package {package_name!r}: {resolver.stderr[-1000:]}")
    context["koishi_entry_url"] = resolver.stdout.strip().splitlines()[-1]
    context["host_environment"].update(
        {
            "LIGAMINA_NODE_EXECUTABLE": "/usr/local/bin/node",
            "LIGAMINA_CORE_EXECUTABLE": "/usr/local/bin/ligamina-cli",
        }
    )
    harness = profile / ".ligamina/koishi/runner.mjs"
    harness.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2("/input/project/lab/baselines/koishi/runner.mjs", harness)
    return {
        "status": "passed",
        "kind": "locked_npm_offline_install",
        "lock_sha256": context["expected_environment"]["lockfile_sha256"],
        "npm_cache_sha256": context["npm_cache_sha256"],
        "framework_version": "4.18.11",
        "host_lock_sha256": context["host_lock_sha256"],
    }


def run_side(request: dict[str, Any]) -> dict[str, Any]:
    _assert_oracle_absent(request)
    context = request.get("context")
    if not isinstance(context, dict):
        raise ValueError("side request must contain a context object")
    mode = context.get("execution", {}).get("mode")
    if mode not in {"native", "federated"}:
        raise ValueError("side request does not select a registered execution mode")
    ecosystem = context.get("sample", {}).get("ecosystem")
    setup_evidence = _prepare_profile(request)
    context["repo_root"] = "/input/project"
    context["work_dir"] = "/work/profile"
    context["node_executable"] = "/usr/local/bin/node"
    context["execution"]["workspace"] = "/work/side"
    context["host_environment"].update(
        {
            "LIGAMINA_CORE_EXECUTABLE": "/usr/local/bin/ligamina-cli",
            "LIGAMINA_NODE_EXECUTABLE": "/usr/local/bin/node",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
        }
    )
    Path("/work/side").mkdir(parents=True, exist_ok=True)
    if ecosystem == "nonebot":
        runner = "/input/project/lab/baselines/nonebot/runner.py" if mode == "native" else "/input/project/lab/baselines/federated.py"
        command = [sys.executable, runner]
    else:
        command = [
            "/usr/local/bin/node",
            str(Path("/work/profile/.ligamina/koishi/runner.mjs") if mode == "native" else Path("/input/project/lab/baselines/federated.py")),
        ]
        if mode == "federated":
            command = [sys.executable, "/input/project/lab/baselines/federated.py"]
    result = execute_harness(command, request, mode, 180, cwd=Path("/work/side"))
    for key in ("diagnostics", "stderr"):
        raw = result.pop(key, None)
        if isinstance(raw, str) and raw:
            result[f"{key}_evidence"] = {
                "sha256": hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest(),
                "size_bytes": len(raw.encode("utf-8", errors="replace")),
            }
            result[f"{key}_summary"] = _safe_diagnostic_summary(raw)
    result["profile_setup"] = setup_evidence
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request-json", required=True)
    options = parser.parse_args()
    try:
        request = json.loads(options.request_json)
        result = run_side(request)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as error:  # Worker boundary converts setup faults into explicit attempt output.
        print(json.dumps({"status": "failed", "reason_code": "worker_side_setup_failed", "diagnostic": str(error)}, ensure_ascii=False))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
