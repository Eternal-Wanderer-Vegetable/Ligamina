"""P7-03 Worker side: exercise AstrBot plugins through stock AstrBot's event pipeline.

This side intentionally does not import Ligamina's AstrBot Host or bridge. The only
platform code is a disposable test adapter that constructs native AstrBot events and
captures outbound MessageChain values without an external account or network.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tarfile
import time
import uuid
from pathlib import Path
from typing import Any

PROTOCOL_OUT = sys.stdout
sys.stdout = sys.stderr
INPUT = Path("/input")
WORK = Path("/work")
HOST_ID = "astrbot-ab05"
ACCOUNT_ID = "astrbot-ab05-bot"
TOOL_PREFIX = "cmd.astrbot-ab05."


def emit(value: dict[str, Any]) -> None:
    PROTOCOL_OUT.write(json.dumps(value, ensure_ascii=False) + "\n")
    PROTOCOL_OUT.flush()


def worker_request() -> dict[str, Any]:
    request_path = INPUT / "request.json"
    if request_path.is_file():
        return json.loads(request_path.read_text(encoding="utf-8"))
    raw = sys.stdin.buffer.read()
    if not raw:
        raise ValueError("Worker request is missing")
    return json.loads(raw.decode("utf-8"))


def tool_name_part(value: str) -> str:
    import hashlib

    sanitized = "".join(char if char.isascii() and (char.isalnum() or char in "_-.") else "_" for char in value)
    return sanitized if sanitized == value else f"{sanitized}-{hashlib.sha256(value.encode('utf-8')).hexdigest()[:8]}"


def _stringify(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _command_map() -> dict[str, tuple[Any, Any]]:
    from astrbot.core.star.filter.command import CommandFilter
    from astrbot.core.star.star import star_map
    from astrbot.core.star.star_handler import star_handlers_registry

    commands: dict[str, tuple[Any, Any]] = {}
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
            command_id = ".".join(path)
            if command_id and command_id not in commands:
                commands[command_id] = (handler, command)
    return commands


def _arguments_text(command: Any, supplied: dict[str, Any]) -> str:
    # AB-05's `arguments` field is AstrBot's native greedy raw-text parameter.
    if set(supplied) == {"arguments"}:
        return _stringify(supplied["arguments"]) if supplied["arguments"] is not None else ""
    params = getattr(command, "handler_params", {}) or {}
    tokens = []
    last_supplied = max((index for index, name in enumerate(params) if name in supplied), default=-1)
    for index, (name, annotation_or_default) in enumerate(params.items()):
        if index > last_supplied:
            break
        if name in supplied:
            tokens.append(_stringify(supplied[name]))
        elif not isinstance(annotation_or_default, type):
            # Preserve positional arity where a later optional parameter is supplied.
            tokens.append(_stringify(annotation_or_default))
        else:
            tokens.append("")
    return " ".join(token for token in tokens if token != "")


def _message_text(message: Any) -> str:
    from astrbot.core.message.components import Plain

    chain = getattr(message, "chain", None)
    if chain is None:
        return str(message)
    return "".join(component.text if isinstance(component, Plain) else f"[{component.type}]" for component in chain)


def _runtime_tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    files = sorted((path for path in root.rglob("*") if path.is_file() and path.name != "runtime-manifest.json"),
                   key=lambda path: path.relative_to(root).as_posix())
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _extract_runtime(request: dict[str, Any]) -> Path:
    if sys.version.split()[0] != "3.12.14":
        raise RuntimeError(f"Worker Python differs from the locked runtime: {sys.version.split()[0]}")
    archive = INPUT / "runtime-site.tar.gz"
    if _sha256_file(archive) != request.get("runtime_archive_sha256"):
        raise RuntimeError("AstrBot runtime archive digest differs from campaign binding")
    destination = WORK / "runtime"
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, mode="r:gz") as tar:
        members = tar.getmembers()
        names = set()
        for member in members:
            parts = Path(member.name).parts
            if (not member.name.startswith("site-packages/") or member.name.startswith("/") or ".." in parts
                    or member.name in names or not (member.isdir() or member.isfile())):
                raise RuntimeError(f"unsafe AstrBot runtime archive entry: {member.name}")
            names.add(member.name)
        tar.extractall(path=destination, filter="data")
    runtime_site = destination / "site-packages"
    manifest_path = runtime_site / "runtime-manifest.json"
    if _sha256_file(manifest_path) != request.get("runtime_manifest_sha256"):
        raise RuntimeError("AstrBot runtime manifest digest differs from campaign binding")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (manifest.get("schema_version") != 1
            or manifest.get("kind") != "astrbot_native_test_runtime"
            or manifest.get("python_version") != "3.12.14"
            or manifest.get("astrbot_version") != "4.29.1"
            or manifest.get("requirements_lock_sha256") != request.get("requirements_lock_sha256")
            or manifest.get("site_packages_sha256") != request.get("runtime_site_packages_sha256")
            or manifest.get("site_packages_sha256") != _runtime_tree_sha256(runtime_site)
            or manifest.get("plugin_code_executed") is not False):
        raise RuntimeError("AstrBot runtime manifest or site-packages digest is invalid")
    return runtime_site


async def run(request: dict[str, Any], plugin_dir: Path) -> dict[str, Any]:
    started = time.monotonic()
    runtime_site = _extract_runtime(request)
    sys.path.insert(0, str(runtime_site))
    os.environ["ASTRBOT_ROOT"] = str(WORK / "astrbot-native")
    os.environ["ASTRBOT_DISABLE_METRICS"] = "1"
    root = Path(os.environ["ASTRBOT_ROOT"])
    plugins = root / "data" / "plugins"
    plugins.mkdir(parents=True, exist_ok=True)
    import shutil

    shutil.copytree(plugin_dir, plugins / plugin_dir.name, dirs_exist_ok=True)
    os.chdir(root)
    sys.path.insert(0, str(root))

    from astrbot.core import LogBroker, astrbot_config, db_helper
    from astrbot.core.core_lifecycle import AstrBotCoreLifecycle
    from astrbot.core.message.components import Plain
    from astrbot.core.message.message_event_result import MessageChain
    from astrbot.core.platform import AstrBotMessage, MessageMember, MessageType, Platform, PlatformMetadata
    from astrbot.core.platform.astr_message_event import AstrMessageEvent

    class NativeTestEvent(AstrMessageEvent):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.captured_messages: list[str] = []

        async def send(self, message: MessageChain | None) -> None:
            if message is not None:
                self.captured_messages.append(_message_text(message))
            self._has_send_oper = True

    class NativeTestPlatform(Platform):
        """A stock AstrBot Platform that emits private messages and captures replies."""

        def __init__(self, event_queue):
            super().__init__({"id": "p7-native-test"}, event_queue)
            self._stop = asyncio.Event()
            self.metadata = PlatformMetadata(
                name="p7_native_test",
                description="isolated P7 native baseline test adapter",
                id="p7-native-test",
                support_proactive_message=False,
            )

        async def run(self):
            await self._stop.wait()

        def meta(self):
            return self.metadata

        def get_client(self):
            return None

        def create_event(self, text: str, actor_id: str, message_id: str):
            message = AstrBotMessage()
            message.type = MessageType.FRIEND_MESSAGE
            message.self_id = ACCOUNT_ID
            message.session_id = actor_id
            message.message_id = message_id
            message.group = None
            message.sender = MessageMember(user_id=actor_id, nickname="AB-05 native actor")
            message.message = [Plain(text)]
            message.message_str = text
            message.raw_message = {"source": "p7_native_test"}
            message.timestamp = 1794268800
            return NativeTestEvent(text, message, self.meta(), actor_id)

        async def dispatch(self, core, text: str, message_index: int):
            actor_id = f"tool-actor-{uuid.uuid4().hex}"
            event = self.create_event(text, actor_id, f"p7-native-{message_index}")
            scheduler = core.pipeline_scheduler_mapping.get("default")
            if scheduler is None:
                raise RuntimeError("stock AstrBot default pipeline scheduler is missing")
            await asyncio.wait_for(scheduler.execute(event), timeout=45)
            return event.captured_messages

    config = astrbot_config
    config["admins_id"] = []
    config["wake_prefix"] = ["/"]
    config["provider_settings"] = {**(config.get("provider_settings") or {}), "enable": False}
    config["t2i"] = False
    config["disable_metrics"] = True
    config["disable_builtin_commands"] = True
    config["platform"] = []
    config.save_config()
    core = AstrBotCoreLifecycle(LogBroker(), db_helper)
    await core.initialize()
    adapter = NativeTestPlatform(core.event_queue)
    commands = _command_map()
    listed = sorted(
        TOOL_PREFIX + tool_name_part(command_id)
        for command_id in commands
    )
    steps = []
    index = 0
    for step in request["tool_calls"]:
        records = []
        for spec in step:
            command_id = str(spec["command_id"])
            record: dict[str, Any] = {
                "command_id": command_id,
                "tool": TOOL_PREFIX + tool_name_part(command_id),
                "arguments": spec.get("arguments", {}),
                "is_error": False,
                "refused_operations": [],
            }
            try:
                pair = commands.get(command_id)
                if pair is None:
                    raise KeyError(f"native command not discovered: {command_id}")
                _, command = pair
                wake_prefixes = config.get("wake_prefix") or ["/"]
                prefix = str(wake_prefixes[0]) if wake_prefixes else ""
                raw = _arguments_text(command, spec.get("arguments", {}))
                message = f"{prefix}{command_id}" + (f" {raw}" if raw else "")
                record["replies"] = await adapter.dispatch(core, message, index)
            except Exception as error:
                record.update({"is_error": True, "protocol_error": f"{type(error).__name__}: {str(error)[:400]}", "replies": []})
            index += 1
            records.append(record)
        steps.append(records)
    return {
        "status": "completed",
        "native_adapter": "p7_native_test",
        "bridge_imported": False,
        "tool_calls": {"listed_command_tools": listed, "steps": steps},
        "gateway_operation_count": 0,
        "gateway_errors": [],
        "core_exit_code": 0,
        "host_alive_after_shutdown": False,
        "astrbot_version": "4.29.1",
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def main() -> None:
    request = worker_request()
    plugin_dirs = [path for path in (INPUT / "plugin").iterdir() if path.is_dir()]
    if len(plugin_dirs) != 1:
        emit({"status": "unknown", "diagnostic": "expected exactly one frozen plugin directory"})
        return
    requirements = INPUT / "requirements.txt"
    lines = [line for line in requirements.read_text(encoding="utf-8").splitlines() if line.strip()] if requirements.is_file() else []
    installed: dict[str, Any] = {"status": "not_needed"}
    if lines:
        site = WORK / "site"
        site.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        install = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--no-index", "--no-cache-dir", "--disable-pip-version-check",
             "--find-links", str(INPUT / "wheelhouse"), "--target", str(site), "-r", str(requirements),
             "-c", str(INPUT / "constraints.txt")],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300, check=False,
        )
        if install.returncode:
            emit({"status": "failed", "diagnostic": "dependency_install_failed", "install": {
                "status": "failed", "seconds": round(time.monotonic() - started, 1),
                "error_tail": (install.stderr or install.stdout)[-1200:],
            }})
            return
        installed = {"status": "installed", "seconds": round(time.monotonic() - started, 1)}
    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(run(request, plugin_dirs[0]))
        result["install"] = installed
        emit(result)
    except BaseException as error:  # Worker owns cleanup; report only bounded diagnostics.
        emit({"status": "failed", "diagnostic": f"{type(error).__name__}: {str(error)[:500]}", "install": installed})
    finally:
        sys.stderr.flush()
        os._exit(0)


if __name__ == "__main__":
    main()
