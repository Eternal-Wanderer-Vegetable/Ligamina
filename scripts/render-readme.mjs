import { readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const start = '<!-- ligamina:phase1:start -->';
const end = '<!-- ligamina:phase1:end -->';
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

export async function expectedReadme() {
  const current = await readFile(path.join(root, 'README.md'), 'utf8');
  if (current.split(start).length !== 2 || current.split(end).length !== 2
      || current.indexOf(start) > current.indexOf(end)) {
    throw new Error('README must contain exactly one ordered phase-one marker pair');
  }
  const expected = current.slice(0, current.indexOf(start)) + await phase1Block()
    + current.slice(current.indexOf(end) + end.length);
  return { current, expected };
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const args = process.argv.slice(2);
    if (args.some((arg) => arg !== '--check')) throw new Error('Usage: node scripts/render-readme.mjs [--check]');
    const { current, expected } = await expectedReadme();
    if (args.includes('--check')) {
      if (current !== expected) throw new Error('README evidence block is stale; run node scripts/render-readme.mjs');
      console.log('README evidence block matches phase-one reports.');
    } else {
      await writeFile(path.join(root, 'README.md'), expected, 'utf8');
      console.log('README evidence block rendered from phase-one reports.');
    }
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}
