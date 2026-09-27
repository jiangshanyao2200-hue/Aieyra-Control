import assert from 'node:assert/strict';
import test from 'node:test';
import { createAccountPanel } from '../../web/account.js';

test('late account refresh cannot undo a completed logout', async () => {
  globalThis.window = new EventTarget();
  let resolveOld;
  let reads = 0;
  const panel = createAccountPanel({
    request: (route) => {
      if (route.endsWith('/logout')) return Promise.resolve({});
      reads++;
      if (reads === 1) return new Promise((resolve) => (resolveOld = resolve));
      return Promise.resolve({ enabled: false });
    },
    onChange: () => {},
  });
  const stale = panel.refresh();
  await panel.action('logout');
  resolveOld({ enabled: true, user: { name: 'Previous session' } });
  await stale;
  assert.equal(panel.enabled(), false);
});

test('malformed account responses keep the last verified state readable', async () => {
  globalThis.window = new EventTarget();
  let calls = 0;
  const panel = createAccountPanel({
    request: async () => (++calls === 1 ? { enabled: true, user: { name: 'Fixture' } } : null),
    onChange: () => {},
  });
  await panel.refresh();
  await panel.refresh();
  assert.equal(panel.enabled(), true);
  assert.match(panel.markup(), /暂时无法连接/);
});
