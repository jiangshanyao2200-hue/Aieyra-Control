'use strict';
const path = require('node:path');
const fs = require('node:fs');

function installationPaths({ packaged, platform = process.platform, executable = process.execPath, source = path.resolve(__dirname, '..') }) {
  const home = packaged ? (platform === 'darwin' ? path.resolve(path.dirname(executable), '../../..') : path.dirname(executable)) : source;
  const data = path.join(home, 'data');
  return { home, data, shared: path.join(data, 'shared'), desktop: path.join(data, 'desktop'),
    config: path.join(data, 'config', 'control.json'),
    python: packaged ? path.join(home, 'runtime', 'python', platform === 'win32' ? 'python.exe' : 'bin/python3') : null };
}
function prepareDirectories(paths) {
  if (paths.home.includes('/AppTranslocation/')) throw Error('请先把 Aieyra Control 文件夹移到固定位置，再打开应用。');
  for (const name of ['config', 'shared', 'agents', 'desktop', 'logs', 'cache', 'updates', 'backups']) fs.mkdirSync(path.join(paths.data, name), { recursive: true });
  fs.accessSync(paths.data, fs.constants.W_OK);
}
module.exports = { installationPaths, prepareDirectories };
