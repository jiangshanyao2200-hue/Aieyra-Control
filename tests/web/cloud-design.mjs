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
  '/center': 'center.html',
  '/center.js': 'center.js',
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
  calls = [],
  topics = [],
  replies = [],
  topicFailure = false,
  invalidTopics = false,
  replyFailure = false;
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
const topic = (i, type = 'bug') => ({
  id: '00000000-0000-0000-0000-' + String(i).padStart(12, '0'),
  type,
  title: 'Synthetic topic ' + i,
  summary: 'An explicitly reviewed product summary.',
  content: 'Detailed synthetic evidence ' + i,
  state: 'open',
  author: { name: 'Fixture Matrix' },
  projectUrl: type === 'project' ? 'https://example.com/project' : '',
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
                  product: 'aieyra-control',
                  status: 'received',
                  version: '0.6.1',
                  report: {
                    title: '<img src=x onerror=alert(1)>',
                    summary: 'Selected diagnostics',
                  },
                  note: 'Private maintenance response',
                },
                {
                  id: 'ACF-' + 'b'.repeat(24),
                  product: 'aieyra-os',
                  status: 'triaged',
                  version: '0.2.20',
                  report: { title: 'Synthetic OS report', summary: 'Selected failure summary' },
                  note: '',
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
  await context.route(base + '/v1/matrix/**', async (r) => {
    const u = new URL(r.request().url());
    calls.push(u.pathname + u.search);
    if (r.request().method() !== 'GET') {
      writes.push(u.pathname);
      return r.fulfill({ status: 403, json: { error: 'browser_read_only' } });
    }
    if (u.pathname.endsWith('/replies'))
      return r.fulfill({
        status: replyFailure ? 503 : 200,
        json: { items: replies, nextCursor: null, hasMore: false },
      });
    if (u.pathname === '/v1/matrix/topics') {
      const filtered = topics.filter(
        (x) =>
          (!u.searchParams.get('type') || x.type === u.searchParams.get('type')) &&
          (!u.searchParams.get('query') || x.title.includes(u.searchParams.get('query'))),
      );
      const before = Number(u.searchParams.get('before') || 0);
      const next = before + 2 < filtered.length ? before + 2 : null;
      return r.fulfill({
        status: topicFailure ? 503 : 200,
        json: {
          items: invalidTopics ? null : filtered.slice(before, before + 2),
          nextCursor: next,
          hasMore: !!next,
        },
      });
    }
    const item = topics.find((x) => u.pathname.endsWith('/' + x.id));
    return r.fulfill({ status: item ? 200 : 404, json: item ? { item } : { error: 'missing' } });
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
    topics = [];
    replies = [];
    topicFailure = false;
    invalidTopics = false;
    replyFailure = false;
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
  await test('four-pages-fit-four-viewports-and-left-navigation', async () => {
    for (const [w, h] of [
      [1440, 940],
      [820, 680],
      [390, 844],
      [320, 640],
    ]) {
      for (const route of ['/', '/share', '/center', '/download']) {
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
  await test('center-public-text-safe-read-only-detail-and-replies', async () => {
    topics = [topic(1), topic(2)];
    topics[0].title = '<img src=x onerror=alert(1)>';
    topics[0].content = '<script>malicious()</script> literal evidence';
    replies = [{ author: { name: '<img src=x>' }, content: 'Public reply text' }];
    await open('/center');
    await page.locator('.matrix-topics > li').first().waitFor();
    assert.equal(await page.locator('.matrix-topics > li').count(), 2);
    await page.locator('.matrix-topics summary').first().click();
    await page.locator('.matrix-content').waitFor();
    assert.ok((await page.locator('.matrix-content').innerText()).includes('<script>'));
    await page.getByRole('button', { name: '读取回复', exact: true }).click();
    await page.getByText('Public reply text', { exact: true }).waitFor();
    assert.equal(
      await page
        .locator('.matrix-topics img,.matrix-topics script,textarea,[contenteditable=true]')
        .count(),
      0,
    );
    assert.equal(await page.locator('form').getAttribute('role'), 'search');
    await page.screenshot({ path: path.join(out, 'center-populated.png'), fullPage: true });
  });
  await test('share-lists-projects-with-safe-project-link', async () => {
    topics = [topic(1), topic(2, 'project'), topic(3, 'project')];
    await open('/share', 390, 844);
    await page.locator('.matrix-topics > li').first().waitFor();
    assert.equal(await page.locator('.matrix-topics > li').count(), 2);
    assert.equal(await page.locator('select').count(), 0);
    await page.locator('.matrix-topics summary').first().click();
    const link = page.getByRole('link', { name: '查看项目' });
    await link.waitFor();
    assert.equal(await link.getAttribute('href'), 'https://example.com/project');
    assert.equal(await link.getAttribute('rel'), 'noopener noreferrer');
    assert.ok(!(await page.locator('.matrix-topics').innerText()).includes('Synthetic topic 1'));
    await page.screenshot({ path: path.join(out, 'share-project-mobile.png'), fullPage: true });
  });
  await test('center-pagination-retains-loaded-content-on-failure-and-retries', async () => {
    topics = [topic(1), topic(2), topic(3), topic(4), topic(5)];
    await open('/center');
    await page.locator('[data-center-more]').waitFor({ state: 'visible' });
    await page.locator('.matrix-topics summary').first().click();
    await page.locator('.matrix-content').waitFor();
    topicFailure = true;
    await page.locator('[data-center-more]').click();
    await page.getByText('更多话题暂时无法读取，已显示内容保留。', { exact: true }).waitFor();
    assert.equal(await page.locator('.matrix-topics > li').count(), 2);
    assert.equal(await page.locator('details[open]').count(), 1);
    topicFailure = false;
    await page.locator('[data-center-more]').click();
    await page.waitForFunction(() => document.querySelectorAll('.matrix-topics > li').length === 4);
    await page.locator('[data-center-more]').click();
    await page.waitForFunction(() => document.querySelectorAll('.matrix-topics > li').length === 5);
    assert.ok(await page.locator('[data-center-more]').isHidden());
  });
  await test('center-filters-and-invalid-response-recover-without-stale-pagination', async () => {
    topics = [topic(1), topic(2), topic(3), topic(4, 'repair')];
    await open('/center');
    await page.locator('[data-center-more]').waitFor({ state: 'visible' });
    await page.locator('select[name=type]').selectOption('repair');
    await page.getByRole('button', { name: '查找', exact: true }).click();
    await page.waitForFunction(
      () => document.querySelector('[data-center-status]').textContent === '已展示 1 个话题',
    );
    assert.ok((await page.locator('.matrix-topics').innerText()).includes('Synthetic topic 4'));
    assert.ok(await page.locator('[data-center-more]').isHidden());
    invalidTopics = true;
    await page.getByRole('button', { name: '查找', exact: true }).click();
    await page.getByText('暂时无法读取，请使用查找重试。', { exact: true }).waitFor();
    assert.ok(await page.locator('[data-center-more]').isHidden());
    invalidTopics = false;
    await page.getByRole('button', { name: '查找', exact: true }).click();
    await page.locator('.matrix-topics > li').first().waitFor();
  });
  await test('empty-share-and-center-never-invent-content', async () => {
    for (const route of ['/share', '/center']) {
      await open(route, 320, 640);
      await page.waitForFunction(() =>
        document.querySelector('[data-center-status]').textContent.includes('暂时没有'),
      );
      assert.equal(await page.locator('.matrix-topics > li').count(), 0);
      await page.screenshot({
        path: path.join(out, 'empty-' + route.slice(1) + '-mobile.png'),
        fullPage: true,
      });
      await context.close();
    }
  });
  await test('center-and-share-text-zoom-and-keyboard-search-fit', async () => {
    for (const route of ['/share', '/center']) {
      topics = [topic(1, 'project'), topic(2)];
      await open(route, 390, 844);
      await page.evaluate(() => (document.documentElement.style.fontSize = '32px'));
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      const heading = await page.locator('h1').boundingBox();
      const rail = await page.locator('.site-rail').boundingBox();
      assert.ok(heading.x >= rail.width);
      await page.locator('input[name=query]').fill('Synthetic topic 1');
      await page.locator('input[name=query]').press('Enter');
      await page.waitForFunction(
        () => document.querySelectorAll('.matrix-topics > li').length === 1,
      );
      await page.screenshot({
        path: path.join(out, route.slice(1) + '-200percent.png'),
        fullPage: true,
      });
      await context.close();
    }
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
  await test('authorization-callback-clearly-returns-to-requesting-app', async () => {
    await open('/auth/callback?code=fixture-code&flow=fixture-flow');
    assert.ok(
      (await page.locator('[role=status]').innerText()).includes(
        '授权已完成，请返回发起授权的应用',
      ),
    );
    assert.equal(await page.locator('[data-login]').isVisible(), false);
    assert.equal(new URL(page.url()).search, '');
  });
  await test('private-feedback-safe-rendering-and-mobile-login', async () => {
    await open('/feedback');
    await page.locator('.feedback-list summary').first().waitFor();
    assert.equal(await page.locator('.feedback-list img').count(), 0);
    assert.equal(await page.locator('.feedback-list li').count(), 2);
    assert.ok(
      (await page.locator('.feedback-meta').allTextContents()).some(
        (t) => t.includes('Aieyra Control') && t.includes('0.6.1'),
      ),
    );
    assert.ok(
      (await page.locator('.feedback-meta').allTextContents()).some(
        (t) => t.includes('Aieyra OS') && t.includes('0.2.20'),
      ),
    );
    await page.locator('summary').first().click();
    assert.ok(
      (await page.locator('.feedback-list').innerText()).includes('Private maintenance response'),
    );
    await page.screenshot({ path: path.join(out, 'feedback-private.png') });
    await context.close();
    await open('/feedback', 390, 844);
    await page.locator('.feedback-list li').nth(1).waitFor();
    await page.locator('summary').nth(1).click();
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.screenshot({ path: path.join(out, 'feedback-products-mobile.png') });
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
