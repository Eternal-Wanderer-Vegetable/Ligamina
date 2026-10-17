import { readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { matchesTextSha256 } from './evidence-hash.mjs';

export const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const start = '<!-- ligamina:phase1:start -->';
const end = '<!-- ligamina:phase1:end -->';
const phase2Start = '<!-- ligamina:phase2:start -->';
const phase2End = '<!-- ligamina:phase2:end -->';
const auditPath = 'outputs/acceptance/t18-final-audit-20261012.json';
const reports = [
  ['Windows', 'outputs/acceptance/t13-representative-windows-full-20261010.json'],
  ['WSL Linux', 'outputs/acceptance/t13-representative-wsl-final-20261010.json'],
];

const load = async (relative) => JSON.parse(await readFile(path.join(root, relative), 'utf8'));
const escape = (value) => String(value).replaceAll('|', '\\|').replaceAll('\n', ' ');

export async function phase1Block() {
  const audit = await load(auditPath);
  if (audit.task !== 'T18' || !Array.isArray(audit.criteria) || audit.criteria.length !== 15
      || !Array.isArray(audit.supported_plugins) || !audit.gates) {
    throw new Error('Invalid phase-one audit input');
  }
  const loaded = await Promise.all(reports.map(async ([label, file]) => {
    const report = await load(file);
    if (report.suite !== 'representative' || report.status !== 'completed' || !Array.isArray(report.samples)
        || report.samples.length !== 20 || !Array.isArray(report.support_list)) {
      throw new Error(`Invalid representative report: ${file}`);
    }
    const identities = new Set(report.samples.map((sample) => sample.sample_id));
    if (identities.size !== report.samples.length) throw new Error(`Duplicate samples in ${file}`);
    const counts = {};
    for (const level of ['L0', 'L1', 'L2', 'L3', 'L4']) {
      const c = { passed: 0, failed: 0, skipped: 0, unknown: 0 };
      for (const sample of report.samples) {
        const status = sample.levels?.[level]?.status;
        if (status === 'not_run') c.skipped++;
        else if (Object.hasOwn(c, status)) c[status]++;
        else throw new Error(`Unsupported or missing ${level} status '${status}' in ${file}`);
      }
      counts[level] = `${c.passed} / ${c.failed} / ${c.skipped} / ${c.unknown}`;
    }
    for (const plugin of audit.supported_plugins) {
      const sample = report.samples.find((item) => item.ecosystem === plugin.ecosystem
        && item.package === plugin.package && item.version === plugin.version);
      if (sample?.levels?.L4?.status !== 'passed') throw new Error(`Support entry lacks ${label} L4: ${plugin.package}`);
    }
    return { label, counts };
  }));
  const passed = audit.criteria.filter((criterion) => criterion.status === 'pass').length;
  return [
    start,
    `验收日期：${escape(audit.review_date)}。C01–C15：${passed}/${audit.criteria.length}；G1：**${escape(audit.gates.G1)}**；G2：**${escape(audit.gates.G2)}**。`,
    '',
    '固定代表样本每平台 20 个；以下按级别展示 **通过 / 失败 / 跳过或未执行 / 未知**，分母均为 20。加载与联邦派发通过数分别保留，行为支持只看 L4。',
    '',
    '| 级别 | Windows | WSL Linux |',
    '| --- | --- | --- |',
    ...['L0', 'L1', 'L2', 'L3', 'L4'].map((level) => `| ${level} | ${loaded[0].counts[level]} | ${loaded[1].counts[level]} |`),
    '',
    '| 生态 | 已通过行为验证的精确插件版本 |',
    '| --- | --- |',
    ...audit.supported_plugins.map((plugin) => `| ${escape(plugin.ecosystem)} | ${escape(plugin.package)} == ${escape(plugin.version)} |`),
    '',
    `证据：[T18 最终审阅](${auditPath})、[Windows 20 样本](${reports[0][1]})、[WSL Linux 20 样本](${reports[1][1]})。`,
    '',
    '稳定性与安装依据：[WSL 24 小时 / 200 恢复循环](outputs/acceptance/t14-stability-linux-wsl-100x24h-20261011.json)、[Windows Sandbox C14](outputs/acceptance/t15-windows-sandbox-c14-20261011.json)、[Linux 容器 smoke](outputs/acceptance/t16-linux-wsl-container-20261010.json)。',
    '',
    '范围：虚拟 Gateway、已锁定 Host/profile 和上述行为用例；未连接真实 QQ/其他平台网关。Linux 容器结果是本机 WSL 发起的 smoke。四个版本的支持不能外推为商店全集兼容。',
    end,
  ].join('\n');
}

export async function phase2Block() {
  const catalogPath = 'outputs/acceptance/phase2-catalog-snapshot-20261012.json';
  const recipesPath = 'outputs/acceptance/phase2-s206-20261012.json';
  const schedulerPath = 'outputs/acceptance/phase2-s207-20261012.json';
  const reportingPath = 'outputs/acceptance/phase2-s208-20261012.json';
  const baselinePath = 'outputs/acceptance/phase2-s209-baseline-20261012.json';
  const s210BaselinePath = 'outputs/acceptance/phase2-s210-baseline-20261012.json';
  const s210SmokePath = 'outputs/acceptance/phase2-s210-census-linux-shard0of256-smoke-20261012.json';
  const s210ImplementationPath = 'outputs/acceptance/phase2-s210-implementation-20261012.json';
  const s210LinuxFormalPath = 'outputs/acceptance/phase2-s210-census-linux-shard0of256-20261012.json';
  const s210Linux1FormalPath = 'outputs/acceptance/phase2-s210-census-linux-shard1of256-20261012.json';
  const s210Linux2FormalPath = 'outputs/acceptance/phase2-s210-census-linux-shard2of256-20261012.json';
  const s210WindowsFormalPath = 'outputs/acceptance/phase2-s210-census-windows-shard0of256-20261012.json';
  const s210Windows1FormalPath = 'outputs/acceptance/phase2-s210-census-windows-shard1of256-20261012.json';
  const s210WindowsAttempt1Path = 'outputs/acceptance/phase2-s210-census-windows-shard0of256-attempt1-20261012.json';
  const s210InterimAggregatePath = 'outputs/acceptance/phase2-s210-census-interim-20261012.json';
  const s211RerunPath = 'outputs/acceptance/phase2-s211-process-exit-regression-20261014.json';
  const s212AuditPath = 'outputs/acceptance/phase2-s212-audit-20261017.json';
  const s212MatrixPath = 'outputs/acceptance/phase2-s212-compatibility-matrix-20261017.md';
  const pilotManifestPath = 'outputs/acceptance/phase2-s209-pilot-manifest-20261012.json';
  const pilotAcceptancePath = 'outputs/acceptance/phase2-s209-20261012.json';
  const nonebotLinuxPilotPath = 'outputs/acceptance/phase2-s209-nonebot-linux-pilot-20261012.json';
  const koishiLinuxPilotPath = 'outputs/acceptance/phase2-s209-koishi-linux-pilot-20261012.json';
  const koishiWindowsPilotPath = 'outputs/acceptance/phase2-s209-koishi-windows-pilot-20261012.json';
  const planPath = 'outputs/acceptance/phase2-s207-cli-smoke-20261012.json';
  const featureListPath = 'feature_list.json';
  const [catalog, recipes, scheduler, reporting, baseline, s210Baseline, s210Smoke, s210Implementation, s210LinuxFormal, s210Linux1Formal, s210Linux2Formal, s210WindowsFormal, s210Windows1Formal, s210WindowsAttempt1, s210InterimAggregate, s211Rerun, s212Audit, pilotManifest, pilotAcceptance, nonebotLinuxPilot, koishiLinuxPilot, koishiWindowsPilot, plan, featureList] = await Promise.all(
    [catalogPath, recipesPath, schedulerPath, reportingPath, baselinePath, s210BaselinePath, s210SmokePath, s210ImplementationPath, s210LinuxFormalPath, s210Linux1FormalPath, s210Linux2FormalPath, s210WindowsFormalPath, s210Windows1FormalPath, s210WindowsAttempt1Path, s210InterimAggregatePath, s211RerunPath, s212AuditPath, pilotManifestPath, pilotAcceptancePath, nonebotLinuxPilotPath, koishiLinuxPilotPath, koishiWindowsPilotPath, planPath, featureListPath].map(load),
  );
  for (const [relative, expected] of Object.entries(reporting.input_reports ?? {})) {
    const bytes = await readFile(path.join(root, relative));
    if (!matchesTextSha256(bytes, expected)) throw new Error(`Stale phase-two report input: ${relative}`);
  }
  const count = catalog.counts?.registry_entries;
  const official = Object.values(catalog.counts?.official_maintained ?? {}).reduce((sum, value) => sum + value, 0);
  const suggested = Object.values(recipes.catalog_classification_dry_run?.suggested_entries ?? {}).reduce((sum, value) => sum + value, 0);
  const unmapped = Object.values(recipes.catalog_classification_dry_run?.unmapped_entries ?? {}).reduce((sum, value) => sum + value, 0);
  const realPlan = scheduler.verification?.real_snapshot_cli_plan;
  if (catalog.kind !== 'phase2_catalog_snapshot_acceptance' || count !== 5690 || official !== 55
      || catalog.integrity?.offline_verify !== 'passed') {
    throw new Error(`Invalid frozen catalog acceptance report: ${catalogPath}`);
  }
  if (recipes.task_id !== 'S2-06' || recipes.status !== 'completed'
      || recipes.catalog_classification_dry_run?.total_entries !== count || suggested + unmapped !== count) {
    throw new Error(`Invalid recipe classification report: ${recipesPath}`);
  }
  if (scheduler.task_id !== 'S2-07' || scheduler.status !== 'completed'
      || realPlan?.selected_linux_units !== 3 || realPlan?.stage_jobs !== 18 || realPlan?.execution_started !== false) {
    throw new Error(`Invalid scheduler acceptance report: ${schedulerPath}`);
  }
  if (plan.snapshot_id !== catalog.snapshot_id || plan.selected_units !== 3
      || plan.expected_stage_jobs !== 18 || plan.execution_started !== false
      || plan.job_counts?.queued !== 18 || plan.job_counts?.passed !== 0) {
    throw new Error(`Invalid catalog plan report: ${planPath}: ${JSON.stringify({
      snapshot_id: plan.snapshot_id, expected_snapshot_id: catalog.snapshot_id,
      selected_units: plan.selected_units, expected_stage_jobs: plan.expected_stage_jobs,
      execution_started: plan.execution_started, job_counts: plan.job_counts,
    })}`);
  }
  const reportingComplete = reporting.status === 'completed';
  const reportingPending = reporting.status === 'acceptance-pending';
  if (reporting.task_id !== 'S2-08' || (!reportingComplete && !reportingPending)
      || reporting.next_task !== (reportingComplete ? 'S2-09' : 'S2-08 acceptance')
      || reporting.catalog_execution_started !== false) {
    throw new Error(`Invalid reporting acceptance report: ${reportingPath}`);
  }
  if (baseline.task_id !== 'S2-09' || baseline.status !== 'baseline_completed'
      || baseline.exit_code !== 0 || baseline.implementation_changes_present !== false
      || !baseline.repository_head.startsWith('cbcbb0a')) {
    throw new Error(`Invalid S2-09 baseline report: ${baselinePath}`);
  }
  if (s210Baseline.task_id !== 'S2-10' || s210Baseline.status !== 'baseline_completed'
      || s210Baseline.exit_code !== 0 || s210Baseline.working_tree_before_run !== 'clean'
      || !s210Baseline.repository_head.startsWith('20d8f04')
      || s210Baseline.summary?.lab_unittest?.tests !== 102) {
    throw new Error(`Invalid S2-10 baseline report: ${s210BaselinePath}`);
  }
  const s210SourceHashes = {
    catalog_sha256: 'catalogs/p2-41649744e057d61031d5/catalog.json',
    artifact_lock_sha256: 'catalogs/p2-41649744e057d61031d5/artifacts.lock.json',
    profile_lock_sha256: 'catalogs/p2-41649744e057d61031d5/profiles.lock.json',
    s209_blocker_evidence_sha256: pilotAcceptancePath,
  };
  const s210SourceBytes = {};
  for (const [key, relative] of Object.entries(s210SourceHashes)) {
    const bytes = await readFile(path.join(root, relative));
    s210SourceBytes[key] = bytes;
    const expectedHashes = [s210Smoke, s210LinuxFormal, s210Linux1Formal, s210Linux2Formal,
      s210WindowsFormal, s210Windows1Formal, s210WindowsAttempt1].map((report) => report.source_hashes?.[key]);
    if (expectedHashes.some((expected) => !matchesTextSha256(bytes, expected))) {
      throw new Error(`Stale S2-10 census input: ${relative}`);
    }
  }
  const previousWindowsAttempt = s210Implementation.formal_shards?.windows_0_of_256?.previous_attempt_report;
  if (previousWindowsAttempt) {
    const bytes = await readFile(path.join(root, previousWindowsAttempt));
    if (!matchesTextSha256(bytes, s210Implementation.formal_shards.windows_0_of_256.previous_attempt_file_sha256)) {
      throw new Error(`Stale S2-10 Windows attempt evidence: ${previousWindowsAttempt}`);
    }
  }
  for (const attempt of Object.values(s210Implementation.retry_attempts ?? {})) {
    const bytes = await readFile(path.join(root, attempt.report));
    const report = JSON.parse(bytes);
    if (!matchesTextSha256(bytes, attempt.file_sha256) || report.report_sha256 !== attempt.report_sha256
        || report.status !== attempt.status || report.kind !== 'phase2_s210_census_shard') {
      throw new Error(`Stale S2-10 retry evidence: ${attempt.report}`);
    }
  }
  if (s210Smoke.task_id !== 'S2-10' || s210Smoke.kind !== 'phase2_s210_census_shard'
      || s210Smoke.status !== 'completed' || s210Smoke.snapshot_id !== catalog.snapshot_id
      || s210Smoke.platform !== 'linux' || s210Smoke.shard?.index !== 0 || s210Smoke.shard?.count !== 256
      || s210Smoke.denominator?.full_entry_os_units !== 11380
      || s210Smoke.denominator?.selected_units !== 16 || s210Smoke.denominator?.terminal_units !== 16
      || s210Smoke.denominator?.expected_stage_jobs !== 96 || s210Smoke.denominator?.terminal_stage_jobs !== 96
      || s210Smoke.denominator?.closed_exactly_once !== true
      || s210Smoke.denominator?.all_selected_stages_terminal !== true
      || s210Smoke.denominator?.full_census_closed !== false
      || ['passed', 'failed', 'skipped', 'unknown'].reduce((sum, status) => sum + (s210Smoke.counts?.[status] ?? 0), 0) !== 16) {
    throw new Error(`Invalid S2-10 smoke shard evidence: ${s210SmokePath}`);
  }
  if (s210Implementation.task_id !== 'S2-10' || s210Implementation.kind !== 'phase2_s210_implementation_checkpoint'
      || s210Implementation.status !== 'in_progress' || s210Implementation.validation?.exit_code !== 0
      || s210Implementation.denominator?.entry_os_units !== 11380
      || s210Implementation.denominator?.full_census_closed !== (s210InterimAggregate.status === 'census-closed')
      || s210Implementation.implementation?.full_census_closed !== (s210InterimAggregate.status === 'census-closed')
      || s210Implementation.formal_shards?.linux_0_of_256?.report_sha256 !== s210LinuxFormal.report_sha256
      || s210LinuxFormal.task_id !== 'S2-10' || s210LinuxFormal.status !== 'completed'
      || s210LinuxFormal.platform !== 'linux' || s210LinuxFormal.shard?.index !== 0 || s210LinuxFormal.shard?.count !== 256
      || s210LinuxFormal.denominator?.full_entry_os_units !== 11380
      || s210LinuxFormal.denominator?.selected_units !== 16 || s210LinuxFormal.denominator?.terminal_units !== 16
      || s210LinuxFormal.denominator?.expected_stage_jobs !== 96 || s210LinuxFormal.denominator?.terminal_stage_jobs !== 96
      || s210LinuxFormal.denominator?.full_census_closed !== false) {
    throw new Error(`Invalid S2-10 implementation or formal shard evidence: ${s210ImplementationPath}, ${s210LinuxFormalPath}`);
  }
  if (s210Implementation.formal_shards?.windows_0_of_256?.status !== 'completed'
      || s210Implementation.formal_shards.windows_0_of_256.report_sha256 !== s210WindowsFormal.report_sha256
      || s210WindowsFormal.task_id !== 'S2-10' || s210WindowsFormal.status !== 'completed'
      || s210WindowsFormal.platform !== 'windows' || s210WindowsFormal.shard?.index !== 0 || s210WindowsFormal.shard?.count !== 256
      || s210WindowsFormal.denominator?.selected_units !== 26 || s210WindowsFormal.denominator?.terminal_units !== 26
      || s210WindowsFormal.denominator?.expected_stage_jobs !== 156 || s210WindowsFormal.denominator?.terminal_stage_jobs !== 156
      || s210WindowsFormal.stage_counts?.cleanup?.unknown !== 0
      || s210WindowsAttempt1.report_sha256 !== s210Implementation.formal_shards.windows_0_of_256.previous_attempt_report_sha256) {
    throw new Error(`Invalid S2-10 Windows formal shard evidence: ${s210WindowsFormalPath}`);
  }
  if (s210Implementation.formal_shards?.linux_1_of_256?.status !== 'completed'
      || s210Implementation.formal_shards.linux_1_of_256.report_sha256 !== s210Linux1Formal.report_sha256
      || s210Linux1Formal.task_id !== 'S2-10' || s210Linux1Formal.status !== 'completed'
      || s210Linux1Formal.platform !== 'linux' || s210Linux1Formal.shard?.index !== 1 || s210Linux1Formal.shard?.count !== 256
      || s210Linux1Formal.denominator?.selected_units !== 24 || s210Linux1Formal.denominator?.terminal_units !== 24
      || s210Linux1Formal.denominator?.expected_stage_jobs !== 144 || s210Linux1Formal.denominator?.terminal_stage_jobs !== 144) {
    throw new Error(`Invalid S2-10 Linux shard 1 evidence: ${s210Linux1FormalPath}`);
  }
  if (s210Implementation.formal_shards?.linux_2_of_256?.status !== 'completed'
      || s210Implementation.formal_shards.linux_2_of_256.report_sha256 !== s210Linux2Formal.report_sha256
      || s210Linux2Formal.task_id !== 'S2-10' || s210Linux2Formal.status !== 'completed'
      || s210Linux2Formal.platform !== 'linux' || s210Linux2Formal.shard?.index !== 2 || s210Linux2Formal.shard?.count !== 256
      || s210Linux2Formal.denominator?.selected_units !== 28 || s210Linux2Formal.denominator?.terminal_units !== 28
      || s210Linux2Formal.denominator?.expected_stage_jobs !== 168 || s210Linux2Formal.denominator?.terminal_stage_jobs !== 168) {
    throw new Error(`Invalid S2-10 Linux shard 2 evidence: ${s210Linux2FormalPath}`);
  }
  if (s210Implementation.formal_shards?.windows_1_of_256?.status !== 'completed'
      || s210Implementation.formal_shards.windows_1_of_256.report_sha256 !== s210Windows1Formal.report_sha256
      || s210Windows1Formal.task_id !== 'S2-10' || s210Windows1Formal.status !== 'completed'
      || s210Windows1Formal.platform !== 'windows' || s210Windows1Formal.shard?.index !== 1 || s210Windows1Formal.shard?.count !== 256
      || s210Windows1Formal.denominator?.selected_units !== 22 || s210Windows1Formal.denominator?.terminal_units !== 22
      || s210Windows1Formal.denominator?.expected_stage_jobs !== 132 || s210Windows1Formal.denominator?.terminal_stage_jobs !== 132
      || s210Windows1Formal.stage_counts?.cleanup?.unknown !== 0) {
    throw new Error(`Invalid S2-10 Windows shard 1 evidence: ${s210Windows1FormalPath}`);
  }
  const s210InterimShardReports = s210InterimAggregate.evidence_manifest?.shard_reports ?? [];
  if (s210InterimAggregate.task_id !== 'S2-10' || s210InterimAggregate.kind !== 'phase2_s210_census_acceptance'
      || !['census-incomplete', 'census-closed'].includes(s210InterimAggregate.status)
      || s210InterimAggregate.denominator?.expected_entry_os_units !== 11380
      || s210InterimAggregate.denominator?.terminal_units + s210InterimAggregate.denominator?.missing_unit_count !== 11380
      || s210InterimAggregate.denominator?.missing_shard_count !== 512 - s210InterimShardReports.length) {
    throw new Error(`Invalid S2-10 interim aggregation: ${s210InterimAggregatePath}`);
  }
  const observedShardIds = new Set();
  const aggregateShardCounts = { passed: 0, failed: 0, skipped: 0, unknown: 0 };
  let aggregateShardUnits = 0;
  for (const shard of s210InterimShardReports) {
    const bytes = await readFile(path.join(root, shard.path));
    if (!matchesTextSha256(bytes, shard.sha256)) throw new Error(`Stale S2-10 aggregate shard input: ${shard.path}`);
    const report = JSON.parse(bytes);
    const { platform, shard: identity, denominator, counts } = report;
    const shardId = `${platform}:${identity?.index}/${identity?.count}`;
    if (report.task_id !== 'S2-10' || report.kind !== 'phase2_s210_census_shard'
        || report.status !== 'completed' || report.snapshot_id !== catalog.snapshot_id
        || !['linux', 'windows'].includes(platform) || identity?.count !== 256
        || !Number.isInteger(identity?.index) || identity.index < 0 || identity.index >= 256
        || observedShardIds.has(shardId) || denominator?.closed_exactly_once !== true
        || denominator?.all_selected_stages_terminal !== true
        || denominator?.selected_units !== denominator?.terminal_units
        || denominator?.full_entry_os_units !== 11380
        || ['passed', 'failed', 'skipped', 'unknown'].reduce((sum, status) => sum + (counts?.[status] ?? 0), 0) !== denominator.selected_units) {
      throw new Error(`Invalid S2-10 aggregate shard report: ${shard.path}`);
    }
    for (const [key, bytes] of Object.entries(s210SourceBytes)) {
      if (!matchesTextSha256(bytes, report.source_hashes?.[key])) {
        throw new Error(`Stale source hash in S2-10 shard: ${shard.path} (${key})`);
      }
    }
    observedShardIds.add(shardId);
    aggregateShardUnits += denominator.selected_units;
    for (const status of Object.keys(aggregateShardCounts)) aggregateShardCounts[status] += counts[status] ?? 0;
  }
  if (aggregateShardUnits !== s210InterimAggregate.denominator.terminal_units
      || Object.keys(aggregateShardCounts).some((status) => aggregateShardCounts[status] !== s210InterimAggregate.counts?.[status])) {
    throw new Error(`S2-10 interim totals disagree with verified shard reports: ${s210InterimAggregatePath}`);
  }
  const s211RerunState = s211Rerun.full_rerun;
  const s211CompletedReports = s211RerunState?.completed_reports ?? [];
  const s211UnitCounts = s211RerunState?.completed_unit_counts ?? {};
  const s211ExpectedUnitCounts = { passed: 0, failed: 0, skipped: 0, unknown: 0 };
  const s211SeenShards = { linux: new Set(), windows: new Set() };
  let s211VerifiedUnits = 0;
  let s211VerifiedJobs = 0;
  const s211RerunComplete = s211RerunState?.status === 'completed';
  const s211ActiveRuns = s211RerunState?.active_runs ?? [];
  const s211StageJobCounts = s211RerunState?.stage_job_counts ?? {};
  const s211TerminalJobs = ['passed', 'failed', 'skipped', 'unknown']
    .reduce((sum, status) => sum + (s211StageJobCounts[status] ?? 0), 0);
  if (s211Rerun.task_id !== 'S2-11' || !['running', 'completed'].includes(s211RerunState?.status)
      || s211RerunState.expected_canonical_shards !== 512
      || s211RerunState.expected_units !== 11380
      || s211RerunState.completed_canonical_reports !== s211CompletedReports.length
      || s211RerunState.completed_canonical_reports + s211RerunState.remaining_shards !== 512
      || s211RerunState.completed_units + s211RerunState.remaining_units !== 11380
      || s211RerunState.completed_reports.length !== s211RerunState.completed_canonical_reports
      || s211TerminalJobs !== s211RerunState.completed_stage_jobs
      || (s211RerunComplete && (s211RerunState.completed_canonical_reports !== 512
        || s211RerunState.remaining_shards !== 0 || s211RerunState.remaining_units !== 0
        || s211RerunState.completed_units !== 11380 || s211ActiveRuns.length !== 0
        || (s211StageJobCounts.queued ?? 0) !== 0 || (s211StageJobCounts.leased ?? 0) !== 0))) {
    throw new Error(`Invalid S2-11 rerun checkpoint: ${s211RerunPath}`);
  }
  for (const item of s211CompletedReports) {
    const match = /^outputs\/acceptance\/phase2-s211-rerun-census-(linux|windows)-shard(\d+)of256-20261014\.json$/.exec(item.path);
    if (!match || match[1] !== item.platform || Number(match[2]) !== item.index
        || !s211SeenShards[item.platform] || s211SeenShards[item.platform].has(item.index)) {
      throw new Error(`Invalid S2-11 canonical shard index: ${item.path}`);
    }
    const bytes = await readFile(path.join(root, item.path));
    const shard = JSON.parse(bytes);
    const denominator = shard.denominator ?? {};
    if (!matchesTextSha256(bytes, item.file_sha256) || shard.report_sha256 !== item.report_sha256
        || shard.status !== 'completed' || shard.task_id !== 'S2-10'
        || shard.kind !== 'phase2_s210_census_shard'
        || shard.snapshot_id !== catalog.snapshot_id || shard.platform !== item.platform
        || shard.shard?.index !== item.index || shard.shard?.count !== 256
        || denominator.closed_exactly_once !== true
        || denominator.all_selected_stages_terminal !== true
        || denominator.selected_units !== denominator.terminal_units
        || denominator.expected_stage_jobs !== denominator.terminal_stage_jobs
        || denominator.terminal_units !== item.units
        || denominator.terminal_stage_jobs !== item.jobs) {
      throw new Error(`Invalid S2-11 canonical shard report: ${item.path}`);
    }
    const counts = shard.counts ?? {};
    for (const status of Object.keys(s211ExpectedUnitCounts)) {
      s211ExpectedUnitCounts[status] += counts[status] ?? 0;
    }
    s211SeenShards[item.platform].add(item.index);
    s211VerifiedUnits += denominator.terminal_units;
    s211VerifiedJobs += denominator.terminal_stage_jobs;
  }
  for (const platform of ['linux', 'windows']) {
    const indices = [...s211SeenShards[platform]].sort((a, b) => a - b);
    if (indices.some((index, position) => index !== position)) {
      throw new Error(`S2-11 completed shard range is not contiguous for ${platform}`);
    }
  }
  if (s211VerifiedUnits !== s211RerunState.completed_units
      || s211VerifiedJobs !== s211RerunState.completed_stage_jobs
      || Object.keys(s211ExpectedUnitCounts).some((status) => s211ExpectedUnitCounts[status] !== s211UnitCounts[status])
      || Object.values(s211UnitCounts).reduce((sum, value) => sum + value, 0) !== s211VerifiedUnits) {
    throw new Error(`S2-11 rerun checkpoint totals disagree with canonical reports: ${s211RerunPath}`);
  }
  const s212Gates = new Map((s212Audit.gates ?? []).map((gate) => [gate.id, gate.status]));
  const s212Matrix = await readFile(path.join(root, s212MatrixPath), 'utf8');
  const s212CheckpointBytes = await readFile(path.join(root, s211RerunPath));
  if (s212Audit.task_id !== 'S2-12' || s212Audit.kind !== 'phase2_catalog_final_audit'
      || s212Audit.s211_full_rerun?.status !== 'completed'
      || s212Audit.s211_full_rerun.canonical_reports !== 512 || s212Audit.s211_full_rerun.units !== 11380
      || Object.keys(s212Audit.source_sha256 ?? {}).length !== 5
      || !matchesTextSha256(s212CheckpointBytes, s212Audit.source_sha256.s211_scoped_regression)
      || s212Audit.s211_full_rerun.unit_counts?.passed !== 0
      || s212Audit.s211_full_rerun.unit_counts?.failed !== s211UnitCounts.failed
      || s212Audit.s211_full_rerun.unit_counts?.skipped !== s211UnitCounts.skipped
      || s212Audit.s211_full_rerun.unit_counts?.unknown !== s211UnitCounts.unknown
      || !s212Audit.s211_full_rerun.unit_outcome_explanation?.includes('federated and compare skipped')
      || s212Gates.get('P2-CATALOG') !== 'passed' || s212Gates.get('P2-CENSUS') !== 'passed'
      || s212Gates.get('P2-AUTOMATION') !== 'blocked' || s212Gates.get('P2-BEHAVIOR') !== 'not_evaluated'
      || s212Gates.get('P2-FIELD') !== 'not_run'
      || !s212Matrix.includes('## Gate decisions') || !s212Matrix.includes('## Catalog unit outcomes by ecosystem and platform')
      || !s212Matrix.includes('## Why unit-level passed is 0')) {
    throw new Error(`Invalid S2-12 audit or compatibility matrix: ${s212AuditPath} / ${s212MatrixPath}`);
  }
  const pilotInputs = {
    legacy_run_sha256: 'outputs/acceptance/phase2-s209-legacy-linux-20261012.json',
    s205_source_sha256: 'outputs/acceptance/phase2-s205-20261012.json',
    pilot_manifest_sha256: pilotManifestPath,
    worker_linux_sha256: 'outputs/acceptance/phase2-workers-linux-20261012.json',
    worker_windows_sha256: 'outputs/acceptance/phase2-workers-windows-20261012.json',
    queue_acceptance_sha256: schedulerPath,
    budget_sha256: 'config/lab/phase2-pilot-budget-v1.json',
    nonebot_linux_catalog_pilot_sha256: nonebotLinuxPilotPath,
    koishi_linux_catalog_pilot_sha256: koishiLinuxPilotPath,
    koishi_windows_catalog_pilot_sha256: koishiWindowsPilotPath,
  };
  for (const [key, relative] of Object.entries(pilotInputs)) {
    const bytes = await readFile(path.join(root, relative));
    if (!matchesTextSha256(bytes, pilotAcceptance.source_hashes?.[key])) {
      throw new Error(`Stale S2-09 evidence input: ${relative}`);
    }
  }
  if (pilotManifest.task_id !== 'S2-09' || pilotManifest.catalog_entry_os_denominator !== 11380
      || pilotManifest.catalog_assignment_ledger_count !== 11380 || pilotManifest.selected_catalog_entry_count !== 60
      || pilotManifest.expected_pilot_result_count !== 128 || pilotAcceptance.task_id !== 'S2-09'
      || pilotAcceptance.kind !== 'phase2_pilot_acceptance'
      || pilotAcceptance.pilot_manifest_sha256 !== pilotManifest.manifest_sha256
      || pilotAcceptance.status !== 'acceptance-pending'
      || pilotAcceptance.pilot_run_status !== 'completed'
      || pilotAcceptance.readiness?.pilot_denominator !== 'passed'
      || pilotAcceptance.denominator?.terminal_pilot_results !== 128
      || pilotAcceptance.denominator?.closed_exactly_once !== true
      || ['passed', 'failed', 'skipped', 'unknown'].reduce((sum, status) => sum + (pilotAcceptance.counts?.[status] ?? 0), 0) !== 128
      || nonebotLinuxPilot.task_id !== 'S2-09' || nonebotLinuxPilot.kind !== 'phase2_s209_nonebot_linux_pilot_workers'
      || nonebotLinuxPilot.selected_nonebot_linux_entries !== 30 || nonebotLinuxPilot.terminal_results !== 30
      || nonebotLinuxPilot.closed_exactly_once !== true
      || koishiLinuxPilot.task_id !== 'S2-09' || koishiLinuxPilot.kind !== 'phase2_s209_koishi_linux_pilot_workers'
      || koishiLinuxPilot.selected_koishi_linux_entries !== 30 || koishiLinuxPilot.terminal_results !== 30
      || koishiLinuxPilot.closed_exactly_once !== true
      || koishiWindowsPilot.task_id !== 'S2-09' || koishiWindowsPilot.kind !== 'phase2_s209_koishi_windows_pilot_workers'
      || koishiWindowsPilot.selected_koishi_windows_entries !== 30 || koishiWindowsPilot.terminal_results !== 30
      || koishiWindowsPilot.closed_exactly_once !== true) {
    throw new Error(`Invalid S2-09 pilot evidence: ${pilotManifestPath} / ${pilotAcceptancePath}`);
  }
  const pilotCounts = ['passed', 'failed', 'skipped', 'unknown'].map((status) => pilotAcceptance.counts[status]).join(' / ');
  const nonebotPilot = pilotAcceptance.worker_readiness.nonebot_linux_pilot;
  const linuxPilot = pilotAcceptance.worker_readiness.koishi_linux_pilot;
  const windowsPilot = pilotAcceptance.worker_readiness.koishi_windows_pilot;
  const featureByTask = new Map((featureList.features ?? []).map((feature) => [feature.task_id, feature]));
  const s210Status = featureByTask.get('S2-10')?.status ?? 'not-started';
  const s211Status = featureByTask.get('S2-11')?.status ?? 'not-started';
  const s212Status = featureByTask.get('S2-12')?.status ?? 'not-started';
  const censusClosed = s210InterimAggregate.status === 'census-closed';
  const statusLabel = (status) => status === 'done' ? '已完成' : status === 'in-progress' ? '进行中' : '未开始';
  const activeShards = Object.entries(s210Implementation.formal_shards ?? {})
    .filter(([, shard]) => shard.status === 'running')
    .map(([key]) => {
      const match = /^(linux|windows)_(\d+)_of_(\d+)$/.exec(key);
      return match ? `${match[1]} ${match[2]}/${match[3]}` : key;
    });
  const activeShardLabel = activeShards.length ? activeShards.join('、') : '无';
  const formalShardCount = s210InterimShardReports.length;
  const formalUnitCount = s210InterimAggregate.denominator.terminal_units;
  const s210Label = s210Status === 'done' ? '已完成'
    : s210Status === 'in-progress' ? `进行中：实现全仓库验证通过；正式 census ${formalUnitCount.toLocaleString('en-US')}/11,380 单元、${formalShardCount}/512 shards 闭合；执行中：${activeShardLabel}；${censusClosed ? '全快照已闭合' : '全快照未闭合'}`
      : '未开始：全快照 census 尚未启动';
  const s211CountsText = ['passed', 'failed', 'skipped', 'unknown'].map((status) => s211UnitCounts[status]).join(' / ');
  const s211ActiveText = s211ActiveRuns.map((run) => `${run.platform} ${run.shard}/256`).join('、') || '无';
  const s211Label = s211RerunComplete ? `已完成：复跑 512/512 shards、11,380/11,380 单元；单元通过 / 失败 / 跳过 / 未知 ${s211CountsText}`
    : s211Status === 'in-progress' ? `进行中：复跑 ${s211RerunState.completed_canonical_reports}/512 shards、${s211RerunState.completed_units.toLocaleString('en-US')}/11,380 单元；单元通过 / 失败 / 跳过 / 未知 ${s211CountsText}；活动 ${s211ActiveText}`
      : '未开始';
  const measurements = pilotAcceptance.resource_measurements;

  return [
    phase2Start,
    `状态截至 ${escape(s212Audit.audited_at)}：${reportingComplete ? `S2-01–S2-09 pilot 执行已完成；128 个结果位置闭合（通过 / 失败 / 跳过 / 未知：${pilotCounts}）。NoneBot Linux、Koishi Linux 和 Windows 各 30 个目录 entry 已进入 L1/L2 pilot。` : 'S2-01–S2-07 已完成；S2-08 正在进行最终验收。'} 冻结快照 \`${escape(catalog.snapshot_id)}\` 含 **${count.toLocaleString('en-US')} entries**（NoneBot 943、Koishi 4,747；官方维护子集 ${official}）。`,
    '',
    '| 任务 | 状态 |',
    '| --- | --- |',
    ...[
      ...(reportingComplete ? [['S2-01–S2-08', '已完成并分别保存验收证据']] : [['S2-01–S2-07', '已完成并分别保存验收证据'], ['S2-08', '最终验收进行中']]),
      ['S2-09', reportingComplete ? `pilot 执行完成：128/128 位置闭合；通过 / 失败 / 跳过 / 未知 ${pilotCounts}；自动化与行为 gate 保留 blocker` : '等待 S2-08 验收完成'],
      ['S2-10', s210Label],
      ['S2-11', s211Label],
      ['S2-12', statusLabel(s212Status)],
    ].map(([task, status]) => `| ${task} | ${status} |`),
    '',
    `分类 dry run 只建议 ${suggested}/${count} entries，${unmapped} 项仍是 \`unmapped_category\`。S2-09 的 128 个双 OS 结果位置全部闭合：四个历史支持版本和 60 个分层目录 entry 分别保留状态；NoneBot Linux 尝试 ${nonebotPilot.attempted_workers} 个 Docker Worker（L1 ${JSON.stringify(nonebotPilot.dependency_install)}；L2 ${JSON.stringify(nonebotPilot.native_load)}），Koishi Linux 尝试 ${linuxPilot.attempted_workers} 个 Docker Worker（L1 ${JSON.stringify(linuxPilot.dependency_install)}；L2 ${JSON.stringify(linuxPilot.native_load)}），Windows 尝试 ${windowsPilot.attempted_workers} 个 Sandbox Worker（L1 ${JSON.stringify(windowsPilot.dependency_install)}；L2 ${JSON.stringify(windowsPilot.native_load)}）。`,
    '',
    `Windows Sandbox 的 CPython 启动仍受 App Control（Win32 4551）阻止；Node.js 24.20.0 签名 runtime 已用于 Windows Koishi L1/L2 pilot。Koishi Windows Job Object commit P95 ${(measurements.koishi_windows_peak_worker_memory_bytes.p95 / (1024 ** 3)).toFixed(2)} GiB（8 GiB 限额内），scratch P95 ${(measurements.koishi_windows_peak_writable_scratch_bytes.p95 / (1024 ** 2)).toFixed(1)} MiB；预算仍 provisional，NoneBot Windows 无 Worker 样本。目录 cohort 缺 entry-bound behavior fixture，L4 未评估，不能外推为兼容。S2-10 正式 census 已记账闭合 ${formalUnitCount.toLocaleString('en-US')}/11,380 个 entry×OS 单元，${formalShardCount}/512 个 shard；Linux shard 0/256 为 ${s210LinuxFormal.denominator.terminal_units}/${s210LinuxFormal.denominator.selected_units} 单元、shard 1/256 为 ${s210Linux1Formal.denominator.terminal_units}/${s210Linux1Formal.denominator.selected_units} 单元、shard 2/256 为 ${s210Linux2Formal.denominator.terminal_units}/${s210Linux2Formal.denominator.selected_units} 单元；Windows shard 0/256 为 ${s210WindowsFormal.denominator.terminal_units}/${s210WindowsFormal.denominator.selected_units}（通过 / 失败 / 跳过 / 未知：${['passed', 'failed', 'skipped', 'unknown'].map((status) => s210WindowsFormal.counts[status]).join(' / ')}），shard 1/256 为 ${s210Windows1Formal.denominator.terminal_units}/${s210Windows1Formal.denominator.selected_units}（通过 / 失败 / 跳过 / 未知：${['passed', 'failed', 'skipped', 'unknown'].map((status) => s210Windows1Formal.counts[status]).join(' / ')}）。当前执行 shard：${activeShardLabel}。独立 smoke shard 不计入正式分母。这仅表示记账完整，不表示兼容通过。真实平台登录、发送与投递验收未执行。`,
    `S2-11 同冻结快照复跑截至 ${escape(s211RerunState.observed_at)}：${s211RerunState.completed_canonical_reports}/512 份 canonical 报告、${s211RerunState.completed_units.toLocaleString('en-US')}/11,380 单元；单元通过 / 失败 / 跳过 / 未知 ${s211CountsText}，${s211RerunComplete ? '复跑已完成，无剩余报告、单元或活动执行' : `还剩 ${s211RerunState.remaining_shards} 份报告和 ${s211RerunState.remaining_units.toLocaleString('en-US')} 个单元，当前执行 ${s211ActiveText}`}。SQLite 阶段作业状态另计（通过 / 失败 / 跳过 / 未知 / queued / leased：${['passed', 'failed', 'skipped', 'unknown', 'queued', 'leased'].map((status) => s211StageJobCounts[status] ?? 0).join(' / ')}）；阶段作业通过不代表插件单元兼容通过。单元只有六个选定阶段全部 passed 才会记为 passed；本轮所有 11,380 单元的 federated 和 compare 均 skipped，因此单元通过数为 0。${s211RerunComplete ? 'P2-AUTOMATION 受已知 Worker/平台 unknown 阻塞；P2-BEHAVIOR、P2-FIELD 未验证，详见 S2-12 审计和兼容矩阵。' : '此复跑仍在进行，P2-AUTOMATION 待完整复核，行为与真实平台投递未评估。'}`,
    '',
    `证据：[目录快照](${catalogPath})、[分类 dry run](${recipesPath})、[调度验收](${schedulerPath})、[S2-07 实际 CLI 计划](${planPath})、[S2-08 报告验收](${reportingPath})、[S2-09 baseline](${baselinePath})、[S2-09 pilot 清单](${pilotManifestPath})、[S2-09 pilot 结果](${pilotAcceptancePath})、[S2-10 baseline](${s210BaselinePath})、[S2-10 实现验证](${s210ImplementationPath})、[S2-10 smoke shard](${s210SmokePath})、[S2-10 正式 Linux shard 0](${s210LinuxFormalPath})、[S2-10 正式 Linux shard 1](${s210Linux1FormalPath})、[S2-10 正式 Linux shard 2](${s210Linux2FormalPath})、[S2-10 正式 Windows shard 0](${s210WindowsFormalPath})、[S2-10 正式 Windows shard 1](${s210Windows1FormalPath})、[Windows 首次投影 attempt](${s210WindowsAttempt1Path})、[S2-10 当前未闭合汇总](${s210InterimAggregatePath})、[S2-11 复跑 checkpoint](${s211RerunPath})、[S2-12 审计](${s212AuditPath})、[兼容矩阵](${s212MatrixPath})、[完整实施计划](docs/plans/2026-10-12-phase-2-full-catalog.md)。`,
    phase2End,
  ].join('\n');
}

export async function expectedReadme() {
  const current = await readFile(path.join(root, 'README.md'), 'utf8');
  const replace = async (source, blockStart, blockEnd, render, label) => {
    if (source.split(blockStart).length !== 2 || source.split(blockEnd).length !== 2
        || source.indexOf(blockStart) > source.indexOf(blockEnd)) {
      throw new Error(`README must contain exactly one ordered ${label} marker pair`);
    }
    return source.slice(0, source.indexOf(blockStart)) + await render()
      + source.slice(source.indexOf(blockEnd) + blockEnd.length);
  };
  let expected = await replace(current, start, end, phase1Block, 'phase-one');
  expected = await replace(expected, phase2Start, phase2End, phase2Block, 'phase-two');
  return { current, expected };
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const args = process.argv.slice(2);
    if (args.some((arg) => arg !== '--check')) throw new Error('Usage: node scripts/render-readme.mjs [--check]');
    const { current, expected } = await expectedReadme();
    if (args.includes('--check')) {
      if (current !== expected) throw new Error('README evidence block is stale; run node scripts/render-readme.mjs');
      console.log('README evidence blocks match validated phase-one and phase-two reports.');
    } else {
      await writeFile(path.join(root, 'README.md'), expected, 'utf8');
      console.log('README evidence blocks rendered from phase-one and phase-two reports.');
    }
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}
