// Test-only deterministic time/random preload. It activates only when P7
// controls are present in a fresh, network-disabled Worker process.
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

const rawFixedTime = process.env.LIGAMINA_BEHAVIOR_FIXED_TIME_UTC
if (rawFixedTime !== undefined) {
  const fixedMilliseconds = Date.parse(rawFixedTime)
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?Z$/.test(rawFixedTime)
      || !Number.isFinite(fixedMilliseconds)) {
    throw new TypeError('LIGAMINA_BEHAVIOR_FIXED_TIME_UTC must be an ISO-8601 UTC timestamp')
  }

  const NativeDate = Date
  function FixedDate(...args) {
    if (new.target) {
      return Reflect.construct(NativeDate, args.length === 0 ? [fixedMilliseconds] : args, new.target)
    }
    return new NativeDate(fixedMilliseconds).toString()
  }
  Object.setPrototypeOf(FixedDate, NativeDate)
  FixedDate.prototype = NativeDate.prototype
  Object.defineProperty(FixedDate, 'name', { value: 'Date' })
  FixedDate.now = () => fixedMilliseconds
  FixedDate.parse = NativeDate.parse
  FixedDate.UTC = NativeDate.UTC
  globalThis.Date = FixedDate
}
