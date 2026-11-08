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
HARDENING = ["--network", "none", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges"]


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


def docker(label: str, *args: str) -> list[str]:
    return ["-e", "docker", "run", "--rm", "-i", "--label", f"io.ligamina.field.run={label}", *HARDENING, *args]


def hosts(label: str) -> list[dict]:
    def host(host_id: str, args: list[str]) -> dict:
        return {
            "id": host_id, "enabled": True, "program": WSL, "args": args,
            "max_restarts": 1, "restart_backoff_ms": 2000,
            "startup_timeout_ms": 240000, "shutdown_timeout_ms": 15000,
        }

    return [
        host("field-koishi-echo", docker(
            label,
            "--tmpfs", "/work:rw,exec,nosuid,nodev,size=1536m,mode=1777",
            "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=128m",
            "--volume", f"{TOOL04_BUNDLES}/koishi/project:/input/project:ro",
            "--volume", f"{FIELD_DIR}:/input/field:ro",
            "--pids-limit=128", "--memory=2g", "--cpus=1",
            "-e", "LIGAMINA_FIELD_PLUGIN=@koishijs/plugin-echo",
            "-e", "LIGAMINA_KOISHI_NICKNAMES=Bot",
            "--entrypoint", "/usr/local/bin/python", TOOL04_IMAGE, "/input/field/koishi_host.py",
        )),
        host("field-nonebot-roll", docker(
            label,
            "--tmpfs", "/work:rw,exec,nosuid,nodev,size=768m,mode=1777",
            "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=64m",
            "--volume", f"{TOOL04_BUNDLES}/nonebot/project:/input/project:ro",
            "--volume", f"{FIELD_DIR}:/input/field:ro",
            "--pids-limit=128", "--memory=1g", "--cpus=1",
            "-e", "LIGAMINA_FIELD_PLUGIN=nonebot_plugin_roll",
            "-e", "LIGAMINA_NONEBOT_NICKNAMES=Bot",
            "--entrypoint", "/usr/local/bin/python", TOOL04_IMAGE, "/input/field/nonebot_host.py",
        )),
        host("field-astrbot-teamup", docker(
            label,
            "--workdir", "/work",
            "--tmpfs", "/work:rw,exec,nosuid,nodev,size=1024m,mode=1777",
            "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=256m,mode=1777",
            "--volume", f"{TEAMUP_BUNDLE}:/input:ro",
            "--pids-limit=256", "--memory=3g", "--cpus=2",
            "-e", "HOME=/work", "-e", "PYTHONPATH=/input/project/hosts/astrbot",
            "-e", "LIGAMINA_ASTRBOT_PLUGINS=/input/plugin/astrbot_plugin_teamup",
            "-e", "LIGAMINA_ASTRBOT_DATA_DIR=/work/astrbot",
            "-e", "ASTRBOT_DISABLE_METRICS=1", "-e", "PYTHONDONTWRITEBYTECODE=1",
            "--entrypoint", "/opt/astrbot/venv/bin/python", ASTRBOT_IMAGE, "-m", "ligamina_astrbot.worker",
        )),
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--work", required=True, type=Path)
    parser.add_argument("--port", type=int, default=63910)
    args = parser.parse_args()
    work = args.work.resolve()
    if (work / "secrets.json").exists():
        raise SystemExit(f"{work} already holds a generated config; use a new directory")
    work.mkdir(parents=True, exist_ok=True)
    label = secrets.token_hex(8)
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
        "hosts": hosts(label),
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
    print(json.dumps({"work": str(work), "run_label": label, "port": args.port}))


if __name__ == "__main__":
    main()
