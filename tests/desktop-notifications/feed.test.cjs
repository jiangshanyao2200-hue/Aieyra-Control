'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { HumanRequestFeed, readJSON } = require('../../desktop/human-request-feed.cjs');
const { HumanNotificationLedger } = require('../../desktop/human-notifications.cjs');
const item = {
  id: 'alpha-real-reference',
  version: 1,
  category: 'direction',
  state: 'waiting_user',
};
const body = (extra = {}) => ({
  schema_version: 1,
  source: 'coordination',
  available: true,
  stale: false,
  observed_at: new Date().toISOString(),
  items: [item],
  ...extra,
});
function fixture(t, read) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'control-human-feed-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const ledger = new HumanNotificationLedger({ file: path.join(dir, 'ledger.json') });
  const feed = new HumanRequestFeed({ baseUrl: 'http://127.0.0.1:17910', ledger, read });
  feed.stopped = false;
  t.after(() => feed.stop());
  return { ledger, feed };
}
test('confirmed schema consumes fresh waiting items and separates recorded decision from resumed', async (t) => {
  let result = body();
  const f = fixture(t, async () => ({ status: 200, body: result }));
  await f.feed.tick();
  assert.equal(f.feed.snapshot().state, 'ready');
  assert.equal(f.ledger.snapshot().pending_count, 1);
  result = body({ items: [{ ...item, version: 2, state: 'decision_recorded' }] });
  await f.feed.tick();
  assert.equal(f.ledger.snapshot().items[0].state, 'decision_recorded');
  assert.equal(f.ledger.reserve(), null);
});
test('actual service unavailable response is not verified zero and preserves last observation', async (t) => {
  let result = body();
  const f = fixture(t, async () => ({ status: 200, body: result }));
  await f.feed.tick();
  result = body({
    available: false,
    stale: true,
    items: [],
    observed_at: null,
    reason: 'human_contract_not_connected',
  });
  await f.feed.tick();
  assert.equal(f.feed.snapshot().state, 'human_contract_not_connected');
  assert.equal(f.ledger.snapshot().pending_count, 1);
  assert.equal(f.ledger.snapshot().current, false);
  assert.equal(f.ledger.reserve(), null);
  assert.equal(f.feed.timer._idleTimeout, 60000);
  let updates = 0;
  f.feed.changed = () => updates++;
  f.ledger.on('change', () => updates++);
  await f.feed.tick();
  assert.equal(updates, 0);
});
test('unhealthy/foreign service never queried and simultaneous refreshes share one read', async (t) => {
  let count = 0,
    release;
  const f = fixture(t, () => {
    count++;
    return new Promise((r) => {
      release = r;
    });
  });
  f.feed.ready = () => false;
  await f.feed.tick();
  assert.equal(count, 0);
  f.feed.ready = () => true;
  const pending = f.feed.tick();
  await f.feed.tick();
  assert.equal(count, 1);
  release({ status: 200, body: body() });
  await pending;
  assert.equal(f.ledger.snapshot().current, true);
});
test('stop while response is pending cannot reanimate a fresh pending request', async (t) => {
  let release;
  const f = fixture(
    t,
    () =>
      new Promise((r) => {
        release = r;
      }),
  );
  const pending = f.feed.tick();
  f.feed.stop();
  release({ status: 200, body: body() });
  await pending;
  assert.equal(f.ledger.snapshot().current, false);
  assert.equal(f.ledger.reserve(), null);
});
test('missing endpoint, wrong schema, stale and dropped connection are distinct from empty success', async (t) => {
  let result = { status: 404, body: null };
  const f = fixture(t, async () => result);
  await f.feed.tick();
  assert.equal(f.feed.snapshot().state, 'endpoint_unavailable');
  result = { status: 200, body: body({ schema_version: 9 }) };
  await f.feed.tick();
  assert.equal(f.feed.snapshot().state, 'invalid_contract');
  result = { status: 200, body: body({ stale: true }) };
  await f.feed.tick();
  assert.equal(f.feed.snapshot().state, 'stale');
  assert.equal(f.ledger.reserve(), null);
  f.feed.read = async () => {
    throw Error('socket ended');
  };
  await f.feed.tick();
  assert.equal(f.feed.snapshot().state, 'offline');
});
test('HTTP reader uses fixed GET path, does not follow redirects, and rejects non-loopback/HTML', async (t) => {
  const requests = [];
  let mode = 'json';
  const server = http.createServer((req, res) => {
    requests.push({ method: req.method, url: req.url });
    if (mode === 'redirect') {
      res.writeHead(302, { location: 'https://example.invalid' });
      res.end();
    } else if (mode === 'html') {
      res.writeHead(200, { 'content-type': 'text/html' });
      res.end('<html>no</html>');
    } else {
      res.writeHead(200, { 'content-type': 'application/json; charset=utf-8' });
      res.end(JSON.stringify(body()));
    }
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  t.after(() => new Promise((r) => server.close(r)));
  const base = `http://127.0.0.1:${server.address().port}`;
  assert.equal((await readJSON(base)).body.schema_version, 1);
  mode = 'redirect';
  assert.equal((await readJSON(base)).status, 302);
  mode = 'html';
  await assert.rejects(readJSON(base), { code: 'INVALID_JSON' });
  await assert.rejects(readJSON('https://example.invalid'), { code: 'INVALID_ORIGIN' });
  assert.equal(requests.length, 3);
  assert.ok(requests.every((r) => r.method === 'GET' && r.url === '/api/human-requests'));
});
test('bounded HTTP body and absolute timeout stop stalled data without mutation', async (t) => {
  let mode = 'large';
  const server = http.createServer((_req, res) => {
    res.writeHead(200, { 'content-type': 'application/json' });
    if (mode === 'large') res.end('x'.repeat(500));
    else res.write('{');
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  t.after(() => {
    server.closeAllConnections();
    return new Promise((r) => server.close(r));
  });
  const base = `http://127.0.0.1:${server.address().port}`;
  await assert.rejects(readJSON(base, { maxBytes: 100 }), { code: 'RESPONSE_LIMIT' });
  mode = 'stall';
  await assert.rejects(readJSON(base, { timeout: 100 }), { code: 'READ_TIMEOUT' });
});
