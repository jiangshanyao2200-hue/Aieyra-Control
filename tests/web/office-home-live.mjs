import { chromium } from 'playwright';
import { createServer } from 'node:http';
import { spawn } from 'node:child_process';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const out =
  process.env.CONTROL_TEST_OUTPUT ||
  path.join(
    os.tmpdir(),
    'aieyra-control-test-output',
    `office-home-live-${Date.now()}-${process.pid}`,
  );
await mkdir(out, { recursive: true });
const bridge = spawn(
  'python.exe',
  [
    '-u',
    '-c',
    `import sys,json,urllib.request
for line in sys.stdin:
 try:
  p=json.loads(line);r=urllib.request.urlopen('http://127.0.0.1:17910'+p['path'],timeout=12);v=json.loads(r.read());print(json.dumps({'id':p['id'],'status':r.status,'body':v}),flush=True)
 except Exception as e:print(json.dumps({'id':p['id'],'status':502,'body':{'error':type(e).__name__}}),flush=True)
`,
  ],
  { stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true, env: { ...process.env, PYTHONUTF8: '1' } },
);
const pending = new Map();
let seq = 0,
  buffer = '';
bridge.stdout.on('data', (chunk) => {
  buffer += chunk;
  while (buffer.includes('\n')) {
    const i = buffer.indexOf('\n'),
      line = buffer.slice(0, i);
    buffer = buffer.slice(i + 1);
    const r = JSON.parse(line);
    pending.get(r.id)?.(r);
    pending.delete(r.id);
  }
});
const get = (route) =>
  new Promise((resolve) => {
    const id = ++seq;
    pending.set(id, resolve);
    bridge.stdin.write(JSON.stringify({ id, path: route }) + '\n');
  });
const writes = [],
  external = [],
  errors = [],
  checks = [];
const server = createServer(async (req, res) => {
  try {
    if (req.method !== 'GET') {
      writes.push(req.url);
      res.writeHead(405);
      return res.end();
    }
    const url = new URL(req.url, 'http://localhost');
    if (url.pathname.startsWith('/api/')) {
      const v = await get(req.url);
      res.writeHead(v.status, { 'content-type': 'application/json' });
      return res.end(JSON.stringify(v.body));
    }
    const name = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
    if (!/^[\w.-]+$/.test(name)) {
      res.writeHead(404);
      return res.end();
    }
    const raw = await readFile(path.join(root, 'web', name));
    res.writeHead(200, {
      'content-type':
        {
          '.js': 'text/javascript',
          '.css': 'text/css',
          '.html': 'text/html',
          '.svg': 'image/svg+xml',
        }[path.extname(name)] || 'text/plain',
    });
    res.end(raw);
  } catch {
    res.writeHead(500);
    res.end();
  }
});
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const base = `http://127.0.0.1:${server.address().port}`;
const browser = await chromium.launch({
  headless: true,
  executablePath: process.env.CONTROL_CHROME,
});
try {
  for (const size of [
    { width: 1440, height: 940 },
    { width: 390, height: 844 },
  ]) {
    const context = await browser.newContext({ viewport: size });
    const page = await context.newPage();
    page.on('pageerror', (e) => errors.push(String(e)));
    await page.route('**/*', (route) => {
      if (!route.request().url().startsWith(base)) {
        external.push(route.request().url());
        return route.abort();
      }
      if (route.request().method() !== 'GET') {
        writes.push(route.request().url());
        return route.abort();
      }
      return route.continue();
    });
    await page.goto(base);
    await page.waitForFunction(
      () =>
        document.querySelectorAll('[data-seat]').length > 0 &&
        document.querySelector('#main').dataset.refreshing === 'false',
    );
    const seats = await page.locator('[data-seat]').count();
    assert.ok(seats >= 6);
    assert.equal(
      await page.locator('textarea,form,nav:not(.home-sidebar),input,select').count(),
      0,
    );
    assert.ok(!(await page.locator('#home-grid').innerText()).includes('离线'));
    assert.equal(await page.locator('.home-orb').count(), 3);
    await page.waitForTimeout(350);
    await page.screenshot({ path: path.join(out, 'live-' + size.width + '.png') });
    await page.locator('[data-seat]').first().click();
    assert.equal(await page.locator('dialog[open]').count(), 1);
    const detail = await page.locator('#dialog-body').innerText();
    assert.ok(
      detail.includes('通信租约') &&
        detail.includes('最后运行上报') &&
        detail.includes('最近通信观测'),
    );
    await page.screenshot({ path: path.join(out, 'live-station-' + size.width + '.png') });
    await page.keyboard.press('Escape');
    await page.locator('#open-chat').click();
    await page.waitForFunction(
      () => !document.querySelector('#chat-boundary').textContent.includes('正在读取'),
    );
    assert.ok(!(await page.locator('#chat-boundary').innerText()).includes('暂时无法'));
    const messages = await page.locator('[data-message]').count();
    await page.screenshot({ path: path.join(out, 'live-chat-' + size.width + '.png') });
    await page.keyboard.press('Escape');
    await page.locator('#open-tasks').click();
    const tasks = await page.locator('.home-task').count();
    await page.screenshot({ path: path.join(out, 'live-tasks-' + size.width + '.png') });
    await page.keyboard.press('Escape');
    checks.push({ width: size.width, seats, messages, tasks, read_only: true });
    await context.close();
  }
  assert.deepEqual(writes, []);
  assert.deepEqual(external, []);
  assert.deepEqual(errors, []);
  await writeFile(
    path.join(out, 'results.json'),
    JSON.stringify({ passed: true, checks, writes, external, errors }, null, 2),
  );
  console.log(JSON.stringify({ passed: true, checks }));
} finally {
  await browser.close();
  bridge.stdin.end();
  bridge.kill();
  await new Promise((r) => server.close(r));
}
