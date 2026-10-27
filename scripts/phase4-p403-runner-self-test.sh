#!/usr/bin/env bash
set -Eeuo pipefail

mode="${1:?mode is required}"
report="${2:?report path is required}"
mkdir -p "$(dirname -- "$report")"

case "$mode" in
  runner_contract_success|runner_contract_failure|runner_contract_cancel) ;;
  *) printf 'unsupported P4-03 runner self-test mode: %s\n' "$mode" >&2; exit 2 ;;
esac

finish() {
  local exit_code=$?
  python3 - "$report" "$mode" "$exit_code" <<'PY'
import hashlib
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
mode = sys.argv[2]
code = int(sys.argv[3])
status = "passed" if code == 0 else "interrupted" if code in {130, 143} else "failed"
expected_exit = {"runner_contract_success": 0, "runner_contract_failure": 23}.get(mode)
expectation_met = code == expected_exit if expected_exit is not None else code in {130, 143}
report = {
    "schema_version": 1,
    "kind": "phase4_p403_runner_contract_self_test",
    "mode": mode,
    "status": status,
    "exit_code": code,
    "expected_exit_code": expected_exit,
    "probe_expectation_met": expectation_met,
    "plugin_execution": "not_started",
    "worker_claimed": False,
    "report_sha256": None,
}
report["report_sha256"] = hashlib.sha256(
    json.dumps({key: value for key, value in report.items() if key != "report_sha256"},
               sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()
path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps(report, ensure_ascii=False))
PY
  return "$exit_code"
}

trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

case "$mode" in
  runner_contract_success)
    printf '%s\n' 'P4-03 self-owned runner success probe; no plugin Worker is dispatched.'
    exit 0
    ;;
  runner_contract_failure)
    printf '%s\n' 'P4-03 self-owned runner failure probe; intentional exit 23; no plugin Worker is dispatched.' >&2
    exit 23
    ;;
  runner_contract_cancel)
    printf '%s\n' 'P4-03 self-owned runner cancellation probe; cancel this workflow run to exercise traps and always() cleanup.'
    while true; do sleep 1; done
    ;;
esac
