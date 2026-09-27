'use strict';
// Desktop attention is not task execution. This ledger never submits decisions.
const fs = require('node:fs');
const path = require('node:path');
const { EventEmitter } = require('node:events');
const { createHash } = require('node:crypto');
const CATEGORIES = Object.freeze({
  information: '补充信息',
  direction: '方向选择',
  authorization: '具体授权',
  environment: '环境操作',
});
const STATES = new Set([
  'waiting_user',
  'decision_recorded',
  'delivered_to_agent',
  'resuming',
  'resolved',
  'failed',
  'unknown',
  'cancelled',
  'expired',
]);
const keyOf = (item) => `${item.id}@${item.version}`;
function ordered(value) {
  if (Array.isArray(value)) return value.map(ordered);
  if (value && typeof value === 'object')
    return Object.fromEntries(
      Object.keys(value)
        .sort()
        .filter((key) => value[key] !== undefined)
        .map((key) => [key, ordered(value[key])]),
    );
  return value;
}
function attentionHash(item) {
  const origin = item.origin;
  // Older/minimal feeds do not prove the question is unchanged. Keep their
  // conservative per-version behavior instead of suppressing unseen content.
  if (
    typeof item.question !== 'string' ||
    !item.question.trim() ||
    !Array.isArray(item.options) ||
    !origin ||
    typeof origin.runtime_ref !== 'string' ||
    typeof origin.native_task_id !== 'string' ||
    !Number.isSafeInteger(origin.task_generation) ||
    typeof origin.native_request_id !== 'string'
  )
    return null;
  const content = {
    category: item.category,
    question: item.question,
    options: item.options,
    input_schema: item.input_schema ?? null,
    recommendation: item.recommendation ?? null,
    facts: item.facts ?? null,
    impact: item.impact ?? null,
    resume_summary: item.resume_summary ?? null,
    origin: Object.fromEntries(
      ['runtime_ref', 'native_task_id', 'task_generation', 'native_request_id'].map((key) => [
        key,
        origin[key],
      ]),
    ),
  };
  return createHash('sha256')
    .update(JSON.stringify(ordered(content)))
    .digest('hex');
}
function timestamp(value) {
  if (typeof value !== 'string' || !/(Z|[+-]\d\d:\d\d)$/.test(value)) return null;
  const result = Date.parse(value);
  return Number.isFinite(result) ? result : null;
}
function validItem(item) {
  return (
    item &&
    typeof item.id === 'string' &&
    /^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,199}$/.test(item.id) &&
    Number.isSafeInteger(item.version) &&
    item.version > 0 &&
    Object.hasOwn(CATEGORIES, item.category) &&
    STATES.has(item.state) &&
    (item.expires_at == null || timestamp(item.expires_at) !== null)
  );
}
function writeAtomic(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const temporary = `${file}.${process.pid}.tmp`;
  let fd;
  try {
    fd = fs.openSync(temporary, 'w', 0o600);
    fs.writeFileSync(fd, JSON.stringify(value, null, 2));
    fs.fsyncSync(fd);
    fs.closeSync(fd);
    fd = undefined;
    fs.renameSync(temporary, file);
  } finally {
    if (fd !== undefined) fs.closeSync(fd);
    try {
      fs.unlinkSync(temporary);
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
    }
  }
}
class HumanNotificationLedger extends EventEmitter {
  constructor({ file, now = () => Date.now(), maxAge = 120000, cooldown = 30000 } = {}) {
    super();
    this.file = file;
    this.now = now;
    this.maxAge = maxAge;
    this.cooldown = cooldown;
    this.items = [];
    this.observedAt = null;
    this.feed = 'unavailable';
    this.error = null;
    this.data = {
      schema_version: 1,
      enabled: true,
      snoozed_until: 0,
      last_presented_at: 0,
      seen: {},
    };
    try {
      if (fs.statSync(file).size > 4 * 1024 * 1024) throw Error('ledger_too_large');
      const saved = JSON.parse(fs.readFileSync(file, 'utf8'));
      if (
        saved.schema_version !== 1 ||
        typeof saved.enabled !== 'boolean' ||
        !Number.isFinite(saved.snoozed_until) ||
        !Number.isFinite(saved.last_presented_at) ||
        !saved.seen ||
        typeof saved.seen !== 'object' ||
        Array.isArray(saved.seen) ||
        Object.values(saved.seen).some(
          (entry) =>
            !entry ||
            !Number.isFinite(entry.at) ||
            !['reserved', 'shown', 'failed'].includes(entry.state) ||
            (entry.attention_hash !== undefined && !/^[a-f0-9]{64}$/.test(entry.attention_hash)),
        )
      )
        throw Error('invalid_ledger');
      this.data = saved;
    } catch (error) {
      if (error.code !== 'ENOENT') this.error = 'ledger_unreadable';
    }
  }
  persist(next) {
    if (this.error) return false;
    try {
      writeAtomic(this.file, next);
      this.data = next;
      return true;
    } catch {
      this.error = 'ledger_write_failed';
      this.emit('change');
      return false;
    }
  }
  update({ items, observed_at, stale = false } = {}) {
    const at = timestamp(observed_at),
      now = this.now();
    if (
      !Array.isArray(items) ||
      items.length > 10000 ||
      items.some((item) => !validItem(item)) ||
      new Set(items.map((item) => item.id)).size !== items.length ||
      at === null ||
      at > now + 60000
    ) {
      this.unavailable('invalid_contract');
      return false;
    }
    // A delayed poll must not resurrect an older waiting version after a decision.
    if (this.observedAt !== null && at < this.observedAt) return false;
    const existing = new Map(this.items.map((item) => [item.id, item.version]));
    if (items.some((item) => item.version < (existing.get(item.id) || 0))) {
      this.unavailable('regressed_version');
      return false;
    }
    this.items = structuredClone(items);
    this.observedAt = at;
    this.feed = stale || now - at > this.maxAge ? 'stale' : 'ready';
    this.emit('change');
    return true;
  }
  unavailable(reason = 'offline') {
    if (this.feed === reason) return;
    this.feed = reason;
    this.emit('change');
  }
  fresh() {
    return (
      this.feed === 'ready' &&
      this.observedAt !== null &&
      this.now() - this.observedAt <= this.maxAge
    );
  }
  waiting() {
    const now = this.now();
    return this.items.filter(
      (item) =>
        item.state === 'waiting_user' &&
        (item.expires_at == null || timestamp(item.expires_at) > now),
    );
  }
  snapshot() {
    const pending = this.waiting();
    return {
      enabled: this.data.enabled,
      snoozed_until: this.data.snoozed_until,
      persistence_error: this.error,
      source: this.fresh() ? 'ready' : this.feed === 'ready' ? 'stale' : this.feed,
      observed_at: this.observedAt === null ? null : new Date(this.observedAt).toISOString(),
      pending_count: pending.length,
      current: this.fresh(),
      categories: Object.fromEntries(
        Object.keys(CATEGORIES).map((category) => [
          category,
          pending.filter((item) => item.category === category).length,
        ]),
      ),
      items: this.items.map(({ id, version, category, state, expires_at }) => ({
        id,
        version,
        category,
        state,
        expires_at: expires_at ?? null,
      })),
    };
  }
  setEnabled(enabled) {
    if (typeof enabled !== 'boolean') return false;
    const ok = this.persist({ ...this.data, enabled });
    this.emit('change');
    return ok;
  }
  snooze(minutes) {
    if (![0, 15, 60].includes(minutes)) return false;
    const ok = this.persist({
      ...this.data,
      snoozed_until: minutes ? this.now() + minutes * 60000 : 0,
    });
    this.emit('change');
    return ok;
  }
  reserve() {
    const now = this.now();
    if (
      this.error ||
      !this.fresh() ||
      !this.data.enabled ||
      this.data.snoozed_until > now ||
      (this.data.last_presented_at > 0 && now - this.data.last_presented_at < this.cooldown)
    )
      return null;
    const waiting = this.waiting(),
      hashes = new Map(waiting.map((item) => [item.id, attentionHash(item)]));
    // Upgrade an old entry only when the exact already-seen version is still
    // observable. Never infer the content of an unobserved historical version.
    let seen = { ...this.data.seen },
      migrated = false;
    for (const item of waiting) {
      const key = keyOf(item),
        hash = hashes.get(item.id);
      if (hash && seen[key] && !seen[key].attention_hash) {
        seen[key] = { ...seen[key], attention_hash: hash };
        migrated = true;
      }
    }
    if (migrated && !this.persist({ ...this.data, seen })) return null;
    const known = new Set(
      Object.entries(seen)
        .filter(([, entry]) => entry.attention_hash)
        .map(([key, entry]) => `${key.slice(0, key.lastIndexOf('@'))}@${entry.attention_hash}`),
    );
    const items = waiting.filter(
      (item) =>
        !Object.hasOwn(seen, keyOf(item)) &&
        !(hashes.get(item.id) && known.has(`${item.id}@${hashes.get(item.id)}`)),
    );
    if (!items.length) return null;
    // Do not evict old identities and risk replay. The tray stays usable if storage fills.
    if (Object.keys(this.data.seen).length + items.length > 20000) {
      this.error = 'ledger_capacity';
      this.emit('change');
      return null;
    }
    for (const item of items)
      seen[keyOf(item)] = {
        at: now,
        state: 'reserved',
        ...(hashes.get(item.id) ? { attention_hash: hashes.get(item.id) } : {}),
      };
    if (!this.persist({ ...this.data, seen, last_presented_at: now })) return null;
    return {
      ids: items.map((item) => ({ id: item.id, version: item.version })),
      total: this.waiting().length,
      categories: [...new Set(items.map((item) => CATEGORIES[item.category]))],
    };
  }
  nextAttentionDelay() {
    if (this.error || !this.fresh() || !this.data.enabled) return null;
    const seen = this.data.seen;
    // Migrate an observed legacy version before a freshness-only version arrives.
    const migrate = this.waiting().some(
      (item) => seen[keyOf(item)] && !seen[keyOf(item)].attention_hash && attentionHash(item),
    );
    const known = new Set(
      Object.entries(seen)
        .filter(([, entry]) => entry.attention_hash)
        .map(([key, entry]) => `${key.slice(0, key.lastIndexOf('@'))}@${entry.attention_hash}`),
    );
    const unseen = this.waiting().some(
      (item) =>
        !Object.hasOwn(seen, keyOf(item)) &&
        !(attentionHash(item) && known.has(`${item.id}@${attentionHash(item)}`)),
    );
    if (!unseen && !migrate) return null;
    return Math.max(
      0,
      this.data.snoozed_until - this.now(),
      this.data.last_presented_at ? this.data.last_presented_at + this.cooldown - this.now() : 0,
    );
  }
  presentation(batch, state) {
    if (!batch || !['shown', 'failed'].includes(state)) return false;
    const seen = { ...this.data.seen };
    for (const item of batch.ids) {
      const key = keyOf(item);
      if (Object.hasOwn(seen, key)) seen[key] = { ...seen[key], state };
    }
    return this.persist({ ...this.data, seen });
  }
  reference(id = null) {
    // Opening a stale item is safe only as a view. It never implies permission.
    const item = id ? this.items.find((item) => item.id === id) : this.waiting()[0];
    return item ? { id: item.id, version: item.version } : null;
  }
}
module.exports = { HumanNotificationLedger, CATEGORIES, timestamp, validItem };
