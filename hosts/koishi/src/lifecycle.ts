import { mkdir } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { Context } from '@koishijs/core'
import SQLiteDriver from '@minatojs/driver-sqlite'
import { BridgeAdapter, PlatformCall, ServiceCall } from './adapter.js'

export class HostLifecycle {
  app?: any
  adapter?: BridgeAdapter
  private started = false

  constructor(private options: {
    hostId: string
    platformCall: PlatformCall
    serviceCall?: ServiceCall
    dataDirectory?: string
    plugins?: string[]
    pluginConfig?: Record<string, unknown>
    nicknames?: string[]
  }) {}

  async start() {
    if (this.started) return
    if (this.app) throw new Error('Koishi lifecycle cannot be started twice')
    const dataDirectory = resolve(this.options.dataDirectory ?? process.env.LIGAMINA_KOISHI_DATA_DIR ?? './data')
    await mkdir(dataDirectory, { recursive: true })
    const app = new Context({ nickname: this.options.nicknames ?? parseList(process.env.LIGAMINA_KOISHI_NICKNAMES) })
    this.app = app
    app.plugin(SQLiteDriver as any, { path: resolve(dataDirectory, 'koishi.sqlite') })
    this.adapter = new BridgeAdapter(app, {
      hostId: this.options.hostId,
      platformCall: this.options.platformCall,
      serviceCall: this.options.serviceCall,
    })
    for (const moduleName of this.options.plugins ?? parseList(process.env.LIGAMINA_KOISHI_PLUGINS)) {
      const module = await import(moduleName)
      const plugin = module.default ?? module.apply ?? module
      const config = this.options.pluginConfig?.[moduleName] ?? {}
      app.plugin(plugin, config)
    }
    await app.start()
    this.started = true
  }

  async stop() {
    if (!this.app || !this.started) return
    this.started = false
    await this.app.stop()
  }
}

export function parseList(value = ''): string[] {
  return value.split(',').map((item) => item.trim()).filter(Boolean)
}

export function parsePluginConfig(value = ''): Record<string, unknown> {
  if (!value) return {}
  const result: unknown = JSON.parse(value)
  if (!result || typeof result !== 'object' || Array.isArray(result)) {
    throw new TypeError('LIGAMINA_KOISHI_PLUGIN_CONFIG must be a JSON object')
  }
  return result as Record<string, unknown>
}
