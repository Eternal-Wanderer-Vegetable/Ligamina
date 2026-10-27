/** Record Koishi Session scope and clock under its isolated Host data directory. */

import { appendFile, mkdir } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'

export default function phase4ProbePlugin(ctx) {
  const dataDirectory = process.env.LIGAMINA_KOISHI_DATA_DIR
  if (!dataDirectory) throw new Error('Phase 4 Koishi observation data directory is not configured')
  const observationFile = resolve(dataDirectory, 'phase4-probes', 'observations.ndjson')
  ctx.app.on('message', async (session) => {
    const direct = Boolean(session.isDirect)
    const timestamp = new Date(session.timestamp).toISOString().replace('.000Z', 'Z')
    const record = {
      event_id: String(session.referrer ?? ''),
      account_id: String(session.selfId ?? ''),
      actor_id: String(session.userId ?? ''),
      conversation: {
        kind: direct ? 'private' : 'group',
        id: String(direct ? session.userId : session.channelId),
      },
      timestamp,
    }
    await mkdir(dirname(observationFile), { recursive: true })
    await appendFile(observationFile, `${JSON.stringify(record)}\n`, { encoding: 'utf8', flag: 'a' })
  })
}
