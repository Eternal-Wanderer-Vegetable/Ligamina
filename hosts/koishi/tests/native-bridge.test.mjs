import assert from 'node:assert/strict'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { test } from 'node:test'
import { HostLifecycle } from '../dist/lifecycle.js'
import { observed } from '../dist/bridge-smoke-plugin.js'

const pluginPath = pathToFileURL(resolve('dist/bridge-smoke-plugin.js')).href

function envelope({ eventId, traceId, conversation, message, actorId = '23', messageId }) {
  return {
    schema_version: 'ligamina/0.1',
    event_id: eventId,
    trace_id: traceId,
    platform: 'onebot.v11',
    account_id: '9000',
    conversation,
    actor_id: actorId,
    timestamp: '2026-10-10T01:02:03Z',
    target_host: 'koishi-test',
    message,
    extensions: { 'onebot.v11': { message_id: messageId, sender: { nickname: 'Alice' } } },
  }
}

test('native Koishi Session dispatch preserves quote/direct identity, authority, and SQLite persistence', async () => {
  const dataDirectory = await mkdtemp(join(tmpdir(), 'ligamina-koishi-'))
  const calls = []
  const platformCall = async (operation) => {
    calls.push(operation)
    if (operation.operation === 'get_message') {
      return {
        status: 'succeeded',
        data: {
          target: operation.target,
          message_id: operation.params.message_id,
          actor_id: '9000',
          timestamp: '2026-10-10T01:00:00Z',
          message: [{ type: 'text', text: 'quoted text' }],
          nickname: 'Bot',
        },
      }
    }
    return { status: 'succeeded', platform_message_id: `sent-${calls.length}` }
  }

  observed.length = 0
  let lifecycle = new HostLifecycle({ hostId: 'koishi-test', platformCall, dataDirectory, plugins: [pluginPath], nicknames: ['Bot'] })
  try {
    await lifecycle.start()
    const app = lifecycle.app
    await app.database.createUser('onebot.v11', '23', { name: 'Alice', authority: 1 })
    await lifecycle.adapter.dispatch(envelope({
      eventId: 'denied',
      traceId: 'trace-denied',
      conversation: { kind: 'group', id: '42' },
      messageId: '101',
      message: [{ type: 'text', text: 'ligamina.secure denied' }],
    }))
    const bridgeBot = lifecycle.adapter.bots[0]
    assert.equal(await bridgeBot.supports('message.delete'), false)
    await assert.rejects(() => bridgeBot.deleteMessage('42', '77'), (error) => error.code === 'unsupported_api')
    await assert.rejects(() => bridgeBot.internal.unknown(), (error) => error.code === 'unsupported_api')
    const sendsAfterDenied = calls.filter((operation) => operation.operation === 'send_message')
    assert.equal(sendsAfterDenied.length, 1)
    assert.notDeepEqual(sendsAfterDenied[0].params.message, [{ type: 'text', text: 'secure:denied' }])

    await app.database.setUser('onebot.v11', '23', { authority: 3 })
    await lifecycle.adapter.dispatch(envelope({
      eventId: 'allowed',
      traceId: 'trace-allowed',
      conversation: { kind: 'group', id: '23' },
      messageId: '102',
      message: [{ type: 'text', text: 'ligamina.secure passed' }],
    }))
    const sends = calls.filter((operation) => operation.operation === 'send_message')
    const send = sends.find((operation) => JSON.stringify(operation.params.message) === JSON.stringify([{ type: 'text', text: 'secure:passed' }]))
    assert.ok(send)
    assert.equal(send.trace_id, 'trace-allowed')
    assert.deepEqual(send.target, { kind: 'group', id: '23' })
    await lifecycle.adapter.dispatch(envelope({
      eventId: 'quoted-at',
      traceId: 'trace-quoted-at',
      conversation: { kind: 'group', id: '42' },
      messageId: '104',
      message: [
        { type: 'reply', message_id: '77' },
        { type: 'mention', user_id: '9000' },
        { type: 'text', text: 'hello' },
      ],
    }))
    assert.ok(calls.some((operation) => operation.operation === 'get_message' && operation.trace_id === 'trace-quoted-at'))
    assert.ok(observed.some((entry) => entry.quoteId === '77' && entry.channelId === '42' && entry.guildId === '42' && entry.atSelf && !entry.hasAt))

    await lifecycle.adapter.dispatch(envelope({
      eventId: 'private',
      traceId: 'trace-private',
      conversation: { kind: 'private', id: '23' },
      messageId: '103',
      message: [{ type: 'text', text: 'ligamina.secure direct' }],
    }))
    const privateObservation = observed.find((entry) => entry.content.includes('ligamina.secure direct'))
    assert.equal(privateObservation?.direct, true)
    assert.equal(privateObservation?.channelId, '23')
    assert.equal(privateObservation?.guildId, undefined)
    const privateSend = calls.filter((operation) => operation.operation === 'send_message')
      .find((operation) => JSON.stringify(operation.params.message) === JSON.stringify([{ type: 'text', text: 'secure:direct' }]))
    assert.deepEqual(privateSend?.target, { kind: 'private', id: '23' })

    await lifecycle.stop()
    lifecycle = new HostLifecycle({ hostId: 'koishi-test', platformCall, dataDirectory, plugins: [pluginPath], nicknames: ['Bot'] })
    await lifecycle.start()
    const persisted = await lifecycle.app.database.getUser('onebot.v11', '23', ['authority'])
    assert.equal(persisted.authority, 3)
  } finally {
    await lifecycle.stop()
    await rm(dataDirectory, { recursive: true, force: true })
  }
})
