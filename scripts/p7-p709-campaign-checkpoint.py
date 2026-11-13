#!/usr/bin/env python3
"""Build a cumulative P7-09 AstrBot campaign checkpoint.

Stdlib only; run on the Linux side where the external StageQueue lives:

    python3 scripts/p7-p709-campaign-checkpoint.py \
        --previous outputs/acceptance/<previous checkpoint>.json \
        --add-report outputs/acceptance/<batch report>.json [...] \
        --output outputs/acceptance/p7-p709-astrbot-campaign-checkpoint-<UTC>.json

The queue database is opened read-only. Every previously recorded batch entry
is re-verified against its sidecar and recomputed from its report before new
reports are appended; any mismatch aborts without writing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

TERMINAL = {"passed", "failed", "unknown", "skipped"}
RUN_RE = re.compile(r"p7-p709-astrbot-campaign-execution-batch-(?P<run>.+)\.json$")


class CheckpointError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verified_sha(path: Path) -> str:
    digest = sha256_file(path)
    sidecar = path.with_name(path.name + ".sha256")
    if not sidecar.is_file():
        raise CheckpointError(f"missing sidecar: {sidecar}")
    recorded = sidecar.read_text(encoding="utf-8").split()[0]
    if recorded != digest:
        raise CheckpointError(f"sidecar mismatch: {path} {digest} != {recorded}")
    return digest


def batch_entry(root: Path, rel: str, run_id: str) -> tuple[dict, list[dict]]:
    path = root / rel
    match = RUN_RE.search(path.name)
    if not match:
        raise CheckpointError(f"not a campaign batch report: {rel}")
    run = match.group("run")
    report = json.loads(path.read_text(encoding="utf-8"))
    if report.get("kind") != "p7_p709_astrbot_campaign_execution_checkpoint":
        raise CheckpointError(f"unexpected report kind: {rel}")
    if report.get("run_id") != run_id:
        raise CheckpointError(f"report belongs to run {report.get('run_id')}, not {run_id}: {rel}")
    calls = report["calls"]
    comparison = Counter(c["comparison"] for c in calls if c.get("comparison"))
    c7 = Counter(c["c7_status"] for c in calls if c.get("c7_status"))
    inputs = []
    for kind in ("linux-worker-probe", "linux-admission"):
        input_rel = f"outputs/acceptance/p7-p709-{kind}-campaign-{run}.json"
        inputs.append({"path": input_rel, "sha256": verified_sha(root / input_rel)})
    gate_rel = rel.replace("-execution-batch-", "-batch-gate-")
    if (root / gate_rel).is_file():
        gate = json.loads((root / gate_rel).read_text(encoding="utf-8"))
        if gate.get("report", {}).get("sha256") != sha256_file(path):
            raise CheckpointError(f"gate record is bound to different report bytes: {gate_rel}")
        inputs.append({"path": gate_rel, "sha256": verified_sha(root / gate_rel), "gate_status": gate.get("status")})
    regate_rel = rel.replace("-execution-batch-", "-batch-regate-")
    if (root / regate_rel).is_file():
        regate = json.loads((root / regate_rel).read_text(encoding="utf-8"))
        if regate.get("report", {}).get("sha256") != sha256_file(path):
            raise CheckpointError(f"gate re-evaluation is bound to different report bytes: {regate_rel}")
        inputs.append({"path": regate_rel, "sha256": verified_sha(root / regate_rel), "gate_status": regate.get("status")})
    entry = {
        "path": rel,
        "sha256": verified_sha(path),
        "calls": len(calls),
        "comparison": dict(sorted(comparison.items())),
        "C7": dict(sorted(c7.items())),
        "queue_counts_at_report": report["queue_counts"],
        "inputs": inputs,
    }
    return entry, calls


def same_core(old: dict, new: dict) -> bool:
    keys = ("path", "sha256", "calls", "comparison", "C7")
    if any(old.get(k) != new.get(k) for k in keys) or any(item not in new["inputs"] for item in old["inputs"]):
        return False
    return "queue_counts_at_report" not in old or old["queue_counts_at_report"] == new["queue_counts_at_report"]


def queue_snapshot(db: str, run_id: str) -> tuple[dict, dict]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        con.execute("BEGIN")
        quick = con.execute("PRAGMA quick_check").fetchone()[0]
        rows = {
            r[0]: {"job_id": r[1], "status": r[2], "attempts_used": r[3], "case_id": r[4],
                   "registry_entry_id": r[5], "lease_owner": r[6], "result": json.loads(r[7]) if r[7] else None}
            for r in con.execute(
                "SELECT unit_id, job_id, status, attempts_used, case_id, registry_entry_id, lease_owner, result_json FROM jobs WHERE run_id = ?", (run_id,)
            )
        }
        leases = [
            {"attempt_id": r[0], "attempts_used": r[1], "case_id": r[2], "unit_id": r[3], "worker_id": r[4]}
            for r in con.execute(
                "SELECT a.attempt_id, j.attempts_used, j.case_id, j.unit_id, a.worker_id FROM jobs j "
                "JOIN attempts a ON a.job_id = j.job_id AND a.attempt_number = j.attempts_used "
                "WHERE j.status = 'leased' AND j.run_id = ? ORDER BY j.unit_id", (run_id,)
            )
        ]
    finally:
        con.close()
    counts = Counter(r["status"] for r in rows.values())
    ordered = {k: counts.get(k, 0) for k in ("queued", "leased", "passed", "failed", "skipped", "unknown")}
    ordered["total"] = len(rows)
    if sum(counts.values()) != len(rows):
        raise CheckpointError("queue counts do not conserve rows")
    unknown_reasons = Counter(
        (r["result"] or {}).get("reason_code") or "unspecified" for r in rows.values() if r["status"] == "unknown"
    )
    cleanup_unconfirmed = 0
    pre_dispatch = []
    for unit_id, r in sorted(rows.items()):
        if r["status"] not in TERMINAL:
            continue
        result = r["result"] or {}
        runs = result.get("worker_runs") or []
        if runs and not all(w.get("cleanup_confirmed") is True for w in runs):
            cleanup_unconfirmed += 1
        if not runs and r["status"] == "failed":
            pre_dispatch.append({
                "job_id": r["job_id"], "unit_id": unit_id, "case_id": r["case_id"],
                "registry_entry_id": r["registry_entry_id"], "attempts_used": r["attempts_used"],
                "diagnostic_type": result.get("diagnostic_type"), "reason_code": result.get("reason_code"),
                "worker_runs": 0,
            })
    queue = {
        "path": db,
        "counts": ordered,
        "conserved_total": len(rows),
        "attempts_used_sum": sum(r["attempts_used"] for r in rows.values()),
        "quick_check": quick,
        "unknown_reasons": dict(unknown_reasons.most_common()),
        "terminal_worker_cleanup_unconfirmed": cleanup_unconfirmed,
        "active_leases": leases,
        "terminal_pre_dispatch_failures": pre_dispatch,
    }
    return queue, rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--previous", required=True,
                        help="previous checkpoint; with --new-run-plan it is only the field template")
    parser.add_argument("--new-run-plan", help="start a new checkpoint chain for this campaign plan")
    parser.add_argument("--queue-db", help="external StageQueue of the new run (required with --new-run-plan)")
    parser.add_argument("--add-report", action="append", default=[])
    parser.add_argument("--output", required=True)
    parser.add_argument("--superseded-by", help="close this run: plan path of the run that replaces it")
    parser.add_argument("--superseded-reason", default="")
    args = parser.parse_args()
    root = args.root.resolve()
    output = root / args.output
    if output.exists() or output.with_name(output.name + ".sha256").exists():
        raise CheckpointError(f"refusing to overwrite {args.output}")

    previous = json.loads((root / args.previous).read_text(encoding="utf-8"))
    verified_sha(root / args.previous)
    if args.new_run_plan:
        if not args.queue_db:
            raise CheckpointError("--new-run-plan requires --queue-db")
        plan = json.loads((root / args.new_run_plan).read_text(encoding="utf-8"))
        previous = {**previous, "run_id": plan["run_id"], "plan_sha256": verified_sha(root / args.new_run_plan),
                    "plan_path": args.new_run_plan, "census_sha256": plan["inputs"]["census_report_sha256"],
                    "worker_image": plan["inputs"]["worker_image"], "completed_bounded_batches": [],
                    "queue": {"path": args.queue_db},
                    "next": "Continue the same queue with fresh exact-image probe/admission per bounded batch; "
                            "halt on any batch gate anomaly and rerun affected jobs only under a corrected plan."}
        previous.pop("superseded", None)
    run_id = previous["run_id"]
    batches: list[dict] = []
    all_calls: list[dict] = []
    seen = set()
    for old in previous["completed_bounded_batches"]:
        entry, calls = batch_entry(root, old["path"], run_id)
        if not same_core(old, entry):
            raise CheckpointError(f"previous entry no longer reproduces: {old['path']}")
        batches.append(old)
        all_calls.extend(calls)
        seen.add(old["path"])
    for rel in args.add_report:
        if rel in seen:
            raise CheckpointError(f"report already recorded: {rel}")
        entry, calls = batch_entry(root, rel, run_id)
        batches.append(entry)
        all_calls.extend(calls)
        seen.add(rel)

    queue, rows = queue_snapshot(previous["queue"]["path"], run_id)
    appearances = Counter(c["unit_id"] for c in all_calls)
    # A retryable attempt may be claimed again; each report call must map to its own recorded attempt.
    over = [u for u, n in appearances.items() if n > 1 and (u not in rows or rows[u]["attempts_used"] < n)]
    if over:
        raise CheckpointError(f"unit IDs reported more often than their queue attempts: {over[:3]}")
    unit_ids = sorted(appearances)
    matched = [rows[u] for u in unit_ids if u in rows and rows[u]["status"] in TERMINAL]
    if len(matched) != len(unit_ids):
        raise CheckpointError(f"{len(unit_ids) - len(matched)} reported unit IDs lack a terminal queue row")
    dispatched = [(r["result"] or {}).get("worker_runs") or [] for r in matched]
    dispatched = [runs for runs in dispatched if runs]
    rows_cleanup = sum(1 for runs in dispatched if all(w.get("cleanup_confirmed") is True for w in runs))
    runs_cleanup = sum(1 for runs in dispatched for w in runs if w.get("cleanup_confirmed") is True)
    if rows_cleanup != len(dispatched):
        raise CheckpointError("reported dispatched rows with unconfirmed cleanup")
    pre_dispatch_units = {p["unit_id"] for p in queue["terminal_pre_dispatch_failures"]}

    comparison = Counter(c["comparison"] for c in all_calls if c.get("comparison"))
    c7 = Counter(c["c7_status"] for c in all_calls if c.get("c7_status"))
    aggregate = {
        "calls": len(all_calls),
        "comparison": {k: comparison.get(k, 0) for k in ("equivalent", "non_equivalent", "nondeterministic", "unknown")},
        "C7": {k: c7.get(k, 0) for k in ("passed", "failed")},
        "all_reported_calls_finished": all(c["status"] == "finished" for c in all_calls),
        "matched_queue_rows": len(matched),
        "repeated_attempt_calls": sum(n - 1 for n in appearances.values()),
        # Terminal rows whose batch report was never written (for example an interrupted executor).
        "terminal_rows_without_report": sum(
            1 for u, r in rows.items() if r["status"] in TERMINAL and u not in appearances),
        "pre_dispatch_failures": sum(1 for u in unit_ids if u in pre_dispatch_units),
        "reported_rows_cleanup_confirmed": rows_cleanup,
        "reported_worker_runs_cleanup_confirmed": runs_cleanup,
    }
    cmp_ = aggregate["comparison"]
    boundary = (
        f"This checkpoint accounts for {aggregate['calls']} finished command-job calls from {len(batches)} "
        f"sidecar-verified bounded reports ({len(unit_ids)} unique jobs). {sum(cmp_.values())} calls have Native×2/Federated comparisons "
        f"({cmp_['equivalent']} equivalent, {cmp_['non_equivalent']} non-equivalent, {cmp_['nondeterministic']} "
        f"nondeterministic, {cmp_['unknown']} unknown). Every report unit ID matches a terminal StageQueue row, "
        f"and all {runs_cleanup} recorded Worker runs have confirmed cleanup. "
        + ("One pre-dispatch worker_spec_invalid failure is kept separate. " if aggregate["pre_dispatch_failures"] == 1
           else f"{aggregate['pre_dispatch_failures']} pre-dispatch failures are kept separate. ")
        + f"The campaign denominator remains fixed at {queue['conserved_total']:,}; this is execution evidence only, "
        "not plugin correctness or support."
    )
    checkpoint = dict(previous)
    checkpoint.update({
        "observed_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": "running" if queue["counts"]["queued"] or queue["counts"]["leased"] else "queue_terminal",
        "completed_bounded_batches": batches,
        "completed_report_aggregate": aggregate,
        "queue": queue,
        "acceptance_boundary": boundary,
        "pre_dispatch_failures": [
            {"unit_id": p["unit_id"], "reason_code": p["reason_code"]} for p in queue["terminal_pre_dispatch_failures"]
        ],
    })
    if args.superseded_by:
        checkpoint["status"] = "superseded"
        checkpoint["superseded"] = {"by_plan": args.superseded_by, "by_plan_sha256": verified_sha(root / args.superseded_by),
                                    "reason": args.superseded_reason}
        checkpoint["next"] = "Closed. Do not resume this queue; results are retained as history only."
    text = json.dumps(checkpoint, ensure_ascii=True, indent=2) + "\n"
    output.write_bytes(text.encode("utf-8"))
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    output.with_name(output.name + ".sha256").write_text(f"{digest}  {output.name}\n", encoding="utf-8")
    print(json.dumps({"output": args.output, "sha256": digest, "reports": len(batches),
                      "aggregate": aggregate, "queue_counts": queue["counts"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except CheckpointError as exc:
        print(f"checkpoint error: {exc}", file=sys.stderr)
        sys.exit(1)
