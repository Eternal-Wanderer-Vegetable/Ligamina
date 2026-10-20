#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"
cd "$repo_root"

run_id="${GITHUB_RUN_ID:-local-$(date -u +%Y%m%dT%H%M%SZ)}"
run_id="${run_id//[^A-Za-z0-9_.-]/_}"
report="outputs/acceptance/phase3-p309-linux-self-test-${run_id}.json"
log="outputs/acceptance/phase3-p309-linux-self-test-${run_id}.log"
mkdir -p outputs/acceptance
export LIGAMINA_P3_SELF_TEST_REPORT="$report"
export LIGAMINA_P3_SELF_TEST_LOG="$log"
printf '{"schema_version":1,"kind":"phase3_linux_self_test","status":"in-progress","windows_tests_run":false,"plugin_execution":false}\n' > "$report"

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
    "status": "passed" if code == 0 else "failed",
    "exit_code": code,
    "repository": os.environ.get("GITHUB_REPOSITORY"),
    "commit_sha": os.environ.get("GITHUB_SHA"),
    "ref": os.environ.get("GITHUB_REF"),
    "event": os.environ.get("GITHUB_EVENT_NAME", "local"),
    "run_id": os.environ.get("GITHUB_RUN_ID"),
    "windows_tests_run": False,
    "plugin_execution": False,
    "log_sha256": hashlib.sha256(pathlib.Path(log_path).read_bytes()).hexdigest() if pathlib.Path(log_path).is_file() else None,
})
pathlib.Path(report_path).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
  return "$exit_code"
}
trap finish EXIT

{
  set -o pipefail
  node --test scripts/evidence-hash.test.mjs scripts/phase3-workflows.test.mjs
  node scripts/check-docs.mjs
  uv python install 3.12.14
  uv run --locked --project lab --python 3.12.14 python -m unittest \
    lab.tests.test_automation_execution \
    lab.tests.test_automation_recovery \
    lab.tests.test_automation_selection \
    lab.tests.test_automation_health \
    lab.tests.test_queue \
    lab.tests.test_automation_dispatcher \
    lab.tests.test_native_runner \
    lab.tests.test_compare \
    lab.tests.test_baselines.StockBaselineSourceTests.test_both_native_host_profiles_are_present \
    lab.tests.test_baselines.StockBaselineSourceTests.test_linux_profile_cleanup_removes_host_owned_staging_without_docker \
    lab.tests.test_baselines.StockBaselineSourceTests.test_native_baseline_sources_do_not_import_ligamina_bridge \
    lab.tests.test_baselines.StockBaselineSourceTests.test_recipe_helpers_import_without_lab_schema_dependencies \
    lab.tests.test_baselines.StockBaselineSourceTests.test_registry_proxy_is_scoped_to_host_resolver_environment \
    lab.tests.test_baselines.StockBaselineSourceTests.test_s205_worker_closure_includes_recipe_helpers -v
  uv run --locked --project lab --python 3.12.14 python -m lab.automation validate-behavior
} 2>&1 | tee "$log"
