"""Finalize P4-03 report files when GitHub cancels a runner step process tree."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any


_CANCEL_PROBE_MARKER = "P4-03 self-owned runner cancellation probe;"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object in {path}")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _canonical_report_sha256(report: dict[str, Any]) -> str:
    canonical = {key: value for key, value in report.items() if key != "report_sha256"}
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def reconcile_cancelled_p403_run(
    *,
    mode: str,
    job_status: str,
    controlled_report_path: Path,
    self_test_report_path: Path,
    outer_cleanup_report_path: Path,
    log_path: Path,
) -> dict[str, Any]:
    """Close interrupted report files after `if: always()` cleanup on cancellation.

    GitHub may terminate the step process tree before Bash EXIT traps can persist
    their reports. The finalizer runs after the external cancellation and records
    that boundary separately from any shell exit code observed by a trap.
    """
    if job_status.lower() != "cancelled":
        return {"status": "not_cancelled", "probe_expectation_met": None}

    controlled = _read_json(controlled_report_path)
    if controlled is None:
        controlled = {
            "schema_version": 1,
            "kind": "phase4_p403_controlled_run",
            "mode": mode,
            "status": "interrupted",
            "windows_tests_run": False,
            "plugin_execution": "not_started",
        }

    cleanup = _read_json(outer_cleanup_report_path)
    cleanup_confirmed = cleanup is not None and cleanup.get("cleanup_confirmed") is True
    cleanup_sha256 = (
        hashlib.sha256(outer_cleanup_report_path.read_bytes()).hexdigest()
        if outer_cleanup_report_path.is_file()
        else None
    )
    log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
    probe_started = _CANCEL_PROBE_MARKER in log_text
    log_sha256 = hashlib.sha256(log_path.read_bytes()).hexdigest() if log_path.is_file() else None

    if controlled.get("status") in {None, "in-progress", "queued", "pending"}:
        controlled["status"] = "interrupted"
        controlled["exit_code"] = None
        controlled["exit_code_observed"] = False
    controlled.update({
        "mode": mode,
        "external_cancellation_observed": True,
        "cancellation_source": "github_actions_workflow_cancel",
        "outer_cleanup_confirmed": cleanup_confirmed,
        "windows_tests_run": False,
        "log_sha256": log_sha256,
        "evidence_sha256": controlled.get("evidence_sha256", {}),
    })
    _write_json(controlled_report_path, controlled)

    expectation_met: bool | None = None
    if mode == "runner_contract_cancel":
        self_test = _read_json(self_test_report_path)
        shell_exit_code = self_test.get("exit_code") if self_test else None
        shell_exit_trap_reported = (
            self_test is not None
            and self_test.get("mode") == mode
            and self_test.get("status") == "interrupted"
            and shell_exit_code in {130, 143}
        )
        expectation_met = probe_started and cleanup_confirmed
        if self_test is None or self_test.get("mode") != mode:
            self_test = {
                "schema_version": 1,
                "kind": "phase4_p403_runner_contract_self_test",
                "mode": mode,
                "status": "interrupted",
                "exit_code": None,
                "expected_exit_code": None,
                "probe_expectation_met": expectation_met,
                "plugin_execution": "not_started",
                "worker_claimed": False,
                "cancellation_report_source": "github_actions_finalizer",
            }
        self_test.update({
            "status": "interrupted",
            "external_cancellation_observed": True,
            "probe_started": probe_started,
            "shell_exit_trap_reported": shell_exit_trap_reported,
            "outer_cleanup_confirmed": cleanup_confirmed,
            "outer_cleanup_report_sha256": cleanup_sha256,
            "probe_expectation_met": expectation_met,
        })
        self_test["report_sha256"] = _canonical_report_sha256(self_test)
        _write_json(self_test_report_path, self_test)
        controlled["evidence_sha256"][self_test_report_path.as_posix()] = hashlib.sha256(
            self_test_report_path.read_bytes()
        ).hexdigest()
        _write_json(controlled_report_path, controlled)

    return {
        "status": "interrupted",
        "probe_started": probe_started,
        "outer_cleanup_confirmed": cleanup_confirmed,
        "shell_exit_trap_reported": shell_exit_trap_reported if mode == "runner_contract_cancel" else None,
        "probe_expectation_met": expectation_met,
    }


def main() -> int:
    if len(sys.argv) != 7:
        print(
            "usage: python -m lab.automation.phase4_p403_reports "
            "<mode> <job-status> <controlled-report> <self-test-report> <outer-cleanup-report> <log>",
            file=sys.stderr,
        )
        return 2
    mode, job_status, controlled, self_test, cleanup, log = sys.argv[1:]
    result = reconcile_cancelled_p403_run(
        mode=mode,
        job_status=job_status,
        controlled_report_path=Path(controlled),
        self_test_report_path=Path(self_test),
        outer_cleanup_report_path=Path(cleanup),
        log_path=Path(log),
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
