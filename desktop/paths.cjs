'use strict';
const path = require('node:path');
const fs = require('node:fs');
const os = require('node:os');

function externalPath(value, homes) {
  if (typeof value !== 'string' || !path.isAbsolute(value))
    throw Error('private_storage_must_be_absolute');
  // Resolve existing ancestors as well, so a junction cannot enter the software tree.
  function canonical(value) {
    if (fs.existsSync(value)) return fs.realpathSync(value);
    const parent = path.dirname(value);
    return parent === value ? value : path.join(canonical(parent), path.basename(value));
  }
  const target = canonical(path.resolve(value));
  for (const home of homes) {
    const rel = path.relative(canonical(path.resolve(home)), target);
    if (!rel || (!rel.startsWith(`..${path.sep}`) && rel !== '..' && !path.isAbsolute(rel)))
      throw Error('private_storage_must_be_outside_software');
  }
  return target;
}

function dataRoot(home, source, platform, env) {
  const homes = [home, source];
  if (env.AIEYRA_CONTROL_DATA) return externalPath(env.AIEYRA_CONTROL_DATA, homes);
  const user = os.homedir();
  const base =
    platform === 'win32'
      ? env.APPDATA || path.join(user, 'AppData', 'Roaming')
      : platform === 'darwin'
        ? path.join(user, 'Library', 'Application Support')
        : env.XDG_CONFIG_HOME || path.join(user, '.config');
  const locator = env.AIEYRA_CONTROL_STORAGE || path.join(base, 'Aieyra Control', 'storage.json');
  if (!path.isAbsolute(locator)) throw Error('storage_locator_must_be_absolute');
  if (fs.existsSync(locator)) {
    let target;
    try {
      const value = JSON.parse(fs.readFileSync(locator, 'utf8').replace(/^\uFEFF/, ''));
      if (value.schema_version !== 1) throw Error('invalid_schema');
      target = externalPath(value.data_root, homes);
    } catch {
      throw Error('invalid_storage_locator');
    }
    if (!fs.existsSync(target) || !fs.statSync(target).isDirectory())
      throw Error('configured_storage_unavailable');
    return target;
  }
  const legacy = path.join(home, 'data');
  if (fs.existsSync(legacy) && fs.readdirSync(legacy).length)
    throw Error('legacy_storage_requires_explicit_migration');
  const storage =
    platform === 'win32'
      ? env.LOCALAPPDATA || path.join(user, 'AppData', 'Local')
      : platform === 'darwin'
        ? path.join(user, 'Library', 'Application Support')
        : env.XDG_DATA_HOME || path.join(user, '.local', 'share');
  return externalPath(path.join(storage, 'Aieyra Control', 'data'), homes);
}

function installationPaths({
  packaged,
  platform = process.platform,
  executable = process.execPath,
  source = path.resolve(__dirname, '..'),
  env = process.env,
}) {
  const home = packaged
    ? platform === 'darwin'
      ? path.resolve(path.dirname(executable), '../../..')
      : path.dirname(executable)
    : source;
  const data = dataRoot(home, source, platform, env);
  const bundledPython = path.join(
    home,
    'runtime',
    'python',
    platform === 'win32' ? 'python.exe' : 'bin/python3',
  );
  return {
    home,
    data,
    shared: path.join(data, 'shared'),
    desktop: path.join(data, 'desktop'),
    config: path.join(data, 'config', 'control.json'),
    python: packaged || fs.existsSync(bundledPython) ? bundledPython : null,
  };
}
function prepareDirectories(paths) {
  if (paths.home.includes('/AppTranslocation/'))
    throw Error('请先把 Aieyra Control 文件夹移到固定位置，再打开应用。');
  for (const name of [
    'config',
    'shared',
    'agents',
    'desktop',
    'logs',
    'cache',
    'updates',
    'backups',
  ])
    fs.mkdirSync(path.join(paths.data, name), { recursive: true });
  fs.mkdirSync(path.join(paths.shared, 'projects'), { recursive: true });
  fs.accessSync(paths.data, fs.constants.W_OK);
}
module.exports = { installationPaths, prepareDirectories, externalPath };
