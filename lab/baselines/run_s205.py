"""Prepare and execute the four supported-plugin pairs in Linux Docker Workers."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from lab.representative import (
    discover_python_entry,
    download_and_verify,
    install_sample,
    validate_manifest,
)
from lab.workers import DockerWorker, WorkerLimits, WorkerSpec


ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = ROOT / "outputs/acceptance/phase2-s205-20261012.json"
SUPPORTED_PATH = ROOT / "outputs/acceptance/t18-final-audit-20261012.json"
RUNTIME_LOCK_PATH = ROOT / "lab/baselines/runtime-images.lock.json"
NATIVE_FILES = (
    "__init__.py",
    "compare.py",
    "native_runner.py",
    "representative.py",
)


def _runtime_images() -> dict[str, Any]:
    lock = json.loads(RUNTIME_LOCK_PATH.read_text(encoding="utf-8"))
    if lock.get("task_id") != "S2-05" or lock.get("platform") != "linux/amd64":
        raise ValueError("S2-05 runtime image lock has the wrong task or platform")
    return lock


def _copy(path: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, destination)


def _copytree(source: Path, destination: Path) -> None:
    shutil.copytree(source, destination, dirs_exist_ok=True, symlinks=False)


def _ignore_links(directory: str, names: list[str]) -> list[str]:
    return [name for name in names if (Path(directory) / name).is_symlink() or (Path(directory) / name).is_junction()]


def _copy_sample_profile(work: Path, destination: Path, ecosystem: str) -> None:
    ignored = shutil.ignore_patterns(".venv") if ecosystem == "nonebot" else _ignore_node_links
    shutil.copytree(work, destination, dirs_exist_ok=True, symlinks=False, ignore=ignored)
    if ecosystem == "nonebot":
        site_packages = work / ".venv/lib/python3.12/site-packages"
        target = destination / ".venv/lib/python3.12/site-packages"
        if not site_packages.is_dir():
            raise FileNotFoundError(f"Linux Python profile site-packages is missing: {site_packages}")
        shutil.copytree(site_packages, target, dirs_exist_ok=True, symlinks=False, ignore=_ignore_links)


def _ignore_node_links(directory: str, names: list[str]) -> list[str]:
    links = set(_ignore_links(directory, names))
    return [name for name in names if name == ".bin" or name in links]


def _prepare_payload(sample: dict[str, Any], fixture: dict[str, Any], entry: str, artifact: Path, work: Path) -> dict[str, Any]:
    fields = (
        "sample_id",
        "ecosystem",
        "package",
        "version",
        "artifact_url",
        "artifact_hash_algorithm",
        "artifact_hash",
        "source_url",
        "license",
        "category",
        "entry_module",
        "entry_point",
        "preload_plugins",
    )
    selected = {key: sample[key] for key in fields if key in sample}
    selected["_artifact_hash_algorithm"] = sample["_artifact_hash_algorithm"]
    selected["_artifact_hash"] = sample["_artifact_hash"]
    selected["_entry"] = entry
    payload = {
        "sample_id": sample["sample_id"],
        "sample": selected,
        "fixture": fixture,
        "entry": entry,
        "artifact_hash_algorithm": sample["_artifact_hash_algorithm"],
        "artifact_hash": sample["_artifact_hash"],
        "account_id": "9000",
        "artifact_path": str(artifact),
        "work_path": str(work),
    }
    return payload


def _extract_runtime_assets(destination: Path, runtime_image: str) -> dict[str, str]:
    docker = shutil.which("docker") or shutil.which("docker.exe")
    if not docker:
        raise RuntimeError("Docker CLI is required to extract the pinned S2-05 runtime assets")
    destination.mkdir(parents=True, exist_ok=True)
    node = destination / "node"
    core = destination / "ligamina-cli"
    created = subprocess.run(
        [docker, "create", runtime_image], capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120, check=False,
    )
    if created.returncode:
        raise RuntimeError(f"could not create the locked runtime asset container: {created.stderr[-2000:]}")
    container_id = created.stdout.strip()
    try:
        for source, target in (("/usr/local/bin/node", node), ("/usr/local/bin/ligamina-cli", core)):
            copied = subprocess.run(
                [docker, "cp", f"{container_id}:{source}", str(target)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, check=False,
            )
            if copied.returncode:
                raise RuntimeError(f"could not extract {source}: {copied.stderr[-2000:]}")
    finally:
        subprocess.run([docker, "rm", container_id], capture_output=True, timeout=60, check=False)
    return {
        "node_sha256": hashlib.sha256(node.read_bytes()).hexdigest(),
        "core_sha256": hashlib.sha256(core.read_bytes()).hexdigest(),
    }


def _prepare_project(
    project: Path, work: Path, payload: dict[str, Any], *, ecosystem: str, runtime_assets: Path
) -> None:
    project.mkdir(parents=True)
    _copy_sample_profile(work, project / "sample", ecosystem)

    source_lab = ROOT / "lab"
    lab_target = project / "lab"
    lab_target.mkdir()
    for name in NATIVE_FILES:
        source = source_lab / name
        if source.is_file():
            _copy(source, lab_target / name)
    _copytree(source_lab / "baselines", lab_target / "baselines")

    _copytree(ROOT / "contracts", project / "contracts")
    _copytree(ROOT / "hosts/nonebot/ligamina_nonebot", project / "hosts/nonebot/ligamina_nonebot")
    _copy(ROOT / "samples/profiles/nonebot-group-v0.1.json", project / "samples/profiles/nonebot-group-v0.1.json")
    _copy(ROOT / "samples/profiles/koishi-group-v0.1.json", project / "samples/profiles/koishi-group-v0.1.json")

    runtime = project / "runtime"
    _copy(runtime_assets / "node", runtime / "node/node")
    _copy(runtime_assets / "ligamina-cli", runtime / "ligamina-cli")
    bootstrap = """#!/bin/sh
set -eu
mkdir -p /work/project
tar --exclude=./sample/node_modules \\
    --exclude=./sample/.venv/lib/python3.12/site-packages \\
    -C /input/project -cf - . | tar -C /work/project -xf -
if [ -d /input/project/sample/node_modules ]; then
    ln -s /input/project/sample/node_modules /work/project/sample/node_modules
fi
if [ -d /input/project/sample/.venv/lib/python3.12/site-packages ]; then
    mkdir -p /work/project/sample/.venv/lib/python3.12
    ln -s /input/project/sample/.venv/lib/python3.12/site-packages \
        /work/project/sample/.venv/lib/python3.12/site-packages
fi
chmod 0755 /work/project/runtime/node/node /work/project/runtime/ligamina-cli
cd /work/project
exec python lab/baselines/paired_worker.py --payload sample/payload.json --timeout 180
"""
    (project.parent / "bootstrap.sh").write_text(bootstrap, encoding="utf-8", newline="\n")
    (project / "sample/payload.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if ecosystem == "nonebot" and (fixture_config := payload["fixture"].get("nonebot_config")):
        dotenv = [
            f"{key}={json.dumps(value, ensure_ascii=False, separators=(',', ':'))}"
            for key, value in sorted(fixture_config.items())
        ]
        (project / "sample/.env.prod").write_text("\n".join(dotenv) + "\n", encoding="utf-8")


def _provision_linux_profile(sample: dict[str, Any], work: Path, images: dict[str, Any]) -> dict[str, Any]:
    docker = shutil.which("docker") or shutil.which("docker.exe")
    if not docker:
        return {"status": "environment_missing", "error": "Docker CLI is unavailable"}
    image = images["profile_images"][sample["ecosystem"]]
    command = (
        ("sh", "-lc", "mkdir -p /tmp/home && rm -rf .venv && uv sync --locked --no-dev --no-build --python 3.12.14")
        if sample["ecosystem"] == "nonebot"
        else ("npm", "ci", "--ignore-scripts", "--no-audit", "--fund=false")
    )
    source = str(work.resolve()).replace("\\", "/")
    if "," in source:
        raise ValueError("Docker profile path cannot contain commas")
    argv = [
        docker, "run", "--rm", "--pull=never", "--network", "bridge", "--read-only",
        "--cap-drop=ALL", "--security-opt=no-new-privileges:true", "--pids-limit", "256",
        "--memory", "6144m", "--memory-swap", "6144m", "--cpus", "2",
        "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=1024m",
        "--mount", f"type=bind,src={source},dst=/profile", "--workdir", "/profile",
        "--env", "HOME=/tmp/home", "--env", "UV_CACHE_DIR=/tmp/uv-cache",
        "--env", "npm_config_cache=/tmp/npm-cache", image, *command,
    ]
    started = time.monotonic()
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1200, check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "failed", "command": argv, "elapsed_seconds": round(time.monotonic() - started, 3),
            "error": "Linux profile provisioning timed out after 1200s",
        }
    return {
        "status": "passed" if completed.returncode == 0 else "failed",
        "command": argv,
        "returncode": completed.returncode,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "stdout_tail": completed.stdout[-4000:],
        "stderr_tail": completed.stderr[-4000:],
        "install_scripts": "disabled" if sample["ecosystem"] == "koishi" else "Python build backends disabled",
    }


def _cleanup_linux_profile(sample: dict[str, Any], work: Path, images: dict[str, Any]) -> dict[str, Any]:
    docker = shutil.which("docker") or shutil.which("docker.exe")
    if not docker:
        return {"status": "environment_missing", "error": "Docker CLI is unavailable"}
    image = images["profile_images"][sample["ecosystem"]]
    source = str(work.resolve()).replace("\\", "/")
    command = [
        docker, "run", "--rm", "--pull=never", "--network", "none", "--read-only", "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true", "--mount", f"type=bind,src={source},dst=/profile",
        "--workdir", "/profile", image, "sh", "-lc", "rm -rf .venv node_modules",
    ]
    started = time.monotonic()
    completed = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, check=False,
    )
    return {
        "status": "passed" if completed.returncode == 0 else "failed",
        "command": command,
        "returncode": completed.returncode,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "stderr_tail": completed.stderr[-2000:],
    }


def _supported_ids() -> set[str]:
    acceptance = json.loads(SUPPORTED_PATH.read_text(encoding="utf-8"))
    return {
        f"{item['ecosystem']}:{item['package']}@{item['version']}"
        for item in acceptance["supported_plugins"]
    }


def _supported_sample_ids(manifest_path: Path) -> set[str]:
    required = _supported_ids()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    matched = {
        item["sample_id"]
        for item in manifest["samples"]
        if f"{item['ecosystem']}:{item['package']}@{item['version']}" in required
    }
    if len(matched) != len(required):
        raise ValueError("T18 supported sample versions do not all resolve to manifest entries")
    return matched


def _run_sample(
    sample: dict[str, Any], fixture: dict[str, Any], artifact: Path, sample_work: Path, work: Path,
    *, images: dict[str, Any], runtime_assets: Path,
) -> dict[str, Any]:
    sample["fixture"] = fixture
    if sample["ecosystem"] == "nonebot":
        entry = discover_python_entry(sample, artifact)
    else:
        entry = sample.get("entry_point", ".")
    profile, _ = install_sample(sample, sample_work, work / "shared-cache")
    if profile["status"] != "passed":
        return {
            "sample_id": sample["sample_id"],
            "status": "failed",
            "stage": "profile_prepare",
            "profile": profile,
        }
    linux_profile = _provision_linux_profile(sample, sample_work, images)
    if linux_profile["status"] != "passed":
        return {
            "sample_id": sample["sample_id"], "status": "failed", "stage": "linux_profile_prepare",
            "profile": profile, "linux_profile_prepare": linux_profile,
        }

    bundle_root = work / f"bundle-{sample['sample_id']}"
    input_dir = bundle_root / "input"
    project = input_dir / "project"
    payload = _prepare_payload(sample, fixture, entry, sample_work / "artifact.bin", sample_work)
    _prepare_project(project, sample_work, payload, ecosystem=sample["ecosystem"], runtime_assets=runtime_assets)

    worker = DockerWorker(image_digest=images["worker_image"], repository_root=ROOT)
    limits = WorkerLimits(
        timeout_seconds=180,
        memory_mb=6144,
        cpu_cores=2,
        process_limit=256,
        output_limit_bytes=4 * 1024 * 1024,
    )
    spec = WorkerSpec(
        run_id=f"phase2-s205-{sample['sample_id']}",
        entry_id=sample["sample_id"],
        command=("sh", "/input/bootstrap.sh"),
        input_dir=input_dir,
        limits=limits,
    )
    started = time.monotonic()
    result = worker.run(spec)
    paired = None
    parse_error = None
    if result.stdout:
        try:
            paired = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            parse_error = f"{type(error).__name__}: {error}"
    status = "passed" if result.status == "passed" and paired and paired.get("comparison", {}).get("status") == "passed" else "failed"
    return {
        "sample_id": sample["sample_id"],
        "ecosystem": sample["ecosystem"],
        "package": sample["package"],
        "version": sample["version"],
        "artifact_hash": sample["_artifact_hash"],
        "profile_prepare": profile,
        "linux_profile_prepare": linux_profile,
        "status": status,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "worker": {
            "worker_id": result.worker_id,
            "status": result.status,
            "reason_code": result.reason_code,
            "exit_code": result.exit_code,
            "cleanup_confirmed": result.cleanup_confirmed,
            "stdout_sha256": result.stdout_sha256,
            "stderr_sha256": result.stderr_sha256,
            "output_truncated": result.output_truncated,
            "stderr_tail": result.stderr.decode("utf-8", errors="replace")[-4000:],
        },
        "parse_error": parse_error,
        "pair": paired,
    }


def run(sample_ids: set[str] | None = None) -> dict[str, Any]:
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    manifest_path = ROOT / "samples/third-party.lock.json"
    selected_sample_ids = sample_ids or _supported_sample_ids(manifest_path)
    manifest, samples = validate_manifest(manifest_path, selected_sample_ids)
    required = _supported_ids()
    selected = {
        f"{sample['ecosystem']}:{sample['package']}@{sample['version']}"
        for sample in samples
    }
    if sample_ids is None and not required.issubset(selected):
        raise ValueError("all four previously supported sample versions are required")
    images = _runtime_images()

    existing = {item["sample_id"]: item for item in report.get("implementation", {}).get("samples", [])}
    with tempfile.TemporaryDirectory(prefix="ligamina-s205-") as directory:
        temp_root = Path(directory)
        runtime_assets = temp_root / "runtime-assets"
        runtime_hashes = _extract_runtime_assets(runtime_assets, images["runtime_artifact_image"])
        results = list(existing.values())
        for sample in samples:
            fixture_path = (manifest_path.parent / sample["fixture_path"]).resolve()
            fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
            sample_work = temp_root / sample["sample_id"]
            sample_work.mkdir()
            artifact = sample_work / "artifact.bin"
            l0 = download_and_verify(sample, artifact)
            if l0["status"] != "passed":
                record = {"sample_id": sample["sample_id"], "status": "failed", "stage": "artifact_download", "artifact": l0}
            else:
                try:
                    record = _run_sample(
                        sample, fixture, artifact, sample_work, temp_root,
                        images=images, runtime_assets=runtime_assets,
                    )
                except Exception as error:
                    record = {
                        "sample_id": sample["sample_id"], "status": "failed", "stage": "runner_exception",
                        "error": f"{type(error).__name__}: {error}",
                    }
                record["artifact_verification"] = l0
            cleanup = _cleanup_linux_profile(sample, sample_work, images)
            record["profile_source_cleanup"] = cleanup
            if cleanup["status"] != "passed":
                record["status"] = "failed"
                record["cleanup_error"] = cleanup
            results = [item for item in results if item["sample_id"] != sample["sample_id"]]
            results.append(record)
            report["implementation"] = {
                "approach": "unmodified plugin artifact in one exact Linux sample lock, run against stock native framework and production Core/Host/Gateway in a network-disabled Docker Worker",
                "platform": "linux/amd64",
                "runtime_images_lock": "lab/baselines/runtime-images.lock.json",
                "runtime_artifacts": runtime_hashes,
                "windows_sandbox_blocker": {
                    "status": "blocked_before_plugin_start",
                    "win32_error": 4551,
                    "reason": "Windows Sandbox App Control policy blocked the unsigned CPython executable; the failure occurred before the sample runner started.",
                    "evidence": "outputs/acceptance/phase2-s205-windows-blocker-20261012.json",
                },
                "native_paths": {
                    "nonebot": "stock NoneBot 2.5.0 with official OneBot v11 adapter 2.4.6; inject native OneBot Event through Bot.handle_event and capture Bot API sends",
                    "koishi": "stock Koishi core 4.18.11 Context/Session/Bot; dispatch native Session and capture Bot.sendMessage",
                },
                "files": [
                    "lab/baselines/nonebot/runner.py",
                    "lab/baselines/koishi/runner.mjs",
                    "lab/baselines/federated.py",
                    "lab/baselines/paired_worker.py",
                    "lab/native_runner.py",
                ],
                "samples": results,
            }
            report["status"] = "in-progress"
            REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report["implementation"]["samples"] = results
    report["status"] = "completed" if len(results) == 4 and all(item.get("status") == "passed" for item in results) else "in-progress"
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-id", action="append", default=[])
    options = parser.parse_args()
    report = run(set(options.sample_id) or None)
    print(json.dumps({
        "status": report["status"],
        "samples": [
        {"sample_id": item["sample_id"], "status": item["status"], "comparison": (item.get("pair") or {}).get("comparison", {}).get("status")}
            for item in report.get("implementation", {}).get("samples", [])
        ],
        "report": str(REPORT_PATH),
    }, ensure_ascii=False))
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
