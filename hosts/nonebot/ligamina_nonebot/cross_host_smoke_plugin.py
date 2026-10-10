"""Cross-Host fixture that calls a named capability through Core."""

from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, MessageEvent

matcher = on_message(priority=1, block=True)


@matcher.handle()
async def invoke_remote_capability(bot: Bot, event: MessageEvent) -> None:
    if str(event.get_message()).strip() != "cross-host":
        return
    result = await bot.adapter.invoke_service(
        "ligamina.koishi.math.add",
        {"left": 19, "right": 23},
    )
    await bot.send(event, f"cross-host:{result}")
