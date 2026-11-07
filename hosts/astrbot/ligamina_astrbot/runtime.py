"""Host state shared with the bridge adapter. Must not import AstrBot.

AstrBot has import-time side effects relative to its data root, so nothing that
imports ``astrbot`` may be loaded before the lifecycle sets that root.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

ADAPTER_NAME = "ligamina_bridge"
PlatformCall = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

# The Host sets these before the AstrBot lifecycle instantiates the adapter, because
# AstrBot constructs platform adapters itself from cmd_config.json.
RUNTIME: dict[str, Any] = {"platform_call": None, "host_id": None, "instance": None}
