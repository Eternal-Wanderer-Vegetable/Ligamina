import { spawnSync } from 'node:child_process'
import { readFile, unlink, writeFile } from 'node:fs/promises'
import { join } from 'node:path'

const project = process.env.LIGAMINA_P404_PROJECT
const workspace = process.env.LIGAMINA_P404_WORKSPACE
const requestPath = process.env.LIGAMINA_P404_REQUEST
const resultPath = process.env.LIGAMINA_P404_RESULT
if (![project, workspace, requestPath, resultPath].every(Boolean)) {
  throw new Error('P4-04 Koishi behavior paths are not configured')
}

const request = JSON.parse(await readFile(requestPath, 'utf8'))
const runner = join(project, 'lab', 'baselines', 'koishi', 'runner.mjs')
const resolver = join(project, '.p404-resolve-plugin.mjs')
await writeFile(resolver, "console.log(import.meta.resolve('@koishijs/plugin-echo'))\n", 'utf8')
const resolved = spawnSync(process.execPath, [resolver], { cwd: project, encoding: 'utf8', windowsHide: true })
await unlink(resolver)
if (resolved.status !== 0) throw new Error(`could not resolve locked Koishi plugin: ${resolved.stderr}`)
request.context.repo_root = project
request.context.work_dir = project
request.context.koishi_entry_url = String(resolved.stdout).trim().split(/\r?\n/).at(-1)
request.context.execution.workspace = workspace
request.context.fingerprint_paths = {
  artifact: join(project, 'catalog-plugin.tgz'),
  lockfile: join(project, 'frozen-profile-lock.json'),
  profile: join(project, 'samples', 'profiles', 'koishi-group-v0.1.json'),
}

const child = spawnSync(process.execPath, [runner], {
  cwd: project,
  env: { ...process.env, LIGAMINA_KOISHI_DATA_DIR: join(workspace, 'native-koishi-data') },
  input: JSON.stringify(request),
  encoding: 'utf8',
  timeout: 180_000,
  windowsHide: true,
})
if (child.error) throw child.error
if (child.status !== 0) {
  throw new Error(`stock Koishi runner exited ${child.status}: ${(child.stderr ?? '').slice(-3000)}`)
}
const rows = String(child.stdout ?? '').split(/\r?\n/).filter(Boolean)
if (rows.length !== 1) throw new Error('stock Koishi runner did not emit exactly one protocol document')
const nativeReport = JSON.parse(rows[0])
const envelope = {
  schema_version: 1,
  kind: 'phase4_p404_koishi_native_behavior_guest_result',
  status: nativeReport.status,
  runtime_version: process.version.slice(1),
  plugin_entry_url: request.context.koishi_entry_url,
  binding: nativeReport.binding,
  environment: nativeReport.environment,
  native_report: nativeReport,
  child_stderr_sha256: null,
  child_stderr_tail: String(child.stderr ?? '').slice(-3000),
}
await writeFile(resultPath, `${JSON.stringify(envelope, null, 2)}\n`, 'utf8')
process.stdout.write(`LIGAMINA_P404_KOISHI=${JSON.stringify(envelope)}\n`)
