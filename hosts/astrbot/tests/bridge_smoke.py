"""Boot the real AstrBot lifecycle headless and dispatch events through the bridge."""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from ligamina_astrbot import contracts  # noqa: E402
from ligamina_astrbot.lifecycle import HostLifecycle  # noqa: E402

PROTOCOL = contracts._EVENT_VALIDATOR.schema["properties"]["schema_version"]["const"]


def envelope(call: int, text: str, *, kind: str = "private", conversation: str = "") -> dict:
    actor = f"actor-{call}"
    return {
        "schema_version": PROTOCOL,
        "event_id": f"event-{call}",
        "trace_id": f"trace-{call}",
        "platform": "ligamina.tool",
        "account_id": "ligamina-tool",
        "conversation": {"kind": kind, "id": conversation or actor},
        "actor_id": actor,
        "timestamp": "2026-11-07T00:00:00Z",
        "target_host": "astrbot-test",
        "message": [{"type": "text", "text": text}],
        "extensions": {"ligamina.tool": {"message_id": f"message-{call}"}},
    }


async def main() -> None:
    calls: list[dict] = []

    async def platform_call(operation: dict) -> dict:
        calls.append(operation)
        return {"status": "succeeded", "platform_message_id": str(9_000 + len(calls))}

    with tempfile.TemporaryDirectory(prefix="ligamina-astrbot-", ignore_cleanup_errors=True) as root:
        lifecycle = HostLifecycle(host_id="astrbot-test", platform_call=platform_call, data_dir=root)
        await lifecycle.start([str(HERE / "fixtures" / "ligamina_smoke")])
        try:
            from ligamina_astrbot.commands import describe_commands
            from ligamina_astrbot.inventory import describe_runtime_inventory
            from jsonschema import Draft202012Validator

            schema_path = HERE.parents[2] / "contracts" / "lab" / "inventory" / "v1" / "capability-inventory.schema.json"
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            inventory = describe_runtime_inventory(lifecycle)
            assert list(Draft202012Validator(schema).iter_errors(inventory)) == [], inventory
            assert inventory["source"]["kind"] == "runtime"
            assert {item["name"] for item in inventory["capabilities"]["commands"]["items"]} >= {
                "ligamina_echo", "ligamina_add", "ligamina_twice", "ligamina_admin", "ligamina_proactive",
            }
            commands = describe_commands()
            print(commands, file=sys.stderr)
            for descriptor in commands:
                assert contracts.validate_command_descriptor(descriptor) == [], descriptor
            by_id = {descriptor["command_id"]: descriptor for descriptor in commands}
            assert set(by_id) == {"ligamina_echo", "ligamina_add", "ligamina_twice", "ligamina_admin", "ligamina_proactive"}, sorted(by_id)
            assert by_id["ligamina_echo"]["aliases"] == ["/lecho"], by_id["ligamina_echo"]
            assert by_id["ligamina_add"]["arguments"] == [
                {"name": "left", "required": True, "type": "integer"},
                {"name": "right", "required": True, "type": "integer"},
            ], by_id["ligamina_add"]
            assert by_id["ligamina_echo"]["arguments"] == [{"name": "text", "required": True, "type": "text"}]
            assert "Add two integers" in by_id["ligamina_add"]["description"], by_id["ligamina_add"]
            bridge = lifecycle.bridge
            await bridge.dispatch(envelope(1, "/ligamina_echo hello astrbot"))
            await bridge.dispatch(envelope(2, "/ligamina_add 2 40"))
            await bridge.dispatch(envelope(3, "/ligamina_twice x"))
            await bridge.dispatch(envelope(4, "/lecho alias"))
            await bridge.dispatch(envelope(5, "/ligamina_echo in group", kind="group", conversation="room-1"))
        finally:
            await lifecycle.stop()

    by_trace: dict[str, list[str]] = {}
    for operation in calls:
        assert operation["operation"] == "send_message", operation
        by_trace.setdefault(operation["trace_id"], []).append(operation["params"]["message"][0]["text"])
    print({trace: texts for trace, texts in sorted(by_trace.items())}, file=sys.stderr)
    assert by_trace.get("trace-1") == ["HELLO ASTRBOT"], by_trace
    assert by_trace.get("trace-2") == ["sum:42"], by_trace
    assert by_trace.get("trace-3") == ["first:x", "second:x"], by_trace
    assert by_trace.get("trace-4") == ["ALIAS"], by_trace
    assert by_trace.get("trace-5") == ["IN GROUP"], by_trace
    group_send = next(op for op in calls if op["trace_id"] == "trace-5")
    assert group_send["target"] == {"kind": "group", "id": "room-1"}, group_send
    print("AstrBot bridge smoke passed", file=sys.stderr)


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUTF8", "1")
    asyncio.run(main())
