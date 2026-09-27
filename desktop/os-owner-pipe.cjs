'use strict';
const { StringDecoder } = require('node:string_decoder');
const PIPE_PREFIX = String.raw`\\.\pipe\aieyra-control-owner-`;
function validOwnerPipeName(value) {
  return (
    typeof value === 'string' &&
    value.startsWith(PIPE_PREFIX) &&
    /^[a-f0-9]{32}$/.test(value.slice(PIPE_PREFIX.length))
  );
}

// Only the newly-created, single-instance owner may use this inherited pipe.
// No PID lookup, arbitrary command, remote callback, or second-instance adoption.
class OSOwnerPipe {
  constructor({ input, output, pid = process.pid, timeout = 10000, shutdown, failed }) {
    Object.assign(this, { input, output, pid, timeout, shutdown, failed });
    this.ownerId = null;
    this.closed = false;
    this.isReady = false;
    this.buffer = '';
    this.decoder = new StringDecoder('utf8');
    this.onData = (chunk) =>
      this.receive(Buffer.isBuffer(chunk) ? this.decoder.write(chunk) : String(chunk));
    this.onEnd = () =>
      this.ownerId ? this.closeOwner('owner_pipe_closed') : this.fail('owner_handshake_missing');
    this.onError = () => this.fail('owner_pipe_error');
  }
  start() {
    this.input.on('data', this.onData);
    this.input.once('end', this.onEnd);
    this.input.once('error', this.onError);
    this.output.on('error', this.onError);
    this.timer = setTimeout(() => this.fail('owner_handshake_timeout'), this.timeout);
    this.timer.unref?.();
  }
  write(value) {
    try {
      this.output.write(JSON.stringify(value) + '\n');
    } catch {
      this.fail('owner_pipe_error', false);
    }
  }
  receive(text) {
    if (this.closed) return;
    this.buffer += text;
    if (Buffer.byteLength(this.buffer, 'utf8') > 16384) {
      this.fail('owner_message_too_large');
      return;
    }
    while (!this.closed && this.buffer.includes('\n')) {
      const at = this.buffer.indexOf('\n'),
        line = this.buffer.slice(0, at).trim();
      this.buffer = this.buffer.slice(at + 1);
      if (!line) continue;
      let message;
      try {
        message = JSON.parse(line);
      } catch {
        this.fail('owner_message_invalid');
        return;
      }
      if (!message || Array.isArray(message) || typeof message !== 'object') {
        this.fail('owner_message_invalid');
        return;
      }
      if (!this.ownerId) {
        if (
          message.type !== 'owner.hello' ||
          typeof message.owner_id !== 'string' ||
          !/^[a-f0-9]{32}$/.test(message.owner_id)
        ) {
          this.fail('owner_handshake_invalid');
          return;
        }
        this.ownerId = message.owner_id;
        clearTimeout(this.timer);
        this.announce();
      } else if (message.type === 'owner.shutdown' && message.owner_id === this.ownerId)
        this.closeOwner('owner_shutdown');
      else
        this.write({
          type: 'control.ignored',
          reason:
            message.owner_id === this.ownerId ? 'unsupported_owner_message' : 'owner_mismatch',
        });
    }
  }
  ready() {
    this.isReady = true;
    this.announce();
  }
  announce() {
    if (!this.closed && this.ownerId && this.isReady && !this.announced) {
      this.announced = true;
      this.write({ type: 'control.ready', owner_id: this.ownerId, pid: this.pid, owned: true });
    }
  }
  closeOwner(reason) {
    if (this.closed) return;
    this.stop();
    this.shutdown(reason);
  }
  fail(code, report = true) {
    if (this.closed) return;
    // Mark closed before reporting, so a broken output cannot recurse.
    this.closed = true;
    clearTimeout(this.timer);
    if (report) {
      try {
        this.output.write(JSON.stringify({ type: 'control.error', code }) + '\n');
      } catch {}
    }
    this.removeListeners();
    this.failed(code);
  }
  removeListeners() {
    this.input.off('data', this.onData);
    this.input.off('end', this.onEnd);
    this.input.off('error', this.onError);
    this.output.off('error', this.onError);
    this.input.pause?.();
  }
  stop() {
    this.closed = true;
    clearTimeout(this.timer);
    this.removeListeners();
  }
}
module.exports = { OSOwnerPipe, validOwnerPipeName, PIPE_PREFIX };
