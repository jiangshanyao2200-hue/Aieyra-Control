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
