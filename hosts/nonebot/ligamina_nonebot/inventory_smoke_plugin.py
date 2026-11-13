"""Self-authored matchers covering the runtime inventory categories."""

from nonebot import on_command, on_endswith, on_fullmatch, on_keyword, on_message, on_notice, on_regex, on_startswith

on_command("inventory.echo")
on_message()
on_keyword({"inventory-keyword"})
on_regex(r"inventory-[0-9]+")
on_startswith("inventory-start")
on_endswith("inventory-end")
on_fullmatch("inventory-full")
on_notice()
