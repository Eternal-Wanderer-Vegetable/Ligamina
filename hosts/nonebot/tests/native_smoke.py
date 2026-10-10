"""Run with ``uv run --locked python tests/native_smoke.py``.

This calls the locked FastAPI driver's lifespan directly, so the real
NoneBot startup and shutdown hooks run without binding a network socket.
"""

import asyncio
import importlib
import sys
from pathlib import Path

import nonebot
from nonebot.adapters.onebot.v11 import Adapter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
nonebot.init(driver="~fastapi")
driver = nonebot.get_driver()
driver.register_adapter(Adapter)
assert nonebot.load_plugin("ligamina_nonebot.smoke_plugin") is not None
plugin = importlib.import_module("ligamina_nonebot.smoke_plugin")


async def main() -> None:
    # NoneBug uses this same native driver lifespan in its test harness.
    async with driver._lifespan:
        assert plugin.lifecycle == ["started"]
    assert plugin.lifecycle == ["started", "stopped"]
    print("NoneBot 2.5.0 + OneBot v11 2.4.6 native plugin lifecycle passed (no socket opened)")


asyncio.run(main())
