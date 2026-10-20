#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"
cd "$repo_root"

run_id="${GITHUB_RUN_ID:?GITHUB_RUN_ID is required}"
report="outputs/acceptance/phase3-p309-linux-controlled-${run_id}.json"
log="outputs/acceptance/phase3-p309-linux-controlled-${run_id}.log"
probe_report="outputs/acceptance/phase3-p309-linux-workers-${run_id}.json"
admission_report="outputs/acceptance/phase3-p309-linux-admission-${run_id}.json"
recovery_report="outputs/acceptance/phase3-p309-linux-recovery-${run_id}.json"
manifest="outputs/acceptance/phase3-p305-linux-pilot-rebuilt-worker-manifest-20261020.json"
evidence_root="${RUNNER_TEMP:?RUNNER_TEMP is required}/Ligamina-automation-evidence"
mkdir -p outputs/acceptance
printf '{"schema_version":1,"kind":"phase3_linux_controlled_run","status":"in-progress","windows_tests_run":false,"plugin_execution":"not_started"}\n' > "$report"

finish() {
  local exit_code=$?
  python3 - "$report" "$log" "$exit_code" <<'PY'
import hashlib
import json
import os
import pathlib
import sys

report_path, log_path, code_text = sys.argv[1:]
report = json.loads(pathlib.Path(report_path).read_text(encoding="utf-8"))
code = int(code_text)
report.update({
    "status": "completed" if code == 0 else "failed_or_interrupted",
    "exit_code": code,
    "repository": os.environ.get("GITHUB_REPOSITORY"),
    "commit_sha": os.environ.get("GITHUB_SHA"),
    "ref": os.environ.get("GITHUB_REF"),
    "event": os.environ.get("GITHUB_EVENT_NAME"),
    "run_id": os.environ.get("GITHUB_RUN_ID"),
    "windows_tests_run": False,
    "log_sha256": hashlib.sha256(pathlib.Path(log_path).read_bytes()).hexdigest() if pathlib.Path(log_path).is_file() else None,
})
pathlib.Path(report_path).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
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
run_step validate_manifest uv run --locked --project lab --python 3.12.14 python -m lab.automation \
  validate-recovery-pilot "$manifest"
run_step linux_worker_probes uv run --locked --project hosts/nonebot --python 3.12.14 python \
  deploy/lab/linux/probes/acceptance.py --report "$probe_report"
run_step linux_worker_admission uv run --locked --project lab --python 3.12.14 python -m lab.automation \
  preflight --platform linux --probe-report "$probe_report" --output "$admission_report"
run_step linux_scoped_recovery uv run --locked --project lab --python 3.12.14 python -m lab.automation \
  execute-recovery-pilot --manifest "$manifest" --admission "$admission_report" \
  --evidence-root "$evidence_root" --output "$recovery_report"
