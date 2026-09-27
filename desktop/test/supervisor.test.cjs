'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const net = require('node:net');
const path = require('node:path');
const { ServiceSupervisor, probe } = require('../supervisor.cjs');
async function port() {
  const server = net.createServer();
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const p = server.address().port;
  await new Promise((r) => server.close(r));
  return p;
}
async function until(check, timeout = 8000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await check()) return;
    await new Promise((r) => setTimeout(r, 50));
  }
  throw Error('condition timed out');
}
function manager(p) {
  return new ServiceSupervisor({
    port: p,
    python: process.env.AIEYRA_CONTROL_PYTHON || 'python',
    script: path.join(__dirname, 'service-fixture.py'),
    cwd: __dirname,
    interval: 60,
    healthyInterval: 100,
    restartBase: 30,
    restartMax: 100,
    startupTimeout: 2500,
  });
}
test('owned service starts, restarts after crash, then exits with its owner', async () => {
  const p = await port(),
    s = manager(p);
  s.healthyInterval = 30000;
  try {
    s.start();
    await until(() => s.snapshot().state === 'ready');
    const first = s.child.pid;
    assert.equal(s.snapshot().ownership, 'owned');
    s.child.kill();
    await until(() => s.snapshot().state === 'ready' && s.child?.pid !== first);
    assert.ok(s.state.restarts >= 2);
    await s.stop();
    assert.equal((await probe(s.baseUrl)).kind, 'absent');
  } finally {
    await s.stop();
  }
});
test('unchanged healthy observations do not emit duplicate desktop updates', async () => {
  const s = manager(await port());
  const observations = [];
  s.on('state', (state) => observations.push(state));
  s.publish({ state: 'ready', ownership: 'external', version: 'fixture' });
  s.publish({ state: 'ready', ownership: 'external', version: 'fixture' });
  s.publish({ state: 'offline', error: 'connection lost' });
  assert.equal(observations.length, 2);
  assert.equal(observations[1].state, 'offline');
});
test('existing service is reused and survives another supervisor stopping', async () => {
  const p = await port(),
    owner = manager(p),
    client = manager(p);
  try {
    owner.start();
    await until(() => owner.state.state === 'ready');
    client.start();
    await until(() => client.state.state === 'ready');
    assert.equal(client.snapshot().ownership, 'external');
    assert.equal(client.child, null);
    await client.stop();
    assert.equal((await probe(owner.baseUrl)).kind, 'healthy');
  } finally {
    await client.stop();
    await owner.stop();
  }
});
test('external service restart never causes the desktop to take ownership', async () => {
  const p = await port(),
    owner = manager(p),
    client = manager(p);
  try {
    owner.start();
    await until(() => owner.state.state === 'ready');
    client.start();
    await until(() => client.state.state === 'ready');
    await owner.stop();
    await until(() => client.state.state === 'offline');
    await new Promise((r) => setTimeout(r, 200));
    assert.equal(client.child, null);
    assert.equal(client.state.restarts, 0);
    assert.equal((await probe(client.baseUrl)).kind, 'absent');
    owner.start();
    await until(() => client.state.state === 'ready');
    assert.equal(client.snapshot().ownership, 'external');
  } finally {
    await client.stop();
    await owner.stop();
  }
});
test('foreign service prevents spawning and remains running', async () => {
  const server = http.createServer((_req, res) =>
    res.end(JSON.stringify({ service: 'unrelated' })),
  );
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const s = manager(server.address().port);
  try {
    s.start();
    await until(() => s.state.state === 'conflict');
    assert.equal(s.child, null);
    assert.equal(s.state.restarts, 0);
    await s.stop();
    assert.ok(server.listening);
  } finally {
    await s.stop();
    await new Promise((r) => server.close(r));
  }
});
test('stop during readiness check cannot spawn an orphan', async () => {
  const s = manager(await port());
  s.start();
  await s.stop();
  await new Promise((r) => setTimeout(r, 100));
  assert.equal(s.child, null);
  assert.equal(s.state.state, 'stopped');
});
test('missing service stays visible as unavailable instead of spawning repeatedly', async () => {
  const s = manager(await port());
  s.script = path.join(__dirname, 'does-not-exist.py');
  try {
    s.start();
    await until(() => s.state.state === 'unavailable');
    assert.equal(s.state.restarts, 0);
  } finally {
    await s.stop();
  }
});
