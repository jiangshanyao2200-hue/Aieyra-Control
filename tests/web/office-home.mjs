import { chromium } from 'playwright';
import { createServer } from 'node:http';
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';
import { fixture } from './fixture.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const out =
  process.env.CONTROL_TEST_OUTPUT ||
  path.join(root, 'tests/test-output', `office-home-${Date.now()}-${process.pid}`);
await mkdir(out, { recursive: true });
let historyItems = null,
  historyDelay = 0,
  accountEnabled = false,
  accountPending = false,
  accountCalls = [];
let snapshot,
  registry,
  fail = new Set(),
  memoryDelay = 0,
  writes = [],
  reads = [],
  projection;
const longText =
  '这是完整的工作动态，包含具体实施过程、验证证据与下一步。\n'.repeat(90) + '全文结束标记';
function reset() {
  accountEnabled = false;
  accountPending = false;
  accountCalls = [];
  snapshot = fixture();
  const now = new Date().toISOString();
  snapshot.version = '0.5.3 · fixture';
  snapshot.agents = Array.from({ length: 8 }, (_, i) => ({
    id: 'agent-' + i,
    name: [
      'Control · 项目领导',
      'Workspace · 客户端',
      'OS · 原生运行时',
      '共享池 · 可靠性',
      '官网 · 体验设计',
      '视觉 · 资源维护',
      '协作 · 交接归档',
      '工具 · 执行验证',
    ][i],
    project: snapshot.projects[i % 4].id,
    last_seen: i < 3 ? Date.now() / 1000 : 1,
    runtime: {
      bound: i === 0,
      stale: i !== 0,
      state: i === 0 ? 'running' : 'unknown',
      session_state: i === 0 ? 'connected' : 'expired',
      last_activity_at: i === 0 ? now : '2020-01-01T00:00:00Z',
    },
  }));
  registry = {
    seats: snapshot.agents.map((a, i) => ({
      id: 'seat-' + i,
      name: a.name,
      project: a.project,
      actor_id: a.id,
      membership_state: 'active',
      desired_state: 'registered',
      ownership: 'external',
      station_binding: { native_session_id: 'native-' + i },
    })),
    governance: [{ actor_id: 'agent-0', role: 'leader', active: true, projects: ['control'] }],
    hosts: [],
  };
  snapshot.messages = Array.from({ length: 14 }, (_, i) => ({
    id: 'm' + i,
    sender: 'agent-' + (i % 8),
    sender_name: snapshot.agents[i % 8].name,
    project: snapshot.agents[i % 8].project,
    kind: i % 3 === 0 ? 'handoff' : 'progress',
    body:
      i === 13
        ? longText
        : [
            '接入状态已经核对，下一步继续验证界面和任务回执。',
            '共享资料已整理，项目蓝图与恢复记录保持同一版本。',
            '正在处理本轮用户要求，产物与检查结果稍后补充。',
          ][i % 3],
    seq: i + 1,
    created: Date.now() / 1000 - (14 - i) * 50,
  }));
  snapshot.deliveries = [];
  snapshot.tasks[0].owner = 'agent-0';
  snapshot.tasks[0].lease_until = Date.now() / 1000 + 300;
  snapshot.tasks[1].owner = 'agent-1';
  snapshot.tasks[1].lease_until = Date.now() / 1000 - 30;
  historyItems = null;
  historyDelay = 0;
  fail = new Set();
  memoryDelay = 0;
  writes = [];
  reads = [];
  projection = { available: false, tasks: [], runtime_stations: [] };
}
reset();
const server = createServer(async (req, res) => {
  try {
    const u = new URL(req.url, 'http://localhost'),
      send = (code, v) => {
        res.writeHead(code, { 'content-type': 'application/json' });
        res.end(JSON.stringify(v));
      };
    if (req.method === 'POST' && u.pathname.startsWith('/api/cloud/')) {
      accountCalls.push(u.pathname);
      let raw = '';
      for await (const part of req) raw += part;
      if (req.headers['x-control-csrf'] !== 'fixture-csrf' || raw !== '{}') return send(403, {});
      if (u.pathname.endsWith('/login')) {
        accountPending = true;
        return send(200, {
          authorize_url: 'https://api.aieyra.cn/aieyra/control/authorize?flow=' + 'a'.repeat(43),
          expires_in: 300,
        });
      }
      if (u.pathname.endsWith('/poll')) {
        accountEnabled = true;
        return send(200, { enabled: true, user: { name: 'My Account' } });
      }
      if (u.pathname.endsWith('/logout')) {
        accountEnabled = false;
        accountPending = false;
        return send(200, { enabled: false });
      }
      return send(200, { available: false });
    }
    if (req.method !== 'GET') {
      writes.push(req.url);
      return send(405, { error: 'read_only' });
    }
    if (u.pathname === '/api/session') return send(200, { csrf: 'fixture-csrf' });
    if (u.pathname.startsWith('/api/')) reads.push(u.pathname);
    if (fail.has(u.pathname)) return send(503, { error: 'synthetic_read_failure' });
    if (u.pathname === '/api/snapshot') return send(200, snapshot);
    if (u.pathname === '/api/chat-history') {
      if (historyDelay) await new Promise((r) => setTimeout(r, historyDelay));
      const now = Date.now() / 1000,
        before = Number(u.searchParams.get('before') || Number.MAX_SAFE_INTEGER);
      const rows = (historyItems || snapshot.messages)
        .filter((m) => m.created >= now - 86400 && m.created <= now && m.seq < before)
        .sort((a, b) => b.seq - a.seq);
      const chosen = rows.slice(0, 100);
      return send(200, {
        messages: chosen.slice().reverse(),
        has_more: rows.length > 100,
        next_before: rows.length > 100 ? chosen.at(-1).seq : null,
        retention_hours: 24,
        window_start: now - 86400,
        window_end: now,
      });
    }
    if (u.pathname === '/api/registry') return send(200, registry);
    if (u.pathname === '/api/project-memory') {
      const project = u.searchParams.get('project');
      if (!project)
        return send(200, {
          projects: snapshot.projects.map((p) => ({
            ...p,
            version: 3,
            summary: '本轮项目断点与恢复资料。',
          })),
        });
      if (memoryDelay) await new Promise((r) => setTimeout(r, memoryDelay));
      if (u.searchParams.has('history'))
        return send(200, {
          revisions: [
            { version: 3, summary: '当前工作段', created_at: new Date().toISOString() },
            { version: 2, summary: '上一个工作段', created_at: '2026-09-25T01:00:00Z' },
          ],
        });
      return send(200, {
        current_version: 3,
        project: { id: project },
        memory: {
          project,
          version: Number(u.searchParams.get('version') || 3),
          created_at: new Date().toISOString(),
          summary: '项目 ' + project + ' 的档案',
          sections: Object.fromEntries(
            ['blueprint', 'timeline', 'checkpoint', 'recovery', 'index'].map((k) => [
              k,
              project + ' / ' + k + '\n' + '项目长期记录。'.repeat(35),
            ]),
          ),
        },
      });
    }
    if (u.pathname === '/api/human-requests')
      return send(200, {
        items: [
          {
            id: 'h1',
            question: '报告采用哪个版本？',
            facts: '目前有两个候选版本。',
            recommendation: '在 Agent 会话中核对。',
            state: 'waiting_user',
            origin: { project: 'control' },
            options: [{ id: 'a', label: '当前版本', description: '已检查' }],
          },
        ],
      });
    if (u.pathname === '/api/requirements')
      return send(200, {
        items: [{ id: 'req1', project: 'control', summary: '本轮只读观察界面', version: 2 }],
      });
    if (u.pathname === '/api/requirement')
      return send(200, {
        id: u.searchParams.get('id'),
        project: 'control',
        summary: '本轮只读观察界面',
        description: '原始需求完整说明',
        tasks: snapshot.tasks,
        version: 2,
      });
    if (u.pathname === '/api/cloud')
      return send(200, {
        enabled: accountEnabled,
        user: accountEnabled ? { name: 'My Account' } : null,
        mode: accountEnabled ? 'cloud_enabled' : 'local',
      });
    if (u.pathname === '/api/collaboration') return send(200, projection);
    if (u.pathname.startsWith('/api/')) return send(404, {});
    const name = u.pathname === '/' ? 'index.html' : u.pathname.slice(1);
    if (!/^[\w.-]+$/.test(name)) return send(404, {});
    res.writeHead(200, {
      'content-type':
        {
          '.html': 'text/html',
          '.js': 'text/javascript',
          '.css': 'text/css',
          '.svg': 'image/svg+xml',
        }[path.extname(name)] || 'text/plain',
    });
    res.end(await readFile(path.join(root, 'web', name)));
  } catch (e) {
    res.writeHead(500);
    res.end(String(e));
  }
});
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const base = `http://127.0.0.1:${server.address().port}`;
const browser = await chromium.launch({
  headless: true,
  executablePath: process.env.CONTROL_CHROME,
});
let page, context;
const results = [],
  errors = [];
async function open(size = { width: 1440, height: 940 }, touch = false) {
  context = await browser.newContext({ viewport: size, hasTouch: touch, isMobile: touch });
  page = await context.newPage();
  page.setDefaultTimeout(6000);
  page.on('pageerror', (e) => errors.push(String(e)));
  await page.goto(base);
  await page.waitForFunction(() => document.querySelector('#main')?.dataset.refreshing === 'false');
}
async function refresh() {
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
  await page.waitForFunction(() => document.querySelector('#main').dataset.refreshing === 'false');
}
async function test(name, fn) {
  if (process.env.CONTROL_TEST_FILTER && !name.includes(process.env.CONTROL_TEST_FILTER)) return;
  try {
    await fn();
    results.push({ name, state: 'PASS' });
    console.log('PASS ' + name);
  } catch (e) {
    results.push({ name, state: 'FAIL', error: String(e.stack) });
    console.error('FAIL ' + name + ' ' + e.message);
    await page
      ?.screenshot({ path: path.join(out, 'failure-' + results.length + '.png') })
      .catch(() => {});
  } finally {
    await context?.close();
    reset();
  }
}
async function chat() {
  await page.locator('#open-chat').click();
  await page.waitForFunction(
    () => !document.querySelector('#chat-boundary')?.textContent.includes('正在读取'),
  );
}
function manyHistory(n = 245) {
  const now = Date.now() / 1000;
  historyItems = Array.from({ length: n }, (_, i) => ({
    id: 'hist-' + i,
    seq: i + 1,
    sender: 'agent-0',
    sender_name: '项目领导',
    created: now - (n - i) * 20,
    body: '历史消息 ' + i + ' · 完整内容用于滚轮阅读和锚点验证。',
  }));
  snapshot.messages = historyItems.slice(-100).reverse();
}
try {
  await test('home-unframed-stations-and-three-sidebar-entrypoints', async () => {
    await open();
    assert.equal(await page.locator('.home-seat').count(), 8);
    assert.equal(await page.locator('.home-orb').count(), 3);
    assert.equal(
      await page
        .locator(
          '#office-world,canvas,.room-floor,.room-base,.seat-caption rect,nav:not(.home-sidebar),form,textarea',
        )
        .count(),
      0,
    );
    assert.ok(!(await page.locator('#office-dialog').isVisible()));
    const info = await page
      .locator('.home-seat')
      .first()
      .evaluate((e) => ({
        bg: getComputedStyle(e).backgroundColor,
        border: getComputedStyle(e).borderWidth,
      }));
    assert.deepEqual(info, { bg: 'rgba(0, 0, 0, 0)', border: '0px' });
    assert.deepEqual([...new Set(reads)].sort(), ['/api/cloud', '/api/registry', '/api/snapshot']);
    await page.screenshot({ path: path.join(out, 'home-desktop.png') });
  });
  await test('station-name-code-state-and-stable-identity', async () => {
    await open();
    const codes = await page.locator('.seat-code').allTextContents();
    assert.equal(new Set(codes).size, 8);
    assert.ok(codes.every(Boolean));
    registry.seats.reverse();
    await refresh();
    assert.deepEqual(await page.locator('.seat-code').allTextContents(), codes);
    assert.equal(await page.locator('.seat-status.running').count(), 1);
    assert.ok(!(await page.locator('#main').innerText()).includes('离线'));
  });
  await test('communication-and-task-leases-remain-distinct-from-past-runtime', async () => {
    const r = snapshot.agents[0].runtime;
    r.lease_until = Date.now() / 1000 + 90;
    r.last_reported_state = 'running';
    await open();
    await page.locator('[data-seat=seat-0]').click();
    let text = await page.locator('#dialog-body').innerText();
    assert.ok(
      text.includes('通信租约') &&
        text.includes('已连接') &&
        text.includes('任务租约') &&
        text.includes('最后运行上报'),
    );
    r.bound = false;
    r.stale = true;
    r.state = 'unknown';
    r.session_state = 'disconnected';
    r.lease_until = 0;
    await refresh();
    text = await page.locator('#dialog-body').innerText();
    assert.ok(
      text.includes('已断开') &&
        text.includes('无有效租约') &&
        text.includes('最后上报不代表当前仍在执行'),
    );
    assert.equal(await page.locator('[data-seat=seat-0] .seat-status.running').count(), 0);
    await page.keyboard.press('Escape');
    await page.locator('#open-tasks').click();
    await page.locator('[data-task]').first().click();
    text = await page.locator('#dialog-body').innerText();
    assert.ok(text.includes('任务租约') && text.includes('通信租约'));
    await page.screenshot({ path: path.join(out, 'lease-details.png') });
    fail = new Set(['/api/snapshot']);
    await refresh();
    assert.ok((await page.locator('#dialog-body').innerText()).includes('状态待同步'));
  });
  await test('no-drag-no-canvas-zoom-no-hover-window', async () => {
    await open();
    const s = page.locator('[data-seat=seat-0]'),
      a = await s.boundingBox();
    await s.hover();
    assert.equal(await page.locator('[role=tooltip]').count(), 0);
    await page.mouse.move(600, 32);
    await page.mouse.down();
    await page.mouse.move(810, 130, { steps: 4 });
    await page.mouse.up();
    await page.mouse.wheel(0, -220);
    assert.deepEqual(await s.boundingBox(), a);
    assert.equal(await page.locator('#office-dialog[open]').count(), 0);
  });
  await test('native-station-dialog-full-content-close-restores-focus', async () => {
    await open();
    await page.locator('[data-seat=seat-5]').click();
    assert.equal(await page.locator('dialog[open]').count(), 1);
    assert.ok((await page.locator('#dialog-body').innerText()).includes('全文结束标记'));
    assert.equal(
      await page.locator('#dialog-body').evaluate((e) => getComputedStyle(e).overflowY),
      'auto',
    );
    await page.screenshot({ path: path.join(out, 'station-dialog.png') });
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('dialog[open]').count(), 0);
    assert.equal(await page.evaluate(() => document.activeElement.dataset.seat), 'seat-5');
  });
  await test('dialog-focus-trap-and-backdrop', async () => {
    await open();
    await page.locator('[data-seat]').first().click();
    for (let i = 0; i < 12; i++) {
      await page.keyboard.press('Tab');
      assert.ok(
        await page.evaluate(() =>
          document.querySelector('#office-dialog').contains(document.activeElement),
        ),
      );
    }
    await page.mouse.click(8, 8);
    assert.equal(await page.locator('dialog[open]').count(), 0);
  });
  await test('chat-window-expand-restore-and-manual-resize', async () => {
    await open();
    await chat();
    const d = page.locator('#office-dialog'),
      before = await d.boundingBox();
    await page.locator('#dialog-max').click();
    const max = await d.boundingBox();
    assert.ok(max.width > before.width && max.height > before.height);
    await page.locator('#dialog-max').click();
    assert.deepEqual(await d.boundingBox(), before);
    await page.mouse.move(before.x + before.width - 3, before.y + before.height - 3);
    await page.mouse.down();
    await page.mouse.move(before.x + before.width + 65, before.y + before.height + 40, {
      steps: 4,
    });
    await page.mouse.up();
    const resized = await d.boundingBox();
    assert.ok(resized.width > before.width + 20);
    await page.screenshot({ path: path.join(out, 'chat-resized.png') });
  });
  await test('chat-retention-only-last-24-hours-and-latest-bottom', async () => {
    manyHistory();
    historyItems.push({
      ...historyItems[0],
      id: 'old-record',
      seq: 999,
      created: Date.now() / 1000 - 90000,
      body: '旧消息不得显示',
    });
    await open();
    await chat();
    assert.equal(await page.locator('[data-message]').count(), 100);
    assert.ok(!(await page.locator('#dialog-body').innerText()).includes('旧消息不得显示'));
    assert.ok((await page.locator('#dialog-footer').innerText()).includes('最近24小时'));
    assert.ok(
      await page
        .locator('#dialog-body')
        .evaluate((e) => e.scrollHeight - e.scrollTop - e.clientHeight < 5),
    );
    await page.screenshot({ path: path.join(out, 'chat-latest.png') });
  });
  await test('wheel-loads-older-history-and-preserves-reading-anchor', async () => {
    manyHistory();
    await open();
    await chat();
    const body = page.locator('#dialog-body');
    await body.evaluate((e) => (e.scrollTop = 110));
    const before = await page.locator('[data-message]').first().getAttribute('data-message');
    await body.hover();
    await page.mouse.wheel(0, -450);
    await page.waitForFunction(() => document.querySelectorAll('[data-message]').length === 200);
    assert.ok(await body.evaluate((e) => e.scrollTop > 1000));
    assert.ok(await page.locator(`[data-message="${before}"]`).count());
    await body.evaluate((e) => (e.scrollTop = 0));
    await page.waitForFunction(() => document.querySelectorAll('[data-message]').length === 245);
    assert.ok((await page.locator('#chat-boundary').innerText()).includes('起点'));
    assert.ok(reads.filter((x) => x === '/api/chat-history').length >= 3);
  });
  await test('new-chat-keeps-reading-position-and-latest-button', async () => {
    manyHistory();
    await open();
    await chat();
    const b = page.locator('#dialog-body');
    await b.evaluate((e) => (e.scrollTop = 750));
    const old = await b.evaluate((e) => e.scrollTop);
    snapshot.messages.unshift({
      ...historyItems.at(-1),
      id: 'new-record',
      seq: 999,
      created: Date.now() / 1000,
      body: '最新消息',
    });
    await refresh();
    assert.ok(Math.abs((await b.evaluate((e) => e.scrollTop)) - old) < 2);
    assert.ok(await page.locator('[data-latest]').isVisible());
    await page.locator('[data-latest]').click();
    assert.ok(await b.evaluate((e) => e.scrollHeight - e.scrollTop - e.clientHeight < 5));
    assert.ok(await page.locator('[data-latest]').isHidden());
  });
  await test('history-failure-retry-and-close-reopen-race', async () => {
    await open();
    fail.add('/api/chat-history');
    await chat();
    assert.ok((await page.locator('#chat-boundary').innerText()).includes('暂时'));
    fail.clear();
    await page.locator('[data-history-retry]').click();
    await page.waitForFunction(() => document.querySelectorAll('[data-message]').length === 14);
    await page.keyboard.press('Escape');
    historyDelay = 350;
    await page.locator('#open-chat').click();
    await page.keyboard.press('Escape');
    await page.locator('#open-chat').click();
    await page.waitForTimeout(650);
    assert.equal(await page.locator('[data-message]').count(), 14);
    assert.ok(!(await page.locator('#chat-boundary').innerText()).includes('正在读取'));
  });
  await test('chat-long-text-expansion-survives-refresh', async () => {
    await open();
    await chat();
    await page.locator('.chat-record details summary').click();
    assert.ok((await page.locator('#dialog-body').innerText()).includes('全文结束标记'));
    await refresh();
    assert.equal(await page.locator('.chat-record details[open]').count(), 1);
  });
  await test('tasks-list-status-filter-detail-back', async () => {
    await open();
    await page.locator('#open-tasks').click();
    assert.ok((await page.locator('#dialog-body').innerText()).includes('接单已过期'));
    await page.locator('[data-filter=attention]').click();
    assert.ok((await page.locator('.home-task').count()) > 0);
    await page.locator('.home-task').first().click();
    assert.ok((await page.locator('#dialog-title').innerText()).includes('详情'));
    await page.keyboard.press('Escape');
    assert.equal(
      await page.locator('[data-filter=attention]').getAttribute('aria-pressed'),
      'true',
    );
    assert.equal(await page.locator('dialog[open]').count(), 1);
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('dialog[open]').count(), 0);
    assert.equal(await page.evaluate(() => document.activeElement.id), 'open-tasks');
    await page.locator('#open-tasks').click();
    await page.screenshot({ path: path.join(out, 'tasks.png') });
  });
  await test('failed-and-malformed-reads-preserve-stations', async () => {
    await open();
    fail = new Set(['/api/snapshot', '/api/registry']);
    await refresh();
    assert.equal(await page.locator('.home-seat').count(), 8);
    assert.equal(await page.locator('.seat-status').filter({ hasText: '状态待同步' }).count(), 8);
    assert.ok((await page.locator('#home-sync').innerText()).includes('保留'));
    fail.clear();
    registry = { seats: 'bad', governance: [] };
    await refresh();
    assert.equal(await page.locator('.home-seat').count(), 8);
  });
  await test('empty-stations-chat-and-tasks', async () => {
    registry = { seats: [], governance: [] };
    snapshot.messages = [];
    snapshot.tasks = [];
    await open();
    assert.ok((await page.locator('#main').innerText()).includes('等待工位'));
    await chat();
    assert.ok((await page.locator('#dialog-body').innerText()).includes('还没有'));
    await page.keyboard.press('Escape');
    await page.locator('#open-tasks').click();
    assert.ok((await page.locator('#dialog-body').innerText()).includes('暂无'));
  });
  await test('removed-station-detail-stays-readable', async () => {
    await open();
    await page.locator('[data-seat=seat-0]').click();
    registry.seats[0].membership_state = 'removed';
    await refresh();
    assert.equal(await page.locator('.home-seat').count(), 7);
    assert.ok((await page.locator('#dialog-body').innerText()).includes('已不在'));
    await page.keyboard.press('Escape');
    assert.equal(await page.evaluate(() => document.activeElement.id), 'main');
  });
  await test('responsive-five-widths-stations-dialogs-and-orbs-fit', async () => {
    for (const size of [
      { width: 1920, height: 1080 },
      { width: 1440, height: 940 },
      { width: 820, height: 680 },
      { width: 390, height: 844 },
      { width: 320, height: 640 },
    ]) {
      await open(size);
      const geometry = await page.locator('.home-seat').evaluateAll((es) =>
        es.map((e) => {
          const r = e.getBoundingClientRect();
          return { x: r.left, y: r.top, right: r.right, bottom: r.bottom };
        }),
      );
      const rail = await page.locator('.home-sidebar').boundingBox(),
        orbs = await page.locator('.home-sidebar .home-orb').evaluateAll((es) =>
          es.map((e) => {
            const r = e.getBoundingClientRect();
            return { x: r.x, y: r.y, width: r.width, height: r.height };
          }),
        );
      assert.equal(orbs.length, 3);
      assert.ok(rail.x === 0 && rail.y === 0 && rail.height === size.height);
      assert.ok(orbs[0].x === orbs[1].x && orbs[1].y >= orbs[0].y + orbs[0].height);
      assert.ok(
        orbs.every(
          (r) =>
            r.x >= 0 && r.x + r.width <= rail.width && r.y >= 0 && r.y + r.height <= size.height,
        ),
      );
      assert.ok(geometry.every((r) => r.x >= rail.width && r.right <= size.width));
      for (let i = 0; i < geometry.length; i++)
        for (let j = i + 1; j < geometry.length; j++) {
          const a = geometry[i],
            b = geometry[j];
          assert.ok(!(a.x < b.right && a.right > b.x && a.y < b.bottom && a.bottom > b.y));
        }
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await page.screenshot({ path: path.join(out, 'home-' + size.width + '.png') });
      await chat();
      const r = await page.locator('#office-dialog').boundingBox();
      assert.ok(
        r.x >= 0 && r.y >= 0 && r.x + r.width <= size.width && r.y + r.height <= size.height,
      );
      await page.screenshot({ path: path.join(out, 'chat-' + size.width + '.png') });
      await context.close();
    }
  });
  await test('adaptive-one-and-many-stations-long-names', async () => {
    registry.seats = registry.seats.slice(0, 1);
    await open();
    const one = await page.locator('.home-seat').evaluate((e) => e.offsetWidth);
    await context.close();
    reset();
    for (let i = 8; i < 50; i++)
      registry.seats.push({
        ...registry.seats[0],
        id: 'seat-' + i,
        actor_id: 'agent-' + i,
        name: '长名称工位测试 用于自动排列和文字完整展示 ' + i,
      });
    await open();
    assert.equal(await page.locator('.home-seat').count(), 50);
    assert.ok(
      (await page
        .locator('.home-seat')
        .first()
        .evaluate((e) => e.offsetWidth)) < one,
    );
    assert.ok(await page.locator('#main').evaluate((e) => e.scrollHeight > e.clientHeight));
    await page.locator('.home-seat').last().focus();
    await page.keyboard.press('Enter');
    assert.equal(await page.locator('dialog[open]').count(), 1);
  });
  await test('touch-tap-and-native-chat-scroll', async () => {
    manyHistory();
    await open({ width: 390, height: 844 }, true);
    await page.locator('.home-seat').first().tap();
    assert.equal(await page.locator('dialog[open]').count(), 1);
    await page.locator('#dialog-close').tap();
    await page.locator('#open-chat').tap();
    await page.waitForFunction(() => document.querySelectorAll('[data-message]').length === 100);
    const b = page.locator('#dialog-body'),
      before = await b.evaluate((e) => e.scrollTop),
      cdp = await context.newCDPSession(page);
    await cdp.send('Input.dispatchTouchEvent', {
      type: 'touchStart',
      touchPoints: [{ x: 170, y: 250 }],
    });
    await cdp.send('Input.dispatchTouchEvent', {
      type: 'touchMove',
      touchPoints: [{ x: 170, y: 560 }],
    });
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
    await page.waitForTimeout(120);
    assert.ok((await b.evaluate((e) => e.scrollTop)) < before);
  });
  await test('html-injection-is-text-only', async () => {
    registry.seats[0].name = '<img src=x onerror="window.__xss=1">';
    snapshot.messages.at(-1).body = '<img src=x onerror="window.__xss=1">';
    await open();
    await chat();
    assert.equal(await page.evaluate(() => window.__xss), undefined);
    assert.equal(await page.locator('#app img').count(), 0);
    assert.ok((await page.locator('#dialog-body').innerText()).includes('<img'));
  });
  await test('account-sidebar-login-poll-and-logout', async () => {
    await open();
    const navigations = [];
    await context.route('https://api.aieyra.cn/**', (route) => {
      navigations.push(route.request().url());
      return route.fulfill({ contentType: 'text/html', body: '<h1>Fixture sign-in</h1>' });
    });
    await page.locator('#open-account').click();
    const popupReady = page.waitForEvent('popup');
    await page.locator('[data-account=login]').click();
    const popup = await popupReady;
    await popup.locator('h1').waitFor();
    assert.equal(navigations.length, 1);
    assert.equal(await popup.evaluate(() => window.opener), null);
    await page.locator('a.account-primary').waitFor();
    assert.ok(
      (await page.locator('a.account-primary').getAttribute('href')).startsWith(
        'https://api.aieyra.cn/',
      ),
    );
    await page.locator('[data-account=logout]').waitFor();
    assert.equal(await page.locator('#open-account').getAttribute('aria-label'), '账号');
    await page.locator('[data-account=logout]').click();
    await page.locator('[data-account=login]').waitFor();
    assert.equal(await page.locator('#open-account').getAttribute('aria-label'), '登录');
    assert.deepEqual(accountCalls, ['/api/cloud/login', '/api/cloud/poll', '/api/cloud/logout']);
    await page.screenshot({ path: path.join(out, 'account-dialog.png') });
  });
  await test('all-requests-read-only-no-script-errors', async () => {
    assert.deepEqual(errors, []);
    assert.deepEqual(writes, []);
  });
} finally {
  await browser.close();
  await new Promise((r) => server.close(r));
  await writeFile(
    path.join(out, 'results.json'),
    JSON.stringify(
      {
        results,
        errors,
        passed: results.filter((r) => r.state === 'PASS').length,
        failed: results.filter((r) => r.state === 'FAIL').length,
      },
      null,
      2,
    ),
  );
}
if (results.some((r) => r.state === 'FAIL')) process.exitCode = 1;
