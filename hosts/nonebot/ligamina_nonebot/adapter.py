"""Native OneBot v11 Bridge Adapter for Ligamina's virtual event/platform boundary."""

from __future__ import annotations

import contextvars
import re
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable

from nonebot.adapters import Adapter as BaseAdapter
from nonebot.adapters.onebot.v11 import Adapter as OneBotAdapter
from nonebot.adapters.onebot.v11 import Bot, Event, Message, MessageSegment
from nonebot.adapters.onebot.v11.event import MessageEvent

from .contracts import classify_operation_support, validate_event, validate_operation

PlatformCall = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
ServiceCall = Callable[[dict[str, Any]], Awaitable[Any]]
MAX_ONEBOT_ID = (1 << 63) - 1
MAX_REMEMBERED_MESSAGES = 10_000


@dataclass(frozen=True)
class EventContext:
    trace_id: str
    target: dict[str, str]
    message_id: str


_current_event: contextvars.ContextVar[EventContext | None] = contextvars.ContextVar(
    "ligamina_nonebot_event", default=None
)


class BridgeApiError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class BridgeAdapter(BaseAdapter):
    """Runs OneBot v11 native Events/Bots without opening a OneBot network connection."""

    def __init__(
        self,
        driver,
        *,
        host_id: str,
        platform_call: PlatformCall,
        service_call: ServiceCall | None = None,
        nickname: str = "",
    ) -> None:
        super().__init__(driver)
        if not host_id.strip():
            raise ValueError("host_id must not be empty")
        self.host_id = host_id
        self.platform_call = platform_call
        self.service_call = service_call
        self.nickname = nickname
        self._message_targets: OrderedDict[str, dict[str, str]] = OrderedDict()
        self._stopping = False
        self.driver.on_shutdown(self.shutdown)

    async def invoke_service(
        self,
        capability_id: str,
        arguments: dict[str, Any],
        *,
        trace_id: str | None = None,
    ) -> Any:
        """Invoke one explicitly named Core capability through the trusted Host session."""
        if not isinstance(capability_id, str) or not capability_id.strip():
            raise BridgeApiError("invalid_request", "capability_id must be a non-empty string")
        if not isinstance(arguments, dict):
            raise BridgeApiError("invalid_request", "capability arguments must be an object")
        if self.service_call is None:
            raise BridgeApiError("host_not_ready", "service.invoke is unavailable")
        event_context = _current_event.get()
        return await self.service_call({
            "capability_id": capability_id,
            "arguments": arguments,
            "trace_id": trace_id or (event_context.trace_id if event_context else str(uuid.uuid4())),
        })

    @classmethod
    def get_name(cls) -> str:
        return "Ligamina OneBot V11 Bridge"

    async def dispatch(self, envelope: dict[str, Any]) -> None:
        errors = validate_event(envelope)
        if errors:
            raise BridgeApiError("invalid_request", "; ".join(errors))
        if envelope["platform"] != "onebot.v11":
            raise BridgeApiError("unsupported_api", "Bridge Adapter only accepts onebot.v11 events")
        if envelope["target_host"] != self.host_id:
            raise BridgeApiError("wrong_host", "event target_host does not match this Host")
        if self._stopping:
            raise BridgeApiError("host_stopping", "Bridge Adapter is shutting down")

        account_id = envelope["account_id"]
        bot = self.bots.get(account_id)
        if bot is None:
            bot = Bot(self, account_id)
            self.bot_connect(bot)

        event = to_onebot_event(envelope, nickname=self.nickname)
        target = {
            "kind": envelope["conversation"]["kind"],
            "id": envelope["conversation"]["id"],
        }
        self._remember_message_target(str(event.message_id), target)
        context = EventContext(
            trace_id=envelope["trace_id"],
            target=target,
            message_id=str(event.message_id),
        )
        token = _current_event.set(context)
        try:
            # Keep the framework's native preprocessing and Matcher route intact.
            await bot.handle_event(event)
        finally:
            _current_event.reset(token)

    async def _call_api(self, bot: Bot, api: str, **data: Any) -> Any:
        if api == "send_msg":
            return await self._send_message(bot, data)
        if api == "send_group_msg":
            return await self._send_message(bot, data, expected_kind="group")
        if api == "send_private_msg":
            return await self._send_message(bot, data, expected_kind="private")
        if api == "get_msg":
            return await self._get_message(bot, data)
        if api == "get_login_info":
            return await self._get_self_info(bot)
        raise BridgeApiError("unsupported_api", f"OneBot API {api!r} is not supported by this profile")

    async def _send_message(
        self,
        bot: Bot,
        data: dict[str, Any],
        *,
        expected_kind: str | None = None,
    ) -> dict[str, int]:
        allowed = {"group_id", "user_id", "message_type", "message", "_timeout"}
        if expected_kind is not None:
            allowed.add("auto_escape")
        unexpected = set(data) - allowed
        if unexpected:
            raise BridgeApiError("unsupported_api", f"send message parameters are unsupported: {sorted(unexpected)}")
        send_data = data
        auto_escape = False
        if expected_kind is not None:
            target_field = "group_id" if expected_kind == "group" else "user_id"
            other_field = "user_id" if expected_kind == "group" else "group_id"
            if data.get(target_field) is None or data.get(other_field) is not None or data.get("message_type") is not None:
                raise BridgeApiError("invalid_request", f"{expected_kind} send requires only {target_field}")
            auto_escape = data.get("auto_escape", False)
            if not isinstance(auto_escape, bool):
                raise BridgeApiError("invalid_request", "auto_escape must be a boolean")
            send_data = {**data, "message_type": expected_kind}
        target = _send_target(send_data)
        segments = _outgoing_segments(data.get("message"), auto_escape=auto_escape)
        operation = self._operation(
            bot,
            "send_message",
            target,
            {"message": segments},
        )
        result = await self._execute(operation)
        message_id = result.get("platform_message_id")
        if not isinstance(message_id, str):
            raise BridgeApiError("invalid_platform_result", "send_message result has no platform_message_id")
        numeric_message_id = _onebot_id(message_id, "platform_message_id")
        self._remember_message_target(message_id, target)
        return {"message_id": numeric_message_id}

    async def _get_message(self, bot: Bot, data: dict[str, Any]) -> dict[str, Any]:
        unexpected = set(data) - {"message_id", "_timeout"}
        if unexpected:
            raise BridgeApiError("unsupported_api", f"get_msg parameters are unsupported: {sorted(unexpected)}")
        message_id = _onebot_id(str(data.get("message_id", "")), "message_id")
        message_id_text = str(message_id)
        context = _current_event.get()
        target = self._message_targets.get(message_id_text) or (
            context.target if context is not None else None
        )
        if target is None:
            raise BridgeApiError("unsupported_api", "get_msg has no known conversation scope")
        operation = self._operation(
            bot,
            "get_message",
            target,
            {"message_id": message_id_text},
        )
        result = await self._execute(operation)
        payload = result.get("data")
        if not isinstance(payload, dict):
            raise BridgeApiError("invalid_platform_result", "get_message result has no data object")
        return to_onebot_reply(payload, expected_target=target)

    async def _get_self_info(self, bot: Bot) -> dict[str, Any]:
        context = _current_event.get()
        operation = self._operation(
            bot,
            "get_self_info",
            context.target if context is not None else {"kind": "private", "id": bot.self_id},
            {},
        )
        result = await self._execute(operation)
        payload = result.get("data")
        if not isinstance(payload, dict):
            raise BridgeApiError("invalid_platform_result", "get_self_info result has no data object")
        user_id = _onebot_id(str(payload.get("user_id", bot.self_id)), "user_id")
        nickname = payload.get("nickname")
        if not isinstance(nickname, str):
            raise BridgeApiError("invalid_platform_result", "get_self_info result has no nickname")
        return {"user_id": user_id, "nickname": nickname}

    def _operation(
        self,
        bot: Bot,
        operation_name: str,
        target: dict[str, str],
        params: dict[str, Any],
    ) -> dict[str, Any]:
        if classify_operation_support(operation_name) != "supported":
            raise BridgeApiError("unsupported_api", f"operation {operation_name!r} is not supported")
        context = _current_event.get()
        operation = {
            "operation_id": str(uuid.uuid4()),
            "trace_id": context.trace_id if context is not None else str(uuid.uuid4()),
            "account_id": bot.self_id,
            "operation": operation_name,
            "target": target,
            "params": params,
        }
        errors = validate_operation(operation)
        if errors:
            raise BridgeApiError("invalid_request", "; ".join(errors))
        return operation

    async def _execute(self, operation: dict[str, Any]) -> dict[str, Any]:
        result = await self.platform_call(operation)
        if not isinstance(result, dict):
            raise BridgeApiError("invalid_platform_result", "Core returned a non-object platform result")
        status = result.get("status")
        if status != "succeeded":
            code = "operation_unknown" if status == "unknown" else "platform_operation_failed"
            raise BridgeApiError(code, str(result.get("error") or f"operation status is {status!r}"))
        return result

    def _remember_message_target(self, message_id: str, target: dict[str, str]) -> None:
        self._message_targets[message_id] = dict(target)
        self._message_targets.move_to_end(message_id)
        while len(self._message_targets) > MAX_REMEMBERED_MESSAGES:
            self._message_targets.popitem(last=False)

    async def shutdown(self) -> None:
        self._stopping = True
        for bot in list(self.bots.values()):
            self.bot_disconnect(bot)


def to_onebot_event(envelope: dict[str, Any], *, nickname: str = "") -> MessageEvent:
    """Map only the explicit v0.1 identity/message fields into the native v11 Event model."""
    account_id = _onebot_id(envelope["account_id"], "account_id")
    actor_id = _onebot_id(envelope["actor_id"], "actor_id")
    conversation = envelope["conversation"]
    conversation_kind = conversation["kind"]
    conversation_id = _onebot_id(conversation["id"], "conversation.id")
    if conversation_kind == "private" and actor_id != conversation_id:
        raise BridgeApiError("invalid_request", "private conversation ID must match actor_id")
    metadata = _onebot_metadata(envelope)
    message_id = _onebot_id(metadata.get("message_id"), "extensions.onebot.v11.message_id")
    timestamp = _timestamp_seconds(envelope["timestamp"])
    message = Message(_incoming_segment(segment) for segment in envelope["message"])
    sender = metadata.get("sender")
    sender_name = sender.get("nickname", nickname) if isinstance(sender, dict) else nickname
    event_payload: dict[str, Any] = {
        "time": timestamp,
        "self_id": account_id,
        "post_type": "message",
        "sub_type": _sub_type(metadata, conversation_kind),
        "user_id": actor_id,
        "message_type": conversation_kind,
        "message_id": message_id,
        "message": message,
        "raw_message": str(message),
        "font": _font(metadata),
        "sender": {"user_id": actor_id, **({"nickname": sender_name} if sender_name else {})},
        "event_id": envelope["event_id"],
        "trace_id": envelope["trace_id"],
        "extensions": envelope.get("extensions", {}),
    }
    if conversation_kind == "group":
        event_payload["group_id"] = conversation_id
    # Let the locked adapter's model parser produce its native Group/PrivateMessageEvent.
    event = OneBotAdapter.json_to_event(event_payload)
    if not isinstance(event, MessageEvent):
        raise BridgeApiError("invalid_request", "native OneBot event parser did not produce a message event")
    return event


def to_onebot_reply(payload: dict[str, Any], *, expected_target: dict[str, str]) -> dict[str, Any]:
    target = payload.get("target")
    if target != expected_target:
        raise BridgeApiError("invalid_platform_result", "get_message returned a different conversation")
    message_id = _onebot_id(payload.get("message_id"), "data.message_id")
    actor_id = _onebot_id(payload.get("actor_id"), "data.actor_id")
    timestamp = _timestamp_seconds(str(payload.get("timestamp", "")))
    message_value = payload.get("message")
    if not isinstance(message_value, list):
        raise BridgeApiError("invalid_platform_result", "get_message data.message must be a list")
    message = Message(_incoming_segment(segment) for segment in message_value)
    reply: dict[str, Any] = {
        "time": timestamp,
        "message_type": expected_target["kind"],
        "message_id": message_id,
        "real_id": _onebot_id(str(payload.get("real_id", message_id)), "data.real_id"),
        "sender": {"user_id": actor_id, **({"nickname": payload["nickname"]} if isinstance(payload.get("nickname"), str) else {})},
        "message": message,
    }
    if expected_target["kind"] == "group":
        reply["group_id"] = _onebot_id(expected_target["id"], "target.id")
    else:
        reply["user_id"] = actor_id
    return reply


def _onebot_metadata(envelope: dict[str, Any]) -> dict[str, Any]:
    extensions = envelope.get("extensions", {})
    if not isinstance(extensions, dict):
        return {}
    metadata = extensions.get("onebot.v11", {})
    if not isinstance(metadata, dict):
        return {}
    sender = metadata.get("sender")
    if sender is not None:
        if not isinstance(sender, dict):
            raise BridgeApiError("invalid_request", "onebot.v11.sender must be an object")
        if "user_id" in sender and _onebot_id(sender["user_id"], "sender.user_id") != _onebot_id(envelope["actor_id"], "actor_id"):
            raise BridgeApiError("invalid_request", "onebot.v11.sender.user_id conflicts with actor_id")
    return metadata


def _sub_type(metadata: dict[str, Any], conversation_kind: str) -> str:
    value = metadata.get("sub_type")
    default = "normal" if conversation_kind == "group" else "friend"
    if value is None:
        return default
    if value not in {"normal", "anonymous", "notice", "friend", "group", "other"}:
        raise BridgeApiError("invalid_request", "onebot.v11.sub_type is not supported")
    return value


def _font(metadata: dict[str, Any]) -> int:
    value = metadata.get("font", 0)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise BridgeApiError("invalid_request", "onebot.v11.font must be a nonnegative integer")
    return value


def _timestamp_seconds(value: str) -> int:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise BridgeApiError("invalid_request", "timestamp must be ISO-8601") from error
    if parsed.tzinfo is None:
        raise BridgeApiError("invalid_request", "timestamp must include a timezone")
    return int(parsed.timestamp())


def _onebot_id(value: Any, field: str) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        numeric = value
    elif isinstance(value, str) and value.isascii() and value.isdigit():
        numeric = int(value)
    else:
        raise BridgeApiError("unsupported_api", f"{field} is outside the OneBot v11 numeric ID profile")
    if numeric < 0 or numeric > MAX_ONEBOT_ID:
        raise BridgeApiError("unsupported_api", f"{field} exceeds the OneBot v11 numeric ID profile")
    return numeric


def _incoming_segment(segment: dict[str, Any]) -> MessageSegment:
    segment_type = segment.get("type")
    if segment_type == "text":
        return MessageSegment.text(segment["text"])
    if segment_type == "mention":
        return MessageSegment.at(_onebot_id(segment["user_id"], "message.mention.user_id"))
    if segment_type == "reply":
        return MessageSegment.reply(_onebot_id(segment["message_id"], "message.reply.message_id"))
    if segment_type == "image_ref":
        return MessageSegment.image(str(segment["resource_ref"]))
    raise BridgeApiError("unsupported_api", f"message segment {segment_type!r} is not supported")


def _outgoing_segments(message: Any, *, auto_escape: bool = False) -> list[dict[str, Any]]:
    if isinstance(message, str):
        message = Message([MessageSegment.text(message)]) if auto_escape else Message(message)
    if not isinstance(message, Message):
        raise BridgeApiError("unsupported_api", "send_msg requires a OneBot v11 Message")
    segments: list[dict[str, Any]] = []
    for segment in message:
        if segment.type == "text":
            segments.append({"type": "text", "text": str(segment.data.get("text", ""))})
        elif segment.type == "at":
            segments.append({"type": "mention", "user_id": str(_onebot_id(segment.data.get("qq"), "message.at.qq"))})
        elif segment.type == "reply":
            segments.append({"type": "reply", "message_id": str(_onebot_id(segment.data.get("id"), "message.reply.id"))})
        elif segment.type == "image":
            file_ref = segment.data.get("file")
            if not isinstance(file_ref, str) or not file_ref:
                raise BridgeApiError("unsupported_api", "image segment has no stable resource reference")
            media_type = _media_type(file_ref)
            outgoing = {"type": "image_ref", "resource_ref": file_ref}
            if media_type:
                outgoing["media_type"] = media_type
            segments.append(outgoing)
        else:
            raise BridgeApiError("unsupported_api", f"OneBot message segment {segment.type!r} is unsupported")
    return segments


def _send_target(data: dict[str, Any]) -> dict[str, str]:
    group_id = data.get("group_id")
    user_id = data.get("user_id")
    message_type = data.get("message_type")
    if message_type == "group" and group_id is not None:
        target = {"kind": "group", "id": str(_onebot_id(group_id, "group_id"))}
    elif message_type == "private" and user_id is not None:
        target = {"kind": "private", "id": str(_onebot_id(user_id, "user_id"))}
    elif message_type is not None:
        raise BridgeApiError("invalid_request", "message_type requires its matching target ID")
    elif group_id is not None and user_id is None:
        target = {"kind": "group", "id": str(_onebot_id(group_id, "group_id"))}
    elif user_id is not None and group_id is None:
        target = {"kind": "private", "id": str(_onebot_id(user_id, "user_id"))}
    else:
        raise BridgeApiError("invalid_request", "send_msg requires one unambiguous target")
    return target


def _media_type(resource_ref: str) -> str | None:
    match = re.search(r"\.(png|jpe?g|gif|webp|bmp)(?:[?#].*)?$", resource_ref, re.IGNORECASE)
    if match is None:
        return None
    extension = match.group(1).lower()
    return "image/jpeg" if extension in {"jpg", "jpeg"} else f"image/{extension}"
