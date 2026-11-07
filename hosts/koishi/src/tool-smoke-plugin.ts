import { Context } from '@koishijs/core'

/** Self-authored plugin used to verify that native commands work as Ligamina tools. */
const plugin = (ctx: Context) => {
  ctx.command('ligamina.echo <value:text>', 'Echo the given text back')
    .option('upper', '-u  Convert to upper case')
    .action(({ options }, value) => {
      const text = value ?? ''
      return options?.upper ? text.toUpperCase() : text
    })

  ctx.command('ligamina.twice <value:text>', 'Send the text in two separate messages')
    .action(async ({ session }, value) => {
      if (!session) throw new Error('command session is required')
      await session.send(`first:${value}`)
      await session.send(`second:${value}`)
    })

  ctx.command('ligamina.quote <id:string>', 'Try to read a platform message (unsupported in tool sessions)')
    .action(async ({ session }, id) => {
      if (!session) throw new Error('command session is required')
      try {
        await session.bot.getMessage(session.channelId!, id)
        return 'read'
      } catch (error) {
        return `read-failed:${(error as { code?: string }).code ?? 'unknown'}`
      }
    })

  ctx.command('ligamina.slow <ms:number>', 'Reply after the given delay in milliseconds')
    .action(async ({ session }, ms) => {
      if (!session) throw new Error('command session is required')
      await new Promise((resolve) => setTimeout(resolve, Math.max(0, Math.min(Number(ms) || 0, 10_000))))
      try {
        await session.send('slow:done')
      } catch (error) {
        // A reply after the Core deadline reaches a closed tool session and is refused.
        lateRefusals.push((error as { code?: string }).code ?? 'unknown')
      }
    })

  ctx.command('ligamina.refusals', 'Report how many late replies were refused')
    .action(() => `refusals:${lateRefusals.length}:${lateRefusals.join(',')}`)

  ctx.command('ligamina.crash', 'Exit the Host process (restart test only)')
    .action(() => {
      process.stderr.write('tool smoke plugin: exiting on request\n')
      process.exit(17)
    })
}

const lateRefusals: string[] = []

export default plugin
