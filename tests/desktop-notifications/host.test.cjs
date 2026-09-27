'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { HumanNotificationHost } = require('../../desktop/human-notification-host.cjs');
const { HumanNotificationLedger } = require('../../desktop/human-notifications.cjs');
function setup(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'control-host-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const shown = [],
    opened = [];
  class FakeToast extends EventEmitter {
    static isSupported() {
      return true;
    }
    constructor(options) {
      super();
      this.options = options;
    }
    show() {
      shown.push(this);
      this.emit('show');
    }
    close() {
      this.emit('close');
    }
  }
  const ledger = new HumanNotificationLedger({ file: path.join(dir, 'ledger.json') });
  const host = new HumanNotificationHost({
    ledger,
    Notification: FakeToast,
    open: (id) => opened.push(id),
    aggregateMs: 60000,
  });
  host.start();
  t.after(() => host.stop());
  return {
    ledger,
    host,
    shown,
    opened,
    update: (state = 'waiting_user') =>
      ledger.update({
        observed_at: new Date().toISOString(),
        items: [
          {
            id: 'alpha-a',
            version: 1,
            state,
            category: 'information',
            question: 'private unprinted question',
          },
          { id: 'alpha-b', version: 1, state, category: 'environment' },
        ],
      }),
  };
}
test('one silent aggregate omits private questions and does not open/focus any window', (t) => {
  const f = setup(t);
  f.update();
  assert.equal(f.host.present(), true);
  assert.equal(f.shown.length, 1);
  assert.equal(f.shown[0].options.silent, true);
  assert.match(f.shown[0].options.title, /2 项/);
  assert.doesNotMatch(JSON.stringify(f.shown[0].options), /private|alpha-a/);
  assert.deepEqual(f.opened, []);
  assert.equal(f.host.present(), false);
});
test('only user click opens latest detail, duplicate clicks never dispatch a decision', (t) => {
  const f = setup(t);
  f.update();
  f.host.present();
  f.shown[0].emit('click');
  f.shown[0].emit('click');
  assert.deepEqual(f.opened, ['alpha-a']);
  assert.equal(f.ledger.snapshot().items[0].state, 'waiting_user');
});
test('active Control window, offline feed and submitted decision do not notify', (t) => {
  const f = setup(t);
  f.update();
  f.host.focused = () => true;
  assert.equal(f.host.present(), false);
  f.host.focused = () => false;
  f.ledger.unavailable();
  assert.equal(f.host.present(), false);
  f.update('decision_recorded');
  assert.equal(f.host.present(), false);
  assert.equal(f.shown.length, 0);
});
test('tray offers bounded snooze and settings but no approve/dispatch action', (t) => {
  const f = setup(t);
  f.update();
  const menu = f.host.menu();
  assert.match(menu[0].label, /需要你处理/);
  assert.match(menu[1].label, /补充信息/);
  menu.at(-1).submenu[0].click();
  assert.equal(f.host.present(), false);
  assert.equal(f.ledger.snapshot().items[0].state, 'waiting_user');
  assert.equal(f.opened.length, 0);
});
