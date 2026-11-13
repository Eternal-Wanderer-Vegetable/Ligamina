#!/usr/bin/env bash
# P7-09 AstrBot campaign supervisor: bounded batches with fresh exact-image probe/admission,
# followed by an anomaly gate. Any gate anomaly or executor failure writes a shared halt file,
# so every slot stops before its next batch; terminal queue rows are never rewritten.
#
#   P709_IMAGE=... P709_PLAN=... P709_QUEUE=... P709_BUNDLE=... \
#     bash scripts/p7-p709-campaign-supervisor.sh SLOT
#
# Optional: P709_MAX_JOBS (default 100), P709_HALT (default: "$P709_QUEUE.halt").
set -euo pipefail
if [[ $# -ne 1 || ! "$1" =~ ^[1-9]$ ]]; then
  echo "usage: p7-p709-campaign-supervisor.sh SLOT" >&2
  exit 2
fi
SLOT="$1"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${P709_IMAGE:?}" "${P709_PLAN:?}" "${P709_QUEUE:?}" "${P709_BUNDLE:?}"
MAX_JOBS="${P709_MAX_JOBS:-100}"
HALT="${P709_HALT:-$P709_QUEUE.halt}"
CENSUS=outputs/acceptance/p7-p709-astrbot-full-census-20261112.json
SNAPSHOT=catalogs/ab-15937e90d26b7caf9d12
RUNTIME=/mnt/e/stella/Ligamina-artifact-cache/p7-p703-runtime-20261110.tar.gz
CACHE=/mnt/e/stella/Ligamina-artifact-cache
cd "$ROOT"
LOG="$ROOT/outputs/acceptance/p7-p709-campaign-supervisor-slot-$SLOT-$(date -u +%Y%m%dT%H%M%SZ)-pid-$$.log"
exec >> "$LOG" 2>&1
halt() {
  echo "halt slot=$SLOT reason=$1 utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf '%s slot=%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$SLOT" "$1" >> "$HALT"
  exit 4
}
echo "supervisor_start slot=$SLOT pid=$$ max_jobs=$MAX_JOBS image=$P709_IMAGE plan=$P709_PLAN utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
BATCH=0
while true; do
  if [[ -e "$HALT" ]]; then
    echo "halt_present slot=$SLOT file=$HALT"
    exit 4
  fi
  STATE="$(P709_QUEUE="$P709_QUEUE" python3 -c 'import os,sqlite3,time; c=sqlite3.connect("file:"+os.environ["P709_QUEUE"]+"?mode=ro",uri=True); s=dict(c.execute("select status,count(*) from jobs group by status").fetchall()); e=c.execute("select min(lease_expires_at) from jobs where status=?",("leased",)).fetchone()[0]; print(s.get("queued",0),s.get("leased",0),int(e or 0),int(time.time()))')"
  read -r QUEUED LEASED MIN_EXPIRY NOW <<< "$STATE"
  echo "queue slot=$SLOT queued=$QUEUED leased=$LEASED utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  if (( QUEUED == 0 && LEASED == 0 )); then
    echo "supervisor_complete slot=$SLOT utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    break
  fi
  JOBS="$MAX_JOBS"
  if (( QUEUED == 0 )); then
    if [[ "$SLOT" != "1" ]] || (( MIN_EXPIRY > NOW )); then
      sleep 30
      continue
    fi
    JOBS=1
    echo "reconcile_expired_lease slot=$SLOT"
  fi
  BATCH=$((BATCH + 1))
  RUN="$(date -u +%Y%m%dT%H%M%SZ)-slot-$SLOT-pid-$$-b$(printf '%04d' "$BATCH")"
  PROBE="outputs/acceptance/p7-p709-linux-worker-probe-campaign-$RUN.json"
  ADMISSION="outputs/acceptance/p7-p709-linux-admission-campaign-$RUN.json"
  REPORT="outputs/acceptance/p7-p709-astrbot-campaign-execution-batch-$RUN.json"
  uv run --locked --project hosts/nonebot --python 3.12.14 python deploy/lab/linux/probes/acceptance.py \
    --worker-image "$P709_IMAGE" --report "$PROBE" || halt "probe_failed run=$RUN"
  sha256sum "$PROBE" > "$PROBE.sha256"
  uv run --locked --project lab --python 3.12.14 python -m lab.automation preflight --platform linux \
    --worker-image "$P709_IMAGE" --probe-report "$PROBE" --output "$ADMISSION" || halt "admission_failed run=$RUN"
  sha256sum "$ADMISSION" > "$ADMISSION.sha256"
  uv run --locked --project lab --python 3.12.14 python -m lab.astrbot_campaign_execute --plan "$P709_PLAN" \
    --census "$CENSUS" --snapshot "$SNAPSHOT" --admission "$ADMISSION" --runtime-archive "$RUNTIME" \
    --cache-root "$CACHE" --bundle-root "$P709_BUNDLE" --queue-db "$P709_QUEUE" --output "$REPORT" \
    --max-jobs "$JOBS" --worker-id "p7-p709-$RUN" || halt "executor_failed run=$RUN"
  echo "batch_complete slot=$SLOT run=$RUN max_jobs=$JOBS report=$REPORT utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  python3 scripts/p7-p709-campaign-gate.py --report "$REPORT" || halt "gate_anomaly run=$RUN"
  echo "gate_passed slot=$SLOT run=$RUN"
done
