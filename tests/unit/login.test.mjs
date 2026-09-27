import assert from 'node:assert/strict';
import test from 'node:test';
import { EventEmitter } from 'node:events';
import { createRequire } from 'node:module';
const { LoginWindow, validLoginUrl, portalUrl } = createRequire(import.meta.url)(
  '../../desktop/login.cjs',
);
const url = 'https://api.aieyra.cn/aieyra/control/authorize?flow=' + 'a'.repeat(43);

test('desktop login opens only an exact official authorization URL', () => {
  assert.equal(validLoginUrl(url), true);
  for (const bad of [
    'file:///tmp/private',
    url.replace('api.aieyra.cn', 'api.aieyra.cn.evil.invalid'),
    url.replace('https://', 'https://user@'),
    url + '&redirect=https://evil.invalid',
    url + '&flow=' + 'b'.repeat(43),
    url + '#extra',
  ])
    assert.equal(validLoginUrl(bad), false);
  assert.equal(portalUrl('https://api.aieyra.cn/sign-in'), true);
  assert.equal(portalUrl('https://ctrlupdate.aieyra.cn/auth/callback?code=x'), true);
  assert.equal(portalUrl('https://ctrlupdate.aieyra.cn/v1/session'), false);
});

test('native login shows immediately, isolates remote content and returns to its owner', async () => {
  const windows = [],
    permissions = {},
    owner = {
      shown: 0,
      focused: 0,
      isDestroyed: () => false,
      show() {
        this.shown++;
      },
      focus() {
        this.focused++;
      },
    };
  class Window extends EventEmitter {
    constructor(options) {
      super();
      this.options = options;
      this.urls = [];
      this.destroyed = false;
      this.webContents = new EventEmitter();
      this.webContents.setWindowOpenHandler = (callback) => {
        this.popup = callback;
      };
      this.webContents.stop = () => {};
      windows.push(this);
    }
    loadURL(value) {
      this.urls.push(value);
      return Promise.resolve();
    }
    focus() {
      this.focused = true;
    }
    isDestroyed() {
      return this.destroyed;
    }
    destroy() {
      this.destroyed = true;
      this.emit('closed');
    }
  }
  const controller = new LoginWindow({
    BrowserWindow: Window,
    session: {
      fromPartition(name) {
        assert.equal(name, 'persist:control-account');
        return {
          setPermissionRequestHandler(value) {
            permissions.request = value;
          },
          setPermissionCheckHandler(value) {
            permissions.check = value;
          },
        };
      },
    },
  });
  assert.equal(controller.prepare(owner), true);
  const portal = windows[0];
  assert.equal(portal.options.show, true);
  assert.equal(portal.options.webPreferences.nodeIntegration, false);
  assert.equal(portal.options.webPreferences.sandbox, true);
  assert.equal(portal.options.webPreferences.preload, undefined);
  assert.ok(portal.urls[0].startsWith('data:text/html'));
  assert.equal(controller.open(url, owner), true);
  assert.equal(windows.length, 1);
  assert.equal(portal.urls.at(-1), url);
  let denied = false;
  portal.webContents.emit(
    'will-navigate',
    {
      preventDefault() {
        denied = true;
      },
    },
    'file:///secret',
  );
  assert.equal(denied, true);
  assert.equal(permissions.check(), false);
  assert.deepEqual(portal.popup({ url: 'https://evil.invalid' }), { action: 'deny' });
  controller.fail();
  assert.match(decodeURIComponent(portal.urls.at(-1)), /暂时无法打开登录页/);
  controller.close(true);
  assert.equal(portal.destroyed, true);
  assert.equal(owner.shown, 1);
  assert.equal(owner.focused, 1);
});
