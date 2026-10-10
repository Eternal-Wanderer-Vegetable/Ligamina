import { createInterface } from 'node:readline'
import { randomUUID } from 'node:crypto'
import { HostLifecycle, parseList, parsePluginConfig } from './lifecycle.js'
import { EventEnvelope, PlatformOperation } from './adapter.js'
import { CapabilityError, capabilityDescriptors, invokeCapability } from './capabilities.js'

const PROTOCOL_VERSION = '0.1'
const MAX_FRAME_BYTES = 1024 * 1024

class Worker {
  private readonly protocolWrite = process.stdout.write.bind(process.stdout) as unknown as (chunk: Uint8Array, callback: (error?: Error | null) => void) => boolean
  private readonly pending = new Map<string, { resolve(value: unknown): void; reject(error: Error): void }>()
  private readonly requests = new Map<string, { method: string; task: Promise<void> }>()
  private input?: ReturnType<typeof createInterface>
  private lifecycle?: HostLifecycle
  private hostId?: string
  private epoch?: number
  private ready = false
  private draining = false
  private stopping = false

  constructor() {
    // Koishi plugins often use console output. Preserve one protocol writer and
    // redirect ordinary process stdout writes so logs cannot corrupt NDJSON.
    const stderrWrite = process.stderr.write.bind(process.stderr) as (...args: any[]) => boolean
    Reflect.set(process.stdout, 'write', (...args: any[]) => stderrWrite(...args))
  }

  async run() {
    const input = this.input = createInterface({ input: process.stdin, crlfDelay: Infinity })
    for await (const line of input) {
      if (Buffer.byteLength(line, 'utf8') + 1 > MAX_FRAME_BYTES) {
        this.log('Host received an oversized NDJSON frame')
        break
      }
      let packet: any
      try {
        packet = JSON.parse(line)
      } catch (error) {
        this.log(`Host received invalid JSON: ${String(error)}`)
        break
      }
      if (!packet || packet.jsonrpc !== '2.0' || typeof packet !== 'object') {
        this.log('Host received a non-JSON-RPC packet')
        break
      }
      if (typeof packet.method === 'string') await this.receiveRequest(packet)
      else this.receiveResponse(packet)
      if (this.stopping) break
    }

    await Promise.allSettled([...this.requests.values()].map(({ task }) => task))
    await this.lifecycle?.stop()
    for (const pending of this.pending.values()) pending.reject(new Error('Core stdio closed'))
    this.pending.clear()
  }

  private async receiveRequest(packet: any) {
    if (packet.method === 'request.cancel') return
    if (typeof packet.id !== 'string') return
    if (packet.method === 'runtime.stop' || packet.method === 'host.shutdown') {
      await this.handleRequest(packet.id, packet.method, packet.params ?? {})
      return
    }
    const task = this.handleRequest(packet.id, packet.method, packet.params ?? {})
    this.requests.set(packet.id, { method: packet.method, task })
    void task.finally(() => this.requests.delete(packet.id)).catch(() => {})
  }

  private async handleRequest(id: string, method: string, params: any) {
    try {
      if (!params || typeof params !== 'object' || Array.isArray(params)) {
        throw new WorkerError('invalid_request', 'params must be an object')
      }
      let result: unknown
      if (method === 'host.initialize') {
        if (params.protocol_version !== PROTOCOL_VERSION) throw new WorkerError('protocol_mismatch', 'unsupported Host protocol version')
        if (typeof params.host_id !== 'string' || !params.host_id.trim() || !Number.isSafeInteger(params.epoch)) {
          throw new WorkerError('invalid_request', 'host_id and integer epoch are required')
        }
        if (this.ready || this.lifecycle) throw new WorkerError('already_initialized', 'Host has already initialized')
        const hostId = params.host_id as string
        this.hostId = hostId
        this.epoch = params.epoch
        this.lifecycle = new HostLifecycle({
          hostId,
          platformCall: (operation) => this.platformCall(operation),
          serviceCall: (invocation) => this.serviceCall(invocation),
          plugins: parseList(process.env.LIGAMINA_KOISHI_PLUGINS),
          pluginConfig: parsePluginConfig(process.env.LIGAMINA_KOISHI_PLUGIN_CONFIG),
        })
        await this.lifecycle.start()
        this.ready = true
        result = { ready: true, protocol_version: PROTOCOL_VERSION, capabilities: capabilityDescriptors() }
      } else if (method === 'event.dispatch') {
        this.requireReady()
        const envelope = (params.event ?? params) as EventEnvelope
        if (!this.lifecycle?.adapter) throw new WorkerError('host_not_ready', 'Koishi Bridge Adapter is unavailable')
        await this.lifecycle.adapter.dispatch(envelope)
        result = { accepted: true, event_id: envelope.event_id }
      } else if (method === 'capability.invoke') {
        this.requireReady()
        if (typeof params.capability_id !== 'string') throw new WorkerError('invalid_request', 'capability_id must be a string')
        try {
          result = invokeCapability(params.capability_id, params.arguments)
        } catch (error) {
          if (error instanceof CapabilityError) throw new WorkerError(error.code, error.message)
          throw error
        }
      } else if (method === 'runtime.health') {
        result = { ready: this.ready && !this.draining && !this.stopping, host_id: this.hostId, epoch: this.epoch, protocol_version: PROTOCOL_VERSION }
      } else if (method === 'runtime.capabilities') {
        this.requireReady()
        result = { capabilities: capabilityDescriptors() }
      } else if (method === 'runtime.stop' || method === 'host.shutdown') {
        this.draining = true
        const activeEvents = [...this.requests.entries()]
          .filter(([requestId, request]) => requestId !== id && request.method === 'event.dispatch')
          .map(([, request]) => request.task)
        await Promise.allSettled(activeEvents)
        await this.lifecycle?.stop()
        this.ready = false
        result = { stopping: true }
      } else {
        throw new WorkerError('method_not_found', `unsupported Host method: ${method}`)
      }
      await this.send({ jsonrpc: '2.0', id, result })
      if (method === 'runtime.stop' || method === 'host.shutdown') {
        this.stopping = true
        this.input?.close()
      }
    } catch (error) {
      const value = error instanceof WorkerError
        ? { code: error.code, message: error.message }
        : { code: 'host_error', message: error instanceof Error ? error.message : String(error) }
      this.log(`Host request ${method} failed: ${value.message}`)
      await this.send({ jsonrpc: '2.0', id, error: { code: -32000, message: value.message, data: { code: value.code } } })
    }
  }

  private receiveResponse(packet: any) {
    if (typeof packet.id !== 'string') {
      this.log('Host ignored Core response without a string ID')
      return
    }
    const pending = this.pending.get(packet.id)
    if (!pending) return
    this.pending.delete(packet.id)
    if (packet.error) {
      pending.reject(new Error(String(packet.error.message ?? 'platform.execute failed')))
    } else {
      pending.resolve(packet.result)
    }
  }

  private async platformCall(operation: PlatformOperation) {
    this.requireReady(true)
    const id = `platform-${randomUUID()}`
    const promise = new Promise<unknown>((resolve, reject) => this.pending.set(id, { resolve, reject }))
    await this.send({ jsonrpc: '2.0', id, method: 'platform.execute', params: operation })
    const result = await promise
    if (!result || typeof result !== 'object' || Array.isArray(result)) {
      throw new WorkerError('invalid_platform_result', 'platform.execute result must be an object')
    }
    return result as Record<string, unknown>
  }

  private async serviceCall(invocation: { capability_id: string; arguments: Record<string, unknown>; trace_id: string }) {
    this.requireReady()
    const id = `service-${randomUUID()}`
    const promise = new Promise<unknown>((resolve, reject) => this.pending.set(id, { resolve, reject }))
    await this.send({ jsonrpc: '2.0', id, method: 'service.invoke', params: invocation })
    return promise
  }

  private requireReady(allowDraining = false) {
    if (!this.ready || this.stopping || (this.draining && !allowDraining)) throw new WorkerError('host_not_ready', 'Host is not ready')
  }

  private async send(packet: Record<string, unknown>) {
    const frame = Buffer.from(`${JSON.stringify(packet)}\n`, 'utf8')
    if (frame.byteLength > MAX_FRAME_BYTES) throw new WorkerError('frame_too_large', 'outgoing NDJSON frame exceeds 1 MiB')
    await new Promise<void>((resolve, reject) => {
      this.protocolWrite(frame, (error?: Error | null) => error ? reject(error) : resolve())
    })
  }

  private log(message: string) {
    process.stderr.write(`${message}\n`)
  }
}

class WorkerError extends Error {
  constructor(public code: string, message: string) {
    super(message)
  }
}

void new Worker().run().catch((error) => {
  process.stderr.write(`Koishi Host stopped with error: ${error instanceof Error ? error.stack ?? error.message : String(error)}\n`)
  process.exitCode = 1
})
