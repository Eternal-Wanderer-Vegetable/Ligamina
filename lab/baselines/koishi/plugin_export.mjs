function isPlugin(value) {
  return typeof value === 'function' || typeof value?.apply === 'function'
}

export function resolvePluginExport(moduleNamespace, label = 'plugin') {
  const visited = new Set()
  const find = (candidate) => {
    if (isPlugin(candidate)) return candidate
    if (!candidate || typeof candidate !== 'object' || visited.has(candidate)) return undefined
    visited.add(candidate)
    const keys = ['default', 'module.exports', ...Object.keys(candidate).sort()]
    for (const key of keys) {
      if (!(key in candidate)) continue
      const plugin = find(candidate[key])
      if (plugin) return plugin
    }
    return undefined
  }
  const plugin = find(moduleNamespace)
  if (plugin) return plugin

  const exports = moduleNamespace && typeof moduleNamespace === 'object'
    ? Object.keys(moduleNamespace).sort().join(',')
    : 'none'
  throw new TypeError(`Koishi plugin ${label} has no callable plugin export (exports: ${exports || 'none'})`)
}
