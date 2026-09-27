import { spawn, spawnSync } from 'node:child_process';
import { access, readdir, open } from 'node:fs/promises';
import { constants } from 'node:fs';
import { homedir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// Run inside WSL/Linux: no Windows console, WSLg surface or system audio.
if (process.platform !== 'linux')
  throw Error(
    'Run this check inside WSL/Linux with Linux Node. Windows browser checks require a separately verified isolated desktop.',
  );
let executable = process.env.CONTROL_CHROME;
if (!executable) {
  const cache = path.join(homedir(), '.cache/ms-playwright');
  const builds = (await readdir(cache).catch(() => []))
    .filter((name) => /^chromium-\d+$/.test(name))
    .sort((a, b) => Number(b.slice(9)) - Number(a.slice(9)));
  for (const build of builds) {
    for (const location of ['chrome-linux64/chrome', 'chrome-linux/chrome']) {
      const candidate = path.join(cache, build, location);
      try {
        await access(candidate, constants.X_OK);
        executable = candidate;
        break;
      } catch {}
    }
    if (executable) break;
  }
}
if (!executable) throw Error('Set CONTROL_CHROME to an installed Linux Chromium executable.');
const file = await open(executable, 'r'),
  magic = Buffer.alloc(4);
try {
  await file.read(magic, 0, 4, 0);
} finally {
  await file.close();
}
if (!magic.equals(Buffer.from([0x7f, 0x45, 0x4c, 0x46])))
  throw Error('CONTROL_CHROME must be a Linux ELF executable; Windows launch paths are disabled.');
const env = { ...process.env, CONTROL_CHROME: executable };
for (const key of ['DISPLAY', 'WAYLAND_DISPLAY', 'PULSE_SERVER']) delete env[key];
if (process.env.CONTROL_HEADLESS_LIBS)
  env.LD_LIBRARY_PATH = [process.env.CONTROL_HEADLESS_LIBS, env.LD_LIBRARY_PATH]
    .filter(Boolean)
    .join(':');
const browserProbe = spawnSync(executable, ['--version'], {
  env,
  encoding: 'utf8',
  timeout: 10000,
  windowsHide: true,
});
if (browserProbe.error || browserProbe.status !== 0)
  throw Error(
    `Linux Chromium cannot start: ${browserProbe.error?.message || browserProbe.stderr || browserProbe.stdout}`,
  );
const directory = path.dirname(fileURLToPath(import.meta.url));
console.log(
  JSON.stringify({
    runner: 'linux-headless',
    executable,
    display: null,
    wayland: null,
    audio: null,
  }),
);
if (process.argv.includes('--legacy'))
  throw Error('Superseded browser suites were retired; run the current home checks.');
const suite = process.argv.includes('--live')
  ? 'office-home-live.mjs'
  : process.argv.includes('--cloud')
    ? 'cloud-design.mjs'
    : 'office-home.mjs';
env.CONTROL_TEST_OUTPUT ||= path.resolve(
  directory,
  '../test-output',
  `${path.basename(suite, '.mjs')}-${Date.now()}-${process.pid}`,
);
console.log(JSON.stringify({ suite, output: env.CONTROL_TEST_OUTPUT }));
const child = spawn(
  process.execPath,
  [
    path.join(directory, suite),
    ...process.argv.slice(2).filter((a) => !['--legacy', '--live', '--cloud'].includes(a)),
  ],
  { env, stdio: 'inherit', windowsHide: true },
);
for (const signal of ['SIGINT', 'SIGTERM']) process.once(signal, () => child.kill(signal));
child.once('error', (error) => {
  console.error(error.message);
  process.exitCode = 1;
});
child.once('exit', (code, signal) => {
  process.exitCode = code ?? (signal ? 1 : 0);
});
