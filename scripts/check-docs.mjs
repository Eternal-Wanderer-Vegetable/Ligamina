import { readFile, readdir, stat } from 'node:fs/promises';
import path from 'node:path';
import { expectedReadme, root } from './render-readme.mjs';

const failures = [];
const assert = (condition, message) => { if (!condition) failures.push(message); };
const exists = async (file) => { try { await stat(file); return true; } catch { return false; } };
const read = (file) => readFile(path.join(root, file), 'utf8');
async function walk(dir) {
  const files = [];
  for (const item of await readdir(path.join(root, dir), { withFileTypes: true })) {
    const relative = path.posix.join(dir, item.name);
    if (item.isDirectory()) files.push(...await walk(relative));
    else files.push(relative);
  }
  return files;
}
const stripFences = (text) => text.replace(/^(```|~~~)[\s\S]*?^\1[^\n]*$/gm, '');
const slug = (text) => text.toLowerCase().replace(/<[^>]*>/g, '').replace(/[^\p{L}\p{N}_\-\s]/gu, '').trim().replace(/\s/g, '-');

try {
  const mandatory = ['AGENTS.md', 'README.md', 'feature_list.json', 'progress.md', 'session-handoff.md',
    'init.ps1', 'init.sh', 'skills-lock.json', '.agents/skills/harness-creator/SKILL.md', 'docs/README.md',
    'docs/evidence/README.md', 'docs/plans/2026-10-12-phase-2-full-catalog.md'];
  for (const file of mandatory) assert(await exists(path.join(root, file)), `Missing ${file}`);
  const docs = ['AGENTS.md', 'README.md', 'progress.md', 'session-handoff.md',
    ...await walk('docs'), 'hosts/nonebot/README.md', 'hosts/koishi/README.md',
    'clients/reference/README.md', 'deploy/windows/README.md', 'deploy/linux/README.md'].filter((file) => file.endsWith('.md'));
  let links = 0;
  for (const file of docs) {
    const text = await read(file);
    const lines = text.trimEnd().split(/\r?\n/).length;
    const budget = file === 'AGENTS.md' ? 100 : file === 'README.md' || file.startsWith('docs/guides/') ? 150 : null;
    if (budget) assert(lines <= budget, `${file}: ${lines} lines exceeds budget ${budget}`);
    assert(!/YYYY-MM-DD|\[feat-XXX|\[Completed item|\[Current work item/.test(text), `${file}: scaffolding placeholder remains`);
    for (const match of stripFences(text).matchAll(/!?\[[^\]\n]+\]\(([^\n)]+)\)/g)) {
      const href = match[1].replace(/^<|>$/g, '');
      if (/^[a-z][a-z\d+.-]*:/i.test(href)) continue;
      links++;
      const [relative, fragment] = href.split('#');
      const target = path.resolve(path.dirname(path.join(root, file)), decodeURIComponent(relative || path.basename(file)));
      assert(target === root || target.startsWith(root + path.sep), `${file}: link escapes repository: ${href}`);
      const found = await exists(target);
      assert(found, `${file}: broken local link ${href}`);
      if (found && fragment && target.endsWith('.md')) {
        const headings = (await readFile(target, 'utf8')).split(/\r?\n/).filter((line) => /^#{1,6} /.test(line));
        assert(headings.some((line) => slug(line.replace(/^#+ /, '')) === decodeURIComponent(fragment)), `${file}: missing anchor ${href}`);
      }
    }
  }
  const historical = (await walk('outputs')).filter((file) => file.endsWith('.md'));
  const index = await read('docs/evidence/README.md');
  for (const file of historical) assert(index.includes(`../../${file}`), `Unindexed historical Markdown: ${file}`);
  const tracker = JSON.parse(await read('feature_list.json'));
  assert(tracker.schema_version === 1 && Array.isArray(tracker.features) && tracker.features.length > 0, 'Invalid feature tracker');
  const byId = new Map();
  for (const feature of tracker.features) {
    assert(/^feat-\d+$/.test(feature.id) && !byId.has(feature.id), `Invalid/duplicate feature ID ${feature.id}`);
    assert(['not-started', 'in-progress', 'blocked', 'done'].includes(feature.status), `Invalid status ${feature.id}`);
    assert(typeof feature.name === 'string' && typeof feature.description === 'string'
      && typeof feature.done_when === 'string' && typeof feature.evidence === 'string'
      && Array.isArray(feature.dependencies) && Array.isArray(feature.evidence_paths), `Invalid feature fields ${feature.id}`);
    byId.set(feature.id, feature);
  }
  for (const feature of tracker.features) {
    for (const dependency of feature.dependencies) {
      assert(byId.has(dependency), `${feature.id}: missing dependency ${dependency}`);
      if (['done', 'in-progress'].includes(feature.status)) assert(byId.get(dependency)?.status === 'done', `${feature.id}: unsatisfied dependency ${dependency}`);
    }
    if (feature.status === 'done') {
      assert(feature.evidence.trim().length > 0 && feature.evidence_paths.length > 0, `${feature.id}: done without evidence`);
    }
    for (const evidence of feature.evidence_paths) {
      const target = path.resolve(root, evidence);
      assert(target.startsWith(root + path.sep) && await exists(target), `${feature.id}: invalid evidence path ${evidence}`);
    }
  }
  const visited = new Set();
  function visit(id, ancestors = new Set()) {
    if (ancestors.has(id)) { failures.push(`Feature dependency cycle at ${id}`); return; }
    if (visited.has(id)) return;
    const next = new Set(ancestors).add(id);
    for (const dependency of byId.get(id)?.dependencies ?? []) visit(dependency, next);
    visited.add(id);
  }
  for (const id of byId.keys()) visit(id);
  const active = tracker.features.filter((feature) => feature.status === 'in-progress');
  assert(active.length <= 1, 'More than one active feature');
  assert(tracker.active_feature === (active[0]?.id ?? null), 'active_feature disagrees with feature status');
  assert(byId.has(tracker.next_feature), 'next_feature missing from tracker');
  assert(byId.get(tracker.next_feature)?.status === 'not-started', 'next_feature must be not-started');
  const lock = JSON.parse(await read('skills-lock.json'));
  assert(lock.skills?.['harness-creator']?.source === 'walkinglabs/learn-harness-engineering', 'Wrong skill source');
  assert(lock.skills?.['harness-creator']?.computedHash?.length === 64, 'Missing skill installation hash');
  const { current, expected } = await expectedReadme();
  assert(current === expected, 'README phase-one block is stale');
  const summary = { status: failures.length ? 'failed' : 'passed', documents: docs.length, local_links: links,
    indexed_historical_markdown: historical.length, features: tracker.features.length, failures };
  console.log(JSON.stringify(summary, null, 2));
  if (failures.length) process.exitCode = 1;
} catch (error) {
  console.error(error.stack);
  process.exitCode = 1;
}
