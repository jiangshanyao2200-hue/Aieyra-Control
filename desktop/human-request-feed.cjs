'use strict';
const http = require('node:http');
const { timestamp } = require('./human-notifications.cjs');

function readJSON(baseUrl, { signal, timeout = 5000, maxBytes = 1024 * 1024 } = {}) {
  const base = new URL(baseUrl);
  if (
    base.protocol !== 'http:' ||
    base.hostname !== '127.0.0.1' ||
    base.username ||
    base.password ||
    base.pathname !== '/' ||
    base.search ||
    base.hash
  ) {
    return Promise.reject(
      Object.assign(new Error('invalid_local_origin'), { code: 'INVALID_ORIGIN' }),
    );
  }
  return new Promise((resolve, reject) => {
    let finished = false,
      timer;
    const finish = (error, result) => {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      error ? reject(error) : resolve(result);
    };
    const request = http.get(
      new URL('/api/human-requests', base),
      { signal, headers: { Accept: 'application/json' } },
      (response) => {
        const chunks = [];
        let length = 0;
        response.on('data', (chunk) => {
          length += chunk.length;
          if (length > maxBytes) {
            const error = Object.assign(new Error('response_too_large'), {
              code: 'RESPONSE_LIMIT',
            });
            finish(error);
            request.destroy();
            return;
          }
          chunks.push(chunk);
        });
        response.on('error', (error) => finish(error));
        response.on('end', () => {
          if (response.statusCode !== 200)
            return finish(null, { status: response.statusCode, body: null });
          if (!/^application\/json(?:;|$)/i.test(response.headers['content-type'] || '')) {
            return finish(
              Object.assign(new Error('invalid_content_type'), { code: 'INVALID_JSON' }),
            );
          }
          try {
            finish(null, {
              status: response.statusCode,
              body: JSON.parse(Buffer.concat(chunks).toString('utf8')),
            });
          } catch {
            finish(Object.assign(new Error('invalid_json'), { code: 'INVALID_JSON' }));
          }
        });
      },
    );
    request.on('error', (error) => finish(error));
    timer = setTimeout(() => {
      const error = Object.assign(new Error('request_timeout'), { code: 'READ_TIMEOUT' });
      finish(error);
      request.destroy();
    }, timeout);
    timer.unref?.();
  });
}

class HumanRequestFeed {
  constructor({
    baseUrl,
    ledger,
    ready = () => true,
    interval = 15000,
    inactiveInterval = 60000,
    read = readJSON,
    changed = () => {},
  }) {
    Object.assign(this, { baseUrl, ledger, ready, interval, inactiveInterval, read, changed });
    this.stopped = true;
    this.timer = null;
    this.pending = null;
    this.controller = null;
    this.epoch = 0;
    this.state = {
      state: 'unavailable',
      checked_at: null,
      http_status: null,
      authority: null,
      schema_version: null,
    };
  }
  snapshot() {
    return { ...this.state, running: !this.stopped, pending: Boolean(this.pending) };
  }
  publish(state) {
    const changed = Object.keys(state).some(
      (key) => key !== 'checked_at' && state[key] !== this.state[key],
    );
    this.state = { ...this.state, ...state };
    if (changed) this.changed();
  }
  start() {
    if (!this.stopped) return;
    this.stopped = false;
    this.epoch++;
    void this.tick();
  }
  stop() {
    this.stopped = true;
    this.epoch++;
    clearTimeout(this.timer);
    this.timer = null;
    this.controller?.abort();
    this.ledger.unavailable('offline');
  }
  retry() {
    if (!this.stopped) {
      clearTimeout(this.timer);
      this.timer = null;
      void this.tick();
    }
  }
  async tick() {
    if (this.stopped || this.pending) return;
    clearTimeout(this.timer);
    this.timer = null;
    const epoch = this.epoch;
    this.pending = this.check(epoch);
    try {
      await this.pending;
    } finally {
      this.pending = null;
      if (!this.stopped) {
        const delay = ['human_contract_not_connected', 'waiting_service'].includes(this.state.state)
          ? Math.max(this.interval, this.inactiveInterval)
          : this.interval;
        this.timer = setTimeout(() => void this.tick(), delay);
        this.timer.unref?.();
      }
    }
  }
  async check(epoch) {
    if (!this.ready()) {
      this.ledger.unavailable('offline');
      this.publish({ state: 'waiting_service' });
      return;
    }
    const controller = new AbortController();
    this.controller = controller;
    try {
      const result = await this.read(this.baseUrl, { signal: controller.signal });
      if (this.stopped || epoch !== this.epoch || !this.ready()) return;
      this.publish({ checked_at: new Date().toISOString(), http_status: result.status });
      if (result.status !== 200) {
        const reason =
          result.status === 404
            ? 'endpoint_unavailable'
            : [401, 403].includes(result.status)
              ? 'access_unavailable'
              : 'offline';
        this.ledger.unavailable(reason);
        this.publish({ state: reason });
        return;
      }
      const body = result.body;
      if (
        !body ||
        body.schema_version !== 1 ||
        !Array.isArray(body.items) ||
        typeof body.available !== 'boolean' ||
        typeof body.stale !== 'boolean' ||
        typeof body.source !== 'string' ||
        body.source.length > 100 ||
        (body.observed_at !== null && timestamp(body.observed_at) === null)
      )
        throw Object.assign(new Error('invalid_contract'), { code: 'INVALID_CONTRACT' });
      this.publish({ authority: body.source, schema_version: 1 });
      if (!body.available) {
        // Do not replace the last known list with a false empty list while unavailable.
        const reason =
          body.reason === 'human_contract_not_connected'
            ? 'human_contract_not_connected'
            : 'unavailable';
        this.ledger.unavailable(reason);
        this.publish({ state: reason });
        return;
      }
      const accepted = this.ledger.update({
        items: body.items,
        observed_at: body.observed_at,
        stale: body.stale,
      });
      this.publish({ state: accepted ? this.ledger.snapshot().source : 'invalid_contract' });
    } catch (error) {
      if (this.stopped || epoch !== this.epoch) return;
      const reason = [
        'INVALID_CONTRACT',
        'INVALID_JSON',
        'RESPONSE_LIMIT',
        'INVALID_ORIGIN',
      ].includes(error.code)
        ? 'invalid_contract'
        : 'offline';
      this.ledger.unavailable(reason);
      this.publish({ state: reason, checked_at: new Date().toISOString(), http_status: null });
    } finally {
      if (this.controller === controller) this.controller = null;
    }
  }
}
module.exports = { HumanRequestFeed, readJSON };
