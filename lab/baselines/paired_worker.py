"""Run one installed plugin's stock and federated path inside an OS Worker."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from lab.native_runner import run_pair
from lab.representative import HOST_VERSIONS, isolated_environment, test_events

def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def artifact_hash(path: Path, algorithm: str) -> str:
    if algorithm == "sha256":
        return sha256(path)
    if algorithm == "sri-sha512":
        import base64

        return "sha512-" + base64.b64encode(hashlib.sha512(path.read_bytes()).digest()).decode("ascii")
    raise ValueError(f"unsupported artifact digest algorithm: {algorithm}")


def resolve_koishi_entry(sample: dict[str, Any], work: Path, node: str) -> str:
    resolver = work / "resolve-plugin.mjs"
    if not resolver.is_file():
        resolver.write_text("console.log(import.meta.resolve(process.argv[2]));\n", encoding="utf-8")
    process = subprocess.run(
        [node, str(resolver), sample["package"]],
        cwd=work,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    if process.returncode:
        raise RuntimeError(f"could not resolve installed Koishi plugin: {process.stderr[-1500:]}")
    return process.stdout.strip().splitlines()[-1]


def installed_framework(sample: dict[str, Any], work: Path, node: str, python: str) -> tuple[str, str]:
    if sample["ecosystem"] == "nonebot":
        env = isolated_environment(work / "fingerprint-home")
        site_packages = work / ".venv" / ("Lib" if os.name == "nt" else "lib") / "python3.12" / "site-packages"
        env["PYTHONPATH"] = str(site_packages)
        process = subprocess.run(
            [python, "-c", "import importlib.metadata; print(importlib.metadata.version('nonebot2'))"],
            cwd=work,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
        if process.returncode:
            raise RuntimeError(f"could not read NoneBot profile version: {process.stderr[-1000:]}")
        return process.stdout.strip(), ".".join(map(str, sys.version_info[:3]))
    package_lock = json.loads((work / "package-lock.json").read_text(encoding="utf-8"))
    framework = package_lock.get("packages", {}).get("node_modules/@koishijs/core", {}).get("version")
    node_run = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10, check=False)
    if node_run.returncode:
        raise RuntimeError("could not read the bundled Node runtime version")
    return framework, node_run.stdout.strip().removeprefix("v")


def _copy_source_tree(source: Path, target: Path) -> None:
    shutil.copytree(source, target, dirs_exist_ok=True)


def build_case(
    project: Path, payload: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], Path, list[str], list[str]]:
    sample = payload["sample"]
    work = project / "sample"
    sample["fixture"] = payload["fixture"]
    sample["_entry"] = payload["entry"]
    sample["_artifact_hash"] = payload["artifact_hash"]
    sample["_artifact_hash_algorithm"] = payload["artifact_hash_algorithm"]
    artifact = work / "artifact.bin"
    if artifact_hash(artifact, sample["_artifact_hash_algorithm"]) != sample["_artifact_hash"]:
        raise RuntimeError("bundled artifact bytes differ from the official digest")

    python_names = ("python.exe", "python") if os.name == "nt" else ("python", "python3")
    node_names = ("node.exe", "node") if os.name == "nt" else ("node",)
    python = next(
        (str(path) for name in python_names if (path := project / "runtime" / "python" / name).is_file()),
        shutil.which("python") or shutil.which("python3") or sys.executable,
    )
    node = next(
        (str(path) for name in node_names if (path := project / "runtime" / "node" / name).is_file()),
        shutil.which("node") or "node",
    )
    if not Path(python).is_file() or not (Path(node).is_file() or shutil.which(node)):
        raise FileNotFoundError("the pinned Python and Node runtimes must be available in the worker")
    module_url = resolve_koishi_entry(sample, work, node) if sample["ecosystem"] == "koishi" else ""
    if sample["ecosystem"] == "koishi":
        sample["_entry_url"] = module_url

    env = isolated_environment(work / "paired-cache", work / "shared-cache")
    env["LIGAMINA_PYTHON_EXECUTABLE"] = python
    env["LIGAMINA_NODE_EXECUTABLE"] = node
    site_packages = work / ".venv" / ("Lib" if os.name == "nt" else "lib") / "python3.12" / "site-packages"
    env["LIGAMINA_PROFILE_SITE_PACKAGES"] = str(site_packages)
    env["PYTHONPATH"] = os.pathsep.join((str(project / "hosts" / "nonebot"), env["LIGAMINA_PROFILE_SITE_PACKAGES"]))
    env["LIGAMINA_CORE_EXECUTABLE"] = str(project / "runtime" / ("ligamina-cli.exe" if os.name == "nt" else "ligamina-cli"))
    env["PATH"] = os.pathsep.join((str(project / "runtime" / "node"), str(project / "runtime" / "python"), env.get("PATH", "")))
    sample_environment = payload["fixture"].get("environment", {})
    for key, value in sample_environment.items():
        if not key.startswith("LIGAMINA_SAMPLE_") or not isinstance(value, str):
            raise ValueError(f"invalid fixture environment override: {key!r}")
        env[key] = value

    config = {
        "ecosystem": sample["ecosystem"],
        "package": sample["package"],
        "version": sample["version"],
        "entry": sample["_entry"],
        "preload_plugins": sample.get("preload_plugins", []),
        "host_profile": HOST_VERSIONS[sample["ecosystem"]],
        "nonebot_config": payload["fixture"].get("nonebot_config", {}),
        "plugin_config": payload["fixture"].get("plugin_config", {}),
        "environment": sample_environment,
    }
    events = test_events(sample)
    stimulus = {"events": events}
    lockfile = work / ("uv.lock" if sample["ecosystem"] == "nonebot" else "package-lock.json")
    profile = project / "samples" / "profiles" / f"{sample['ecosystem']}-group-v0.1.json"
    framework, runtime = installed_framework(sample, work, node, python)
    expected_environment = {
        "ecosystem": sample["ecosystem"],
        "framework_version": framework,
        "runtime_version": runtime,
        "lockfile_sha256": sha256(lockfile),
        "profile_sha256": sha256(profile),
        "artifact_hash": sample["_artifact_hash"],
        "config_sha256": hashlib.sha256(canonical(config)).hexdigest(),
        "stimulus_sha256": hashlib.sha256(canonical(stimulus)).hexdigest(),
    }
    expected_texts = payload["fixture"].get("expected_outgoing_texts")
    if not isinstance(expected_texts, list) or not all(isinstance(text, str) for text in expected_texts):
        raise ValueError("S2-05 supported sample fixture must provide expected_outgoing_texts")
    expected_target = payload["fixture"].get("expected_target") or payload["fixture"].get("conversation")
    operations = []
    receipts = []
    for index, text in enumerate(expected_texts, start=1):
        operation_id = f"operation-{index}"
        target = {"kind": expected_target["kind"], "id": str(expected_target["id"])}
        operations.append({
            "operation_id": operation_id,
            "account_id": str(payload.get("account_id", "9000")),
            "operation": "send_message",
            "target": target,
            "params": {"message": [{"type": "text", "text": text}]},
        })
        receipts.append({"operation_id": operation_id, "status": "succeeded", "target": target})

    expected = {
        "environment": expected_environment,
        "events": events,
        "operations": operations,
        "operation_receipts": receipts,
        "cleanup": {"active_capability_count": 0, "live_processes": []},
    }
    context = {
        "repo_root": str(project),
        "work_dir": str(work),
        "site_packages": str(site_packages),
        "node_executable": node,
        "sample": sample,
        "fixture": payload["fixture"],
        "account_id": str(payload.get("account_id", "9000")),
        "config": config,
        "sample_environment": sample_environment,
        "host_environment": env,
        "koishi_entry_url": module_url,
        "expected_environment": expected_environment,
        "fingerprint_paths": {
            "artifact": str(artifact),
            "lockfile": str(lockfile),
            "profile": str(profile),
        },
    }
    case = {"case_id": payload["sample_id"], "stimulus": stimulus, "expected": expected, "context": context}
    harness_dir = work / ".ligamina"
    harness_dir.mkdir(exist_ok=True)
    _copy_source_tree(ROOT / "lab" / "baselines", harness_dir)
    native_command = (
        [python, str(harness_dir / "nonebot" / "runner.py")]
        if sample["ecosystem"] == "nonebot"
        else [node, str(harness_dir / "koishi" / "runner.mjs")]
    )
    federated_command = [python, str(harness_dir / "federated.py")]
    return case, context, work, native_command, federated_command


def execute_payload(project: Path, payload_path: Path, timeout_seconds: float = 180) -> dict[str, Any]:
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    case, context, work, native_command, federated_command = build_case(project, payload)
    report = run_pair(case, native_command, federated_command, timeout_seconds, cwd=work)
    native_path = report["native"].get("stock_native", {})
    if report["native"].get("status") == "completed" and (
        native_path.get("bridge_modules")
        or native_path.get("bridge_adapter_imported") is True
    ):
        raise RuntimeError("stock baseline imported the Ligamina Bridge")
    report.update({
        "sample_id": payload["sample_id"],
        "package": payload["sample"]["package"],
        "version": payload["sample"]["version"],
        "artifact_hash": payload["artifact_hash"],
        "lockfile_sha256": context["expected_environment"]["lockfile_sha256"],
        "profile_sha256": context["expected_environment"]["profile_sha256"],
        "same_artifact_lock_config_stimulus": {
            "artifact_hash": context["expected_environment"]["artifact_hash"],
            "lockfile_sha256": context["expected_environment"]["lockfile_sha256"],
            "config_sha256": context["expected_environment"]["config_sha256"],
            "stimulus_sha256": context["expected_environment"]["stimulus_sha256"],
        },
        "federated_trace": report["federated"].get("federated_trace"),
        "worker_cleanup_confirmed": True,
    })
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=180)
    options = parser.parse_args()
    project = Path(__file__).resolve().parents[2]
    report = execute_payload(project, options.payload, options.timeout)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["comparison"]["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
