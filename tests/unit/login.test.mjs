import assert from 'node:assert/strict';
import test from 'node:test';
import { createRequire } from 'node:module';
const { openLogin } = createRequire(import.meta.url)('../../desktop/login.cjs');
const url = 'https://api.aieyra.cn/aieyra/control/authorize?flow=' + 'a'.repeat(43);

test('desktop login opens only an exact official authorization URL', async () => {
  const calls = [];
  const shell = { openExternal: async (value) => calls.push(value) };
  assert.equal(await openLogin(url, shell), true);
  for (const bad of [
    'file:///tmp/private',
    url.replace('api.aieyra.cn', 'api.aieyra.cn.evil.invalid'),
    url.replace('https://', 'https://user@'),
    url + '&redirect=https://evil.invalid',
    url + '&flow=' + 'b'.repeat(43),
    url + '#extra',
  ])
    assert.equal(await openLogin(bad, shell), false);
  assert.deepEqual(calls, [url]);
  assert.equal(
    await openLogin(url, {
      openExternal: async () => {
        throw Error('unavailable');
      },
    }),
    false,
  );
});
