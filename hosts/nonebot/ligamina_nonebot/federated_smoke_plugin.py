"""Self-authored G1 fixture that replies through the Core-owned gateway."""

from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, MessageEvent

matcher = on_message(priority=1, block=True)


@matcher.handle()
async def reply_through_gateway(bot: Bot, event: MessageEvent) -> None:
    kind = "private" if event.message_type == "private" else "group"
    text = str(event.get_message()).strip()
    await bot.send(event, f"nonebot:{kind}:{text}")
