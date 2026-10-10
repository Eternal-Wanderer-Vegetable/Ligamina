import { Context, Session } from '@koishijs/core'

/** Self-authored G1 fixture that replies through the Core-owned gateway. */
export default (ctx: Context) => {
  ctx.on('message', async (session: Session) => {
    const kind = session.isDirect ? 'private' : 'group'
    await session.send(`koishi:${kind}:${session.content ?? ''}`)
  })
}
