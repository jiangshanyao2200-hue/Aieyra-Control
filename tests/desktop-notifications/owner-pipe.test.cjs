'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { PassThrough } = require('node:stream');
const { OSOwnerPipe, validOwnerPipeName, PIPE_PREFIX } = require('../../desktop/os-owner-pipe.cjs');
const owner = '0123456789abcdef0123456789abcdef';
function fixture(t, extra = {}) {
  const input = new PassThrough(),
    output = new PassThrough(),
    messages = [],
    stopped = [],
    failures = [];
  output.on('data', (data) =>
    messages.push(
      ...String(data)
        .trim()
        .split('\n')
        .filter(Boolean)
        .map((s) => JSON.parse(s)),
    ),
  );
  const pipe = new OSOwnerPipe({
    input,
    output,
    pid: 1234,
    shutdown: (reason) => stopped.push(reason),
    failed: (reason) => failures.push(reason),
    ...extra,
  });
  t.after(() => pipe.stop());
  pipe.start();
  return {
    pipe,
    input,
    output,
    messages,
    stopped,
    failures,
    send: (message) => input.write(JSON.stringify(message) + '\n'),
  };
}
test('owner ready waits for both actual shell readiness and first complete hello', (t) => {
  const f = fixture(t);
  f.pipe.ready();
  f.input.write('{"type":"owner.hello",');
  assert.deepEqual(f.messages, []);
  f.input.write(`"owner_id":"${owner}"}\n`);
  assert.deepEqual(f.messages, [
    { type: 'control.ready', owner_id: owner, pid: 1234, owned: true },
  ]);
  f.pipe.ready();
  assert.equal(f.messages.length, 1);
});
test('only matching owner shutdown exits, wrong ID and duplicate hello never steal ownership', (t) => {
  const f = fixture(t);
  f.send({ type: 'owner.hello', owner_id: owner });
  f.pipe.ready();
  f.send({ type: 'owner.shutdown', owner_id: 'different-owner-012345' });
  f.send({ type: 'owner.hello', owner_id: 'different-owner-012345' });
  assert.deepEqual(f.stopped, []);
  assert.equal(f.pipe.ownerId, owner);
  f.send({ type: 'owner.shutdown', owner_id: owner });
  f.send({ type: 'owner.shutdown', owner_id: owner });
  assert.deepEqual(f.stopped, ['owner_shutdown']);
});
test('bound inherited pipe EOF shuts down owner, no unbound pipe can acquire it', async (t) => {
  const f = fixture(t);
  f.send({ type: 'owner.hello', owner_id: owner });
  f.pipe.ready();
  f.input.end();
  await new Promise((r) => setImmediate(r));
  assert.deepEqual(f.stopped, ['owner_pipe_closed']);
  const g = fixture(t);
  g.input.end();
  await new Promise((r) => setImmediate(r));
  assert.deepEqual(g.failures, ['owner_handshake_missing']);
});
test('missing hello timeout fails new owner safely without claiming attached', async (t) => {
  const f = fixture(t, { timeout: 20 });
  await new Promise((r) => setTimeout(r, 40));
  assert.deepEqual(f.failures, ['owner_handshake_timeout']);
  assert.equal(f.messages[0].type, 'control.error');
});
test('malformed/oversize/control-before-hello have bounded explicit errors', (t) => {
  const f = fixture(t);
  f.input.write('{broken}\n');
  assert.deepEqual(f.failures, ['owner_message_invalid']);
  const g = fixture(t);
  g.input.write('x'.repeat(17000));
  assert.deepEqual(g.failures, ['owner_message_too_large']);
  const h = fixture(t);
  h.send({ type: 'owner.shutdown', owner_id: owner });
  assert.deepEqual(h.failures, ['owner_handshake_invalid']);
});
test('owner named channel is constrained to local random product pipe, never remote/TCP/path', () => {
  assert.equal(validOwnerPipeName(PIPE_PREFIX + '123456781234123412341234567890ab'), true);
  for (const value of [
    'tcp://127.0.0.1:1234',
    String.raw`\\remote\pipe\aieyra-control-owner-1234`,
    PIPE_PREFIX + '../other',
    PIPE_PREFIX + 'short',
    'C:/owner',
    PIPE_PREFIX + '12345678-1234-1234-1234-1234567890ab',
    PIPE_PREFIX + 'ABCDEF0123456789ABCDEF0123456789',
  ]) {
    assert.equal(validOwnerPipeName(value), false);
  }
});
