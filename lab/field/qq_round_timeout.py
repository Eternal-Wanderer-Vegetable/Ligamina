"""Round C driver: pause the field Hosts after one more normal reply, then check for late replies.

    python lab/field/qq_round_timeout.py --stella-log <log> --owner <owner> --sender <qq> --bot-id <qq> --turns 9 --journal <json>

1. Waits for the next delivered reply from `sender` (a normal call, Hosts running).
2. `docker pause`s every Host container of this Core (owner label): calls now hang
   until the Core's 30 s command deadline.
3. Waits until `turns` more replies from `sender` were delivered.
4. Unpauses, then watches Stella's outbound log for 120 s: no extra reply may appear,
   because the Core refuses replies that arrive after a tool call's deadline.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import qq_round  # noqa: E402

DOCKER = [r"C:\Windows\System32\wsl.exe", "-e", "docker"]


def containers(owner: str) -> list[str]:
    out = subprocess.run([*DOCKER, "ps", "--format", "{{.Names}}", "--filter", f"label=io.{'ligamina'}.owner={owner}"],
                         capture_output=True, text=True, timeout=60).stdout
    return [line for line in out.splitlines() if line]


def delivered(log: Path, since: str, sender: str, bot_id: str) -> list[dict]:
    return [t for t in qq_round.collect(log, since, bot_id, sender) if t["delivery"] is not None]


def wait_turns(log, since, sender, bot_id, count) -> list[dict]:
    while True:
        done = delivered(log, since, sender, bot_id)
        if len(done) >= count:
            return done
        time.sleep(2)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stella-log", type=Path, required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--sender", required=True)
    parser.add_argument("--bot-id", required=True)
    parser.add_argument("--turns", type=int, default=9)
    parser.add_argument("--journal", type=Path, required=True)
    args = parser.parse_args()
    journal: dict = {"started": time.strftime("%H:%M:%S")}

    def save() -> None:
        args.journal.write_text(json.dumps(journal, ensure_ascii=False, indent=2), encoding="utf-8")

    first = wait_turns(args.stella_log, journal["started"], args.sender, args.bot_id, 1)[0]
    journal["before_pause"] = first
    names = containers(args.owner)
    subprocess.run([*DOCKER, "pause", *names], capture_output=True, timeout=60)
    journal["paused"] = {"at": time.strftime("%H:%M:%S"), "containers": names}
    save()
    print(json.dumps({"paused": names}), flush=True)

    paused_since = journal["paused"]["at"]
    turns = wait_turns(args.stella_log, paused_since, args.sender, args.bot_id, args.turns)
    journal["paused_turns"] = turns
    subprocess.run([*DOCKER, "unpause", *names], capture_output=True, timeout=60)
    journal["unpaused_at"] = time.strftime("%H:%M:%S")
    save()
    print(json.dumps({"unpaused": journal["unpaused_at"]}), flush=True)

    time.sleep(120)
    last_reply = max(t["time"] for t in turns)
    # Every text Stella handed to the sender after the unpause; a late plugin reply would
    # show up here as plugin output (dice/echo/nickname text) without a new request.
    sent = [line.split("[即将发送给 QQ 的台词]: ", 1)[1]
            for clock, line in qq_round.lines(args.stella_log)
            if clock >= journal["unpaused_at"] and "[即将发送给 QQ 的台词]: " in line]
    journal["texts_sent_after_unpause"] = sent
    journal["sender_turns_after_unpause"] = qq_round.collect(args.stella_log, journal["unpaused_at"], args.bot_id, args.sender)
    journal["last_paused_reply"] = last_reply
    journal["containers_after"] = containers(args.owner)
    save()
    print(json.dumps({"texts_sent_after_unpause": len(sent), "containers_after": journal["containers_after"]},
                     ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
