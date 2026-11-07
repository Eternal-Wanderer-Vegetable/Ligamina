"""Native AstrBot platform adapter for Ligamina's virtual event/platform boundary.

The unmodified AstrBot pipeline (waking, whitelist, rate limit, command dispatch,
result decoration, respond) handles every event. This adapter only converts Core
event envelopes into ``AstrBotMessage`` objects and converts the pipeline's sends
into Core ``platform.execute`` operations. It opens no network connection.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Any

from astrbot.api.platform import (
    AstrBotMessage,
    AstrMessageEvent,
    Group,
    MessageMember,
    MessageType,
    Platform,
    PlatformMetadata,
    register_platform_adapter,
)
from astrbot.core.message.components import At, Image, Plain, Reply
from astrbot.core.message.message_event_result import MessageChain

from .contracts import validate_event, validate_operation
from .runtime import ADAPTER_NAME, RUNTIME


class BridgeApiError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class BridgeEvent(AstrMessageEvent):
    """A native AstrBot event whose sends become Core platform operations."""

    def __init__(self, *args: Any, envelope: dict[str, Any], done: asyncio.Future, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._envelope = envelope
        self._done = done

    async def send(self, message: MessageChain) -> None:
        if message is None:
            return
        segments = outgoing_segments(message)
        if segments:
            conversation = self._envelope["conversation"]
            operation = {
                "operation_id": str(uuid.uuid4()),
                "trace_id": self._envelope["trace_id"],
                "account_id": self._envelope["account_id"],
                "operation": "send_message",
                "target": {"kind": conversation["kind"], "id": conversation["id"]},
                "params": {"message": segments},
            }
            errors = validate_operation(operation)
            if errors:
                raise BridgeApiError("invalid_request", "; ".join(errors))
            call = RUNTIME["platform_call"]
            if call is None:
                raise BridgeApiError("host_not_ready", "platform.execute is unavailable")
            result = await call(operation)
            if not isinstance(result, dict) or result.get("status") != "succeeded":
                status = result.get("status") if isinstance(result, dict) else None
                code = "operation_unknown" if status == "unknown" else "platform_operation_failed"
                raise BridgeApiError(code, f"platform operation ended with status {status!r}")
        await super().send(message)

    def cleanup_temporary_local_files(self) -> None:
        # PipelineScheduler.execute calls this in its `finally`, i.e. exactly when the
        # native pipeline has finished with this event. Use it as the completion signal.
        try:
            super().cleanup_temporary_local_files()
        finally:
            if not self._done.done():
                self._done.set_result(None)


@register_platform_adapter(ADAPTER_NAME, "Ligamina virtual platform bridge (no network connection)")
class BridgePlatform(Platform):
    def __init__(self, platform_config: dict, platform_settings: dict, event_queue: asyncio.Queue) -> None:
        super().__init__(platform_config, event_queue)
        self.settings = platform_settings
        self._stop = asyncio.Event()
        RUNTIME["instance"] = self

    def meta(self) -> PlatformMetadata:
        return PlatformMetadata(
            name=ADAPTER_NAME,
            description="Ligamina virtual platform bridge",
            id=self.config.get("id", ADAPTER_NAME),
        )

    async def run(self) -> None:
        await self._stop.wait()

    async def terminate(self) -> None:
        self._stop.set()

    async def send_by_session(self, session: Any, message_chain: MessageChain) -> None:
        # Proactive sends outside an event need an explicit account and a ledger path;
        # they are not part of this profile yet.
        raise BridgeApiError("unsupported_api", "proactive send_by_session is not supported by the Ligamina bridge")

    async def dispatch(self, envelope: dict[str, Any], *, timeout: float = 30.0) -> None:
        errors = validate_event(envelope)
        if errors:
            raise BridgeApiError("invalid_request", "; ".join(errors))
        if envelope["target_host"] != RUNTIME["host_id"]:
            raise BridgeApiError("wrong_host", "event target_host does not match this Host")
        message = to_astrbot_message(envelope)
        done: asyncio.Future = asyncio.get_running_loop().create_future()
        event = BridgeEvent(
            message_str=message.message_str,
            message_obj=message,
            platform_meta=self.meta(),
            session_id=message.session_id,
            envelope=envelope,
            done=done,
        )
        self.commit_event(event)
        try:
            await asyncio.wait_for(done, timeout=timeout)
        except asyncio.TimeoutError as error:
            raise BridgeApiError("timeout", f"AstrBot pipeline did not finish in {timeout:g} seconds") from error


def to_astrbot_message(envelope: dict[str, Any]) -> AstrBotMessage:
    conversation = envelope["conversation"]
    metadata = (envelope.get("extensions") or {}).get(envelope["platform"]) or {}
    sender = metadata.get("sender") if isinstance(metadata, dict) else None
    nickname = sender.get("nickname") if isinstance(sender, dict) else None
    message = AstrBotMessage()
    message.type = MessageType.GROUP_MESSAGE if conversation["kind"] == "group" else MessageType.FRIEND_MESSAGE
    message.self_id = envelope["account_id"]
    message.session_id = conversation["id"]
    message.message_id = str(metadata.get("message_id") or envelope["event_id"]) if isinstance(metadata, dict) else envelope["event_id"]
    if conversation["kind"] == "group":
        message.group = Group(group_id=conversation["id"])
    message.sender = MessageMember(user_id=envelope["actor_id"], nickname=nickname)
    components = []
    for segment in envelope["message"]:
        kind = segment.get("type")
        if kind == "text":
            components.append(Plain(segment["text"]))
        elif kind == "mention":
            components.append(At(qq=segment["user_id"]))
        elif kind == "reply":
            components.append(Reply(id=segment["message_id"]))
        elif kind == "image_ref":
            components.append(Image(file=segment["resource_ref"]))
        else:
            raise BridgeApiError("unsupported_api", f"message segment {kind!r} is unsupported")
    message.message = components
    message.message_str = "".join(segment.get("text", "") for segment in envelope["message"] if segment.get("type") == "text")
    message.raw_message = envelope
    try:
        message.timestamp = int(datetime.fromisoformat(envelope["timestamp"].replace("Z", "+00:00")).timestamp())
    except ValueError as error:
        raise BridgeApiError("invalid_request", "timestamp must be ISO-8601") from error
    return message


def outgoing_segments(chain: MessageChain) -> list[dict[str, str]]:
    segments: list[dict[str, str]] = []
    for component in chain.chain:
        if isinstance(component, Plain):
            if component.text:
                segments.append({"type": "text", "text": component.text})
        elif isinstance(component, At):
            segments.append({"type": "mention", "user_id": str(component.qq)})
        elif isinstance(component, Reply):
            segments.append({"type": "reply", "message_id": str(component.id)})
        elif isinstance(component, Image):
            reference = component.url or component.file
            if not reference:
                raise BridgeApiError("unsupported_api", "image component has no stable resource reference")
            segments.append({"type": "image_ref", "resource_ref": str(reference)})
        else:
            raise BridgeApiError("unsupported_api", f"AstrBot message component {type(component).__name__} is unsupported")
    return segments
