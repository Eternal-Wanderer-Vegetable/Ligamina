"""Discover native NoneBot command matchers as v0.2 command descriptors."""

from __future__ import annotations

from typing import Any

from nonebot.matcher import matchers
from nonebot.rule import CommandRule, ShellCommandRule

MAX_COMMANDS = 512


def describe_commands(driver: Any) -> list[dict[str, Any]]:
    """Describe matchers created by ``on_command`` / ``on_shell_command``.

    The invocation uses the driver's configured command start and separator, so the
    text Core sends goes through NoneBot's own command parsing. Matchers without a
    command rule (keywords, regex, message hooks) are not commands and are skipped.
    """
    config = getattr(driver, "config", None)
    starts = sorted(getattr(config, "command_start", None) or {"/"})
    prefix = "" if "" in starts else starts[0]
    separators = sorted(getattr(config, "command_sep", None) or {"."})
    separator = separators[0]

    descriptors: list[dict[str, Any]] = []
    seen: set[str] = set()
    for priority in sorted(matchers.keys()):
        for matcher in matchers[priority]:
            rule = _command_rule(matcher)
            if rule is None:
                continue
            # on_command merges the command and its aliases into an unordered set, so
            # pick the primary name deterministically: most segments first, then by text.
            ordered = sorted((command for command in rule.cmds if command), key=lambda c: (-len(c), separator.join(c)))
            names = [separator.join(command) for command in ordered]
            names = [name for name in names if name and not any(char.isspace() for char in name)]
            if not names:
                continue
            command_id = names[0]
            if command_id in seen or len(descriptors) >= MAX_COMMANDS:
                continue
            seen.add(command_id)
            descriptor: dict[str, Any] = {
                "command_id": _clip(command_id, 96),
                "invocation": _clip(prefix + command_id, 256),
                "framework": "nonebot",
                "usage": _clip(f"{prefix}{command_id} <text>", 1024),
            }
            aliases = [prefix + name for name in names[1:]]
            if aliases:
                descriptor["aliases"] = [_clip(alias, 256) for alias in aliases[:64]]
            description = _plugin_description(matcher)
            if description:
                descriptor["description"] = _clip(description, 2048)
            if isinstance(rule, ShellCommandRule):
                descriptor["usage"] = _clip(f"{prefix}{command_id} [shell-style arguments]", 1024)
            descriptors.append(descriptor)
    return descriptors


def _command_rule(matcher: Any) -> CommandRule | ShellCommandRule | None:
    for dependent in getattr(getattr(matcher, "rule", None), "checkers", ()):
        call = getattr(dependent, "call", None)
        if isinstance(call, (CommandRule, ShellCommandRule)):
            return call
    return None


def _plugin_description(matcher: Any) -> str:
    plugin = getattr(matcher, "plugin", None)
    metadata = getattr(plugin, "metadata", None)
    parts = []
    for field in ("description", "usage"):
        value = getattr(metadata, field, None)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return " Usage: ".join(parts)


def _clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit]
