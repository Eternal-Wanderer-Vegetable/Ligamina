#!/usr/bin/env python3
"""Anomaly gate for one bounded P7-09 AstrBot campaign batch (stdlib only).

Reads the batch report and the matching StageQueue rows read-only, writes a gate
record (JSON + sidecar) next to the report, and exits 4 when a harness-level
anomaly signature crosses its threshold. The supervisor halts the campaign on a
non-zero exit; terminal queue rows are never rewritten. After the cause is fixed,
the affected jobs are rerun under a corrected plan, which regenerates the same
unit IDs.

    python3 scripts/p7-p709-campaign-gate.py --report outputs/acceptance/<batch>.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

TERMINAL = {"passed", "failed", "unknown", "skipped"}
# Fractions of a batch's differential jobs. Each signature matched a defect that made
# results meaningless in the 2026-11-12 campaign; they are not plugin-quality thresholds.
DEFAULT_LIMITS = {
    "comparison_unknown": 0.25,
    "c7_command_not_listed": 0.05,
    "federated_all_calls_error": 0.20,
    "native_all_calls_silent": 0.20,
    # Jobs with a Worker run that timed out or failed. Such a side has no readable output, so it
    # is a per-job outcome (recorded unknown), not a harness signature, unless it becomes common.
    "worker_not_passed": 0.10,
}
SIDES = ("native_first", "native_second", "federated")


def _all(signals: dict | None, key: str) -> bool:
    return isinstance(signals, dict) and signals.get("calls", 0) > 0 and signals.get(key) == signals["calls"]


def evaluate(report: dict, rows: dict[str, dict], limits: dict[str, float]) -> dict:
    calls = report.get("calls") or []
    reasons: list[str] = []
    if report.get("status") not in {"running", "completed"}:
        reasons.append(f"report_status:{report.get('status')}")
    statuses = Counter(call.get("status") for call in calls)
    for status in set(statuses) - {"finished", "idle"}:
        reasons.append(f"call_status:{status}:{statuses[status]}")
    unit_ids = [call["unit_id"] for call in calls if call.get("unit_id")]
    missing = [unit for unit in unit_ids if rows.get(unit, {}).get("status") not in TERMINAL]
    if missing:
        reasons.append(f"non_terminal_queue_rows:{len(missing)}")

    counts = Counter()
    jobs = 0
    for unit in unit_ids:
        result = (rows.get(unit) or {}).get("result") or {}
        comparison = result.get("comparison")
        if not isinstance(comparison, dict):
            continue
        jobs += 1
        signals = comparison.get("call_signals") or {}
        native = signals.get("native_first")
        if comparison.get("category") == "unknown":
            counts["comparison_unknown"] += 1
        if ((result.get("contracts") or {}).get("C7") or {}).get("reason_code") == "mcp_command_not_listed":
            counts["c7_command_not_listed"] += 1
        if _all(signals.get("federated"), "errors") and isinstance(native, dict) and native.get("errors") == 0:
            counts["federated_all_calls_error"] += 1
        if _all(native, "silent"):
            counts["native_all_calls_silent"] += 1
        runs = result.get("worker_runs") or []
        if any(run.get("status") != "passed" for run in runs):
            counts["worker_not_passed"] += 1
        # Missing signals are a harness anomaly only when that side's Worker exited normally
        # (or no run is recorded): its output was readable but carried no tool calls.
        if any(not isinstance(signals.get(side), dict)
               and (index >= len(runs) or runs[index].get("status") == "passed")
               for index, side in enumerate(SIDES)):
            counts["call_signals_missing"] += 1
        if any(run.get("cleanup_confirmed") is not True for run in runs):
            counts["cleanup_unconfirmed"] += 1
    if counts["call_signals_missing"]:
        reasons.append(f"call_signals_missing:{counts['call_signals_missing']}")
    if counts["cleanup_unconfirmed"]:
        reasons.append(f"cleanup_unconfirmed:{counts['cleanup_unconfirmed']}")
    rates = {key: (counts[key] / jobs if jobs else 0.0) for key in limits}
    for key, limit in limits.items():
        if rates[key] > limit:
            reasons.append(f"{key}:{counts[key]}/{jobs}>{limit}")
    return {
        "differential_jobs": jobs,
        "counts": dict(sorted(counts.items())),
        "rates": {key: round(value, 4) for key, value in rates.items()},
        "limits": limits,
        "status": "anomaly" if reasons else "passed",
        "reasons": reasons,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--reevaluate", action="store_true",
                        help="write a separate -batch-regate- record; the original gate record is kept")
    for key, value in DEFAULT_LIMITS.items():
        parser.add_argument(f"--max-{key.replace('_', '-')}", type=float, default=value, dest=key)
    args = parser.parse_args()
    report_bytes = args.report.read_bytes()
    report = json.loads(report_bytes)
    limits = {key: getattr(args, key) for key in DEFAULT_LIMITS}
    con = sqlite3.connect(f"file:{report['queue_db']}?mode=ro", uri=True)
    try:
        rows = {
            unit: {"status": status, "result": json.loads(result) if result else None}
            for unit, status, result in con.execute(
                "SELECT unit_id, status, result_json FROM jobs WHERE run_id = ?", (report["run_id"],))
        }
    finally:
        con.close()
    marker = "-batch-regate-" if args.reevaluate else "-batch-gate-"
    output = args.report.with_name(args.report.name.replace("-execution-batch-", marker))
    gate_previous = None
    if args.reevaluate:
        previous = args.report.with_name(args.report.name.replace("-execution-batch-", "-batch-gate-"))
        gate_previous = {"path": previous.as_posix(), "sha256": hashlib.sha256(previous.read_bytes()).hexdigest()}
    if output == args.report or output.exists():
        raise SystemExit(f"refusing to overwrite gate record: {output}")
    gate = {
        "schema_version": 1,
        "kind": "p7_p709_astrbot_campaign_batch_gate",
        "observed_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "report": {"path": args.report.as_posix(), "sha256": hashlib.sha256(report_bytes).hexdigest()},
        "run_id": report["run_id"],
        **({"reevaluates": gate_previous} if gate_previous else {}),
        **evaluate(report, rows, limits),
        "acceptance_boundary": "A passed gate only means no harness anomaly signature crossed its limit; it is not plugin correctness or support.",
    }
    text = json.dumps(gate, ensure_ascii=False, indent=2) + "\n"
    output.write_text(text, encoding="utf-8")
    output.with_name(output.name + ".sha256").write_text(
        f"{hashlib.sha256(text.encode('utf-8')).hexdigest()}  {output.name}\n", encoding="ascii")
    print(json.dumps({"gate": output.as_posix(), "status": gate["status"], "reasons": gate["reasons"],
                      "rates": gate["rates"]}, ensure_ascii=False))
    return 4 if gate["status"] == "anomaly" else 0


if __name__ == "__main__":
    sys.exit(main())
