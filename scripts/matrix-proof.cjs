// Native device proof. Secrets travel only over stdin/stdout to the local vault.
const { generateKeyPairSync, createPrivateKey, createPublicKey, sign } = require('node:crypto');
let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (part) => {
  input += part;
  if (input.length > 100000) process.exit(2);
});
process.stdin.on('end', () => {
  try {
    const value = JSON.parse(input);
    if (value.action === 'generate') {
      const pair = generateKeyPairSync('ed25519');
      process.stdout.write(
        JSON.stringify({
          privateKey: pair.privateKey.export({ type: 'pkcs8', format: 'pem' }),
          publicKey: pair.publicKey.export({ type: 'spki', format: 'pem' }),
        }),
      );
    } else if (value.action === 'sign' && typeof value.message === 'string') {
      const key = createPrivateKey(value.privateKey);
      if (key.asymmetricKeyType !== 'ed25519') throw Error();
      process.stdout.write(
        JSON.stringify({
          signature: sign(null, Buffer.from(value.message), key).toString('base64url'),
          publicKey: createPublicKey(key).export({ type: 'spki', format: 'pem' }),
        }),
      );
    } else throw Error();
  } catch {
    process.stderr.write('matrix_proof_failed');
    process.exitCode = 2;
  }
});
