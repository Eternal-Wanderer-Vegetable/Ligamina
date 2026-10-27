#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"
cd "$repo_root"

run_id="${GITHUB_RUN_ID:?GITHUB_RUN_ID is required}"
manifest="outputs/acceptance/phase4-p403-recovery-manifest-20261027.json"
executor_manifest="outputs/acceptance/phase4-p403-linux-execution-manifest-20261027-v3.json"
if [[ "${P4_P403_MODE:-}" == "linux_unknown_recovery_remaining" ]]; then
  executor_manifest="outputs/acceptance/phase4-p403-linux-execution-manifest-20261027-v4-not-run.json"
fi
cleanup_report="outputs/acceptance/phase4-p403-docker-cleanup-run-${run_id}.json"
queue_root="${RUNNER_TEMP:?RUNNER_TEMP is required}/Ligamina-p403-queue-${run_id}"
evidence_root="${RUNNER_TEMP}/Ligamina-p403-evidence-${run_id}"
project_venv="${RUNNER_TEMP}/Ligamina-p403-project-venv-${run_id}"
uv_cache="${RUNNER_TEMP}/Ligamina-p403-uv-cache-${run_id}"
bundle="outputs/acceptance/phase4-p403-runner-state-run-${run_id}.tar.gz"
export UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-$project_venv}"
export UV_CACHE_DIR="${UV_CACHE_DIR:-$uv_cache}"

set +e
uv run --locked --project lab --python 3.12.14 python -m lab.automation \
  reconcile-p403-linux-runner --manifest "$manifest" --executor-manifest "$executor_manifest" \
  --queue-root "$queue_root" --output "$cleanup_report"
reconcile_exit=$?
set -e

bundle_status="not_required"
bundle_paths=()
[[ -d "$queue_root" ]] && bundle_paths+=("Ligamina-p403-queue-${run_id}")
[[ -d "$evidence_root" ]] && bundle_paths+=("Ligamina-p403-evidence-${run_id}")
if ((${#bundle_paths[@]})); then
  if tar -czf "$bundle" -C "$RUNNER_TEMP" "${bundle_paths[@]}" 2>&1; then
    bundle_status="created"
  else
    bundle_status="failed"
  fi
fi

python3 - "$RUNNER_TEMP" "$run_id" "$bundle" "$cleanup_report" "$reconcile_exit" "$bundle_status" "$project_venv" "$uv_cache" <<'PY'
import hashlib
import json
import pathlib
import shutil
import sys

runner_temp_text, run_id, bundle_text, cleanup_report_text, reconcile_code, bundle_status, project_venv_text, uv_cache_text = sys.argv[1:]
runner_temp = pathlib.Path(runner_temp_text).resolve(strict=True)
bundle = pathlib.Path(bundle_text).resolve(strict=False)
cleanup_report = pathlib.Path(cleanup_report_text)
docker_cleanup = json.loads(cleanup_report.read_text(encoding="utf-8")) if cleanup_report.is_file() else None
bundle_sha = hashlib.sha256(bundle.read_bytes()).hexdigest() if bundle.is_file() else None
removed = {}
safe = True
for name in (
    f"Ligamina-p403-queue-{run_id}",
    f"Ligamina-p403-evidence-{run_id}",
    f"Ligamina-p403-project-venv-{run_id}",
    f"Ligamina-p403-uv-cache-{run_id}",
):
    target = runner_temp / name
    try:
        if target.is_symlink() or target.resolve(strict=False).parent != runner_temp or target.name != name:
            safe = False
            removed[name] = "refused_outside_runner_temp_or_symlink"
            continue
        if target.exists():
            needs_bundle = name in {
                f"Ligamina-p403-queue-{run_id}",
                f"Ligamina-p403-evidence-{run_id}",
            }
            if needs_bundle and bundle_status != "created":
                safe = False
                removed[name] = "preserved_bundle_missing"
                continue
            shutil.rmtree(target)
        removed[name] = "removed" if not target.exists() else "removal_failed"
        safe = safe and not target.exists()
    except OSError as error:
        safe = False
        removed[name] = f"cleanup_error:{type(error).__name__}"
bundle_ready = bundle_status == "not_required" or (bundle_status == "created" and bundle_sha is not None)
cleanup_confirmed = (
    safe and bundle_ready
    and (docker_cleanup is None or docker_cleanup.get("status") in {"confirmed", "not_started"})
    and int(reconcile_code) == 0
)
report = {
    "schema_version": 1,
    "kind": "phase4_p403_runner_outer_cleanup",
    "run_id": run_id,
    "status": "confirmed" if cleanup_confirmed else "unconfirmed",
    "cleanup_confirmed": cleanup_confirmed,
    "docker_cleanup_report": cleanup_report.as_posix() if cleanup_report.is_file() else None,
    "docker_cleanup_report_sha256": hashlib.sha256(cleanup_report.read_bytes()).hexdigest() if cleanup_report.is_file() else None,
    "docker_cleanup_status": docker_cleanup.get("status") if docker_cleanup else None,
    "runner_temp_bundle": bundle.as_posix() if bundle.is_file() else None,
    "runner_temp_bundle_sha256": bundle_sha,
    "runner_temp_bundle_status": bundle_status,
    "project_venv_path": project_venv_text,
    "uv_cache_path": uv_cache_text,
    "removed_paths": removed,
}
out = pathlib.Path("outputs/acceptance") / f"phase4-p403-outer-cleanup-run-{run_id}.json"
out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
out.with_suffix(out.suffix + ".sha256").write_text(
    f"{hashlib.sha256(out.read_bytes()).hexdigest()}  {out.name}\n", encoding="ascii"
)
print(json.dumps(report, ensure_ascii=False))
PY

if [[ "${P4_P403_JOB_STATUS:-}" == "cancelled" ]]; then
  python3 -m lab.automation.phase4_p403_reports \
    "${P4_P403_MODE:-unknown}" "$P4_P403_JOB_STATUS" \
    "outputs/acceptance/phase4-p403-linux-controlled-run-${run_id}.json" \
    "outputs/acceptance/phase4-p403-runner-self-test-run-${run_id}.json" \
    "outputs/acceptance/phase4-p403-outer-cleanup-run-${run_id}.json" \
    "outputs/acceptance/phase4-p403-linux-controlled-run-${run_id}.log"
fi

python3 - "$repo_root/outputs/acceptance/phase4-p403-outer-cleanup-run-${run_id}.json" <<'PY'
import json
import pathlib
import sys
report = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
raise SystemExit(0 if report.get("cleanup_confirmed") is True else 3)
PY
