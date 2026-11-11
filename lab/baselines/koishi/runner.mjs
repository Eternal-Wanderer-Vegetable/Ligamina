import { createHash } from 'node:crypto'
import { mkdir, readFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { dirname, resolve } from 'node:path'
import process from 'node:process'
import { pathToFileURL } from 'node:url'
import { Adapter, Bot, Context, h, Universal, version } from '@koishijs/core'
import SQLiteDriver from '@minatojs/driver-sqlite'
import { fingerprintArtifact } from './artifact_fingerprint.mjs'
import { resolvePluginExport } from './plugin_export.mjs'

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

function registerPluginModule(app, module, label, config = {}) {
  const plugin = resolvePluginExport(module, label)
  try {
    app.plugin(plugin, config)
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error)
    throw new Error(`Koishi rejected plugin ${label}: ${detail}`)
  }
}

function traceProfileStage(context, stage) {
  if (context.execution?.profile_stage_trace === true) {
    process.stderr.write(`[P7-08-STAGE] ${stage}\n`)
  }
}

// Independent stock-framework inventory used by the P7 Native baseline. This
// intentionally reads Koishi's registered command objects instead of importing
// Ligamina Host inventory code.
function nativeCommandDescriptors(app) {
  const commands = app?.$commander?._commandList ?? []
  const descriptors = []
  for (const command of commands) {
    if (descriptors.length >= 512) break
    const name = typeof command?.name === 'string' ? command.name : ''
    if (!name || /\s/.test(name)) continue
    const descriptor = { command_id: name, invocation: name, framework: 'koishi' }
    const declaration = typeof command.declaration === 'string' ? command.declaration.trim() : ''
    const optionSyntax = Object.values(command._options ?? {})
      .map((option) => typeof option?.syntax === 'string' ? `[${option.syntax}]` : '')
      .filter(Boolean)
    const usage = [name, declaration, ...optionSyntax].filter(Boolean).join(' ')
    if (usage) descriptor.usage = usage.slice(0, 1024)
    const aliases = Object.keys(command._aliases ?? {}).filter((alias) => alias !== name && alias.trim())
    if (aliases.length) descriptor.aliases = aliases.slice(0, 64).map((alias) => alias.slice(0, 256))
    const args = (Array.isArray(command._arguments) ? command._arguments : [])
      .filter((argument) => typeof argument?.name === 'string' && argument.name)
      .slice(0, 64)
      .map((argument) => ({
        name: argument.name.slice(0, 128),
        required: argument.required === true,
        ...(argument.variadic === true ? { variadic: true } : {}),
        ...(typeof argument.type === 'string' ? { type: argument.type.slice(0, 64) } : {}),
      }))
    if (args.length) descriptor.arguments = args
    const options = Object.values(command._options ?? {})
      .filter((option) => typeof option?.name === 'string' && option.name)
      .slice(0, 64)
      .map((option) => ({
        name: option.name.slice(0, 128),
        ...(typeof option.syntax === 'string' ? { syntax: option.syntax.slice(0, 256) } : {}),
        ...(typeof option.type === 'string' ? { type: option.type.slice(0, 64) } : {}),
      }))
    if (options.length) descriptor.options = options
    descriptors.push(descriptor)
  }
  return descriptors.sort((left, right) => left.command_id.localeCompare(right.command_id))
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
  const captureSourceEventId = Boolean(behaviorFixture || context.execution?.capture_source_event_id)

  const nativeDataDirectory = resolve(work, 'native-koishi-data')
  await mkdir(nativeDataDirectory, { recursive: true })
  process.env.LIGAMINA_KOISHI_DATA_DIR = nativeDataDirectory
  const serviceProfile = context.service_profile ?? {}
  if (serviceProfile.database_provider) {
    traceProfileStage(context, 'database-provider-register-start')
    if (!context.database_provider_url) throw new Error('Koishi service profile is missing its locked database provider URL')
    const databaseModule = await import(context.database_provider_url)
    registerPluginModule(app, databaseModule, serviceProfile.database_provider.package ?? 'database provider')
    traceProfileStage(context, 'database-provider-register-complete')
  } else {
    app.plugin(SQLiteDriver, { path: resolve(nativeDataDirectory, 'koishi.sqlite') })
    traceProfileStage(context, 'sqlite-provider-register-complete')
  }
  const providerLabels = (serviceProfile.providers ?? [])
    .filter((provider) => provider.package !== serviceProfile.database_provider?.package)
    .map((provider) => provider.package)
  for (const [index, providerUrl] of (context.provider_urls ?? []).entries()) {
    traceProfileStage(context, `service-provider-register-start:${providerLabels[index] ?? index + 1}`)
    const providerModule = await import(providerUrl)
    registerPluginModule(app, providerModule, providerLabels[index] ?? `service provider ${index + 1}`)
    traceProfileStage(context, `service-provider-register-complete:${providerLabels[index] ?? index + 1}`)
  }
  const pluginModule = await import(context.koishi_entry_url)
  const pluginConfig = behaviorFixture?.setup?.plugin_config ?? sample.fixture?.plugin_config ?? {}
  if (behaviorFixture && behaviorFixture.adapter?.id !== 'koishi.onebot.v11') {
    throw new Error('v2 fixture adapter does not match the stock Koishi runner')
  }
  registerPluginModule(app, pluginModule, sample.package, pluginConfig)
  traceProfileStage(context, 'sample-plugin-register-complete')
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
  const startupAccounts = context.execution?.startup_accounts ?? []
  if (!Array.isArray(startupAccounts) || startupAccounts.some((account) => (
    !account || typeof account.account_id !== 'string' || !account.account_id.startsWith('p7-offline-')
    || !Array.isArray(account.guild_ids) || account.guild_ids.some((guildId) => (
      typeof guildId !== 'string' || !guildId.startsWith('p7-offline-')
    ))
  ))) {
    throw new TypeError('Koishi startup fixture must use synthetic offline account and group ids')
  }
  const startupAccountById = new Map(startupAccounts.map((account) => [account.account_id, account]))
  const accountIds = new Set([
    ...events.map((event) => String(event.account_id)),
    ...startupAccounts.map((account) => account.account_id),
  ])
  for (const accountId of [...accountIds]) {
    const bot = new NativeBot(app, operations, receipts, accountId, captureSourceEventId)
    const startupAccount = startupAccountById.get(accountId)
    if (startupAccount) {
      bot.internal = {
        getGuildList: async () => ({
          data: startupAccount.guild_ids.map((guildId) => ({ guildId })),
        }),
      }
    }
    bots.set(accountId, bot)
    adapter.fork(app, bot)
  }
  app.on = originalOn
  traceProfileStage(context, 'app-start-begin')
  await app.start()
  traceProfileStage(context, 'app-start-complete')
  for (const bot of bots.values()) bot.online()
  for (const [key, authority] of Object.entries(behaviorFixture?.setup?.user_authority ?? {})) {
    const [platform, userId] = key.split(':', 2)
    if (platform !== 'onebot.v11' || !userId || !Number.isInteger(authority) || authority < 0 || authority > 5) {
      throw new Error(`invalid v2 Koishi authority seed ${key}`)
    }
    await app.database.createUser(platform, userId, { authority })
  }

  let runtimeCommands = []
  let runtimeServices = []
  let runtimePlugins = [...(sample.preload_plugins ?? [])]
  if (context.service_profile) runtimePlugins.push(sample.package)
  let runtimeInventory = null
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
    // Capture while plugins are still registered. app.stop() disposes plugins
    // and may clear Koishi's command registry before the report is assembled.
    runtimeCommands = nativeCommandDescriptors(app)
    runtimeServices = Object.keys(app[Context.internal] ?? {}).sort()
    const missingServices = (context.execution?.required_services ?? []).filter((service) => !runtimeServices.includes(service))
    if (missingServices.length) throw new Error(`Koishi offline service profile did not register: ${missingServices.join(', ')}`)
    runtimeInventory = {
      ecosystem: 'koishi',
      capabilities: { services: { status: 'observed', items: runtimeServices.map((name) => ({ kind: 'provided_service', name })) } },
    }
    traceProfileStage(context, 'runtime-service-enumeration-complete')
  } finally {
    traceProfileStage(context, 'app-stop-begin')
    for (const remove of removeBotDisposeListeners) remove()
    for (const bot of bots.values()) await bot.dispose()
    await app.stop()
    traceProfileStage(context, 'app-stop-complete')
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
  const testContextProfile = context.test_context_profile
  if (testContextProfile !== null && testContextProfile !== undefined) {
    if (
      testContextProfile.execution_scope !== 'linux_worker_only_network_disabled'
      || testContextProfile.authority_scope !== 'synthetic_test_actor_only'
    ) {
      throw new Error('P7 Koishi test context profile exceeds its synthetic offline Worker scope')
    }
    const { profile_sha256: claimedProfileHash, ...profilePayload } = testContextProfile
    const observedProfileHash = createHash('sha256').update(canonical(profilePayload), 'utf8').digest('hex')
    if (claimedProfileHash !== observedProfileHash) {
      throw new Error('P7 Koishi test context profile hash mismatch')
    }
    const expectedPluginConfig = { adminId: [testContextProfile.synthetic_actor_id] }
    if (
      !testContextProfile.synthetic_actor_id
      || canonical(testContextProfile.plugin_config) !== canonical(expectedPluginConfig)
      || canonical(context.config.plugin_config) !== canonical(expectedPluginConfig)
      || canonical(sample.fixture?.plugin_config ?? {}) !== canonical(expectedPluginConfig)
      || request.stimulus.events.some((event) => event.actor_id !== testContextProfile.synthetic_actor_id)
    ) {
      throw new Error('P7 Koishi actor/config differs from the source-bound synthetic admin profile')
    }
    environment.test_context_profile_sha256 = claimedProfileHash
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
    stock_native: {
      framework: 'Koishi',
      api: 'Context/Session/Bot',
      bridge_adapter_imported: false,
      runtime_services: runtimeServices,
      runtime_plugins: runtimePlugins,
      runtime_inventory: runtimeInventory,
    },
  }
  report.stock_native.runtime_commands = runtimeCommands
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
