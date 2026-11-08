"""Discover native AstrBot command handlers as v0.2 command descriptors."""

from __future__ import annotations

import inspect
from typing import Any

MAX_COMMANDS = 512


def describe_commands(wake_prefix: str = "/") -> list[dict[str, Any]]:
    """Describe ``filter.command`` handlers of loaded, activated, non-reserved plugins.

    AstrBot's built-in stars (``reserved`` metadata, e.g. ``builtin_commands`` with
    plugin installation and configuration commands) are never reported. Command
    groups report their full path (``group.sub``); the invocation uses the configured
    wake prefix and spaces, exactly as a user would type it.
    """
    from astrbot.core.star.filter.command import CommandFilter, GreedyStr
    from astrbot.core.star.star import star_map
    from astrbot.core.star.star_handler import star_handlers_registry

    descriptors: list[dict[str, Any]] = []
    seen: set[str] = set()
    for handler in star_handlers_registry:
        if not getattr(handler, "enabled", True):
            continue
        star = star_map.get(handler.handler_module_path)
        if star is None or getattr(star, "reserved", False) or not getattr(star, "activated", True):
            continue
        for command in (item for item in handler.event_filters if isinstance(item, CommandFilter)):
            parents = [name for name in (command.parent_command_names or []) if name]
            path = [*parents[0].split()] if parents else []
            path.append(command.command_name)
            if not all(part and not any(char.isspace() for char in part) for part in path):
                continue
            command_id = ".".join(path)
            if command_id in seen or len(descriptors) >= MAX_COMMANDS:
                continue
            seen.add(command_id)
            invocation = wake_prefix + " ".join(path)
            arguments = []
            for name, annotation in (getattr(command, "handler_params", None) or {}).items():
                greedy = annotation is GreedyStr
                has_default = not isinstance(annotation, type)
                kind = "text" if greedy else _type_name(annotation if isinstance(annotation, type) else type(annotation))
                arguments.append({"name": name[:128], "required": not has_default, "type": kind})
            descriptor: dict[str, Any] = {
                "command_id": command_id[:96],
                "invocation": invocation[:256],
                "framework": "astrbot",
                "usage": " ".join([invocation, *(f"<{a['name']}>" if a["required"] else f"[{a['name']}]" for a in arguments)])[:1024],
            }
            aliases = sorted(wake_prefix + " ".join([*path[:-1], alias]) for alias in (command.alias or set()) if alias)
            if aliases:
                descriptor["aliases"] = [alias[:256] for alias in aliases[:64]]
            description = (handler.desc or inspect.getdoc(handler.handler) or "").strip()
            plugin_name = getattr(star, "name", None)
            if plugin_name:
                description = f"{description} (plugin {plugin_name})".strip()
            if description:
                descriptor["description"] = description[:2048]
            if arguments:
                descriptor["arguments"] = arguments[:64]
            descriptors.append(descriptor)
    return descriptors


def _type_name(kind: type) -> str:
    if kind is bool:
        return "boolean"
    if kind is int:
        return "integer"
    if kind is float:
        return "number"
    return "string"
