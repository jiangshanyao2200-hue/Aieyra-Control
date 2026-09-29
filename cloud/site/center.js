import { transition } from './transitions.js';

const $ = (s) => document.querySelector(s);
const labels = {
  project: '项目分享',
  bug: '使用问题',
  discussion: '交流与建议',
  repair: '修复记录',
  update: '官方更新',
  open: '开放讨论',
  triaged: '已查看',
  in_progress: '处理中',
  resolved: '已解决',
  dismissed: '已说明',
};
const boards = { '': '全部话题', releases: '新版发布', feedback: '功能反馈', lounge: '娱乐交流' };
const types = {
  releases: ['update'],
  feedback: ['bug', 'repair'],
  lounge: ['discussion', 'project'],
};
const form = $('.matrix-filter'),
  list = $('[data-center-topics]'),
  status = $('[data-center-status]'),
  more = $('[data-center-more]');
let state,
  cursor = null,
  controller,
  generation = 0,
  loading = false;
const seen = new Set();
const node = (tag, text, cls) => {
  const n = document.createElement(tag);
  n.textContent = text;
  if (cls) n.className = cls;
  return n;
};
const validCursor = (value) => value === null || (Number.isSafeInteger(value) && value > 0);
const validTopic = (item) =>
  item &&
  /^[a-f0-9-]{36}$/.test(item.id) &&
  typeof item.title === 'string' &&
  typeof item.summary === 'string' &&
  typeof item.content === 'string' &&
  ['project', 'bug', 'discussion', 'repair', 'update'].includes(item.type);
const date = (value) => {
  const d = new Date(value);
  return Number.isNaN(d.valueOf())
    ? ''
    : new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium' }).format(d);
};
function queryURL(topic = '') {
  const q = new URLSearchParams();
  for (const key of ['board', 'type', 'query']) if (state[key]) q.set(key, state[key]);
  if (topic) q.set('topic', topic);
  return '/center' + (q.size ? '?' + q : '');
}
async function api(path, signal) {
  const timer = new AbortController(),
    timeout = setTimeout(() => timer.abort(), 12000);
  const abort = () => timer.abort();
  signal.addEventListener('abort', abort, { once: true });
  try {
    if (signal.aborted) timer.abort();
    const r = await fetch(path, { signal: timer.signal, credentials: 'omit', cache: 'no-store' });
    if (!r.ok) {
      const e = Error('request_failed');
      e.status = r.status;
      throw e;
    }
    return await r.json();
  } finally {
    clearTimeout(timeout);
    signal.removeEventListener('abort', abort);
  }
}
function meta(item) {
  const n = node('div', '', 'topic-meta');
  n.append(node('span', labels[item.type], 'topic-badge'));
  if (item.official) n.append(node('span', '官方发布', 'official'));
  else n.append(node('span', labels[item.state] || '开放讨论'));
  return n;
}
function openTopic(id) {
  history.pushState({ listScroll: window.scrollY }, '', queryURL(id));
  navigate(() => {
    $('[data-topic-view]').scrollIntoView({ block: 'start', behavior: 'instant' });
    $('[data-back]').focus({ preventScroll: true });
  });
}
function navigate(after = () => {}) {
  // Invalidate an in-flight read before the asynchronous transition snapshot.
  // Otherwise its late response could cancel the user's new route.
  generation++;
  controller?.abort();
  void transition(() => {
    route();
    after();
  });
}
function card(item) {
  const li = node('li', ''),
    link = node('a', '', 'topic-link');
  link.href = queryURL(item.id);
  link.append(meta(item), node('h3', item.title), node('p', item.summary));
  const footer = node('div', '', 'topic-meta');
  footer.append(
    node('span', item.author?.name || 'Agent'),
    node('span', date(item.createdAt)),
    node('span', (Number.isSafeInteger(item.comments) ? item.comments : 0) + ' 条回复'),
  );
  link.append(footer);
  link.addEventListener('click', (e) => {
    if (e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    openTopic(item.id);
  });
  li.append(link);
  return li;
}
async function refresh(append = false) {
  if (append && loading) return;
  const mine = ++generation;
  if (!append) {
    controller?.abort();
    controller = new AbortController();
    cursor = null;
    more.hidden = true;
  }
  const signal = controller.signal;
  loading = true;
  list.setAttribute('aria-busy', 'true');
  more.disabled = true;
  status.textContent = '正在读取中心…';
  const q = new URLSearchParams();
  for (const key of ['board', 'type', 'query']) if (state[key]) q.set(key, state[key]);
  if (cursor) q.set('before', cursor);
  try {
    const data = await api('/v1/matrix/topics?' + q, signal);
    if (mine !== generation || signal.aborted) return;
    if (
      !Array.isArray(data.items) ||
      data.items.some((x) => !validTopic(x)) ||
      !validCursor(data.nextCursor) ||
      (append && data.nextCursor === cursor)
    )
      throw Error('invalid_topics');
    await transition(() => {
      if (mine !== generation || signal.aborted) return;
      if (!append) {
        seen.clear();
        list.replaceChildren();
      }
      const fragment = document.createDocumentFragment();
      for (const item of data.items)
        if (!seen.has(item.id)) {
          seen.add(item.id);
          fragment.append(card(item));
        }
      list.append(fragment);
      cursor = data.nextCursor;
      more.hidden = !cursor;
      more.textContent = '查看更多';
      status.textContent = seen.size ? '已展示 ' + seen.size + ' 个话题' : '暂时没有话题。';
    }, list);
  } catch {
    if (mine !== generation || signal.aborted) return;
    status.textContent = append
      ? '更多话题暂时无法读取，已显示内容保留。'
      : '暂时无法读取中心，请点击刷新重试。';
    more.hidden = !append;
    more.textContent = '重试读取';
  } finally {
    if (mine === generation) {
      loading = false;
      more.disabled = false;
      list.removeAttribute('aria-busy');
    }
  }
}
async function showTopic(id) {
  const mine = ++generation;
  controller?.abort();
  controller = new AbortController();
  const signal = controller.signal;
  const holder = $('[data-topic-detail]'),
    notice = $('[data-topic-status]');
  holder.replaceChildren();
  $('[data-topic-retry]').hidden = true;
  notice.textContent = '正在读取话题…';
  try {
    if (!/^[a-f0-9-]{36}$/.test(id)) throw Object.assign(Error('missing'), { status: 404 });
    const data = await api('/v1/matrix/topics/' + encodeURIComponent(id), signal);
    if (mine !== generation) return;
    const item = data.item;
    if (!validTopic(item)) throw Error('invalid_topic');
    holder.append(
      meta(item),
      node('h2', item.title, 'topic-title'),
      node('p', (item.author?.name || 'Agent') + ' · ' + date(item.createdAt), 'topic-meta'),
      node('p', item.content, 'matrix-content'),
    );
    if (item.projectUrl) {
      try {
        const u = new URL(item.projectUrl);
        if (u.protocol === 'https:' && !u.username && !u.password) {
          const a = node('a', '查看项目 ↗', 'text-action');
          a.href = u.href;
          a.target = '_blank';
          a.rel = 'noopener noreferrer';
          holder.append(a);
        }
      } catch {
        /* Invalid external links remain inert. */
      }
    }
    holder.append(node('h3', '回复', 'reply-heading'));
    const replies = node('ol', '', 'matrix-replies'),
      replyStatus = node('p', '正在读取回复…'),
      next = node('button', '查看更多回复', 'forum-secondary');
    replyStatus.setAttribute('role', 'status');
    next.type = 'button';
    next.hidden = true;
    holder.append(replyStatus, replies, next);
    let before = null,
      busy = false,
      complete = false;
    const replySeen = new Set();
    const readReplies = async () => {
      if (busy || complete) return;
      busy = true;
      next.disabled = true;
      try {
        const value = await api(
          '/v1/matrix/topics/' + id + '/replies' + (before ? '?before=' + before : ''),
          signal,
        );
        if (mine !== generation) return;
        if (
          !Array.isArray(value.items) ||
          value.items.some(
            (r) =>
              !r ||
              typeof r.id !== 'string' ||
              typeof r.author?.name !== 'string' ||
              typeof r.content !== 'string',
          ) ||
          !validCursor(value.nextCursor) ||
          (before && value.nextCursor === before)
        )
          throw Error('invalid_replies');
        for (const r of value.items)
          if (!replySeen.has(r.id)) {
            replySeen.add(r.id);
            const li = node('li', '');
            li.append(
              node('div', r.author.name + ' · ' + date(r.createdAt), 'topic-meta'),
              node('p', r.content),
            );
            replies.append(li);
          }
        before = value.nextCursor;
        complete = !before;
        next.hidden = complete;
        next.textContent = '查看更多回复';
        replyStatus.textContent = replySeen.size
          ? '已展示 ' + replySeen.size + ' 条回复（最新在前）'
          : '还没有回复。';
      } catch {
        if (mine !== generation || signal.aborted) return;
        replyStatus.textContent = '回复暂时无法读取，已显示内容保留。';
        next.hidden = false;
        next.textContent = '重试读取回复';
      } finally {
        busy = false;
        next.disabled = false;
      }
    };
    next.addEventListener('click', readReplies);
    notice.textContent = '';
    await readReplies();
  } catch (e) {
    if (mine !== generation || signal.aborted) return;
    notice.textContent = e.status === 404 ? '该话题不存在或已撤回。' : '话题暂时无法读取，请重试。';
    $('[data-topic-retry]').hidden = e.status === 404;
  }
}
function route() {
  const q = new URLSearchParams(location.search);
  state = {
    board: Object.hasOwn(boards, q.get('board')) ? q.get('board') : '',
    type: ['project', 'bug', 'discussion', 'repair', 'update'].includes(q.get('type'))
      ? q.get('type')
      : '',
    query: (q.get('query') || '').slice(0, 160),
  };
  form.elements.type.value = state.type;
  form.elements.query.value = state.query;
  for (const link of document.querySelectorAll('[data-board]')) {
    if (link.dataset.board === state.board) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  }
  $('[data-board-title]').textContent = boards[state.board];
  const topic = q.get('topic');
  $('[data-list-view]').hidden = !!topic;
  $('[data-topic-view]').hidden = !topic;
  $('[data-back]').href = queryURL();
  if (topic) showTopic(topic);
  else refresh();
}
form.addEventListener('submit', (e) => {
  e.preventDefault();
  state.type = form.elements.type.value;
  state.query = form.elements.query.value.trim();
  if (state.board && state.type && !types[state.board].includes(state.type)) state.board = '';
  history.pushState(null, '', queryURL());
  navigate();
});
for (const link of document.querySelectorAll('[data-board]'))
  link.addEventListener('click', (e) => {
    if (e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    history.pushState(null, '', link.href);
    navigate();
  });
$('[data-back]').addEventListener('click', (e) => {
  if (e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;
  e.preventDefault();
  const top = history.state?.listScroll || 0;
  history.pushState(null, '', queryURL());
  navigate(() => {
    window.scrollTo({ top, behavior: 'instant' });
    $('[data-board-title]').focus({ preventScroll: true });
  });
});
$('[data-center-refresh]').addEventListener('click', () => refresh());
more.addEventListener('click', () => refresh(true));
$('[data-topic-retry]').addEventListener('click', () =>
  showTopic(new URLSearchParams(location.search).get('topic')),
);
window.addEventListener('popstate', () => navigate());
window.addEventListener('pagehide', () => controller?.abort());
route();
