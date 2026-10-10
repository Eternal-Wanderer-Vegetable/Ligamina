"""Prepare the container-local Core config and exec the Ligamina daemon."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


CONFIG_SOURCE = Path("/etc/ligamina/daemon.json")
CONFIG_RUNTIME = Path("/run/ligamina/daemon.json")
DATA_ROOT = Path("/var/lib/ligamina")
HOST_ROOT = Path("/opt/ligamina/hosts")
CORE = "/usr/local/bin/ligamina-cli"


def main() -> int:
    if not CONFIG_SOURCE.is_file():
        print("missing /etc/ligamina/daemon.json; copy daemon.example.json and configure tokens", file=sys.stderr)
        return 2
    config = json.loads(CONFIG_SOURCE.read_text(encoding="utf-8"))
    admin_token = os.environ.get("LIGAMINA_ADMIN_TOKEN", "")
    if not admin_token or admin_token == "CHANGE_ME" or config.get("admin_token") != admin_token:
        print("LIGAMINA_ADMIN_TOKEN must match daemon.json admin_token and must not be CHANGE_ME", file=sys.stderr)
        return 2

    profiles = {part.strip().lower() for part in os.environ.get("LIGAMINA_PROFILES", "nonebot,koishi").split(",") if part.strip()}
    if not profiles or profiles - {"nonebot", "koishi"}:
        print("LIGAMINA_PROFILES must be a comma-separated subset of nonebot,koishi", file=sys.stderr)
        return 2

    hosts = config.get("hosts")
    if not isinstance(hosts, list):
        print("daemon.json must define hosts as an array", file=sys.stderr)
        return 2
    seen: set[str] = set()
    for host in hosts:
        host_id = host.get("id")
        if host_id == "nonebot-container":
            profile = "nonebot"
            host_root = HOST_ROOT / "nonebot"
            host.update({
                "program": str(host_root / ".venv/bin/python"),
                "args": ["-m", "ligamina_nonebot.worker"],
                "current_dir": str(host_root),
            })
            environment = host.setdefault("environment", {})
            environment["LIGAMINA_NONEBOT_DATA_DIR"] = str(DATA_ROOT / "nonebot")
        elif host_id == "koishi-container":
            profile = "koishi"
            host_root = HOST_ROOT / "koishi"
            host.update({
                "program": "/usr/local/bin/node",
                "args": [str(host_root / "dist/worker.js")],
                "current_dir": str(host_root),
            })
            environment = host.setdefault("environment", {})
            environment["LIGAMINA_KOISHI_DATA_DIR"] = str(DATA_ROOT / "koishi")
        else:
            print(f"unsupported container Host id: {host_id!r}", file=sys.stderr)
            return 2
        if profile in seen:
            print(f"duplicate container Host profile: {profile}", file=sys.stderr)
            return 2
        seen.add(profile)
        host["enabled"] = profile in profiles

    if seen != {"nonebot", "koishi"}:
        print("daemon.json must include both nonebot-container and koishi-container entries", file=sys.stderr)
        return 2

    # Docker's bridge forwards to the container interface. Compose restricts the
    # published host port to 127.0.0.1, so this opt-in stays inside that boundary.
    config["listen"] = "0.0.0.0:26701"
    config["allow_non_loopback_listen"] = True
    config["database_path"] = str(DATA_ROOT / "ligamina.sqlite")
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    CONFIG_RUNTIME.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    CONFIG_RUNTIME.chmod(0o600)
    os.execv(CORE, [CORE, "daemon", "--config", str(CONFIG_RUNTIME)])
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
