"""Natural-language plugin calls through Stella's capability executor (P6 field experiment).

Level 2 of the Stella MCP experiment: Stella's own Comes executor
(``capability.comes.executor.execute``) receives a natural-language objective, its
restricted agent asks a local model which tool to call and with which arguments, and
the call goes through Stella's MCP client to the field Core. Run with Stella's
interpreter; see ``run_stella_mcp_experiment.ps1 -Level comes``.

Safety: the model endpoint is pinned to a local OpenAI-compatible server (LM Studio by
default). The plugin-role endpoint resolver and ``chat_completion`` are both replaced
in-process, and any request to another base URL raises. No chat platform is involved:
the event is Stella's test event over a recording fake bot, and the driver asserts that
the fake bot sent nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import re
import sys
import time
from pathlib import Path

CASES = [
    {
        "case_id": "comes-roll-2d6",
        "capability": "dice.roll",
        "objective": "帮我掷两个六面骰子",
        "remote_tool": "cmd.field-nonebot-roll.rd",
        "expect": r"^你掷出了2个6面骰子, 点数为【([2-9]|1[0-2])】$",
    },
    {
        "case_id": "comes-echo",
        "capability": "text.echo",
        "objective": "请让复读插件原样复述这句话：field-check-42",
        "remote_tool": "cmd.field-koishi-echo.echo",
        "expect": r"^field-check-42$",
    },
    {
        "case_id": "comes-teamup-nickname",
        "capability": "teamup.nickname",
        "objective": "把我的组队昵称设置成 测试员",
        "remote_tool": "cmd.field-astrbot-teamup.____-f0d4f036",
        "expect": r"^已设置昵称：测试员$",
    },
]

DECLARATIONS = '''
[[capability]]
id = "dice.roll"
description = "掷骰子：调用 NoneBot 插件 nonebot_plugin_roll。参数写成 [x]d[y]，例如 1d6、2d20。"
examples = ["帮我掷个骰子", "扔两个六面骰子", "roll 3d6"]
keywords = ["掷骰", "骰子"]
providers = [{ kind = "mcp", server = "plugins", tool = "cmd.field-nonebot-roll.rd" }]

[[capability]]
id = "text.echo"
description = "复读：调用 Koishi 插件 @koishijs/plugin-echo，原样发送给定文本。"
examples = ["复述这句话", "echo hello"]
keywords = ["复述", "复读"]
providers = [{ kind = "mcp", server = "plugins", tool = "cmd.field-koishi-echo.echo" }]

[[capability]]
id = "teamup.nickname"
description = "组队报名：调用 AstrBot 插件 astrbot_plugin_teamup 的 组队昵称 命令，设置自己在组队报名中的昵称。"
examples = ["把我的组队昵称改成小明", "组队昵称设为 阿强"]
keywords = ["组队昵称"]
providers = [{ kind = "mcp", server = "plugins", tool = "cmd.field-astrbot-teamup.____-f0d4f036" }]
'''


def pin_model(base_url: str, model: str) -> list[dict]:
    """Route every model call made by Stella's plugin role to the local endpoint."""
    import astrbot_compat.llm.provider as provider  # Stella
    import core.llm.openai_client as openai_client  # Stella

    calls: list[dict] = []
    original = openai_client.chat_completion

    def local_args(_model: str, endpoint=None) -> dict:
        return {"base_url": base_url, "api_key": "", "kind": "", "slot": "field-local", "role": "plugin",
                "timeout": 180.0, "model": model, "temperature": 0.2, "max_tokens": 1024}

    async def guarded(messages, *, base_url: str, **kwargs):
        if base_url.rstrip("/") != pinned:
            raise RuntimeError(f"refusing model call to {base_url}: only {pinned} is allowed")
        begin = time.monotonic()
        response = await original(messages, base_url=base_url, **kwargs)
        calls.append({"model": kwargs.get("model"), "tools": [t.get("function", {}).get("name") for t in kwargs.get("tools") or []],
                      "ms": round((time.monotonic() - begin) * 1000)})
        return response

    pinned = base_url.rstrip("/")
    provider._plugin_call_args = local_args
    provider._plugin_binding = lambda: None
    openai_client.chat_completion = guarded
    return calls


def stella_test_event(stella_root: Path):
    """Stella's own test event (``tests/capability/conftest.py``): a real AstrMessageEvent over a fake bot."""
    spec = importlib.util.spec_from_file_location("stella_capability_conftest", stella_root / "tests/capability/conftest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from astrbot_compat.events import build_event  # Stella

    bot = module.FakeBot()
    return bot, build_event(module.FakeNbEvent(), bot)


async def run(args: argparse.Namespace) -> dict:
    sys.path.insert(0, str(args.stella_root))
    from capability.adapters.mcp import install_mcp_runtime
    from capability.comes.executor import execute
    from capability.loader import load_capability_file
    from capability.providers.mcp.client import McpServerClient
    from capability.providers.mcp.manager import McpServerManager
    from capability.providers.mcp.model import SERVER_READY, load_mcp_configs
    from capability.registry import CapabilityRegistry
    from config import settings
    from core.tasks import Task, TaskType

    settings.COMES_ENABLED = True
    settings.ASTRBOT_LLM_ENABLED = True
    model_calls = pin_model(args.model_base_url, args.model)

    config = load_mcp_configs(args.mcp_config)["plugins"]
    started = time.monotonic()
    deadline = started + args.wait_seconds
    while True:
        client = McpServerClient(config)
        await client.start()
        catalog = {d.name for d in client.catalog()} if client.status_dict()["state"] == SERVER_READY else set()
        if all(case["remote_tool"] in catalog for case in CASES):
            break
        await client.close()
        if time.monotonic() > deadline:
            return {"run_id": args.run_id, "status": "not_ready", "catalog": sorted(catalog)}
        await asyncio.sleep(5)
    schemas = {d.name: json.loads(json.dumps(getattr(d, "input_schema", None) or getattr(d, "inputSchema", None), default=str))
               for d in client.catalog() if d.name in {c["remote_tool"] for c in CASES}}
    manager = McpServerManager()
    manager._clients["plugins"] = client  # same wiring as Stella's executor tests

    declarations = args.mcp_config.parent / "capabilities.toml"
    declarations.write_text(DECLARATIONS, encoding="utf-8")
    registry = CapabilityRegistry()
    loaded = load_capability_file(declarations, registry)
    install_mcp_runtime(manager=manager, target=registry)

    bots = []
    results = []
    try:
        for case in CASES:
            for attempt in range(1, args.attempts + 1):
                bot, event_coro = stella_test_event(args.stella_root)
                bots.append(bot)
                event = await event_coro
                begin = time.monotonic()
                calls_before = len(model_calls)
                result = await execute(
                    Task(task_id=f"{case['case_id']}-{attempt}", type=TaskType.TOOL_EXECUTE,
                         capability=case["capability"], objective=case["objective"]),
                    event=event, target=registry,
                )
                outputs = [{"tool": name, "text": text} for name, text in (result.data or [])]
                texts = [o["text"] for o in outputs]
                ok = bool(texts) and all(re.match(case["expect"], t) for t in texts)
                results.append({
                    "case_id": case["case_id"], "attempt": attempt, "objective": case["objective"],
                    "status": str(getattr(result.status, "value", result.status)),
                    "tool_outputs": outputs, "summary": result.summary,
                    "metadata": json.loads(json.dumps(result.metadata or {}, ensure_ascii=False, default=str)),
                    "model_calls": len(model_calls) - calls_before,
                    "ms": round((time.monotonic() - begin) * 1000),
                    "result": "passed" if ok else "failed",
                })
    finally:
        await client.close()
    return {
        "run_id": args.run_id,
        "status": "completed",
        "executor": "Stella capability.comes.executor.execute + McpServerClient",
        "model": {"base_url": args.model_base_url, "model": args.model},
        "capabilities_loaded": loaded,
        "tool_schemas": schemas,
        "platform_sends_by_fake_bot": sum(len(b.sent) + len(b.actions) for b in bots),
        "results": results,
        "passed": sum(r["result"] == "passed" for r in results),
        "total": len(results),
        "model_calls_total": len(model_calls),
        "duration_ms": round((time.monotonic() - started) * 1000),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stella-root", required=True, type=Path)
    parser.add_argument("--mcp-config", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--model-base-url", default="http://127.0.0.1:1234",
                        help="Stella convention: no /v1 suffix (its client appends /v1/chat/completions)")
    parser.add_argument("--model", default="google/gemma-4-e4b")
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--wait-seconds", type=float, default=420)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit(f"refusing to overwrite {args.output}")
    if not re.match(r"^http://(127\.0\.0\.1|localhost)(:\d+)?/?$", args.model_base_url):
        raise SystemExit("the model endpoint must be a local loopback server")
    report = asyncio.run(run(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({k: report.get(k) for k in ("run_id", "status", "passed", "total", "platform_sends_by_fake_bot", "duration_ms")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
