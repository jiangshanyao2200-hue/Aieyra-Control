'use strict';
const fs = require('node:fs'),
  path = require('node:path'),
  crypto = require('node:crypto');
let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => {
  input += chunk;
  if (Buffer.byteLength(input) > 2 * 1024 * 1024) process.exit(2);
});
process.stdin.on('end', () => {
  try {
    const envelope = JSON.parse(input),
      payload = Buffer.from(envelope.payload, 'base64');
    const publicKey = fs.readFileSync(path.join(__dirname, '../config/release-public.pem'));
    if (!crypto.verify(null, payload, publicKey, Buffer.from(envelope.signature, 'base64')))
      throw Error('invalid signature');
    const manifest = JSON.parse(payload.toString('utf8'));
    if (
      manifest.product !== 'aieyra-control' ||
      manifest.schema !== 1 ||
      !Number.isSafeInteger(manifest.sequence) ||
      manifest.sequence < 1
    )
      throw Error('invalid manifest');
    if (JSON.stringify(manifest) !== JSON.stringify(envelope.manifest))
      throw Error('display manifest mismatch');
    process.stdout.write(JSON.stringify(manifest));
  } catch {
    process.stderr.write('release_signature_invalid');
    process.exitCode = 1;
  }
});
