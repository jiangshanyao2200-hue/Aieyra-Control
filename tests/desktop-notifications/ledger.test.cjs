'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { HumanNotificationLedger } = require('../../desktop/human-notifications.cjs');
const BASE = Date.parse('2026-09-22T15:00:00Z');
const item = (id = 'alpha-item', extra = {}) => ({
  id,
  version: 1,
  category: 'direction',
  state: 'waiting_user',
  ...extra,
});
function fixture(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'control-human-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  let now = BASE;
  const file = path.join(dir, 'ledger.json');
  const ledger = new HumanNotificationLedger({ file, now: () => now });
  return {
    ledger,
    file,
    clock: (value) => (now = value),
    update: (items, extra = {}) =>
      ledger.update({ items, observed_at: new Date(now).toISOString(), ...extra }),
  };
}
test('only real human categories and waiting states form a private aggregate', (t) => {
  const { ledger, update } = fixture(t);
  update([
    item('a'),
    item('b', { category: 'authorization' }),
    item('done', { state: 'resolved' }),
    item('sent', { state: 'delivered_to_agent' }),
  ]);
  const batch = ledger.reserve();
  assert.equal(batch.ids.length, 2);
  assert.equal(batch.total, 2);
  assert.deepEqual(batch.categories, ['方向选择', '具体授权']);
  assert.equal('question' in batch, false);
  assert.equal(ledger.snapshot().items.find((i) => i.id === 'sent').state, 'delivered_to_agent');
  assert.equal(ledger.reserve(), null);
});
test('reserve is durable before display, repeated polls/restart never replay uncertain toast', (t) => {
  const f = fixture(t);
  f.update([item()]);
  assert.ok(f.ledger.reserve());
  assert.equal(JSON.parse(fs.readFileSync(f.file)).seen['alpha-item@1'].state, 'reserved');
  const restarted = new HumanNotificationLedger({ file: f.file, now: () => BASE + 60000 });
  restarted.update({ items: [item()], observed_at: new Date(BASE + 60000).toISOString() });
  assert.equal(restarted.reserve(), null);
});
test('decisions, cancellation, expiry and non-human failures never become consent or notifications', (t) => {
  const f = fixture(t);
  f.update(
    [
      'decision_recorded',
      'delivered_to_agent',
      'resuming',
      'resolved',
      'failed',
      'unknown',
      'cancelled',
      'expired',
    ]
      .map((state, n) => item(`r${n}`, { state }))
      .concat(item('expired-time', { expires_at: new Date(BASE - 1).toISOString() })),
  );
  assert.equal(f.ledger.reserve(), null);
  assert.equal(f.ledger.snapshot().pending_count, 0);
  assert.equal(f.update([item('bad', { category: 'agent_failure' })]), false);
});
test('snooze persists, expires without deciding, new versions can notify once', (t) => {
  const f = fixture(t);
  f.update([item()]);
  assert.equal(f.ledger.snooze(15), true);
  assert.equal(f.ledger.reserve(), null);
  assert.equal(f.ledger.snooze(-1), false);
  f.clock(BASE + 16 * 60000);
  f.update([item()]);
  assert.ok(f.ledger.reserve());
  assert.equal(f.ledger.snapshot().items[0].state, 'waiting_user');
  f.clock(BASE + 17 * 60000);
  f.update([item('alpha-item', { version: 2 })]);
  assert.ok(f.ledger.reserve());
});
test('old polls and version regressions cannot revive a decided request', (t) => {
  const f = fixture(t);
  f.update([item('a', { version: 2, state: 'decision_recorded' })]);
  assert.equal(f.update([item('a')], { observed_at: new Date(BASE - 1000).toISOString() }), false);
  assert.equal(f.ledger.snapshot().items[0].state, 'decision_recorded');
  f.clock(BASE + 1000);
  assert.equal(f.update([item('a')]), false);
  assert.equal(f.ledger.reserve(), null);
});
test('offline, stale, timezone-free or future observations fail closed and preserve known status', (t) => {
  const f = fixture(t);
  f.update([item()]);
  f.ledger.unavailable('offline');
  assert.equal(f.ledger.reserve(), null);
  assert.equal(f.ledger.snapshot().pending_count, 1);
  assert.equal(f.ledger.snapshot().current, false);
  assert.equal(f.update([item()], { observed_at: '2026-09-22T15:00:00' }), false);
  assert.equal(f.update([item()], { observed_at: new Date(BASE + 120001).toISOString() }), false);
  f.update([item()], { stale: true });
  assert.equal(f.ledger.reserve(), null);
  f.update([item()]);
  f.clock(BASE + 120001);
  assert.equal(f.ledger.reserve(), null);
  assert.equal(f.ledger.snapshot().source, 'stale');
});
test('broken persistence is preserved and cannot reset deduplication or emit a toast', (t) => {
  const f = fixture(t);
  fs.writeFileSync(f.file, '{broken');
  const broken = new HumanNotificationLedger({ file: f.file, now: () => BASE });
  broken.update({ items: [item()], observed_at: new Date(BASE).toISOString() });
  assert.equal(broken.reserve(), null);
  assert.equal(broken.snooze(0), false);
  assert.equal(fs.readFileSync(f.file, 'utf8'), '{broken');
  assert.equal(broken.snapshot().persistence_error, 'ledger_unreadable');
});
test('duplicate IDs and unrecognized lifecycle states do not partially notify', (t) => {
  const f = fixture(t);
  assert.equal(f.update([item(), item()]), false);
  assert.equal(f.ledger.reserve(), null);
  assert.equal(f.update([item('a', { state: 'blocked' })]), false);
  assert.equal(f.ledger.reserve(), null);
});
test('presenter failure remains a known notification failure, not task failure or replay', (t) => {
  const f = fixture(t);
  f.update([item()]);
  const batch = f.ledger.reserve();
  f.ledger.presentation(batch, 'failed');
  f.clock(BASE + 60000);
  f.update([item()]);
  assert.equal(f.ledger.reserve(), null);
  assert.equal(f.ledger.snapshot().items[0].state, 'waiting_user');
  assert.equal(JSON.parse(fs.readFileSync(f.file)).seen['alpha-item@1'].state, 'failed');
});

const liveItem = (extra = {}) =>
  item('live-human', {
    question: '选择通知布局',
    recommendation: '建议短提示',
    facts: '两种布局均保留非打扰弹窗',
    impact: '仅记录设计方向',
    resume_summary: '保存设计决定',
    options: [
      { id: 'corner', label: '短提示与指挥台' },
      { id: 'side', label: '可收起侧栏' },
    ],
    input_schema: { allow_text: true },
    origin: {
      runtime_ref: 'session-r8',
      native_task_id: 'task-1',
      task_generation: 1,
      native_request_id: 'native-human-1',
    },
    ...extra,
  });

test('freshness versions of one unchanged live question never repeat across restart', (t) => {
  const f = fixture(t);
  f.update([liveItem()]);
  const initial = f.ledger.reserve();
  f.ledger.presentation(initial, 'shown');
  f.clock(BASE + 60000);
  f.update([liveItem({ version: 2 })]);
  assert.equal(f.ledger.reserve(), null);
  const restarted = new HumanNotificationLedger({ file: f.file, now: () => BASE + 120000 });
  restarted.update({
    items: [liveItem({ version: 3 })],
    observed_at: new Date(BASE + 120000).toISOString(),
  });
  assert.equal(restarted.reserve(), null);
  assert.equal(
    restarted.reference('live-human').version,
    3,
    'navigation still uses current CAS version',
  );
});

test('changed question, choices or original task generation can notify once', (t) => {
  const f = fixture(t);
  f.update([liveItem()]);
  assert.ok(f.ledger.reserve());
  f.clock(BASE + 60000);
  f.update([liveItem({ version: 2, question: '选择布局并确认文字密度' })]);
  assert.ok(f.ledger.reserve());
  f.clock(BASE + 120000);
  const options = [
    { id: 'corner', label: '更新后的短提示' },
    { id: 'side', label: '侧栏' },
  ];
  f.update([liveItem({ version: 3, options })]);
  assert.ok(f.ledger.reserve());
  f.clock(BASE + 180000);
  f.update([liveItem({ version: 4, options })]);
  assert.equal(f.ledger.reserve(), null);
  f.clock(BASE + 240000);
  f.update([liveItem({ version: 5, origin: { ...liveItem().origin, task_generation: 2 } })]);
  assert.ok(f.ledger.reserve());
});

test('existing exact-version ledger migrates without replay or storing question text', (t) => {
  const f = fixture(t);
  fs.writeFileSync(
    f.file,
    JSON.stringify({
      schema_version: 1,
      enabled: true,
      snoozed_until: 0,
      last_presented_at: BASE,
      seen: { 'live-human@3': { at: BASE, state: 'shown' } },
    }),
  );
  let now = BASE + 60000;
  const migrated = new HumanNotificationLedger({ file: f.file, now: () => now });
  migrated.update({ items: [liveItem({ version: 3 })], observed_at: new Date(now).toISOString() });
  assert.equal(migrated.reserve(), null);
  now += 60000;
  migrated.update({ items: [liveItem({ version: 4 })], observed_at: new Date(now).toISOString() });
  assert.equal(migrated.reserve(), null);
  const stored = fs.readFileSync(f.file, 'utf8');
  assert.doesNotMatch(stored, /选择通知布局|session-r8|短提示与指挥台/);
  assert.equal(JSON.parse(stored).seen['live-human@3'].state, 'shown');
});
