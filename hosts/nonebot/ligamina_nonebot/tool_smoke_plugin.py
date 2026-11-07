"""Self-authored NoneBot plugin used to verify native commands as tools."""

import asyncio

from nonebot import on_command
from nonebot.adapters import Message
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata

__plugin_meta__ = PluginMetadata(
    name="tool smoke",
    description="Echo commands for the command tool surface",
    usage="/ligamina.echo <text>",
)

echo = on_command(("ligamina", "echo"), aliases={"ligamina-say"}, block=True)
twice = on_command(("ligamina", "twice"), block=True)
slow = on_command(("ligamina", "slow"), block=True)


@echo.handle()
async def _(arguments: Message = CommandArg()) -> None:
    await echo.send(arguments.extract_plain_text().strip().upper())


@twice.handle()
async def _(arguments: Message = CommandArg()) -> None:
    text = arguments.extract_plain_text().strip()
    await twice.send(f"first:{text}")
    await twice.send(f"second:{text}")


@slow.handle()
async def _(arguments: Message = CommandArg()) -> None:
    delay = min(max(int(arguments.extract_plain_text().strip() or 0), 0), 10_000)
    await asyncio.sleep(delay / 1000)
    await slow.send("slow:done")
