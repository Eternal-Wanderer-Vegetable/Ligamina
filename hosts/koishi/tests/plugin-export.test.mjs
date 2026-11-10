import assert from 'node:assert/strict'
import test from 'node:test'
import { resolvePluginExport } from '../../../lab/baselines/koishi/plugin_export.mjs'

test('unwraps a default plugin nested in a CommonJS namespace default', () => {
  const plugin = () => {}
  const commonJsExports = { default: plugin }
  const namespace = { default: commonJsExports, 'module.exports': commonJsExports }

  assert.equal(resolvePluginExport(namespace, '@koishijs/plugin-database-memory'), plugin)
})

test('retains object plugins that expose an apply method', () => {
  const plugin = { apply() {} }

  assert.equal(resolvePluginExport({ default: plugin }, 'koishi-plugin-cron'), plugin)
})

test('accepts a callable named export for service providers', () => {
  class HTTP {}

  assert.equal(resolvePluginExport({ HTTP }, '@koishijs/plugin-http'), HTTP)
})
