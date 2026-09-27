import {
  esc,
  list,
  text,
  timestamp,
  normalize,
  taskRecordLabel,
  taskRecordState,
} from './model.js';
import { updateHTML } from './dom.js';
import {
  stations,
  statusLabel,
  orderedTasks,
  stationColor,
  palette,
  communicationCurrent,
} from './station-model.js';
import { stationCodes, recentMessages, homeLayout } from './office-home-model.js';
import { createAccountPanel } from './account.js';

const $ = (s) => document.querySelector(s),
  sources = { snapshot: { value: null }, registry: { value: null } },
  controllers = new Set();
let rows = [],
  codes = new Map(),
  view = null,
  stack = [],
  origin = null,
  polling = false,
  disposed = false,
  taskFilter = 'all',
  chatOpenGeneration = 0,
  backdropPressed = false;
let chat = {
  items: [],
  before: null,
  more: false,
  loaded: false,
  loading: false,
  error: false,
  newCount: 0,
};
const colors = new Map();
let language = 'zh';
try {
  if (localStorage.getItem('control-language') === 'en') language = 'en';
} catch {}
const L = (zh, en) => (language === 'en' ? en : zh),
  icon = (name) =>
    `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${{ account: '<circle cx="12" cy="8" r="4"/><path d="M5 21v-2a7 7 0 0 1 14 0v2"/>', tasks: '<path d="M9 6h11M9 12h11M9 18h11M3 5l1 1 2-2m-3 7 1 1 2-2m-3 7 1 1 2-2"/>', chat: '<path d="M4 4h16v12H9l-5 4zm4 4h8m-8 4h5"/>', expand: '<path d="M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5"/>', restore: '<path d="M3 8h5V3m8 0v5h5M8 21v-5H3m18 0h-5v5"/>', close: '<path d="m6 6 12 12M6 18 18 6"/>', back: '<path d="m14 5-7 7 7 7"/>' }[name]}</svg>`;
const clock = (at, full = false) => {
  const n = timestamp(at);
  return Number.isFinite(n)
    ? new Intl.DateTimeFormat(
        language === 'en' ? 'en-GB' : 'zh-CN',
        full
          ? { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }
          : { hour: '2-digit', minute: '2-digit', hour12: false },
      ).format(n)
    : L('尚无记录', 'No record');
};
const reachable = () =>
  !!sources.snapshot.value &&
  !sources.snapshot.error &&
  Date.now() - sources.snapshot.at < 180000 &&
  sources.snapshot.value.connection?.state === 'online';
const actor = (id) =>
  list(sources.snapshot.value?.agents).find((a) => a.id === id)?.name ||
  id ||
  L('未分配', 'Unassigned');
const full = (value) => `<p>${esc(text(value) || L('暂无记录', 'No record'))}</p>`;
const state = (t) => taskRecordState(t, reachable());
const group = (t) =>
  ['accepted', 'delivered', 'cancelled', 'superseded'].includes(state(t))
    ? 'done'
    : ['blocked', 'expired', 'unconfirmed'].includes(state(t))
      ? 'attention'
      : 'active';
function leaseLabel(value) {
  const at = timestamp(value);
  if (!(at > 0)) return L('无有效租约', 'No active lease');
  const label =
    (at > Date.now() ? L('截至 ', 'Until ') : L('已到期 · ', 'Expired · ')) + clock(value, true);
  return (reachable() ? '' : L('上次记录 · ', 'Last record · ')) + label;
}
function communicationFacts(agent = {}, current = true) {
  const r = agent.runtime || {},
    known = current && reachable(),
    at = timestamp(r.lease_until),
    connected = known && r.session_state === 'connected' && communicationCurrent(r),
    labels = {
      disconnected: L('已断开', 'Disconnected'),
      expired: L('租约已到期', 'Lease expired'),
      revoked: L('接入已撤销', 'Access revoked'),
      invalidated: L('绑定已失效', 'Binding invalid'),
    },
    label = !known
      ? L('状态待同步', 'Awaiting update')
      : connected
        ? L('已连接', 'Connected')
        : labels[r.session_state] ||
          (at > 0 && at <= Date.now() ? labels.expired : L('状态待同步', 'Awaiting update')),
    last = r.last_reported_state || (connected ? r.state : '');
  return `<dt>${L('通信状态', 'Communication')}</dt><dd>${esc(label)}</dd><dt>${L('通信租约', 'Communication lease')}</dt><dd>${esc(known ? leaseLabel(r.lease_until) : L('状态待同步', 'Awaiting update'))}</dd><dt>${L('最后运行上报', 'Last reported execution')}</dt><dd>${esc(last ? statusLabel(last, language) : L('暂无记录', 'No record'))}</dd><dt>${L('最近通信观测', 'Last communication observation')}</dt><dd>${esc(clock(r.observed_at || r.last_activity_at, true))}</dd>`;
}
function assignColors() {
  const used = new Set(colors.values());
  for (const s of [...rows].sort((a, b) => a.id.localeCompare(b.id))) {
    if (colors.has(s.id)) continue;
    let c = stationColor('registry:' + s.id);
    if (used.has(c)) c = palette.find((p) => !used.has(p)) || c;
    colors.set(s.id, c);
    used.add(c);
  }
}
function desk(s) {
  const name = s.name || L('未命名工位', 'Unnamed station'),
    label = statusLabel(s.status.state, language);
  return `<button class="home-seat" data-key="seat-${esc(s.id)}" data-seat="${esc(s.id)}" style="--station-accent:${colors.get(s.id)}" aria-label="${esc(name + ' · ' + codes.get(s.id) + ' · ' + label)}" aria-haspopup="dialog"><svg viewBox="-88 -74 176 145" aria-hidden="true"><g class="floor-seat seat-${esc(s.status.state)}"><ellipse class="work-orbit" cx="0" cy="23" rx="69" ry="34"/><path class="desk-side" d="M-49-1 0 23 49-1V8L0 33-49 8Z"/><path class="desk-top" d="M-49-1 0-26 49-1 0 23Z"/><path class="desk-leg" d="M-41 11v21m82-21v21"/><path class="monitor-back" d="M-25-43 13-25 13 4-25-14Z"/><path class="monitor-screen" d="M-20-35 8-21 8-4-20-18Z"/><path class="monitor-lines" d="M-16-29 4-19 M-16-24-2-17 M-16-19-5-13"/><path class="keyboard" d="M-2 5 12-2 26 5 12 12Z"/><path class="seat-chair" d="M-11 27 2 20 18 28 5 35Z M-11 27v12l16 8 13-8V28"/><circle class="seat-signal" cx="42" cy="-3" r="3"/></g></svg><strong class="seat-name">${esc(name)}</strong><span class="seat-code">${esc(codes.get(s.id))}</span><span class="seat-status ${esc(s.status.state)}">${esc(label)}</span></button>`;
}
function layout() {
  const g = homeLayout(rows.length, innerWidth, innerHeight),
    grid = $('#home-grid');
  grid.style.setProperty('--columns', g.columns);
  grid.style.setProperty('--size', g.size + 'px');
  grid.style.setProperty('--gap', g.gap + 'px');
}
function render() {
  rows = stations(
    sources.registry.value,
    sources.snapshot.value,
    reachable() && !sources.registry.error,
  );
  codes = stationCodes(rows);
  assignColors();
  updateHTML(
    $('#home-grid'),
    rows.length
      ? rows.map(desk).join('')
      : `<p class="home-empty">${L(sources.registry.error ? '工位暂时无法读取，正在等待恢复。' : '等待工位接入', 'Waiting for workstations')}</p>`,
  );
  layout();
  $('#home-sync').textContent =
    sources.snapshot.error || sources.registry.error
      ? L('更新待恢复 · 已保留上次记录', 'Updates paused · last records retained')
      : '';
  if (view) renderDialog();
}
function task(t) {
  return `<button class="home-task" data-key="task-${esc(t.id)}" data-task="${esc(t.id)}"><span class="task-state ${esc(state(t))}">${esc(taskRecordLabel(t, language, reachable()))}</span><strong>${esc(t.title || t.id)}</strong><small>${esc(actor(t.owner))}${t.project ? ' · ' + esc(t.project) : ''}</small></button>`;
}
function taskList() {
  const tasks = orderedTasks(sources.snapshot.value?.tasks, reachable()),
    filtered = tasks.filter((t) => taskFilter === 'all' || group(t) === taskFilter);
  return `<div class="task-filters" aria-label="${L('任务状态筛选', 'Task status filter')}">${[
    ['all', '全部', 'All'],
    ['active', '进行中', 'Active'],
    ['attention', '待关注', 'Attention'],
    ['done', '已结束', 'Finished'],
  ]
    .map(
      ([id, zh, en]) =>
        `<button data-filter="${id}" aria-pressed="${taskFilter === id}">${L(zh, en)} <span>${tasks.filter((t) => id === 'all' || group(t) === id).length}</span></button>`,
    )
    .join(
      '',
    )}</div>${filtered.map(task).join('') || `<p class="dialog-empty">${L('暂无此状态的任务', 'No tasks in this state')}</p>`}`;
}
function stationDetail() {
  const s = rows.find((s) => s.id === view.id) || view.record,
    current = rows.some((s) => s.id === view.id);
  return `<h3>${esc(s.name || L('未命名工位', 'Unnamed station'))}</h3><p class="dialog-meta">${esc(codes.get(s.id) || view.code)} · ${esc(statusLabel(current ? s.status.state : 'sync', language))}</p><dl class="station-facts"><dt>${L('项目', 'Project')}</dt><dd>${esc(s.project || '—')}</dd><dt>${L('最近活动', 'Last activity')}</dt><dd>${esc(clock(s.status.at, true))}</dd>${communicationFacts(s.agent, current)}${s.leader ? `<dt>${L('角色', 'Role')}</dt><dd>${L('项目领导', 'Project leader')}</dd>` : ''}</dl><p class="dialog-meta">${L('通信连接与实际执行分别记录；最后上报不代表当前仍在执行。续租可保留旧状态，通信观测时间不等于状态上报时间。', 'Communication and execution are separate; a past report does not confirm current execution. Renewal can retain an older state; communication time is not its report time.')}</p>${!current ? `<p class="dialog-meta">${L('此工位已不在当前成员中。', 'This station is no longer a current member.')}</p>` : ''}<h4>${L('最近动态', 'Recent activity')}</h4>${
    s.messages
      ?.slice(0, 8)
      .map(
        (m) =>
          `<article class="station-activity" data-key="station-msg-${esc(m.id)}"><time>${esc(clock(m.created, true))}</time>${full(m.body)}</article>`,
      )
      .join('') || `<p class="dialog-empty">${L('暂无已记录动态', 'No recorded activity')}</p>`
  }<h4>${L('任务', 'Tasks')}</h4>${s.tasks?.map((t) => task(t) + `<p class="dialog-meta">${L('任务租约：', 'Task lease: ')}${esc(leaseLabel(t.lease_until))}</p>`).join('') || `<p class="dialog-empty">${L('暂无登记任务', 'No registered tasks')}</p>`}`;
}
function taskDetail() {
  const t = list(sources.snapshot.value?.tasks).find((t) => t.id === view.id) || view.record,
    a = list(sources.snapshot.value?.agents).find((a) => a.id === t.owner) || {};
  return `<h3>${esc(t.title || t.id)}</h3><p class="dialog-meta">${esc(taskRecordLabel(t, language, reachable()))} · ${esc(actor(t.owner))}</p><p class="dialog-meta">${esc(t.project || '')} · ${esc(clock(t.updated || t.updated_at, true))}</p><dl class="station-facts"><dt>${L('任务租约', 'Task lease')}</dt><dd>${esc(leaseLabel(t.lease_until))}</dd>${communicationFacts(a)}</dl><h4>${L('任务说明', 'Description')}</h4>${full(t.description || t.summary || t.body || t.title)}<h4>${L('交付与证据', 'Delivery and evidence')}</h4>${full(t.evidence)}${['doing', 'expired', 'unconfirmed'].includes(state(t)) ? `<p class="dialog-meta">${L('认领记录不等于已执行；以工位上报和交付证据为准。', 'A claim does not prove execution. Check workstation reports and delivery evidence.')}</p>` : ''}`;
}
function message(m) {
  const raw = text(m.body),
    preview = raw.split('\n').slice(0, 6).join('\n').slice(0, 280),
    long = raw.length > preview.length;
  return `<article class="chat-record" data-key="chat-${esc(m.id)}" data-message="${esc(m.id)}"><header><strong>${esc(m.sender_name || actor(m.sender))}</strong><time title="${esc(clock(m.created, true))}">${esc(clock(m.created))}</time></header>${full(long ? preview : raw)}${long ? `<details data-preserve-open><summary>${L('展开余下全文', 'Read the rest')}</summary>${full(raw.slice(preview.length))}</details>` : ''}</article>`;
}
function chatMarkup() {
  const items = recentMessages(chat.items);
  return `<div class="chat-boundary" id="chat-boundary">${chat.loading ? L('正在读取消息…', 'Loading messages…') : chat.error ? `${L('暂时无法读取历史，已保留记录。', 'History unavailable; records retained.')} <button data-history-retry>${L('重试', 'Retry')}</button>` : chat.more ? `<button data-older>${L('向上滚动查看更早消息', 'Scroll up for older messages')}</button>` : chat.loaded ? L('已到最近24小时的起点', 'Start of the last 24 hours') : ''}</div><div id="chat-records">${items.map(message).join('') || (!chat.loading ? `<p class="dialog-empty">${L('最近24小时还没有群聊消息', 'No messages in the last 24 hours')}</p>` : '')}</div>`;
}
function scrollState() {
  const b = $('#dialog-body');
  if (!b) return null;
  const atBottom = b.scrollHeight - b.scrollTop - b.clientHeight < 65,
    anchor = [...b.querySelectorAll('[data-message]')].find(
      (e) => e.offsetTop + e.offsetHeight > b.scrollTop,
    );
  return {
    top: b.scrollTop,
    bottom: atBottom,
    id: anchor?.dataset.message,
    offset: anchor ? anchor.offsetTop - b.scrollTop : 0,
  };
}
function restoreScroll(s) {
  const b = $('#dialog-body');
  if (!b || !s) return;
  if (s.bottom) {
    b.scrollTop = b.scrollHeight;
    chat.newCount = 0;
  } else {
    const anchor = [...b.querySelectorAll('[data-message]')].find(
      (e) => e.dataset.message === s.id,
    );
    b.scrollTop = anchor ? anchor.offsetTop - s.offset : s.top;
  }
}
function renderDialog(options = {}) {
  if (!view) return;
  const saved = scrollState(),
    title = {
      station: L('工位详情', 'Workstation'),
      tasks: L('任务', 'Tasks'),
      task: L('任务详情', 'Task details'),
      chat: L('群聊', 'Conversation'),
      account: L('账号', 'Account'),
    }[view.type];
  updateHTML($('#dialog-title'), esc(title));
  $('#dialog-back').hidden = !stack.length;
  const b = $('#dialog-body');
  b.classList.toggle('chat-history', view.type === 'chat');
  updateHTML(
    b,
    view.type === 'account'
      ? account.markup()
      : view.type === 'chat'
        ? chatMarkup()
        : view.type === 'station'
          ? stationDetail()
          : view.type === 'tasks'
            ? taskList()
            : taskDetail(),
  );
  updateHTML(
    $('#dialog-footer'),
    view.type === 'chat'
      ? `<span>${L('最近24小时 · 只读', 'Last 24 hours · read only')}</span><button class="chat-new" data-latest ${chat.newCount ? '' : 'hidden'}>${L('新消息', 'New messages')} ${chat.newCount} ↓</button>`
      : '',
  );
  if (options.reset) b.scrollTop = view.type === 'chat' ? b.scrollHeight : 0;
  else if (view.type === 'chat') restoreScroll(options.scroll || saved);
  else if (saved) b.scrollTop = saved.top;
}
function open(type, id = null) {
  const dialog = $('#office-dialog'),
    record =
      type === 'station'
        ? rows.find((s) => s.id === id)
        : type === 'task'
          ? list(sources.snapshot.value?.tasks).find((t) => t.id === id)
          : null;
  if (['station', 'task'].includes(type) && !record) return;
  if (!dialog.open) {
    origin = document.activeElement;
    stack = [];
  } else
    stack.push({
      view,
      scroll: $('#dialog-body').scrollTop,
      focus: document.activeElement?.dataset.task,
    });
  view = { type, id, record, code: codes.get(id) };
  renderDialog({ reset: true });
  if (!dialog.open) dialog.showModal();
  $('#dialog-close').focus({ preventScroll: true });
  if (type === 'chat') {
    chatOpenGeneration++;
    chat = {
      items: [],
      before: null,
      more: false,
      loaded: false,
      loading: false,
      error: false,
      newCount: 0,
    };
    renderDialog({ reset: true });
    loadHistory(false);
  }
}
function back() {
  if (!stack.length) return close();
  const previous = stack.pop();
  view = previous.view;
  renderDialog({ reset: true });
  $('#dialog-body').scrollTop = previous.scroll;
  const focus = [...document.querySelectorAll('[data-task]')].find(
    (e) => e.dataset.task === previous.focus,
  );
  (focus || $('#dialog-close')).focus({ preventScroll: true });
}
function close() {
  chatOpenGeneration++;
  chat.loading = false;
  view = null;
  stack = [];
  $('#office-dialog').close();
  if (origin?.isConnected) origin.focus({ preventScroll: true });
  else $('#main').focus({ preventScroll: true });
}
function maximize() {
  const d = $('#office-dialog'),
    max = d.classList.toggle('maximized');
  $('#dialog-max').innerHTML = icon(max ? 'restore' : 'expand');
  $('#dialog-max').setAttribute(
    'aria-label',
    max ? L('还原窗口', 'Restore window') : L('放大窗口', 'Expand window'),
  );
  $('#dialog-max').setAttribute('aria-pressed', String(max));
}
async function get(path) {
  const controller = new AbortController();
  controllers.add(controller);
  const timeout = setTimeout(() => controller.abort(), 8000);
  try {
    const r = await fetch(path, { cache: 'no-store', signal: controller.signal });
    if (!r.ok) throw Error('read');
    return await r.json();
  } finally {
    clearTimeout(timeout);
    controllers.delete(controller);
  }
}
async function accountRequest(path, body) {
  if (body === undefined) return get(path);
  const session = await get('/api/session'),
    controller = new AbortController();
  controllers.add(controller);
  const timeout = setTimeout(() => controller.abort(), 20000);
  try {
    const r = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Control-CSRF': session.csrf },
      body: JSON.stringify(body),
      cache: 'no-store',
      signal: controller.signal,
    });
    if (!r.ok) throw Error('account_request_failed');
    return await r.json();
  } finally {
    clearTimeout(timeout);
    controllers.delete(controller);
  }
}
async function loadHistory(older = false) {
  if (chat.loading || view?.type !== 'chat' || (older && (!chat.more || !chat.before))) return;
  const generation = chatOpenGeneration,
    before = older ? chat.before : null;
  chat.loading = true;
  chat.error = false;
  const saved = scrollState();
  renderDialog();
  try {
    const v = await get('/api/chat-history' + (before ? '?before=' + before : ''));
    if (
      !Array.isArray(v?.messages) ||
      typeof v.has_more !== 'boolean' ||
      v.retention_hours !== 24 ||
      (v.has_more &&
        (!Number.isSafeInteger(v.next_before) ||
          v.next_before < 1 ||
          (before && v.next_before >= before)))
    )
      throw Error('shape');
    if (generation !== chatOpenGeneration || view?.type !== 'chat') return;
    const existing = new Set(chat.items.map((m) => m.id)),
      incoming = recentMessages(v.messages),
      added = incoming.filter((m) => !existing.has(m.id)).length;
    chat.items = recentMessages([...chat.items, ...incoming]);
    if (older || !chat.loaded) {
      chat.before = v.next_before;
      chat.more = v.has_more;
    }
    chat.loaded = true;
    if (!older && !saved?.bottom) chat.newCount += added;
  } catch {
    if (generation === chatOpenGeneration) chat.error = true;
  } finally {
    if (generation === chatOpenGeneration) {
      chat.loading = false;
      if (view?.type === 'chat') renderDialog({ scroll: saved });
    }
  }
}
async function refresh() {
  if (polling || disposed) return;
  polling = true;
  $('#main').dataset.refreshing = 'true';
  await Promise.all(
    Object.keys(sources).map(async (key) => {
      try {
        const v = await get('/api/' + key);
        if (
          key === 'snapshot'
            ? !['agents', 'messages', 'tasks', 'resources', 'projects'].every((k) =>
                Array.isArray(v?.[k]),
              )
            : !Array.isArray(v?.seats) || !Array.isArray(v?.governance)
        )
          throw Error('shape');
        sources[key] = {
          value: key === 'snapshot' ? normalize(v) : v,
          at: Date.now(),
          error: false,
        };
      } catch {
        sources[key].error = true;
      }
    }),
  );
  if (disposed) return;
  polling = false;
  if (view?.type === 'chat') {
    const saved = scrollState(),
      ids = new Set(chat.items.map((m) => m.id));
    const incoming = recentMessages(sources.snapshot.value?.messages);
    if (!saved?.bottom) chat.newCount += incoming.filter((m) => !ids.has(m.id)).length;
    chat.items = recentMessages([...chat.items, ...incoming]);
  }
  render();
  $('#main').dataset.refreshing = 'false';
}

document.documentElement.lang = language === 'en' ? 'en' : 'zh-CN';
$('#app').innerHTML =
  `<main id="main" class="office-home" tabindex="-1" aria-label="${L('工位首页', 'Workstation home')}"><div id="home-grid" class="home-grid"></div></main><div id="home-sync" class="home-sync" role="status"></div><nav class="home-sidebar" aria-label="${L('任务、群聊与账号', 'Tasks, chat and account')}"><button id="open-tasks" class="home-orb" aria-label="${L('任务', 'Tasks')}" aria-haspopup="dialog">${icon('tasks')}<span>${L('任务', 'Tasks')}</span></button><button id="open-chat" class="home-orb" aria-label="${L('群聊', 'Conversation')}" aria-haspopup="dialog">${icon('chat')}<span>${L('群聊', 'Chat')}</span></button><button id="open-account" class="home-orb" aria-label="登录" aria-haspopup="dialog">${icon('account')}<span>登录</span></button></nav><dialog id="office-dialog" class="office-dialog" aria-labelledby="dialog-title"><header><button id="dialog-back" class="dialog-tool" aria-label="${L('返回列表', 'Back')}" hidden>${icon('back')}</button><h2 id="dialog-title"></h2><div class="dialog-controls"><button id="dialog-max" class="dialog-tool" aria-label="${L('放大窗口', 'Expand window')}" aria-pressed="false">${icon('expand')}</button><button id="dialog-close" class="dialog-tool" aria-label="${L('关闭弹窗', 'Close dialog')}">${icon('close')}</button></div></header><div id="dialog-body" class="dialog-body" tabindex="0"></div><footer id="dialog-footer" class="dialog-footer"></footer></dialog>`;
$('#main').addEventListener('click', (e) => {
  const b = e.target.closest('[data-seat]');
  if (b) open('station', b.dataset.seat);
});
const account = createAccountPanel({
  request: accountRequest,
  onChange: () => {
    const b = $('#open-account');
    b.setAttribute('aria-label', account.enabled() ? '账号' : '登录');
    b.querySelector('span').textContent = account.enabled() ? '账号' : '登录';
    if (view?.type === 'account') renderDialog();
  },
});
$('#open-account').addEventListener('click', () => {
  open('account');
  account.refresh();
});
$('#open-tasks').addEventListener('click', () => open('tasks'));
$('#open-chat').addEventListener('click', () => open('chat'));
$('#dialog-close').addEventListener('click', close);
$('#dialog-back').addEventListener('click', back);
$('#dialog-max').addEventListener('click', maximize);
$('#office-dialog').addEventListener('cancel', (e) => {
  e.preventDefault();
  if (stack.length) back();
  else close();
});
const outsideDialog = (e) => {
  const r = $('#office-dialog').getBoundingClientRect();
  return e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom;
};
$('#office-dialog').addEventListener('pointerdown', (e) => {
  backdropPressed = e.target === e.currentTarget && outsideDialog(e);
});
$('#office-dialog').addEventListener('click', (e) => {
  const dismiss = backdropPressed && e.target === e.currentTarget && outsideDialog(e);
  backdropPressed = false;
  if (dismiss) return close();
  const a = e.target.closest('[data-account]');
  if (a) return account.action(a.dataset.account);
  const t = e.target.closest('[data-task]');
  if (t) return open('task', t.dataset.task);
  const f = e.target.closest('[data-filter]');
  if (f) {
    taskFilter = f.dataset.filter;
    renderDialog({ reset: true });
    return;
  }
  if (e.target.closest('[data-older],[data-history-retry]'))
    return loadHistory(chat.loaded && chat.more);
  if (e.target.closest('[data-latest]')) {
    chat.newCount = 0;
    renderDialog({ reset: true });
  }
});
$('#office-dialog').addEventListener('keydown', (e) => {
  if (e.key !== 'Tab') return;
  const nodes = [
      ...e.currentTarget.querySelectorAll('button:not(:disabled),a[href],summary,[tabindex="0"]'),
    ].filter((n) => n.getClientRects().length && !n.closest('[hidden]')),
    first = nodes[0],
    last = nodes.at(-1);
  if (!first) return;
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault();
    first.focus();
  }
});
$('#dialog-body').addEventListener(
  'scroll',
  () => {
    if (view?.type !== 'chat') return;
    const b = $('#dialog-body');
    if (b.scrollHeight - b.scrollTop - b.clientHeight < 65 && chat.newCount) {
      chat.newCount = 0;
      const n = $('[data-latest]');
      if (n) n.hidden = true;
    }
    if (b.scrollTop < 70 && chat.more && !chat.loading && !chat.error) loadHistory(true);
  },
  { passive: true },
);
window.addEventListener('resize', () => {
  layout();
  const d = $('#office-dialog');
  if (d.style.width) d.style.width = Math.min(parseFloat(d.style.width), innerWidth - 36) + 'px';
  if (d.style.height)
    d.style.height = Math.min(parseFloat(d.style.height), innerHeight - 36) + 'px';
});
window.addEventListener('pagehide', () => {
  disposed = true;
  clearInterval(timer);
  controllers.forEach((c) => c.abort());
});
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) refresh();
});
render();
refresh();
account.refresh();
const timer = setInterval(() => {
  if (!document.hidden) refresh();
}, 8000);
