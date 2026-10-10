"""Small deterministic Worker for exercising the native_runner contract only."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any


def fixture_result(
    request: dict[str, Any], profile_path: Path, lockfile_path: Path, injection: str | None
) -> dict[str, Any]:
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    events = request["stimulus"]["events"]
    operations = []
    receipts = []
    for event in events:
        text = next(
            (
                segment["text"]
                for segment in event["message"]
                if segment.get("type") == "text" and segment["text"].startswith("/echo ")
            ),
            None,
        )
        reply_id = next(
            (
                segment["message_id"]
                for segment in event["message"]
                if segment.get("type") == "reply"
            ),
            None,
        )
        if text is not None and reply_id is not None:
            operation_id = "op-" + event["event_id"].removeprefix("evt-")
            operation = {
                "operation_id": operation_id,
                "account_id": event["account_id"],
                "operation": "send_message",
                "target": {
                    "kind": event["conversation"]["kind"],
                    "id": event["conversation"]["id"],
                },
                "params": {
                    "message": [
                        {"type": "reply", "message_id": reply_id},
                        {"type": "text", "text": text.removeprefix("/echo ")},
                    ]
                },
            }
            operations.append(operation)
            receipts.append(
                {
                    "operation_id": operation_id,
                    "status": "succeeded",
                    "platform_message_id": "virtual-message-1",
                    "target": operation["target"],
                }
            )

    observed_events = json.loads(json.dumps(events))
    if injection == "wrong-recipient" and operations:
        operations[0]["target"]["id"] = "wrong-room"
    elif injection == "duplicate-reply" and operations:
        operations.append(json.loads(json.dumps(operations[0])))
        receipts.append(json.loads(json.dumps(receipts[0])))
    elif injection == "drop-message-segment" and observed_events:
        observed_events[0]["message"] = [
            segment for segment in observed_events[0]["message"] if segment.get("type") != "reply"
        ]
    cleanup = {"active_capability_count": 0, "live_processes": []}
    if injection == "leak-process":
        cleanup["live_processes"] = [1234]

    return {
        "status": "completed",
        "mode": os.environ.get("LIGAMINA_RUN_MODE"),
        "environment": {
            "ecosystem": profile["ecosystem"],
            "framework_version": profile["framework_version"],
            "runtime_version": ".".join(map(str, sys.version_info[:3])),
            "lockfile_sha256": hashlib.sha256(lockfile_path.read_bytes()).hexdigest(),
            "profile_sha256": hashlib.sha256(profile_path.read_bytes()).hexdigest(),
        },
        "events": observed_events,
        "operations": operations,
        "operation_receipts": receipts,
        "cleanup": cleanup,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--lockfile", type=Path, required=True)
    parser.add_argument(
        "--inject",
        choices=("wrong-recipient", "duplicate-reply", "drop-message-segment", "leak-process"),
    )
    options = parser.parse_args()
    request = json.load(sys.stdin)
    print(json.dumps(fixture_result(request, options.profile, options.lockfile, options.inject)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
