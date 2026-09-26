import {createProjectMemory} from './project-memory.js';
import {createCloudSettings} from './cloud-settings.js';
import {strings} from './i18n.js';
import {updateHTML} from './dom.js';
import {returnFocus} from './focus.js';
import {createSharedSpace} from './shared-space.js';
import {esc, text, list, normalize, normalizeWatchdog, timestamp, safeUrl, matches, runtimeState, isBound, workstationKey, runningCount, pendingStates, mergeDeliveries, taskRecordState, taskRecordLabel} from './model.js';

const paths = {
  command:'M4 4h16v10H4z M8 20h8 M12 14v6 M7 8h3 M14 8h3',
  requests:'M4 3h16v18H4z M8 7h8 M8 11h8 M8 15h4 M15 16l2 2 4-5',
  overview:'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',
  projects:'M3 7h7l2 2h9v11H3z M3 7V4h7l2 3h8v2',
  agents:'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2 M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8 M22 21v-2a4 4 0 0 0-3-3.87 M16 3.13a4 4 0 0 1 0 7.75',
  tasks:'M9 5h11 M9 12h11 M9 19h11 M3 5l1 1 2-3 M3 12l1 1 2-3 M3 19l1 1 2-3',
  resources:'M3 3h18v7H3z M3 14h18v7H3z M7 6.5h.01 M7 17.5h.01 M11 6.5h6 M11 17.5h6',
  activity:'M3 12h4l3-8 4 16 3-8h4',
  search:'M21 21l-5-5 M18 10.5a7.5 7.5 0 1 0-15 0 7.5 7.5 0 0 0 15 0',
  arrow:'M5 12h14 M14 7l5 5-5 5',
  external:'M14 3h7v7 M21 3l-11 11 M10 3H3v18h18v-7',
  refresh:'M20 7a9 9 0 1 0 1 9 M20 2v6h-6',
  close:'M6 6l12 12 M6 18 18 6',
  menu:'M4 6h16 M4 12h16 M4 18h16',
  check:'M5 12l4 4L19 6',
  chevron:'M9 5l7 7-7 7',
  chat:'M21 11a9 9 0 0 1-9 9 10 10 0 0 1-4-.8L3 21l1.8-5A9 9 0 1 1 21 11 M8 10h8 M8 14h5',
  send:'m22 2-7 20-4-9-9-4Z M22 2 11 13',
  globe:'M21 12a9 9 0 1 0-18 0 9 9 0 0 0 18 0 M3 12h18 M12 3a18 18 0 0 1 0 18 18 18 0 0 1 0-18',
  clock:'M21 12a9 9 0 1 0-18 0 9 9 0 0 0 18 0 M12 7v5l3 2',
  code:'M8 5l-7 7 7 7 M16 5l7 7-7 7 M14 3l-4 18',
  document:'M14 2H4v20h16V8Z M14 2v6h6 M8 12h8 M8 16h6',
  copy:'M9 9h12v12H9z M5 15H3V3h12v2',
  shield:'M12 2 3 6v6c0 5 9 10 9 10s9-5 9-10V6Z M8 12l3 3 5-6',
  warning:'m12 3 10 18H2Z M12 9v5 M12 17h.01',
};
const icon = (name, cls='') => `<svg class="icon ${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${paths[name] || paths.document}"/></svg>`;
const app = document.querySelector('#app');
const dialog = document.querySelector('#detail');
const projectMemory = createProjectMemory({api,dialog,getLanguage:()=>state.lang});
let drawerReturnFocus = null;
const pages = ['command','requests','overview','projects','agents','tasks','resources','activity'];
let preferredLanguage = 'zh';
try { preferredLanguage = localStorage.getItem('control-language') === 'en' ? 'en' : 'zh'; } catch {}
const state = {
  lang:preferredLanguage, page:'command',
  snapshot:null, watchdog:{available:false,state:'unknown',error:'not_published',seats:[],triggers:[],blockers:[]}, error:'', csrf:null, apiReachable:false, loading:true, refreshing:false,
  query:'', project:'all', taskStatus:'all', resourceKind:'all',
  deliveries:[], resourceBusy:false, detail:null, briefs:{}, freshnessSignature:'',
  registry:null, registryError:'', localAgents:null, localAgentsError:'', registryBusy:false, managementBusy:false, managementError:'', managementDialog:null,
};
const t = (key, params={}) => Object.entries(params).reduce((s,[k,v]) => s.replaceAll(`{${k}}`, String(v)), strings[state.lang][key] ?? key);
const sharedSpace=createSharedSpace({api,getSnapshot:()=>data(),getLanguage:()=>state.lang,writeReady:()=>state.apiReachable&&Boolean(state.csrf),onChange:reload=>{if(reload)void refresh();else render();}});
let commandCenter = null, commandLoading = null, commandLoadFailed = false;
function ensureCommandCenter() {
  if (!commandLoading) commandLoading = import('./command-center.js').then(module => {
    commandCenter = module.createCommandCenter({api, getLanguage:()=>state.lang, writeReady:()=>state.apiReachable && Boolean(state.csrf), onChange:()=>render(), onOpenPanel:()=>{state.page='command';state.query='';},getConnection:connection});
    render(); return commandCenter;
  }).catch(() => {commandLoadFailed=true;render();return null;});
  return commandLoading;
}
function commandView() {
  if (commandCenter) return commandCenter.render();
  if (!commandLoadFailed) void ensureCommandCenter();
  return loadingView(commandLoadFailed,'command');
}
function loadingView(failed,kind) {
  return `<section class="panel cc-loading" role="status"><p>${esc(t(kind+(failed?'LoadFailed':'Loading')))}</p>${failed?`<button class="button subtle" data-action="reload-interface">${esc(state.lang==='zh'?'重新加载界面':'Reload interface')}</button>`:''}</section>`;
}
let requestDashboard = null, requestLoading = null, requestLoadFailed = false;
function requestsView() {
  if (requestDashboard) return requestDashboard.render();
  if (!requestLoading && !requestLoadFailed) requestLoading = import('./request-dashboard.js').then(module => {
    requestDashboard = module.createRequestDashboard({getSnapshot:()=>data(), getLanguage:()=>state.lang,
      getReachable:()=>state.apiReachable, onChange:()=>render(), api, writeReady:()=>state.apiReachable&&Boolean(state.csrf)});
    render();
  }).catch(()=>{requestLoadFailed=true;render();});
  return loadingView(requestLoadFailed,'requests');
}
const data = () => state.snapshot ?? normalize({});
const projectName = id => data().projects.find(p => p.id === id)?.name || id || t('notProvided');
const agentName = id => data().agents.find(a => a.id === id)?.name || id || t('noOwner');
const connection = () => state.error ? 'offline' : state.loading && !state.snapshot ? 'connecting' : ['online','offline'].includes(data().connection.state) ? data().connection.state : 'unknown';
function heartbeatState(agent) {
  const observed=timestamp(data().observed_at);
  if(!state.apiReachable||data().connection.state!=='online'||!Number.isFinite(observed)||Date.now()-observed>60000)return 'unknown';
  return agent.online===true?'online':agent.online===false?'stale':'unknown';
}
function watchdogBadge(status) { return badge(status || 'unknown', t(status || 'unknown')); }
function watchdogDate(value) { return dateText(value, true); }
function watchdogSeatCard(seat) {
  const task = seat.task_title || seat.task_id || t('notProvided');
  const binding = seat.bound === true ? `<span class="watchdog-bound">${esc(t('watchdogBound'))}</span>` : seat.bound === false ? `<span class="watchdog-bound is-muted">${esc(t('watchdogUnbound'))}</span>` : `<span class="watchdog-bound is-muted">${esc(t('watchdogBindingUnknown'))}</span>`;
  return `<article class="watchdog-seat"><div class="watchdog-seat-head">${avatar(seat.name,'lavender')}<div><strong>${esc(seat.name)}</strong><small>${esc(seat.seat_id || t('notProvided'))}</small></div>${watchdogBadge(seat.state)}</div><dl><div><dt>${esc(t('watchdogSource'))}</dt><dd>${esc(seat.source || t('unknown'))}</dd></div><div><dt>${esc(t('watchdogTask'))}</dt><dd>${esc(task)}</dd></div><div><dt>${esc(t('watchdogObserved'))}</dt><dd>${esc(watchdogDate(seat.last_activity_at || seat.observed_at))}</dd></div></dl>${binding}</article>`;
}
function watchdogPanel() {
  const w=state.watchdog||{};
  const available=w.available===true;
  const quota=w.quota||{};
  const seats=Array.isArray(w.seats)?w.seats:[];
  const triggers=Array.isArray(w.triggers)?w.triggers.slice(0,5):[];
  const blockers=Array.isArray(w.blockers)?w.blockers:[];
  return `<section class="panel watchdog-panel" aria-labelledby="watchdog-title"><div class="panel-heading"><h2 id="watchdog-title">${esc(t('watchdogTitle'))}<span class="count">${available?`${seats.length}/5`:'?'}</span></h2><div class="watchdog-heading-status">${watchdogBadge(available?w.state:'unknown')}</div></div>${!available?`<div class="watchdog-unavailable"><div class="watchdog-unavailable-icon">${icon('shield')}</div><div><strong>${esc(t(w.error==='not_published'?'watchdogNotPublished':'watchdogUnavailable'))}</strong><p>${esc(t('watchdogUnknownNote'))}</p></div></div>`:`<div class="watchdog-meta"><span>${esc(t('watchdogSource'))} · ${esc(w.source||t('unknown'))}</span><span>${esc(t('watchdogObserved'))} · ${esc(watchdogDate(w.observed_at))}</span><span>${esc(t('watchdogInterval'))} · ${w.interval_seconds===null?t('unknown'):`${w.interval_seconds}s`}</span><span>${esc(t('watchdogReason'))} · ${esc(w.reason?t(w.reason):t('unknown'))}</span><span>${esc(t('watchdogRemainingWork'))} · ${w.remaining_work===null?t('unknown'):(w.remaining_work?t('yes'):t('no'))}</span></div>${w.duplicate_count?`<p class="watchdog-warning">${icon('warning')}${esc(t('watchdogDuplicates',{n:w.duplicate_count}))}</p>`:''}${seats.length?`<div class="watchdog-seats">${seats.slice(0,5).map(watchdogSeatCard).join('')}</div>`:`<div class="watchdog-empty">${icon('agents')}<strong>${esc(t('watchdogNoSeats'))}</strong><span>${esc(t('watchdogNoSeatsText'))}</span></div>`}<div class="watchdog-lower"><section class="watchdog-quota"><h3>${esc(t('watchdogQuota'))} ${watchdogBadge(quota.state)}</h3><dl><div><dt>${esc(t('watchdogQuotaSource'))}</dt><dd>${esc(quota.source||t('unknown'))}</dd></div><div><dt>${esc(t('watchdogQuotaScope'))}</dt><dd>${esc(quota.scope||t('unknown'))}</dd></div><div><dt>${esc(t('watchdogRemaining'))}</dt><dd>${quota.remaining_percent===null?t('unknown'):`${quota.remaining_percent}%`}</dd></div><div><dt>${esc(t('watchdogQuotaObserved'))}</dt><dd>${esc(watchdogDate(quota.observed_at))}</dd></div></dl><p class="quiet-note">${icon('shield')}${esc(quota.reason?t(quota.reason):t('watchdogQuotaNote'))}</p></section><section class="watchdog-triggers"><h3>${esc(t('watchdogTriggers'))}${Number.isInteger(w.trigger_count)?`<span class="count">${w.trigger_count}</span>`:''}</h3>${triggers.length?`<ol>${triggers.map(trigger=>`<li><div><strong>${esc(trigger.id)}</strong><span>${esc(trigger.reason?t(trigger.reason):t('watchdogNoReason'))}</span></div>${watchdogBadge(trigger.state)}<time>${esc(watchdogDate(trigger.updated_at||trigger.created_at))}</time></li>`).join('')}</ol>`:`<div class="watchdog-empty compact"><span>${esc(t('watchdogNoTriggers'))}</span></div>`}</section></div>${blockers.length?`<div class="watchdog-blockers"><strong>${esc(t('watchdogBlockers'))}</strong>${blockers.map(item=>`<span>${esc(typeof item==='string'?t(item):item.summary?t(item.summary):item.code?t(item.code):t('unknown'))}</span>`).join('')}</div>`:''}`}</section>`;
}
const badge = (status, label) => `<span class="badge status-${esc(status || 'unknown')}"><span class="status-dot"></span>${esc(label || t(status || 'unknown'))}</span>`;

function normalizeRegistry(raw) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw) || !Array.isArray(raw.hosts) || !Array.isArray(raw.adapters) || !Array.isArray(raw.seats)) throw new Error('invalid_registry');
  return {...raw, projects:Array.isArray(raw.projects)?raw.projects:[], governance:Array.isArray(raw.governance)?raw.governance:[], hosts:raw.hosts.filter(v=>v&&typeof v==='object'), adapters:raw.adapters.filter(v=>v&&typeof v==='object'), seats:raw.seats.filter(v=>v&&typeof v==='object'), operations:Array.isArray(raw.operations)?raw.operations.filter(v=>v&&typeof v==='object'):[], capabilities:raw.capabilities&&typeof raw.capabilities==='object'?raw.capabilities:{}};
}
function normalizeLocalAgents(raw) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw) || !Array.isArray(raw.agents)) throw new Error('invalid_local_agents');
  return {...raw, agents:raw.agents.filter(v=>v&&typeof v==='object')};
}
function registryBadge(value) {
  const status = String(value || 'unknown');
  const labels = {online:t('hostOnline'),offline:t('hostOffline'),available:t('available'),unavailable:t('adapterUnavailable'),unsupported:t('unsupported'),registered:t('registered'),active:t('active'),paused:t('paused'),closed:t('closed'),unknown:t('unknown'),pending:t('pending'),accepted:t('accepted'),succeeded:t('succeeded'),failed:t('failed')};
  return badge(status, labels[status] || status);
}
function registryHost(id) { return state.registry?.hosts?.find(v=>v.id===id); }
function registryAdapter(id) { return state.registry?.adapters?.find(v=>v.id===id); }
function registryProject(id) { return state.registry?.projects?.find(v=>v.id===id); }
function operationLabel(operation) { return ({open:t('openSeat'),pause:t('pauseSeat'),resume:t('resumeSeat'),interrupt:t('interruptSeat'),close:t('closeSeat')})[operation] || operation; }
function registryErrorText() { return state.registryError || t('registryUnavailable'); }

const cloudSettings=createCloudSettings({api,dialog});
function registryPanel() {
  if (!state.registry && !state.registryError && !state.localAgents) return '';
  if (!state.registry) return `<section class="panel registry-panel"><div class="panel-heading"><h2>${esc(t('workstationManagement'))}</h2><button class="button subtle" data-action="refresh-registry" ${state.registryBusy?'disabled':''}>${icon('refresh',state.registryBusy?'spin':'')}${esc(t('refreshRegistry'))}</button></div><div class="registry-unavailable">${icon('warning')}<div><strong>${esc(t('registryUnavailable'))}</strong><p>${esc(registryErrorText())}</p></div></div></section>`;
  const r=state.registry, canManage=r.capabilities?.manage_workstations===true;
  const hosts=r.hosts||[], adapters=r.adapters||[], seats=r.seats||[], ops=r.operations||[], grants=r.governance||[];
  return `<section class="panel registry-panel"><div class="panel-heading"><div><h2>${esc(t('workstationManagement'))}<span class="count">${seats.length}</span></h2><p class="panel-subtitle">${esc(t('registryTruth'))}</p></div><div class="registry-toolbar"><button class="button subtle" data-action="refresh-registry" ${state.registryBusy?'disabled':''}>${icon('refresh',state.registryBusy?'spin':'')}${esc(t('refreshRegistry'))}</button></div></div><div class="registry-summary"><span>${icon('resources')} ${esc(t('hosts'))} <strong>${hosts.length}</strong></span><span>${icon('code')} ${esc(t('adapters'))} <strong>${adapters.length}</strong></span><span>${icon('agents')} ${esc(t('seats'))} <strong>${seats.length}</strong></span><span>${icon('shield')} ${esc(t('operations'))} <strong>${ops.length}</strong></span></div>
  
  <section class="registry-section registry-seats"><div class="registry-section-head"><h3>${esc(t('seats'))}</h3><span class="registry-note">${esc(t('receiptRequired'))}</span></div>${seats.length?seats.map(s=>{const a=registryAdapter(s.adapter_id),p=registryProject(s.project),pending=ops.find(o=>o.seat_id===s.id&&['pending','accepted','unknown'].includes(o.state));return `<article class="registry-seat"><div class="registry-card-head"><div><strong>${esc(s.name||s.id)}</strong><small>${esc(s.id||t('notProvided'))} · ${esc(p?.name||s.project||t('unknown'))}</small></div><div class="registry-state-stack">${registryBadge(s.desired_state)}${registryBadge(s.observed_state)}</div></div><div class="registry-seat-meta"><span>${esc(t('host'))}: ${esc(registryHost(s.host_id)?.name||s.host_id||t('notProvided'))}</span><span>${esc(t('adapter'))}: ${esc(a?.name||s.adapter_id||t('notProvided'))}</span><span>${esc(t('scope'))}: ${esc(s.scope||t('notProvided'))}</span><span>${esc(t('epoch'))}: ${esc(String(s.epoch??'?'))} · ${esc(t('version'))}: ${esc(String(s.version??'?'))}</span></div>${pending?`<p class="registry-pending">${icon('clock')}${esc(t('operationPending'))} · <code>${esc(pending.id||'')}</code> · ${registryBadge(pending.state)}</p>`:''}<div class="registry-seat-footer"><span>${esc(t('ownership'))}: ${esc(s.ownership||t('unknown'))} · ${esc(t('runtimeRef'))}: ${esc(s.runtime_ref||t('unbound'))}</span><div></div></div></article>`}).join(''):`<p class="muted small">${esc(t('noSeats'))}</p>`}</section>
  <details id="registry-infrastructure" class="registry-disclosure" data-preserve-open><summary><span>${esc(state.lang==='en'?'Hosts and adapters':'主机与适配器')}</span><small>${hosts.length} / ${adapters.length}</small></summary><div class="registry-grid"><section class="registry-section"><h3>${esc(t('hosts'))}</h3>${hosts.length?hosts.map(h=>`<article class="registry-card"><div class="registry-card-head"><div><strong>${esc(h.name||h.id)}</strong><small>${esc(h.id||t('notProvided'))}</small></div>${registryBadge(h.online===true?'online':h.online===false?'offline':'unknown')}</div><dl><div><dt>${esc(t('actor'))}</dt><dd class="mono">${esc(h.actor_id||t('notProvided'))}</dd></div><div><dt>${esc(t('capacity'))}</dt><dd>${esc(String(h.active_reservations??0))} / ${esc(String(h.max_active??'?'))} ${esc(t('activeSeats'))} · ${esc(String(h.max_seats??'?'))} ${esc(t('totalSeats'))}</dd></div><div><dt>${esc(t('projects'))}</dt><dd>${esc(Array.isArray(h.projects)?h.projects.join(', '):t('notProvided'))}</dd></div></dl></article>`).join(''):`<p class="muted small">${esc(t('noHosts'))}</p>`}</section><section class="registry-section"><h3>${esc(t('adapters'))}</h3>${adapters.length?adapters.map(a=>`<article class="registry-card"><div class="registry-card-head"><div><strong>${esc(a.name||a.id)}</strong><small>${esc(a.id||t('notProvided'))} · ${esc(a.version_label||t('versionUnknown'))}</small></div>${registryBadge(a.availability)}</div><dl><div><dt>${esc(t('host'))}</dt><dd>${esc(registryHost(a.host_id)?.name||a.host_id||t('notProvided'))}</dd></div><div><dt>${esc(t('kind'))}</dt><dd>${esc(a.kind||t('unknown'))}</dd></div><div><dt>${esc(t('actions'))}</dt><dd>${esc(Array.isArray(a.actions)?a.actions.join(', '):t('notProvided'))}</dd></div></dl></article>`).join(''):`<p class="muted small">${esc(t('noAdapters'))}</p>`}</section></div></details>
  <details id="registry-governance" class="registry-section registry-governance registry-disclosure" data-preserve-open><summary><span>${esc(t('governance'))}</span><small>${grants.length}</small></summary><p class="registry-note">${esc(t('centerAuthority'))}</p>${grants.length?`<div class="governance-table">${grants.map(g=>`<div class="governance-entry"><strong>${esc(g.name||g.actor_id)}</strong><span>${registryBadge(g.active?'active':'revoked')}</span><small>${esc(g.role||t('unknown'))} · ${esc(Array.isArray(g.projects)?g.projects.join(', '):t('notProvided'))}</small></div>`).join('')}</div>`:`<p class="muted small">${esc(t('noGovernance'))}</p>`}</details>
  <details id="registry-operations" class="registry-section registry-operations registry-disclosure" data-preserve-open><summary><span>${esc(t('operations'))}</span><small>${ops.length}${ops.some(o=>['pending','accepted','unknown','failed'].includes(o.state))?esc(state.lang==='en'?' · needs review':' · 需核对'):''}</small></summary><p class="registry-note">${esc(t('operationsNote'))}</p>${ops.length?`<div class="operation-list">${ops.slice(0,12).map(o=>`<div class="operation-entry"><div><strong>${esc(operationLabel(o.operation))}</strong><small>${esc(o.id||t('notProvided'))} · ${esc(o.seat_id||t('notProvided'))}</small></div>${registryBadge(o.state)}</div>`).join('')}</div>`:`<p class="muted small">${esc(t('noOperations'))}</p>`}</details></section>`;
}
function dateText(value, relative=false) {
  const ms = timestamp(value);
  if (!Number.isFinite(ms)) return t('noDate');
  if (!relative) return new Intl.DateTimeFormat(state.lang === 'zh' ? 'zh-CN' : 'en-GB', {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}).format(ms);
  const seconds = (Date.now()-ms)/1000;
  if (seconds < -60) return t('futureTime');
  if (seconds < 60) return t('justNow');
  if (seconds < 3600) return t('minutesAgo',{n:Math.floor(seconds/60)});
  if (seconds < 86400) return t('hoursAgo',{n:Math.floor(seconds/3600)});
  return t('daysAgo',{n:Math.floor(seconds/86400)});
}
function avatar(name, color='mint') { return `<span class="avatar color-${esc(color)}">${esc([...text(name)][0] || '·')}</span>`; }
function empty(kind, filtered=false) {
  const key = filtered ? 'noResults' : {projects:'noProjects',agents:'noAgents',tasks:'noTasks',resources:'noResources',activity:'noActivity',messages:'noMessages'}[kind];
  return `<div class="empty">${icon(filtered?'search':kind==='messages'?'chat':kind)}<h3>${esc(t(key))}</h3><p>${esc(t(`${key}Text`))}</p>${filtered ? `<button class="button subtle" data-action="clear-filters">${esc(t('clearFilters'))}</button>`:''}</div>`;
}
function filtered(items) { return items.filter(item => (state.project==='all' || item.project===state.project || (state.page==='projects' && item.id===state.project)) && matches(item,state.query)); }
function panelTitle(title, page, n) { return `<div class="panel-heading"><h2>${esc(title)}${Number.isInteger(n)?`<span class="count">${n}</span>`:''}</h2>${page?`<button class="text-button" data-page="${page}">${esc(t('viewAll'))}${icon('arrow')}</button>`:''}</div>`; }
function filters(options='') {
  return `<div class="filter-bar"><label class="select-wrap">${icon('projects')}<select data-focus="project-filter" id="project-filter" aria-label="${esc(t('filterLabel'))}"><option value="all">${esc(t('allProjects'))}</option>${data().projects.map(p=>`<option value="${esc(p.id)}" ${state.project===p.id?'selected':''}>${esc(p.name||p.id)}</option>`).join('')}</select></label>${options}${state.query||state.project!=='all'?`<button class="text-button clear-filter" data-action="clear-filters">${icon('close')}${esc(t('clearFilters'))}</button>`:''}</div>`;
}
function projectCard(project, index=0) {
  const tasks = data().tasks.filter(v=>v.project===project.id);
  const agents = data().agents.filter(v=>v.project===project.id);
  const colors = ['mint','lavender','blue','amber'];
  return `<button class="project-card" data-detail="project" data-id="${esc(project.id)}"><div class="project-card-top"><span class="project-icon color-${colors[index%colors.length]}">${icon('projects')}</span>${icon('external','muted')}</div><h3>${esc(project.name||project.id||t('unknown'))}</h3><p class="project-source">${esc(project.source||t('notProvided'))}</p><div class="project-card-bottom"><span>${esc(t('projectTasks',{n:tasks.length}))}</span><span class="tiny-dot">·</span><span>${esc(t('projectAgents',{n:agents.length}))}</span></div>${tasks.some(v=>v.status==='blocked')?`<span class="card-note">${badge('blocked')}</span>`:''}</button>`;
}
function taskRows(tasks, compact=false) {
  return tasks.length ? `<div class="task-list ${compact?'compact':''}">${tasks.map(task=>`<button class="task-row" data-detail="task" data-id="${esc(task.id)}"><span class="task-marker marker-${esc(task.status||'unknown')}">${['delivered','accepted'].includes(task.status)?icon('check'):''}</span><span class="task-info"><strong>${esc(task.title||task.id||t('unknown'))}</strong><span>${esc(projectName(task.project))}<span class="meta-separator">/</span>${esc(agentName(task.owner))}</span></span>${badge(taskRecordState(task,state.apiReachable&&data().connection?.state==='online'),taskRecordLabel(task,state.lang,state.apiReachable&&data().connection?.state==='online'))}${icon('chevron','row-chevron')}</button>`).join('')}</div>` : empty('tasks',Boolean(state.query||state.project!=='all'||state.taskStatus!=='all'));
}
function agentRow(agent, compact=false) {
  const runtime = runtimeState(agent,state.apiReachable);
  const shared=data().agents.filter(a=>workstationKey(a)===workstationKey(agent)).length>1;
  return `<button class="agent-row ${compact?'compact':''} " data-agent="${esc(agent.id)}">${avatar(agent.name||agent.id,runtime==='running'?'mint':'lavender')}<span class="agent-info"><strong>${esc(agent.name||agent.id)}</strong><small>${esc(projectName(agent.project))}${shared?` · ${esc(t('sharedRuntime'))}`:''}</small></span><span class="agent-state">${badge(runtime)}<small>${esc(t('heartbeat'))} · ${esc(t(heartbeatState(agent)))}</small></span></button>`;
}
function governance() {
  const g=data().governance;
  return `<section class="panel governance">${panelTitle(t('governance'))}${g.leader?`<div class="lead-row">${avatar(g.leader.name,'amber')}<div><small>${esc(g.leader.label||t('currentLead'))}</small><strong>${esc(g.leader.name||t('unknown'))}</strong></div>${badge(g.leader.status)}</div>`:`<p class="muted small">${esc(t('noGovernance'))}</p>`}${list(g.deputies).map(d=>`<div class="deputy-row"><span class="tiny-dot amber"></span><div><strong>${esc(d.name||t('deputy'))}</strong><p>${esc(d.scope||t('notProvided'))}</p></div></div>`).join('')}<p class="panel-footnote">${icon('shield')}${esc(t('governanceHint'))}</p></section>`;
}
function overview() {
  const s=data(), ps=filtered(s.projects.map(p=>({...p,project:p.id}))), ts=filtered(s.tasks);
  const metrics=[['projectCount',s.known.projects?ps.length:'—','projects','projectHint','mint'],['runtimeCount',s.known.agents?runningCount(filtered(s.agents),state.apiReachable):'—','agents','runtimeHint','lavender'],['taskCount',s.known.tasks?ts.filter(v=>taskRecordState(v,state.apiReachable&&s.connection?.state==='online')==='doing').length:'—','tasks','taskHint','blue'],['deliveryCount',s.known.deliveries||state.deliveries.length?mergeDeliveries(s.deliveries,state.deliveries).filter(v=>pendingStates.has(v.state)).length:'—','chat','deliveryHint','amber']];
  return `${filters()}<div class="metrics">${metrics.map(([title,n,ic,hint,color])=>`<div class="metric"><div class="metric-label"><span>${esc(t(title))}</span><span class="color-${color}">${icon(ic)}</span></div><strong>${n}</strong><small>${esc(t(hint))}</small></div>`).join('')}</div>${watchdogPanel()}
  <div class="overview-grid"><div class="overview-primary"><section>${panelTitle(t('currentProjects'),'projects',ps.length)}<div class="project-grid">${ps.length?ps.slice(0,4).map(projectCard).join(''):empty('projects',Boolean(state.query||state.project!=='all'))}</div></section><section class="panel">${panelTitle(t('tasks'),'tasks')}${taskRows(ts.slice(0,6),true)}</section><section class="panel">${panelTitle(t('recentMessages'),'agents')}<div class="discussion-preview">${s.messages.length?s.messages.slice(-2).reverse().map(m=>`<button class="discussion-row" data-detail="message" data-id="${esc(m.id)}">${avatar(m.sender_name,'blue')}<span><strong>${esc(m.sender_name||t('unknown'))}<time>${esc(dateText(m.created,true))}</time></strong><p>${esc(m.body)}</p></span></button>`).join(''):empty('messages')}</div></section></div><div class="overview-secondary"><section class="panel">${panelTitle(t('workstationActivity'),'agents')}<div class="agent-list">${filtered(s.agents).length?filtered(s.agents).slice(0,5).map(a=>agentRow(a,true)).join(''):empty('agents')}</div><p class="panel-footnote">${icon('clock')}${esc(t('heartbeatNote'))}</p></section>${governance()}<div class="capability-note">${icon('globe')}<div><strong>${esc(t('capabilities'))}</strong><p>${esc(s.capabilities.mobile_pairing===true?'':t('mobilePending'))}</p><p>${esc(s.capabilities.remote_execution===true?'':t('remoteDisabled'))}</p></div></div></div></div>`;
}
function projectsView() { const items=filtered(data().projects.map(p=>({...p,project:p.id})));return `${filters()}<div class="section-meta">${esc(t('filterResults',{n:items.length}))}</div><div class="project-grid full">${items.length?items.map(projectCard).join(''):empty('projects',Boolean(state.query||state.project!=='all'))}</div>`; }
function tasksView() {
  const items=filtered(data().tasks).filter(v=>state.taskStatus==='all'||v.status===state.taskStatus);
  return `${filters(`<label class="select-wrap"><select id="task-filter" data-focus="task-filter" aria-label="${esc(t('taskFilter'))}">${['all','planned','doing','blocked','delivered','accepted'].map(v=>`<option value="${v}" ${state.taskStatus===v?'selected':''}>${esc(t(v))}</option>`).join('')}</select></label>`)}<div class="section-meta">${esc(t('filterResults',{n:items.length}))}</div><section class="panel task-panel">${taskRows(items)}</section>`;
}
function resourceView() {
  const items=filtered(data().resources).filter(v=>state.resourceKind==='all'||v.kind===state.resourceKind);
  return `${filters(`<button class="button subtle push-right" data-action="refresh-resources" ${state.resourceBusy||data().capabilities.resource_refresh!==true||!state.apiReachable?'disabled':''}>${icon('refresh',state.resourceBusy?'spin':'')}${esc(t('refreshResources'))}</button>`)}<div class="chip-row" role="group" aria-label="${esc(t('resourceFilter'))}">${['all','directory','server','github','repository','document'].map(k=>`<button class="chip ${state.resourceKind===k?'selected':''}" data-kind="${k}" aria-pressed="${state.resourceKind===k}">${esc(t(k))}</button>`).join('')}</div><p class="quiet-note">${icon('shield')}${esc(t('metadataNote'))}</p><div class="resource-grid">${items.length?items.map(r=>`<button class="resource-card" data-detail="resource" data-id="${esc(r.id)}"><div class="resource-top"><span class="resource-icon color-${r.kind==='server'?'blue':r.kind==='github'?'lavender':'mint'}">${icon(r.kind==='server'?'resources':['github','repository'].includes(r.kind)?'code':r.kind==='directory'?'projects':'document')}</span>${badge(r.status)}</div><small class="resource-kind">${esc(t(r.kind||'unknown'))}<span> / </span>${esc(projectName(r.project))}</small><h3>${esc(r.name||r.id)}</h3><p class="resource-summary">${esc(r.summary||t('noSummary'))}</p><code class="resource-location">${esc(r.location||t('notProvided'))}</code><div class="resource-foot"><span>${icon('clock')}${esc(dateText(r.observed_at,true))}</span>${icon('arrow')}</div></button>`).join(''):empty('resources',Boolean(state.query||state.project!=='all'||state.resourceKind!=='all'))}</div>`;
}
function replyMarkup(d, full=false) { return typeof d.reply==='string'&&d.reply.trim()?`<section class="public-reply"><div class="reply-heading">${icon('chat')}<strong>${esc(t('publicReply'))}</strong><time>${esc(dateText(d.reply_at,true))}</time></div><div class="reply-body ${full?'':'preview'}">${esc(d.reply)}</div>${d.reply_truncated?`<p class="reply-truncated">${esc(t('replyTruncated'))}</p>`:''}<p class="reply-note">${esc(t('turnEnded'))}</p>${!full?`<button class="text-button" data-detail="delivery" data-id="${esc(d.id)}">${esc(t('details'))}${icon('arrow')}</button>`:''}</section>`:''; }
function activityView() {
  const s=data();
  const records=[...s.messages.map(v=>({type:'message',time:v.created,id:v.id,title:v.sender_name,body:v.body,status:v.kind||'discussion',project:v.project,scope:v.project})),...s.tasks.filter(v=>v.evidence).map(v=>({type:'task',time:v.updated_at||v.updated,id:v.id,title:v.title,body:v.evidence,status:v.status,project:v.project,scope:v.scope})),...mergeDeliveries(s.deliveries,state.deliveries).map(v=>({type:'delivery',time:v.updated_at||v.created_at,id:v.id,title:v.target==='all'?t('engineering'):agentName(v.target),body:v.body,status:v.state,project:v.project}))].filter(v=>(state.project==='all'||v.project===state.project)&&matches(v,state.query)).sort((a,b)=>(timestamp(b.time)||0)-(timestamp(a.time)||0));
  return `${filters()}<div class="activity-list">${records.length?records.slice(0,100).map(r=>`<button class="activity-row" data-detail="${r.type}" data-id="${esc(r.id)}"><span class="activity-axis">${icon(r.type==='task'?'code':r.type==='delivery'?'send':'chat')}</span><span class="activity-content"><span class="activity-top"><strong>${esc(r.title||t('unknown'))}</strong>${badge(r.status)}<time>${esc(dateText(r.time,true))}</time></span><span class="activity-body">${esc(r.body||t('notProvided'))}</span><span class="activity-meta">${esc(r.scope||r.id)}${icon('arrow')}</span></span></button>`).join(''):empty('activity',Boolean(state.query||state.project!=='all'))}</div>`;
}
function globalResults() {
  const groups=[['projects',data().projects,'project'],['agents',data().agents,'agent'],['tasks',data().tasks,'task'],['resources',data().resources,'resource'],['activity',data().messages,'message']];
  const filteredGroups=groups.map(([label,items,type])=>[label,filtered(items.map(v=>({...v,project:v.project||(type==='project'?v.id:undefined)}))),type]);
  return `${filters()}<p class="section-meta">${esc(t('searchFor',{q:state.query}))}</p><div class="search-groups">${filteredGroups.some(g=>g[1].length)?filteredGroups.filter(g=>g[1].length).map(([label,items,type])=>`<section class="panel">${panelTitle(t(label),null,items.length)}${items.slice(0,40).map(item=>`<button class="search-result" data-detail="${type}" data-id="${esc(item.id)}">${icon(label)}<span><strong>${esc(item.title||item.name||item.sender_name||item.id)}</strong><small>${esc(item.scope||item.location||item.root||item.body||projectName(item.project))}</small></span>${icon('chevron')}</button>`).join('')}</section>`).join(''):empty('projects',true)}</div>`;
}
function render() {
  state.freshnessSignature=freshnessSignature();
  document.documentElement.lang=state.lang==='zh'?'zh-CN':'en';document.title='Aieyra Control';
  const L=(zh,en)=>state.lang==='zh'?zh:en;
  const views={requests:requestsView,overview:globalResults,projects:projectsView,agents:registryPanel,tasks:requestsView,resources:()=>sharedSpace.render(),activity:activityView};
  const drawer=state.page!=='command'||state.query.trim();
  const title=state.query.trim()?t('search'):({resources:L('共享','Shared'),tasks:L('任务','Tasks'),requests:L('任务','Tasks'),overview:t('search')})[state.page]||t(state.page);
  updateHTML(app,`<div id="control-shell" class="${commandCenter?.isChatCollapsed()?'chat-collapsed':''}"><main id="main" tabindex="-1">${commandView()}</main><div id="workspace-drawer" class="workspace-drawer" ${drawer?'':'hidden'} role="region" aria-label="${esc(title)}"><div class="workspace-drawer-header"><h2>${esc(title)}</h2><button class="icon-button" data-page="command" aria-label="${esc(t('close'))}">${icon('close')}</button></div>${state.page==='overview'||state.query.trim()?`<label class="drawer-search">${icon('search')}<input id="global-search" data-focus="search" type="search" value="${esc(state.query)}" placeholder="${esc(t('searchShort'))}" aria-label="${esc(t('search'))}" autocomplete="off"></label>`:''}<div id="workspace-drawer-body">${drawer?(state.query.trim()?globalResults():views[state.page]?.()||''):''}</div></div><nav id="control-dock" aria-label="${esc(L('主要入口','Main navigation'))}"><div class="dock-tools">${[['command','chat',L('群聊','Chat')],['resources','resources',L('共享','Shared')],['tasks','tasks',L('任务','Tasks')]].map(([name,glyph,label])=>`<button class="dock-tool ${state.page===name?'active':''}" data-page="${name}" aria-current="${state.page===name?'page':'false'}" aria-label="${esc(label)}">${icon(glyph)}<span>${esc(label)}</span></button>`).join('')}</div></nav></div>`);
  commandCenter?.activate(true);requestDashboard?.activate(['tasks','requests'].includes(state.page));
  const overlayOpen=Boolean(drawer)||!app.querySelector('#scene-inspector')?.hidden;
  for(const node of app.querySelectorAll('.command-scene-viewport,.scene-tools'))node.inert=overlayOpen;
  const chat=app.querySelector('#chat-panel');if(chat)chat.inert=overlayOpen&&matchMedia('(max-width:760px)').matches;
}

async function api(path, payload) {
  const controller=new AbortController(); const timer=setTimeout(()=>controller.abort(),12000);
  try {
    const response=await fetch(path,{method:payload===undefined?'GET':'POST',cache:'no-store',credentials:'same-origin',signal:controller.signal,headers:{'Accept':'application/json',...(payload===undefined?{}:{'Content-Type':'application/json','X-Control-CSRF':state.csrf||''})},...(payload===undefined?{}:{body:JSON.stringify(payload)})});
    let result;try{result=await response.json();}catch{const e=new Error('invalid_response');e.status=response.status;e.uncertain=true;throw e;}
    if(!response.ok){if(response.status===403&&payload!==undefined&&result.code==='csrf_rejected')state.csrf=null;const e=new Error(text(result.error)||text(result.code)||`HTTP ${response.status}`);e.status=response.status;e.code=result.code;throw e;}
    return result;
  } finally {clearTimeout(timer);}
}
async function session() { const result=await api('/api/session'); if(typeof result.csrf!=='string'||!result.csrf)throw new Error('invalid_session');state.csrf=result.csrf; }
async function refresh() {
  if(state.refreshing)return;
  state.refreshing=true;render();
  const results=await Promise.allSettled([api('/api/snapshot'),state.csrf?Promise.resolve():session(),api('/api/watchdog'),api('/api/registry'),api('/api/local-agents')]);
  if(results[0].status==='fulfilled'){
    try{state.snapshot=normalize(results[0].value);state.error='';state.apiReachable=true;
    }catch{state.error='invalid_snapshot';state.apiReachable=false;}
  }else {state.error=results[0].reason.message;state.apiReachable=false;}
  if(results[1].status==='rejected')state.csrf=null;
  if(results[2].status==='fulfilled'){
    try{state.watchdog=normalizeWatchdog(results[2].value);}catch{state.watchdog={available:false,state:'unknown',error:'invalid',seats:[],triggers:[],blockers:[]};}
  }else state.watchdog={available:false,state:'unknown',error:results[2].reason?.status===404?'not_published':'unavailable',seats:[],triggers:[],blockers:[]};
  if(results[3].status==='fulfilled'){
    try{state.registry=normalizeRegistry(results[3].value);state.registryError='';}catch{state.registry=null;state.registryError=t('registryInvalid');}
  }else {state.registry=null;state.registryError=results[3].reason?.message||t('registryUnavailable');}
  if(results[4].status==='fulfilled'){
    try{state.localAgents=normalizeLocalAgents(results[4].value);state.localAgentsError='';}catch{state.localAgents=null;state.localAgentsError=t('localAgentsInvalid');}
  }else {state.localAgents=null;state.localAgentsError=results[4].reason?.message||t('localAgentsUnavailable');}
  state.refreshing=false;state.loading=false;render();refreshOpenDetail();
}
function toast(message) { const element=document.querySelector('#toast');element.textContent=message;element.hidden=false;clearTimeout(toast.timer);toast.timer=setTimeout(()=>element.hidden=true,4200); }
function navigate(page) {
  if(!pages.includes(page))return;
  const changed=page!==state.page;
  const closing=page==='command'&&(state.page!=='command'||state.query.trim());
  if(changed&&page!=='command')drawerReturnFocus=returnFocus(document.activeElement,`#control-dock [data-page="${page}"]`);
  state.page=page;state.query='';
  if(page!=='command')commandCenter?.closePanel();
  render();
  if(changed){const body=app.querySelector('#workspace-drawer-body');if(body)body.scrollTop=0;}
  if(page!=='command')app.querySelector('#workspace-drawer .icon-button')?.focus({preventScroll:true});
  else if(closing){drawerReturnFocus?.();drawerReturnFocus=null;}
}

function findItem(type,id) { const key={project:'projects',agent:'agents',task:'tasks',resource:'resources',message:'messages'}[type];return type==='delivery'?mergeDeliveries(data().deliveries,state.deliveries).find(v=>v.id===id):data()[key]?.find(v=>v.id===id); }
function metricsStale(metrics) {
  const observed=timestamp(metrics?.observed_at);
  return metrics?.stale===true||!Number.isFinite(observed)||Date.now()-observed>132000||observed-Date.now()>60000;
}
function freshnessSignature() {
  return JSON.stringify([state.apiReachable,data().agents.map(a=>[runtimeState(a,state.apiReachable),heartbeatState(a)]),data().tasks.map(task=>taskRecordState(task,state.apiReachable&&data().connection.state==='online')),data().resources.map(r=>metricsStale(r.metrics))]);
}
const detailField=(label,value,mono=false)=>`<div class="detail-field" data-field="${esc(label)}"><dt>${esc(t(label))}</dt><dd class="${mono?'mono':''}">${esc(value===undefined||value===null||value===''?t('notProvided'):value)}</dd></div>`;
function metricsDetail(metrics, field) {
  const stale=metricsStale(metrics),unavailable=!state.apiReachable||!metrics||metrics.state!=='sampled';
  if(unavailable)return `<section class="detail-section server-metrics"><h3>${esc(t('serverMetrics'))} ${badge(stale&&metrics?'stale':'unavailable')}</h3><p class="quiet-note">${icon('clock')}${esc(t(stale&&metrics?'metricsStale':!state.apiReachable?'currentUnavailable':'metricsMissing'))}</p>${metrics?.observed_at?`<dl>${field('metricsTime',dateText(metrics.observed_at))}</dl>`:''}</section>`;
  const number=v=>typeof v==='number'&&Number.isFinite(v)&&v>=0;
  const n=v=>number(v)?new Intl.NumberFormat(state.lang,{maximumFractionDigits:2}).format(v):t('unknown');
  const bytes=v=>number(v)?`${n(v/1024**3)} GiB`:t('unknown');
  const duration=number(metrics.uptime_seconds)?`${Math.floor(metrics.uptime_seconds/86400)} ${t('days')} ${Math.floor(metrics.uptime_seconds%86400/3600)} ${t('hours')}`:t('unknown');
  return `<section class="detail-section server-metrics"><h3>${esc(t('serverMetrics'))} ${badge(stale?'stale':'sampled')}</h3>${stale?`<p class="quiet-note">${esc(t('metricsStale'))}</p>`:''}<dl>${field('metricsTime',dateText(metrics.observed_at))}${field('load1m',n(metrics.load_1m))}${field('cpuCores',n(metrics.cpu_count))}${field('memoryFree',`${bytes(metrics.memory_available_bytes)} / ${bytes(metrics.memory_total_bytes)}`)}${field('diskFree',`${bytes(metrics.disk_available_bytes)} / ${bytes(metrics.disk_total_bytes)}`)}${field('uptime',duration)}</dl></section>`;
}
function refreshOpenDetail() {
  if(!dialog.open||!state.detail)return;
  const item=findItem(state.detail.type,state.detail.id);if(!item)return;
  if(state.detail.type==='resource'){
    const current=dialog.querySelector('.server-metrics'),next=metricsDetail(item.metrics,detailField);
    if(current&&current.outerHTML!==next)current.outerHTML=next;
  }
  if(state.detail.type==='agent'){
    const values={runtime:t(runtimeState(item,state.apiReachable)),lastKnown:t(item.runtime?.last_known_state||item.runtime?.state||'unknown'),runtimeObserved:dateText(item.runtime?.observed_at),lastActivity:dateText(item.runtime?.last_activity_at),lastSeen:dateText(item.last_seen)};
    for(const [key,value] of Object.entries(values)){const field=dialog.querySelector(`[data-field="${key}"] dd`);if(field&&field.textContent!==value)field.textContent=value;}
  }
}
function briefSection(item, field) {
  if(item.brief_available!==true)return '';
  const saved=state.briefs[item.id];
  if(!saved||saved.status!=='ready')return `<section class="detail-section brief-section"><h3>${esc(t('brief'))}</h3>${saved?.status==='failed'?`<p class="inline-error">${esc(t('briefFailed'))}</p>`:''}<button class="button subtle" data-brief="${esc(item.id)}" ${saved?.status==='loading'?'disabled':''}>${icon('code')}${esc(t(saved?.status==='loading'?'briefLoading':'loadBrief'))}</button></section>`;
  const brief=saved.value;
  const ref=(label,r)=>r&&typeof r==='object'?`<div class="brief-reference"><span>${esc(t(label))}</span>${badge(r.freshness||'unknown')}<code>${esc(r.path||t('notProvided'))}${Number.isInteger(r.line)?`:${r.line}`:''}</code>${r.name?`<small>${esc(r.name)}</small>`:''}${r.sha256?`<details><summary>SHA256</summary><code>${esc(r.sha256)}</code></details>`:''}</div>`:'';
  return `<section class="detail-section brief-section"><h3>${esc(t('brief'))}</h3><dl>${field('scope',brief.scope)}${field('coverage',t(brief.coverage||'unknown'))}${field('checkedAt',dateText(brief.checked_at))}${field('source',brief.source_ref,true)}</dl><p class="quiet-note brief-caution">${icon('shield')}${esc(t('briefCaution'))}</p>${list(brief.features).map(f=>`<article class="brief-feature"><h4>${esc(f.summary||f.id||t('unknown'))}</h4><p>${esc(t('verification'))} · ${esc(t(text(f.verification)||'unknown'))}</p>${ref('source',f.source)}${ref('test',f.test)}</article>`).join('')}${ref('verificationReceipt',brief.verification_receipt)}${Array.isArray(brief.needs_coordination)&&brief.needs_coordination.length?`<h4>${esc(t('needsCoordination'))}</h4><ul class="brief-needs">${brief.needs_coordination.filter(v=>typeof v==='string').map(v=>`<li>${esc(v)}</li>`).join('')}</ul>`:''}</section>`;
}
async function loadBrief(id) {
  const resource=data().resources.find(r=>r.id===id);
  if(resource?.brief_available!==true||state.briefs[id]?.status==='loading')return;
  state.briefs[id]={status:'loading'};showDetail('resource',id);
  try{const value=await api(`/api/resources/${encodeURIComponent(id)}/brief`);if(!value||typeof value!=='object'||Array.isArray(value))throw new Error('invalid_brief');state.briefs[id]={status:'ready',value};}
  catch{state.briefs[id]={status:'failed'};}
  if(dialog.open&&state.detail?.type==='resource'&&state.detail.id===id)showDetail('resource',id);
}
function showDetail(type,id) {
  const item=findItem(type,id);if(!item)return;state.detail={type,id};
  const field=detailField;
  let fields=field('sourceRecord',item.id,true), body='';
  if(type==='project')fields+=field('root',item.root,true)+field('source',item.source,true);
  if(type==='agent')fields+=field('projects',projectName(item.project))+field('runtime',t(runtimeState(item,state.apiReachable)))+field('binding',t(isBound(item)?'bound':'notBound'))+field('lastSeen',dateText(item.last_seen))+field('runtimeObserved',dateText(item.runtime?.observed_at))+field('lastActivity',dateText(item.runtime?.last_activity_at))+field('lastKnown',t(item.runtime?.last_known_state||item.runtime?.state||'unknown'))+field('thread',item.runtime?.thread_id,true);
  if(type==='task'){fields+=field('projects',projectName(item.project))+field('owner',agentName(item.owner))+field('status',taskRecordLabel(item,state.lang,state.apiReachable&&data().connection?.state==='online'))+field('scope',item.scope,true)+field('version',item.version);body=`<section class="detail-section"><h3>${esc(t('evidence'))}</h3><pre>${esc(item.evidence||t('noEvidence'))}</pre></section>`;}
  if(type==='resource'){
    fields+=field('location',item.location,true)+field('source',item.source_ref||item.source||item.project,true)+field('status',t(item.status||'unknown'))+field('observed',dateText(item.observed_at))+field('sensitivity',t(item.sensitivity||'unknown'))+field('size',typeof item.size==='number'?`${new Intl.NumberFormat().format(item.size)} B`:null);
    const details=item.details&&typeof item.details==='object'?Object.entries(item.details).filter(([key,value])=>typeof value==='string'&&!/password|secret|token|credential|密钥|密码/i.test(key)).slice(0,20):[];
    body=`<p class="detail-description">${esc(item.summary||t('noSummary'))}</p>${details.length?`<section class="detail-section"><h3>${esc(t('sourceDetails'))}</h3><dl>${details.map(([key,value])=>field(key,value)).join('')}</dl></section>`:''}${item.kind==='server'||item.metrics?metricsDetail(item.metrics,field):''}${briefSection(item,field)}${item.source_sha256?`<details class="source-integrity"><summary>${esc(t('sourceDigest'))}</summary><code>${esc(item.source_sha256)}</code></details>`:''}`;
  }
  if(type==='message'){fields+=field('owner',item.sender_name)+field('projects',projectName(item.project))+field('time',dateText(item.created))+field('receipt',Number.isInteger(item.receipt_count)?t('readCount',{n:item.receipt_count}):t('unknown'))+field('reply',item.reply_to,true);body=`<section class="detail-section"><h3>${esc(t('messageLabel'))}</h3><pre>${esc(item.body)}</pre></section>`;}
  if(type==='delivery'){fields+=field('target',item.target==='all'?t('engineering'):agentName(item.target))+field('status',t(item.state||'unknown'))+field('messageId',item.message_id,true)+field('queueId',item.queue_id,true)+field('observed',dateText(item.updated_at));body=`<p class="quiet-note">${esc(t('deliveryNote'))} ${esc(t('turnEnded'))}</p><section class="detail-section"><h3>${esc(t('messageLabel'))}</h3><pre>${esc(item.body)}</pre>${replyMarkup(item,true)}${item.error?`<p class="inline-error">${esc(item.error)}</p>`:''}</section>`;}
  const url=type==='resource'?safeUrl(item.location):null;
  dialog.innerHTML=`<div class="detail-header"><span class="eyebrow">${esc(t('sourceRecord'))}</span><button class="icon-button" data-dialog-close aria-label="${esc(t('close'))}" autofocus>${icon('close')}</button></div><h2 id="detail-title">${esc(item.name||item.title||item.sender_name||t(type==='delivery'?'delivery':'details'))}</h2><dl class="detail-fields">${fields}</dl>${body}<div class="detail-actions">${url?`<a class="button primary" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${icon('external')}${esc(t('openLink'))}</a>`:''}${type==='project'?`<button class="button primary" data-project-tasks="${esc(item.id)}">${esc(t('openProject'))}${icon('arrow')}</button>`:''}${type==='agent'?`<button class="button primary" data-chat-agent="${esc(item.id)}">${icon('chat')}${esc(t('send'))}</button>`:''}<button class="button subtle" data-copy="${esc(type==='resource'?item.location:item.id)}">${icon('copy')}${esc(t(type==='resource'?'copyLocation':'copyId'))}</button></div>`;
  if(!dialog.open)dialog.showModal();
}
async function refreshResources() {
  if(state.resourceBusy||data().capabilities.resource_refresh!==true||!state.apiReachable||!state.csrf)return;
  state.resourceBusy=true;render();
  try{const result=await api('/api/resources/refresh',{});if(!Array.isArray(result.resources))throw new Error('invalid_resources');state.snapshot.resources=list(result.resources);state.snapshot.known.resources=true;toast(t('refreshSuccess'));}
  catch{toast(t('refreshFailed'));}finally{state.resourceBusy=false;render();}
}
async function refreshRegistryOnly() {
  if(state.registryBusy)return;state.registryBusy=true;render();
  const results=await Promise.allSettled([api('/api/registry'),api('/api/local-agents')]);
  if(results[0].status==='fulfilled'){try{state.registry=normalizeRegistry(results[0].value);state.registryError='';}catch{state.registry=null;state.registryError=t('registryInvalid');}}else{state.registry=null;state.registryError=results[0].reason?.message||t('registryUnavailable');}
  if(results[1].status==='fulfilled'){try{state.localAgents=normalizeLocalAgents(results[1].value);state.localAgentsError='';}catch{state.localAgents=null;state.localAgentsError=t('localAgentsInvalid');}}
  state.registryBusy=false;render();
}
app.addEventListener('input',event=>{
  if(event.target.id==='global-search'){state.query=event.target.value;render();}
});
app.addEventListener('change',event=>{
  if(event.target.id==='project-filter')state.project=event.target.value;
  else if(event.target.id==='task-filter')state.taskStatus=event.target.value;
  else return;
  render();
});
app.addEventListener('click',event=>{
  const button=event.target.closest('button');if(!button||button.disabled)return;
  if(button.dataset.page||button.dataset.action||button.dataset.detail)commandCenter?.closeMenus();
  if(button.dataset.page){if(button.dataset.page==='command'&&button.closest('#control-dock'))commandCenter?.showChat();navigate(button.dataset.page);return;}
  if(button.dataset.agent){navigate('command');commandCenter?.targetAgent(button.dataset.agent);return;}
  if(button.dataset.detail){showDetail(button.dataset.detail,button.dataset.id);return;}
  if(button.dataset.seatOperation)return;
  if(button.dataset.kind){state.resourceKind=button.dataset.kind;render();return;}
  switch(button.dataset.action){
    case 'reload-interface':window.location.reload();break;
    case 'refresh':refresh();commandCenter?.refresh();break;
    case 'search-open':navigate('overview');app.querySelector('#global-search')?.focus();break;
    case 'refresh-registry':refreshRegistryOnly();break;
    case 'agent-access-open':break;
    case 'project-memory-open':void projectMemory.open(button.dataset.project||'');break;
    case 'cloud-open':void cloudSettings.open();break;
    case 'management-open':break;
    case 'refresh-resources':refreshResources();break;
    case 'clear-filters':state.query='';state.project='all';state.taskStatus='all';state.resourceKind='all';render();break;
    case 'language':state.lang=state.lang==='zh'?'en':'zh';try{localStorage.setItem('control-language',state.lang);}catch{}render();break;
  }
});
dialog.addEventListener('click',async event=>{
  const button=event.target.closest('button');
  if(button?.disabled)return;
  if(button?.hasAttribute('data-dialog-close'))dialog.close();
  if(button?.dataset.copy){try{await navigator.clipboard.writeText(button.dataset.copy);toast(t('copied'));}catch{toast(t('copyFailed'));}}
  if(button?.dataset.projectTasks){state.project=button.dataset.projectTasks;dialog.close();navigate('tasks');}
  if(button?.dataset.chatAgent){dialog.close();navigate('command');const center=await ensureCommandCenter();center?.targetAgent(button.dataset.chatAgent);}
  if(button?.dataset.brief)loadBrief(button.dataset.brief);
  if(event.target===dialog){const r=dialog.getBoundingClientRect();if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)dialog.close();}
});
dialog.addEventListener('cancel',event=>{if(state.managementBusy)event.preventDefault();});
dialog.addEventListener('close',()=>{state.detail=null;state.managementDialog=null;state.managementBusy=false;});
document.addEventListener('keydown',event=>{
  if(event.isComposing||event.keyCode===229||event.defaultPrevented)return;
  if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='k'){event.preventDefault();if(!dialog.open){navigate('overview');app.querySelector('#global-search')?.focus();}}
  if(event.key==='Escape'&&!dialog.open){
    if(commandCenter?.dismissMenus()){event.preventDefault();return;}
    if(state.page!=='command'||state.query.trim()){event.preventDefault();navigate('command');}
    else if(commandCenter?.dismissPanel())event.preventDefault();
  }
});
matchMedia('(max-width:760px)').addEventListener('change',()=>render());
window.addEventListener('hashchange',()=>{const page=location.hash.slice(1).split('?')[0];if(page==='command'){state.page='command';state.query='';render();}});
if(typeof window.controlPlatform?.onHumanRequestOpen==='function')window.controlPlatform.onHumanRequestOpen(async reference=>{navigate('command');const center=await ensureCommandCenter();if(!center)throw new Error('command_view_unavailable');await center.openHuman(reference);});
window.addEventListener('online',refresh);
document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible')refresh();});
setInterval(()=>{if(document.visibilityState==='visible')refresh();},20000);
setInterval(()=>{if(document.visibilityState!=='visible'||!state.snapshot)return;if(state.freshnessSignature!==freshnessSignature())render();refreshOpenDetail();},5000);
render();refresh();
