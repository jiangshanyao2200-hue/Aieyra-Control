import {list, runtimeDispatchReady} from './model.js';

// Mentions choose recipients; ordinary text (including email addresses) stays public
// within the local project room. Never silently turn an invalid mention into all.
export function resolveMentions(body, snapshot, registry, reachable = true) {
  const agents = list(snapshot?.agents);
  const matches = [...String(body).matchAll(/(?:^|[\s，。；、！!？?（(])@(?:\[([^\]\r\n]+)\]|([^\s，。；、！!？?（）()\[\]]+))/gu)];
  if (!matches.length) return {target: 'all', names: []};
  const ids = new Set(), names = [];
  for (const match of matches) {
    const name = match[1] || match[2];
    let candidates;
    if (['领导', 'leader'].includes(name.toLowerCase())) {
      const grants = list(registry?.governance).filter(g => g.active === true && g.role === 'leader');
      const own = grants.filter(g => list(g.projects).includes('control'));
      const eligible = own.length ? own : grants;
      const leaderIds = new Set(eligible.map(g => g.actor_id));
      candidates = agents.filter(a => leaderIds.has(a.id));
    } else candidates = agents.filter(a => a.id === name || a.name === name);
    const unique = [...new Map(candidates.map(a => [a.id, a])).values()];
    if (unique.length !== 1) return {error: unique.length ? 'mention_ambiguous' : 'mention_unknown', name};
    const agent = unique[0];
    if (!runtimeDispatchReady(agent, reachable && snapshot?.connection?.state === 'online')) return {error: 'mention_unavailable', name};
    ids.add(agent.id); names.push(agent.name || agent.id);
  }
  if (ids.size > 1) return {error: 'mention_multiple', name: names.join('、')};
  return {target: [...ids][0], names: [...new Set(names)]};
}

export function mentionText(agent) {
  const name = agent?.name || agent?.id || '';
  return /[\s，。；、！!？?（）()\[\]@]/u.test(name) ? `@[${agent.id}] ` : `@${name} `;
}
