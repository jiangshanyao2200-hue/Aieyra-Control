'use strict';
const test = require('node:test'),
  assert = require('node:assert/strict'),
  path = require('node:path');
const { installationPaths } = require('../../desktop/paths.cjs');
test('source installations keep user data beside source and honor projects elsewhere', () => {
  const source = path.resolve('fixture-installation'),
    p = installationPaths({ packaged: false, source });
  assert.equal(p.home, source);
  assert.equal(p.shared, path.join(source, 'data', 'shared'));
  assert.equal(p.config, path.join(source, 'data', 'config', 'control.json'));
});
test('native Mac app resolves portable folder outside signed bundle', () => {
  const folder = path.resolve('fixture-mac'),
    p = installationPaths({
      packaged: true,
      platform: 'darwin',
      executable: path.join(folder, 'Aieyra Control.app', 'Contents', 'MacOS', 'Electron'),
    });
  assert.equal(p.home, folder);
  assert.equal(p.python, path.join(folder, 'runtime', 'python', 'bin/python3'));
  assert.ok(!p.data.includes('Contents'));
});
