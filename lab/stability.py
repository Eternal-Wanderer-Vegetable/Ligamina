"""Exercise repeated Host recovery and a combined Core + Host stability window."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
if sys.path and Path(sys.path[0]).resolve() == SCRIPT_DIR:
    sys.path.pop(0)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import queue

try:
    from lab import run as harness
    from lab.report import summarize_cases
except ModuleNotFoundError:  # supports `python lab/stability.py`
    import run as harness
    from report import summarize_cases


HOSTS = ("nonebot-smoke", "koishi-smoke")
CAPABILITIES = {
    "nonebot-smoke": harness.NONEBOT_CAPABILITY,
    "koishi-smoke": harness.KOISHI_CAPABILITY,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def read_rss_bytes(pid: int) -> int | None:
    if os.name != "nt":
        try:
            for line in Path(f"/proc/{pid}/status").read_text(encoding="ascii").splitlines():
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
        except (OSError, ValueError, IndexError):
            return None
        return None

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    process_query_limited_information = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    psapi.GetProcessMemoryInfo.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ProcessMemoryCounters),
        ctypes.c_ulong,
    ]
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return None
    try:
        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            return None
        return int(counters.WorkingSetSize)
    finally:
        kernel32.CloseHandle(handle)


def host_record(status: dict[str, Any], host_id: str) -> dict[str, Any] | None:
    return next((host for host in status.get("hosts", []) if host.get("id") == host_id), None)


def capability_records(api: harness.Api, base_url: str, token: str) -> list[dict[str, Any]]:
    _, result = api.request(base_url, "GET", "/v1/capabilities", token=token, timeout=5)
    return result.get("capabilities", [])


def wait_for_status(api: harness.Api, base_url: str, admin_token: str, predicate, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        _, last = api.request(base_url, "GET", "/v1/status", token=admin_token, timeout=5)
        if predicate(last):
            return last
        time.sleep(0.05)
    return last


def wait_for_capabilities(api: harness.Api, base_url: str, token: str, predicate, timeout: float) -> list[dict[str, Any]] | None:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = capability_records(api, base_url, token)
        if predicate(last):
            return last
        time.sleep(0.05)
    return None


def check_capability(api: harness.Api, base_url: str, token: str, capability_id: str) -> Any:
    _, result = api.request(
        base_url,
        "POST",
        "/v1/invocations",
        token=token,
        body={"capability_id": capability_id, "arguments": {"left": 19, "right": 23}},
        timeout=10,
    )
    return result.get("result")


def create_process_report_queue(stream, target: queue.Queue[str]) -> None:
    harness.start_reader(stream, target)


def sample_processes(daemon_pid: int, status: dict[str, Any], capabilities: list[dict[str, Any]], elapsed: float) -> dict[str, Any]:
    by_host = {host.get("id"): host for host in status.get("hosts", [])}
    components: dict[str, Any] = {"core": {"pid": daemon_pid, "rss_bytes": read_rss_bytes(daemon_pid)}}
    for host_id in HOSTS:
        host = by_host.get(host_id, {})
        pid = host.get("pid")
        components[host_id] = {
            "pid": pid,
            "rss_bytes": read_rss_bytes(int(pid)) if pid else None,
            "state": host.get("state"),
            "restart_count": host.get("restart_count"),
        }
    return {
        "at": utc_now(),
        "elapsed_seconds": round(elapsed, 3),
        "components": components,
        "registered_capability_count": len(capabilities),
        "registered_capabilities": sorted(
            (
                {"host_id": item.get("host_id"), "capability_id": item.get("descriptor", {}).get("capability_id")}
                for item in capabilities
            ),
            key=lambda item: (item["host_id"] or "", item["capability_id"] or ""),
        ),
    }


def resource_budget(
    samples: list[dict[str, Any]],
    duration_seconds: float,
    warmup_seconds: float,
    interval_seconds: float,
) -> dict[str, Any]:
    if duration_seconds < warmup_seconds + 3600 or warmup_seconds < 1800:
        return {"status": "skipped", "reason": "the run did not include a 30-minute warmup, baseline window, and final window"}
    baseline_start = warmup_seconds
    baseline_end = warmup_seconds + 1800
    final_start = max(baseline_end, duration_seconds - 1800)
    final_end = duration_seconds
    required_samples = max(1, int((1800 + interval_seconds - 1) // interval_seconds))
    components: dict[str, Any] = {}
    overall = "passed"
    for component in ("core", *HOSTS):
        first: list[int] = []
        last: list[int] = []
        baseline_pids: set[int] = set()
        final_pids: set[int] = set()
        for sample in samples:
            elapsed = sample["elapsed_seconds"]
            entry = sample["components"].get(component, {})
            rss = entry.get("rss_bytes")
            pid = entry.get("pid")
            if rss is None or pid is None:
                continue
            if baseline_start <= elapsed < baseline_end:
                first.append(int(rss))
                baseline_pids.add(int(pid))
            if final_start <= elapsed <= final_end:
                last.append(int(rss))
                final_pids.add(int(pid))
        if (
            len(first) < required_samples
            or len(last) < required_samples
            or len(baseline_pids | final_pids) != 1
        ):
            components[component] = {
                "status": "unknown",
                "reason": "missing samples or process identity changed inside the comparison windows",
                "baseline_samples": len(first),
                "final_samples": len(last),
                "required_samples_per_window": required_samples,
                "baseline_pids": sorted(baseline_pids),
                "final_pids": sorted(final_pids),
            }
            overall = "unknown"
            continue
        baseline = int(median(first))
        final = int(median(last))
        growth = final - baseline
        limit = max(20 * 1024 * 1024, int(baseline * 0.10))
        status = "passed" if growth <= limit else "failed"
        if status == "failed":
            overall = "failed"
        components[component] = {
            "status": status,
            "baseline_median_rss_bytes": baseline,
            "final_median_rss_bytes": final,
            "growth_bytes": growth,
            "allowed_growth_bytes": limit,
            "baseline_samples": len(first),
            "final_samples": len(last),
            "pid": next(iter(baseline_pids)),
        }
    return {
        "status": overall,
        "sampling_interval_seconds": interval_seconds,
        "warmup_seconds": warmup_seconds,
        "baseline_window_seconds": [baseline_start, baseline_end],
        "final_window_seconds": [final_start, final_end],
        "components": components,
    }


def linear_slope_mib_per_hour(samples: list[dict[str, Any]], component: str) -> float | None:
    pairs = [
        (float(sample["elapsed_seconds"]), float(sample["components"][component]["rss_bytes"]))
        for sample in samples
        if sample.get("components", {}).get(component, {}).get("rss_bytes") is not None
    ]
    if len(pairs) < 2:
        return None
    mean_x = sum(point[0] for point in pairs) / len(pairs)
    mean_y = sum(point[1] for point in pairs) / len(pairs)
    denominator = sum((x - mean_x) ** 2 for x, _ in pairs)
    if denominator == 0:
        return None
    slope_bytes_per_second = sum((x - mean_x) * (y - mean_y) for x, y in pairs) / denominator
    return round(slope_bytes_per_second * 3600 / (1024 * 1024), 4)


def run_stability(
    cycles_per_host: int,
    duration_hours: float,
    interval_seconds: float,
    warmup_minutes: float,
    report_path: Path,
    offline: bool,
    skip_build: bool,
) -> tuple[int, dict[str, Any]]:
    report: dict[str, Any] = {
        "suite": "stability",
        "platform": harness.available_platform(),
        "status": "running",
        "gate": "NOT READY",
        "started_at": utc_now(),
        "configuration": {
            "cycles_per_host": cycles_per_host,
            "duration_hours": duration_hours,
            "sampling_interval_seconds": interval_seconds,
            "warmup_minutes": warmup_minutes,
            "resource_growth_limit": "max(20 MiB, 10%)",
            "capability_revocation_seconds": 5,
            "recovery_seconds": 30,
            "shutdown_seconds": 10,
        },
        "build_steps": [],
        "recovery_cycles": [],
        "monitor_samples": [],
        "http_requests": [],
        "checks": [],
        "cleanup": {"host_pids": [], "remaining_pids": [], "core_exit_code": None},
    }
    expected_cycles = cycles_per_host * len(HOSTS)
    temp = Path(tempfile.mkdtemp(prefix="ligamina-stability-"))
    daemon: subprocess.Popen[str] | None = None
    base_url: str | None = None
    admin_token = client_token = ""
    daemon_stdout: queue.Queue[str] = queue.Queue()
    daemon_stderr: queue.Queue[str] = queue.Queue()
    api = harness.Api(report)
    host_pids: set[int] = set()
    live_host_pids: set[int] = set()
    run_code = 1
    try:
        harness.assert_platform(report["platform"])
        if skip_build:
            report["build_status"] = "skipped"
            report["build_skip_reason"] = "the caller confirmed that the T12 build is already available"
        else:
            harness.build_dependencies(report, offline)
            report["build_status"] = "passed"
        config = harness.make_config(temp)
        secrets = config.pop("_secrets")
        admin_token = secrets["admin_token"]
        client_token = secrets["client_token"]
        for host in config["hosts"]:
            host["max_restarts"] = cycles_per_host + 5
            host["restart_backoff_ms"] = 100
        report["environment"] = harness.environment_fingerprint(config)
        config_path = temp / "daemon.json"
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        executable = ROOT / "target/debug/ligamina-cli.exe" if report["platform"] == "windows" else ROOT / "target/debug/ligamina-cli"
        if not executable.is_file():
            raise FileNotFoundError(f"built Core daemon was not found: {executable}")
        daemon = subprocess.Popen(
            [str(executable), "daemon", "--config", str(config_path)],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert daemon.stdout is not None and daemon.stderr is not None
        threading.Thread(target=create_process_report_queue, args=(daemon.stdout, daemon_stdout), daemon=True).start()
        threading.Thread(target=create_process_report_queue, args=(daemon.stderr, daemon_stderr), daemon=True).start()
        ready = None
        ready_deadline = time.monotonic() + 100
        while time.monotonic() < ready_deadline:
            if daemon.poll() is not None:
                break
            try:
                packet = json.loads(daemon_stdout.get(timeout=0.25))
            except queue.Empty:
                continue
            except json.JSONDecodeError:
                continue
            if packet.get("ready") is True:
                ready = packet
                break
        if ready is None:
            raise RuntimeError("Core daemon did not announce readiness: " + "".join(list(daemon_stderr.queue))[-4000:])
        base_url = f"http://{ready['listen']}"
        api.request(base_url, "GET", "/v1/status", token=admin_token)
        initial_status = wait_for_status(
            api,
            base_url,
            admin_token,
            lambda value: all((host_record(value, host_id) or {}).get("state") == "active" for host_id in HOSTS),
            timeout=90,
        )
        baseline_caps = capability_records(api, base_url, client_token)
        baseline_by_host = {
            host_id: sorted(
                item.get("descriptor", {}).get("capability_id")
                for item in baseline_caps
                if item.get("host_id") == host_id
            )
            for host_id in HOSTS
        }
        if any(not values for values in baseline_by_host.values()):
            raise RuntimeError(f"expected capabilities were not registered: {baseline_by_host}")
        report["initial_status"] = initial_status
        report["baseline_capabilities"] = baseline_by_host
        cli_environment = os.environ.copy()
        cli_environment["LIGAMINA_ADMIN_TOKEN"] = admin_token
        cli_checks = []
        for cli_command in ("status", "doctor"):
            process = subprocess.run(
                [str(executable), cli_command, "--url", base_url],
                cwd=ROOT,
                env=cli_environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
            )
            try:
                cli_result = json.loads(process.stdout)
            except json.JSONDecodeError:
                cli_result = None
            cli_checks.append({
                "command": cli_command,
                "status": "passed" if process.returncode == 0 and isinstance(cli_result, dict) else "failed",
                "returncode": process.returncode,
                "response": cli_result,
                "stderr": process.stderr[-2000:],
            })
        report["cli_checks"] = cli_checks
        report["cleanup"]["host_pids"] = sorted(
            int(host["pid"]) for host in initial_status["hosts"] if host.get("pid")
        )
        host_pids.update(report["cleanup"]["host_pids"])
        live_host_pids.update(report["cleanup"]["host_pids"])

        host_failed: dict[str, bool] = {host_id: False for host_id in HOSTS}
        recovery_budget = {"status": "passed", "revoke_limit_seconds": 5, "recovery_limit_seconds": 30, "maximum_revocation_seconds": 0.0, "maximum_recovery_seconds": 0.0}
        for host_id in HOSTS:
            for attempt in range(1, cycles_per_host + 1):
                entry: dict[str, Any] = {"host_id": host_id, "attempt": attempt, "status": "unknown"}
                if host_failed[host_id]:
                    entry.update({"status": "skipped", "reason": "previous recovery failure made later cycles unprovable"})
                    report["recovery_cycles"].append(entry)
                    continue
                try:
                    status = api.request(base_url, "GET", "/v1/status", token=admin_token)[1]
                    old = host_record(status, host_id)
                    if not old or old.get("state") != "active" or not old.get("pid"):
                        entry.update({"status": "unknown", "reason": "host was not active before restart"})
                        host_failed[host_id] = True
                        report["recovery_cycles"].append(entry)
                        continue
                    old_pid = int(old["pid"])
                    old_restart_count = int(old.get("restart_count", 0))
                    host_pids.add(old_pid)
                    started = time.monotonic()
                    entry["old_pid"] = old_pid
                    entry["kill"] = harness.kill_host(report["platform"], old_pid)
                    revoked = wait_for_capabilities(
                        api,
                        base_url,
                        client_token,
                        lambda values: not any(item.get("host_id") == host_id for item in values),
                        timeout=5,
                    )
                    revoked_seconds = time.monotonic() - started
                    entry["revocation_seconds"] = round(revoked_seconds, 3)
                    entry["capabilities_after_revocation"] = revoked
                    recovery_budget["maximum_revocation_seconds"] = max(recovery_budget["maximum_revocation_seconds"], revoked_seconds)
                    if revoked is None or revoked_seconds > 5:
                        entry.update({"status": "failed", "reason": "capability registration was not revoked within five seconds"})
                        host_failed[host_id] = True
                        recovery_budget["status"] = "failed"
                        report["recovery_cycles"].append(entry)
                        continue
                    dead_deadline = time.monotonic() + 2
                    while time.monotonic() < dead_deadline and harness.process_alive(old_pid, report["platform"]):
                        time.sleep(0.05)
                    entry["old_process_reaped"] = not harness.process_alive(old_pid, report["platform"])
                    if entry["old_process_reaped"]:
                        live_host_pids.discard(old_pid)
                    recovered = wait_for_status(
                        api,
                        base_url,
                        admin_token,
                        lambda value: (
                            (host := host_record(value, host_id)) is not None
                            and host.get("state") == "active"
                            and host.get("pid")
                            and int(host["pid"]) != old_pid
                            and int(host.get("restart_count", 0)) > old_restart_count
                        ),
                        timeout=30,
                    )
                    new = host_record(recovered, host_id)
                    entry["new_pid"] = new.get("pid") if new else None
                    entry["restart_count"] = new.get("restart_count") if new else None
                    if not new or new.get("state") != "active":
                        entry.update({"status": "failed", "reason": "host did not recover within thirty seconds"})
                        host_failed[host_id] = True
                        recovery_budget["status"] = "failed"
                        report["recovery_cycles"].append(entry)
                        continue
                    new_pid = int(new["pid"])
                    host_pids.add(new_pid)
                    live_host_pids.add(new_pid)
                    restored_caps = wait_for_capabilities(
                        api,
                        base_url,
                        client_token,
                        lambda values: sorted(
                            item.get("descriptor", {}).get("capability_id")
                            for item in values
                            if item.get("host_id") == host_id
                        ) == baseline_by_host[host_id],
                        timeout=30,
                    )
                    recovered_seconds = time.monotonic() - started
                    entry["recovery_seconds"] = round(recovered_seconds, 3)
                    recovery_budget["maximum_recovery_seconds"] = max(recovery_budget["maximum_recovery_seconds"], recovered_seconds)
                    restored_by_host = sorted(
                        item.get("descriptor", {}).get("capability_id")
                        for item in (restored_caps or [])
                        if item.get("host_id") == host_id
                    )
                    sibling_id = next(candidate for candidate in HOSTS if candidate != host_id)
                    sibling = host_record(recovered, sibling_id)
                    entry["restored_capabilities"] = restored_by_host
                    entry["sibling_state"] = sibling.get("state") if sibling else None
                    if restored_caps is None or recovered_seconds > 30:
                        entry.update({"status": "failed", "reason": "capability registration did not recover within thirty seconds"})
                        host_failed[host_id] = True
                        recovery_budget["status"] = "failed"
                        report["recovery_cycles"].append(entry)
                        continue
                    invocation = check_capability(api, base_url, client_token, CAPABILITIES[host_id])
                    entry["capability_result"] = invocation
                    if restored_by_host != baseline_by_host[host_id] or not sibling or sibling.get("state") != "active" or invocation != 42:
                        entry.update({"status": "failed", "reason": "capability registration, sibling availability, or probe invocation diverged"})
                        host_failed[host_id] = True
                        recovery_budget["status"] = "failed"
                    else:
                        entry["status"] = "passed"
                except (harness.SmokeFailure, OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
                    entry.update({"status": "unknown", "reason": f"{type(error).__name__}: {error}"})
                    host_failed[host_id] = True
                    recovery_budget["status"] = "unknown" if recovery_budget["status"] == "passed" else recovery_budget["status"]
                report["recovery_cycles"].append(entry)
        report["recovery_budget"] = recovery_budget

        monitor_duration = duration_hours * 3600
        monitor_started = time.monotonic()
        last_sample_elapsed = -interval_seconds
        monitor_status = "passed"
        while True:
            elapsed = time.monotonic() - monitor_started
            if elapsed >= monitor_duration and report["monitor_samples"]:
                break
            if elapsed - last_sample_elapsed < interval_seconds:
                remaining = interval_seconds - (elapsed - last_sample_elapsed)
                if monitor_duration and elapsed < monitor_duration:
                    time.sleep(min(remaining, max(0.05, monitor_duration - elapsed)))
                    continue
            try:
                current_status = api.request(base_url, "GET", "/v1/status", token=admin_token, timeout=5)[1]
                current_caps = capability_records(api, base_url, client_token)
                cap_signature = sorted(
                    (item.get("host_id"), item.get("descriptor", {}).get("capability_id"))
                    for item in current_caps
                )
                expected_signature = sorted((host_id, capability_id) for host_id, values in baseline_by_host.items() for capability_id in values)
                states_ok = all((host_record(current_status, host_id) or {}).get("state") == "active" for host_id in HOSTS)
                caps_ok = cap_signature == expected_signature and len(current_caps) <= len(baseline_caps)
                probes = {
                    host_id: check_capability(api, base_url, client_token, CAPABILITIES[host_id])
                    for host_id in HOSTS
                }
                probes_ok = all(value == 42 for value in probes.values())
                sample = sample_processes(int(daemon.pid), current_status, current_caps, elapsed)
                sample.update({"status": "passed" if states_ok and caps_ok and probes_ok else "failed", "probe_results": probes})
                report["monitor_samples"].append(sample)
                if sample["status"] != "passed":
                    monitor_status = "failed"
                    sample["errors"] = {"states_ok": states_ok, "capabilities_ok": caps_ok, "probes_ok": probes_ok}
                for host in current_status.get("hosts", []):
                    if host.get("pid"):
                        host_pids.add(int(host["pid"]))
                last_sample_elapsed = elapsed
            except (harness.SmokeFailure, OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
                monitor_status = "unknown"
                report["monitor_samples"].append({"at": utc_now(), "elapsed_seconds": round(elapsed, 3), "status": "unknown", "error": f"{type(error).__name__}: {error}"})
                last_sample_elapsed = elapsed
            if monitor_duration <= 0 or elapsed >= monitor_duration:
                break
        duration_seconds = time.monotonic() - monitor_started
        report["monitor_duration_seconds"] = round(duration_seconds, 3)
        report["monitor_status"] = monitor_status
        report["resource_budget"] = resource_budget(
            report["monitor_samples"], duration_seconds, warmup_minutes * 60, interval_seconds
        )
        report["resource_trends_mib_per_hour"] = {
            component: linear_slope_mib_per_hour(report["monitor_samples"], component)
            for component in ("core", *HOSTS)
        }
        if monitor_status != "passed" and report["resource_budget"]["status"] == "passed":
            report["resource_budget"]["monitor_status"] = monitor_status
        report["checks"] = [
            {"id": "recovery_budget", "status": recovery_budget["status"]},
            {"id": "combined_monitor", "status": monitor_status},
            {"id": "resource_budget", "status": report["resource_budget"]["status"]},
            {"id": "cli_status_doctor", "status": "passed" if all(item["status"] == "passed" for item in cli_checks) else "failed"},
        ]
        run_code = 0
    except FileNotFoundError as error:
        report.update({"status": "environment_missing", "error": str(error)})
        run_code = 2
    except BaseException as error:
        report.update({"status": "failed", "error": f"{type(error).__name__}: {error}", "traceback": traceback.format_exc()[-6000:]})
        run_code = 1
    finally:
        if daemon is not None and daemon.poll() is None:
            shutdown_started = time.monotonic()
            if base_url:
                shutdown_request_started = time.monotonic()
                try:
                    api.request(base_url, "POST", "/v1/shutdown", token=admin_token, expected=(202,), timeout=3)
                except Exception as error:
                    report["cleanup"]["shutdown_error"] = f"{type(error).__name__}: {error}"
                finally:
                    report["cleanup"]["shutdown_request_seconds"] = round(
                        time.monotonic() - shutdown_request_started, 3
                    )
            wait_started = time.monotonic()
            try:
                shutdown_remaining = max(0.05, 10 - (time.monotonic() - shutdown_started))
                daemon.wait(timeout=shutdown_remaining)
            except subprocess.TimeoutExpired:
                try:
                    daemon.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    daemon.terminate()
                    try:
                        daemon.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        daemon.kill()
                    daemon.wait(timeout=5)
                    report["cleanup"]["forced_shutdown"] = True
            report["cleanup"]["daemon_wait_seconds"] = round(time.monotonic() - wait_started, 3)
            report["cleanup"]["shutdown_duration_seconds"] = round(time.monotonic() - shutdown_started, 3)
        if daemon is not None:
            report["cleanup"]["core_exit_code"] = daemon.returncode
        if not daemon_stderr.empty():
            report["daemon_stderr_tail"] = "".join(list(daemon_stderr.queue))[-4000:]
        if not daemon_stdout.empty():
            report["daemon_stdout_tail"] = "".join(list(daemon_stdout.queue))[-4000:]
        report["cleanup"]["host_pids"] = sorted(host_pids)
        report["cleanup"]["remaining_pids"] = [
            pid for pid in sorted(live_host_pids) if harness.process_alive(pid, report["platform"])
        ]
        core_pid = int(daemon.pid) if daemon is not None and daemon.pid else None
        report["cleanup"]["core_pid"] = core_pid
        if core_pid and harness.process_alive(core_pid, report["platform"]):
            report["cleanup"]["remaining_pids"].append(core_pid)
        clean_exit = (
            not report["cleanup"]["remaining_pids"]
            and report["cleanup"]["core_exit_code"] == 0
            and not report["cleanup"].get("forced_shutdown")
            and not report["cleanup"].get("shutdown_error")
            and report["cleanup"].get("shutdown_duration_seconds", 0) <= 10
        )
        report["cleanup"]["status"] = "passed" if clean_exit else "failed"
        shutil.rmtree(temp, ignore_errors=True)
        report["ended_at"] = utc_now()
        report["expected_cycles"] = expected_cycles
        report["case_summary"] = summarize_cases(report["recovery_cycles"], expected_cycles)
        ready_checks = (
            report.get("case_summary", {}).get("gate_passed") is True
            and report.get("recovery_budget", {}).get("status") == "passed"
            and report.get("monitor_status") == "passed"
            and report.get("resource_budget", {}).get("status") == "passed"
            and all(item.get("status") == "passed" for item in report.get("cli_checks", []))
            and report.get("cleanup", {}).get("status") == "passed"
        )
        if run_code == 0:
            report["status"] = "passed" if ready_checks else "completed"
            report["gate"] = "PASS" if ready_checks else "NOT READY"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report["report"] = str(report_path.resolve())
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return run_code if run_code else (0 if report.get("gate") == "PASS" else 1), report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycles-per-host", type=int, default=100)
    parser.add_argument("--duration-hours", type=float, default=24)
    parser.add_argument("--interval-seconds", type=float, default=60)
    parser.add_argument("--warmup-minutes", type=float, default=30)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--skip-build", action="store_true", help="use an already built T12 runtime")
    options = parser.parse_args(argv)
    if options.cycles_per_host < 1 or options.duration_hours < 0 or options.interval_seconds <= 0 or options.warmup_minutes < 0:
        parser.error("cycle count must be positive and durations must not be negative")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    report_path = options.report or ROOT / "outputs/acceptance" / f"t14-stability-{harness.available_platform()}-{stamp}.json"
    code, report = run_stability(
        options.cycles_per_host,
        options.duration_hours,
        options.interval_seconds,
        options.warmup_minutes,
        report_path,
        options.offline or os.environ.get("CARGO_NET_OFFLINE") == "true",
        options.skip_build,
    )
    print(json.dumps({
        "suite": report["suite"],
        "status": report["status"],
        "gate": report["gate"],
        "report": str(report_path.resolve()),
        "case_summary": report.get("case_summary"),
        "resource_status": report.get("resource_budget", {}).get("status"),
        "cleanup": report.get("cleanup"),
        "error": report.get("error"),
    }, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
