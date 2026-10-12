import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

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
  const pilotManifestPath = 'outputs/acceptance/phase2-s209-pilot-manifest-20261012.json';
  const pilotAcceptancePath = 'outputs/acceptance/phase2-s209-20261012.json';
  const nonebotLinuxPilotPath = 'outputs/acceptance/phase2-s209-nonebot-linux-pilot-20261012.json';
  const koishiLinuxPilotPath = 'outputs/acceptance/phase2-s209-koishi-linux-pilot-20261012.json';
  const koishiWindowsPilotPath = 'outputs/acceptance/phase2-s209-koishi-windows-pilot-20261012.json';
  const planPath = 'outputs/acceptance/phase2-s207-cli-smoke-20261012.json';
  const featureListPath = 'feature_list.json';
  const [catalog, recipes, scheduler, reporting, baseline, pilotManifest, pilotAcceptance, nonebotLinuxPilot, koishiLinuxPilot, koishiWindowsPilot, plan, featureList] = await Promise.all(
    [catalogPath, recipesPath, schedulerPath, reportingPath, baselinePath, pilotManifestPath, pilotAcceptancePath, nonebotLinuxPilotPath, koishiLinuxPilotPath, koishiWindowsPilotPath, planPath, featureListPath].map(load),
  );
  for (const [relative, expected] of Object.entries(reporting.input_reports ?? {})) {
    const actual = createHash('sha256').update(await readFile(path.join(root, relative))).digest('hex');
    if (actual !== expected) throw new Error(`Stale phase-two report input: ${relative}`);
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
    throw new Error(`Invalid catalog plan report: ${planPath}`);
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
    const actual = createHash('sha256').update(await readFile(path.join(root, relative))).digest('hex');
    if (actual !== pilotAcceptance.source_hashes?.[key]) throw new Error(`Stale S2-09 evidence input: ${relative}`);
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
  const s210Label = s210Status === 'done' ? '已完成'
    : s210Status === 'in-progress' ? '进行中：baseline 与分片派发准备；全量 Worker 尚未启动'
      : '未开始：全快照 census 尚未启动';
  const measurements = pilotAcceptance.resource_measurements;

  return [
    phase2Start,
    `状态截至 ${escape(reporting.date)}：${reportingComplete ? `S2-01–S2-09 pilot 执行已完成；128 个结果位置闭合（通过 / 失败 / 跳过 / 未知：${pilotCounts}）。NoneBot Linux、Koishi Linux 和 Windows 各 30 个目录 entry 已进入 L1/L2 pilot。` : 'S2-01–S2-07 已完成；S2-08 正在进行最终验收。'} 冻结快照 \`${escape(catalog.snapshot_id)}\` 含 **${count.toLocaleString('en-US')} entries**（NoneBot 943、Koishi 4,747；官方维护子集 ${official}）。`,
    '',
    '| 任务 | 状态 |',
    '| --- | --- |',
    ...[
      ...(reportingComplete ? [['S2-01–S2-08', '已完成并分别保存验收证据']] : [['S2-01–S2-07', '已完成并分别保存验收证据'], ['S2-08', '最终验收进行中']]),
      ['S2-09', reportingComplete ? `pilot 执行完成：128/128 位置闭合；通过 / 失败 / 跳过 / 未知 ${pilotCounts}；自动化与行为 gate 保留 blocker` : '等待 S2-08 验收完成'],
      ['S2-10', s210Label],
      ['S2-11–S2-12', s210Status === 'done' ? '未开始' : '未开始'],
    ].map(([task, status]) => `| ${task} | ${status} |`),
    '',
    `分类 dry run 只建议 ${suggested}/${count} entries，${unmapped} 项仍是 \`unmapped_category\`。S2-09 的 128 个双 OS 结果位置全部闭合：四个历史支持版本和 60 个分层目录 entry 分别保留状态；NoneBot Linux 尝试 ${nonebotPilot.attempted_workers} 个 Docker Worker（L1 ${JSON.stringify(nonebotPilot.dependency_install)}；L2 ${JSON.stringify(nonebotPilot.native_load)}），Koishi Linux 尝试 ${linuxPilot.attempted_workers} 个 Docker Worker（L1 ${JSON.stringify(linuxPilot.dependency_install)}；L2 ${JSON.stringify(linuxPilot.native_load)}），Windows 尝试 ${windowsPilot.attempted_workers} 个 Sandbox Worker（L1 ${JSON.stringify(windowsPilot.dependency_install)}；L2 ${JSON.stringify(windowsPilot.native_load)}）。`,
    '',
    `Windows Sandbox 的 CPython 启动仍受 App Control（Win32 4551）阻止；Node.js 24.20.0 签名 runtime 已用于 Windows Koishi L1/L2 pilot。Koishi Windows Job Object commit P95 ${(measurements.koishi_windows_peak_worker_memory_bytes.p95 / (1024 ** 3)).toFixed(2)} GiB（8 GiB 限额内），scratch P95 ${(measurements.koishi_windows_peak_writable_scratch_bytes.p95 / (1024 ** 2)).toFixed(1)} MiB；预算仍 provisional，NoneBot Windows 无 Worker 样本。目录 cohort 缺 entry-bound behavior fixture，L4 未评估，不能外推为兼容。S2-10 状态：${s210Status}，pilot 报告标注的全量 census 状态为 ${pilotAcceptance.readiness.full_census}；真实平台登录、发送与投递验收未执行。`,
    '',
    `证据：[目录快照](${catalogPath})、[分类 dry run](${recipesPath})、[调度验收](${schedulerPath})、[S2-07 实际 CLI 计划](${planPath})、[S2-08 报告验收](${reportingPath})、[S2-09 baseline](${baselinePath})、[S2-09 pilot 清单](${pilotManifestPath})、[S2-09 pilot 结果](${pilotAcceptancePath})、[完整实施计划](docs/plans/2026-10-12-phase-2-full-catalog.md)。`,
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
