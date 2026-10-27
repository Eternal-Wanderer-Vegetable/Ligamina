import { createHash } from 'node:crypto'
import { mkdir, readFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { dirname, resolve } from 'node:path'
import process from 'node:process'
import { pathToFileURL } from 'node:url'
import { Adapter, Bot, Context, h, Universal, version } from '@koishijs/core'
import SQLiteDriver from '@minatojs/driver-sqlite'
import { fingerprintArtifact } from './artifact_fingerprint.mjs'

const protocolWrite = process.stdout.write.bind(process.stdout)
Reflect.set(process.stdout, 'write', (...args) => process.stderr.write(...args))

async function readRequest() {
  const chunks = []
  for await (const chunk of process.stdin) chunks.push(chunk)
  return JSON.parse(Buffer.concat(chunks).toString('utf8'))
}

function waitForMiddleware(app, eventId, timeoutMs = 15_000) {
  return new Promise((resolve, reject) => {
    let timer
    const dispose = app.on('middleware', () => {
      clearTimeout(timer)
      dispose()
      resolve()
    })
    timer = setTimeout(() => {
      dispose()
      reject(new Error(`Koishi middleware did not settle for event ${eventId} within ${timeoutMs}ms`))
    }, timeoutMs)
  })
}

function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`
  }
  return JSON.stringify(value)
}

async function digest(path) {
  return createHash('sha256').update(await readFile(path)).digest('hex')
}

async function readProbeObservation(dataDirectory, events, sourcePath) {
  const path = resolve(dataDirectory, 'phase4-probes', 'observations.ndjson')
  const raw = await readFile(path)
  const rows = raw.toString('utf8').split(/\r?\n/).filter(Boolean).map((line) => JSON.parse(line))
  if (rows.length !== events.length) throw new Error('Phase 4 observer did not persist exactly one record per dispatched Koishi Session')
  if (rows.map((row) => row.event_id).join('\0') !== events.map((event) => String(event.event_id)).join('\0')) {
    throw new Error('Phase 4 observer event ids differ from the ordered Koishi stimulus')
  }
  return {
    status: 'recorded',
    storage: {
      relative_path: 'native-koishi-data/phase4-probes/observations.ndjson',
      file_sha256: createHash('sha256').update(raw).digest('hex'),
      record_count: rows.length,
      probe_source_sha256: await digest(sourcePath),
    },
    events: rows,
  }
}

function nativeElements(event) {
  return (event.message ?? []).map((segment) => {
    if (segment.type === 'text') return h.text(segment.text ?? '')
    if (segment.type === 'mention') return h.at(segment.user_id ?? '')
    if (segment.type === 'reply') return h.quote(segment.message_id ?? '')
    if (segment.type === 'image_ref') return h.image(segment.resource_ref ?? '')
    throw new TypeError(`unsupported stock Koishi stimulus segment: ${segment.type}`)
  })
}

function messageSegments(content) {
  const text = h.normalize(content).join('')
  return text ? [{ type: 'text', text }] : []
}

class NativeAdapter extends Adapter {}

class NativeBot extends Bot {
  constructor(ctx, operations, receipts, accountId, captureSourceEventId = false) {
    super(ctx, {}, 'onebot.v11')
    this.operations = operations
    this.receipts = receipts
    this.captureSourceEventId = captureSourceEventId
    this.pendingSends = []
    this.update({ selfId: accountId, user: { id: accountId, name: 'Bot' }, status: Universal.Status.OFFLINE })
  }

  sendMessage(channelId, content, referrer, options = {}) {
    const session = options.session
    const target = session?.isDirect
      ? { kind: 'private', id: String(session.userId) }
      : { kind: 'group', id: String(session?.guildId ?? channelId) }
    const operationId = `operation-${this.operations.length + 1}`
    this.operations.push({
      operation_id: operationId,
      account_id: this.selfId,
      operation: 'send_message',
      target,
      params: { message: messageSegments(content) },
      ...(this.captureSourceEventId && session?.referrer ? { source_event_id: String(session.referrer) } : {}),
    })
    const platformMessageId = `virtual-message-${this.receipts.length + 1}`
    this.receipts.push({
      operation_id: operationId,
      status: 'succeeded',
      target,
      ...(this.captureSourceEventId ? { account_id: this.selfId, source_event_id: String(session?.referrer ?? '') } : {}),
    })
    const pending = Promise.resolve([platformMessageId])
    this.pendingSends.push(pending)
    return pending
  }
}

async function main() {
  const request = await readRequest()
  const context = request.context
  const sample = context.sample
  const behaviorFixture = context.behavior_fixture
  const work = resolve(context.execution?.workspace ?? context.work_dir)
  const profileWork = resolve(context.work_dir)
  const operations = []
  const receipts = []
  const app = new Context({ nickname: ['Bot'] })
  const removeBotDisposeListeners = []
  const bots = new Map()

  const nativeDataDirectory = resolve(work, 'native-koishi-data')
  await mkdir(nativeDataDirectory, { recursive: true })
  process.env.LIGAMINA_KOISHI_DATA_DIR = nativeDataDirectory
  app.plugin(SQLiteDriver, { path: resolve(nativeDataDirectory, 'koishi.sqlite') })
  const pluginModule = await import(context.koishi_entry_url)
  const pluginConfig = behaviorFixture?.setup?.plugin_config ?? sample.fixture?.plugin_config ?? {}
  if (behaviorFixture && behaviorFixture.adapter?.id !== 'koishi.onebot.v11') {
    throw new Error('v2 fixture adapter does not match the stock Koishi runner')
  }
  app.plugin(pluginModule.default ?? pluginModule.apply ?? pluginModule, pluginConfig)
  const probeSourcePath = resolve(context.repo_root, 'lab/baselines/koishi/phase4_probe_plugin.mjs')
  if (behaviorFixture) {
    const probeModule = await import(pathToFileURL(probeSourcePath).href)
    app.plugin(probeModule.default, {})
  }
  const adapter = new NativeAdapter(app)
  const originalOn = app.on
  app.on = function (event, ...args) {
    const dispose = originalOn.call(this, event, ...args)
    if (event === 'dispose' && dispose) removeBotDisposeListeners.push(dispose)
    return dispose
  }
  const events = request.stimulus.events
  for (const accountId of [...new Set(events.map((event) => String(event.account_id)))]) {
    const bot = new NativeBot(app, operations, receipts, accountId, Boolean(behaviorFixture))
    bots.set(accountId, bot)
    adapter.fork(app, bot)
  }
  app.on = originalOn
  await app.start()
  for (const bot of bots.values()) bot.online()
  for (const [key, authority] of Object.entries(behaviorFixture?.setup?.user_authority ?? {})) {
    const [platform, userId] = key.split(':', 2)
    if (platform !== 'onebot.v11' || !userId || !Number.isInteger(authority) || authority < 0 || authority > 5) {
      throw new Error(`invalid v2 Koishi authority seed ${key}`)
    }
    await app.database.createUser(platform, userId, { authority })
  }

  try {
    for (const event of events) {
      const isPrivate = event.conversation.kind === 'private'
      const elements = nativeElements(event)
      const bot = bots.get(String(event.account_id))
      if (!bot) throw new Error(`fixture account ${event.account_id} has no native Koishi Bot`)
      const session = bot.session({
        type: 'message',
        platform: 'onebot.v11',
        selfId: String(event.account_id),
        timestamp: Date.parse(event.timestamp),
        user: { id: String(event.actor_id), name: 'Alice', nick: 'Alice' },
        channel: {
          id: String(event.conversation.id),
          type: isPrivate ? Universal.Channel.Type.DIRECT : Universal.Channel.Type.TEXT,
        },
        ...(isPrivate ? {} : { guild: { id: String(event.conversation.id) } }),
        message: {
          id: String(event.extensions?.['onebot.v11']?.message_id ?? event.event_id),
          content: elements.join(''),
          elements,
        },
        referrer: event.event_id,
      })
      const middleware = waitForMiddleware(app, event.event_id)
      await bot.dispatch(session)
      await middleware
      await new Promise((resolve) => setImmediate(resolve))
      await Promise.allSettled(bot.pendingSends.splice(0))
    }
  } finally {
    for (const remove of removeBotDisposeListeners) remove()
    for (const bot of bots.values()) await bot.dispose()
    await app.stop()
  }

  const packageRequire = createRequire(resolve(profileWork, 'package.json'))
  const runtimeVersion = process.version.replace(/^v/, '')
  const lockfileHash = await digest(context.fingerprint_paths.lockfile)
  const profileHash = await digest(context.fingerprint_paths.profile)
  const artifactHash = fingerprintArtifact(
    await readFile(context.fingerprint_paths.artifact),
    sample._artifact_hash_algorithm,
  )
  const environment = {
    ecosystem: 'koishi',
    framework_version: version,
    runtime_version: runtimeVersion,
    lockfile_sha256: lockfileHash,
    profile_sha256: profileHash,
    artifact_hash: artifactHash,
    config_sha256: createHash('sha256').update(canonical(context.config), 'utf8').digest('hex'),
    stimulus_sha256: createHash('sha256').update(canonical(request.stimulus), 'utf8').digest('hex'),
  }
  const expected = context.expected_environment
  if (canonical(environment) !== canonical(expected)) {
    throw new Error(`stock native fingerprint mismatch: expected ${canonical(expected)}, observed ${canonical(environment)}`)
  }
  if (packageRequire('@koishijs/core/package.json').version !== version) {
    throw new Error('Koishi package metadata disagrees with the imported framework version')
  }

  const report = {
    status: 'completed',
    environment,
    events,
    operations,
    operation_receipts: receipts,
    cleanup: { active_capability_count: 0, live_processes: [] },
    stock_native: { framework: 'Koishi', api: 'Context/Session/Bot', bridge_adapter_imported: false },
  }
  if (behaviorFixture) {
    report.native_trace = { bridge_adapter_imported: false }
    report.binding = context.binding
    report.capability_probe_observation = await readProbeObservation(nativeDataDirectory, events, probeSourcePath)
    report.plugin_observation = {
      events: events.map((event) => ({
        event_id: event.event_id,
        account_id: event.account_id,
        actor_id: event.actor_id,
        conversation: event.conversation,
      })),
      operations,
      receipts,
      response_or_operation_count: operations.length,
    }
  }
  protocolWrite(`${JSON.stringify(report)}\n`)
}

await main()
