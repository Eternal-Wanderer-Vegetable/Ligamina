"""Generate a field Core config with one isolated Host per framework (P6 MCP experiment).

Each Host runs one frozen, previously accepted catalog plugin inside
``docker run -i --network none --read-only`` under WSL, started by a Core on the host
machine. Tokens are written next to the config under the ignored ``.work`` directory and
never printed.

    python lab/field/three_host_config.py --work .work/field-20261108/run1 --port 63910
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
from pathlib import Path

WSL = r"C:\Windows\System32\wsl.exe"
FIELD_DIR = "/mnt/e/stella/Ligamina/lab/field"
TOOL04_IMAGE = "ligamina-tool04-runtime@sha256:9c1280e40a0bfa7fb2a7135a58316624a9999ad5333ad66938eb363f7213b6b5"
ASTRBOT_IMAGE = "ligamina-astrbot-tool-runtime@sha256:07b6a1ae5a1a3c659294da7abd966f93de54aacd1055fb13efb25762e91b5ba0"
TOOL04_BUNDLES = "/home/vegetable/ligamina-tool04-bundles-20261107-v1"
TEAMUP_BUNDLE = "/home/vegetable/ligamina-ab05-bundles-20261108-v2/ab-01803ad6c618f2321ed12e48"
TEAMUP_COMMANDS = ["组队菜单", "组队昵称", "组队大厅", "组队创建", "组队重置"]


def tool_name(host: str, command: str) -> str:
    """Core's MCP name rule (commands.rs): unsafe characters become `_`, plus a hash suffix."""
    def part(value: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", value)
        return cleaned if cleaned == value else f"{cleaned}-{hashlib.sha256(value.encode()).hexdigest()[:8]}"
    return f"cmd.{part(host)}.{part(command)}"


TOOLS = [
    tool_name("field-koishi-echo", "echo"),
    tool_name("field-nonebot-roll", "rd"),
    *(tool_name("field-astrbot-teamup", command) for command in TEAMUP_COMMANDS),
]


def hosts() -> list[dict]:
    """One untrusted Host per framework; the Core builds the hardened `docker run` (ADR 0003)."""
    def host(host_id: str, isolation: dict) -> dict:
        return {
            "id": host_id, "enabled": True, "trust": "untrusted",
            "isolation": {"docker": [WSL, "-e", "docker"], "network": "none", **isolation},
            "max_restarts": 1, "restart_backoff_ms": 2000,
            "startup_timeout_ms": 240000, "shutdown_timeout_ms": 15000,
        }

    def ro(source: str, target: str) -> dict:
        return {"source": source, "target": target, "read_only": True}

    return [
        host("field-koishi-echo", {
            "image": TOOL04_IMAGE,
            "entrypoint": "/usr/local/bin/python", "command": ["/input/field/koishi_host.py"],
            "mounts": [ro(f"{TOOL04_BUNDLES}/koishi/project", "/input/project"), ro(FIELD_DIR, "/input/field")],
            "tmpfs": [{"target": "/work", "size_mb": 1536, "exec": True}, {"target": "/tmp", "size_mb": 128}],
            "environment": {"LIGAMINA_FIELD_PLUGIN": "@koishijs/plugin-echo", "LIGAMINA_KOISHI_NICKNAMES": "Bot"},
            "memory_mb": 2048, "cpus": 1, "pids_limit": 128,
        }),
        host("field-nonebot-roll", {
            "image": TOOL04_IMAGE,
            "entrypoint": "/usr/local/bin/python", "command": ["/input/field/nonebot_host.py"],
            "mounts": [ro(f"{TOOL04_BUNDLES}/nonebot/project", "/input/project"), ro(FIELD_DIR, "/input/field")],
            "tmpfs": [{"target": "/work", "size_mb": 768, "exec": True}, {"target": "/tmp", "size_mb": 64}],
            "environment": {"LIGAMINA_FIELD_PLUGIN": "nonebot_plugin_roll", "LIGAMINA_NONEBOT_NICKNAMES": "Bot"},
            "memory_mb": 1024, "cpus": 1, "pids_limit": 128,
        }),
        host("field-astrbot-teamup", {
            "image": ASTRBOT_IMAGE,
            "entrypoint": "/opt/astrbot/venv/bin/python", "command": ["-m", "ligamina_astrbot.worker"],
            "workdir": "/work",
            "mounts": [ro(TEAMUP_BUNDLE, "/input")],
            "tmpfs": [{"target": "/work", "size_mb": 1024, "exec": True}, {"target": "/tmp", "size_mb": 256, "exec": True}],
            "environment": {
                "HOME": "/work", "PYTHONPATH": "/input/project/hosts/astrbot",
                "LIGAMINA_ASTRBOT_PLUGINS": "/input/plugin/astrbot_plugin_teamup",
                "LIGAMINA_ASTRBOT_DATA_DIR": "/work/astrbot",
                "ASTRBOT_DISABLE_METRICS": "1", "PYTHONDONTWRITEBYTECODE": "1",
            },
            "memory_mb": 3072, "cpus": 2, "pids_limit": 256,
        }),
    ]


def owner_for(database_path: str) -> str:
    """Same value as the Core's container owner label (deployment/container.rs)."""
    return hashlib.sha256(database_path.encode()).hexdigest()[:12]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--work", required=True, type=Path)
    parser.add_argument("--port", type=int, default=63910)
    args = parser.parse_args()
    work = args.work.resolve()
    if (work / "secrets.json").exists():
        raise SystemExit(f"{work} already holds a generated config; use a new directory")
    work.mkdir(parents=True, exist_ok=True)
    tokens = {"admin": secrets.token_hex(32), "mcp_client": secrets.token_hex(32), "unused_gateway": secrets.token_hex(32)}
    config = {
        "listen": f"127.0.0.1:{args.port}",
        "database_path": str(work / "core.sqlite3"),
        "admin_token": tokens["admin"],
        "mcp_client_id": "field-mcp",
        "clients": [{"id": "field-mcp", "token": tokens["mcp_client"]}],
        # The daemon requires one Gateway; tool sessions never use it and it is never leased.
        "gateways": [{"id": "unused", "client_id": "field-mcp", "account_id": "field-unused",
                      "registration_token": tokens["unused_gateway"]}],
        "hosts": hosts(),
        "command_tools": {"enabled": True, "allow": TOOLS},
    }
    (work / "daemon.json").write_text(json.dumps(config, indent=2), encoding="utf-8", newline="\n")
    (work / "secrets.json").write_text(json.dumps(tokens), encoding="utf-8", newline="\n")
    # Stella's MCP client configuration for this run; the token is read from an env var.
    (work / "mcp.toml").write_text(
        "[servers.plugins]\n"
        "enabled = true\n"
        'transport = "streamable_http"\n'
        f'url = "http://127.0.0.1:{args.port}/mcp"\n'
        'auth_env = "FIELD_MCP_TOKEN"\n'
        f"allowed_tools = {json.dumps(TOOLS)}\n"
        "connect_timeout = 30\n"
        "call_timeout = 120\n",
        encoding="utf-8",
    )
    owner = owner_for(config["database_path"])
    print(json.dumps({"work": str(work), "owner": owner, "run_label": owner, "port": args.port}))


if __name__ == "__main__":
    main()
