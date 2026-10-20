"""Runtime admission evidence for the pinned Linux Docker Worker backend."""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .selection import ROOT, AutomationContractError, object_sha256, sha256


def _docker_facts(docker_cli: str | None) -> dict[str, Any]:
    if not docker_cli:
        return {"available": False, "reason_code": "docker_cli_unavailable"}
    try:
        result = subprocess.run(
            [docker_cli, "info", "--format", "{{json .}}"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"available": False, "reason_code": "docker_daemon_unavailable"}
    if result.returncode != 0:
        return {"available": False, "reason_code": "docker_daemon_unavailable"}
    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"available": False, "reason_code": "docker_info_invalid"}
    return {
        "available": True,
        "server_version": info.get("ServerVersion"),
        "operating_system": info.get("OperatingSystem"),
        "architecture": info.get("Architecture"),
        "cgroup_version": info.get("CgroupVersion"),
        "cgroup_driver": info.get("CgroupDriver"),
        "security_options": sorted(info.get("SecurityOptions", [])),
        "mem_total_bytes": info.get("MemTotal"),
        "n_cpu": info.get("NCPU"),
    }


def collect_linux_health(*, probe_report_path: Path | None = None) -> dict[str, Any]:
    """Collect current Linux backend facts; admit only after exact pinned probes pass."""
    if platform.system() != "Linux":
        raise AutomationContractError("Linux admission can only be collected on Linux")
    image_lock_path = ROOT / "deploy/lab/linux/worker-image.lock.json"
    image_lock = json.loads(image_lock_path.read_text(encoding="utf-8"))
    docker_cli = shutil.which("docker") or shutil.which("docker.exe")
    docker = _docker_facts(docker_cli)
    image: dict[str, Any] = {"locked_reference": image_lock.get("image"), "lock_sha256": sha256(image_lock_path.read_bytes()), "present": False}
    failures: list[str] = []
    if not docker_cli:
        failures.append("docker_cli_unavailable")
    elif not docker.get("available"):
        failures.append(str(docker.get("reason_code", "docker_daemon_unavailable")))
    else:
        inspect = subprocess.run(
            [docker_cli, "image", "inspect", image_lock["image"], "--format", "{{.Id}}"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20, check=False,
        )
        image["present"] = inspect.returncode == 0
        image["image_id"] = inspect.stdout.strip() if inspect.returncode == 0 else None
        if inspect.returncode != 0:
            failures.append("pinned_worker_image_unavailable")
    probe: dict[str, Any] = {"status": "not_run", "report_path": None, "report_sha256": None}
    if probe_report_path is None:
        failures.append("benign_probe_report_missing")
    else:
        report_path = Path(probe_report_path).resolve(strict=True)
        raw = report_path.read_bytes()
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AutomationContractError(f"invalid benign probe report: {report_path}") from error
        results = value.get("results", [])
        results_valid = isinstance(results, list) and bool(results) and all(
            isinstance(item, dict) and item.get("passed") is True and item.get("cleanup_confirmed") is True
            for item in results
        )
        probe = {"status": "passed" if value.get("passed") is True and results_valid else "failed", "report_path": str(report_path), "report_sha256": sha256(raw), "worker_image": value.get("worker_image"), "result_count": len(results) if isinstance(results, list) else 0}
        if probe["status"] != "passed" or probe["worker_image"] != image_lock["image"]:
            failures.append("benign_probe_report_not_accepted")
    host = {"system": platform.system(), "release": platform.release(), "machine": platform.machine(), "python": platform.python_version()}
    config = json.loads((ROOT / "config/lab/phase3-automation-v1.json").read_text(encoding="utf-8"))
    available_memory = None
    for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
        if line.startswith("MemAvailable:"):
            available_memory = int(line.split()[1]) * 1024
            break
    scratch_free = shutil.disk_usage(ROOT).free
    worker_memory = config["linux_worker"]["default_limits"]["memory_bytes"]
    required_memory = worker_memory * config["campaign_policy"]["initial_linux_concurrency"] + config["campaign_policy"]["admission_memory_headroom_bytes"]
    resources = {"memory_available_bytes": available_memory, "memory_required_bytes": required_memory, "scratch_free_bytes": scratch_free, "scratch_minimum_bytes": config["campaign_policy"]["minimum_free_scratch_bytes"]}
    if available_memory is None or available_memory < required_memory:
        failures.append("linux_memory_headroom_insufficient")
    if scratch_free < config["campaign_policy"]["minimum_free_scratch_bytes"]:
        failures.append("linux_scratch_headroom_insufficient")
    facts = {"backend": "docker", "host": host, "docker": docker, "image": image, "probe": probe, "resources": resources}
    health = {
        "schema_version": 1,
        "kind": "phase3_worker_admission",
        "collected_at_epoch": int(time.time()),
        "platform": "linux",
        "status": "admitted" if not failures else "blocked",
        "reason_codes": sorted(set(failures)),
        "capabilities": {"claim_allowed": not failures, "backend": "docker", "network_disabled": not failures, "cleanup_evidence_required": True},
        "facts": facts,
        "environment_fingerprint": object_sha256(facts),
    }
    return health


def require_current_admission(health: dict[str, Any], current_fingerprint: str) -> None:
    """Fail closed if a saved admission is blocked or its environment changed."""
    if health.get("status") != "admitted" or health.get("capabilities", {}).get("claim_allowed") is not True:
        raise AutomationContractError("worker backend has no successful admission")
    if health.get("environment_fingerprint") != current_fingerprint:
        raise AutomationContractError("worker admission expired because its environment fingerprint changed")
