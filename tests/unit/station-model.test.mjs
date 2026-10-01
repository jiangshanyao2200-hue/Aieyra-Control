import assert from 'node:assert/strict';
import test from 'node:test';
import { presence } from '../../web/station-model.js';

const now = Date.now();
const agent = (runtime = {}) => ({
  runtime: {
    bound: true,
    stale: false,
    state: 'running',
    last_activity_at: new Date(now).toISOString(),
    session_state: 'connected',
    lease_until: (now + 90000) / 1000,
    ...runtime,
  },
});

test('cached execution does not outlive an explicit communication lease', () => {
  assert.equal(presence(agent(), true, now).state, 'running');
  for (const lease_until of [(now - 1) / 1000, 0, 'bad']) {
    assert.notEqual(presence(agent({ lease_until }), true, now).state, 'running');
  }
  assert.notEqual(presence(agent({ session_state: 'disconnected' }), true, now).state, 'running');
});

test('native evidence without a transport lease still follows its own freshness', () => {
  const native = agent();
  delete native.runtime.session_state;
  delete native.runtime.lease_until;
  assert.equal(presence(native, true, now).state, 'running');
  assert.equal(presence(native, false, now).state, 'sync');
});

test('explicit execution report time takes precedence over transport observations', () => {
  for (const runtime_reported_at of [null, 'bad', new Date(now - 240000).toISOString()]) {
    const reported = agent({ runtime_reported_at, observed_at: new Date(now).toISOString() });
    assert.equal(presence(reported, true, now).state, 'sync');
  }
  assert.equal(
    presence(agent({ runtime_reported_at: new Date(now).toISOString() }), true, now).state,
    'running',
  );
});

test('legacy transport observations are not execution evidence', () => {
  const legacy = agent({ adapter: 'aieyra-agent/1', observed_at: new Date(now).toISOString() });
  assert.equal(presence(legacy, true, now).state, 'sync');
});
