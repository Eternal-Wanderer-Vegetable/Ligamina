"""Exercise an installed plugin through stock NoneBot and OneBot v11 APIs."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib.metadata
import json
import os
import site
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _test_context_profile_sha256(context: dict[str, Any]) -> str | None:
    profile = context.get("test_context_profile")
    if profile is None:
        return None
    if not isinstance(profile, dict):
        raise ValueError("P7 test context profile must be an object")
    if (
        profile.get("execution_scope") != "linux_worker_only_network_disabled"
        or profile.get("authority_scope") != "synthetic_test_actor_only"
        or profile.get("event_envelope_role_claims") is not False
    ):
        raise ValueError("P7 test context profile exceeds its synthetic offline Worker scope")
    payload = {key: value for key, value in profile.items() if key != "profile_sha256"}
    digest = profile.get("profile_sha256")
    if not isinstance(digest, str) or _canonical_hash(payload) != digest:
        raise ValueError("P7 test context profile hash mismatch")
    return digest


def _message_segments(message: Any) -> list[dict[str, Any]]:
    if isinstance(message, str):
        return [{"type": "text", "text": message}]
    result = []
    for segment in message:
        data = segment.data
        if segment.type == "text":
            result.append({"type": "text", "text": data.get("text", "")})
        elif segment.type == "reply":
            result.append({"type": "reply", "message_id": str(data.get("id", ""))})
        elif segment.type == "at":
            result.append({"type": "mention", "user_id": str(data.get("qq", ""))})
        else:
            result.append({"type": segment.type, "data": data})
    return result


def _onebot_message(event: dict[str, Any]) -> list[dict[str, Any]]:
    segments = []
    for segment in event.get("message", []):
        kind = segment.get("type")
        if kind == "text":
            segments.append({"type": "text", "data": {"text": segment.get("text", "")}})
        elif kind == "mention":
            segments.append({"type": "at", "data": {"qq": segment.get("user_id", "")}})
        elif kind == "reply":
            segments.append({"type": "reply", "data": {"id": segment.get("message_id", "")}})
        elif kind == "image_ref":
            segments.append({"type": "image", "data": {"file": segment.get("resource_ref", "")}})
        else:
            raise ValueError(f"unsupported stock OneBot stimulus segment: {kind!r}")
    return segments or [{"type": "text", "data": {"text": ""}}]


def _fingerprints(context: dict[str, Any], stimulus: dict[str, Any]) -> dict[str, Any]:
    paths = context["fingerprint_paths"]
    sample = context["sample"]
    lockfile = Path(paths["lockfile"])
    profile = Path(paths["profile"])
    artifact = Path(paths["artifact"])
    algorithm = sample["_artifact_hash_algorithm"]
    if algorithm != "sha256":
        raise ValueError(f"unsupported NoneBot artifact digest algorithm: {algorithm}")
    artifact_hash = _sha256(artifact)
    if artifact_hash != sample["_artifact_hash"]:
        raise ValueError("installed artifact bytes no longer match the frozen SHA-256")
    return {
        "lockfile_sha256": _sha256(lockfile),
        "profile_sha256": _sha256(profile),
        "artifact_hash": artifact_hash,
        "config_sha256": _canonical_hash(context["config"]),
        "stimulus_sha256": _canonical_hash(stimulus),
        "test_context_profile_sha256": _test_context_profile_sha256(context),
    }


def _native_command_descriptors(driver: Any) -> list[dict[str, Any]]:
    """Describe stock NoneBot command matchers without importing the Ligamina Host."""
    from nonebot.matcher import matchers
    from nonebot.rule import CommandRule, ShellCommandRule

    config = getattr(driver, "config", None)
    starts = sorted(getattr(config, "command_start", None) or {"/"})
    prefix = "" if "" in starts else starts[0]
    separators = sorted(getattr(config, "command_sep", None) or {"."})
    separator = separators[0]
    descriptors: list[dict[str, Any]] = []
    seen: set[str] = set()
    for priority in sorted(matchers.keys()):
        for matcher in matchers[priority]:
            rule = None
            for dependent in getattr(getattr(matcher, "rule", None), "checkers", ()):
                call = getattr(dependent, "call", None)
                if isinstance(call, (CommandRule, ShellCommandRule)):
                    rule = call
                    break
            if rule is None:
                continue
            ordered = sorted((command for command in rule.cmds if command), key=lambda item: (-len(item), separator.join(item)))
            names = [separator.join(command) for command in ordered]
            names = [name for name in names if name and not any(char.isspace() for char in name)]
            if not names:
                continue
            command_id = names[0]
            if command_id in seen or len(descriptors) >= 512:
                continue
            seen.add(command_id)
            descriptor: dict[str, Any] = {
                "command_id": command_id[:96],
                "invocation": (prefix + command_id)[:256],
                "framework": "nonebot",
                "usage": (f"{prefix}{command_id} [shell-style arguments]" if isinstance(rule, ShellCommandRule)
                          else f"{prefix}{command_id} <text>")[:1024],
            }
            aliases = [prefix + name for name in names[1:]]
            if aliases:
                descriptor["aliases"] = [alias[:256] for alias in aliases[:64]]
            descriptors.append(descriptor)
    return sorted(descriptors, key=lambda item: item["command_id"])


def _probe_observation(work: Path, events: list[dict[str, Any]]) -> dict[str, Any]:
    data_root = (work / "nonebot-data").resolve()
    path = (data_root / "phase4-probes" / "observations.ndjson").resolve()
    path.relative_to(data_root)
    raw = path.read_bytes()
    rows = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line]
    event_by_message_id = {
        str(event.get("extensions", {}).get("onebot.v11", {}).get("message_id", "")): event
        for event in events
    }
    observations = []
    for row in rows:
        event = event_by_message_id.get(str(row.get("message_id", "")))
        if event is None:
            raise RuntimeError("phase4 observer wrote an unbound OneBot message id")
        observations.append({
            "event_id": str(event["event_id"]),
            "account_id": row["account_id"],
            "actor_id": row["actor_id"],
            "conversation": row["conversation"],
            "timestamp": row["timestamp"],
        })
    if len(observations) != len(events):
        raise RuntimeError("phase4 observer did not persist exactly one record per dispatched OneBot event")
    source_path = Path(__file__).resolve().parent / "fixtures" / "phase4_probe_observer.py"
    return {
        "status": "recorded",
        "storage": {
            "relative_path": str(path.relative_to(work.resolve())).replace("\\", "/"),
            "file_sha256": hashlib.sha256(raw).hexdigest(),
            "record_count": len(rows),
            "probe_source_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
        },
        "events": observations,
    }


class NativeOneBotApiRecorder:
    """Record the stock OneBot API calls supported by the isolated Native runner."""

    def __init__(
        self,
        *,
        operations: list[dict[str, Any]],
        receipts: list[dict[str, Any]],
        api_inputs: list[dict[str, Any]],
        source_event_id,
        capture_source_event_id: bool,
    ) -> None:
        self.operations = operations
        self.receipts = receipts
        self.api_inputs = api_inputs
        self.source_event_id = source_event_id
        self.capture_source_event_id = capture_source_event_id

    async def __call__(self, bot: Any, api: str, **params: Any) -> Any:
        if api == "get_msg":
            message_id = str(params.get("message_id", ""))
            return {
                "message_id": message_id,
                "real_id": message_id,
                "sender": {"user_id": int(bot.self_id)},
                "time": 0,
                "message": [],
            }
        is_forward = api == "send_group_forward_msg"
        allowed = {"group_id", "messages", "_timeout"} if is_forward else {"group_id", "user_id", "message", "message_type", "auto_escape", "_timeout"}
        if api not in {"send_msg", "send_group_msg", "send_private_msg", "send_group_forward_msg"}:
            raise RuntimeError(f"stock native baseline has no virtual API fixture for {api!r}")
        unexpected = set(params) - allowed
        if unexpected:
            raise RuntimeError(f"stock native API {api!r} has unsupported parameters: {sorted(unexpected)}")
        raw_message = params.get("messages", []) if is_forward else params.get("message", [])
        self.api_inputs.append({
            "api": api,
            "message_type": type(raw_message).__name__,
            "message": _native_api_input_value(raw_message),
        })
        if is_forward:
            group_id = params.get("group_id")
            if group_id is None:
                raise RuntimeError("send_group_forward_msg requires group_id")
            target_id = _native_onebot_id(group_id, "group_id")
            user_id = None
            message = _native_forward_nodes(raw_message)
        else:
            group_id = params.get("group_id")
            user_id = params.get("user_id")
            target_id = group_id if group_id is not None else user_id
            message = _message_segments(params.get("message", []))
        kind = "group" if group_id is not None else "private" if user_id is not None else None
        if kind is None or target_id is None:
            raise RuntimeError(f"native OneBot API {api!r} omitted a target")
        operation_id = f"operation-{len(self.operations) + 1}"
        target = {"kind": kind, "id": str(target_id)}
        operation = {
            "operation_id": operation_id,
            "account_id": str(bot.self_id),
            "operation": "send_message",
            "target": target,
            "params": {"message": message},
        }
        source_event_id = self.source_event_id()
        if self.capture_source_event_id:
            operation["source_event_id"] = source_event_id
        self.operations.append(operation)
        receipt = {"operation_id": operation_id, "status": "succeeded", "target": target}
        if self.capture_source_event_id:
            receipt["account_id"] = str(bot.self_id)
            receipt["source_event_id"] = source_event_id
        self.receipts.append(receipt)
        return {"message_id": f"virtual-message-{len(self.receipts)}"}


def _native_forward_nodes(messages: Any) -> list[dict[str, Any]]:
    if not isinstance(messages, list) or not messages:
        raise RuntimeError("send_group_forward_msg requires a non-empty messages list")
    nodes = []
    for node in messages:
        if not isinstance(node, dict) or set(node) != {"type", "data"} or node.get("type") != "node":
            raise RuntimeError("send_group_forward_msg messages must contain OneBot node objects")
        data = node["data"]
        if not isinstance(data, dict):
            raise RuntimeError("send_group_forward_msg node data must be an object")
        if set(data) == {"name", "uin", "content"}:
            nickname, user_id, content = data["name"], data["uin"], data["content"]
        elif set(data) == {"nickname", "user_id", "content"}:
            nickname, user_id, content = data["nickname"], data["user_id"], data["content"]
        else:
            raise RuntimeError("forward node requires name/uin/content or nickname/user_id/content")
        if not isinstance(nickname, str):
            raise RuntimeError("forward node nickname must be a string")
        nodes.append({
            "type": "forward_node",
            "user_id": _native_onebot_id(user_id, "forward node user_id"),
            "nickname": nickname,
            "content": _native_forward_content(content),
        })
    return nodes


def _native_onebot_id(value: Any, label: str) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        numeric = value
    elif isinstance(value, str) and value.isascii() and value.isdigit():
        numeric = int(value)
    else:
        raise RuntimeError(f"{label} must be a numeric OneBot ID")
    if numeric < 0 or numeric > (1 << 63) - 1:
        raise RuntimeError(f"{label} is outside the numeric OneBot ID profile")
    return str(numeric)


def _native_forward_content(value: Any) -> str | list[dict[str, Any]]:
    if isinstance(value, str):
        return value
    if not isinstance(value, (list, tuple)) or not value:
        raise RuntimeError("forward node content must be a string or non-empty message list")
    segments = []
    for item in value:
        if isinstance(item, dict):
            if set(item) != {"type", "data"}:
                raise RuntimeError("forward node content segments must have type and data")
            segment_type, data = item.get("type"), item.get("data")
        else:
            segment_type = getattr(item, "type", None)
            data = getattr(item, "data", None)
        if not isinstance(segment_type, str) or not isinstance(data, dict):
            raise RuntimeError("forward node content segments must have type and data")
        if segment_type == "text" and isinstance(data.get("text"), str):
            segments.append({"type": "text", "text": data["text"]})
        elif segment_type == "at":
            segments.append({"type": "mention", "user_id": _native_onebot_id(data.get("qq"), "forward node mention ID")})
        elif segment_type == "reply":
            segments.append({"type": "reply", "message_id": _native_onebot_id(data.get("id"), "forward node reply ID")})
        elif segment_type == "image" and isinstance(data.get("file"), str) and data["file"]:
            segments.append({"type": "image_ref", "resource_ref": data["file"]})
        elif segment_type == "image":
            raise RuntimeError("forward node image segment has no stable resource reference")
        elif segment_type == "text":
            raise RuntimeError("forward node text segment has invalid data")
        else:
            raise RuntimeError(f"unsupported forward node content segment {segment_type!r}")
    return segments


def _native_api_input_value(value: Any) -> Any:
    """Keep API input evidence JSON-safe without copying image payload bytes."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _native_api_input_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_native_api_input_value(item) for item in value]
    if hasattr(value, "type") and hasattr(value, "data"):
        return {
            "type": str(getattr(value, "type")),
            "data": _native_api_input_value(getattr(value, "data")),
        }
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        return {"byte_length": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    getvalue = getattr(value, "getvalue", None)
    if callable(getvalue):
        try:
            raw = getvalue()
        except Exception:
            raw = None
        if isinstance(raw, (bytes, bytearray, memoryview)):
            raw = bytes(raw)
            return {"byte_length": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    return {"unsupported_type": type(value).__name__}


async def _run(request: dict[str, Any]) -> dict[str, Any]:
    case = request
    context = case["context"]
    sample = context["sample"]
    behavior_fixture = context.get("behavior_fixture")
    work = Path(context.get("execution", {}).get("workspace", context["work_dir"])).resolve()
    seed = context.get("execution", {}).get("random_seed")
    if isinstance(seed, int) and not isinstance(seed, bool):
        import random

        random.seed(seed)
        os.environ["LIGAMINA_BEHAVIOR_RANDOM_SEED"] = str(seed)
    site.addsitedir(str(Path(context["site_packages"]).resolve()))
    os.environ["HOME"] = str(work / "native-home")
    os.environ["TEMP"] = str(work / "native-temp")
    os.environ["TMP"] = os.environ["TEMP"]
    Path(os.environ["HOME"]).mkdir(parents=True, exist_ok=True)
    Path(os.environ["TEMP"]).mkdir(parents=True, exist_ok=True)
    for key, value in context.get("sample_environment", {}).items():
        if not key.startswith("LIGAMINA_SAMPLE_") or not isinstance(value, str):
            raise ValueError(f"invalid sample environment key: {key!r}")
        os.environ[key] = value
    nonebot_config = context.get("config", {}).get("nonebot_config", {})
    if not isinstance(nonebot_config, dict):
        raise ValueError("P7 NoneBot config must be an object")
    context_profile = context.get("test_context_profile")
    if context_profile is not None:
        _test_context_profile_sha256(context)
        expected_superusers = [context_profile["synthetic_superuser_id"]]
        if nonebot_config != {"superusers": expected_superusers}:
            raise ValueError("P7 Native NoneBot config differs from the bound synthetic superuser profile")
        expected_host_value = json.dumps(expected_superusers, separators=(",", ":"))
        if context.get("host_environment", {}).get("SUPERUSERS") != expected_host_value:
            raise ValueError("P7 Native and Federated superuser settings do not match")
    if behavior_fixture:
        if behavior_fixture.get("adapter", {}).get("id") != "nonebot.onebot.v11":
            raise ValueError("v2 fixture adapter does not match the stock NoneBot runner")
        config = behavior_fixture["setup"].get("plugin_config", {})
        package = sample.get("package")
        if package == "nonebot_plugin_help":
            if (
                set(config) != {"help_at_sender", "help_ignore_plugins"}
                or not isinstance(config["help_at_sender"], bool)
                or config["help_ignore_plugins"] != ["phase4_random_seed", "phase4_probe_observer"]
            ):
                raise ValueError("v2 NoneBot Help config is outside the reviewed allowlist")
            nonebot_config.update(config)
            (work / ".env.prod").write_text(
                "\n".join(f"{key}={json.dumps(value, ensure_ascii=False, separators=(',', ':'))}" for key, value in sorted(config.items())) + "\n",
                encoding="utf-8",
            )
        elif config:
            if set(config) != {"name", "limit", "name_list"}:
                raise ValueError("v2 NoneBot plugin config contains keys outside the reviewed allowlist")
            os.environ["GUESS_NAME"] = str(config["name"])
            os.environ["GUESS_LIMIT"] = str(config["limit"])
            os.environ["GUESS_NAME_LIST"] = json.dumps(config["name_list"], ensure_ascii=False)
        os.environ["LIGAMINA_BEHAVIOR_RANDOM_SEED"] = str(behavior_fixture["determinism"]["random_seed"])
        data_directory = work / "nonebot-data"
        os.environ["LIGAMINA_NONEBOT_DATA_DIR"] = str(data_directory)
        os.environ["LIGAMINA_P4_OBSERVATION_FILE"] = str(data_directory / "phase4-probes" / "observations.ndjson")
        fixture_plugins = Path(__file__).resolve().parent / "fixtures"
        site.addsitedir(str(fixture_plugins))
        sample["preload_plugins"] = [*sample.get("preload_plugins", []), "phase4_random_seed", "phase4_probe_observer"]

    import nonebot
    from nonebot.adapters.onebot.v11 import Adapter, Bot

    if nonebot.__version__ != context["expected_environment"]["framework_version"]:
        raise RuntimeError(f"NoneBot version mismatch: {nonebot.__version__}")
    nonebot.init(driver="~fastapi", nickname={"Bot"}, **nonebot_config)
    driver = nonebot.get_driver()
    driver.register_adapter(Adapter)
    adapter = driver._adapters[Adapter.get_name()]

    plugin_names = [*sample.get("preload_plugins", []), sample["_entry"]]
    loaded_plugin_names = []
    for plugin_name in plugin_names:
        loaded_plugin = nonebot.load_plugin(plugin_name)
        if loaded_plugin is None:
            raise RuntimeError(f"NoneBot could not load plugin {plugin_name!r}")
        loaded_plugin_names.append(plugin_name)
    runtime_commands = _native_command_descriptors(driver)

    operations: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    api_inputs: list[dict[str, Any]] = []
    current_event_id: str | None = None
    capture_source_event_id = bool(behavior_fixture or context.get("execution", {}).get("capture_source_event_id"))

    adapter._call_api = NativeOneBotApiRecorder(
        operations=operations,
        receipts=receipts,
        api_inputs=api_inputs,
        source_event_id=lambda: current_event_id,
        capture_source_event_id=capture_source_event_id,
    )
    events = case["stimulus"]["events"]
    bots: dict[str, Any] = {}
    async with driver._lifespan:
        try:
            for event in events:
                account_id = str(event["account_id"])
                bot = bots.get(account_id)
                if bot is None:
                    bot = Bot(adapter, account_id)
                    adapter.bot_connect(bot)
                    bots[account_id] = bot
                conversation = event["conversation"]
                message_type = "group" if conversation["kind"] == "group" else "private"
                timestamp = int(datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00")).timestamp())
                raw = {
                    "post_type": "message",
                    "message_type": message_type,
                    "sub_type": "normal",
                    "self_id": int(event["account_id"]),
                    "user_id": int(event["actor_id"]),
                    "message_id": int(event.get("extensions", {}).get("onebot.v11", {}).get("message_id", "1")),
                    "time": timestamp,
                    "message": _onebot_message(event),
                    "raw_message": "".join(
                        str(segment.get("text", ""))
                        for segment in event.get("message", [])
                        if segment.get("type") == "text"
                    ),
                    "font": 0,
                    "sender": {"user_id": int(event["actor_id"]), "nickname": "Alice", "card": ""},
                }
                if message_type == "group":
                    raw["group_id"] = int(conversation["id"])
                native_event = adapter.json_to_event(raw)
                if native_event is None:
                    raise RuntimeError("stock OneBot adapter rejected the stimulus event")
                current_event_id = str(event["event_id"])
                await bot.handle_event(native_event)
        finally:
            for bot in bots.values():
                if bot.self_id in adapter.bots:
                    adapter.bot_disconnect(bot)

    bridge_modules = sorted(
        name for name in sys.modules
        if name == "ligamina_nonebot" or name.startswith("ligamina_nonebot.")
    )
    if bridge_modules:
        raise RuntimeError(f"stock baseline imported Ligamina Bridge modules: {bridge_modules}")

    fingerprints = _fingerprints(context, case["stimulus"])
    expected = context["expected_environment"]
    environment = {
        "ecosystem": "nonebot",
        "framework_version": nonebot.__version__,
        "runtime_version": ".".join(map(str, sys.version_info[:3])),
        **fingerprints,
    }
    if environment != expected:
        raise RuntimeError(f"stock native fingerprint mismatch: expected {expected}, observed {environment}")
    result = {
        "status": "completed",
        "environment": environment,
        "events": events,
        "operations": operations,
        "operation_receipts": receipts,
        "cleanup": {"active_capability_count": 0, "live_processes": []},
        "stock_native": {
            "framework": "NoneBot",
            "adapter": "OneBot v11",
            "bridge_modules": bridge_modules,
            "api_inputs": api_inputs,
            "runtime_commands": runtime_commands,
            "runtime_plugins": loaded_plugin_names,
        },
    }
    if behavior_fixture:
        result["native_trace"] = {"bridge_adapter_imported": False}
        result["binding"] = context.get("binding")
        result["capability_probe_observation"] = _probe_observation(work, events)
        result["plugin_observation"] = {
            "events": [
                {"event_id": event["event_id"], "account_id": event["account_id"], "actor_id": event["actor_id"], "conversation": event["conversation"]}
                for event in events
            ],
            "operations": operations,
            "receipts": receipts,
            "response_or_operation_count": len(operations),
        }
    return result


def main() -> int:
    request = json.load(sys.stdin)
    with contextlib.redirect_stdout(sys.stderr):
        result = asyncio.run(_run(request))
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
