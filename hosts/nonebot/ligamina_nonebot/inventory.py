"""Runtime view of framework registrations, separate from MCP command exposure."""

from __future__ import annotations

import os
import re
from typing import Any

from nonebot.matcher import matchers
from nonebot.rule import CommandRule, ShellCommandRule

MAX_ITEMS = 4096


def describe_runtime_inventory(driver: Any) -> dict[str, Any]:
    config = getattr(driver, "config", None)
    separators = sorted(getattr(config, "command_sep", None) or {"."})
    separator = separators[0]
    commands: list[dict[str, Any]] = []
    handlers: list[dict[str, Any]] = []
    evidence: list[str] = ["nonebot.matcher.matchers"]
    for priority in sorted(matchers.keys()):
        for index, matcher in enumerate(matchers[priority]):
            if len(handlers) >= MAX_ITEMS:
                break
            plugin = getattr(matcher, "plugin", None)
            metadata = getattr(plugin, "metadata", None)
            owner = getattr(metadata, "name", None) or getattr(plugin, "name", None)
            event_type = str(getattr(matcher, "type", "unknown"))
            specific = False
            for checker in getattr(getattr(matcher, "rule", None), "checkers", ()):
                call = getattr(checker, "call", None)
                if call is None:
                    continue
                rule_name = type(call).__name__
                if isinstance(call, (CommandRule, ShellCommandRule)):
                    kind = "shell_command" if isinstance(call, ShellCommandRule) else "command"
                    names = sorted(
                        (separator.join(command) for command in getattr(call, "cmds", ()) if command),
                    )
                    if names:
                        for name in names[:64]:
                            commands.append(_item(kind, name, f"matcher[{priority}][{index}].{rule_name}", owner, event_type))
                        specific = True
                        continue
                kind = _rule_kind(rule_name)
                if kind is None:
                    # Alconna is optional and its rule class is owned by the plugin.
                    # Inspect only stable public-ish literal fields; never call the rule.
                    if "alconna" in (type(call).__module__ + "." + rule_name).lower():
                        name = _alconna_name(call)
                        commands.append(_item("alconna_command", name, f"matcher[{priority}][{index}].{rule_name}", owner, event_type))
                        specific = True
                    continue
                trigger = _trigger_value(call)
                handlers.append(_item(kind, trigger, f"matcher[{priority}][{index}].{rule_name}", owner, event_type))
                specific = True
            if not specific:
                kind = "notice" if event_type == "notice" else "message" if event_type in {"message", "request", "meta_event"} else "event"
                handlers.append(_item(kind, f"{event_type}:{priority}:{index}", f"matcher[{priority}][{index}]", owner, event_type))
    plugin_name = os.environ.get("LIGAMINA_INVENTORY_PACKAGE") or ",".join(
        item.strip() for item in os.environ.get("LIGAMINA_NONEBOT_PLUGINS", "").split(",") if item.strip()
    ) or "loaded-plugins"
    plugin: dict[str, str] = {"package": plugin_name[:256]}
    _copy_identity(plugin, "registry_entry_id", "LIGAMINA_INVENTORY_ENTRY_ID", 128)
    _copy_identity(plugin, "version", "LIGAMINA_INVENTORY_VERSION", 128)
    _copy_sha(plugin)
    records = {
        "commands": _observed(commands),
        "handlers": _observed(handlers),
        "services": _unavailable("NoneBot has no shared service registry for loaded plugins."),
        "schedulers": _unavailable("Optional scheduler plugins own their job registries; no common registry is exposed."),
        "configuration": _unavailable("NoneBot plugin configuration schemas are not exposed by the matcher registry."),
    }
    return {
        "schema_version": 1,
        "ecosystem": "nonebot",
        "plugin": plugin,
        "source": {
            "kind": "runtime",
            "collector": "nonebot-runtime/1",
            "confidence": "observed",
            "evidence": [{"reference": value} for value in evidence],
        },
        "capabilities": records,
        "limitations": ["Runtime inventory describes framework-visible registrations; optional plugin-owned registries may not be visible."],
    }


def _rule_kind(rule_name: str) -> str | None:
    return {
        "KeywordsRule": "keyword",
        "RegexRule": "regex",
        "StartswithRule": "startswith",
        "EndswithRule": "endswith",
        "FullmatchRule": "fullmatch",
    }.get(rule_name)


def _trigger_value(rule: Any) -> str:
    for key in ("keywords", "regex", "msg"):
        value = getattr(rule, key, None)
        if isinstance(value, str) and value:
            return value[:256]
        if isinstance(value, (tuple, list, set, frozenset)):
            safe = sorted(item for item in value if isinstance(item, str))
            if safe:
                return ",".join(safe[:8])[:256]
    return type(rule).__name__


def _alconna_name(rule: Any) -> str:
    for key in ("command", "command_name", "name"):
        value = getattr(rule, key, None)
        if isinstance(value, str) and value:
            return value[:256]
        name = getattr(value, "name", None)
        if isinstance(name, str) and name:
            return name[:256]
    return "alconna-command"


def _item(kind: str, name: str, reference: str, owner: str | None, event_type: str) -> dict[str, Any]:
    attributes: dict[str, str] = {"event_type": event_type}
    if owner:
        attributes["plugin"] = str(owner)[:128]
    return {"kind": kind, "name": str(name)[:256] or kind, "attributes": attributes, "evidence": [{"reference": reference}]}


def _observed(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"status": "observed", "items": items[:MAX_ITEMS]}


def _unavailable(reason: str) -> dict[str, Any]:
    return {"status": "unavailable", "items": [], "reason": reason}


def _copy_identity(plugin: dict[str, str], key: str, env: str, limit: int) -> None:
    value = os.environ.get(env)
    if value:
        plugin[key] = value[:limit]


def _copy_sha(plugin: dict[str, str]) -> None:
    value = os.environ.get("LIGAMINA_INVENTORY_ARTIFACT_SHA256", "")
    if re.fullmatch(r"[a-f0-9]{64}", value):
        plugin["artifact_sha256"] = value
