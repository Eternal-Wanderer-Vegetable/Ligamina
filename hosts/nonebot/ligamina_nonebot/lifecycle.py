"""Version-pinned NoneBot driver lifecycle used by the stdio Host worker."""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from typing import Any

import nonebot
from nonebot.log import logger, logger_id

from .adapter import BridgeAdapter, PlatformCall, ServiceCall


class HostLifecycle:
    """Own one NoneBot application and its native driver lifespan."""

    def __init__(
        self,
        *,
        host_id: str,
        platform_call: PlatformCall,
        service_call: ServiceCall | None = None,
    ) -> None:
        self.host_id = host_id
        self.platform_call = platform_call
        self.service_call = service_call
        self.driver: Any | None = None
        self.adapter: BridgeAdapter | None = None
        self._lifespan: Any | None = None
        self._started = False

    async def start(self, plugins: Sequence[str] = ()) -> None:
        if self._started:
            return
        if self.driver is not None:
            raise RuntimeError("NoneBot lifecycle cannot be started twice")

        nicknames = {
            value.strip()
            for value in os.environ.get("LIGAMINA_NONEBOT_NICKNAMES", "").split(",")
            if value.strip()
        }
        # NoneBot 2.5.0's default handler writes to stdout, which would corrupt
        # the JSON-RPC stream. Route framework diagnostics to the worker's stderr.
        if logger_id is not None:
            logger.remove(logger_id)
        logger.add(sys.stderr)
        nonebot.init(driver="~fastapi", nickname=nicknames)
        self.driver = nonebot.get_driver()
        self.driver.register_adapter(
            BridgeAdapter,
            host_id=self.host_id,
            platform_call=self.platform_call,
            service_call=self.service_call,
            nickname=next(iter(sorted(nicknames)), ""),
        )
        # Driver.register_adapter has no return value in NoneBot 2.5.0. This pinned
        # lookup stays inside the lifecycle boundary and avoids importing the
        # network-facing OneBot Adapter.
        self.adapter = self.driver._adapters[BridgeAdapter.get_name()]
        for plugin in plugins:
            if not plugin.strip():
                continue
            loaded = nonebot.load_plugin(plugin)
            if loaded is None:
                raise RuntimeError(f"NoneBot plugin could not be loaded: {plugin}")

        # NoneBot 2.5.0 exposes the driver's lifespan as a private context manager.
        # Keep this version-pinned seam isolated here and exercise startup/shutdown.
        self._lifespan = getattr(self.driver, "_lifespan", None)
        if self._lifespan is None or not hasattr(self._lifespan, "__aenter__"):
            raise RuntimeError("locked NoneBot driver has no supported lifespan context")
        await self._lifespan.__aenter__()
        self._started = True

    async def stop(self) -> None:
        if not self._started:
            return
        lifespan, self._lifespan = self._lifespan, None
        self._started = False
        if lifespan is not None:
            await lifespan.__aexit__(None, None, None)


def configured_plugins() -> list[str]:
    return [
        name.strip()
        for name in os.environ.get("LIGAMINA_NONEBOT_PLUGINS", "").split(",")
        if name.strip()
    ]
