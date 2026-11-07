/** Discovers native Koishi commands and describes them with the v0.2 command contract. */

const MAX_COMMANDS = 512

export interface CommandArgument {
  name: string
  required: boolean
  variadic?: boolean
  type?: string
  description?: string
}

export interface CommandOption {
  name: string
  syntax?: string
  type?: string
  description?: string
}

export interface CommandDescriptor {
  command_id: string
  invocation: string
  framework: 'koishi'
  description?: string
  usage?: string
  aliases?: string[]
  arguments?: CommandArgument[]
  options?: CommandOption[]
}

/**
 * Reads the commands registered in a started Koishi Context. Hidden commands and
 * Koishi's own built-in `help` (when not loaded by a plugin) are reported as-is;
 * Core decides what to expose. Descriptions come from the Koishi i18n registry.
 */
export function describeCommands(app: any): CommandDescriptor[] {
  const list: any[] = app?.$commander?._commandList ?? []
  const descriptors: CommandDescriptor[] = []
  for (const command of list) {
    if (descriptors.length >= MAX_COMMANDS) break
    const name = typeof command?.name === 'string' ? command.name : ''
    if (!name || /\s/.test(name)) continue
    const descriptor: CommandDescriptor = { command_id: name, invocation: name, framework: 'koishi' }
    const description = i18nText(app, `commands.${name}.description`)
    if (description) descriptor.description = clip(description, 2048)
    const declaration = typeof command.declaration === 'string' ? command.declaration.trim() : ''
    const optionSyntax = Object.values<any>(command._options ?? {})
      .map((option) => (typeof option?.syntax === 'string' ? `[${option.syntax}]` : ''))
      .filter(Boolean)
    descriptor.usage = clip([name, declaration, ...optionSyntax].filter(Boolean).join(' '), 1024)
    const aliases = Object.keys(command._aliases ?? {}).filter((alias) => alias !== name && alias.trim())
    if (aliases.length) descriptor.aliases = aliases.slice(0, 64).map((alias) => clip(alias, 256))
    const args = (Array.isArray(command._arguments) ? command._arguments : [])
      .filter((argument: any) => typeof argument?.name === 'string' && argument.name)
      .slice(0, 64)
      .map((argument: any): CommandArgument => ({
        name: clip(argument.name, 128),
        required: argument.required === true,
        ...(argument.variadic === true ? { variadic: true } : {}),
        ...(typeof argument.type === 'string' ? { type: clip(argument.type, 64) } : {}),
      }))
    if (args.length) descriptor.arguments = args
    const options = Object.values<any>(command._options ?? {})
      .filter((option) => typeof option?.name === 'string' && option.name)
      .slice(0, 64)
      .map((option): CommandOption => {
        const text = i18nText(app, `commands.${name}.options.${option.name}`)
        return {
          name: clip(option.name, 128),
          ...(typeof option.syntax === 'string' ? { syntax: clip(option.syntax, 256) } : {}),
          ...(typeof option.type === 'string' ? { type: clip(option.type, 64) } : {}),
          ...(text ? { description: clip(text, 1024) } : {}),
        }
      })
    if (options.length) descriptor.options = options
    descriptors.push(descriptor)
  }
  return descriptors
}

function i18nText(app: any, path: string): string | undefined {
  const i18n = app?.i18n
  if (!i18n || typeof i18n.get !== 'function') return undefined
  try {
    // `get` returns a per-locale dictionary without emitting "missing" warnings.
    const entries = i18n.get(path) as Record<string, unknown> | undefined
    if (!entries || typeof entries !== 'object') return undefined
    for (const locale of ['zh-CN', 'en-US', '', ...Object.keys(entries)]) {
      const value = entries[locale]
      if (typeof value === 'string' && value.trim()) return value.trim()
    }
  } catch {
    return undefined
  }
  return undefined
}

function clip(value: string, max: number): string {
  return value.length > max ? value.slice(0, max) : value
}
