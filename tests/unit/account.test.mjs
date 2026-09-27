import assert from 'node:assert/strict';
import test from 'node:test';
import { createAccountPanel } from '../../web/account.js';

const authorizeUrl = 'https://api.aieyra.cn/aieyra/control/authorize?flow=' + 'a'.repeat(43);
function fixture(t, request, platform = true) {
  globalThis.window = new EventTarget();
  const opened = [],
    timers = [];
  if (platform)
    window.controlPlatform = {
      prepareLogin: async () => {},
      openLogin: async (url) => {
        opened.push(url);
        return true;
      },
      finishLogin: async () => {},
    };
  t.mock.method(globalThis, 'setTimeout', (fn) => {
    timers.push(fn);
    return fn;
  });
  t.mock.method(globalThis, 'clearTimeout', (fn) => {
    const i = timers.indexOf(fn);
    if (i >= 0) timers.splice(i, 1);
  });
  return { panel: createAccountPanel({ request, onChange() {} }), opened, timers };
}

test('login opens the official URL and retries transient polling failures', async (t) => {
  let polls = 0;
  const { panel, opened, timers } = fixture(t, async (route) => {
    if (route.endsWith('/login')) return { authorize_url: authorizeUrl, expires_in: 300 };
    if (++polls === 1) throw Object.assign(Error('offline'), { status: 503 });
    return { enabled: true, user: { name: 'Fixture' } };
  });
  await panel.action('login');
  assert.deepEqual(opened, [authorizeUrl]);
  await timers.shift()();
  assert.match(panel.markup(), /正在重试/);
  assert.match(panel.markup(), /继续登录/);
  await timers.shift()();
  assert.equal(panel.enabled(), true);
  assert.equal(timers.length, 0);
});

test('pending login survives page reload and expires only on terminal response', async (t) => {
  const { panel, timers, opened } = fixture(t, async (route) => {
    if (route === '/api/cloud')
      return { enabled: false, pending: true, authorize_url: authorizeUrl, expires_in: 200 };
    throw Object.assign(Error('expired'), { status: 401 });
  });
  await panel.refresh();
  assert.match(panel.markup(), /继续登录/);
  assert.deepEqual(opened, []);
  await timers.shift()();
  assert.match(panel.markup(), /登录已过期/);
  assert.doesNotMatch(panel.markup(), /继续登录/);
  assert.equal(timers.length, 0);
});

test('browser popup is reserved synchronously and cleared on start failure', async (t) => {
  let rejectStart,
    closed = false;
  const { panel } = fixture(
    t,
    () =>
      new Promise((_, reject) => {
        rejectStart = reject;
      }),
    false,
  );
  const popup = {
    opener: 'parent',
    close() {
      closed = true;
    },
  };
  window.open = () => popup;
  const pending = panel.action('login');
  assert.equal(popup.opener, null);
  await Promise.resolve();
  rejectStart(Error('offline'));
  await pending;
  assert.equal(closed, true);
  assert.match(panel.markup(), /暂未完成/);
});

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
