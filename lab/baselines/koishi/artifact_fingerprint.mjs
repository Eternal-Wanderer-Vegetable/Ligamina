import { createHash } from 'node:crypto'

export function fingerprintArtifact(bytes, algorithm) {
  if (algorithm === 'sha256') return createHash('sha256').update(bytes).digest('hex')
  if (algorithm === 'sri-sha512') return `sha512-${createHash('sha512').update(bytes).digest('base64')}`
  throw new Error(`unsupported artifact fingerprint algorithm: ${algorithm}`)
}
