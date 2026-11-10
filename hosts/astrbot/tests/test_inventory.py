"""Unit checks for AstrBot runtime inventory without starting plugins or the core."""

import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from jsonschema import Draft202012Validator
from ligamina_astrbot.inventory import describe_runtime_inventory


def test_runtime_inventory_includes_plugin_llm_tools_but_skips_builtin_tools():
    astrbot = ModuleType("astrbot")
    astrbot.__path__ = []
    core = ModuleType("astrbot.core")
    core.__path__ = []
    star_package = ModuleType("astrbot.core.star")
    star_package.__path__ = []
    star_module = ModuleType("astrbot.core.star.star")
    star_module.star_map = {}
    handler_module = ModuleType("astrbot.core.star.star_handler")
    handler_module.star_handlers_registry = []
    modules = {
        "astrbot": astrbot,
        "astrbot.core": core,
        "astrbot.core.star": star_package,
        "astrbot.core.star.star": star_module,
        "astrbot.core.star.star_handler": handler_module,
    }
    plugin_tool = SimpleNamespace(
        name="fixture_lookup",
        description="Look up a fixture value",
        handler=SimpleNamespace(__module__="data.plugins.fixture.main"),
        parameters={"properties": {"query": {"type": "string"}}},
    )
    builtin_tool = SimpleNamespace(
        name="builtin_help",
        description="Built in",
        handler=SimpleNamespace(__module__="astrbot.core.builtin_stars.help"),
        parameters={"properties": {}},
    )
    lifecycle = SimpleNamespace(core=SimpleNamespace(provider_manager=SimpleNamespace(llm_tools=SimpleNamespace(func_list=[plugin_tool, builtin_tool]))))

    with patch.dict("sys.modules", modules):
        inventory = describe_runtime_inventory(lifecycle)

    llm_tools = [item for item in inventory["capabilities"]["handlers"]["items"] if item["kind"] == "llm_tool"]
    assert [(item["name"], item["attributes"]["plugin"]) for item in llm_tools] == [("fixture_lookup", "fixture")]
    assert inventory["capabilities"]["commands"]["status"] == "observed"


def test_worker_runtime_capability_response_includes_inventory_without_plugin_startup():
    import asyncio
    import sys

    previous_stdout = sys.stdout
    try:
        from ligamina_astrbot.worker import Worker
    finally:
        sys.stdout = previous_stdout

    worker = Worker()
    worker._ready = True
    worker._lifecycle = SimpleNamespace(core=None)

    response = asyncio.run(worker._dispatch("runtime.capabilities", {}))

    assert response["capabilities"] == []
    assert response["commands"] == []
    assert response["inventory"]["ecosystem"] == "astrbot"
    assert response["inventory"]["source"]["kind"] == "runtime"
    schema_path = Path(__file__).resolve().parents[3] / "contracts/lab/inventory/v1/capability-inventory.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert list(Draft202012Validator(schema).iter_errors(response["inventory"])) == []
