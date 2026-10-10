import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { createInterface } from 'node:readline'
import { pathToFileURL } from 'node:url'
import { test } from 'node:test'

test('Koishi stdio worker initializes, dispatches natively, calls the gateway, and shuts down', async () => {
  const dataDirectory = await mkdtemp(join(tmpdir(), 'ligamina-koishi-worker-'))
  const environment = {
    ...process.env,
    LIGAMINA_KOISHI_DATA_DIR: dataDirectory,
    LIGAMINA_KOISHI_PLUGINS: pathToFileURL(resolve('dist/bridge-smoke-plugin.js')).href,
    LIGAMINA_KOISHI_NICKNAMES: 'Bot',
  }
  const child = spawn(process.execPath, [resolve('dist/worker.js')], {
    cwd: process.cwd(),
    env: environment,
    stdio: ['pipe', 'pipe', 'pipe'],
  })
  const lines = createInterface({ input: child.stdout, crlfDelay: Infinity })
  const queue = []
  const waiters = []
  lines.on('line', (line) => {
    const packet = JSON.parse(line)
    const waiter = waiters.shift()
    if (waiter) waiter(packet)
    else queue.push(packet)
  })
  const receive = () => queue.length
    ? Promise.resolve(queue.shift())
    : new Promise((resolvePacket) => waiters.push(resolvePacket))
  const send = (packet) => child.stdin.write(`${JSON.stringify(packet)}\n`)
  const rpc = async (method, params) => {
    const id = `test-${method}-${Math.random()}`
    send({ jsonrpc: '2.0', id, method, params })
    while (true) {
      const packet = await receive()
      if (packet.method === 'platform.execute') {
        const operation = packet.params
        if (operation.operation === 'get_message') {
          assert.deepEqual(operation.target, { kind: 'group', id: '42' })
          assert.equal(operation.trace_id, 'worker-trace')
          send({ jsonrpc: '2.0', id: packet.id, result: {
            status: 'succeeded',
            data: {
              target: operation.target,
              message_id: '77',
              actor_id: '9000',
              timestamp: '2026-10-10T01:00:00Z',
              message: [{ type: 'text', text: 'quoted' }],
              nickname: 'Bot',
            },
          } })
        } else {
          assert.equal(operation.operation, 'send_message')
          assert.equal(operation.trace_id, 'worker-trace')
          send({ jsonrpc: '2.0', id: packet.id, result: { status: 'succeeded', platform_message_id: '501' } })
        }
        continue
      }
      if (packet.id === id) {
        assert.equal(packet.error, undefined, JSON.stringify(packet.error))
        return packet.result
      }
    }
  }
  const rpcError = async (method, params) => {
    const id = `test-${method}-${Math.random()}`
    send({ jsonrpc: '2.0', id, method, params })
    while (true) {
      const packet = await receive()
      if (packet.id === id) return packet.error
    }
  }

  try {
    const initialized = await rpc('host.initialize', {
      host_id: 'koishi-worker-test',
      epoch: 1,
      protocol_version: '0.1',
    })
    assert.equal(initialized.ready, true)
    assert.equal(initialized.protocol_version, '0.1')
    assert.deepEqual(initialized.capabilities, [{
      capability_id: 'ligamina.koishi.math.add',
      input_schema: {
        type: 'object',
        properties: {
          left: { type: 'integer', minimum: -9007199254740991, maximum: 9007199254740991 },
          right: { type: 'integer', minimum: -9007199254740991, maximum: 9007199254740991 },
        },
        required: ['left', 'right'],
        additionalProperties: false,
      },
      output_schema: { type: 'integer', minimum: -9007199254740991, maximum: 9007199254740991 },
      pure: true,
    }])
    assert.equal(await rpc('capability.invoke', {
      capability_id: 'ligamina.koishi.math.add',
      arguments: { left: 19, right: 23 },
    }), 42)
    assert.equal((await rpcError('capability.invoke', {
      capability_id: 'ligamina.koishi.math.add',
      arguments: { left: '19', right: 23 },
    })).data.code, 'invalid_request')
    assert.equal((await rpcError('capability.invoke', {
      capability_id: 'ligamina.koishi.unmapped',
      arguments: {},
    })).data.code, 'unsupported_capability')
    assert.deepEqual(await rpc('event.dispatch', {
      schema_version: 'ligamina/0.1',
      event_id: 'worker-event',
      trace_id: 'worker-trace',
      platform: 'onebot.v11',
      account_id: '9000',
      conversation: { kind: 'group', id: '42' },
      actor_id: '23',
      timestamp: '2026-10-10T01:02:03Z',
      target_host: 'koishi-worker-test',
      message: [
        { type: 'reply', message_id: '77' },
        { type: 'mention', user_id: '9000' },
        { type: 'text', text: 'ligamina.secure worker' },
      ],
      extensions: { 'onebot.v11': { message_id: '78', sender: { nickname: 'Alice' } } },
    }), { accepted: true, event_id: 'worker-event' })
    assert.equal((await rpc('runtime.health', {})).ready, true)
    assert.deepEqual(await rpc('host.shutdown', {}), { stopping: true })
    child.stdin.end()
    const exitCode = await new Promise((resolveCode, reject) => {
      child.once('error', reject)
      child.once('exit', (code) => resolveCode(code))
    })
    assert.equal(exitCode, 0, child.stderr.read()?.toString())
  } finally {
    if (!child.killed && child.exitCode === null) child.kill()
    await rm(dataDirectory, { recursive: true, force: true })
  }
})
