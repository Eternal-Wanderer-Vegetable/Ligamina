import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_ID = 'p2-41649744e057d61031d5'
EXPECTED_UNITS = 11_380
EXPECTED_SHARDS = 512
STAGES = ('prepare', 'install', 'native', 'federated', 'compare', 'cleanup')
STATUSES = ('passed', 'failed', 'skipped', 'unknown')
QUEUE = ROOT / '.work/catalog-census/phase2-s211-rerun.sqlite3'
CHECKPOINT = ROOT / 'outputs/acceptance/phase2-s211-process-exit-regression-20261014.json'
AUDIT = ROOT / 'outputs/acceptance/phase2-s212-audit-20261017.json'
MATRIX = ROOT / 'outputs/acceptance/phase2-s212-compatibility-matrix-20261017.md'
PATTERN = re.compile(r'phase2-s211-rerun-census-(linux|windows)-shard(\d+)of256-20261014\.json\Z')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


checkpoint = json.loads(CHECKPOINT.read_text(encoding='utf-8'))
state = checkpoint['full_rerun']
if checkpoint.get('task_id') != 'S2-11' or state.get('status') != 'completed':
    raise SystemExit('S2-11 full rerun is not marked completed in the validated checkpoint')
if state.get('completed_canonical_reports') != EXPECTED_SHARDS or state.get('completed_units') != EXPECTED_UNITS:
    raise SystemExit('S2-11 full rerun denominator is incomplete')
if state.get('remaining_shards') != 0 or state.get('remaining_units') != 0 or state.get('active_runs'):
    raise SystemExit('S2-11 full rerun still has remaining or active work')

conn = sqlite3.connect(f'file:{QUEUE.as_posix()}?mode=ro', uri=True, timeout=10)
conn.execute('BEGIN')
unit_counts = Counter()
cohort_counts = defaultdict(Counter)
reason_counts = Counter()
stage_counts = {stage: Counter() for stage in STAGES}
seen_shards = set()
seen_units = set()
platform_shards = {'linux': set(), 'windows': set()}
verified_jobs = 0
for item in state['completed_reports']:
    match = PATTERN.fullmatch(Path(item['path']).name)
    if not match or match.group(1) != item['platform'] or int(match.group(2)) != item['index']:
        raise SystemExit(f"invalid S2-11 report identity: {item['path']}")
    path = ROOT / item['path']
    raw = path.read_bytes()
    report = json.loads(raw)
    if sha(raw) != item['file_sha256']:
        raise SystemExit(f"file SHA mismatch: {item['path']}")
    if sha(canonical({key: value for key, value in report.items() if key != 'report_sha256'})) != report.get('report_sha256'):
        raise SystemExit(f"report SHA mismatch: {item['path']}")
    denominator = report.get('denominator', {})
    if (report.get('status') != 'completed' or report.get('task_id') != 'S2-10'
            or report.get('kind') != 'phase2_s210_census_shard' or report.get('snapshot_id') != SNAPSHOT_ID
            or denominator.get('closed_exactly_once') is not True
            or denominator.get('all_selected_stages_terminal') is not True
            or denominator.get('selected_units') != denominator.get('terminal_units')
            or denominator.get('expected_stage_jobs') != denominator.get('terminal_stage_jobs')):
        raise SystemExit(f"incomplete or mismatched shard: {item['path']}")
    platform, index = item['platform'], item['index']
    if report.get('platform') != platform or report.get('shard') != {'index': index, 'count': 256}:
        raise SystemExit(f"shard index disagrees with path: {item['path']}")
    if (platform, index) in seen_shards:
        raise SystemExit(f"duplicate shard: {platform}/{index}")
    seen_shards.add((platform, index))
    platform_shards[platform].add(index)
    run = conn.execute('SELECT state, manifest_sha256, manifest_json FROM runs WHERE run_id=?', (report['run_id'],)).fetchone()
    if not run or run[0] != 'completed' or run[1] != report.get('run_manifest_sha256') or run[1] != item.get('run_manifest_sha256'):
        raise SystemExit(f"SQLite run/manifest mismatch: {item['path']}")
    manifest = json.loads(run[2])
    if (manifest.get('snapshot_id') != SNAPSHOT_ID or manifest.get('platforms') != [platform]
            or manifest.get('shard_index') != index or manifest.get('shard_count') != 256):
        raise SystemExit(f"SQLite manifest identity mismatch: {item['path']}")
    db_counts = dict(conn.execute('SELECT status, COUNT(*) FROM jobs WHERE run_id=? GROUP BY status', (report['run_id'],)).fetchall())
    if sum(db_counts.values()) != denominator['terminal_stage_jobs'] or any(db_counts.get(s, 0) != report['queue']['counts'].get(s, 0) for s in STATUSES):
        raise SystemExit(f"SQLite stage jobs mismatch: {item['path']}")
    if db_counts.get('queued', 0) or db_counts.get('leased', 0):
        raise SystemExit(f"open stage jobs remain: {item['path']}")
    rows = report.get('plugin_reports', [])
    counts = Counter(row.get('unit_status') for row in rows)
    if len(rows) != denominator['terminal_units'] or any(counts[s] != report['counts'].get(s) for s in STATUSES):
        raise SystemExit(f"unit outcome counts mismatch: {item['path']}")
    for row in rows:
        key = (row['registry_entry_id'], platform)
        if key in seen_units:
            raise SystemExit(f"duplicate catalog entry/platform: {key}")
        seen_units.add(key)
        status = row['unit_status']
        unit_counts[status] += 1
        cohort_counts[(row['ecosystem'], platform)][status] += 1
        reason_counts[(status, row.get('reason_code') or '(none)')] += 1
        stages = row.get('stage_statuses', {})
        if set(stages) != set(STAGES):
            raise SystemExit(f"missing selected stage statuses: {item['path']}")
        for stage, stage_status in stages.items():
            if stage_status not in STATUSES:
                raise SystemExit(f"non-terminal stage status: {item['path']} {stage}")
            stage_counts[stage][stage_status] += 1
    verified_jobs += denominator['terminal_stage_jobs']

for platform in ('linux', 'windows'):
    if sorted(platform_shards[platform]) != list(range(256)):
        raise SystemExit(f"non-contiguous shard coverage for {platform}")
if len(seen_shards) != EXPECTED_SHARDS or len(seen_units) != EXPECTED_UNITS or sum(unit_counts.values()) != EXPECTED_UNITS or verified_jobs != 68_280:
    raise SystemExit('S2-11 unit or stage-job total is inconsistent')
if any(unit_counts[s] != state['completed_unit_counts'][s] for s in STATUSES):
    raise SystemExit('S2-11 canonical unit counts disagree with checkpoint')
queue_counts = dict(conn.execute('SELECT status, COUNT(*) FROM jobs GROUP BY status').fetchall())
for status in ('passed', 'failed', 'skipped', 'unknown', 'queued', 'leased'):
    queue_counts.setdefault(status, 0)
if queue_counts != state['stage_job_counts']:
    raise SystemExit('final SQLite stage-job status counts disagree with checkpoint')
active = conn.execute("SELECT COUNT(*) FROM runs WHERE state='running'").fetchone()[0]
if active:
    raise SystemExit(f'{active} SQLite runs remain active')

source_paths = {
    'catalog_snapshot': 'outputs/acceptance/phase2-catalog-snapshot-20261012.json',
    's210_aggregate': 'outputs/acceptance/phase2-s210-census-interim-20261012.json',
    's211_failure_ledger': 'outputs/acceptance/phase2-s211-failures-20261014.json',
    's211_scoped_regression': 'outputs/acceptance/phase2-s211-process-exit-regression-20261014.json',
    'phase1_supported_versions': 'outputs/acceptance/t18-final-audit-20261012.json',
}
source_hashes = {key: sha((ROOT / path).read_bytes()) for key, path in source_paths.items()}
snapshot = json.loads((ROOT / source_paths['catalog_snapshot']).read_text(encoding='utf-8'))
ledger = json.loads((ROOT / source_paths['s211_failure_ledger']).read_text(encoding='utf-8'))
regression = json.loads((ROOT / source_paths['s211_scoped_regression']).read_text(encoding='utf-8'))
t18 = json.loads((ROOT / source_paths['phase1_supported_versions']).read_text(encoding='utf-8'))
if ledger.get('failed_and_unknown_units_reviewed') != 2056 or len(t18.get('supported_plugins', [])) != 4:
    raise SystemExit('S2-11 attribution ledger or phase-one support evidence did not close')

unknown_reasons = [
    {'reason_code': reason, 'count': count}
    for (status, reason), count in reason_counts.most_common()
    if status == 'unknown'
]
failed_reasons = [
    {'reason_code': reason, 'count': count}
    for (status, reason), count in reason_counts.most_common()
    if status == 'failed'
]
stage_json = {stage: {status: stage_counts[stage][status] for status in STATUSES} for stage in STAGES}
cohort_json = {
    f'{eco}/{platform}': {status: cohort_counts[(eco, platform)][status] for status in STATUSES}
    for eco in ('nonebot', 'koishi') for platform in ('linux', 'windows')
}
now = datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')
catalog_entries = 5_690
audit = {
    'schema_version': 1,
    'task_id': 'S2-12',
    'kind': 'phase2_catalog_final_audit',
    'audited_at': now,
    'source_snapshot_id': SNAPSHOT_ID,
    'source_sha256': source_hashes,
    'scope': {'catalog_entries': catalog_entries, 'ecosystems': {'nonebot': 943, 'koishi': 4747}, 'platforms': ['linux', 'windows'], 'unit_denominator': EXPECTED_UNITS},
    's211_full_rerun': {
        'status': 'completed', 'completed_at': state['completed_at'], 'queue_db': state['queue_db'],
        'runner_command': 'pwsh -NoProfile -File .work/catalog-census/run-s211-full.ps1',
        'shard_replay_command': 'uv run --locked --project lab --python 3.12.14 python -m lab.census --snapshot catalogs/p2-41649744e057d61031d5 --queue-db .work/catalog-census/phase2-s211-rerun.sqlite3 --platform <linux|windows> --shard <index>/256 --report outputs/acceptance/phase2-s211-rerun-census-<platform>-shard<index>of256-20261014.json --timeout-seconds 240',
        'canonical_reports': len(state['completed_reports']), 'expected_reports': EXPECTED_SHARDS,
        'units': EXPECTED_UNITS, 'stage_jobs': verified_jobs, 'invalid_reports': 0,
        'report_file_sha256_manifest_verified': True, 'sqlite_run_manifest_job_closure_verified': True,
        'contiguous_shards': {'linux': 256, 'windows': 256}, 'active_runs': active,
        'unit_counts': {status: unit_counts[status] for status in STATUSES},
        'unit_outcome_explanation': 'The unit reducer returns passed only when all six selected stages pass. Every one of the 11,380 units has federated and compare skipped, so no catalog unit qualifies as passed; 24,914 passed SQLite stage jobs are prepare/install/native/cleanup work and remain a separate denominator.',
        'stage_job_counts': queue_counts,
        'by_ecosystem_platform': cohort_json,
        'by_stage': stage_json,
        'failed_reason_counts': failed_reasons,
        'unknown_reason_counts': unknown_reasons,
        'attribution_ledger_reviewed_original_failed_unknown_units': ledger['failed_and_unknown_units_reviewed'],
        'native_pass_federated_fail_pairs': ledger.get('native_pass_federated_fail_pairs'),
    },
    'gates': [
        {'id': 'P2-CATALOG', 'status': 'passed', 'evidence': 'Frozen 5,690-entry snapshot and source hashes are retained; NoneBot 943 + Koishi 4,747.'},
        {'id': 'P2-CENSUS', 'status': 'passed', 'evidence': '512/512 unique canonical shards, 11,380/11,380 entry×OS units and 68,280/68,280 terminal selected stage jobs; no queued/leased jobs or active runs.'},
        {'id': 'P2-AUTOMATION', 'status': 'blocked', 'evidence': 'All jobs are terminal, but this completed run has 2,158 Linux worker_cleanup_unconfirmed units whose exact Worker diagnostic is Docker Engine API unavailable, plus 796 Windows App Control Win32 4551 unknown units and 16 other worker/preparation/timeout unknown units. A later local Docker Desktop 29.8.2 availability check was not a rerun and does not rewrite these reports.'},
        {'id': 'P2-BEHAVIOR', 'status': 'not_evaluated', 'evidence': 'Federated and compare each have 11,380 skipped stages; entry-bound behavior fixtures are absent, and the catalog unit outcomes contain 0 passed.'},
        {'id': 'P2-FIELD', 'status': 'not_run', 'evidence': 'No real-platform login/action/receipt/recipient evidence was collected.'},
    ],
    'compatibility_declarations': {
        'new_catalog_plugin_support_claims': 0,
        'retained_phase1_exact_supported_versions': t18['supported_plugins'],
        'phase1_support_source': source_paths['phase1_supported_versions'],
        'phase1_support_expanded_by_phase2': False,
        'unit_pass_is_compatibility_claim': False,
        'stage_job_pass_is_plugin_compatibility_claim': False,
    },
    'limitations': [
        'S2-11 is a completed full rerun with terminal census accounting; completion does not make its failed, skipped, or unknown units compatible.',
        'All 11,380 federated and 11,380 compare stages were skipped; parity is not established.',
        '5,621 of 5,690 entries remain unmapped by the conservative behavior category dry run.',
        'The Docker Engine outage was observed in the rerun environment; Docker became available only after the run ended. A corrected replay remains follow-up work if P2-AUTOMATION must pass.',
    ],
}
AUDIT.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

lines = [
    '# S2-12 compatibility matrix', '',
    f"Audited at {now}; source snapshot `{SNAPSHOT_ID}`. This matrix separates catalog census closure from behavior compatibility and real-platform evidence.", '',
    '## Gate decisions', '',
    '| Gate | Result | Evidence boundary |', '| --- | --- | --- |',
    '| P2-CATALOG | PASS | 5,690 frozen entries: NoneBot 943, Koishi 4,747. |',
    '| P2-CENSUS | PASS | 512/512 shards, 11,380/11,380 units, 68,280/68,280 terminal stage jobs; zero active, queued or leased work. |',
    '| P2-AUTOMATION | BLOCKED | All jobs closed, but 2,158 Linux units recorded Docker Engine API unavailable; 796 Windows NoneBot units were blocked by App Control Win32 4551; 16 additional worker/preparation/timeout unknowns remain. |',
    '| P2-BEHAVIOR | NOT EVALUATED | Federated and compare stages were skipped for all 11,380 units; no catalog behavior pass is established. |',
    '| P2-FIELD | NOT RUN | No real-platform login, action, receipt or recipient evidence. |', '',
    '## Catalog unit outcomes by ecosystem and platform', '',
    '| Cohort | Denominator | Passed | Failed | Skipped | Unknown | Behavior conclusion |', '| --- | ---: | ---: | ---: | ---: | ---: | --- |',
]
for eco, platform in (('nonebot', 'linux'), ('nonebot', 'windows'), ('koishi', 'linux'), ('koishi', 'windows')):
    c = cohort_counts[(eco, platform)]
    denominator = sum(c.values())
    lines.append(f"| {eco} / {platform} | {denominator} | {c['passed']} | {c['failed']} | {c['skipped']} | {c['unknown']} | Not established |")
lines += [
    f"| **Total** | **{sum(unit_counts.values())}** | **{unit_counts['passed']}** | **{unit_counts['failed']}** | **{unit_counts['skipped']}** | **{unit_counts['unknown']}** | **No catalog behavior pass** |", '',
    '## Why unit-level passed is 0', '',
    'The unit reducer returns `passed` only when all six selected stages pass. Every one of the 11,380 units has `federated=skipped` and `compare=skipped`, so no catalog unit qualifies as passed. The 24,914 passed SQLite stage jobs belong to prepare/install/native/cleanup work and use a separate job denominator; they do not establish a plugin compatibility pass.', '',
    '## Selected stage job outcomes', '',
    '| Stage | Passed | Failed | Skipped | Unknown |', '| --- | ---: | ---: | ---: | ---: |',
]
for stage in STAGES:
    c = stage_counts[stage]
    lines.append(f"| {stage} | {c['passed']} | {c['failed']} | {c['skipped']} | {c['unknown']} |")
lines += [
    '',
    f"SQLite job total: **{sum(queue_counts[s] for s in STATUSES)}** terminal jobs (passed {queue_counts['passed']}, failed {queue_counts['failed']}, skipped {queue_counts['skipped']}, unknown {queue_counts['unknown']}); queued 0, leased 0. Job-level passed is stage-level and is not a plugin-unit compatibility pass.", '',
    '## Failure and unknown classification', '',
    '| Unit status | Reason code | Units |', '| --- | --- | ---: |',
]
for (status, reason), count in sorted(reason_counts.items(), key=lambda item: (item[0][0], item[0][1])):
    if status in ('failed', 'unknown'):
        lines.append(f'| {status} | `{reason}` | {count} |')
lines += [
    '',
    'The 2,158 `worker_cleanup_unconfirmed` outcomes all carry a Docker named-pipe API-unavailable diagnostic from the run environment. Docker Desktop was started after the completed run and `docker info` then reported 29.8.2; that later check is not treated as a rerun. The 796 Windows NoneBot unknowns carry the explicit App Control Win32 4551 restriction. Original S2-10 results remain immutable; the 2,056-unit S2-11 source attribution ledger and raw 512 S2-11 shard reports remain available. Replay commands are recorded in the machine-readable audit and original dispatcher script.', '',
    '## Support claims and limits', '',
    '- New catalog-wide supported-plugin claims: **0**.',
    '- Retain only the four exact phase-one versions supported by [T18](t18-final-audit-20261012.json); phase two does not expand them.',
    '- The S2-11 probe regression confirms that two frozen Koishi plugins calling `process.exit(0)` are reported as failed native loads, with Worker cleanup separately observed; it does not turn them into compatibility passes.',
    '- 5,621 of 5,690 entries have no conservative behavior-category mapping; their behavior remains unevaluated.',
    '- Real-platform delivery remains unverified. Virtual Worker results do not establish login, action, receipt or recipient behavior on a real platform.',
    '',
    f"Machine-readable audit: [phase2-s212-audit-20261017.json](phase2-s212-audit-20261017.json). Full report and queue checkpoint: [S2-11 checkpoint](phase2-s211-process-exit-regression-20261014.json). Failure provenance: [S2-11 attribution ledger](phase2-s211-failures-20261014.json).",
]
MATRIX.write_text('\n'.join(lines) + '\n', encoding='utf-8')
print(json.dumps({'audit': AUDIT.as_posix(), 'matrix': MATRIX.as_posix(), 'audited_at': now,
                  'unit_counts': {status: unit_counts[status] for status in STATUSES},
                  'stage_job_counts': queue_counts, 'reports': len(seen_shards), 'invalid_reports': 0}, ensure_ascii=False, indent=2))
