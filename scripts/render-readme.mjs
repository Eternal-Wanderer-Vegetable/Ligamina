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
  const planPath = 'outputs/acceptance/phase2-s207-cli-smoke-20261012.json';
  const [catalog, recipes, scheduler, reporting, baseline, plan] = await Promise.all(
    [catalogPath, recipesPath, schedulerPath, reportingPath, baselinePath, planPath].map(load),
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

  return [
    phase2Start,
    `状态截至 ${escape(reporting.date)}：${reportingComplete ? 'S2-01–S2-08 已完成；S2-09 双平台 pilot 正在进行（实施前 baseline 已通过，插件尚未派发）。' : 'S2-01–S2-07 已完成；S2-08 正在进行最终验收。'} 冻结快照 \`${escape(catalog.snapshot_id)}\` 含 **${count.toLocaleString('en-US')} entries**（NoneBot 943、Koishi 4,747；官方维护子集 ${official}）。`,
    '',
    '| 任务 | 状态 |',
    '| --- | --- |',
    ...[
      ...(reportingComplete ? [['S2-01–S2-08', '已完成并分别保存验收证据']] : [['S2-01–S2-07', '已完成并分别保存验收证据'], ['S2-08', '最终验收进行中']]),
      ['S2-09', reportingComplete ? '进行中：baseline 通过；pilot 尚未派发' : '等待 S2-08 验收完成'],
      ['S2-10', '未开始：全快照 census 尚未启动'],
      ['S2-11–S2-12', '未开始'],
    ].map(([task, status]) => `| ${task} | ${status} |`),
    '',
    `分类 dry run 只建议 ${suggested}/${count} entries，${unmapped} 项仍是 \`unmapped_category\`。S2-07 在 Linux shard 规划了 3 个单元、18 个 stage jobs；队列仍为 18 queued，\`execution_started=false\`，**没有派发目录插件**。`,
    '',
    'Windows Sandbox 的 CPython 启动仍受 App Control（Win32 4551）阻止；Windows 插件 pilot 需要先解决该运行前置。S2-05 的四个历史精确版本回归不外推到目录全集；真实平台登录、发送与投递验收未执行。',
    '',
    `证据：[目录快照](${catalogPath})、[分类 dry run](${recipesPath})、[调度验收](${schedulerPath})、[S2-07 实际 CLI 计划](${planPath})、[S2-08 报告验收](${reportingPath})、[S2-09 baseline](${baselinePath})、[完整实施计划](docs/plans/2026-10-12-phase-2-full-catalog.md)。`,
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
