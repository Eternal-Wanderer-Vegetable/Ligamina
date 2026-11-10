// Test-only deterministic Math.random preload. It activates only when a P7 seed
// is present and is injected into fresh, network-disabled Worker processes.
const rawSeed = process.env.LIGAMINA_BEHAVIOR_RANDOM_SEED
if (rawSeed !== undefined) {
  const parsed = Number(rawSeed)
  if (!Number.isSafeInteger(parsed) || parsed < 0) {
    throw new TypeError('LIGAMINA_BEHAVIOR_RANDOM_SEED must be a non-negative safe integer')
  }
  let state = parsed >>> 0
  Math.random = () => {
    state = (state + 0x6D2B79F5) >>> 0
    let value = state
    value = Math.imul(value ^ (value >>> 15), value | 1)
    value ^= value + Math.imul(value ^ (value >>> 7), value | 61)
    return ((value ^ (value >>> 14)) >>> 0) / 4294967296
  }
}
