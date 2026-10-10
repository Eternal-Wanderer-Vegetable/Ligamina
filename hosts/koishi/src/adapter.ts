import { randomUUID } from 'node:crypto'
import { Adapter, Bot, Fragment, h, Session, Universal } from '@koishijs/core'
import { classifyOperationSupport, validateEvent, validateOperation } from './contracts.js'

const MAX_REMEMBERED_CONVERSATIONS = 10_000
const SUPPORTED_KOISHI_APIS = new Set(['message.create', 'message.get', 'login.get'])
const KOISHI_API_NAMES = new Set(Object.values(Universal.Methods).map(({ name }) => name))

export type PlatformCall = (operation: PlatformOperation) => Promise<Record<string, unknown>>
export type ServiceCall = (invocation: {
  capability_id: string
  arguments: Record<string, unknown>
  trace_id: string
}) => Promise<unknown>

export interface PlatformOperation {
  operation_id: string
  trace_id: string
  account_id: string
  operation: string
  target: Target
  params: Record<string, unknown>
}

export interface Target {
  kind: 'group' | 'private'
  id: string
}

export interface EventEnvelope {
  schema_version: 'ligamina/0.1'
  event_id: string
  trace_id: string
  platform: string
  account_id: string
  conversation: Target
  actor_id: string
  timestamp: string
  target_host: string
  message: Array<Record<string, unknown>>
  extensions?: Record<string, unknown>
}

export interface BridgeAdapterConfig {
  hostId: string
  platformCall: PlatformCall
  serviceCall?: ServiceCall
}

export class BridgeError extends Error {
  constructor(public code: string, message: string) {
    super(message)
    this.name = 'BridgeError'
  }
}

export class BridgeAdapter extends Adapter<any, BridgeBot> {
  private readonly botsByKey = new Map<string, BridgeBot>()
  private readonly channelTargets = new Map<string, Map<string, Target>>()
  private readonly traces = new Map<string, string>()
  private stopping = false

  constructor(ctx: any, public config: BridgeAdapterConfig) {
    super(ctx)
    if (!config.hostId.trim()) throw new TypeError('hostId must not be empty')
    ctx.on('dispose', () => { this.stopping = true })
  }

  async dispatch(envelope: EventEnvelope): Promise<void> {
    const errors = validateEvent(envelope)
    if (errors.length) throw new BridgeError('invalid_request', errors.join('; '))
    if (envelope.target_host !== this.config.hostId) {
      throw new BridgeError('wrong_host', 'event target_host does not match this Host')
    }
    if (this.stopping) throw new BridgeError('host_stopping', 'Bridge Adapter is shutting down')

    const bot = await this.getBot(envelope.platform, envelope.account_id)
    const target = { ...envelope.conversation }
    this.rememberTarget(envelope.platform, target)
    const metadata = platformMetadata(envelope)
    const messageId = requiredString(metadata.message_id, `extensions.${envelope.platform}.message_id`)
    const timestamp = Date.parse(envelope.timestamp)
    if (!Number.isFinite(timestamp)) throw new BridgeError('invalid_request', 'timestamp must be ISO-8601')

    let replyId: string | undefined
    const elements = [] as ReturnType<typeof h.parse>
    for (const segment of envelope.message) {
      switch (segment.type) {
        case 'text':
          if (typeof segment.text !== 'string') throw new BridgeError('invalid_request', 'text segment requires text')
          elements.push(h.text(segment.text))
          break
        case 'mention':
          elements.push(h.at(requiredString(segment.user_id, 'mention.user_id')))
          break
        case 'image_ref':
          elements.push(h.image(requiredString(segment.resource_ref, 'image_ref.resource_ref')))
          break
        case 'reply': {
          if (replyId) throw new BridgeError('unsupported_api', 'multiple reply segments are unsupported')
          replyId = requiredString(segment.message_id, 'reply.message_id')
          break
        }
        default:
          throw new BridgeError('unsupported_api', `message segment ${String(segment.type)} is unsupported`)
      }
    }

    const sender = isRecord(metadata.sender) ? metadata.sender : {}
    const isGroup = target.kind === 'group'
    const event = {
      type: 'message',
      platform: envelope.platform,
      selfId: envelope.account_id,
      timestamp,
      user: {
        id: envelope.actor_id,
        ...(typeof sender.nickname === 'string' ? { name: sender.nickname, nick: sender.nickname } : {}),
      },
      channel: {
        id: target.id,
        type: isGroup ? Universal.Channel.Type.TEXT : Universal.Channel.Type.DIRECT,
      },
      message: {
        id: messageId,
        messageId,
        content: elements.join(''),
        elements,
      },
      ...(isGroup ? { guild: { id: target.id } } : {}),
      referrer: envelope.event_id,
    } as unknown as Universal.Event
    const session = bot.session(event)
    this.traces.set(this.sessionKey(session), envelope.trace_id)
    if (replyId) event.message!.quote = await bot.getMessageForTarget(target, replyId, session)
    await this.dispatchAndWait(session)
  }

  resolveTarget(platform: string, channelId: string, guildId?: string, session?: Session): Target {
    if (session) {
      if (session.isDirect) {
        if (!session.userId) throw new BridgeError('invalid_request', 'direct session has no user ID')
        return { kind: 'private', id: session.userId }
      }
      const id = session.guildId || guildId || channelId
      if (!id) throw new BridgeError('invalid_request', 'group session has no guild/channel ID')
      return { kind: 'group', id }
    }
    const candidates = [...(this.channelTargets.get(this.targetKey(platform, channelId))?.values() ?? [])]
    if (guildId) {
      const group = candidates.find((candidate) => candidate.kind === 'group' && candidate.id === guildId)
      if (group) return group
    }
    if (candidates.length === 1) return candidates[0]
    throw new BridgeError('invalid_request', candidates.length ? 'channel target is ambiguous without a Session' : 'channel has no known conversation scope')
  }

  traceFor(session?: Session): string {
    return session ? this.traces.get(this.sessionKey(session)) ?? randomUUID() : randomUUID()
  }

  async invokeService(
    capabilityId: string,
    argumentsValue: Record<string, unknown>,
    session?: Session,
  ): Promise<unknown> {
    if (!capabilityId.trim()) throw new BridgeError('invalid_request', 'capability_id must not be empty')
    if (!argumentsValue || typeof argumentsValue !== 'object' || Array.isArray(argumentsValue)) {
      throw new BridgeError('invalid_request', 'capability arguments must be an object')
    }
    if (!this.config.serviceCall) throw new BridgeError('host_not_ready', 'service.invoke is unavailable')
    return this.config.serviceCall({
      capability_id: capabilityId,
      arguments: argumentsValue,
      trace_id: this.traceFor(session),
    })
  }

  rememberSentTarget(platform: string, target: Target) {
    this.rememberTarget(platform, target)
  }

  async connect(bot: BridgeBot) {
    bot.online()
  }

  async disconnect(bot: BridgeBot) {
    bot.offline()
  }

  private async getBot(platform: string, accountId: string): Promise<BridgeBot> {
    const key = `${platform}\0${accountId}`
    let bot = this.botsByKey.get(key)
    if (bot) return bot
    bot = new BridgeBot(this.ctx, { platform, selfId: accountId }, this)
    this.fork(this.ctx, bot)
    this.botsByKey.set(key, bot)
    await bot.start()
    return bot
  }

  private rememberTarget(platform: string, target: Target) {
    const key = this.targetKey(platform, target.id)
    const values = this.channelTargets.get(key) ?? new Map<string, Target>()
    values.set(`${target.kind}\0${target.id}`, target)
    this.channelTargets.delete(key)
    this.channelTargets.set(key, values)
    while (this.channelTargets.size > MAX_REMEMBERED_CONVERSATIONS) {
      this.channelTargets.delete(this.channelTargets.keys().next().value!)
    }
  }

  private targetKey(platform: string, channelId: string) {
    return `${platform}\0${channelId}`
  }

  private sessionKey(session: Session) {
    return `${session.sid}\0${session.id}`
  }

  private async dispatchAndWait(session: Session) {
    let dispose = () => {}
    const completed = new Promise<void>((resolve) => {
      dispose = this.ctx.on('middleware', (processed: Session) => {
        if (processed.id === session.id && processed.sid === session.sid) resolve()
      })
    })
    let timer: ReturnType<typeof setTimeout> | undefined
    try {
      session.bot.dispatch(session)
      await Promise.race([
        completed,
        new Promise<never>((_, reject) => {
          timer = setTimeout(() => reject(new BridgeError('timeout', 'Koishi middleware did not finish in 30 seconds')), 30_000)
        }),
      ])
    } finally {
      if (timer) clearTimeout(timer)
      dispose()
      this.traces.delete(this.sessionKey(session))
    }
  }
}

export class BridgeBot extends Bot<any, { platform: string; selfId: string }> {
  constructor(ctx: any, config: { platform: string; selfId: string }, public bridge: BridgeAdapter) {
    super(ctx, config, 'ligamina')
    this.platform = config.platform
    this.selfId = config.selfId
    this.user = { id: config.selfId }
    this.features = [...SUPPORTED_KOISHI_APIS]
    this.internal = new Proxy(Object.create(null), {
      get: (_target, property) => async () => {
        throw new BridgeError('unsupported_api', `Koishi internal API ${String(property)} is unsupported`)
      },
    })
    return new Proxy(this, {
      get: (target, property, receiver) => {
        const value = Reflect.get(target, property, receiver)
        if (typeof property === 'string' && KOISHI_API_NAMES.has(property) && typeof value !== 'function') {
          return async () => { throw new BridgeError('unsupported_api', `Koishi API ${property} is unsupported`) }
        }
        return value
      },
    })
  }

  async start() {
    if (this.isActive) return
    await super.start()
  }

  dispose() {
    // Koishi Cordis may dispose the root Satori service before this Bot's
    // listener runs. Keep shutdown safe if its temporary ctx.bots mixin is gone.
    if (!this.ctx.bots || typeof this.ctx.bots.findIndex !== 'function') return this.stop()
    return super.dispose()
  }

  async sendMessage(channelId: string, content: Fragment, guildId?: string, options?: Universal.SendOptions): Promise<string[]> {
    const session = options?.session as Session | undefined
    const target = this.bridge.resolveTarget(this.platform!, channelId, guildId, session)
    const operation = this.operation('send_message', target, { message: outgoingSegments(content) }, session)
    const result = await this.execute(operation)
    const messageId = requiredString(result.platform_message_id, 'platform_message_id')
    this.bridge.rememberSentTarget(this.platform!, target)
    return [messageId]
  }

  async sendPrivateMessage(userId: string, content: Fragment, _guildId?: string, options?: Universal.SendOptions): Promise<string[]> {
    const operation = this.operation('send_message', { kind: 'private', id: userId }, { message: outgoingSegments(content) }, options?.session as Session | undefined)
    const result = await this.execute(operation)
    return [requiredString(result.platform_message_id, 'platform_message_id')]
  }

  async getMessage(channelId: string, messageId: string): Promise<Universal.Message> {
    const target = this.bridge.resolveTarget(this.platform!, channelId)
    return this.getMessageForTarget(target, messageId)
  }

  async getMessageForTarget(target: Target, messageId: string, session?: Session): Promise<Universal.Message> {
    const operation = this.operation('get_message', target, { message_id: messageId }, session)
    const result = await this.execute(operation)
    const data = result.data
    if (!isRecord(data) || !sameTarget(data.target, target)) {
      throw new BridgeError('invalid_platform_result', 'get_message returned no data or a different conversation')
    }
    const id = requiredString(data.message_id, 'data.message_id')
    const actorId = requiredString(data.actor_id, 'data.actor_id')
    const timestamp = Date.parse(requiredString(data.timestamp, 'data.timestamp'))
    if (!Number.isFinite(timestamp) || !Array.isArray(data.message)) {
      throw new BridgeError('invalid_platform_result', 'get_message data has invalid timestamp/message')
    }
    const elements = data.message.map((segment) => incomingElement(segment))
    const isGroup = target.kind === 'group'
    return {
      id,
      messageId: id,
      channel: { id: target.id, type: isGroup ? Universal.Channel.Type.TEXT : Universal.Channel.Type.DIRECT },
      ...(isGroup ? { guild: { id: target.id } } : {}),
      user: { id: actorId, ...(typeof data.nickname === 'string' ? { name: data.nickname } : {}) },
      content: elements.join(''),
      elements,
      timestamp,
    }
  }

  async getLogin(): Promise<Universal.Login> {
    const target: Target = { kind: 'private', id: this.selfId }
    const result = await this.execute(this.operation('get_self_info', target, {}))
    if (!isRecord(result.data)) throw new BridgeError('invalid_platform_result', 'get_self_info returned no data')
    const userId = requiredString(result.data.user_id, 'data.user_id')
    const nickname = requiredString(result.data.nickname, 'data.nickname')
    this.user = { id: userId, name: nickname }
    return this.toJSON()
  }

  async supports(name: string): Promise<boolean> {
    return SUPPORTED_KOISHI_APIS.has(name)
  }

  private operation(name: string, target: Target, params: Record<string, unknown>, session?: Session): PlatformOperation {
    if (classifyOperationSupport(name) !== 'supported') throw new BridgeError('unsupported_api', `operation ${name} is unsupported`)
    const operation: PlatformOperation = {
      operation_id: randomUUID(),
      trace_id: this.bridge.traceFor(session),
      account_id: this.selfId,
      operation: name,
      target,
      params,
    }
    const errors = validateOperation(operation)
    if (errors.length) throw new BridgeError('invalid_request', errors.join('; '))
    return operation
  }

  private async execute(operation: PlatformOperation): Promise<Record<string, unknown>> {
    const result = await this.bridge.config.platformCall(operation)
    if (!isRecord(result) || result.status !== 'succeeded') {
      const status = isRecord(result) ? result.status : undefined
      throw new BridgeError(status === 'unknown' ? 'operation_unknown' : 'platform_operation_failed', `platform operation ended with status ${String(status)}`)
    }
    return result
  }
}

function platformMetadata(envelope: EventEnvelope): Record<string, unknown> {
  const extensions = envelope.extensions ?? {}
  const value = extensions[envelope.platform] ?? extensions['koishi'] ?? extensions['satori'] ?? extensions['onebot.v11']
  return isRecord(value) ? value : {}
}

function requiredString(value: unknown, field: string): string {
  if (typeof value !== 'string' || !value.length) throw new BridgeError('invalid_request', `${field} must be a non-empty string`)
  return value
}

function isRecord(value: unknown): value is Record<string, any> {
  return !!value && typeof value === 'object' && !Array.isArray(value)
}

function sameTarget(value: unknown, target: Target): boolean {
  return isRecord(value) && value.kind === target.kind && value.id === target.id
}

function incomingElement(segment: unknown) {
  if (!isRecord(segment)) throw new BridgeError('invalid_platform_result', 'message segment must be an object')
  switch (segment.type) {
    case 'text': return h.text(requiredStringOrEmpty(segment.text, 'text'))
    case 'mention': return h.at(requiredString(segment.user_id, 'mention.user_id'))
    case 'image_ref': return h.image(requiredString(segment.resource_ref, 'image_ref.resource_ref'))
    case 'reply': return h.quote({ id: requiredString(segment.message_id, 'reply.message_id') })
    default: throw new BridgeError('unsupported_api', `message segment ${String(segment.type)} is unsupported`)
  }
}

function outgoingSegments(fragment: Fragment): Array<Record<string, string>> {
  const segments: Array<Record<string, string>> = []
  for (const element of h.normalize(fragment)) {
    if (element.type === 'text') {
      segments.push({ type: 'text', text: String(element.attrs.content ?? '') })
    } else if (element.type === 'at') {
      segments.push({ type: 'mention', user_id: requiredString(element.attrs.id, 'at.id') })
    } else if (element.type === 'quote') {
      segments.push({ type: 'reply', message_id: requiredString(element.attrs.id, 'quote.id') })
    } else if (element.type === 'img') {
      segments.push({ type: 'image_ref', resource_ref: requiredString(element.attrs.src, 'img.src') })
    } else {
      throw new BridgeError('unsupported_api', `Koishi message element ${element.type} is unsupported`)
    }
  }
  return segments
}

function requiredStringOrEmpty(value: unknown, field: string): string {
  if (typeof value !== 'string') throw new BridgeError('invalid_platform_result', `${field} must be a string`)
  return value
}
