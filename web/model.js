export const text = value => typeof value === 'string' ? value : typeof value === 'number' ? String(value) : '';
export const esc = value => text(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export const list = value => Array.isArray(value) ? value.filter(v => v && typeof v === 'object' && !Array.isArray(v)) : [];
export function normalize(raw) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw new Error('invalid_snapshot');
  const result = {...raw};
  for (const key of ['projects','agents','tasks','messages','resources','deliveries']) result[key] = list(raw[key]);
  for (const key of ['connection','governance','capabilities']) result[key] = raw[key] && typeof raw[key] === 'object' ? raw[key] : {};
  result.known = Object.fromEntries(['projects','agents','tasks','messages','resources','deliveries'].map(k => [k, Array.isArray(raw[k])]));
  result.messages.sort((a,b) => (Number.isFinite(a.seq)&&Number.isFinite(b.seq)) ? a.seq-b.seq : (timestamp(a.created)||0)-(timestamp(b.created)||0));
  return result;
}
export function timestamp(value) {
  if (value === null || value === undefined || value === '') return NaN;
  const date = typeof value === 'number' ? new Date(value < 1e12 ? value * 1000 : value) : new Date(value);
  return date.getTime();
}
export function safeUrl(value) {
  try {
    const url = new URL(value);
    if (!['https:','http:'].includes(url.protocol) || url.username || url.password) return null;
    if ([...url.searchParams.keys()].some(k => /token|secret|password|key|credential|signature/i.test(k))) return null;
    return url.href;
  } catch { return null; }
}
export function matches(item, query) {
  const haystack = ['id','name','title','summary','body','location','root','scope','evidence','sender_name','project','owner']
    .map(k => text(item[k])).join(' ').toLocaleLowerCase();
  return query.toLocaleLowerCase().trim().split(/\s+/).every(q => haystack.includes(q));
}
export function runtimeStale(agent) {
  const observed = timestamp(agent.runtime?.last_activity_at || agent.runtime?.observed_at);
  return agent.runtime?.stale === true || !Number.isFinite(observed) || Date.now() - observed > 180000 || observed - Date.now() > 60000;
}
export function runtimeState(agent, reachable = true) {
  if (!reachable || agent.runtime?.bound === false || runtimeStale(agent)) return 'unknown';
  const state = agent.runtime?.state;
  return ['running','idle','busy','stopped','unknown'].includes(state) ? state : 'unknown';
}
export function isBound(agent) {
  if (!agent) return false;
  return agent.runtime?.bound === true || (agent.runtime?.bound === undefined && typeof agent.runtime?.thread_id === 'string' && agent.runtime.thread_id.length > 0);
}
// A saved binding is not presence; only current runtime observations count.
export function runtimePresent(agent, reachable = true) {
  return isBound(agent) && ['running','busy','idle'].includes(runtimeState(agent, reachable));
}
export function runtimeDispatchReady(agent, reachable = true) {
  return agent?.runtime?.bound === true && typeof agent.runtime.thread_id === 'string' && agent.runtime.thread_id.length > 0 && runtimePresent(agent, reachable);
}
// A recognized ledger state confirms receipt, never execution or acceptance.
export const deliveryStates = new Set(['pending','sending','stored','queue_submitting','queued','received','running','completed','interrupted','failed','unknown']);
// Keep the center's original status intact; project the validity of its claim.
export function taskRecordState(task, reachable = true) {
  if (task?.status !== 'doing') return text(task?.status) || 'unknown';
  const lease = timestamp(task.lease_until);
  if (Number.isFinite(lease) && lease > 0 && lease <= Date.now()) return 'expired';
  if (!reachable || !text(task.owner) || !Number.isFinite(lease) || lease <= 0) return 'unconfirmed';
  return 'doing';
}
export function taskRecordLabel(task, language = 'zh', reachable = true) {
  const labels = {doing:['已接单（登记）','Claimed (recorded)'],expired:['接单已过期','Claim expired'],unconfirmed:['执行待核对','Execution unconfirmed'],planned:['待安排','Planned'],blocked:['有阻塞','Blocked'],delivered:['已交付','Delivered'],accepted:['已验收','Accepted'],cancelled:['已取消','Cancelled'],superseded:['已替代','Superseded'],unknown:['未知','Unknown']};
  return (labels[taskRecordState(task, reachable)] || labels.unknown)[language === 'en' ? 1 : 0];
}
export function workstationKey(agent) { return agent.workstation_id || agent.runtime?.workstation_id || agent.runtime?.thread_id || agent.id; }
export function runningCount(agents, reachable = true) { return new Set(agents.filter(a=>['running','busy'].includes(runtimeState(a, reachable))).map(workstationKey)).size; }
export const pendingStates = new Set(['pending','sending','stored','queue_submitting','queued','received','unknown']);
export function mergeDeliveries(snapshot, local) {
  const records = new Map(local.filter(d => d.id).map(d => [d.id, d]));
  for (const delivery of snapshot) if (delivery.id) records.set(delivery.id, delivery);
  return [...records.values()].sort((a,b) => (timestamp(b.created_at) || 0) - (timestamp(a.created_at) || 0));
}

const watchdogStateValues = new Set(['running','idle','busy','stopped','blocked','unknown','waiting_quota_evidence','observing','not_configured']);
const triggerStateValues = new Set(['submitting','queued','received','started','completed','unknown','stopped','blocked','failed']);
function finitePercent(value) { return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 100 ? value : null; }
function watchdogSeat(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const seatId = text(value.seat_id || value.id);
  const sessionId = text(value.session_id || value.source_session || value.thread_id);
  const actorId = text(value.actor_id);
  const dedupeKey = seatId || sessionId || actorId || text(value.name);
  if (!dedupeKey) return null;
  return {
    seat_id: seatId || null, session_id: sessionId || null, actor_id: actorId || null,
    name: text(value.name || value.label || seatId || sessionId || actorId || '—'),
    state: watchdogStateValues.has(value.state) ? value.state : 'unknown',
    bound: value.bound === true ? true : value.bound === false ? false : null,
    source: text(value.source) || null,
    observed_at: value.observed_at || value.last_activity_at || null,
    last_activity_at: value.last_activity_at || null, task_id: text(value.task_id) || null,
    task_title: text(value.task_title || value.task) || null,
    last_error: text(value.last_error) || null, last_event_type: text(value.last_event_type) || null,
    turn_id: text(value.turn_id) || null, silence_seconds: typeof value.silence_seconds === 'number' ? value.silence_seconds : null,
    dedupeKey,
  };
}
function watchdogTrigger(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const id = text(value.id || value.trigger_id || value.request_id || (value.state ? 'active-trigger' : ''));
  if (!id) return null;
  return {
    id, state: triggerStateValues.has(value.state) ? value.state : 'unknown',
    target: text(value.target || value.target_seat || 'leader') || 'leader',
    reason: text(value.reason || value.summary) || null,
    created_at: value.created_at || value.submitted_at || null,
    updated_at: value.updated_at || value.observed_at || null,
    source: text(value.source) || null,
  };
}
export function normalizeWatchdog(raw) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw new Error('invalid_watchdog');
  const seen = new Set(), seats = [];
  for (const value of list(raw.seats || raw.sessions || raw.workstations)) {
    const seat = watchdogSeat(value);
    if (!seat || seen.has(seat.dedupeKey)) continue;
    seen.add(seat.dedupeKey); seats.push(seat);
  }
  const quotaRaw = raw.quota && typeof raw.quota === 'object' && !Array.isArray(raw.quota) ? raw.quota : {};
  const remaining = finitePercent(quotaRaw.remaining_percent ?? quotaRaw.remainingPercent);
  const quotaScope = text(quotaRaw.scope) || null;
  const quotaVerified = quotaRaw.route_verified === true && quotaScope === 'sub2_5x_team';
  const quotaState = quotaVerified && quotaRaw.exhausted === true ? 'exhausted' : quotaVerified && remaining !== null ? 'latest' : 'unknown';
  const triggerValues = list(raw.triggers || raw.recent_triggers);
  if (raw.active_trigger && typeof raw.active_trigger === 'object' && !Array.isArray(raw.active_trigger)) triggerValues.unshift(raw.active_trigger);
  const triggers = triggerValues.map(watchdogTrigger).filter(Boolean)
    .sort((a,b) => (timestamp(b.updated_at || b.created_at) || 0) - (timestamp(a.updated_at || a.created_at) || 0));
  const state = watchdogStateValues.has(raw.state) ? raw.state : raw.enabled === false ? 'stopped' : 'unknown';
  const triggerCount = Number.isInteger(raw.trigger_count) && raw.trigger_count >= 0 ? raw.trigger_count : triggers.length;
  return {
    available: true, schema_version: text(raw.schema_version) || null, observed_at: raw.observed_at || null,
    source: text(raw.source || raw.source_ref) || null, state, reason: text(raw.reason) || null,
    enabled: typeof raw.enabled === 'boolean' ? raw.enabled : null,
    interval_seconds: typeof raw.interval_seconds === 'number' && raw.interval_seconds >= 0 ? raw.interval_seconds : null,
    last_tick_at: raw.last_tick_at || null, next_check_at: raw.next_check_at || null,
    single_instance: raw.single_instance === true, lock_state: text(raw.lock_state) || null,
    stop_requested: raw.stop_requested === true, remaining_work: typeof raw.remaining_work === 'boolean' ? raw.remaining_work : null,
    seats, duplicate_count: Math.max(0, list(raw.seats || raw.sessions || raw.workstations).length - seats.length),
    quota: {state: quotaState, source: text(quotaRaw.source) || null, scope: quotaScope,
      observed_at: quotaRaw.observed_at || null, remaining_percent: quotaState === 'latest' ? remaining : null,
      exhausted: quotaState === 'exhausted', reason: text(quotaRaw.reason) || null,
      route_verified: quotaRaw.route_verified === true, account_count: Array.isArray(quotaRaw.account_ids) ? quotaRaw.account_ids.length : null},
    triggers, trigger_count: triggerCount, last_trigger_at: raw.last_trigger_at || null,
    blockers: list(raw.blockers).filter(v => typeof v === 'string' || (v && typeof v === 'object')).slice(0,20),
  };
}
