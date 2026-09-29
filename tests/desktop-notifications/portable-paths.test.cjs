'use strict';
const test = require('node:test'),
  assert = require('node:assert/strict'),
  path = require('node:path');
const { installationPaths } = require('../../desktop/paths.cjs');
test('source installations keep private data outside software', () => {
  const source = path.resolve('fixture-installation'),
    data = path.resolve('fixture-private-data'),
    p = installationPaths({ packaged: false, source, env: { AIEYRA_CONTROL_DATA: data } });
  assert.equal(p.home, source);
  assert.equal(p.shared, path.join(data, 'shared'));
  assert.equal(p.config, path.join(data, 'config', 'control.json'));
});
test('native Mac app resolves portable folder outside signed bundle', () => {
  const folder = path.resolve('fixture-mac'),
    p = installationPaths({
      packaged: true,
      source: folder,
      env: { AIEYRA_CONTROL_DATA: path.resolve('fixture-private-mac') },
      platform: 'darwin',
      executable: path.join(folder, 'Aieyra Control.app', 'Contents', 'MacOS', 'Electron'),
    });
  assert.equal(p.home, folder);
  assert.equal(p.python, path.join(folder, 'runtime', 'python', 'bin/python3'));
  assert.ok(!p.data.includes('Contents'));
});
