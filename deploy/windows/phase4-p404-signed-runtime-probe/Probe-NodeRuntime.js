'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

const outputDirectory = process.env.P404_OUTPUT_DIRECTORY;
if (!outputDirectory) {
  throw new Error('P404_OUTPUT_DIRECTORY is required');
}

const markerPath = path.join(outputDirectory, 'runtime-probe-marker.tmp');
let markerCreated = false;
let markerReadBack = false;
let markerRemoved = false;
try {
  fs.writeFileSync(markerPath, 'phase4-p404-signed-runtime-probe\n', { flag: 'wx' });
  markerCreated = true;
  markerReadBack = fs.readFileSync(markerPath, 'utf8') === 'phase4-p404-signed-runtime-probe\n';
} finally {
  if (fs.existsSync(markerPath)) {
    fs.unlinkSync(markerPath);
  }
  markerRemoved = !fs.existsSync(markerPath);
}

process.stdout.write(JSON.stringify({
  schema_version: 1,
  kind: 'phase4_p404_signed_node_runtime_probe',
  runtime_version: process.version,
  platform: process.platform,
  architecture: process.arch,
  executable_path: process.execPath,
  builtin_crypto_sha256: crypto.createHash('sha256').update('phase4-p404').digest('hex'),
  writable_mapping: {
    marker_created: markerCreated,
    marker_read_back: markerReadBack,
    marker_removed: markerRemoved,
  },
  catalog_execution: 'not_started',
  network_request: 'not_attempted',
}));
