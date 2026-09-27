'use strict';

function validLoginUrl(value) {
  try {
    const url = new URL(value);
    return (
      url.origin === 'https://api.aieyra.cn' &&
      !url.username &&
      !url.password &&
      !url.hash &&
      url.pathname === '/aieyra/control/authorize' &&
      [...url.searchParams.keys()].join() === 'flow' &&
      /^[A-Za-z0-9_-]{43}$/.test(url.searchParams.get('flow'))
    );
  } catch {
    return false;
  }
}

function portalUrl(value) {
  try {
    const url = new URL(value);
    return (
      !url.username &&
      !url.password &&
      (url.origin === 'https://api.aieyra.cn' ||
        (url.origin === 'https://ctrlupdate.aieyra.cn' && url.pathname === '/auth/callback'))
    );
  } catch {
    return false;
  }
}

function shellPage(failed, target = '') {
  const link = validLoginUrl(target) ? `<a href="${target}">重试连接</a>` : '';
  return (
    'data:text/html;charset=utf-8,' +
    encodeURIComponent(
      `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"><title>Aieyra 账号</title><style>:root{color-scheme:dark;font:15px 'Segoe UI','Microsoft YaHei',sans-serif;background:#10191d;color:#d9e8e2}body{margin:0;min-height:100vh;display:grid;place-items:center}main{max-width:380px;padding:32px;text-align:center}h1{font-size:23px;font-weight:500}p{line-height:1.8;color:#9ab3a8}a{display:inline-block;color:#d9e8e2;padding:12px 24px;border:1px solid #527264;border-radius:8px;text-decoration:none}</style></head><body><main><h1>${failed ? '暂时无法打开登录页' : '正在打开 Aieyra 登录'}</h1><p>${failed ? '请检查网络后重试，或返回 Control 重新登录。' : '正在建立安全连接，请稍候…'}</p>${link}</main></body></html>`,
    )
  );
}

class LoginWindow {
  constructor({ BrowserWindow, session, hidden = false }) {
    this.BrowserWindow = BrowserWindow;
    this.session = session;
    this.hidden = hidden;
    this.window = null;
    this.generation = 0;
    this.timer = null;
    this.target = '';
  }

  prepare(owner) {
    this.close();
    this.owner = owner;
    const account = this.session.fromPartition('persist:control-account');
    account.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
    account.setPermissionCheckHandler(() => false);
    const portal = new this.BrowserWindow({
      width: 1080,
      height: 800,
      minWidth: 720,
      minHeight: 560,
      show: !this.hidden,
      title: 'Aieyra 账号 · 登录',
      parent: owner,
      autoHideMenuBar: true,
      backgroundColor: '#10191d',
      webPreferences: {
        session: account,
        nodeIntegration: false,
        contextIsolation: true,
        sandbox: true,
        webSecurity: true,
        allowRunningInsecureContent: false,
        backgroundThrottling: false,
      },
    });
    this.window = portal;
    const boundary = (event, url) => {
      if (this.window !== portal || !portalUrl(url)) event.preventDefault();
    };
    portal.webContents.on('will-navigate', boundary);
    portal.webContents.on('will-redirect', boundary);
    portal.webContents.on('will-attach-webview', (event) => event.preventDefault());
    portal.webContents.setWindowOpenHandler(({ url }) => {
      if (this.window === portal && portalUrl(url)) void this.navigate(url);
      return { action: 'deny' };
    });
    portal.webContents.on('did-start-navigation', (_event, url, inPlace, mainFrame) => {
      if (!mainFrame || inPlace || !portalUrl(url) || this.window !== portal) return;
      this.armTimeout();
    });
    portal.webContents.on('did-finish-load', () => {
      if (this.window === portal) clearTimeout(this.timer);
    });
    portal.webContents.on('did-fail-load', (_event, code, _description, _url, mainFrame) => {
      if (mainFrame && code !== -3 && this.window === portal) this.fail();
    });
    portal.on('closed', () => {
      if (this.window !== portal) return;
      clearTimeout(this.timer);
      this.window = null;
      this.generation++;
    });
    void portal.loadURL(shellPage(false)).catch(() => {});
    if (!this.hidden) portal.focus();
    return true;
  }

  armTimeout() {
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.fail(), 25000);
    this.timer.unref?.();
  }

  open(url, owner) {
    if (!validLoginUrl(url)) return false;
    if (!this.window || this.window.isDestroyed()) this.prepare(owner);
    this.target = url;
    void this.navigate(url);
    return true;
  }

  async navigate(url) {
    if (!portalUrl(url) || !this.window || this.window.isDestroyed()) return false;
    const portal = this.window,
      generation = ++this.generation;
    this.armTimeout();
    try {
      await portal.loadURL(url);
      return this.window === portal && this.generation === generation;
    } catch (error) {
      if (
        this.window === portal &&
        this.generation === generation &&
        Number(error.code || error.errno) !== -3
      )
        this.fail();
      return false;
    }
  }

  fail() {
    const portal = this.window;
    if (!portal || portal.isDestroyed()) return;
    this.generation++;
    clearTimeout(this.timer);
    portal.webContents.stop();
    void portal.loadURL(shellPage(true, this.target)).catch(() => {});
  }

  close(completed = false) {
    const portal = this.window;
    this.window = null;
    this.target = '';
    this.generation++;
    clearTimeout(this.timer);
    if (portal && !portal.isDestroyed()) portal.destroy();
    if (completed && !this.hidden && this.owner && !this.owner.isDestroyed()) {
      this.owner.show();
      this.owner.focus();
    }
    return true;
  }
}

module.exports = { validLoginUrl, portalUrl, LoginWindow };
