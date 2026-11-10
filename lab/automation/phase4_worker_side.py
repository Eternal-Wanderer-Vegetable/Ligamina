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
            if re.search(r"^\s*(?:Error|TypeError|SyntaxError|ReferenceError|RangeError)(?:\b|\[)", line)
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


def _koishi_database_provider_module(service_profile: dict[str, Any], provider_modules: list[str]) -> str | None:
    """Resolve the selected Koishi database provider from its structured profile record."""
    provider = service_profile.get("database_provider")
    if provider is None:
        return None
    module_name = provider.get("package") if isinstance(provider, dict) else provider
    if not isinstance(module_name, str) or not module_name:
        raise ValueError("Koishi service profile database provider has no package name")
    if module_name not in provider_modules:
        raise ValueError("Koishi service profile database provider is absent from its preloaded package set")
    return module_name


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


def _tree_sha256(root: Path) -> str:
    rows = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"runtime artifact tree contains a symlink: {path}")
        if path.is_file():
            rows.append({"path": path.relative_to(root).as_posix(), "sha256": _sha256_file(path)})
    encoded = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _scrub_result_diagnostics(result: dict[str, Any]) -> dict[str, Any]:
    """Replace raw harness diagnostics with bounded scrubbed summaries and hashes."""
    for key in ("diagnostic", "diagnostics", "stderr"):
        raw = result.pop(key, None)
        if isinstance(raw, str) and raw:
            result[f"{key}_evidence"] = {
                "sha256": hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest(),
                "size_bytes": len(raw.encode("utf-8", errors="replace")),
            }
            result["diagnostic_summary" if key == "diagnostic" else f"{key}_summary"] = _safe_diagnostic_summary(raw)
    return result


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
    runtime_artifacts = context.get("runtime_artifacts", {})
    local_tarballs = runtime_artifacts.get("koishi_install") == "localized_tarballs"
    if local_tarballs:
        expected_local_lock = runtime_artifacts.get("localized_package_lock_sha256")
        localized_lock = Path("/input/project/sample/package-lock.json")
        if not isinstance(expected_local_lock, str) or _sha256_file(localized_lock) != expected_local_lock:
            raise RuntimeError("localized Koishi lock differs from its prepared input manifest")
        shutil.copy2("/input/project/sample/package.json", profile / "package.json")
        shutil.copy2(localized_lock, profile / "package-lock.json")
        shutil.copytree("/input/project/sample/tarballs", profile / "tarballs")
        cache = None
    else:
        archive = Path("/input/project/sample/npm-cache.tar.gz")
        if _sha256_file(archive) != context["npm_cache_sha256"]:
            raise RuntimeError("offline npm cache archive differs from its prepared asset report")
        with tarfile.open(archive, mode="r:gz") as stream:
            stream.extractall("/work", filter="data")
        shutil.copy2("/input/project/sample/package.json", profile / "package.json")
        shutil.copy2("/input/project/sample/package-lock.json", profile / "package-lock.json")
        cache = Path("/work/npm-cache")
        if not cache.is_dir():
            raise RuntimeError("offline npm cache archive has no npm-cache directory")
    shutil.copy2("/input/project/sample/artifact.bin", profile / "artifact.bin")
    npm_command = [
        "/usr/local/bin/node",
        "/usr/local/lib/node_modules/npm/bin/npm-cli.js",
        "ci",
        "--offline",
        "--ignore-scripts",
        "--no-audit",
        "--fund=false",
        "--strict-peer-deps",
        "--engine-strict",
    ]
    if cache is not None:
        npm_command.extend(["--cache", str(cache)])
    install = _run(npm_command, cwd=profile, timeout=600)
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

    expected_host_dist = runtime_artifacts.get("koishi_host_dist_sha256")
    if expected_host_dist is not None:
        bundled_host_dist = Path("/input/project/sample/koishi-host-dist").resolve(strict=True)
        if _tree_sha256(bundled_host_dist) != expected_host_dist:
            raise RuntimeError("current Koishi Host build differs from its bound runtime artifact hash")
        bundled_host_root = profile / "ligamina-koishi-host"
        if not bundled_host_root.is_symlink():
            raise RuntimeError("expected the profile Koishi Host link before applying the current build")
        bundled_host_root.unlink()
        bundled_host_root.mkdir(parents=True)
        shutil.copytree(bundled_host_dist, bundled_host_root / "dist")
        (bundled_host_root / "node_modules").symlink_to(host_root / "node_modules", target_is_directory=True)

        contract_source = Path("/input/project/contracts")
        required_contracts = (
            "v0.1/event-envelope.schema.json",
            "v0.1/platform-operation.schema.json",
            "v0.1/capability-descriptor.schema.json",
            "v0.2/command-descriptor.schema.json",
            "lab/v1/catalog.schema.json",
            "lab/v1/result.schema.json",
            "lab/v1/summary.schema.json",
            "lab/v1/run.schema.json",
        )
        missing_contracts = [name for name in required_contracts if not (contract_source / name).is_file()]
        if missing_contracts:
            raise RuntimeError("bundled Koishi Host contract schemas are incomplete")
        contract_root = Path("/work/contracts")
        if contract_root.exists() or contract_root.is_symlink():
            raise RuntimeError("Worker contract staging path already exists")
        contract_root.symlink_to(contract_source, target_is_directory=True)

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
    provider_modules = context.get("sample", {}).get("preload_plugins", [])
    if not isinstance(provider_modules, list) or any(not isinstance(item, str) or not item for item in provider_modules):
        raise ValueError("Koishi service profile has malformed preload plugin names")
    provider_url_by_module: dict[str, str] = {}
    for module_name in provider_modules:
        provider_resolver = _run(
            [
                "/usr/local/bin/node",
                "--input-type=module",
                "-e",
                f"console.log(import.meta.resolve({json.dumps(module_name)}))",
            ],
            cwd=profile,
            timeout=15,
        )
        if provider_resolver.returncode or not provider_resolver.stdout.strip().startswith("file:"):
            raise RuntimeError(f"could not resolve a locked Koishi service provider {module_name!r}: {provider_resolver.stderr[-1000:]}")
        provider_url_by_module[module_name] = provider_resolver.stdout.strip().splitlines()[-1]
    database_provider = _koishi_database_provider_module(
        context.get("service_profile", {}), provider_modules
    )
    context["database_provider_url"] = provider_url_by_module.get(database_provider)
    context["provider_urls"] = [
        url for module_name, url in provider_url_by_module.items() if module_name != database_provider
    ]
    context["host_environment"].update(
        {
            "LIGAMINA_NODE_EXECUTABLE": "/usr/local/bin/node",
            "LIGAMINA_CORE_EXECUTABLE": "/usr/local/bin/ligamina-cli",
        }
    )
    harness = profile / ".ligamina/koishi/runner.mjs"
    harness.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2("/input/project/lab/baselines/koishi/runner.mjs", harness)
    shutil.copy2(
        "/input/project/lab/baselines/koishi/artifact_fingerprint.mjs",
        harness.parent / "artifact_fingerprint.mjs",
    )
    shutil.copy2(
        "/input/project/lab/baselines/koishi/plugin_export.mjs",
        harness.parent / "plugin_export.mjs",
    )
    return {
        "status": "passed",
        "kind": "locked_npm_offline_install_local_tarballs" if local_tarballs else "locked_npm_offline_install",
        "lock_sha256": context["expected_environment"]["lockfile_sha256"],
        "npm_cache_sha256": context.get("npm_cache_sha256"),
        "localized_package_lock_sha256": runtime_artifacts.get("localized_package_lock_sha256"),
        "koishi_host_dist_sha256": expected_host_dist,
        "framework_version": "4.18.11",
        "host_lock_sha256": context["host_lock_sha256"],
        "provider_modules": provider_modules,
        "provider_urls": context["provider_urls"],
        "database_provider_url": context.get("database_provider_url"),
    }


def run_side(request: dict[str, Any]) -> dict[str, Any]:
    _assert_oracle_absent(request)
    context = request.get("context")
    if not isinstance(context, dict):
        raise ValueError("side request must contain a context object")
    mode = context.get("execution", {}).get("mode")
    if mode not in {"native", "federated", "tool"}:
        raise ValueError("side request does not select a registered execution mode")
    ecosystem = context.get("sample", {}).get("ecosystem")
    harness_timeout = context.get("execution", {}).get("harness_timeout_seconds", 180)
    if isinstance(harness_timeout, bool) or not isinstance(harness_timeout, int) or not 1 <= harness_timeout <= 180:
        raise ValueError("harness_timeout_seconds must be an integer from 1 to 180")
    setup_evidence = _prepare_profile(request)
    runtime_artifacts = context.get("runtime_artifacts", {})
    core_path = "/usr/local/bin/ligamina-cli"
    expected_core_hash = runtime_artifacts.get("core_cli_sha256")
    if expected_core_hash is not None:
        bundled_core = Path("/input/project/sample/ligamina-cli").resolve(strict=True)
        if _sha256_file(bundled_core) != expected_core_hash:
            raise RuntimeError("current Linux Core binary differs from its bound runtime artifact hash")
        core_path = str(bundled_core)
    context["repo_root"] = "/input/project"
    context["work_dir"] = "/work/profile"
    context["node_executable"] = "/usr/local/bin/node"
    context["execution"]["workspace"] = "/work/side"
    context["host_environment"].update(
        {
            "LIGAMINA_CORE_EXECUTABLE": core_path,
            "LIGAMINA_NODE_EXECUTABLE": "/usr/local/bin/node",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
        }
    )
    seed = context["execution"].get("random_seed")
    if isinstance(seed, int) and not isinstance(seed, bool) and seed >= 0:
        context["host_environment"].update({
            "TZ": "UTC",
            "PYTHONHASHSEED": str(seed % 2**32),
            "LIGAMINA_BEHAVIOR_RANDOM_SEED": str(seed),
        })
        if ecosystem == "koishi":
            context["host_environment"]["LIGAMINA_NODE_RANDOM_PRELOAD"] = "/input/project/lab/baselines/seed_node_random.cjs"
    Path("/work/side").mkdir(parents=True, exist_ok=True)
    if ecosystem == "nonebot":
        # Tool mode reuses the Federated harness: same Core/Host, MCP tool calls instead of events.
        runner = "/input/project/lab/baselines/nonebot/runner.py" if mode == "native" else "/input/project/lab/baselines/federated.py"
        command = [sys.executable, runner]
    else:
        command = [
            "/usr/local/bin/node",
            str(Path("/work/profile/.ligamina/koishi/runner.mjs") if mode == "native" else Path("/input/project/lab/baselines/federated.py")),
        ]
        if mode in {"federated", "tool"}:
            command = [sys.executable, "/input/project/lab/baselines/federated.py"]
    result = execute_harness(command, request, mode, harness_timeout, cwd=Path("/work/side"))
    result = _scrub_result_diagnostics(result)
    result["profile_setup"] = setup_evidence
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    request = parser.add_mutually_exclusive_group(required=True)
    request.add_argument("--request-json")
    request.add_argument("--request-stdin", action="store_true")
    options = parser.parse_args()
    try:
        value = json.load(sys.stdin) if options.request_stdin else json.loads(options.request_json)
        result = run_side(value)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as error:  # Worker boundary converts setup faults into explicit attempt output.
        diagnostic = str(error)
        print(json.dumps({
            "status": "failed",
            "reason_code": "worker_side_setup_failed",
            "diagnostic_summary": _safe_diagnostic_summary(diagnostic),
            "diagnostic_sha256": hashlib.sha256(diagnostic.encode("utf-8", errors="replace")).hexdigest(),
        }, ensure_ascii=False))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
