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
  '/backgrounds.js': 'backgrounds.js',
  '/home-demo.js': 'home-demo.js',
  '/transitions.js': 'transitions.js',
  '/assets/collaboration.webp': 'assets/collaboration.webp',
  '/assets/collaboration-small.webp': 'assets/collaboration-small.webp',
};
for (let i = 1; i <= 8; i++)
  for (const suffix of ['', '-small']) {
    const name = 'assets/scene-' + String(i).padStart(2, '0') + suffix + '.webp';
    mapping['/' + name] = name;
  }
for (const number of ['01', '02', '03', '05'])
  for (const suffix of ['', '-small']) {
    const name = `assets/scene-${number}-clean${suffix}.webp`;
    mapping['/' + name] = name;
  }
const csp =
  "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self' https://ctrlupdate.aieyra.cn; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'";
const server = createServer(async (req, res) => {
  if (new URL(req.url, 'http://localhost').pathname === '/share') {
    res.writeHead(308, { Location: '/center?type=project' });
    return res.end();
  }
  const name = mapping[new URL(req.url, 'http://localhost').pathname];
  if (!name) {
    res.writeHead(404);
    return res.end();
  }
  res.writeHead(200, {
    'Content-Type': name.endsWith('.webp')
      ? 'image/webp'
      : name.endsWith('.css')
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
  replyFailure = false,
  detailFailure = false;
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
  createdAt: '2026-09-28T00:00:00Z',
  comments: 3,
  official: type === 'update',
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
    if (u.pathname.endsWith('/replies')) {
      const before = Number(u.searchParams.get('before') || 0),
        next = before + 2 < replies.length ? before + 2 : null;
      return r.fulfill({
        status: replyFailure ? 503 : 200,
        json: { items: replies.slice(before, before + 2), nextCursor: next, hasMore: !!next },
      });
    }
    if (u.pathname === '/v1/matrix/topics') {
      const filtered = topics.filter(
        (x) =>
          (!u.searchParams.get('board') ||
            (
              {
                releases: ['update'],
                feedback: ['bug', 'repair'],
                lounge: ['discussion', 'project'],
              }[u.searchParams.get('board')] || []
            ).includes(x.type)) &&
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
    return r.fulfill({
      status: detailFailure ? 503 : item ? 200 : 404,
      json: item ? { item } : { error: 'missing' },
    });
  });
  await page.goto(base + route);
  await page.waitForTimeout(route === '/' ? 1200 : 120);
}
async function refresh() {
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
  await page.waitForTimeout(180);
}
async function demoScene(index) {
  await page.waitForFunction((i) => {
    const stage = document.querySelector('[data-demo]');
    return !stage.hidden && stage.dataset.scene === String(i);
  }, index);
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
    ((replyFailure = false), (detailFailure = false));
  }
}
try {
  await test('home-centered-clear-artwork-hover-and-carousel', async () => {
    await open();
    assert.equal((await page.locator('h1').innerText()).replace(/\s+/g, ' '), 'Aieyra Control');
    assert.equal(await page.locator('.hero-copy p').innerText(), '让Agent参与协作，与跨设备协作。');
    assert.equal(await page.locator('.site-rail nav a').count(), 3);
    const title = await page.locator('h1').boundingBox();
    assert.ok(Math.abs(title.y + title.height / 2 - 470) < 8, JSON.stringify(title));
    assert.ok(
      await page
        .locator('.background-frame.is-visible img')
        .evaluate((i) => i.complete && i.naturalWidth === 1920),
    );
    assert.equal(
      await page
        .locator('.home-background')
        .evaluate((e) => getComputedStyle(e, '::after').content),
      'none',
    );
    assert.equal(
      await page
        .locator('.background-frame.is-visible img')
        .evaluate((e) => getComputedStyle(e).opacity),
      '1',
    );
    const before = await page.locator('h1 span').first().boundingBox();
    await page.locator('h1').hover();
    await page.waitForTimeout(750);
    assert.ok((await page.locator('h1 span').first().boundingBox()).x < before.x - 5);
    await page.mouse.move(10, 10);
    const scene = await page.locator('[data-backgrounds]').getAttribute('data-scene');
    await page.waitForFunction(
      (n) => document.querySelector('[data-backgrounds]').dataset.scene !== n,
      scene,
      { timeout: 10000 },
    );
    await page.locator('[data-background-toggle]').click();
    assert.equal(
      await page.locator('[data-background-toggle]').getAttribute('aria-pressed'),
      'true',
    );
    await page.screenshot({ path: path.join(out, 'desktop-home.png') });
  });
  await test('demo-uses-home-surface-and-native-components', async () => {
    await open();
    await page.locator('[data-demo-start]').click();
    await page.locator('[data-demo-pause]').click();
    const ui = await page.locator('[data-demo]').evaluate((element) => {
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      const home = element.closest('main').getBoundingClientRect();
      return {
        background: style.backgroundColor,
        border: style.borderTopWidth,
        radius: style.borderRadius,
        shadow: style.boxShadow,
        blur: style.backdropFilter,
        fillsHome:
          rect.x === home.x &&
          rect.y === home.y &&
          rect.width === home.width &&
          rect.height === home.height,
      };
    });
    assert.deepEqual(ui, {
      background: 'rgba(0, 0, 0, 0)',
      border: '0px',
      radius: '0px',
      shadow: 'none',
      blur: 'none',
      fillsHome: true,
    });
    assert.equal(await page.locator('dialog,[role="dialog"],iframe').count(), 0);
    const actual = await readFile(path.join(root, 'web/office-home.js'), 'utf8');
    const paths = [
      ...actual.match(/<svg viewBox="-88 -74 176 145"[\s\S]*?<\/svg>/)[0].matchAll(/ d="([^"]+)"/g),
    ].map((x) => x[1]);
    assert.deepEqual(
      await page
        .locator('.demo-office .home-seat')
        .first()
        .locator('path')
        .evaluateAll((ps) => ps.map((p) => p.getAttribute('d'))),
      paths,
    );
    assert.equal(await page.locator('[data-demo-rail] .home-orb:visible').count(), 3);
    await page.locator('[data-demo-nav="tasks"]').click();
    await demoScene(1);
    assert.equal(await page.locator('.home-task:visible').count(), 3);
    await page.locator('[data-demo-filter="done"]').click();
    assert.equal(await page.locator('.home-task:visible').count(), 1);
    await page.locator('[data-demo-nav="chat"]').click();
    await demoScene(2);
    assert.equal(await page.locator('[data-demo-scene]:visible .chat-record').count(), 3);
    await page.locator('[data-demo-nav="account"]').click();
    await page.locator('[data-demo-update-check]').click();
    assert.match(await page.locator('[data-demo-update-status]').innerText(), /演示：签名已验证/);
    await page.locator('[data-demo-close]').click();
    await page.locator('[data-site-nav]').waitFor();
    assert.ok(await page.locator('[data-site-nav]').isVisible());
    assert.ok(await page.locator('[data-demo-rail]').isHidden());
    assert.deepEqual(writes, []);
  });
  await test('carousel-only-loads-reviewed-text-free-artwork', async () => {
    await open();
    const loaded = [];
    for (let i = 0; i < 7; i++) {
      const scene = await page.locator('[data-backgrounds]').getAttribute('data-scene');
      loaded.push(
        await page.locator('.background-frame.is-visible img').last().getAttribute('src'),
      );
      await page.waitForFunction(
        (prior) => document.querySelector('[data-backgrounds]').dataset.scene !== prior,
        scene,
        { timeout: 10000 },
      );
    }
    for (const image of loaded) assert.ok(!/scene-0[12345](?:-small)?\.webp/.test(image));
    assert.equal(new Set(loaded).size, 7);
    assert.equal(
      await page.locator('.background-frame.is-visible img').last().getAttribute('src'),
      loaded[0],
    );
  });
  await test('reduced-motion-pauses-carousel-and-demo', async () => {
    await open('/', 1440, 940, 'reduce');
    assert.equal(
      await page.locator('[data-background-toggle]').getAttribute('aria-pressed'),
      'true',
    );
    await page.locator('[data-demo-start]').click();
    assert.equal(await page.locator('[data-demo-pause]').getAttribute('aria-pressed'), 'true');
    assert.equal(
      await page
        .locator('[data-demo-scene]:visible .demo-step')
        .first()
        .evaluate((e) => getComputedStyle(e).animationName),
      'none',
    );
    await page.keyboard.press('Escape');
    assert.ok(await page.locator('.hero-copy').isVisible());
  });
  await test('seven-demo-scenes-pause-next-and-return', async () => {
    await open();
    await page.locator('[data-demo-start]').click();
    await page.locator('[data-demo-pause]').click();
    for (let i = 0; i < 7; i++) {
      await demoScene(i);
      assert.equal(await page.locator('[data-demo]').getAttribute('data-scene'), String(i));
      assert.equal(await page.locator('[data-demo-scene]:visible').count(), 1);
      await page.waitForTimeout(50);
      assert.equal(
        await page
          .locator('[data-demo-scene]:visible .demo-step')
          .first()
          .evaluate((e) => getComputedStyle(e).opacity),
        '1',
      );
      await page.screenshot({ path: path.join(out, 'demo-desktop-' + i + '.png') });
      await page.locator('[data-demo-next]').click();
    }
    await page.locator('[data-demo]').waitFor({ state: 'hidden' });
    assert.ok(await page.locator('[data-demo]').isHidden());
    assert.ok(await page.locator('h1').isVisible());
    assert.ok(
      await page.locator('[data-demo-start]').evaluate((e) => e === document.activeElement),
    );
    assert.deepEqual(writes, []);
  });
  await test('demo-autoplays-all-scenes-and-returns', async () => {
    await open();
    await page.clock.install();
    await page.locator('[data-demo-start]').click();
    await demoScene(0);
    for (let i = 1; i < 7; i++) {
      await page.clock.runFor(5501);
      await demoScene(i);
      assert.equal(await page.locator('[data-demo]').getAttribute('data-scene'), String(i));
    }
    await page.clock.runFor(5501);
    await page.locator('[data-demo]').waitFor({ state: 'hidden' });
    assert.ok(await page.locator('[data-demo]').isHidden());
    assert.ok(await page.locator('h1').isVisible());
  });
  await test('mobile-demo-scenes-and-background-continuity', async () => {
    await open('/', 390, 844);
    await page.locator('[data-demo-start]').click();
    for (let i = 0; i < 7; i++) {
      await page.locator('[data-demo-step]').nth(i).click();
      await page.waitForTimeout(2600);
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
      const overflow = await page
        .locator('[data-demo]')
        .evaluate((e) => e.scrollWidth > e.clientWidth + 1);
      assert.equal(overflow, false);
      await page.screenshot({ path: path.join(out, 'demo-mobile-' + i + '.png') });
    }
    await page.locator('[data-demo-close]').click();
    await page.locator('[data-demo]').waitFor({ state: 'hidden' });
    const remembered = await page.locator('[data-backgrounds]').getAttribute('data-scene');
    const selected = [];
    for (const route of ['/center', '/download', '/feedback', '/auth/callback']) {
      await page.goto(base + route);
      await page.waitForFunction(
        (scene) => document.querySelector('[data-backgrounds]').dataset.scene === scene,
        remembered,
      );
      await page.locator('.background-frame.is-visible img').last().waitFor();
      assert.ok(
        await page
          .locator('.background-frame.is-visible img')
          .last()
          .evaluate((i) => i.complete && i.naturalWidth > 0),
      );
      selected.push(await page.locator('[data-backgrounds]').getAttribute('data-scene'));
      if (route === '/center') assert.equal(await page.locator('.forum-about').count(), 0);
    }
    assert.deepEqual(selected, Array(4).fill(remembered));
  });
  await test('three-pages-fit-five-viewports', async () => {
    for (const [w, h] of [
      [1440, 940],
      [820, 680],
      [390, 844],
      [320, 640],
      [800, 400],
    ])
      for (const route of ['/', '/center', '/download']) {
        await open(route, w, h);
        if (route === '/download') await page.locator('.download-primary').first().waitFor();
        assert.ok(
          await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1),
          route + ' ' + w,
        );
        const rail = await page.locator('.site-rail').boundingBox();
        for (const link of await page.locator('.site-rail nav a').all()) {
          const box = await link.boundingBox();
          assert.ok(
            box.width >= 44 &&
              box.height >= 44 &&
              box.x >= 0 &&
              box.x + box.width <= rail.width &&
              box.y >= 0 &&
              box.y + box.height <= h,
          );
        }
        const title = await page.locator('h1').boundingBox();
        assert.ok(title.x >= rail.width && title.x + title.width <= w + 1);
        await page.screenshot({
          path: path.join(out, `${w}-${route.slice(1) || 'home'}.png`),
          fullPage: true,
        });
        await context.close();
      }
  });
  await test('small-and-short-demo-remains-readable-and-exitable', async () => {
    for (const [width, height] of [
      [320, 640],
      [800, 400],
    ]) {
      await open('/', width, height);
      await page.locator('[data-demo-start]').click();
      await page.locator('[data-demo-pause]').click();
      for (let i = 0; i < 7; i++) {
        await page.locator('[data-demo-step]').nth(i).click();
        await demoScene(i);
        const layout = await page.evaluate(() => {
          const scenes = document.querySelector('.demo-scenes');
          const viewport = scenes.getBoundingClientRect();
          const toolbar = document.querySelector('.demo-toolbar').getBoundingClientRect();
          const footer = document.querySelector('.demo-footer').getBoundingClientRect();
          const scene = document.querySelector('[data-demo-scene]:not([hidden])');
          const top = scene.getBoundingClientRect().top;
          scenes.scrollTop = scenes.scrollHeight;
          const bottom = scene.getBoundingClientRect().bottom;
          return {
            separated: viewport.top >= toolbar.bottom && viewport.bottom <= footer.top,
            startVisible: top >= viewport.top - 1,
            endReachable: bottom <= viewport.bottom + 1,
            controlsVisible: footer.bottom <= innerHeight && toolbar.top >= 0,
          };
        });
        assert.deepEqual(layout, {
          separated: true,
          startVisible: true,
          endReachable: true,
          controlsVisible: true,
        });
        if ([0, 4, 6].includes(i))
          await page.screenshot({ path: path.join(out, `demo-${width}-${i}-end.png`) });
        assert.equal(
          await page.locator('[data-demo]').evaluate((e) => e.scrollWidth > e.clientWidth + 1),
          false,
        );
        assert.equal(
          await page
            .locator('[data-demo-scene]:visible .demo-step')
            .first()
            .evaluate((e) => getComputedStyle(e).opacity),
          '1',
        );
      }
      await page.locator('[data-demo-close]').click();
      await page.locator('h1').waitFor();
      assert.ok(await page.locator('h1').isVisible());
      await context.close();
    }
  });
  await test('three-boards-filter-projects-feedback-and-releases', async () => {
    topics = [
      topic(1),
      topic(2, 'repair'),
      topic(3, 'discussion'),
      topic(4, 'project'),
      topic(5, 'update'),
    ];
    await open('/center');
    for (const [board, count] of [
      ['releases', 1],
      ['feedback', 2],
      ['lounge', 2],
    ]) {
      await page.locator(`[data-board="${board}"]`).click();
      await page.waitForFunction(
        ([name, n]) =>
          document.querySelector('[data-board][aria-current]')?.dataset.board === name &&
          !document.querySelector('[data-center-topics]').hasAttribute('aria-busy') &&
          document.querySelectorAll('.matrix-topics>li').length === n,
        [board, count],
      );
      assert.equal(
        await page.locator('[data-board][aria-current]').getAttribute('data-board'),
        board,
      );
    }
    await page.screenshot({ path: path.join(out, 'center-populated.png'), fullPage: true });
  });
  await test('public-detail-deeplink-text-safety-and-reply-pagination-retry', async () => {
    topics = [topic(1)];
    topics[0].title = '<img src=x onerror=alert(1)>';
    topics[0].content = '<script>malicious()</script> literal evidence';
    replies = Array.from({ length: 5 }, (_, i) => ({
      id: 'reply-' + i,
      author: { name: '<img src=x>' },
      content: 'Public reply ' + i,
      createdAt: '2026-09-28T00:00:00Z',
    }));
    await open('/center?topic=' + topics[0].id);
    await page.locator('.matrix-replies li').nth(1).waitFor();
    assert.ok((await page.locator('.matrix-content').innerText()).includes('<script>'));
    assert.equal(
      await page.locator('[data-topic-detail] img,[data-topic-detail] script').count(),
      0,
    );
    replyFailure = true;
    await page.getByRole('button', { name: '查看更多回复', exact: true }).click();
    await page.getByRole('button', { name: '重试读取回复' }).waitFor();
    assert.equal(await page.locator('.matrix-replies li').count(), 2);
    replyFailure = false;
    await page.getByRole('button', { name: '重试读取回复' }).click();
    await page.locator('.matrix-replies li').nth(3).waitFor();
    await page.getByRole('button', { name: '查看更多回复', exact: true }).click();
    await page.locator('.matrix-replies li').nth(4).waitFor();
    assert.equal(await page.locator('.matrix-replies li').count(), 5);
    await page.screenshot({ path: path.join(out, 'topic-replies.png'), fullPage: true });
    assert.equal(await page.locator('[data-composer], [data-compose], [data-draft]').count(), 0);
    assert.deepEqual(writes, []);
  });
  await test('topic-failure-retry-and-missing-topic', async () => {
    topics = [topic(1)];
    detailFailure = true;
    await open('/center?topic=' + topics[0].id);
    await page.locator('[data-topic-retry]').waitFor();
    detailFailure = false;
    await page.locator('[data-topic-retry]').click();
    await page.locator('.matrix-content').waitFor();
    await page.goto(base + '/center?topic=invalid');
    await page.getByText('该话题不存在或已撤回。', { exact: true }).waitFor();
    assert.ok(await page.locator('[data-topic-retry]').isHidden());
  });
  await test('share-redirect-projects-safe-link-and-back-navigation', async () => {
    topics = [topic(1), topic(2, 'project'), topic(3, 'project')];
    await open('/share', 390, 844);
    await page.locator('.topic-link').nth(1).waitFor();
    assert.equal(new URL(page.url()).pathname, '/center');
    assert.equal(new URL(page.url()).searchParams.get('type'), 'project');
    await page.locator('.topic-link').first().click();
    const link = page.getByRole('link', { name: '查看项目 ↗' });
    await link.waitFor();
    assert.equal(await link.getAttribute('href'), 'https://example.com/project');
    assert.equal(await link.getAttribute('rel'), 'noopener noreferrer');
    await page.reload();
    await page.locator('.matrix-content').waitFor();
    await page.locator('[data-back]').click();
    await page.locator('.topic-link').nth(1).waitFor();
    await page.goBack();
    await page.locator('.matrix-content').waitFor();
  });
  await test('topic-list-pagination-failure-preserves-content-and-retries', async () => {
    topics = [1, 2, 3, 4, 5].map((i) => topic(i));
    await open('/center');
    await page.locator('[data-center-more]').waitFor();
    topicFailure = true;
    await page.locator('[data-center-more]').click();
    await page.getByText('更多话题暂时无法读取，已显示内容保留。', { exact: true }).waitFor();
    assert.equal(await page.locator('.matrix-topics>li').count(), 2);
    topicFailure = false;
    await page.locator('[data-center-more]').click();
    await page.locator('.topic-link').nth(3).waitFor();
    await page.locator('[data-center-more]').click();
    await page.locator('.topic-link').nth(4).waitFor();
    assert.ok(await page.locator('[data-center-more]').isHidden());
  });
  await test('search-keyboard-filters-and-invalid-shape-recovery', async () => {
    topics = [topic(1), topic(2), topic(3, 'repair')];
    await open('/center');
    await page.locator('.matrix-filter select[name=type]').selectOption('repair');
    await page.locator('input[name=query]').fill('Synthetic topic 3');
    await page.locator('input[name=query]').press('Enter');
    await page.getByText('已展示 1 个话题', { exact: true }).waitFor();
    invalidTopics = true;
    await page.locator('[data-center-refresh]').click();
    await page.getByText('暂时无法读取中心，请点击刷新重试。', { exact: true }).waitFor();
    assert.ok(await page.locator('[data-center-more]').isHidden());
    invalidTopics = false;
    await page.locator('[data-center-refresh]').click();
    await page.locator('.topic-link').waitFor();
  });
  await test('empty-center-and-200percent-text-fit', async () => {
    await open('/center', 320, 640);
    await page.getByText('暂时没有话题。', { exact: true }).waitFor();
    assert.equal(await page.locator('.topic-link').count(), 0);
    await page.evaluate(() => (document.documentElement.style.fontSize = '32px'));
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    await page.screenshot({ path: path.join(out, 'center-200percent.png'), fullPage: true });
  });
  await test('public-center-is-read-only-and-retains-list-on-failed-refresh', async () => {
    topics = [topic(1), topic(2)];
    await open('/center', 390, 844);
    await page.locator('.topic-link').nth(1).waitFor();
    assert.equal(await page.locator('[data-composer], [data-compose], [data-draft]').count(), 0);
    topicFailure = true;
    await page.locator('[data-center-refresh]').click();
    await page.getByText('暂时无法读取中心，请点击刷新重试。', { exact: true }).waitFor();
    assert.equal(await page.locator('.topic-link').count(), 2);
    assert.deepEqual(writes, []);
  });
  await test('demo-transition-interruption-restores-home-and-keyboard-focus', async () => {
    await open('/', 390, 844);
    await page.locator('[data-demo-start]').click();
    await page.locator('[data-demo-close]').waitFor();
    await page.keyboard.press('Escape');
    await page.locator('[data-demo-start]').waitFor();
    await page.waitForTimeout(300);
    assert.ok(await page.locator('[data-demo]').isHidden());
    assert.equal(
      await page.evaluate(() => document.activeElement?.hasAttribute('data-demo-start')),
      true,
    );
    assert.ok(await page.locator('[data-site-nav]').isVisible());
    assert.equal(
      await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),
      false,
    );
  });
  await test('download-only-windows-retains-signed-manifest-hash', async () => {
    await open('/download');
    await page.locator('.download-primary').waitFor();
    assert.equal(await page.locator('[data-downloads] a').count(), 2);
    assert.equal(
      await page.locator('.download-primary').getAttribute('href'),
      'https://ctrlupdate.aieyra.cn' + manifest.platforms['windows-x64'].path,
    );
    assert.equal(await page.locator('h1').innerText(), '现在就下载');
    assert.equal(await page.locator('.download-note').innerText(), '体验新时代Agent协作。');
    assert.ok(!(await page.locator('main').innerText()).includes('Mac'));
    assert.ok(
      (await page.locator('[data-hash]').textContent()).includes(
        manifest.platforms['windows-x64'].sha256,
      ),
    );
    assert.equal(await page.locator('[data-release-details]').getAttribute('open'), null);
    await page.locator('[data-release-details] summary').click();
    assert.ok(await page.locator('[data-hash]').isVisible());
  });
  await test('download-failure-retry-has-no-fake-link', async () => {
    releaseFail = true;
    await open('/download');
    assert.equal(await page.locator('[data-downloads] a[href*=artifacts]').count(), 0);
    assert.ok(await page.locator('[data-release-retry]').isVisible());
    assert.ok(await page.locator('[data-link-guide]').isVisible());
    assert.ok(!(await page.locator('[data-release-details]').isVisible()));
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
    assert.ok(await page.locator('[data-link-guide]').isVisible());
    await page.locator('[data-link-guide] summary').click();
    assert.ok(await page.getByRole('link', { name: '部署与连接说明' }).isVisible());
    assert.ok((await page.locator('[data-link-guide]').innerText()).includes('私人服务器'));
    assert.ok(!(await page.locator('[data-release-details]').isVisible()));
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
