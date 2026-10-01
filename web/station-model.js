import { list, text, timestamp, taskRecordState } from './model.js';

export const palette = [
  '#b5c788',
  '#91bed6',
  '#d4a28d',
  '#b6a3d5',
  '#81c6b3',
  '#d1bd7f',
  '#d698b4',
  '#92b29d',
  '#93a6d8',
  '#c2af98',
  '#a8ccbb',
  '#b4c5d0',
];
export function stationColor(id) {
  let hash = 0;
  for (const c of id) hash = (hash * 31 + c.codePointAt(0)) >>> 0;
  return palette[hash % palette.length];
}

export const fresh = (at, now = Date.now(), age = 180000) =>
  Number.isFinite(timestamp(at)) && now - timestamp(at) <= age && timestamp(at) - now <= 60000;

export function communicationCurrent(runtime, now = Date.now()) {
  if (runtime.bound !== true || runtime.stale === true) return false;
  if (runtime.session_state !== undefined && runtime.session_state !== 'connected') return false;
  // Native adapters may have independent execution evidence and no transport lease.
  if (runtime.lease_until !== undefined) {
    const lease = timestamp(runtime.lease_until);
    if (!Number.isFinite(lease) || lease <= now) return false;
  }
  return true;
}

// Execution evidence and transport leases have different lifetimes. A missing
// transport heartbeat says nothing about whether the native agent is working.
export function executionReportedAt(runtime = {}) {
  if (Object.hasOwn(runtime, 'runtime_reported_at')) return runtime.runtime_reported_at;
  // Older bridge snapshots used their transport heartbeat as last_activity_at.
  if (runtime.adapter === 'aieyra-agent/1') return null;
  return runtime.last_activity_at || runtime.observed_at;
}

export function presence(agent = {}, reachable = true, now = Date.now()) {
  const runtime = agent.runtime || {},
    at = executionReportedAt(runtime);
  if (reachable && communicationCurrent(runtime, now) && fresh(at, now)) {
    if (['running', 'busy'].includes(runtime.state))
      return { state: 'running', tone: 'active', at, source: runtime.evidence || 'agent_report' };
    if (['idle', 'paused', 'waiting_user', 'stopped'].includes(runtime.state))
      return {
        state: runtime.state,
        tone: runtime.state === 'waiting_user' ? 'attention' : 'quiet',
        at,
        source: runtime.evidence || 'agent_report',
      };
  }
  if (reachable && fresh(agent.last_seen, now))
    return { state: 'recent', tone: 'active', at: agent.last_seen, source: 'recorded_activity' };
  return { state: 'sync', tone: 'quiet', at: at || agent.last_seen, source: 'unconfirmed' };
}

export function stations(registry, snapshot, reachable = true, now = Date.now()) {
  const actors = new Map(list(snapshot?.agents).map((a) => [a.id, a])),
    seen = new Set();
  const result = [];
  for (const seat of list(registry?.seats)) {
    if (
      !text(seat.id) ||
      !text(seat.actor_id) ||
      seen.has(seat.id) ||
      seat.membership_state === 'removed' ||
      ['removed', 'retired', 'archived'].includes(seat.desired_state)
    )
      continue;
    if (seat.station_binding?.seat_id && seat.station_binding.seat_id !== seat.id) continue;
    if (
      seat.membership_state !== 'active' &&
      seat.desired_state !== 'registered' &&
      seat.ownership !== 'external'
    )
      continue;
    seen.add(seat.id);
    const original = actors.get(seat.actor_id) || {},
      agent =
        original.runtime?.adapter === 'aieyra-agent/1' && original.runtime.seat_id !== seat.id
          ? { ...original, runtime: {} }
          : original,
      status = presence(agent, reachable, now);
    const messages = list(snapshot?.messages)
      .filter((m) => m.sender === seat.actor_id)
      .sort((a, b) => (timestamp(b.created) || 0) - (timestamp(a.created) || 0));
    const tasks = list(snapshot?.tasks).filter((t) => t.owner === seat.actor_id);
    const leader = list(registry?.governance).some(
      (g) =>
        g.active === true &&
        g.role === 'leader' &&
        g.actor_id === seat.actor_id &&
        (listStrings(g.projects).includes(seat.project) || listStrings(g.projects).includes('*')),
    );
    result.push({ ...seat, agent, status, messages, tasks, leader, color: stationColor(seat.id) });
  }
  return result.sort(
    (a, b) =>
      Number(b.leader) - Number(a.leader) ||
      text(a.project).localeCompare(text(b.project)) ||
      text(a.id).localeCompare(text(b.id)),
  );
}
const listStrings = (value) =>
  Array.isArray(value) ? value.filter((x) => typeof x === 'string') : [];
export function statusLabel(state, language = 'zh') {
  return ({
    running: ['正在工作', 'Working'],
    idle: ['待命', 'Ready'],
    paused: ['已暂停', 'Paused'],
    waiting_user: ['等待沟通', 'Awaiting input'],
    stopped: ['已停止', 'Stopped'],
    recent: ['近期有活动', 'Recent activity'],
    sync: ['状态待同步', 'Awaiting update'],
  }[state] || ['状态待同步', 'Awaiting update'])[language === 'en' ? 1 : 0];
}
export function orderedTasks(tasks, reachable) {
  const order = {
    blocked: 0,
    doing: 1,
    unconfirmed: 2,
    expired: 3,
    planned: 4,
    delivered: 5,
    accepted: 6,
  };
  return list(tasks)
    .slice()
    .sort(
      (a, b) =>
        (order[taskRecordState(a, reachable)] ?? 7) - (order[taskRecordState(b, reachable)] ?? 7) ||
        (timestamp(b.updated || b.updated_at) || 0) - (timestamp(a.updated || a.updated_at) || 0),
    );
}
