const CLOUD = 'https://ctrlupdate.aieyra.cn';
const $ = (s) => document.querySelector(s),
  status = $('[role="status"]');
const say = (value) => {
  if (status) status.textContent = value;
};
async function api(path, body, token) {
  const controller = new AbortController(),
    timeout = setTimeout(() => controller.abort(), 12000);
  try {
    const r = await fetch(CLOUD + path, {
      method: body ? 'POST' : 'GET',
      headers: {
        ...(body ? { 'Content-Type': 'application/json' } : {}),
        ...(token ? { Authorization: 'Bearer ' + token } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
      cache: 'no-store',
      credentials: 'include',
      signal: controller.signal,
    });
    if (!r.ok) {
      const e = Error('unavailable');
      e.status = r.status;
      throw e;
    }
    return await r.json();
  } finally {
    clearTimeout(timeout);
  }
}
const readSession = (key) => {
  try {
    return JSON.parse(sessionStorage.getItem(key) || 'null');
  } catch {
    return null;
  }
};
const b64 = (b) =>
  btoa(String.fromCharCode(...new Uint8Array(b)))
    .replaceAll('+', '-')
    .replaceAll('/', '_')
    .replaceAll('=', '');
const random = () => b64(crypto.getRandomValues(new Uint8Array(32)));
async function login() {
  const verifier = random(),
    state = random(),
    redirect_uri = location.origin + '/auth/callback';
  const challenge = b64(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier)));
  const flow = await api('/v1/auth/start', { challenge, state, redirect_uri, scope: 'browser' });
  if (
    typeof flow.flow_id !== 'string' ||
    !String(flow.authorize_url).startsWith('https://api.aieyra.cn/aieyra/control/authorize?')
  )
    throw Error('invalid_flow');
  sessionStorage.setItem(
    'control-login',
    JSON.stringify({ verifier, state, redirect_uri, flow_id: flow.flow_id }),
  );
  location.assign(flow.authorize_url);
}
$('[data-login]')?.addEventListener('click', () =>
  login().catch(() => say('连接未完成，请重试。')),
);
if (location.pathname === '/auth/callback') {
  const q = new URLSearchParams(location.search),
    flow = readSession('control-login');
  history.replaceState(null, '', '/auth/callback');
  if (flow && q.get('state') === flow.state && q.get('flow') === flow.flow_id) {
    try {
      const r = await api('/v1/auth/exchange', { ...flow, code: q.get('code') });
      sessionStorage.removeItem('control-login');
      sessionStorage.removeItem('control-session');
      say('已连接。');
      $('[data-login]').hidden = true;
      const next = sessionStorage.getItem('control-return');
      sessionStorage.removeItem('control-return');
      location.replace(next === '/feedback' ? next : '/download');
    } catch (error) {
      say(
        error.status === 401
          ? '登录已过期，请重新登录。'
          : '登录暂未完成，请重新登录；账号和密码无需在 Control 中填写。',
      );
    }
  } else if (q.get('code') && q.get('flow')) {
    say('授权已完成，请返回发起授权的应用；客户端会自动连接。');
    $('[data-login]').hidden = true;
  } else say('没有待完成的登录，请重新登录。');
}

const SOURCE = 'https://github.com/jiangshanyao2200-hue/Aieyra-Control';
function sourceLink() {
  const a = document.createElement('a');
  a.className = 'download-source';
  a.href = SOURCE;
  a.textContent = 'GitHub ↗';
  a.rel = 'noopener noreferrer';
  return a;
}
async function loadRelease() {
  const downloads = $('[data-downloads]'),
    retry = $('[data-release-retry]');
  retry.hidden = true;
  $('[data-release-details]').hidden = true;
  $('[data-identity]').replaceChildren();
  downloads.replaceChildren(sourceLink());
  try {
    let user;
    try {
      user = await api('/v1/session');
    } catch (e) {
      if (e.status !== 401) throw e;
      $('[data-version]').textContent = 'Windows';
      $('[data-size]').textContent = '';
      const button = document.createElement('button');
      button.className = 'download-primary';
      button.dataset.login = '';
      button.textContent = '登录下载 ↗';
      button.addEventListener('click', () => {
        button.disabled = true;
        login().catch(() => {
          button.disabled = false;
          say('连接未完成，请重试。');
        });
      });
      downloads.prepend(button);
      say('使用 Aieyra 账号');
      return;
    }
    const value = await api('/v1/releases/stable'),
      m = value.manifest;
    if (!m) {
      say('即将开放');
      return;
    }
    if (typeof m.version !== 'string' || !/^[0-9]+[.][0-9]+[.][0-9]+$/.test(m.version))
      throw Error('shape');
    const valid = (a) =>
      a &&
      /^\/artifacts\/[A-Za-z0-9_.-]+$/.test(a.path) &&
      /^[a-f0-9]{64}$/.test(a.sha256) &&
      Number.isSafeInteger(a.size) &&
      a.size > 0;
    const available = m.platforms || { 'windows-x64': m.portable };
    const platforms = { 'windows-x64': available['windows-x64'] };
    const names = { 'windows-x64': 'Windows' };
    if (!valid(platforms['windows-x64'])) throw Error('shape');
    $('[data-version]').textContent = m.version;
    $('[data-size]').textContent = '';
    downloads.replaceChildren();
    const hash = [];
    for (const [key, a] of Object.entries(platforms)) {
      const primary = document.createElement('a');
      primary.className = 'download-primary';
      primary.href = CLOUD + a.path;
      primary.textContent = names[key] + ' ↗';
      primary.setAttribute('aria-label', '下载 ' + names[key]);
      const size = document.createElement('small');
      size.textContent = Math.round(a.size / 1048576) + ' MB';
      primary.append(size);
      downloads.append(primary);
      hash.push(names[key] + '\n' + a.sha256);
    }
    downloads.append(sourceLink());
    $('[data-hash]').textContent = hash.join('\n\n');
    $('[data-release-details]').hidden = false;
    const logout = document.createElement('button');
    logout.className = 'text-action';
    logout.textContent = '退出登录';
    logout.addEventListener('click', async () => {
      logout.disabled = true;
      try {
        await api('/v1/auth/logout', {});
        await loadRelease();
      } catch {
        logout.disabled = false;
        say('退出未完成，请重试。');
      }
    });
    const identity = $('[data-identity]');
    identity.replaceChildren(
      document.createTextNode((user.user?.name || '已登录') + ' · '),
      logout,
    );
    say('');
  } catch {
    downloads.replaceChildren(sourceLink());
    say('暂时无法连接');
    retry.hidden = false;
  }
}
if (location.pathname === '/download') {
  $('[data-release-retry]').addEventListener('click', loadRelease);
  loadRelease();
}
