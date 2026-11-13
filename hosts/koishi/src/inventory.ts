/** Framework-visible registrations for the lab inventory contract. */

import { Context } from '@koishijs/core'
import { describeCommands } from './commands.js'

const MAX_ITEMS = 4096

export function describeRuntimeInventory(app: any, pluginNames: string[] = []): Record<string, unknown> {
  const commands = describeCommands(app).map((descriptor) => ({
    kind: 'command',
    name: descriptor.command_id,
    attributes: {
      ...(descriptor.aliases?.length ? { aliases: descriptor.aliases } : {}),
      ...(descriptor.arguments?.length ? { arguments: descriptor.arguments } : {}),
      ...(descriptor.options?.length ? { options: descriptor.options } : {}),
    },
    evidence: [{ reference: 'app.$commander._commandList' }],
  })).slice(0, MAX_ITEMS)

  const handlers: Record<string, unknown>[] = []
  const hooks = app?.lifecycle?._hooks ?? {}
  for (const [eventName, registrations] of Object.entries<any[]>(hooks)) {
    if (!Array.isArray(registrations) || !/(^|\/)message($|\/)/.test(eventName)) continue
    handlers.push({
      kind: 'message_listener',
      name: eventName,
      attributes: { registrations: registrations.length },
      evidence: [{ reference: `app.lifecycle._hooks[${JSON.stringify(eventName)}]` }],
    })
    if (handlers.length >= MAX_ITEMS) break
  }
  const middlewareHooks = app?.$processor?._hooks
  if (Array.isArray(middlewareHooks) && middlewareHooks.length) {
    handlers.push({
      kind: 'middleware',
      name: 'message_middleware',
      attributes: { registrations: middlewareHooks.length },
      evidence: [{ reference: 'app.$processor._hooks' }],
    })
  }

  const serviceMap = (app as any)?.[Context.internal] ?? {}
  const services = Object.keys(serviceMap).sort().map((name) => ({
    kind: 'provided_service',
    name,
    evidence: [{ reference: `app[Context.internal].${name}` }],
  })).slice(0, MAX_ITEMS)

  const declaredDependencies: Record<string, unknown>[] = []
  const plugins = app?.registry?.keys?.() ?? []
  for (const plugin of plugins) {
    const inject = (plugin as any)?.inject
    const names = Array.isArray(inject)
      ? inject
      : inject && typeof inject === 'object'
        ? [...(Array.isArray(inject.required) ? inject.required : []), ...(Array.isArray(inject.optional) ? inject.optional : [])]
        : []
    for (const name of names) {
      if (typeof name !== 'string' || !name) continue
      declaredDependencies.push({
        kind: 'plugin_dependency',
        name,
        evidence: [{ reference: `app.registry.plugin(${String((plugin as any).name ?? 'anonymous')}).inject` }],
      })
    }
  }

  const packageName = process.env.LIGAMINA_INVENTORY_PACKAGE || pluginNames.join(',') || 'loaded-plugins'
  const plugin: Record<string, string> = { package: packageName.slice(0, 256) }
  copyIdentity(plugin, 'registry_entry_id', 'LIGAMINA_INVENTORY_ENTRY_ID', 128)
  copyIdentity(plugin, 'version', 'LIGAMINA_INVENTORY_VERSION', 128)
  const artifactHash = process.env.LIGAMINA_INVENTORY_ARTIFACT_SHA256 ?? ''
  if (/^[a-f0-9]{64}$/.test(artifactHash)) plugin.artifact_sha256 = artifactHash

  return {
    schema_version: 1,
    ecosystem: 'koishi',
    plugin,
    source: {
      kind: 'runtime',
      collector: 'koishi-runtime/1',
      confidence: 'observed',
      evidence: [
        { reference: 'app.$commander._commandList' },
        { reference: 'app.lifecycle._hooks' },
        { reference: 'app[Context.internal]' },
      ],
    },
    capabilities: {
      commands: { status: 'observed', items: commands },
      handlers: { status: 'observed', items: handlers },
      services: { status: 'observed', items: [...services, ...declaredDependencies].slice(0, MAX_ITEMS) },
      schedulers: { status: 'unavailable', items: [], reason: 'Koishi timer service does not expose a common registry of plugin-scheduled jobs.' },
      configuration: { status: 'unavailable', items: [], reason: 'Runtime plugin configuration values are excluded; schemas and defaults are collected statically.' },
    },
    limitations: ['Runtime inventory contains framework-visible registrations; plugin configuration values are never emitted.'],
  }
}

function copyIdentity(target: Record<string, string>, key: string, env: string, max: number) {
  const value = process.env[env]
  if (value) target[key] = value.slice(0, max)
}
