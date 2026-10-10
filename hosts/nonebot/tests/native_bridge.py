"""Exercise the locked native OneBot path and Bridge Adapter through a plugin."""

import asyncio
import importlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["LIGAMINA_NONEBOT_NICKNAMES"] = "Bot"

import nonebot
from ligamina_nonebot.adapter import BridgeAdapter, BridgeApiError, to_onebot_event


async def main() -> None:
    calls: list[dict] = []

    async def platform_call(operation: dict) -> dict:
        calls.append(operation)
        if operation["operation"] == "get_message":
            return {
                "status": "succeeded",
                "data": {
                    "target": operation["target"],
                    "message_id": "77",
                    "real_id": "77",
                    "actor_id": "9000",
                    "timestamp": "2026-10-10T01:02:03Z",
                    "message": [{"type": "text", "text": "previous"}],
                    "nickname": "Bot",
                },
            }
        return {"status": "succeeded", "platform_message_id": "501"}

    nonebot.init(driver="~fastapi", nickname={"Bot"})
    driver = nonebot.get_driver()
    driver.register_adapter(
        BridgeAdapter,
        host_id="nonebot-test",
        platform_call=platform_call,
        nickname="Bot",
    )
    bridge = driver._adapters[BridgeAdapter.get_name()]
    assert nonebot.load_plugin("ligamina_nonebot.bridge_smoke_plugin") is not None
    plugin = importlib.import_module("ligamina_nonebot.bridge_smoke_plugin")

    try:
        async with driver._lifespan:
            event = {
                "schema_version": "ligamina/0.1",
                "event_id": "event-reply-at",
                "trace_id": "trace-reply-at",
                "platform": "onebot.v11",
                "account_id": "9000",
                "conversation": {"kind": "group", "id": "42"},
                "actor_id": "23",
                "timestamp": "2026-10-10T01:02:03Z",
                "target_host": "nonebot-test",
                "message": [
                    {"type": "reply", "message_id": "77"},
                    {"type": "mention", "user_id": "9000"},
                    {"type": "text", "text": "ping"},
                ],
                "extensions": {
                    "onebot.v11": {
                        "message_id": "78",
                        "sender": {"user_id": "23", "nickname": "Alice"},
                    }
                },
            }
            await bridge.dispatch(event)

            nickname_event = {**event, "event_id": "event-nickname", "trace_id": "trace-nickname",
                "extensions": {"onebot.v11": {"message_id": "79", "sender": {"user_id": "23", "nickname": "Alice"}}},
                "message": [{"type": "text", "text": "Bot ping"}],
            }
            await bridge.dispatch(nickname_event)

            bot = next(iter(bridge.bots.values()))
            await bot.call_api("send_group_msg", group_id=42, message="[CQ:at,qq=23]", auto_escape=True)
            await bot.call_api("send_private_msg", user_id=23, message="[CQ:at,qq=23]", auto_escape=False)

        assert len(plugin.observed) == 2, plugin.observed
        first, second = plugin.observed
        assert first == {
            "message": "ping",
            "original_message": "[CQ:reply,id=77][CQ:at,qq=9000]ping",
            "to_me": True,
            "reply_message_id": 77,
            "sender_nickname": "Alice",
        }, first
        assert second["message"] == "ping" and second["to_me"] is True, second

        sends = [call for call in calls if call["operation"] == "send_message"]
        assert len(sends) == 4, sends
        assert sends[0]["target"] == {"kind": "group", "id": "42"}
        assert sends[0]["trace_id"] == "trace-reply-at"
        assert sends[0]["params"]["message"] == [
            {"type": "reply", "message_id": "78"},
            {"type": "text", "text": "pong"},
        ], sends[0]
        assert sends[1]["trace_id"] == "trace-nickname"
        assert len([call for call in calls if call["operation"] == "get_message"]) == 1
        assert sends[2]["target"] == {"kind": "group", "id": "42"}, sends[2]
        assert sends[2]["params"]["message"] == [{"type": "text", "text": "[CQ:at,qq=23]"}], sends[2]
        assert sends[3]["target"] == {"kind": "private", "id": "23"}, sends[3]
        assert sends[3]["params"]["message"] == [{"type": "mention", "user_id": "23"}], sends[3]

        private_event = to_onebot_event({
            **event,
            "event_id": "private-conversation",
            "conversation": {"kind": "private", "id": "23"},
            "extensions": {"onebot.v11": {"message_id": "80"}},
            "message": [{"type": "text", "text": "hello"}],
        })
        assert private_event.message_type == "private" and private_event.user_id == 23
        assert not hasattr(private_event, "group_id")

        try:
            to_onebot_event({**event, "account_id": "9223372036854775808"})
        except BridgeApiError as error:
            assert "numeric ID profile" in str(error)
        else:
            raise AssertionError("out-of-profile OneBot ID was accepted")
    finally:
        await bridge.shutdown()

    print("NoneBot native Bridge Adapter: dispatch, reply/at/nickname, send API aliases/auto_escape, get_msg and shutdown passed")


if __name__ == "__main__":
    asyncio.run(main())
