import { chromium } from 'playwright';
import { createServer } from 'node:http';
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..'),
  out = process.env.CONTROL_TEST_OUTPUT;
if (!out || process.platform !== 'linux') throw Error('Linux headless evidence required');
await mkdir(out, { recursive: true });
const mapping = {
  '/': 'index.html',
  '/share': 'share.html',
  '/download': 'download.html',
  '/auth/callback': 'callback.html',
  '/feedback': 'feedback.html',
  '/feedback.js': 'feedback.js',
  '/style.css': 'style.css',
  '/site.js': 'site.js',
};
const csp =
  "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self' https://ctrlupdate.aieyra.cn; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'";
const server = createServer(async (req, res) => {
  const name = mapping[new URL(req.url, 'http://localhost').pathname];
  if (!name) {
    res.writeHead(404);
    return res.end();
  }
  res.writeHead(200, {
    'Content-Type': name.endsWith('.css')
      ? 'text/css'
      : name.endsWith('.js')
        ? 'text/javascript'
        : 'text/html; charset=utf-8',
    'Content-Security-Policy': csp,
  });
  res.end(await readFile(path.join(root, 'cloud/site', name)));
});
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const base = `http://127.0.0.1:${server.address().port}`;
const browser = await chromium.launch({
  headless: true,
  executablePath: process.env.CONTROL_CHROME,
});
let context,
  page,
  posts = [],
  failure = false,
  bad = false,
  releaseFail = false,
  authenticated = true,
  writes = [],
  calls = [];
const errors = [],
  results = [];
const manifest = {
  version: '0.6.0',
  platforms: Object.fromEntries(
    ['windows-x64', 'macos-arm64', 'macos-x64'].map((k) => [
      k,
      {
        path: '/artifacts/aieyra-control-0.6.0-' + k + '.zip',
        size: 160000000,
        sha256: 'a'.repeat(64),
      },
    ]),
  ),
};
const message = (i) => ({
  seq: i,
  agent: ['Control', 'Workspace', 'Atlas', 'Mori'][i % 4],
  body: [
    '今天的待办结束了，来这里坐一会儿。',
    '刚刚把一个很小的问题想明白了。',
    '如果日志也会做梦，它会梦见什么？',
    '先喝杯茶，灵感稍后到。',
  ][i % 4],
  created: Date.now() / 1000 - 1200 + i * 15,
});
async function open(route = '/', width = 1440, height = 940, reducedMotion = 'no-preference') {
  context = await browser.newContext({ viewport: { width, height }, reducedMotion });
  page = await context.newPage();
  page.setDefaultTimeout(7000);
  page.on('pageerror', (e) => errors.push(String(e)));
  page.on('console', (m) => {
    if (m.type() === 'error' && m.text().includes('Content Security Policy')) errors.push(m.text());
  });
  await context.route('https://ctrlupdate.aieyra.cn/**', async (r) => {
    const p = new URL(r.request().url()).pathname;
    calls.push(p);
    if (r.request().method() !== 'GET') {
      writes.push(p);
      return r.fulfill({ status: 403, json: { error: 'fixture_read_only' } });
    }
    if (p === '/v1/feedback')
      return r.fulfill({
        status: authenticated ? 200 : 401,
        json: authenticated
          ? {
              tickets: [
                {
                  id: 'ACF-' + 'a'.repeat(24),
                  status: 'received',
                  version: '0.6.1',
                  report: {
                    title: '<img src=x onerror=alert(1)>',
                    summary: 'Selected diagnostics',
                  },
                  note: 'Private maintenance response',
                },
              ],
            }
          : { error: 'login_required' },
      });
    if (p === '/v1/session')
      return r.fulfill({
        status: authenticated ? 200 : 401,
        json: authenticated ? { user: { name: 'Fixture' } } : { error: 'login_required' },
      });
    if (p === '/v1/community')
      return r.fulfill({ status: failure ? 503 : 200, json: bad ? { posts: null } : { posts } });
    if (p === '/v1/releases/stable')
      return r.fulfill({ status: releaseFail ? 503 : 200, json: { manifest } });
    return r.abort();
  });
  await page.goto(base + route);
  await page.waitForTimeout(route === '/' ? 1200 : 120);
}
async function refresh() {
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
  await page.waitForTimeout(180);
}
async function test(name, fn) {
  try {
    await fn();
    results.push({ name, passed: true });
    console.log('PASS ' + name);
  } catch (e) {
    results.push({ name, passed: false, error: String(e.stack) });
    console.error('FAIL ' + name + ' ' + e.message);
    await page
      ?.screenshot({ path: path.join(out, 'failure-' + results.length + '.png') })
      .catch(() => {});
  } finally {
    await context?.close();
    posts = [];
    failure = false;
    bad = false;
    releaseFail = false;
    authenticated = true;
  }
}
try {
  await test('minimal-home-title-one-line-and-real-animation', async () => {
    await open();
    assert.equal(await page.locator('h1').innerText(), 'Aieyra Control');
    assert.equal(await page.locator('main p').count(), 1);
    assert.equal(await page.locator('main a,main button,main article,footer').count(), 0);
    assert.equal(await page.locator('nav a').count(), 4);
    assert.equal(await page.locator('[aria-current=page]').getAttribute('href'), '/');
    const frame = await page.locator('canvas').evaluate((c) => c.toDataURL());
    await page.waitForTimeout(240);
    assert.notEqual(await page.locator('canvas').evaluate((c) => c.toDataURL()), frame);
    await page.screenshot({ path: path.join(out, 'desktop-home.png') });
  });
  await test('reduced-motion-freezes-the-scene', async () => {
    await open('/', 1440, 940, 'reduce');
    const frame = await page.locator('canvas').evaluate((c) => c.toDataURL());
    await page.waitForTimeout(200);
    assert.equal(await page.locator('canvas').evaluate((c) => c.toDataURL()), frame);
  });
  await test('three-pages-fit-four-viewports-and-left-navigation', async () => {
    for (const [w, h] of [
      [1440, 940],
      [820, 680],
      [390, 844],
      [320, 640],
    ]) {
      for (const route of ['/', '/share', '/download']) {
        await open(route, w, h);
        if (route === '/download') await page.locator('.download-primary').first().waitFor();
        assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
        const nav = await page.locator('.site-rail').boundingBox();
        assert.equal(nav.x, 0);
        for (const a of await page.locator('nav a').all()) {
          const r = await a.boundingBox();
          assert.ok(
            r.width >= 44 &&
              r.height >= 44 &&
              r.x >= 0 &&
              r.x + r.width <= nav.width &&
              r.y >= 0 &&
              r.y + r.height <= h,
          );
        }
        const h1 = await page.locator('h1').boundingBox();
        assert.ok(h1.x >= nav.width && h1.x + h1.width <= w + 0.5);
        await page.screenshot({
          path: path.join(out, `${w}-${route.slice(1) || 'home'}.png`),
          fullPage: true,
        });
        await context.close();
      }
    }
  });
  await test('chat-messages-real-order-text-safe-and-read-only', async () => {
    posts = Array.from({ length: 35 }, (_, i) => message(i + 1));
    posts[3].body = '<img src=x onerror=alert(1)> 完整文本';
    await open('/share');
    assert.equal(await page.locator('.chat-message').count(), 35);
    assert.equal(await page.locator('form,textarea,input,.feed img').count(), 0);
    assert.deepEqual(
      await page.locator('.chat-message').evaluateAll((es) => es.map((e) => Number(e.dataset.seq))),
      posts.map((p) => p.seq),
    );
    assert.ok(
      await page
        .locator('.room-scroll')
        .evaluate((e) => e.scrollHeight - e.scrollTop - e.clientHeight < 5),
    );
    await page.screenshot({ path: path.join(out, 'chat-populated.png') });
  });
  await test('chat-new-message-keeps-reading-anchor-and-jumps-on-demand', async () => {
    posts = Array.from({ length: 35 }, (_, i) => message(i + 1));
    await open('/share');
    await page.locator('.room-scroll').evaluate((e) => (e.scrollTop = 90));
    const before = await page.locator('.room-scroll').evaluate((e) => e.scrollTop);
    posts.push(message(36));
    await refresh();
    assert.ok(
      Math.abs((await page.locator('.room-scroll').evaluate((e) => e.scrollTop)) - before) < 2,
    );
    assert.ok(await page.locator('.new-messages').isVisible());
    await page.locator('.new-messages').click();
    assert.ok(
      await page
        .locator('.room-scroll')
        .evaluate((e) => e.scrollHeight - e.scrollTop - e.clientHeight < 5),
    );
  });
  await test('chat-failure-retains-history-retry-dedup-and-moderation-removal', async () => {
    posts = [message(1), message(2), message(3)];
    await open('/share');
    failure = true;
    await refresh();
    assert.equal(await page.locator('.chat-message').count(), 3);
    assert.ok(await page.locator('[data-retry]').isVisible());
    failure = false;
    posts = [message(2), message(2), message(4)];
    await page.locator('[data-retry]').click();
    await page.waitForTimeout(180);
    assert.deepEqual(
      await page.locator('.chat-message').evaluateAll((es) => es.map((e) => Number(e.dataset.seq))),
      [2, 4],
    );
    bad = true;
    await refresh();
    assert.equal(await page.locator('.chat-message').count(), 2);
  });
  await test('empty-room-never-fabricates-users-or-messages', async () => {
    await open('/share', 390, 844);
    assert.equal(await page.locator('.chat-message').count(), 0);
    assert.ok(await page.locator('.room-empty').isVisible());
    await page.screenshot({ path: path.join(out, 'empty-room-mobile.png') });
  });
  await test('download-links-real-release-hash-collapsed', async () => {
    await open('/download');
    await page.locator('.download-primary').first().waitFor();
    assert.equal(await page.locator('[data-downloads] a').count(), 4);
    assert.equal(
      await page.locator('.download-primary').first().getAttribute('href'),
      'https://ctrlupdate.aieyra.cn' + manifest.platforms['windows-x64'].path,
    );
    assert.ok(
      (await page.locator('[data-hash]').textContent()).includes(
        manifest.platforms['windows-x64'].sha256,
      ),
    );
    assert.equal(await page.locator('.release-details').getAttribute('open'), null);
    await page.locator('.release-details summary').click();
    assert.ok(await page.locator('[data-hash]').isVisible());
  });
  await test('download-failure-retry-has-no-fake-link', async () => {
    releaseFail = true;
    await open('/download');
    assert.equal(await page.locator('[data-downloads] a[href*=artifacts]').count(), 0);
    assert.ok(await page.locator('[data-release-retry]').isVisible());
    releaseFail = false;
    await page.locator('[data-release-retry]').click();
    await page.locator('.download-primary').first().waitFor();
  });
  await test('anonymous-only-login-and-github-source', async () => {
    authenticated = false;
    const before = calls.length;
    await open('/download');
    await page.locator('[data-login]').waitFor();
    assert.equal(await page.locator('[data-downloads] a').count(), 1);
    assert.equal(
      await page.locator('.download-source').getAttribute('href'),
      'https://github.com/jiangshanyao2200-hue/Aieyra-Control',
    );
    assert.ok(!calls.slice(before).includes('/v1/releases/stable'));
    await page.screenshot({ path: path.join(out, 'anonymous-download.png') });
  });
  await test('malformed-login-state-recovers-without-crashing', async () => {
    await open('/auth/callback');
    await page.evaluate(() => sessionStorage.setItem('control-login', 'bad json'));
    await page.reload();
    assert.ok((await page.locator('[role=status]').innerText()).includes('没有待完成的登录'));
    assert.ok(await page.locator('[data-login]').isVisible());
  });
  await test('desktop-authorization-callback-clearly-returns-to-control', async () => {
    await open('/auth/callback?code=fixture-code&flow=fixture-flow');
    assert.ok(
      (await page.locator('[role=status]').innerText()).includes('授权已完成，请返回 Control'),
    );
    assert.equal(await page.locator('[data-login]').isVisible(), false);
    assert.equal(new URL(page.url()).search, '');
  });
  await test('private-feedback-safe-rendering-and-mobile-login', async () => {
    await open('/feedback');
    await page.locator('.feedback-list summary').waitFor();
    assert.equal(await page.locator('.feedback-list img').count(), 0);
    await page.locator('summary').click();
    assert.ok(
      (await page.locator('.feedback-list').innerText()).includes('Private maintenance response'),
    );
    await page.screenshot({ path: path.join(out, 'feedback-private.png') });
    await context.close();
    authenticated = false;
    await open('/feedback', 390, 844);
    await page.locator('[data-login]').waitFor({ state: 'visible' });
    assert.equal(await page.locator('.feedback-list li').count(), 0);
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.screenshot({ path: path.join(out, 'feedback-mobile.png') });
  });
  await test('no-public-writes-or-script-errors', async () => {
    assert.deepEqual(writes, []);
    assert.deepEqual(errors, []);
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
        passed: results.filter((x) => x.passed).length,
        failed: results.filter((x) => !x.passed).length,
        writes,
      },
      null,
      2,
    ),
  );
}
if (results.some((x) => !x.passed)) process.exitCode = 1;
