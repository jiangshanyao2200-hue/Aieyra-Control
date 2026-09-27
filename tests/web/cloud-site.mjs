import { chromium } from 'playwright';
import { mkdir, writeFile } from 'node:fs/promises';
import assert from 'node:assert/strict';
import path from 'node:path';
const out = process.env.CONTROL_TEST_OUTPUT;
if (!out || process.platform !== 'linux')
  throw Error('Use Linux headless with an evidence directory');
await mkdir(out, { recursive: true });
const browser = await chromium.launch({
  headless: true,
  executablePath: process.env.CONTROL_CHROME,
});
const errors = [],
  results = [];
try {
  for (const [label, width, height] of [
    ['desktop', 1440, 1000],
    ['mobile', 390, 844],
  ]) {
    const context = await browser.newContext({ viewport: { width, height }, locale: 'zh-CN' });
    const page = await context.newPage();
    page.on('pageerror', (e) => errors.push(String(e)));
    for (const route of ['/', '/share', '/center', '/download', '/feedback']) {
      const response = await page.goto('https://ctrl.aieyra.cn' + route, {
        waitUntil: 'domcontentloaded',
        timeout: 60000,
      });
      await page.locator('h1').waitFor({ state: 'visible' });
      assert.equal(response.status(), 200);
      assert.equal(await page.locator('nav a').count(), 4);
      assert.ok(await page.locator('h1').isVisible());
      assert.equal(await page.locator('.site-rail nav a').count(), 4);
      if (route === '/') {
        assert.equal(await page.locator('h1').innerText(), 'Aieyra Control');
        assert.equal(await page.locator('main p').count(), 1);
        assert.equal(await page.locator('canvas').count(), 1);
        await page.waitForTimeout(1250);
      }
      if (route === '/share' || route === '/center') {
        assert.equal(await page.locator('[data-center-topics]').count(), 1);
        assert.equal(await page.locator('textarea').count(), 0);
        await page.waitForFunction(() => {
          const state = document.querySelector('[data-center-status]');
          return state && !/正在读取/.test(state.textContent);
        });
        assert.match(await page.locator('[data-center-status]').innerText(), /已展示|暂时没有/);
      }
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
      if (route === '/download' && process.env.CONTROL_RELEASE_READY === '1') {
        await page.locator('[data-login]').waitFor({ state: 'visible', timeout: 60000 });
        assert.equal(await page.locator('[data-downloads] a').count(), 1);
        assert.ok(await page.locator('[data-login]').isVisible());
        assert.equal(
          await page.locator('[data-downloads] a').getAttribute('href'),
          'https://github.com/jiangshanyao2200-hue/Aieyra-Control',
        );
      }
      if (route === '/feedback') {
        await page.locator('[data-login]').waitFor({ state: 'visible', timeout: 60000 });
        assert.equal(await page.locator('[data-ticket]').count(), 0);
      }
      await page.screenshot({
        path: path.join(out, label + '-' + (route.slice(1) || 'home') + '.png'),
        fullPage: true,
      });
      results.push({ label, route, passed: true });
    }
    await context.close();
  }
  assert.deepEqual(errors, []);
} finally {
  await browser.close();
  await writeFile(path.join(out, 'results.json'), JSON.stringify({ results, errors }, null, 2));
}
console.log(JSON.stringify({ passed: results.length, errors }));
