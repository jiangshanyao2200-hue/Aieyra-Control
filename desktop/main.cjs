'use strict';
const {
  app,
  BrowserWindow,
  Menu,
  Tray,
  nativeImage,
  ipcMain,
  shell,
  Notification,
  session,
} = require('electron');
const path = require('node:path');
const fs = require('node:fs');
const { randomUUID } = require('node:crypto');
const { ServiceSupervisor } = require('./supervisor.cjs');
const { HumanNotificationLedger } = require('./human-notifications.cjs');
const { HumanNotificationHost } = require('./human-notification-host.cjs');
const { HumanRequestFeed } = require('./human-request-feed.cjs');
const { OSOwnerPipe, validOwnerPipeName } = require('./os-owner-pipe.cjs');
const { installationPaths, prepareDirectories } = require('./paths.cjs');
const { LoginWindow, validLoginUrl } = require('./login.cjs');
const root = path.resolve(__dirname, '..');
const installation = installationPaths({ packaged: app.isPackaged, source: root });
try {
  prepareDirectories(installation);
} catch (error) {
  require('electron').dialog.showErrorBox(
    'Aieyra Control',
    error.message + '\n请将软件解压到可写的文件夹。',
  );
  app.exit(1);
  throw error;
}
process.env.AIEYRA_CONTROL_HOME = installation.home;
process.env.AIEYRA_CONTROL_NODE = process.execPath;
const args = process.argv.slice(1);
const option = (name) =>
  args.find((value) => value.startsWith(`--${name}=`))?.slice(name.length + 3);
const testing = process.env.AIEYRA_CONTROL_PLATFORM_TEST === '1';
const loginWindow = new LoginWindow({ BrowserWindow, session, hidden: testing });
const osManaged = args.includes('--os-managed');
const ownerStdin = args.includes('--os-owner-stdin');
const ownerPipeName = option('os-owner-pipe');
const ownerRequested = ownerStdin || ownerPipeName !== undefined;
app.setName(osManaged ? 'Aieyra OS · 指挥中心' : 'Aieyra Control');
if (process.platform === 'win32') app.setAppUserModelId('cn.aieyra.control');
if (option('user-data-dir')) app.setPath('userData', path.resolve(option('user-data-dir')));
else app.setPath('userData', installation.desktop);
app.setPath('sessionData', path.join(installation.data, 'cache', 'browser'));
app.setPath('logs', path.join(installation.data, 'logs'));
app.setPath('crashDumps', path.join(installation.data, 'logs', 'crashes'));
const port = Number(option('port') || 17910);
const supervisor = new ServiceSupervisor({
  python:
    option('python') ||
    process.env.AIEYRA_CONTROL_PYTHON ||
    installation.python ||
    (process.platform === 'win32' ? 'python' : 'python3'),
  script:
    testing && process.env.AIEYRA_CONTROL_TEST_SERVICE
      ? path.resolve(process.env.AIEYRA_CONTROL_TEST_SERVICE)
      : path.join(root, 'service', 'main.py'),
  cwd: root,
  port,
  dataDir: option('service-data-dir') || installation.shared,
  config: option('config') || installation.config,
});
if (process.env.AIEYRA_CONTROL_PACKAGE_TEST === '1') {
  console.log(
    JSON.stringify({
      root,
      home: installation.home,
      packaged: app.isPackaged,
      python: supervisor.python,
      port,
    }),
  );
  supervisor.on('state', (value) => console.log(JSON.stringify(value)));
  process.on('unhandledRejection', (error) => console.error(error));
}
let window = null,
  tray = null,
  quitting = false,
  cleaned = false,
  cleanup = null,
  loadedService = false,
  loadingService = false;
let humanHost = null,
  humanFeed = null,
  ownerPipe = null,
  humanReady = false,
  pendingHumanOpen = null;
let ownerFailureCode = 0;
let menuSignature = null;
const humans = new HumanNotificationLedger({
  file: path.join(app.getPath('userData'), 'human-notifications.json'),
});
const settingsPath = path.join(app.getPath('userData'), 'desktop.json');
let preferences = { background: false };
try {
  const saved = JSON.parse(fs.readFileSync(settingsPath, 'utf8'));
  preferences.background = saved.background === true;
} catch {}
if (args.includes('--background')) preferences.background = true;
const hidden = osManaged || args.includes('--hidden') || args.includes('--background');

function savePreferences() {
  fs.mkdirSync(path.dirname(settingsPath), { recursive: true });
  const temporary = `${settingsPath}.tmp`;
  fs.writeFileSync(temporary, JSON.stringify(preferences));
  fs.renameSync(temporary, settingsPath);
}
function status() {
  return {
    ...supervisor.snapshot(),
    host_mode: osManaged ? 'os_managed' : 'standalone',
    background: osManaged || preferences.background,
    trayAvailable: Boolean(tray),
    human_requests: humans.snapshot(),
    human_feed: humanFeed?.snapshot() || null,
  };
}
function showWindow() {
  if (!window || window.isDestroyed()) createWindow();
  if (window.isMinimized()) window.restore();
  window.show();
  window.focus();
}
function flushHumanOpen() {
  if (humanReady && pendingHumanOpen && window && !window.isDestroyed())
    window.webContents.send('control:human-open-request', pendingHumanOpen);
}
function openHumanRequest(id = null) {
  const reference = humans.reference(typeof id === 'string' ? id : null);
  pendingHumanOpen = { ...(reference || { id: null, version: null }), navigation_id: randomUUID() };
  showWindow();
  flushHumanOpen();
}
function setBackground(value) {
  if (osManaged) return status();
  preferences.background = Boolean(value) && Boolean(tray);
  savePreferences();
  updateMenus();
  return status();
}
function updateMenus() {
  const state = supervisor.snapshot();
  const labels = {
    ready: '服务已连接',
    starting: '服务启动中',
    reconnecting: '服务重连中',
    conflict: '端口冲突',
    offline: '服务离线',
    unavailable: '等待服务就绪',
    stopped: '服务已停止',
  };
  const template = [
    { label: '打开控制台', click: showWindow },
    { label: labels[state.state] || '正在连接', enabled: false },
    { type: 'separator' },
    ...(humanHost?.menu() || []),
    { type: 'separator' },
    {
      label: osManaged ? '随 Aieyra OS 保持提醒' : '关闭窗口后在后台运行',
      type: 'checkbox',
      checked: osManaged || preferences.background,
      enabled: !osManaged && Boolean(tray),
      click: (item) => setBackground(item.checked),
    },
    { label: '重新连接服务', click: () => supervisor.retry() },
    {
      label: '重新加载界面',
      click: () => {
        if (window && !window.isDestroyed()) window.webContents.reload();
      },
    },
    { type: 'separator' },
    {
      label: osManaged ? '关闭指挥中心窗口' : '退出 Aieyra Control',
      click: () => (osManaged ? window?.hide() : app.quit()),
    },
  ];
  const signature = JSON.stringify(template);
  if (signature === menuSignature) return;
  menuSignature = signature;
  tray?.setContextMenu(Menu.buildFromTemplate(template));
  const attention = humans.snapshot();
  tray?.setToolTip(
    `Aieyra Control · ${labels[state.state] || '正在连接'}${attention.current && attention.pending_count ? ` · ${attention.pending_count} 项待你处理` : ''}`,
  );
  Menu.setApplicationMenu(
    Menu.buildFromTemplate([
      { label: '控制台', submenu: template },
      {
        label: '编辑',
        submenu: [
          { role: 'undo' },
          { role: 'redo' },
          { type: 'separator' },
          { role: 'cut' },
          { role: 'copy' },
          { role: 'paste' },
          { role: 'selectAll' },
        ],
      },
      {
        label: '视图',
        submenu: [
          { role: 'resetZoom' },
          { role: 'zoomIn' },
          { role: 'zoomOut' },
          { role: 'togglefullscreen' },
        ],
      },
    ]),
  );
}
function allowed(url) {
  try {
    const parsed = new URL(url);
    return (
      parsed.origin === supervisor.baseUrl ||
      url === require('node:url').pathToFileURL(path.join(__dirname, 'waiting.html')).href
    );
  } catch {
    return false;
  }
}
function openExternal(url) {
  try {
    const parsed = new URL(url);
    if (['https:', 'http:'].includes(parsed.protocol) && !parsed.username && !parsed.password)
      void shell.openExternal(parsed.href);
  } catch {}
}
function createWindow() {
  window = new BrowserWindow({
    title: osManaged ? 'Aieyra OS · 指挥中心' : 'Aieyra Control',
    width: 1440,
    height: 940,
    minWidth: 720,
    minHeight: 520,
    show: false,
    backgroundColor: '#11151d',
    autoHideMenuBar: true,
    icon: path.join(__dirname, 'assets', 'icon.png'),
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
    },
  });
  const current = window;
  current.on('blur', () => humanHost?.schedule());
  humanReady = false;
  current.webContents.on('did-start-navigation', (_event, _url, inPlace, isMainFrame) => {
    if (isMainFrame && !inPlace) humanReady = false;
  });
  current.webContents.session.setPermissionRequestHandler((_contents, _permission, callback) =>
    callback(false),
  );
  current.webContents.session.setPermissionCheckHandler(() => false);
  current.webContents.setWindowOpenHandler(({ url }) => {
    if (validLoginUrl(url)) loginWindow.open(url, current);
    else openExternal(url);
    return { action: 'deny' };
  });
  current.webContents.on('will-navigate', (event, url) => {
    if (!allowed(url)) {
      event.preventDefault();
      openExternal(url);
    }
  });
  current.webContents.on('will-redirect', (event, url) => {
    if (!allowed(url)) event.preventDefault();
  });
  current.webContents.on('will-attach-webview', (event) => event.preventDefault());
  current.webContents.on('did-fail-load', (_event, code, _description, _url, mainFrame) => {
    if (mainFrame && code !== -3) {
      loadedService = false;
      loadingService = false;
      void current.loadFile(path.join(__dirname, 'waiting.html'));
    }
  });
  current.webContents.on('render-process-gone', () => {
    loadedService = false;
    loadingService = false;
    if (!quitting)
      setTimeout(() => {
        if (!current.isDestroyed()) void current.loadFile(path.join(__dirname, 'waiting.html'));
        supervisor.retry();
      }, 750);
  });
  current.on('close', (event) => {
    if (!quitting && (osManaged || (preferences.background && tray))) {
      event.preventDefault();
      current.hide();
    }
  });
  current.on('closed', () => {
    window = null;
    loadedService = false;
    loadingService = false;
    humanReady = false;
  });
  current.once('ready-to-show', () => {
    if (!hidden) current.show();
  });
  void current.loadFile(path.join(__dirname, 'waiting.html')).then(() => loadService());
}
function loadService() {
  if (
    !window ||
    window.isDestroyed() ||
    loadedService ||
    loadingService ||
    supervisor.snapshot().state !== 'ready'
  )
    return;
  loadingService = true;
  void window
    .loadURL(supervisor.baseUrl + '/')
    .then(() => {
      loadedService = true;
    })
    .catch(() => {
      loadedService = false;
    })
    .finally(() => {
      loadingService = false;
    });
}
function trusted(event) {
  return (
    window &&
    event.sender === window.webContents &&
    event.senderFrame === window.webContents.mainFrame &&
    allowed(event.senderFrame.url)
  );
}
const ownsInstance = app.requestSingleInstanceLock();
if (!ownsInstance) {
  if (ownerRequested)
    process.stdout.write(JSON.stringify({ type: 'control.attached', owned: false }) + '\n', () =>
      app.quit(),
    );
  else app.quit();
} else if (
  ownerRequested &&
  (!osManaged ||
    (ownerStdin && ownerPipeName !== undefined) ||
    (ownerPipeName !== undefined && !validOwnerPipeName(ownerPipeName)))
) {
  process.stdout.write(
    JSON.stringify({ type: 'control.error', code: 'invalid_owner_channel' }) + '\n',
    () => app.exit(1),
  );
} else if (args.includes('--quit')) app.quit();
else {
  if (ownerRequested) {
    const ownerInput = ownerStdin
      ? process.stdin
      : require('node:net').createConnection(ownerPipeName);
    ownerPipe = new OSOwnerPipe({
      input: ownerInput,
      output: ownerStdin ? process.stdout : ownerInput,
      shutdown: () => app.quit(),
      failed: () => {
        ownerFailureCode = 1;
        app.quit();
      },
    });
    ownerPipe.start();
  }
  app.on('second-instance', (_event, argv) => {
    if (argv.includes('--quit')) {
      if (!osManaged) app.quit();
      return;
    }
    if (
      !argv.includes('--hidden') &&
      !argv.includes('--background') &&
      !argv.includes('--os-managed')
    )
      showWindow();
  });
  app.on('window-all-closed', () => {
    if (!osManaged && (!preferences.background || !tray)) app.quit();
  });
  app.on('before-quit', (event) => {
    quitting = true;
    if (cleaned) return;
    event.preventDefault();
    humanHost?.stop();
    loginWindow.close();
    humanFeed?.stop();
    ownerPipe?.stop();
    cleanup ||= supervisor.stop().finally(() => {
      tray?.destroy();
      tray = null;
      cleaned = true;
      if (ownerFailureCode) app.exit(ownerFailureCode);
      else app.quit();
    });
  });
  app.whenReady().then(() => {
    if (quitting) return;
    try {
      tray = new Tray(nativeImage.createFromPath(path.join(__dirname, 'assets', 'icon.png')));
      tray.on('double-click', showWindow);
    } catch {
      preferences.background = false;
    }
    humanHost = new HumanNotificationHost({
      ledger: humans,
      Notification,
      icon: path.join(__dirname, 'assets', 'icon.png'),
      open: openHumanRequest,
      focused: () => Boolean(window && !window.isDestroyed() && window.isFocused()),
      changed: updateMenus,
    });
    humanFeed = new HumanRequestFeed({
      baseUrl: supervisor.baseUrl,
      ledger: humans,
      ready: () => supervisor.snapshot().state === 'ready',
      changed: updateMenus,
    });
    ipcMain.handle('control:status', (event) => (trusted(event) ? status() : null));
    ipcMain.handle('control:prepare-login', (event) =>
      trusted(event) ? loginWindow.prepare(window) : false,
    );
    ipcMain.handle('control:open-login', (event, url) =>
      trusted(event) ? loginWindow.open(url, window) : false,
    );
    ipcMain.handle('control:login-failed', (event) => {
      if (trusted(event)) loginWindow.fail();
    });
    ipcMain.handle('control:finish-login', (event) =>
      trusted(event) ? loginWindow.close(true) : false,
    );
    ipcMain.handle('control:retry', (event) => {
      if (trusted(event)) supervisor.retry();
    });
    ipcMain.handle('control:background', (event, value) =>
      trusted(event) ? setBackground(value) : null,
    );
    ipcMain.handle('control:quit', (event) => {
      if (trusted(event)) {
        if (osManaged) window?.hide();
        else app.quit();
      }
    });
    ipcMain.handle('control:human-status', (event) => (trusted(event) ? humans.snapshot() : null));
    ipcMain.handle('control:human-snooze', (event, minutes) =>
      trusted(event) ? humans.snooze(minutes) : false,
    );
    ipcMain.handle('control:human-enabled', (event, enabled) =>
      trusted(event) ? humans.setEnabled(enabled) : false,
    );
    ipcMain.handle('control:human-open', (event, id) => {
      if (trusted(event)) openHumanRequest(id);
    });
    ipcMain.handle('control:human-ready', (event) => {
      if (trusted(event)) {
        humanReady = true;
        flushHumanOpen();
      }
    });
    ipcMain.handle('control:human-opened', (event, navigationId) => {
      if (trusted(event) && navigationId === pendingHumanOpen?.navigation_id)
        pendingHumanOpen = null;
    });
    let previousServiceState = supervisor.snapshot().state;
    supervisor.on('state', () => {
      const next = supervisor.snapshot().state;
      updateMenus();
      loadService();
      if (next !== 'ready') humans.unavailable('offline');
      else if (previousServiceState !== 'ready') humanFeed.retry();
      previousServiceState = next;
    });
    updateMenus();
    if (!hidden) createWindow();
    supervisor.start();
    humanHost.start();
    humanFeed.start();
    ownerPipe?.ready();
    if (testing)
      globalThis.__controlPlatformTest = {
        supervisor,
        status,
        setBackground,
        showWindow,
        humans,
        humanHost,
        humanFeed,
        humanNavigation: () => ({ ready: humanReady, pending: pendingHumanOpen }),
      };
    // Never leave a user with an invisible application if the OS has no tray.
    if (hidden && !tray && !testing && !osManaged) showWindow();
  });
}
