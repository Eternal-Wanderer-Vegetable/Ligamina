import { Context, Session } from '@koishijs/core'

export interface Observation {
  direct: boolean
  channelId: string
  guildId?: string
  quoteId?: string
  content: string
  atSelf: boolean
  hasAt: boolean
  strippedContent: string
}

export const observed: Observation[] = []

const plugin = (ctx: Context) => {
  ctx.on('message', (session: Session) => {
    observed.push({
      direct: session.isDirect,
      channelId: session.channelId!,
      guildId: session.guildId,
      quoteId: session.quote?.id,
      content: session.content ?? '',
      atSelf: session.stripped.atSelf,
      hasAt: session.stripped.hasAt,
      strippedContent: session.stripped.content,
    })
  })

  ctx.command('ligamina.secure <value:text>', 'authority gate', { authority: 2 })
    .action(async ({ session }, value) => {
      if (!session) throw new Error('command session is required')
      await session.send(`secure:${value}`)
    })
}

export default plugin
