import { createHash } from 'node:crypto';

export function matchesTextSha256(bytes, expected) {
  const raw = Buffer.from(bytes);
  const sha256 = (value) => createHash('sha256').update(value).digest('hex');
  if (sha256(raw) === expected) return true;

  const text = raw.toString('utf8');
  if (!Buffer.from(text, 'utf8').equals(raw) || /\r(?!\n)/.test(text)) return false;

  const lfText = text.replace(/\r\n/g, '\n');
  const lfBytes = Buffer.from(lfText, 'utf8');
  const crlfBytes = Buffer.from(lfText.replace(/\n/g, '\r\n'), 'utf8');
  return sha256(lfBytes) === expected || sha256(crlfBytes) === expected;
}
