import assert from 'node:assert/strict'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { test } from 'node:test'
import { HostLifecycle } from '../dist/lifecycle.js'
import { describeCommands } from '../dist/commands.js'
import { validateCommandDescriptor } from '../dist/contracts.js'

const pluginPath = pathToFileURL(resolve('dist/tool-smoke-plugin.js')).href

function toolEnvelope(id, text) {
  return {
    schema_version: 'ligamina/0.1',
    event_id: `tool-event-${id}`,
    trace_id: `tool-trace-${id}`,
    platform: 'ligamina.tool',
    account_id: 'ligamina-tool',
    conversation: { kind: 'private', id: `tool-actor-${id}` },
    actor_id: `tool-actor-${id}`,
    timestamp: '2026-11-07T00:00:00Z',
    target_host: 'koishi-tools',
    message: [{ type: 'text', text }],
    extensions: { 'ligamina.tool': { message_id: `tool-message-${id}` } },
  }
}

test('native Koishi commands are described with the v0.2 contract and run in a virtual private session', async () => {
  const dataDirectory = await mkdtemp(join(tmpdir(), 'ligamina-koishi-tools-'))
  const calls = []
  const platformCall = async (operation) => {
    calls.push(operation)
    if (operation.operation === 'send_message') return { status: 'succeeded', platform_message_id: `virtual-${calls.length}` }
    return { status: 'failed', error: { code: 'unsupported_in_tool_session' } }
  }
  const lifecycle = new HostLifecycle({ hostId: 'koishi-tools', platformCall, dataDirectory, plugins: [pluginPath] })
  try {
    await lifecycle.start()
    const commands = describeCommands(lifecycle.app)
    for (const descriptor of commands) assert.deepEqual(validateCommandDescriptor(descriptor), [], descriptor.command_id)
    const echo = commands.find((descriptor) => descriptor.command_id === 'ligamina.echo')
    assert.ok(echo, 'ligamina.echo is discovered')
    assert.equal(echo.framework, 'koishi')
    assert.equal(echo.description, 'Echo the given text back')
    assert.match(echo.usage, /^ligamina\.echo <value\.\.\.> \[-u, --upper\]$/)
    assert.deepEqual(echo.arguments, [{ name: 'value', required: true, type: 'text' }])
    assert.equal(echo.options[0].name, 'upper')
    assert.equal(echo.options[0].description, 'Convert to upper case')

    await lifecycle.adapter.dispatch(toolEnvelope('1', 'ligamina.echo -u hello tools'))
    const first = calls.filter((operation) => operation.trace_id === 'tool-trace-1')
    assert.deepEqual(first.map((operation) => [operation.operation, operation.account_id, operation.target, operation.params]), [
      ['send_message', 'ligamina-tool', { kind: 'private', id: 'tool-actor-1' }, { message: [{ type: 'text', text: 'HELLO TOOLS' }] }],
    ])

    await lifecycle.adapter.dispatch(toolEnvelope('2', 'ligamina.twice x'))
    const second = calls.filter((operation) => operation.trace_id === 'tool-trace-2').map((operation) => operation.params.message[0].text)
    assert.deepEqual(second, ['first:x', 'second:x'])

    await lifecycle.adapter.dispatch(toolEnvelope('3', 'ligamina.quote 77'))
    const third = calls.filter((operation) => operation.trace_id === 'tool-trace-3')
    // Without a remembered channel scope the adapter refuses the read before any platform call;
    // the plugin still gets a native error and replies normally.
    assert.deepEqual(third.map((operation) => operation.operation), ['send_message'])
    assert.match(third[0].params.message[0].text, /^read-failed:/)
  } finally {
    await lifecycle.stop()
    await rm(dataDirectory, { recursive: true, force: true })
  }
})
