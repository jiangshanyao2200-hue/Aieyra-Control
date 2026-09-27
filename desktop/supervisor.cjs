'use strict';
const http = require('node:http');
const { spawn } = require('node:child_process');
const { EventEmitter } = require('node:events');
const fs = require('node:fs');

function probe(baseUrl, timeout = 1500) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (result) => {
      if (!settled) {
        settled = true;
        resolve(result);
      }
    };
    const request = http.get(new URL('/api/health', baseUrl), (response) => {
      let data = '';
      let length = 0;
      response.on('data', (chunk) => {
        length += chunk.length;
        if (length > 8192) {
          finish({ kind: 'foreign' });
          response.destroy();
        } else data += chunk;
      });
      response.on('end', () => {
        try {
          const body = JSON.parse(data);
          finish(
            response.statusCode === 200 && body.service === 'aieyra-control'
              ? { kind: 'healthy', version: body.version ?? null }
              : { kind: 'foreign' },
          );
        } catch {
          finish({ kind: 'foreign' });
        }
      });
      response.on('error', () => finish({ kind: 'unavailable' }));
    });
    request.on('error', (error) =>
      finish({ kind: error.code === 'ECONNREFUSED' ? 'absent' : 'unavailable' }),
    );
    request.setTimeout(timeout, () => {
      finish({ kind: 'unavailable' });
      request.destroy();
    });
  });
}

// Own only the ChildProcess created here. Never discover/kill processes by port or name.
class ServiceSupervisor extends EventEmitter {
  constructor({
    python = 'python',
    script,
    cwd,
    port = 17910,
    dataDir,
    config,
    interval = 2000,
    startupTimeout = 15000,
    restartBase = 1000,
    restartMax = 30000,
  }) {
    super();
    if (!Number.isInteger(port) || port < 1024 || port > 65535) throw Error('Invalid service port');
    Object.assign(this, {
      python,
      script,
      cwd,
      port,
      dataDir,
      config,
      interval,
      startupTimeout,
      restartBase,
      restartMax,
    });
    this.baseUrl = `http://127.0.0.1:${port}`;
    this.child = null;
    this.timer = null;
    this.pending = null;
    this.stopped = true;
    this.externalOnly = false;
    this.failures = 0;
    this.nextStart = 0;
    this.startedAt = 0;
    this.healthyAt = 0;
    this.unhealthySince = 0;
    this.state = {
      state: 'stopped',
      ownership: 'none',
      url: this.baseUrl,
      restarts: 0,
      error: null,
    };
  }
  snapshot() {
    return { ...this.state, ownedPid: this.child?.pid || null };
  }
  publish(state) {
    this.state = { ...this.state, ...state };
    this.emit('state', this.snapshot());
  }
  start() {
    if (!this.stopped) return;
    this.stopped = false;
    void this.tick();
  }
  retry() {
    this.nextStart = 0;
    if (!this.stopped) {
      clearTimeout(this.timer);
      void this.tick();
    }
  }
  async tick() {
    if (this.stopped || this.pending) return;
    this.pending = this.check();
    try {
      await this.pending;
    } catch {
      this.publish({ state: 'offline', error: '服务检查失败，正在重连。' });
    } finally {
      this.pending = null;
      if (!this.stopped) this.timer = setTimeout(() => void this.tick(), this.interval);
    }
  }
  backoff() {
    this.failures = Math.min(this.failures + 1, 16);
    this.nextStart =
      Date.now() + Math.min(this.restartMax, this.restartBase * 2 ** (this.failures - 1));
  }
  async check() {
    const childAtProbe = this.child;
    const result = await probe(this.baseUrl);
    if (this.stopped || this.child !== childAtProbe) return;
    if (result.kind === 'healthy') {
      this.unhealthySince = 0;
      if (!this.child) this.externalOnly = true;
      if (!this.healthyAt) this.healthyAt = Date.now();
      if (Date.now() - this.healthyAt > 30000) this.failures = 0;
      this.publish({
        state: 'ready',
        ownership: this.child ? 'owned' : 'external',
        error: null,
        version: result.version,
      });
      return;
    }
    this.healthyAt = 0;
    if (!this.unhealthySince) this.unhealthySince = Date.now();
    if (result.kind === 'foreign') {
      this.publish({
        state: 'conflict',
        error: '端口被其他服务占用，请更换端口；现有服务已保留。',
      });
      return;
    }
    if (this.externalOnly && !this.child) {
      this.publish({
        state: 'offline',
        ownership: 'external',
        error: '已连接的外部服务暂时离线，正在等待它恢复。',
      });
      return;
    }
    if (this.child) {
      if (Date.now() - Math.max(this.startedAt, this.unhealthySince) > this.startupTimeout) {
        this.publish({ state: 'reconnecting', error: '本应用启动的服务未响应，正在恢复。' });
        await this.stopOwned();
        this.backoff();
      } else this.publish({ state: 'starting', error: null });
      return;
    }
    // Only a refused connection is evidence that no process is listening.
    if (result.kind !== 'absent') {
      this.publish({ state: 'offline', ownership: 'none', error: '现有连接暂未响应，正在重连。' });
      return;
    }
    if (Date.now() < this.nextStart) {
      this.publish({
        state: 'reconnecting',
        ownership: 'none',
        error: '服务已退出，正在等待重启。',
      });
      return;
    }
    if (!fs.existsSync(this.script)) {
      this.publish({
        state: 'unavailable',
        ownership: 'none',
        error: '本地服务文件尚未就绪，正在等待。',
      });
      return;
    }
    const args = [this.script, '--port', String(this.port)];
    if (this.dataDir) args.push('--data-dir', this.dataDir);
    if (this.config) args.push('--config', this.config);
    const env = { ...process.env, PYTHONUTF8: '1', PYTHONUNBUFFERED: '1' };
    delete env.__PYVENV_LAUNCHER__;
    delete env.PYTHONHOME;
    delete env.PYTHONPATH;
    this.startedAt = Date.now();
    const child = spawn(this.python, args, {
      cwd: this.cwd,
      env,
      windowsHide: true,
      stdio: process.env.AIEYRA_CONTROL_PACKAGE_TEST === '1' ? 'inherit' : 'ignore',
      shell: false,
    });
    this.child = child;
    this.publish({
      state: 'starting',
      ownership: 'owned',
      error: null,
      restarts: this.state.restarts + 1,
    });
    child.once('error', () => {
      if (this.child === child) {
        this.child = null;
        this.backoff();
        this.publish({
          state: 'unavailable',
          ownership: 'none',
          error: '无法启动 Python，请检查启动器的 PythonPath。',
        });
      }
    });
    child.once('exit', (code, signal) => {
      if (this.child === child) {
        this.child = null;
        this.backoff();
        this.publish({
          state: this.stopped ? 'stopped' : 'reconnecting',
          ownership: 'none',
          lastExitCode: code,
          lastExitSignal: signal,
        });
      }
    });
  }
  async stopOwned() {
    const child = this.child;
    if (!child) return;
    this.child = null;
    if (child.exitCode !== null || child.signalCode !== null) return;
    await new Promise((resolve) => {
      const finish = () => {
        clearTimeout(timer);
        resolve();
      };
      const timer = setTimeout(() => {
        if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL');
        resolve();
      }, 2500);
      child.once('exit', finish);
      child.kill('SIGTERM');
    });
  }
  async stop() {
    this.stopped = true;
    clearTimeout(this.timer);
    if (this.pending) await this.pending;
    await this.stopOwned();
    this.publish({ state: 'stopped', ownership: 'none' });
  }
}
module.exports = { ServiceSupervisor, probe };
