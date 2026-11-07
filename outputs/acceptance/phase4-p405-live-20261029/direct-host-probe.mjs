import { createRequire } from 'node:module'
import { HostLifecycle } from '/opt/ligamina/hosts/koishi/dist/lifecycle.js'

const requireFromHost = createRequire('/opt/ligamina/hosts/koishi/package.json')
const dataDirectory = '/tmp/p405-direct-host'
const pluginUrl = 'file:///opt/ligamina/p405-koishi-profile/node_modules/@koishijs/plugin-echo/lib/index.js'
const operations = []
const lifecycle = new HostLifecycle({
  hostId: 'p405-koishi-echo',
  dataDirectory,
  plugins: [pluginUrl],
  nicknames: ['Bot'],
  platformCall: async (operation) => {
    operations.push(operation)
    return { status: 'succeeded', platform_message_id: `fixture-${operations.length}` }
  },
})

try {
  await lifecycle.start()
  await lifecycle.app.database.createUser('onebot.v11', 'p405-synthetic-actor', { authority: 3 })
  await lifecycle.adapter.dispatch({
    schema_version: 'ligamina/0.1',
    event_id: 'p405-direct-host-event',
    trace_id: 'p405-direct-host-trace',
    platform: 'onebot.v11',
    account_id: 'stella-test-account',
    conversation: { kind: 'group', id: '501' },
    actor_id: 'p405-synthetic-actor',
    timestamp: new Date().toISOString(),
    target_host: 'p405-koishi-echo',
    message: [{ type: 'text', text: 'echo [Ligamina P4-05 TEST] fixture' }],
    extensions: { 'onebot.v11': { message_id: 'p405-synthetic-message', sender: { nickname: 'P4 fixture' } } },
  })
  console.log(JSON.stringify({
    event_dispatched: true,
    operation_count: operations.length,
    operations: operations.map((operation) => ({
      operation: operation.operation,
      target: operation.target,
      message: operation.params.message,
      trace_id: operation.trace_id,
    })),
  }))
} finally {
  await lifecycle.stop()
}
