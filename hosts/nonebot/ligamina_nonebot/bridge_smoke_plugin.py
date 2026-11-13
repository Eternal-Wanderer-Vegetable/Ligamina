"""Self-authored plugin proving the bridge enters the native Matcher route."""

from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent

matcher = on_message(priority=1, block=True)
observed: list[dict[str, object]] = []


@matcher.handle()
async def respond(bot: Bot, event: GroupMessageEvent) -> None:
    observed.append({
        "message": str(event.get_message()),
        "original_message": str(event.original_message),
        "to_me": event.to_me,
        "reply_message_id": event.reply.message_id if event.reply else None,
        "sender_nickname": event.sender.nickname,
    })
    if event.get_plaintext() == "forward":
        await bot.call_api(
            "send_group_forward_msg",
            group_id=event.group_id,
            messages=[
                {"type": "node", "data": {"name": "Alpha", "uin": "9000", "content": "first"}},
                {
                    "type": "node",
                    "data": {
                        "name": "Beta",
                        "uin": "9012",
                        "content": [
                            {"type": "text", "data": {"text": "second"}},
                            {"type": "at", "data": {"qq": "23"}},
                        ],
                    },
                },
            ],
        )
        return
    await bot.send(event, "pong", reply_message=True)
