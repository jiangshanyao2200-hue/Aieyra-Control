import { esc } from './model.js';

export function createAccountPanel({ request, onChange }) {
  let value = { enabled: false },
    busy = false,
    message = '',
    url = '',
    pollTimer = null,
    generation = 0,
    refreshGeneration = 0;
  const stop = () => {
    clearTimeout(pollTimer);
    pollTimer = null;
  };
  const validLink = (v) => {
    try {
      const u = new URL(v);
      return (
        u.origin === 'https://api.aieyra.cn' &&
        u.pathname === '/aieyra/control/authorize' &&
        /^[A-Za-z0-9_-]{43}$/.test(u.searchParams.get('flow'))
      );
    } catch {
      return false;
    }
  };
  const verified = (candidate) => {
    if (!candidate || typeof candidate.enabled !== 'boolean') throw Error('invalid_account');
    return candidate;
  };
  function markup() {
    return `<section class="account-panel"><h3>${value.enabled ? esc(value.user?.name || 'Aieyra') : '登录 Aieyra'}</h3><p class="dialog-meta">${value.enabled ? '已连接 · 接收更新' : '登录后下载与接收更新'}</p>${url ? `<a class="account-primary" href="${esc(url)}" target="_blank" rel="noopener noreferrer">继续登录 ↗</a>` : ''}<p role="status">${esc(message)}</p><div class="account-actions">${value.enabled ? `<button data-account="check" ${busy ? 'disabled' : ''}>检查更新</button><button data-account="logout" ${busy ? 'disabled' : ''}>退出登录</button>` : `<button class="account-primary" data-account="login" ${busy ? 'disabled' : ''}>${url ? '重新登录' : '登录'}</button>`}</div>${value.release?.manifest ? `<p class="dialog-meta">版本 ${esc(value.release.manifest.version)} · 由 Agent 审阅更新</p>` : ''}</section>`;
  }
  async function refresh() {
    if (busy) return;
    const actionId = generation;
    const readId = ++refreshGeneration;
    try {
      const candidate = verified(await request('/api/cloud'));
      if (actionId !== generation || readId !== refreshGeneration) return;
      value = candidate;
      message = '';
    } catch {
      if (actionId !== generation || readId !== refreshGeneration) return;
      message = '暂时无法连接，请重试。';
    }
    onChange();
  }
  async function poll(id) {
    if (id !== generation) return;
    try {
      const r = await request('/api/cloud/poll', {});
      if (id !== generation) return;
      if (!r.pending) {
        value = verified(r);
        refreshGeneration++;
        url = '';
        message = '已登录';
        onChange();
        return;
      }
    } catch {
      if (id !== generation) return;
      message = '连接已过期，请重新登录。';
      url = '';
      onChange();
      return;
    }
    pollTimer = setTimeout(() => poll(id), 1500);
  }
  async function action(name) {
    if (busy || !['login', 'logout', 'check'].includes(name)) return;
    const id = ++generation;
    refreshGeneration++;
    stop();
    busy = true;
    message = '';
    onChange();
    try {
      if (name === 'login') {
        const result = await request('/api/cloud/login', {});
        if (id !== generation) return;
        if (!validLink(result.authorize_url)) throw Error('invalid_link');
        url = result.authorize_url;
        message = '在浏览器完成登录后将自动连接。';
        pollTimer = setTimeout(() => poll(id), 1500);
      } else {
        await request('/api/cloud/' + name, {});
        if (id !== generation) return;
        if (name === 'logout') {
          url = '';
        }
        const candidate = verified(await request('/api/cloud'));
        if (id !== generation) return;
        value = candidate;
        message = name === 'check' ? '已检查更新' : '已退出登录';
      }
    } catch {
      if (id !== generation) return;
      message = '暂未完成，请重试。';
    } finally {
      if (id === generation) {
        busy = false;
        onChange();
      }
    }
  }
  window.addEventListener('pagehide', () => {
    generation++;
    stop();
  });
  return { markup, refresh, action, enabled: () => value.enabled };
}
