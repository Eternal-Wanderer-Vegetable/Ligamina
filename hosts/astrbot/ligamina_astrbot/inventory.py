"""Runtime view of AstrBot plugin registrations; kept outside the MCP tool surface."""

from __future__ import annotations

import os
import re
from typing import Any

MAX_ITEMS = 4096


def describe_runtime_inventory(lifecycle: Any) -> dict[str, Any]:
    commands: list[dict[str, Any]] = []
    handlers: list[dict[str, Any]] = []
    evidence = ["astrbot.core.star.star_handler.star_handlers_registry"]
    if lifecycle is not None and lifecycle.core is not None:
        from astrbot.core.star.star import star_map
        from astrbot.core.star.star_handler import star_handlers_registry

        for index, handler in enumerate(star_handlers_registry):
            if not getattr(handler, "enabled", True):
                continue
            star = star_map.get(handler.handler_module_path)
            if star is None or getattr(star, "reserved", False) or not getattr(star, "activated", True):
                continue
            owner = getattr(star, "name", None) or handler.handler_module_path
            for event_filter in handler.event_filters:
                class_name = type(event_filter).__name__
                if class_name == "CommandFilter":
                    parents = [part for part in (getattr(event_filter, "parent_command_names", None) or []) if part]
                    prefix = " ".join(parents[0].split()) if parents else ""
                    name = " ".join(part for part in (prefix, getattr(event_filter, "command_name", "")) if part)
                    arguments = _command_arguments(event_filter)
                    commands.append(_item(
                        "command", name, f"star_handlers_registry[{index}].event_filters.CommandFilter",
                        owner, {"aliases": sorted(getattr(event_filter, "alias", ()) or ())[:64], "arguments": arguments},
                    ))
                elif class_name == "CommandGroupFilter":
                    names = getattr(event_filter, "get_complete_command_names", None)
                    values = names() if callable(names) else [getattr(event_filter, "group_name", "command-group")]
                    for name in values[:64]:
                        commands.append(_item("command_group", str(name), f"star_handlers_registry[{index}].event_filters.CommandGroupFilter", owner, {}))
                else:
                    kind, name = _handler_filter(event_filter)
                    handlers.append(_item(kind, name, f"star_handlers_registry[{index}].event_filters.{class_name}", owner, {"event_type": str(handler.event_type)}))

        manager = getattr(getattr(lifecycle.core, "provider_manager", None), "llm_tools", None)
        for index, tool in enumerate(getattr(manager, "func_list", ()) or ()):
            module_path = getattr(tool, "handler_module_path", None) or getattr(getattr(tool, "handler", None), "__module__", None)
            if not module_path or not module_path.startswith("data.plugins."):
                continue
            parameters = getattr(tool, "parameters", None)
            properties = parameters.get("properties", {}) if isinstance(parameters, dict) else {}
            handlers.append(_item(
                "llm_tool", str(getattr(tool, "name", f"llm_tool_{index}")),
                f"provider_manager.llm_tools.func_list[{index}]", module_path.split(".")[2],
                {"description": str(getattr(tool, "description", ""))[:512], "parameters": _parameter_summary(properties)},
            ))
        evidence.append("astrbot.core.provider.FunctionToolManager.func_list")

    plugin_name = os.environ.get("LIGAMINA_INVENTORY_PACKAGE") or os.pathsep.join(
        item.strip() for item in os.environ.get("LIGAMINA_ASTRBOT_PLUGINS", "").split(os.pathsep) if item.strip()
    ) or "loaded-plugins"
    plugin: dict[str, str] = {"package": plugin_name[:256]}
    _copy_identity(plugin, "registry_entry_id", "LIGAMINA_INVENTORY_ENTRY_ID", 128)
    _copy_identity(plugin, "version", "LIGAMINA_INVENTORY_VERSION", 128)
    artifact_hash = os.environ.get("LIGAMINA_INVENTORY_ARTIFACT_SHA256", "")
    if re.fullmatch(r"[a-f0-9]{64}", artifact_hash):
        plugin["artifact_sha256"] = artifact_hash
    return {
        "schema_version": 1,
        "ecosystem": "astrbot",
        "plugin": plugin,
        "source": {
            "kind": "runtime",
            "collector": "astrbot-runtime/1",
            "confidence": "observed",
            "evidence": [{"reference": value} for value in evidence],
        },
        "capabilities": {
            "commands": _observed(commands),
            "handlers": _observed(handlers),
            "services": _unavailable("AstrBot Star dependency declarations do not expose a stable runtime service-registration catalogue."),
            "schedulers": _unavailable("Scheduler registrations are owned by plugin or optional scheduler components."),
            "configuration": _unavailable("Runtime config values are intentionally excluded; schema files are collected statically."),
        },
        "limitations": ["Only enabled, activated, non-reserved Star registrations are included; runtime config values are never emitted."],
    }


def _handler_filter(event_filter: Any) -> tuple[str, str]:
    class_name = type(event_filter).__name__
    if class_name == "RegexFilter":
        return "regex", str(getattr(event_filter, "regex_str", "regex"))[:256]
    if class_name == "EventMessageTypeFilter":
        return "event_message_type", str(getattr(event_filter, "event_message_type", class_name))[:256]
    return class_name[:64], class_name


def _command_arguments(event_filter: Any) -> list[dict[str, Any]]:
    result = []
    for name, annotation in (getattr(event_filter, "handler_params", None) or {}).items():
        is_type = isinstance(annotation, type)
        result.append({"name": str(name)[:128], "required": is_type, "type": getattr(annotation, "__name__", type(annotation).__name__)[:64]})
    return result[:64]


def _parameter_summary(properties: Any) -> list[dict[str, str]]:
    if not isinstance(properties, dict):
        return []
    result = []
    for name, schema in sorted(properties.items()):
        if isinstance(name, str):
            kind = schema.get("type", "unknown") if isinstance(schema, dict) else "unknown"
            result.append({"name": name[:128], "type": str(kind)[:64]})
    return result[:64]


def _item(kind: str, name: str, reference: str, owner: str, attributes: dict[str, Any]) -> dict[str, Any]:
    safe_attributes = {key: value for key, value in attributes.items() if value not in (None, "", [], {})}
    return {
        "kind": kind[:64],
        "name": (name or kind)[:256],
        "attributes": {**safe_attributes, "plugin": str(owner)[:128]},
        "evidence": [{"reference": reference}],
    }


def _observed(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"status": "observed", "items": items[:MAX_ITEMS]}


def _unavailable(reason: str) -> dict[str, Any]:
    return {"status": "unavailable", "items": [], "reason": reason}


def _copy_identity(plugin: dict[str, str], key: str, env: str, limit: int) -> None:
    value = os.environ.get(env)
    if value:
        plugin[key] = value[:limit]
