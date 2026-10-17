import { createHash } from 'node:crypto'
import { mkdir, readFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { dirname, resolve } from 'node:path'
import process from 'node:process'
import { Adapter, Bot, Context, h, Universal, version } from '@koishijs/core'
import SQLiteDriver from '@minatojs/driver-sqlite'

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
  constructor(ctx, operations, receipts, accountId) {
    super(ctx, {}, 'onebot.v11')
    this.operations = operations
    this.receipts = receipts
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
    })
    const platformMessageId = `virtual-message-${this.receipts.length + 1}`
    this.receipts.push({
      operation_id: operationId,
      status: 'succeeded',
      target,
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
  const work = resolve(context.work_dir)
  const operations = []
  const receipts = []
  const app = new Context({ nickname: ['Bot'] })
  let removeBotDisposeListener
  let bot

  await mkdir(resolve(work, 'native-koishi-data'), { recursive: true })
  app.plugin(SQLiteDriver, { path: resolve(work, 'native-koishi-data', 'koishi.sqlite') })
  const pluginModule = await import(context.koishi_entry_url)
  app.plugin(pluginModule.default ?? pluginModule.apply ?? pluginModule, sample.fixture?.plugin_config ?? {})
  const adapter = new NativeAdapter(app)
  const originalOn = app.on
  app.on = function (event, ...args) {
    const dispose = originalOn.call(this, event, ...args)
    if (event === 'dispose' && !removeBotDisposeListener) removeBotDisposeListener = dispose
    return dispose
  }
  try {
    bot = new NativeBot(app, operations, receipts, String(context.account_id ?? '9000'))
  } finally {
    app.on = originalOn
  }
  adapter.fork(app, bot)
  await app.start()
  bot.online()

  try {
    for (const event of request.stimulus.events) {
      const isPrivate = event.conversation.kind === 'private'
      const elements = nativeElements(event)
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
    removeBotDisposeListener?.()
    await bot?.dispose()
    await app.stop()
  }

  const packageRequire = createRequire(resolve(work, 'package.json'))
  const runtimeVersion = process.version.replace(/^v/, '')
  const lockfileHash = await digest(context.fingerprint_paths.lockfile)
  const profileHash = await digest(context.fingerprint_paths.profile)
  const artifactHash = createHash('sha512').update(await readFile(context.fingerprint_paths.artifact)).digest('base64')
  const environment = {
    ecosystem: 'koishi',
    framework_version: version,
    runtime_version: runtimeVersion,
    lockfile_sha256: lockfileHash,
    profile_sha256: profileHash,
    artifact_hash: `sha512-${artifactHash}`,
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
    events: request.stimulus.events,
    operations,
    operation_receipts: receipts,
    cleanup: { active_capability_count: 0, live_processes: [] },
    stock_native: { framework: 'Koishi', api: 'Context/Session/Bot', bridge_adapter_imported: false },
  }
  protocolWrite(`${JSON.stringify(report)}\n`)
}

await main()
