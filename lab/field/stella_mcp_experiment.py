"""Drive frozen plugin tool cases through Stella's own MCP client (P6 field experiment).

Run with Stella's interpreter so the MCP client under test is Stella's
``capability.providers.mcp.client.McpServerClient``, not ours:

    <stella python> lab/field/stella_mcp_experiment.py --stella-root E:/stella/stella_project \
        --mcp-config .work/field-20261108/run1/mcp.toml --run-id run1 --output <report.json>

The bearer token comes from the ``FIELD_MCP_TOKEN`` environment variable named in the
config. Nothing here talks to a chat platform: every call is a Core tool session.

Cases and expected replies are read from the frozen TOOL-04 and AB-05 case files. One
deliberate difference: the field NoneBot Host is not seeded, so a successful roll is
matched by pattern (any face 1-6) instead of the seeded 【1】.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL_CASES = ROOT / "contracts/lab/automation/tools/v1/tool-surface-cases.json"
ASTRBOT_CASES = ROOT / "contracts/lab/automation/tools/v1/astrbot-tool-cases.json"
HOSTS = {
    "@koishijs/plugin-echo": "field-koishi-echo",
    "nonebot_plugin_roll": "field-nonebot-roll",
    "astrbot_plugin_teamup": "field-astrbot-teamup",
}
SEEDED_ROLL = "你掷出了1个6面骰子, 点数为【1】"
ROLL_PATTERN = re.compile(r"^你掷出了1个6面骰子, 点数为【[1-6]】$")


def tool_name(host: str, command: str) -> str:
    def part(value: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", value)
        return cleaned if cleaned == value else f"{cleaned}-{hashlib.sha256(value.encode()).hexdigest()[:8]}"
    return f"cmd.{part(host)}.{part(command)}"


def load_cases() -> list[dict]:
    cases = []
    tool = json.loads(TOOL_CASES.read_text(encoding="utf-8"))
    for entry in tool["entries"]:
        for case in entry["cases"]:
            cases.append({"plugin": entry["package"], "artifact": f"{entry['package']}@{entry['version']}", **case})
    astrbot = json.loads(ASTRBOT_CASES.read_text(encoding="utf-8"))
    for entry in astrbot["entries"]:
        if entry["plugin"] in HOSTS:
            for case in entry["cases"]:
                cases.append({"plugin": entry["plugin"], "artifact": f"{entry['plugin']}#{entry['artifact_sha256']}", **case})
    return cases


def judge(expected: dict, outcome: dict) -> list[str]:
    """Returns the list of mismatches for one call (empty means passed)."""
    if outcome.get("error"):
        return [f"call failed: {outcome['error']}"]
    text = outcome["text"]
    problems = []
    if "replies" in expected:
        replies = [reply["text"] if isinstance(reply, dict) else reply for reply in expected["replies"]]
        want = "\n".join(replies)
        if want == SEEDED_ROLL:
            if not ROLL_PATTERN.match(text):
                problems.append(f"expected a 1d6 roll, got {text!r}")
        elif text != want:
            problems.append(f"expected {want!r}, got {text!r}")
    if "replies_must_not_contain" in expected and expected["replies_must_not_contain"] in text:
        problems.append(f"reply leaked {expected['replies_must_not_contain']!r}: {text!r}")
    if expected.get("reply_count") == 1 and not text:
        problems.append("expected one reply, got none")
    return problems


async def run(args: argparse.Namespace) -> dict:
    sys.path.insert(0, str(args.stella_root))
    from capability.providers.mcp.client import McpServerClient  # noqa: PLC0415 - Stella's code
    from capability.providers.mcp.model import SERVER_READY, load_mcp_configs  # noqa: PLC0415

    config = load_mcp_configs(args.mcp_config)["plugins"]
    started = time.monotonic()
    # Hosts install their frozen plugin offline before registering commands; reconnect
    # until every allowed tool is in the catalog Stella's client sees.
    deadline = started + args.wait_seconds
    while True:
        client = McpServerClient(config)
        await client.start()
        ready = client.status_dict()["state"] == SERVER_READY
        catalog = sorted(descriptor.name for descriptor in client.catalog()) if ready else []
        missing = [name for name in config.allowed_tools if name not in catalog]
        if ready and not missing:
            break
        await client.close()
        if time.monotonic() > deadline:
            return {"run_id": args.run_id, "status": "not_ready", "missing_tools": missing,
                    "client_status": {k: v for k, v in client.status_dict().items() if k in ("state", "last_error")}}
        await asyncio.sleep(5)
    wait_ms = round((time.monotonic() - started) * 1000)
    report_cases = []
    try:
        for case in load_cases():
            host = HOSTS[case["plugin"]]
            steps = []
            for step, expected_step in zip(case["stimulus"]["tool_calls"], case["oracle"]["calls"], strict=True):
                async def call(item: dict) -> dict:
                    name = tool_name(host, item["command_id"])
                    begin = time.monotonic()
                    try:
                        text = await client.call_tool(name, item["arguments"])
                        return {"tool": name, "arguments": item["arguments"], "text": text,
                                "allowed_by_stella": config.tool_allowed(name), "ms": round((time.monotonic() - begin) * 1000)}
                    except Exception as error:  # Stella raises sanitized McpClientError/McpToolError
                        return {"tool": name, "arguments": item["arguments"], "error": f"{type(error).__name__}: {error}",
                                "ms": round((time.monotonic() - begin) * 1000)}
                outcomes = await asyncio.gather(*(call(item) for item in step))
                for outcome, expected in zip(outcomes, expected_step, strict=True):
                    outcome["problems"] = judge(expected, outcome)
                steps.append(outcomes)
            passed = all(not outcome["problems"] for step in steps for outcome in step)
            report_cases.append({"case_id": case["case_id"], "class": case["class"], "plugin": case["plugin"],
                                 "artifact": case["artifact"], "host": host,
                                 "result": "passed" if passed else "failed", "steps": steps})
    finally:
        await client.close()
    return {
        "run_id": args.run_id,
        "status": "completed",
        "client": "Stella capability.providers.mcp.client.McpServerClient",
        "catalog": catalog,
        "wait_for_tools_ms": wait_ms,
        "cases": report_cases,
        "passed": sum(case["result"] == "passed" for case in report_cases),
        "total": len(report_cases),
        "duration_ms": round((time.monotonic() - started) * 1000),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stella-root", required=True, type=Path)
    parser.add_argument("--mcp-config", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--wait-seconds", type=float, default=420)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit(f"refusing to overwrite {args.output}")
    report = asyncio.run(run(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({key: report.get(key) for key in ("run_id", "status", "passed", "total", "duration_ms")}))


if __name__ == "__main__":
    main()
