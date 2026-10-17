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


async def _run(request: dict[str, Any]) -> dict[str, Any]:
    case = request
    context = case["context"]
    sample = context["sample"]
    work = Path(context["work_dir"]).resolve()
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

    import nonebot
    from nonebot.adapters.onebot.v11 import Adapter, Bot

    if nonebot.__version__ != context["expected_environment"]["framework_version"]:
        raise RuntimeError(f"NoneBot version mismatch: {nonebot.__version__}")
    nonebot.init(driver="~fastapi", nickname={"Bot"})
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

    async def native_api(bot: Any, api: str, **params: Any) -> Any:
        if api == "get_msg":
            message_id = str(params.get("message_id", ""))
            return {
                "message_id": message_id,
                "real_id": message_id,
                "sender": {"user_id": int(context["account_id"])},
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
        operations.append({
            "operation_id": operation_id,
            "account_id": str(bot.self_id),
            "operation": "send_message",
            "target": target,
            "params": {"message": _message_segments(params.get("message", []))},
        })
        receipt = {
            "operation_id": operation_id,
            "status": "succeeded",
            "target": target,
        }
        receipts.append(receipt)
        return {"message_id": f"virtual-message-{len(receipts)}"}

    adapter._call_api = native_api
    bot = Bot(adapter, str(context["account_id"]))
    events = case["stimulus"]["events"]
    try:
        async with driver._lifespan:
            adapter.bot_connect(bot)
            for event in events:
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
                await bot.handle_event(native_event)
            adapter.bot_disconnect(bot)
    finally:
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
    return {
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


def main() -> int:
    request = json.load(sys.stdin)
    with contextlib.redirect_stdout(sys.stderr):
        result = asyncio.run(_run(request))
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
