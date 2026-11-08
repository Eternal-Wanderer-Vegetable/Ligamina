"""P6-01 QQ rounds through Stella's MCP path: turn collection and the restart watcher.

    python lab/field/qq_round.py collect --stella-log <stdout.log> --since 17:30:00 --output <turns.json>
    python lab/field/qq_round.py restart-watch --stella-log <stdout.log> --core-config <daemon.json> --restarts 9

``collect`` reads Stella's log and returns one record per @-mention of the bot: the
message text, the capability route, the Comes outcome, the text handed to the sender
and the OneBot delivery receipt. QQ user and group identifiers are not recorded.

``restart-watch`` restarts the field Core after every delivered reply, so each next
message meets a fresh Core (terminated MCP session, new Host containers).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "target" / "debug" / ("ligamina" + "-cli.exe")
ANSI = re.compile(r"\x1b\[[0-9;]*m")
TIME = re.compile(r"^\d\d-\d\d (\d\d:\d\d:\d\d) ")
MENTION = re.compile(r"\[message\.group\.normal\]: Message (-?\d+) from \d+@\[群:\d+\] '(.*)'$")
ROUTE = re.compile(r"\[Router\] Route\((.*?)\)")
COMES = re.compile(r"\[Comes\] (\S+) → (\w+) \((\d+)ms, (\S+), (\d+) 次工具调用\)")
SEND = re.compile(r"\[即将发送给 QQ 的台词\]: (.*)$")
DELIVERY = re.compile(r"\[OutboundDelivery\] status=(\w+) .*accepted_parts=(\d+) failed_parts=(\d+) unknown_parts=(\d+) receipt_count=(\d+)")
REJECTED = re.compile(r"\[Delivery\] 投递计划被拒绝")
NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def lines(log: Path):
    for raw in log.read_text(encoding="utf-8", errors="replace").splitlines():
        line = ANSI.sub("", raw)
        match = TIME.match(line)
        if match:
            yield match.group(1), line


def collect(log: Path, since: str, bot_id: str) -> list[dict]:
    turns: list[dict] = []
    current: dict | None = None
    for clock, line in lines(log):
        if clock < since:
            continue
        mention = MENTION.search(line)
        if mention and f"[at:qq={bot_id}]" in mention.group(2):
            text = re.sub(r"\[at:qq=\d+\]", "", mention.group(2)).strip()
            current = {"time": clock, "message": text, "route": None, "comes": None,
                       "sent_text": None, "delivery": None, "delivery_plan_rejected": False}
            turns.append(current)
            continue
        if current is None:
            continue
        if (route := ROUTE.search(line)) and current["route"] is None:
            current["route"] = route.group(1)
        elif (comes := COMES.search(line)) and current["comes"] is None:
            current["comes"] = {"capability": comes.group(1), "status": comes.group(2), "ms": int(comes.group(3)),
                                "mode": comes.group(4), "tool_calls": int(comes.group(5))}
        elif (send := SEND.search(line)) and current["sent_text"] is None:
            current["sent_text"] = send.group(1)
        elif (delivery := DELIVERY.search(line)) and current["delivery"] is None and current["sent_text"] is not None:
            current["delivery"] = {"status": delivery.group(1), "accepted_parts": int(delivery.group(2)),
                                   "failed_parts": int(delivery.group(3)), "unknown_parts": int(delivery.group(4)),
                                   "receipt_count": int(delivery.group(5))}
        elif REJECTED.search(line):
            current["delivery_plan_rejected"] = True
    return turns


def outbound_after(log: Path, since: str) -> list[str]:
    """Every OutboundDelivery line at or after `since` (to catch late replies)."""
    return [line for clock, line in lines(log) if clock >= since and "[OutboundDelivery]" in line]


def start_core(config: Path) -> subprocess.Popen:
    work = config.parent
    stamp = time.strftime("%H%M%S")
    stdout_path = work / f"core.stdout.{stamp}.log"
    stdout = open(stdout_path, "w", encoding="utf-8")
    stderr = open(work / f"core.stderr.{stamp}.log", "w", encoding="utf-8")
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    process = subprocess.Popen([str(CLI), "daemon", "--config", str(config)], stdout=stdout, stderr=stderr,
                               stdin=subprocess.DEVNULL, creationflags=flags)
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        text = stdout_path.read_text(encoding="utf-8", errors="replace")
        if '"ready":true' in text.replace(" ", ""):
            (work / "core.pid").write_text(str(process.pid), encoding="ascii")
            return process
        if process.poll() is not None:
            break
        time.sleep(0.5)
    raise RuntimeError(f"daemon did not become ready (see {stdout_path})")


def stop_core(config: Path) -> None:
    data = json.loads(config.read_text(encoding="utf-8"))
    request = urllib.request.Request(f"http://{data['listen']}/v1/shutdown", method="POST",
                                     headers={"authorization": f"Bearer {data['admin_token']}"})
    try:
        NO_PROXY.open(request, timeout=10).read()
    except Exception:  # noqa: BLE001 - already stopped
        pass
    pid_file = config.parent / "core.pid"
    if pid_file.exists():
        pid = pid_file.read_text(encoding="ascii").strip()
        for _ in range(90):
            alive = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True).stdout
            if pid not in alive:
                return
            time.sleep(1)
        subprocess.run(["taskkill", "/T", "/F", "/PID", pid], capture_output=True)


def restart_watch(log: Path, config: Path, restarts: int, bot_id: str, journal: Path) -> None:
    since = time.strftime("%H:%M:%S")
    seen = 0
    events = []
    while seen < restarts:
        turns = collect(log, since, bot_id)
        done = [t for t in turns if t["delivery"] is not None]
        while len(done) > seen and seen < restarts:
            turn = done[seen]
            seen += 1
            began = time.monotonic()
            stop_core(config)
            start_core(config)
            event = {"after_turn": turn["time"], "message": turn["message"], "restart": seen,
                     "restart_seconds": round(time.monotonic() - began, 1), "at": time.strftime("%H:%M:%S")}
            events.append(event)
            journal.write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(event, ensure_ascii=False), flush=True)
        time.sleep(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    c = sub.add_parser("collect")
    c.add_argument("--stella-log", type=Path, required=True)
    c.add_argument("--since", required=True)
    c.add_argument("--bot-id", required=True)
    c.add_argument("--output", type=Path, required=True)
    w = sub.add_parser("restart-watch")
    w.add_argument("--stella-log", type=Path, required=True)
    w.add_argument("--core-config", type=Path, required=True)
    w.add_argument("--restarts", type=int, required=True)
    w.add_argument("--bot-id", required=True)
    w.add_argument("--journal", type=Path, required=True)
    s = sub.add_parser("start-core")
    s.add_argument("--core-config", type=Path, required=True)
    t = sub.add_parser("stop-core")
    t.add_argument("--core-config", type=Path, required=True)
    o = sub.add_parser("outbound-after")
    o.add_argument("--stella-log", type=Path, required=True)
    o.add_argument("--since", required=True)
    args = parser.parse_args()
    if args.action == "collect":
        turns = collect(args.stella_log, args.since, args.bot_id)
        args.output.write_text(json.dumps(turns, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        print(json.dumps({"turns": len(turns)}))
    elif args.action == "restart-watch":
        restart_watch(args.stella_log, args.core_config, args.restarts, args.bot_id, args.journal)
    elif args.action == "start-core":
        process = start_core(args.core_config)
        print(json.dumps({"pid": process.pid}))
    elif args.action == "stop-core":
        stop_core(args.core_config)
    elif args.action == "outbound-after":
        for line in outbound_after(args.stella_log, args.since):
            print(line)


if __name__ == "__main__":
    sys.exit(main())
