"""AstrBot event-path parity: waking, permissions, quoted replies, proactive sends."""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
os.environ["LIGAMINA_ASTRBOT_ADMINS"] = "admin-1"

from ligamina_astrbot import contracts  # noqa: E402
from ligamina_astrbot.lifecycle import HostLifecycle  # noqa: E402

PROTOCOL = contracts._EVENT_VALIDATOR.schema["properties"]["schema_version"]["const"]
BOT = "bot-1"


def group_event(call: int, segments: list[dict], *, actor: str = "member-1") -> dict:
    return {
        "schema_version": PROTOCOL,
        "event_id": f"event-{call}",
        "trace_id": f"trace-{call}",
        "platform": "onebot.v11",
        "account_id": BOT,
        "conversation": {"kind": "group", "id": "room-1"},
        "actor_id": actor,
        "timestamp": "2026-11-08T00:00:00Z",
        "target_host": "astrbot-parity",
        "message": segments,
        "extensions": {"onebot.v11": {"message_id": str(1000 + call)}},
    }


def text(value: str) -> dict:
    return {"type": "text", "text": value}


async def main() -> None:
    calls: list[dict] = []

    async def platform_call(operation: dict) -> dict:
        calls.append(operation)
        if operation["operation"] == "get_message":
            if operation["params"]["message_id"] == "777":
                return {"status": "succeeded", "data": {
                    "target": operation["target"], "message_id": "777", "actor_id": BOT,
                    "timestamp": "2026-11-08T00:00:00Z", "message": [text("earlier bot message")],
                }}
            return {"status": "failed", "error": "not found"}
        return {"status": "succeeded", "platform_message_id": str(5000 + len(calls))}

    def sent(trace: str) -> list[str]:
        return [op["params"]["message"][0]["text"] for op in calls
                if op["operation"] == "send_message" and op["trace_id"] == trace]

    with tempfile.TemporaryDirectory(prefix="ligamina-astrbot-parity-", ignore_cleanup_errors=True) as root:
        lifecycle = HostLifecycle(host_id="astrbot-parity", platform_call=platform_call, data_dir=root)
        await lifecycle.start([str(HERE / "fixtures" / "ligamina_smoke")])
        try:
            bridge = lifecycle.bridge
            # @bot wakes without the prefix; a plain command without @ or prefix does not.
            await bridge.dispatch(group_event(1, [{"type": "mention", "user_id": BOT}, text(" ligamina_echo hi")]))
            await bridge.dispatch(group_event(2, [text("ligamina_echo hi")]))
            await bridge.dispatch(group_event(3, [text("/ligamina_echo hi")]))
            # A message that starts by @-ing someone else does not wake the bot.
            await bridge.dispatch(group_event(4, [{"type": "mention", "user_id": "other"}, text(" /ligamina_echo hi")]))
            # Admin-only command: member vs configured admin.
            await bridge.dispatch(group_event(5, [text("/ligamina_admin")], actor="member-1"))
            await bridge.dispatch(group_event(6, [text("/ligamina_admin")], actor="admin-1"))
            # Reply to the bot's own message wakes; an unresolvable reply does not.
            await bridge.dispatch(group_event(7, [{"type": "reply", "message_id": "777"}, text("ligamina_echo quoted")]))
            await bridge.dispatch(group_event(8, [{"type": "reply", "message_id": "778"}, text("ligamina_echo quoted")]))
            # Proactive send goes out with its own trace (Core ledger path), not the event's.
            await bridge.dispatch(group_event(9, [text("/ligamina_proactive")]))
        finally:
            await lifecycle.stop()

    print({t: sent(t) for t in (f"trace-{i}" for i in range(1, 10))}, file=sys.stderr)
    assert sent("trace-1") == ["HI"], sent("trace-1")
    assert sent("trace-2") == [], sent("trace-2")
    assert sent("trace-3") == ["HI"], sent("trace-3")
    assert sent("trace-4") == [], sent("trace-4")
    assert "admin-ok" not in sent("trace-5"), sent("trace-5")
    assert sent("trace-6") == ["admin-ok"], sent("trace-6")
    assert sent("trace-7") == ["QUOTED"], sent("trace-7")
    assert sent("trace-8") == [], sent("trace-8")
    assert sent("trace-9") == ["queued"], sent("trace-9")
    proactive = [op for op in calls if op["operation"] == "send_message" and op["trace_id"].startswith("proactive-")]
    assert [op["params"]["message"][0]["text"] for op in proactive] == ["proactive-ok"], proactive
    assert proactive[0]["account_id"] == BOT and proactive[0]["target"] == {"kind": "group", "id": "room-1"}, proactive
    print("AstrBot event parity passed", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
