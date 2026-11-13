from types import SimpleNamespace
from unittest.mock import patch

from ligamina_nonebot.inventory import describe_runtime_inventory


def test_runtime_inventory_keeps_alconna_commands_outside_the_tool_descriptor_list():
    alconna_rule = type("AlconnaRule", (), {"__module__": "nonebot_plugin_alconna.rule"})()
    alconna_rule.command = SimpleNamespace(name="inventory.alconna")
    matcher = SimpleNamespace(
        type="message",
        rule=SimpleNamespace(checkers=[SimpleNamespace(call=alconna_rule)]),
        plugin=None,
    )
    driver = SimpleNamespace(config=SimpleNamespace(command_sep={"."}))

    with patch("ligamina_nonebot.inventory.matchers", {5: [matcher]}):
        inventory = describe_runtime_inventory(driver)

    commands = inventory["capabilities"]["commands"]["items"]
    assert [(item["kind"], item["name"]) for item in commands] == [("alconna_command", "inventory.alconna")]
    assert inventory["source"]["kind"] == "runtime"
