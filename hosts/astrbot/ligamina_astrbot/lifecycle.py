"""Own one headless AstrBot core lifecycle for the stdio Host worker.

AstrBot is imported only after its data root and telemetry switches are set, because
``import astrbot.core`` creates ``<root>/data``, writes ``cmd_config.json`` and opens
the SQLite database as import-time side effects. The real ``AstrBotCoreLifecycle``
then initializes the plugin manager and pipeline; the dashboard is never started.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from .runtime import ADAPTER_NAME, RUNTIME, PlatformCall

# Settings forced for every Host: no LLM provider, no remote text-to-image, no
# telemetry, and only the Ligamina bridge as a platform.
HEADLESS_CONFIG = {
    "provider_settings": {"enable": False},
    "t2i": False,
    "disable_metrics": True,
    "platform": [{"type": ADAPTER_NAME, "id": "ligamina", "enable": True}],
}


class HostLifecycle:
    def __init__(self, *, host_id: str, platform_call: PlatformCall, data_dir: str | os.PathLike[str]) -> None:
        self.host_id = host_id
        self.platform_call = platform_call
        self.root = Path(data_dir).resolve()
        self.core: Any | None = None
        self._start_task: asyncio.Task | None = None
        self._previous_cwd: str | None = None

    async def start(self, plugin_dirs: list[str] = ()) -> None:
        if self.core is not None:
            raise RuntimeError("AstrBot lifecycle cannot be started twice")
        self.root.mkdir(parents=True, exist_ok=True)
        os.environ["ASTRBOT_ROOT"] = str(self.root)
        os.environ["ASTRBOT_DISABLE_METRICS"] = "1"
        os.environ.pop("ASTRBOT_RELOAD", None)
        plugins = self.root / "data" / "plugins"
        plugins.mkdir(parents=True, exist_ok=True)
        for source in plugin_dirs:
            source_path = Path(source).resolve()
            if not (source_path / "main.py").is_file() and not (source_path / f"{source_path.name}.py").is_file():
                raise RuntimeError(f"AstrBot plugin directory has no main.py: {source_path}")
            target = plugins / source_path.name
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(source_path, target)
        # AstrBot imports plugins as `data.plugins.<dir>.main` relative to its root,
        # and some of its stores (knowledge base, templates) use the working directory.
        if str(self.root) not in sys.path:
            sys.path.insert(0, str(self.root))
        self._previous_cwd = os.getcwd()
        os.chdir(self.root)
        if "astrbot" in sys.modules:
            raise RuntimeError("AstrBot was imported before its data root was set")

        RUNTIME["platform_call"] = self.platform_call
        RUNTIME["host_id"] = self.host_id

        from astrbot.core import LogBroker, astrbot_config, db_helper

        from . import bridge  # noqa: F401  registers the platform adapter
        from astrbot.core.core_lifecycle import AstrBotCoreLifecycle

        for key, value in HEADLESS_CONFIG.items():
            if isinstance(value, dict) and isinstance(astrbot_config.get(key), dict):
                astrbot_config[key].update(value)
            else:
                astrbot_config[key] = value
        astrbot_config.save_config()

        self.core = AstrBotCoreLifecycle(LogBroker(), db_helper)
        await self.core.initialize()
        self._start_task = asyncio.create_task(self.core.start())
        for _ in range(200):
            if RUNTIME["instance"] is not None:
                return
            await asyncio.sleep(0.05)
        raise RuntimeError("the Ligamina bridge platform did not start")

    def wake_prefix(self) -> str:
        """First configured wake prefix (AstrBot default ``/``); empty means none."""
        from astrbot.core import astrbot_config

        prefixes = astrbot_config.get("wake_prefix") or ["/"]
        return str(prefixes[0]) if prefixes else ""

    @property
    def bridge(self) -> Any:
        return RUNTIME["instance"]

    async def stop(self) -> None:
        if self.core is None:
            return
        core, self.core = self.core, None
        try:
            await core.stop()
        finally:
            if self._start_task is not None:
                self._start_task.cancel()
                await asyncio.gather(self._start_task, return_exceptions=True)
            RUNTIME.update({"platform_call": None, "host_id": None, "instance": None})
            if self._previous_cwd is not None:
                os.chdir(self._previous_cwd)


def configured_plugins() -> list[str]:
    return [item.strip() for item in os.environ.get("LIGAMINA_ASTRBOT_PLUGINS", "").split(os.pathsep) if item.strip()]
