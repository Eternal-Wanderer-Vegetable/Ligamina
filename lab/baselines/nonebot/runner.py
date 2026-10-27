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


def _fingerprints(context: dict[str, Any], stimulus: dict[str, Any]) -> dict[str, str]:
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
    }


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


async def _run(request: dict[str, Any]) -> dict[str, Any]:
    case = request
    context = case["context"]
    sample = context["sample"]
    behavior_fixture = context.get("behavior_fixture")
    work = Path(context.get("execution", {}).get("workspace", context["work_dir"])).resolve()
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
    if behavior_fixture:
        if behavior_fixture.get("adapter", {}).get("id") != "nonebot.onebot.v11":
            raise ValueError("v2 fixture adapter does not match the stock NoneBot runner")
        config = behavior_fixture["setup"].get("plugin_config", {})
        package = sample.get("package")
        nonebot_config: dict[str, Any] = {}
        if package == "nonebot_plugin_help":
            if (
                set(config) != {"help_at_sender", "help_ignore_plugins"}
                or not isinstance(config["help_at_sender"], bool)
                or config["help_ignore_plugins"] != ["phase4_random_seed", "phase4_probe_observer"]
            ):
                raise ValueError("v2 NoneBot Help config is outside the reviewed allowlist")
            nonebot_config = config
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
    nonebot.init(driver="~fastapi", nickname={"Bot"}, **(nonebot_config if behavior_fixture and sample.get("package") == "nonebot_plugin_help" else {}))
    driver = nonebot.get_driver()
    driver.register_adapter(Adapter)
    adapter = driver._adapters[Adapter.get_name()]

    plugin_names = [*sample.get("preload_plugins", []), sample["_entry"]]
    for plugin_name in plugin_names:
        if nonebot.load_plugin(plugin_name) is None:
            raise RuntimeError(f"NoneBot could not load plugin {plugin_name!r}")

    operations: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    api_inputs: list[dict[str, Any]] = []
    current_event_id: str | None = None

    async def native_api(bot: Any, api: str, **params: Any) -> Any:
        if api == "get_msg":
            message_id = str(params.get("message_id", ""))
            return {
                "message_id": message_id,
                "real_id": message_id,
                "sender": {"user_id": int(bot.self_id)},
                "time": 0,
                "message": [],
            }
        if api not in {"send_msg", "send_group_msg", "send_private_msg"}:
            raise RuntimeError(f"stock native baseline has no virtual API fixture for {api!r}")
        raw_message = params.get("message", [])
        api_inputs.append({
            "api": api,
            "message_type": type(raw_message).__name__,
            "message": [
                {"type": getattr(segment, "type", None), "data": getattr(segment, "data", None)}
                for segment in raw_message
            ] if isinstance(raw_message, (list, tuple)) else str(raw_message),
        })
        group_id = params.get("group_id")
        user_id = params.get("user_id")
        kind = "group" if group_id is not None else "private" if user_id is not None else None
        target_id = group_id if group_id is not None else user_id
        if kind is None or target_id is None:
            raise RuntimeError(f"native OneBot API {api!r} omitted a target")
        operation_id = f"operation-{len(operations) + 1}"
        target = {"kind": kind, "id": str(target_id)}
        operation = {
            "operation_id": operation_id,
            "account_id": str(bot.self_id),
            "operation": "send_message",
            "target": target,
            "params": {"message": _message_segments(params.get("message", []))},
        }
        if behavior_fixture:
            operation["source_event_id"] = current_event_id
        operations.append(operation)
        receipt = {
            "operation_id": operation_id,
            "status": "succeeded",
            "target": target,
        }
        if behavior_fixture:
            receipt["account_id"] = str(bot.self_id)
            receipt["source_event_id"] = current_event_id
        receipts.append(receipt)
        return {"message_id": f"virtual-message-{len(receipts)}"}

    adapter._call_api = native_api
    events = case["stimulus"]["events"]
    bots: dict[str, Any] = {}
    try:
        async with driver._lifespan:
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
