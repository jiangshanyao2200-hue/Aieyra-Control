import { esc } from './model.js';

export function createAccountPanel({ request, onChange }) {
  let value = { enabled: false },
    busy = false,
    message = '',
    url = '',
    pollTimer = null,
    generation = 0,
    refreshGeneration = 0,
    expires = 0,
    failures = 0;
  const stop = () => {
    clearTimeout(pollTimer);
    pollTimer = null;
  };
  const validLink = (v) => {
    try {
      const u = new URL(v);
      return (
        u.origin === 'https://api.aieyra.cn' &&
        !u.username &&
        !u.password &&
        !u.hash &&
        u.pathname === '/aieyra/control/authorize' &&
        [...u.searchParams.keys()].join() === 'flow' &&
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
  const expiry = (seconds) => Date.now() + Math.min(300, Math.max(0, Number(seconds) || 0)) * 1000;
  function complete(candidate) {
    candidate = verified(candidate);
    if (!candidate.enabled) throw Error('invalid_account');
    value = candidate;
    if (url) void window.controlPlatform?.finishLogin?.().catch(() => {});
    stop();
    refreshGeneration++;
    url = '';
    expires = 0;
    message = '已登录';
  }
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
      if (value.enabled) complete(value);
      else if (value.pending && validLink(value.authorize_url)) {
        url = value.authorize_url;
        expires = expiry(value.expires_in);
        if (pollTimer === null) pollTimer = setTimeout(() => poll(actionId), 1500);
        if (!message) message = '在登录窗口完成授权后将自动连接。';
      } else if (!url) message = '';
    } catch {
      if (actionId !== generation || readId !== refreshGeneration) return;
      message = '暂时无法连接，请重试。';
    }
    onChange();
  }
  async function poll(id) {
    if (id !== generation) return;
    if (Date.now() >= expires) {
      stop();
      url = '';
      message = '登录已过期，请重新登录。';
      onChange();
      return;
    }
    try {
      const r = await request('/api/cloud/poll', {});
      if (id !== generation) return;
      if (!r.pending) {
        complete(r);
        onChange();
        return;
      }
      failures = 0;
      message = '在登录窗口完成授权后将自动连接。';
      onChange();
    } catch (error) {
      if (id !== generation) return;
      if (error.status >= 400 && error.status < 500 && ![408, 429].includes(error.status)) {
        stop();
        message =
          error.status === 403 ? '账号暂时无法使用，请确认账号状态。' : '登录已过期，请重新登录。';
        url = '';
        onChange();
        return;
      }
      failures++;
      message = '连接暂时中断，正在重试；无需重新登录。';
      onChange();
    }
    pollTimer = setTimeout(() => poll(id), Math.min(15000, 1500 * 2 ** Math.min(failures, 4)));
  }
  async function action(name) {
    if (busy || !['login', 'logout', 'check'].includes(name)) return;
    const id = ++generation;
    refreshGeneration++;
    stop();
    busy = true;
    message = '';
    let popup = null;
    if (name === 'login') {
      url = '';
      failures = 0;
      // Reserve a browser tab during the click, before the asynchronous request.
      if (!window.controlPlatform?.openLogin) {
        try {
          popup = window.open('', '_blank');
          if (popup) popup.opener = null;
        } catch {}
      }
    }
    onChange();
    try {
      if (name === 'login') {
        await window.controlPlatform?.prepareLogin?.();
        const result = await request('/api/cloud/login', {});
        if (id !== generation) return;
        if (!validLink(result.authorize_url)) throw Error('invalid_link');
        url = result.authorize_url;
        expires = expiry(result.expires_in);
        message = '在登录窗口完成授权后将自动连接。';
        pollTimer = setTimeout(() => poll(id), 1500);
        let opened = false;
        try {
          if (window.controlPlatform?.openLogin)
            opened = await window.controlPlatform.openLogin(url);
          else if (popup && !popup.closed) {
            popup.location.replace(url);
            popup = null;
            opened = true;
          }
        } catch {}
        if (id !== generation) return;
        if (!opened) message = '请点击“继续登录”打开登录页，完成后将自动连接。';
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
      if (name === 'login') void window.controlPlatform?.loginFailed?.().catch(() => {});
    } finally {
      popup?.close();
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
