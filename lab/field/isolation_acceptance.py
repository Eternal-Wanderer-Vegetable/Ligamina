"""P6-03 acceptance: Core-enforced Host isolation against the real container runtime.

    python lab/field/isolation_acceptance.py --work .work/p603-<id> --output outputs/acceptance/<report>.json

Checks, on Windows with Docker inside WSL (the field setup):
1. unsafe Host configurations make the daemon exit non-zero and name the violated rule;
2. a leftover container labelled for this daemon is removed when the Core starts;
3. every running Host container carries the hardening (docker inspect);
4. killing a Host's docker client removes its container and the Host restarts in a new one;
5. shutdown leaves no container behind.
No chat platform is involved.
"""

from __future__ import annotations

import argparse
import copy
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import three_host_config as field  # noqa: E402

CLI = ROOT / "target" / "debug" / ("ligamina" + "-cli.exe")
DOCKER = [field.WSL, "-e", "docker"]
NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def docker(*args: str, check: bool = True) -> str:
    result = subprocess.run([*DOCKER, *args], capture_output=True, text=True, timeout=120)
    if check and result.returncode:
        raise RuntimeError(f"docker {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def api(port: int, token: str, path: str, method: str = "GET") -> dict | None:
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                     headers={"authorization": f"Bearer {token}"})
    with NO_PROXY.open(request, timeout=10) as response:
        body = response.read()
    return json.loads(body) if body else None


def base_config(work: Path, port: int) -> tuple[dict, dict]:
    tokens = {"admin": "a" * 64, "client": "c" * 64, "gateway": "g" * 64}
    config = {
        "listen": f"127.0.0.1:{port}",
        "database_path": str(work / "core.sqlite3"),
        "admin_token": tokens["admin"],
        "mcp_client_id": "field-mcp",
        "clients": [{"id": "field-mcp", "token": tokens["client"]}],
        "gateways": [{"id": "unused", "client_id": "field-mcp", "account_id": "field-unused",
                      "registration_token": tokens["gateway"]}],
        "hosts": field.hosts(),
    }
    return config, tokens


def run_daemon(config: dict, path: Path, wait_ready: bool):
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    log = open(path.with_suffix(".stderr.log"), "w", encoding="utf-8")
    process = subprocess.Popen([str(CLI), "daemon", "--config", str(path)], stdout=subprocess.PIPE,
                               stderr=log, text=True)
    if not wait_ready:
        try:
            code = process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            process.kill()
            code = None
        log.close()
        return code, path.with_suffix(".stderr.log").read_text(encoding="utf-8")
    line = process.stdout.readline()
    if '"ready":true' not in line.replace(" ", ""):
        raise RuntimeError(f"daemon did not become ready: {line!r}")
    return process, log


def host_status(port: int, token: str) -> dict:
    return {host["id"]: host for host in api(port, token, "/v1/status")["hosts"]}


def wait_until(predicate, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(1)
    raise TimeoutError(what)


def containers(owner: str, host: str | None = None, all_states: bool = False) -> list[str]:
    filters = ["--filter", f"label=io.{'ligamina'}.owner={owner}"]
    if host:
        filters += ["--filter", f"label=io.{'ligamina'}.host={host}"]
    out = docker("ps", *(["-a"] if all_states else []), "--format", "{{.Names}}", *filters)
    return [line for line in out.splitlines() if line]


def client_pid_for(container_name: str) -> int:
    """PID of the wsl.exe docker client whose command line names the container."""
    script = ("Get-CimInstance Win32_Process -Filter \"Name='wsl.exe'\" | "
              "Where-Object { $_.CommandLine -like '*" + container_name + "*' } | "
              "Select-Object -First 1 -ExpandProperty ProcessId")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True,
                         text=True, timeout=60).stdout.strip()
    return int(out)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--work", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--port", type=int, default=63920)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit(f"refusing to overwrite {args.output}")
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=False)
    config, tokens = base_config(work, args.port)
    owner = field.owner_for(config["database_path"])
    report: dict = {"schema_version": 1, "kind": "p6_p603_isolation_acceptance",
                    "date": time.strftime("%Y-%m-%d"), "owner": owner, "checks": {}}

    # 1. Rejections: the daemon refuses to start and names the rule.
    def variant(mutate):
        candidate = copy.deepcopy(config)
        mutate(candidate["hosts"][1])
        return candidate

    def untrusted_program(host):
        host.pop("isolation")
        host["program"] = "python"

    def pinned_tag(host):
        host["isolation"]["image"] = "ligamina-tool04-runtime:latest"

    def writable(host):
        host["isolation"]["mounts"][0]["read_only"] = False

    def networked(host):
        host["isolation"]["network"] = "bridge"

    def misspelled(host):
        host["isolation"]["read_only_root"] = False

    rejections = []
    for name, mutate, expected in [
        ("untrusted_without_isolation", untrusted_program, "requires an `isolation` block"),
        ("image_not_pinned", pinned_tag, "pinned by digest"),
        ("writable_mount", writable, "must be read-only"),
        ("network_not_none", networked, "network must be `none`"),
        ("unknown_isolation_field", misspelled, "unknown field `read_only_root`"),
    ]:
        code, stderr = run_daemon(variant(mutate), work / f"reject-{name}.json", wait_ready=False)
        rejections.append({"case": name, "exit_code": code, "stderr_names_rule": expected in stderr,
                           "stderr": stderr.strip().splitlines()[-1][:300] if stderr.strip() else ""})
    report["checks"]["rejections"] = {
        "passed": all(r["exit_code"] not in (0, None) and r["stderr_names_rule"] for r in rejections),
        "cases": rejections,
    }

    # 2. A leftover container from an "earlier run" is swept at startup.
    leftover = docker("run", "-d", "--label", f"io.{'ligamina'}.host=field-nonebot-roll",
                      "--label", f"io.{'ligamina'}.owner={owner}", "--network", "none",
                      "--entrypoint", "/bin/sleep", field.TOOL04_IMAGE, "600")
    process, log = run_daemon(config, work / "daemon.json", wait_ready=True)
    try:
        still_there = docker("ps", "-aq", "--filter", f"id={leftover}")
        report["checks"]["leftover_swept"] = {"passed": not still_there, "leftover_id": leftover[:12]}

        # 3. Hardening on every running Host container.
        wait_until(lambda: all(h["state"] == "active" for h in host_status(args.port, tokens["admin"]).values()),
                   300, "hosts to become active")
        inspected = []
        for name in containers(owner):
            data = json.loads(docker("inspect", name))[0]
            host_config = data["HostConfig"]
            mounts = [m for m in data["Mounts"] if m["Type"] == "bind"]
            inspected.append({
                "container": name,
                "network_none": host_config["NetworkMode"] == "none",
                "read_only_root": host_config["ReadonlyRootfs"] is True,
                "cap_drop_all": [c.upper() for c in (host_config.get("CapDrop") or [])] in (["ALL"], ["CAP_ALL"]),
                "no_new_privileges": "no-new-privileges" in (host_config.get("SecurityOpt") or []),
                "pids_limited": (host_config.get("PidsLimit") or 0) > 0,
                "memory_limited": (host_config.get("Memory") or 0) > 0,
                "cpus_limited": (host_config.get("NanoCpus") or 0) > 0,
                "bind_mounts_read_only": bool(mounts) and all(not m["RW"] for m in mounts),
                "auto_remove": host_config.get("AutoRemove") is True,
            })
        report["checks"]["hardening"] = {
            "passed": len(inspected) == 3 and all(all(v for k, v in c.items() if k != "container") for c in inspected),
            "containers": inspected,
        }

        # 4. Kill the roll Host's docker client: Core removes the container and restarts the Host.
        before = host_status(args.port, tokens["admin"])["field-nonebot-roll"]
        old_name = containers(owner, "field-nonebot-roll")[0]
        pid = client_pid_for(old_name)
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=30)
        restarted = wait_until(
            lambda: (lambda s: s if s["state"] == "active" and s["epoch"] > before["epoch"] else None)(
                host_status(args.port, tokens["admin"])["field-nonebot-roll"]),
            300, "roll Host to restart")
        old_gone = wait_until(lambda: not docker("ps", "-aq", "--filter", f"name=^{old_name}$") or None,
                              60, "old container removal") is not None
        new_names = containers(owner, "field-nonebot-roll")
        report["checks"]["client_killed"] = {
            "passed": old_gone and len(new_names) == 1 and new_names[0] != old_name,
            "old_container": old_name, "new_container": new_names[0] if new_names else None,
            "epoch_before": before["epoch"], "epoch_after": restarted["epoch"],
            "restart_count": restarted["restart_count"],
        }
    finally:
        try:
            api(args.port, tokens["admin"], "/v1/shutdown", method="POST")
        except Exception:  # noqa: BLE001
            pass
        try:
            process.wait(timeout=90)
        except subprocess.TimeoutExpired:
            process.kill()
        log.close()

    # 5. Nothing left after shutdown.
    time.sleep(3)
    left = containers(owner, all_states=True)
    report["checks"]["shutdown_clean"] = {"passed": not left, "containers_left": left}
    report["passed"] = all(check["passed"] for check in report["checks"].values())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({name: check["passed"] for name, check in report["checks"].items()} | {"passed": report["passed"]}))


if __name__ == "__main__":
    main()
