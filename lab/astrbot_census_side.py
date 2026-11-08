"""AB-04 Worker side: install one frozen AstrBot plugin offline and load it in real AstrBot.

Runs inside a network-disabled Docker Worker with the AstrBot runtime image:
  /input/plugin/<dir>          the plugin tree extracted from the frozen archive
  /input/wheelhouse/           pre-downloaded wheels (may be empty)
  /input/requirements.txt      the plugin's requirements (may be empty)
  /input/constraints.txt       the AstrBot lock as constraints (so AstrBot's own pins never change)
  /input/project/hosts/astrbot the Ligamina AstrBot Host code (+ /input/project/contracts)
Prints exactly one JSON object on stdout. Plugin code runs only here.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

PROTOCOL_OUT = sys.stdout
sys.stdout = sys.stderr  # AstrBot and plugins log to stdout; keep it for the single result line

INPUT = Path("/input")
WORK = Path("/work")


def emit(result: dict) -> None:
    PROTOCOL_OUT.write(json.dumps(result, ensure_ascii=False) + "\n")
    PROTOCOL_OUT.flush()


def install() -> dict:
    requirements = INPUT / "requirements.txt"
    lines = [line for line in requirements.read_text(encoding="utf-8").splitlines() if line.strip()] if requirements.is_file() else []
    if not lines:
        return {"status": "not_needed"}
    site = WORK / "site"
    site.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    completed = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-index", "--no-cache-dir", "--disable-pip-version-check",
         "--find-links", str(INPUT / "wheelhouse"), "--target", str(site),
         "-r", str(requirements), "-c", str(INPUT / "constraints.txt")],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300, check=False,
    )
    tail = (completed.stderr or completed.stdout)[-1500:]
    if completed.returncode:
        return {"status": "failed", "seconds": round(time.monotonic() - started, 1), "error_tail": tail}
    sys.path.insert(0, str(site))
    return {"status": "installed", "seconds": round(time.monotonic() - started, 1)}


async def load(plugin_dir: Path, settle: list) -> dict:
    sys.path.insert(0, str(INPUT / "project" / "hosts" / "astrbot"))
    from ligamina_astrbot.lifecycle import HostLifecycle

    sends: list[dict] = []

    async def platform_call(operation: dict) -> dict:
        sends.append(operation)
        return {"status": "failed", "error": {"code": "census_no_platform"}}

    lifecycle = HostLifecycle(host_id="census", platform_call=platform_call, data_dir=WORK / "astrbot")
    started = time.monotonic()
    try:
        await asyncio.wait_for(lifecycle.start([str(plugin_dir)]), timeout=240)
        from astrbot.core.star.star import star_map, star_registry
        from ligamina_astrbot.commands import describe_commands

        manager = lifecycle.core.plugin_manager
        failed = {name: {"error": str(info.get("error", ""))[:600], "traceback_tail": str(info.get("traceback", ""))[-1200:]}
                  for name, info in (manager.failed_plugin_dict or {}).items()}
        # star_map (module path -> metadata) is what AstrBot dispatches handlers through and what
        # commands.py reads; star_registry can miss plugins that are loaded and dispatchable.
        loaded = [
            {"name": star.name, "version": star.version, "activated": bool(star.activated), "root_dir": star.root_dir_name,
             "in_star_registry": any(item is star for item in star_registry)}
            for star in {id(meta): meta for meta in star_map.values()}.values() if not star.reserved
        ]
        commands = describe_commands(lifecycle.wake_prefix())
        return {
            "seconds": round(time.monotonic() - started, 1),
            "loaded_plugins": loaded,
            "failed_plugins": failed,
            "commands": [{"command_id": c["command_id"], "arguments": c.get("arguments", [])} for c in commands][:100],
            "startup_sends": len(sends),
        }
    finally:
        # The verdict is emitted by the caller; teardown is skipped (see census()).
        if lifecycle.core is not None:
            settle.append(lifecycle)


def verdict(plugin_dir: Path, install_result: dict, load_result: dict) -> dict:
    ours = [star for star in load_result["loaded_plugins"] if star["root_dir"] == plugin_dir.name]
    if load_result["failed_plugins"]:
        status, reason = "failed", "plugin_load_failed"
    elif ours and all(star["activated"] for star in ours):
        status, reason = "passed", None
    elif ours:
        status, reason = "failed", "plugin_not_activated"
    else:
        status, reason = "failed", "plugin_not_registered"
    return {"status": status, "reason_code": reason, "install": install_result, "load": load_result}


async def census(plugin_dir: Path, install_result: dict) -> None:
    settle: list = []
    try:
        result = verdict(plugin_dir, install_result, await load(plugin_dir, settle))
    except asyncio.TimeoutError:
        result = {"status": "unknown", "reason_code": "astrbot_startup_timeout", "install": install_result}
    except Exception as error:  # the Host itself failed, not necessarily the plugin
        result = {"status": "unknown", "reason_code": "host_startup_failed", "install": install_result,
                  "diagnostic": f"{type(error).__name__}: {str(error)[:500]}"}
    emit(result)
    # No AstrBot teardown: core.stop() was observed to block the loop indefinitely (and to ignore
    # cancellation) in network-disabled Workers. The Worker is discarded, so main() hard-exits.
    del settle


def main() -> None:
    plugin_dirs = [path for path in (INPUT / "plugin").iterdir() if path.is_dir()]
    if len(plugin_dirs) != 1:
        emit({"status": "unknown", "reason_code": "bundle_invalid", "diagnostic": "expected exactly one plugin directory"})
        return
    plugin_dir = plugin_dirs[0]
    os.environ["ASTRBOT_DISABLE_METRICS"] = "1"
    install_result = install()
    if install_result["status"] == "failed":
        emit({"status": "failed", "reason_code": "dependency_install_failed", "install": install_result})
        return
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(census(plugin_dir, install_result))
    finally:
        # Skip asyncio.run's default-executor join (up to 300 s on 3.12) and non-daemon thread joins.
        sys.stderr.flush()
        os._exit(0)


if __name__ == "__main__":
    main()
