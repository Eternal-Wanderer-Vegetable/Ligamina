"""Native NoneBot command matchers are described and run in a virtual tool session."""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["LIGAMINA_NONEBOT_NICKNAMES"] = "Bot"

from ligamina_nonebot.commands import describe_commands
from ligamina_nonebot.contracts import validate_command_descriptor
from ligamina_nonebot.lifecycle import HostLifecycle

SCHEMA_VERSION_FIELD = "schema_version"


def envelope(protocol: str, call: int, text: str) -> dict:
    actor = str(1_000_000_000_000_000 + call)
    return {
        SCHEMA_VERSION_FIELD: protocol,
        "event_id": f"tool-event-{call}",
        "trace_id": f"tool-trace-{call}",
        "platform": "onebot.v11",
        "account_id": "0",
        "conversation": {"kind": "private", "id": actor},
        "actor_id": actor,
        "timestamp": "2026-11-07T00:00:00Z",
        "target_host": "nonebot-tools",
        "message": [{"type": "text", "text": text}],
        "extensions": {"onebot.v11": {"message_id": str(2_000_000_000_000_000 + call)}},
    }


async def main() -> None:
    calls: list[dict] = []

    async def platform_call(operation: dict) -> dict:
        calls.append(operation)
        if operation["operation"] == "send_message":
            return {"status": "succeeded", "platform_message_id": str(3_000_000_000_000_000 + len(calls))}
        return {"status": "failed", "error": {"code": "unsupported_in_tool_session"}}

    lifecycle = HostLifecycle(host_id="nonebot-tools", platform_call=platform_call)
    await lifecycle.start(["ligamina_nonebot.tool_smoke_plugin"])
    try:
        commands = describe_commands(lifecycle.driver)
        for descriptor in commands:
            assert validate_command_descriptor(descriptor) == [], descriptor
        by_id = {descriptor["command_id"]: descriptor for descriptor in commands}
        echo = by_id["ligamina.echo"]
        assert echo["framework"] == "nonebot", echo
        assert echo["invocation"] == "/ligamina.echo", echo
        assert echo["aliases"] == ["/ligamina-say"], echo
        assert "Echo commands for the command tool surface" in echo["description"], echo
        assert {"ligamina.twice", "ligamina.slow"} <= set(by_id), sorted(by_id)

        # The protocol constant lives in the shared contract; read it instead of hard-coding.
        from ligamina_nonebot import contracts
        protocol = contracts._EVENT_VALIDATOR.schema["properties"]["schema_version"]["const"]

        await lifecycle.adapter.dispatch(envelope(protocol, 1, "/ligamina.echo hello tools"))
        first = [operation for operation in calls if operation["trace_id"] == "tool-trace-1"]
        assert [(o["operation"], o["account_id"], o["target"], o["params"]) for o in first] == [
            ("send_message", "0", {"kind": "private", "id": "1000000000000001"}, {"message": [{"type": "text", "text": "HELLO TOOLS"}]}),
        ], first

        await lifecycle.adapter.dispatch(envelope(protocol, 2, "/ligamina.twice x"))
        second = [o["params"]["message"][0]["text"] for o in calls if o["trace_id"] == "tool-trace-2"]
        assert second == ["first:x", "second:x"], second

        await lifecycle.adapter.dispatch(envelope(protocol, 3, "/ligamina-say alias"))
        third = [o["params"]["message"][0]["text"] for o in calls if o["trace_id"] == "tool-trace-3"]
        assert third == ["ALIAS"], third
    finally:
        await lifecycle.stop()
    print("NoneBot command tools: discovery and virtual-session dispatch passed")


if __name__ == "__main__":
    asyncio.run(main())
