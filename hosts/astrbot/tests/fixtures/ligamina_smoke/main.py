"""Self-authored AstrBot plugin used to verify the Ligamina bridge and command tools."""

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import GreedyStr


class LigaminaSmoke(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)

    @filter.command("ligamina_echo", alias={"lecho"})
    async def echo(self, event: AstrMessageEvent, text: GreedyStr):
        """Echo the given text back in upper case"""
        yield event.plain_result(str(text).upper())

    @filter.command("ligamina_add")
    async def add(self, event: AstrMessageEvent, left: int, right: int):
        """Add two integers"""
        yield event.plain_result(f"sum:{left + right}")

    @filter.command("ligamina_twice")
    async def twice(self, event: AstrMessageEvent, text: GreedyStr):
        """Send the text in two separate messages"""
        await event.send(_chain(f"first:{text}"))
        yield event.plain_result(f"second:{text}")


def _chain(text: str):
    from astrbot.core.message.components import Plain
    from astrbot.core.message.message_event_result import MessageChain

    return MessageChain([Plain(text)])
