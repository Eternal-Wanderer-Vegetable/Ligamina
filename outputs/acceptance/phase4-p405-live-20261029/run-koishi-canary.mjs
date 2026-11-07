import { createHash, randomBytes, randomUUID } from 'node:crypto'
import { execFileSync, spawn } from 'node:child_process'
import { request as httpRequest } from 'node:http'
import { request as httpsRequest } from 'node:https'
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { createInterface } from 'node:readline'
import { setTimeout as delay } from 'node:timers/promises'
import { fileURLToPath } from 'node:url'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..')
const evidenceDir = path.dirname(fileURLToPath(import.meta.url))
const envPath = process.env.STELLA_ENV_PATH ?? (process.platform === 'win32'
  ? 'E:/stella/stella_project/StellaData/.env'
  : '/mnt/e/stella/stella_project/StellaData/.env')
const onebotBase = 'http://127.0.0.1:3000'
const image = 'ligamina-p405-koishi-profile:20261029'
const cli = process.env.LIGAMINA_CLI_PATH ?? path.join(root, 'target', 'debug', process.platform === 'win32' ? 'ligamina-cli.exe' : 'ligamina-cli')
const docker = process.env.DOCKER_CLI_PATH ?? (process.platform === 'win32' ? 'C:/Program Files/Docker/Docker/resources/bin/docker.exe' : 'docker')
const accountId = 'stella-test-account'
const hostId = 'p405-koishi-echo'
const clientId = 'p405-stella-client'
const gatewayId = 'p405-snowluma-gateway'
const runId = randomUUID()
const volumeName = `ligamina-p405-koishi-${runId}`
const hash = (value) => createHash('sha256').update(String(value), 'utf8').digest('hex')
const token = () => randomBytes(32).toString('base64url')
const hashBytes = (value) => createHash('sha256').update(value).digest('hex')

async function requestJson(url, { method = 'GET', body, bearer, timeoutMs = 10_000 } = {}) {
  const parsed = new URL(url)
  const transport = parsed.protocol === 'https:' ? httpsRequest : httpRequest
  const payload = body === undefined ? undefined : Buffer.from(JSON.stringify(body), 'utf8')
  return new Promise((resolve, reject) => {
    const req = transport(parsed, {
      method,
      headers: {
        ...(payload ? { 'content-type': 'application/json', 'content-length': String(payload.byteLength) } : {}),
        ...(bearer ? { authorization: `Bearer ${bearer}` } : {}),
      },
    }, (response) => {
      const chunks = []
      response.on('data', (chunk) => chunks.push(chunk))
      response.on('end', () => {
        const status = response.statusCode ?? 0
        let value
        try { value = JSON.parse(Buffer.concat(chunks).toString('utf8')) } catch { reject(new Error(`invalid_json_${status}`)); return }
        if (status < 200 || status >= 300) { reject(new Error(`http_${status}`)); return }
        resolve({ status, value })
      })
    })
    req.setTimeout(timeoutMs, () => req.destroy(new Error('http_timeout')))
    req.on('error', (error) => reject(new Error(error.code ?? error.message ?? 'http_error')))
    if (payload) req.write(payload)
    req.end()
  })
}

async function onebot(action, body, timeoutMs = 15_000) {
  const { value } = await requestJson(`${onebotBase}/${action}`, {
    method: 'POST', body, timeoutMs,
  })
  if (value?.status !== 'ok' || value?.retcode !== 0) {
    const errorText = [value?.wording, value?.message, value?.data?.message]
      .filter((item) => typeof item === 'string')
      .join('\n')
    const platformResult = errorText.match(/\bresult\s*=\s*(\d{1,6})\b/i)?.[1]
    const detail = platformResult ? `_platform_result_${platformResult}` : ''
    throw new Error(`onebot_${action}_retcode_${Number.isInteger(value?.retcode) ? value.retcode : 'invalid'}${detail}`)
  }
  return value.data
}

function allowedGroupsFromEnv(text) {
  const line = text.split(/\r?\n/).find((value) => /^\s*(?:export\s+)?ALLOWED_GROUPS\s*=/.test(value))
  if (!line) throw new Error('allowed_groups_missing')
  let raw = line.replace(/^\s*(?:export\s+)?ALLOWED_GROUPS\s*=\s*/, '').trim()
  if (raw.length >= 2 && ((raw.startsWith('"') && raw.endsWith('"')) || (raw.startsWith("'") && raw.endsWith("'")))) raw = raw.slice(1, -1)
  const values = raw.startsWith('[') ? JSON.parse(raw) : raw.split(/[;,]/).map((item) => item.trim()).filter(Boolean)
  if (!Array.isArray(values) || values.length === 0) throw new Error('allowed_groups_empty')
  return [...new Set(values.map(String))]
}

async function reserveLoopbackPort() {
  for (let attempt = 0; attempt < 20; attempt++) {
    const port = 26_000 + Math.floor(Math.random() * 20_000)
    const { createServer } = await import('node:net')
    const server = createServer()
    try {
      await new Promise((resolve, reject) => {
        server.once('error', reject)
        server.listen(port, '127.0.0.1', resolve)
      })
      await new Promise((resolve) => server.close(resolve))
      return port
    } catch {
      await new Promise((resolve) => server.close(resolve))
    }
  }
  throw new Error('loopback_port_unavailable')
}

async function waitForDaemon(child, lines, timeoutMs = 90_000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('daemon_start_timeout')), timeoutMs)
    const exit = (code) => reject(new Error(`daemon_exit_${code ?? 'unknown'}`))
    child.once('exit', exit)
    lines.on('line', (line) => {
      try {
        const value = JSON.parse(line)
        if (value?.ready === true) {
          clearTimeout(timer)
          child.off('exit', exit)
          resolve(value)
        }
      } catch { /* Core writes only one readiness JSON line here. */ }
    })
  })
}

async function pollUntil(read, predicate, timeoutMs = 45_000, intervalMs = 200) {
  const until = Date.now() + timeoutMs
  let last
  while (Date.now() < until) {
    last = await read()
    if (predicate(last)) return last
    await delay(intervalMs)
  }
  return last
}

async function waitForProcessExit(child, timeoutMs) {
  if (child.exitCode !== null || child.signalCode !== null) return true
  return new Promise((resolve) => {
    const timer = setTimeout(() => { child.off('exit', onExit); resolve(false) }, timeoutMs)
    const onExit = () => { clearTimeout(timer); resolve(true) }
    child.once('exit', onExit)
  })
}

function processIsAlive(pid) {
  if (!pid) return false
  try { process.kill(pid, 0); return true } catch (error) { return error?.code !== 'ESRCH' }
}

async function verifyMessage(messageId, marker) {
  const until = Date.now() + 12_000
  let lastError = 'message_not_observed'
  while (Date.now() < until) {
    try {
      const data = await onebot('get_msg', { message_id: messageId }, 5_000)
      const segments = Array.isArray(data?.message) ? data.message : []
      const text = segments.map((segment) => segment?.data?.text ?? segment?.text ?? '').join('')
      const rawMessage = typeof data?.raw_message === 'string' ? data.raw_message : ''
      if (text.includes(marker) || rawMessage.includes(marker)) return { observed: true, content_matches: true }
      lastError = 'message_content_mismatch'
    } catch (error) {
      lastError = error instanceof Error ? error.message : 'message_read_error'
    }
    await delay(750)
  }
  return { observed: false, content_matches: false, reason: lastError }
}

const report = {
  schema_version: 1,
  kind: 'phase4_p405_koishi_live_outbound_canary',
  created_at: new Date().toISOString(),
  run_id: runId,
  ingress: { source: 'synthetic_event_envelope', real_platform_ingress_observed: false },
  candidate: {
    registry_entry_id: 'ko-163c3bd4cf87c72c24210251',
    package: '@koishijs/plugin-echo',
    version: '2.2.5',
    artifact_sha256: '79da39692ba8fce04ea597ff85696a0fe193cd89a95a91a0d459ea40ab592265',
    profile_lock_sha256: '33e0897b794134c39215e7824a47edec3eb4acc79fcb9f4df00a03c1455f616b',
    host_provider_lock_sha256: '5e3df82b0c353a8ded6c32708a8a591d9854182a38c6af9924c3e2671862bbcb',
  },
  containment: {
    docker_image: image,
    worker_network: 'none',
    worker_root_filesystem: 'read_only',
    capability_drop: 'ALL',
    no_new_privileges: true,
    worker_user: '10001:10001',
    install_scripts: 'disabled',
  },
  scenario: { id: 'positive-group-echo-canary', sends_requested: 1 },
  result: 'not_run',
  cleanup: { daemon_stopped: true, labeled_worker_containers_remaining: null, koishi_volume_removed: false },
}

let tempDir
let daemon
let daemonLines
let gatewayThread
let gatewayStop = false
let lease
let coreUrl
let adminToken
let clientToken
let gatewayRegistrationToken
let targetGroupId
let expectedText
let errorCode
let apiSendCount = 0

try {
  const [envText, artifact] = await Promise.all([
    readFile(envPath, 'utf8'),
    readFile(path.join(evidenceDir, 'koishi-profile-context', 'plugin-echo-2.2.5.tgz')),
  ])
  if (hashBytes(artifact) !== report.candidate.artifact_sha256) throw new Error('candidate_artifact_hash_mismatch')
  const allowed = new Set(allowedGroupsFromEnv(envText))
  const self = await onebot('get_login_info', {})
  const platformGroups = await onebot('get_group_list', {}, 20_000)
  const matches = platformGroups.filter((group) => group && allowed.has(String(group.group_id)))
  if (matches.length === 0) throw new Error('no_stella_configured_group_is_joined')
  targetGroupId = String(matches[0].group_id)
  report.scope = {
    account_fingerprint: hash(self.user_id).slice(0, 16),
    configured_group_count: allowed.size,
    joined_group_count: platformGroups.length,
    matching_group_count: matches.length,
    selected_group_ordinal: 1,
    selected_group_fingerprint: hash(targetGroupId).slice(0, 16),
  }
  report.profile_assets = {
    profile_package_lock_sha256: hashBytes(await readFile(path.join(evidenceDir, 'koishi-profile-context', 'package-lock.json'))),
    original_offline_cache_sha256: hashBytes(await readFile(path.join(evidenceDir, 'koishi-profile-context', 'npm-cache.tar.gz'))),
    supplemental_offline_cache_sha256: hashBytes(await readFile(path.join(evidenceDir, 'koishi-profile-context', 'npm-cache-supplement.tar.gz'))),
  }
  report.platform = { gateway: 'SnowLuma OneBot HTTP', runtime_version: '1.14.19' }
  expectedText = `[Ligamina P4-05 TEST] ${randomUUID()}`
  report.scenario.marker = expectedText
  tempDir = await mkdtemp(path.join(os.tmpdir(), 'ligamina-p405-live-'))
  execFileSync(docker, ['volume', 'create', '--label', `io.ligamina.p405.run=${runId}`, volumeName], { encoding: 'utf8' })
  const seedScript = `const { createRequire } = require('node:module')
const fs = require('node:fs')
const requireFromHost = createRequire('/opt/ligamina/hosts/koishi/package.json')
const { Context } = requireFromHost('@koishijs/core')
const driverModule = requireFromHost('@minatojs/driver-sqlite')
const SQLiteDriver = driverModule.default ?? driverModule
;(async () => {
  const directory = '/var/lib/ligamina/koishi'
  const database = directory + '/koishi.sqlite'
  const app = new Context()
  app.plugin(SQLiteDriver, { path: database })
  await app.start()
  try {
    await app.database.createUser('onebot.v11', 'p405-synthetic-actor', { authority: 3 })
    const user = await app.database.getUser('onebot.v11', 'p405-synthetic-actor', ['authority'])
    if (user?.authority !== 3) throw new Error('authority_seed_not_observed')
  } finally { await app.stop() }
  fs.chmodSync(directory, 0o777)
  fs.chmodSync(database, 0o666)
  console.log(JSON.stringify({ authority_seeded: true, authority: 3 }))
})().catch((error) => { console.error(error.name); process.exitCode = 1 })`
  execFileSync(docker, [
    'run', '--rm', '--network', 'none', '--read-only',
    '--tmpfs', '/tmp:rw,nosuid,nodev,noexec,size=64m',
    '--cap-drop=ALL', '--security-opt=no-new-privileges', '--pids-limit=64',
    '--memory=512m', '--cpus=1', '--user=0:0',
    '--volume', `${volumeName}:/var/lib/ligamina/koishi`,
    '--workdir', '/opt/ligamina/p405-koishi-profile',
    '--entrypoint', '/usr/local/bin/node', image, '-e', seedScript,
  ], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] })
  report.host_setup = { fresh_sqlite_volume: true, synthetic_actor_authority: 3, authority_seed_verified: true }
  const port = await reserveLoopbackPort()
  coreUrl = `http://127.0.0.1:${port}`
  adminToken = token()
  clientToken = token()
  gatewayRegistrationToken = token()
  const clientReadGrant = {
    id: 'p405-client-operation-read',
    account_id: accountId,
    operations: ['send_message'],
    targets: [{ kind: 'group', id: targetGroupId }],
  }
  const hostSendGrant = {
    id: 'p405-host-exact-group-send',
    account_id: accountId,
    operations: ['send_message'],
    targets: [{ kind: 'group', id: targetGroupId }],
  }
  const containerArgs = [
    'run', '--rm', '-i',
    '--label', `io.ligamina.p405.run=${runId}`,
    '--network', 'none', '--read-only',
    '--tmpfs', '/tmp:rw,nosuid,nodev,noexec,size=64m',
    '--volume', `${volumeName}:/var/lib/ligamina/koishi`,
    '--cap-drop=ALL', '--security-opt=no-new-privileges', '--pids-limit=64',
    '--memory=512m', '--cpus=1', '--user=10001:10001',
    '-e', 'LIGAMINA_KOISHI_PLUGINS=file:///opt/ligamina/p405-koishi-profile/node_modules/@koishijs/plugin-echo/lib/index.js',
    '-e', 'LIGAMINA_KOISHI_DATA_DIR=/var/lib/ligamina/koishi',
    '-e', 'LIGAMINA_KOISHI_NICKNAMES=Bot',
    '--entrypoint', '/usr/local/bin/node', image,
    '/opt/ligamina/hosts/koishi/dist/worker.js',
  ]
  const config = {
    listen: `127.0.0.1:${port}`,
    allow_non_loopback_listen: false,
    database_path: path.join(tempDir, 'core.sqlite'),
    admin_token: adminToken,
    mcp_client_id: clientId,
    clients: [{ id: clientId, token: clientToken, capabilities: [], operation_grants: [clientReadGrant] }],
    gateways: [{ id: gatewayId, client_id: clientId, account_id: accountId, registration_token: gatewayRegistrationToken }],
    hosts: [{
      id: hostId, enabled: true, program: docker, args: containerArgs, current_dir: root,
      environment: {}, max_restarts: 1, restart_backoff_ms: 500, startup_timeout_ms: 60_000,
      shutdown_timeout_ms: 10_000, operation_grants: [{ ...hostSendGrant, client_id: clientId }],
    }],
  }
  const configPath = path.join(tempDir, 'daemon.json')
  await writeFile(configPath, JSON.stringify(config), { mode: 0o600, flag: 'wx' })
  const imageId = execFileSync(docker, ['image', 'inspect', '--format', '{{.Id}}', image], { encoding: 'utf8' }).trim()
  report.containment.docker_image_id = imageId

  daemon = spawn(cli, ['daemon', '--config', configPath], { cwd: root, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] })
  daemonLines = createInterface({ input: daemon.stdout })
  daemon.stderr.on('data', () => {})
  await waitForDaemon(daemon, daemonLines)
  report.core = { ready: true, listen_scope: 'loopback_only' }

  const leaseResponse = await requestJson(`${coreUrl}/v1/gateways/${gatewayId}/lease`, {
    method: 'POST', bearer: gatewayRegistrationToken, body: { ttl_ms: 300_000 },
  })
  lease = leaseResponse.value
  if (!lease?.token || !lease?.lease_id || !lease?.core_epoch) throw new Error('gateway_lease_invalid')

  const runGateway = async () => {
    while (!gatewayStop) {
      let claim
      try {
        const response = await requestJson(`${coreUrl}/v1/gateways/${gatewayId}/actions/claim`, {
          method: 'POST', bearer: lease.token, body: lease, timeoutMs: 5_000,
        })
        claim = response.value
      } catch (error) {
        if (!gatewayStop) report.gateway_last_error = error instanceof Error ? error.message : 'claim_error'
        await delay(250)
        continue
      }
      if (!claim) { await delay(200); continue }
      const operation = claim.operation_payload
      const segments = operation?.params?.message
      const allowedPayload = operation?.operation === 'send_message'
        && operation?.target?.kind === 'group'
        && operation?.target?.id === targetGroupId
        && Array.isArray(segments) && segments.length === 1
        && segments[0]?.type === 'text' && segments[0]?.text === expectedText
      if (!allowedPayload || apiSendCount !== 0) {
        report.gateway_rejected_unexpected_action = true
        try {
          await requestJson(`${coreUrl}/v1/gateways/${gatewayId}/actions/${claim.key.operation_id}/result`, {
            method: 'POST', bearer: lease.token, body: {
              credential: lease,
              result: {
                key: claim.key, fingerprint: claim.fingerprint, status: 'failed',
                platform_message_id: null, data: null, error: 'out_of_scope_or_unexpected_canary_action',
                observed_at_ms: Date.now(), evidence_ref: 'phase4-p405-live-20261029',
              },
            }, timeoutMs: 5_000,
          })
        } catch { report.gateway_unexpected_action_receipt = 'not_confirmed' }
        continue
      }

      apiSendCount += 1
      report.correlation = {
        event_id: report.correlation?.event_id,
        trace_id: claim.trace_id,
        operation_id: claim.key.operation_id,
        operation_fingerprint: claim.fingerprint,
        target_matches_exact_grant: true,
        outgoing_content_sha256: hash(segments[0].text),
      }
      let sendData
      try {
        sendData = await onebot('send_group_msg', {
          group_id: targetGroupId,
          message: [{ type: 'text', data: { text: segments[0].text } }],
        }, 10_000)
      } catch (error) {
        report.platform_send = { api_accepted: false, outcome: 'failed_or_uncertain', error_code: error instanceof Error ? error.message : 'onebot_send_error' }
        try {
          await requestJson(`${coreUrl}/v1/gateways/${gatewayId}/actions/${claim.key.operation_id}/result`, {
            method: 'POST', bearer: lease.token, body: {
              credential: lease,
              result: {
                key: claim.key, fingerprint: claim.fingerprint, status: 'unknown',
                platform_message_id: null, data: null, error: 'OneBot send result was not confirmed; no retry issued',
                observed_at_ms: Date.now(), evidence_ref: 'phase4-p405-live-20261029',
              },
            }, timeoutMs: 5_000,
          })
        } catch { report.platform_send.gateway_unknown_receipt = 'not_confirmed' }
        return
      }

      const messageId = sendData?.message_id ?? sendData?.messageId
      if (messageId === undefined || messageId === null) {
        report.platform_send = { api_accepted: true, outcome: 'accepted_without_message_id' }
        return
      }
      report.platform_send = {
        api_accepted: true,
        gateway_receipt_sent: false,
        platform_message_id_fingerprint: hash(messageId).slice(0, 16),
      }
      try {
        const result = await requestJson(`${coreUrl}/v1/gateways/${gatewayId}/actions/${claim.key.operation_id}/result`, {
          method: 'POST', bearer: lease.token, body: {
            credential: lease,
            result: {
              key: claim.key, fingerprint: claim.fingerprint, status: 'succeeded',
              platform_message_id: String(messageId), data: null, error: null,
              observed_at_ms: Date.now(), evidence_ref: 'phase4-p405-live-20261029',
            },
          }, timeoutMs: 8_000,
        })
        report.platform_send.gateway_receipt_sent = true
        report.correlation.gateway_receipt_status = result.value?.status ?? 'recorded'
      } catch (error) {
        report.platform_send.gateway_receipt_error = error instanceof Error ? error.message : 'receipt_error'
      }
      report.platform_observation = await verifyMessage(messageId, expectedText)
      if (report.platform_observation.observed) {
        report.platform_observation.message_id_fingerprint = hash(messageId).slice(0, 16)
      }
      return
    }
  }
  gatewayThread = runGateway()

  const eventId = randomUUID()
  const traceId = randomUUID()
  report.correlation = { event_id: eventId, trace_id: traceId }
  const event = {
    schema_version: 'ligamina/0.1',
    event_id: eventId,
    trace_id: traceId,
    platform: 'onebot.v11',
    account_id: accountId,
    conversation: { kind: 'group', id: targetGroupId },
    actor_id: 'p405-synthetic-actor',
    timestamp: new Date().toISOString(),
    target_host: hostId,
    message: [{ type: 'text', text: `echo ${expectedText}` }],
    extensions: { 'onebot.v11': { message_id: `p405-synthetic-${eventId}`, sender: { nickname: 'P4 fixture' } } },
  }
  const submitted = await requestJson(`${coreUrl}/v1/events`, { method: 'POST', bearer: clientToken, body: event })
  report.event = {
    api_accepted: submitted.status === 202,
    disposition: submitted.value?.disposition,
    core_receipt_fingerprint: submitted.value?.event?.fingerprint,
    ingress_is_synthetic: true,
  }
  report.authorization = {
    host_grant_id: hostSendGrant.id,
    client_read_grant_id: clientReadGrant.id,
    grant_ids_are_distinct: hostSendGrant.id !== clientReadGrant.id,
    both_targets_are_exact_group_scope: true,
  }

  const eventQuery = new URLSearchParams({
    platform: event.platform,
    account_id: accountId,
    conversation_kind: 'group',
    conversation_id: targetGroupId,
  })
  const finalEvent = await pollUntil(async () => (await requestJson(
    `${coreUrl}/v1/events/${eventId}?${eventQuery}`,
    { bearer: clientToken, timeoutMs: 5_000 },
  )).value, (value) => ['completed', 'failed', 'unknown'].includes(value?.status), 60_000)
  report.event.status = finalEvent?.status ?? 'not_terminal_before_timeout'
  report.event.error_present = Boolean(finalEvent?.error)
  if (report.correlation?.operation_id) {
    const opQuery = new URLSearchParams({ account_id: accountId, operation: 'send_message' })
    const operation = await requestJson(
      `${coreUrl}/v1/operations/${report.correlation.operation_id}?${opQuery}`,
      { bearer: clientToken, timeoutMs: 5_000 },
    )
    report.operation = {
      status: operation.value?.status,
      host_epoch: operation.value?.host_epoch,
      core_epoch_created: operation.value?.core_epoch_created,
      receipt_status: operation.value?.result?.status,
    }
  }
  report.result = report.event.status === 'completed'
    && report.operation?.status === 'succeeded'
    && report.platform_send?.api_accepted === true
    && report.platform_send?.gateway_receipt_sent === true
    && report.platform_observation?.observed === true
    && report.platform_observation?.content_matches === true
    ? 'passed_scoped_outbound_canary'
    : 'failed_or_incomplete'
} catch (error) {
  if (typeof error?.code === 'string' && /^[A-Z0-9_:-]{1,80}$/i.test(error.code)) errorCode = error.code
  else if (Number.isInteger(error?.status)) errorCode = `subprocess_exit_${error.status}`
  else if (error instanceof Error && /^[a-z0-9_:-]{1,80}$/i.test(error.message)) errorCode = error.message
  else errorCode = error instanceof Error ? error.name : 'unknown_error'
  report.result = 'failed_or_incomplete'
} finally {
  gatewayStop = true
  if (gatewayThread) await Promise.race([gatewayThread, delay(7_000)])
  if (daemon) {
    if (processIsAlive(daemon.pid)) {
      try {
        if (coreUrl && adminToken) await requestJson(`${coreUrl}/v1/shutdown`, { method: 'POST', bearer: adminToken, timeoutMs: 3_000 })
      } catch { /* Continue to exact-process cleanup if graceful shutdown is unavailable. */ }
      await waitForProcessExit(daemon, 8_000)
      if (processIsAlive(daemon.pid)) {
        daemon.kill()
        await waitForProcessExit(daemon, 3_000)
      }
    }
    report.cleanup.daemon_stopped = !processIsAlive(daemon.pid)
  }
  try {
    const remaining = execFileSync(docker, ['ps', '-aq', '--filter', `label=io.ligamina.p405.run=${runId}`], { encoding: 'utf8' })
      .split(/\r?\n/).map((value) => value.trim()).filter(Boolean)
    report.cleanup.labeled_worker_containers_remaining = remaining.length
    if (remaining.length) execFileSync(docker, ['rm', '-f', ...remaining], { stdio: 'ignore' })
  } catch { report.cleanup.labeled_worker_containers_remaining = 'not_verified' }
  try {
    const volumes = JSON.parse(execFileSync(docker, ['volume', 'inspect', volumeName], { encoding: 'utf8' }))
    if (volumes[0]?.Labels?.['io.ligamina.p405.run'] === runId) {
      execFileSync(docker, ['volume', 'rm', volumeName], { encoding: 'utf8' })
      report.cleanup.koishi_volume_removed = true
    }
  } catch { report.cleanup.koishi_volume_removed = false }
  if (tempDir) await rm(tempDir, { recursive: true, force: true })
  if (daemonLines) daemonLines.close()
}

if (errorCode) report.error_code = errorCode
const outputName = `phase4-p405-koishi-live-canary-20261029-${runId.slice(0, 8)}.json`
const outputPath = path.join(evidenceDir, outputName)
const serialized = `${JSON.stringify(report, null, 2)}\n`
await writeFile(outputPath, serialized, { mode: 0o600 })
await writeFile(`${outputPath}.sha256`, `${hash(serialized)}  ${outputName}\n`, { mode: 0o600 })
console.log(JSON.stringify({
  result: report.result,
  event_status: report.event?.status ?? 'not_submitted',
  operation_status: report.operation?.status ?? 'not_observed',
  gateway_api_accepted: report.platform_send?.api_accepted ?? false,
  receiver_observed: report.platform_observation?.observed ?? false,
  group_fingerprint: report.scope?.selected_group_fingerprint ?? null,
  cleanup: report.cleanup,
  evidence: outputName,
  error_code: report.error_code ?? null,
}))
if (report.result !== 'passed_scoped_outbound_canary') process.exitCode = 1
