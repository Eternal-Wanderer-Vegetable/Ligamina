#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"
cd "$repo_root"

run_id="${GITHUB_RUN_ID:?GITHUB_RUN_ID is required}"
mode="${P4_P403_MODE:-linux_unknown_recovery}"
manifest="outputs/acceptance/phase4-p403-recovery-manifest-20261027.json"
executor_manifest="outputs/acceptance/phase4-p403-linux-execution-manifest-20261027.json"
validation_report="outputs/acceptance/phase4-p403-validation-run-${run_id}.json"
executor_validation="outputs/acceptance/phase4-p403-executor-validation-run-${run_id}.json"
worker_report="outputs/acceptance/phase4-p403-linux-workers-run-${run_id}.json"
admission_report="outputs/acceptance/phase4-p403-linux-admission-run-${run_id}.json"
recovery_report="outputs/acceptance/phase4-p403-linux-recovery-run-${run_id}.json"
controlled_report="outputs/acceptance/phase4-p403-linux-controlled-run-${run_id}.json"
self_test_report="outputs/acceptance/phase4-p403-runner-self-test-run-${run_id}.json"
log="outputs/acceptance/phase4-p403-linux-controlled-run-${run_id}.log"
evidence_root="${RUNNER_TEMP:?RUNNER_TEMP is required}/Ligamina-p403-evidence-${run_id}"
queue_root="${RUNNER_TEMP}/Ligamina-p403-queue-${run_id}"
cache_root="${repo_root}/../Ligamina-artifact-cache"

mkdir -p outputs/acceptance "$evidence_root" "$queue_root"
printf '{"schema_version":1,"kind":"phase4_p403_controlled_run","mode":"%s","status":"in-progress","windows_tests_run":false,"plugin_execution":"not_started"}\n' "$mode" > "$controlled_report"

finish() {
  local exit_code=$?
  python3 - "$controlled_report" "$log" "$exit_code" "$validation_report" "$executor_validation" "$worker_report" "$admission_report" "$recovery_report" "$executor_manifest" "$self_test_report" <<'PY'
import hashlib
import json
import os
import pathlib
import sys

report_path, log_path, code_text, *evidence_paths = sys.argv[1:]
report_file = pathlib.Path(report_path)
report = json.loads(report_file.read_text(encoding="utf-8"))
code = int(code_text)
if code == 0:
    status = "completed"
elif code in {130, 143}:
    status = "interrupted"
elif code == 3:
    status = "blocked"
else:
    status = "failed"
report.update({
    "status": status,
    "exit_code": code,
    "repository": os.environ.get("GITHUB_REPOSITORY"),
    "commit_sha": os.environ.get("GITHUB_SHA"),
    "ref": os.environ.get("GITHUB_REF"),
    "event": os.environ.get("GITHUB_EVENT_NAME"),
    "run_id": os.environ.get("GITHUB_RUN_ID"),
    "runner_name": os.environ.get("RUNNER_NAME"),
    "windows_tests_run": False,
    "log_sha256": hashlib.sha256(pathlib.Path(log_path).read_bytes()).hexdigest() if pathlib.Path(log_path).is_file() else None,
    "evidence_sha256": {
        path: hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()
        if pathlib.Path(path).is_file() else None
        for path in evidence_paths
    },
})
report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
  return "$exit_code"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

run_step() {
  local label="$1"
  shift
  printf '\n=== %s ===\n' "$label" | tee -a "$log"
  "$@" 2>&1 | tee -a "$log"
}

: > "$log"
case "$mode" in
  runner_contract_success|runner_contract_failure|runner_contract_cancel)
    run_step runner_contract_self_test bash scripts/phase4-p403-runner-self-test.sh "$mode" "$self_test_report"
    exit 0
    ;;
  linux_unknown_recovery) ;;
  *) printf 'unsupported P4-03 mode: %s\n' "$mode" | tee -a "$log" >&2; exit 2 ;;
esac

run_step validate_p403_parent uv run --locked --project lab --python 3.12.14 python -m lab.automation \
  validate-p403-recovery "$manifest" --output "$validation_report"
run_step validate_p403_executor uv run --locked --project lab --python 3.12.14 python -m lab.automation \
  validate-p403-linux-executor --manifest "$manifest" --executor-manifest "$executor_manifest" --output "$executor_validation"
run_step linux_worker_probes uv run --locked --project hosts/nonebot --python 3.12.14 python \
  deploy/lab/linux/probes/acceptance.py --report "$worker_report"
run_step linux_worker_admission uv run --locked --project lab --python 3.12.14 python -m lab.automation \
  preflight --platform linux --probe-report "$worker_report" --output "$admission_report"
python3 - "$controlled_report" <<'PY'
import json
import pathlib
import sys
path = pathlib.Path(sys.argv[1])
report = json.loads(path.read_text(encoding="utf-8"))
report["plugin_execution"] = "dispatched"
path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
run_step p403_unknown_recovery uv run --locked --project lab --python 3.12.14 python -m lab.automation \
  execute-p403-linux-recovery --manifest "$manifest" --executor-manifest "$executor_manifest" \
  --admission "$admission_report" --cache-root "$cache_root" --queue-root "$queue_root" \
  --evidence-root "$evidence_root" --output "$recovery_report"
