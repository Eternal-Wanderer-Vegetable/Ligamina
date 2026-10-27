"""Record framework-observed event scope and clock under the isolated Host data directory."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from nonebot import on_message
from nonebot.adapters.onebot.v11 import MessageEvent


observer = on_message(priority=0, block=False)


@observer.handle()
async def record_host_observation(event: MessageEvent) -> None:
    target = os.environ.get("LIGAMINA_P4_OBSERVATION_FILE")
    if not target:
        raise RuntimeError("Phase 4 observation file is not configured")
    observed_at = datetime.fromtimestamp(int(event.time), timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    if event.message_type == "private":
        conversation = {"kind": "private", "id": str(event.user_id)}
    elif event.message_type == "group":
        conversation = {"kind": "group", "id": str(event.group_id)}
    else:
        raise RuntimeError(f"unsupported OneBot observation scope: {event.message_type!r}")
    record = {
        "message_id": str(event.message_id),
        "account_id": str(event.self_id),
        "actor_id": str(event.user_id),
        "conversation": conversation,
        "timestamp": observed_at,
    }
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
