import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { matchesTextSha256 } from './evidence-hash.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const digest = (bytes) => createHash('sha256').update(bytes).digest('hex');

test('accepts the recorded S2-07 report hash from an LF checkout', async () => {
  const relative = 'outputs/acceptance/phase2-s207-cli-smoke-20261012.json';
  const reporting = JSON.parse(await readFile(path.join(root, 'outputs/acceptance/phase2-s208-20261012.json'), 'utf8'));
  const originalBytes = await readFile(path.join(root, relative));
  const lfBytes = Buffer.from(originalBytes.toString('utf8').replace(/\r\n/g, '\n'), 'utf8');
  const expected = reporting.input_reports[relative];

  assert.notEqual(digest(lfBytes), expected, 'the fixture must reproduce the historical CRLF/LF hash mismatch');
  assert.equal(matchesTextSha256(lfBytes, expected), true);
});

test('still rejects text changes beyond line endings', () => {
  const crlfBytes = Buffer.from('first line\r\nsecond line\r\n', 'utf8');
  const changedLfBytes = Buffer.from('first line\nchanged line\n', 'utf8');

  assert.equal(matchesTextSha256(changedLfBytes, digest(crlfBytes)), false);
});

test('accepts an LF digest from a CRLF checkout', () => {
  const lfBytes = Buffer.from('first line\nsecond line\n', 'utf8');
  const crlfBytes = Buffer.from('first line\r\nsecond line\r\n', 'utf8');

  assert.equal(matchesTextSha256(crlfBytes, digest(lfBytes)), true);
});

test('does not normalize invalid UTF-8 bytes', () => {
  const crlfBytes = Buffer.from([0x61, 0x0d, 0x0a, 0xff]);
  const lfBytes = Buffer.from([0x61, 0x0a, 0xff]);

  assert.equal(matchesTextSha256(lfBytes, digest(crlfBytes)), false);
});
