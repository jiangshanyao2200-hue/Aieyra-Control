export const text = (value) =>
  typeof value === 'string' ? value : typeof value === 'number' ? String(value) : '';
export const esc = (value) =>
  text(value).replace(
    /[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c],
  );
export const list = (value) =>
  Array.isArray(value) ? value.filter((v) => v && typeof v === 'object' && !Array.isArray(v)) : [];
export function normalize(raw) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw new Error('invalid_snapshot');
  const result = { ...raw };
  for (const key of ['projects', 'agents', 'tasks', 'messages', 'resources', 'deliveries'])
    result[key] = list(raw[key]);
  for (const key of ['connection', 'governance', 'capabilities'])
    result[key] = raw[key] && typeof raw[key] === 'object' ? raw[key] : {};
  result.known = Object.fromEntries(
    ['projects', 'agents', 'tasks', 'messages', 'resources', 'deliveries'].map((k) => [
      k,
      Array.isArray(raw[k]),
    ]),
  );
  result.messages.sort((a, b) =>
    Number.isFinite(a.seq) && Number.isFinite(b.seq)
      ? a.seq - b.seq
      : (timestamp(a.created) || 0) - (timestamp(b.created) || 0),
  );
  return result;
}
export function timestamp(value) {
  if (value === null || value === undefined || value === '') return NaN;
  const date =
    typeof value === 'number' ? new Date(value < 1e12 ? value * 1000 : value) : new Date(value);
  return date.getTime();
}
// Keep the center's original status intact; project the validity of its claim.
export function taskRecordState(task, reachable = true) {
  if (task?.status !== 'doing') return text(task?.status) || 'unknown';
  const lease = timestamp(task.lease_until);
  if (Number.isFinite(lease) && lease > 0 && lease <= Date.now()) return 'expired';
  if (!reachable || !text(task.owner) || !Number.isFinite(lease) || lease <= 0)
    return 'unconfirmed';
  return 'doing';
}
export function taskRecordLabel(task, language = 'zh', reachable = true) {
  const labels = {
    doing: ['已接单（登记）', 'Claimed (recorded)'],
    expired: ['接单已过期', 'Claim expired'],
    unconfirmed: ['执行待核对', 'Execution unconfirmed'],
    planned: ['待安排', 'Planned'],
    blocked: ['有阻塞', 'Blocked'],
    delivered: ['已交付', 'Delivered'],
    accepted: ['已验收', 'Accepted'],
    cancelled: ['已取消', 'Cancelled'],
    superseded: ['已替代', 'Superseded'],
    unknown: ['未知', 'Unknown'],
  };
  return (labels[taskRecordState(task, reachable)] || labels.unknown)[language === 'en' ? 1 : 0];
}
