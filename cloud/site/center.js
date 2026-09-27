const $ = (s) => document.querySelector(s);
const list = $('[data-center-topics]'),
  status = $('[data-center-status]'),
  more = $('[data-center-more]');
const projectOnly = document.body.dataset.page === 'share';
const seen = new Set();
const labels = {
  project: '项目',
  bug: '问题',
  discussion: '讨论与建议',
  repair: '修复',
  update: '官方更新',
  open: '待查看',
  triaged: '已分诊',
  in_progress: '处理中',
  resolved: '已解决',
  dismissed: '已说明',
};
let cursor = null,
  generation = 0,
  request = null;
const node = (tag, text, cls) => {
  const n = document.createElement(tag);
  n.textContent = text;
  if (cls) n.className = cls;
  return n;
};
async function api(path, signal) {
  const r = await fetch(path, { signal, credentials: 'same-origin', cache: 'no-store' });
  if (!r.ok) throw Error('请求未完成');
  return r.json();
}
async function details(item, holder, signal) {
  const p = await api('/v1/matrix/topics/' + encodeURIComponent(item.id), signal);
  if (!p.item || typeof p.item.content !== 'string') throw Error('无效话题');
  holder.replaceChildren();
  holder.append(node('p', p.item.content, 'matrix-content'));
  if (p.item.projectUrl) {
    const u = new URL(p.item.projectUrl);
    if (u.protocol === 'https:' && !u.username && !u.password) {
      const a = node('a', '查看项目', 'text-action');
      a.href = u.href;
      a.rel = 'noopener noreferrer';
      a.target = '_blank';
      holder.append(a);
    }
  }
  const replies = node('ol', '', 'matrix-replies');
  holder.append(replies);
  const button = node('button', '读取回复', 'text-action');
  button.type = 'button';
  holder.append(button);
  let before = null,
    busy = false;
  button.addEventListener('click', async () => {
    if (busy) return;
    busy = true;
    button.disabled = true;
    try {
      const data = await api(
        '/v1/matrix/topics/' +
          encodeURIComponent(item.id) +
          '/replies' +
          (before ? '?before=' + before : ''),
        signal,
      );
      if (
        !Array.isArray(data.items) ||
        data.items.some(
          (r) => !r.author || typeof r.author.name !== 'string' || typeof r.content !== 'string',
        )
      )
        throw Error('无效回复');
      for (const r of data.items) {
        const li = node('li', '');
        li.append(node('strong', r.author.name), node('p', r.content));
        replies.append(li);
      }
      before = data.nextCursor;
      button.hidden = !before;
      button.textContent = '更多回复';
      if (!data.items.length) replies.append(node('li', '还没有回复。'));
    } catch (e) {
      if (e.name !== 'AbortError') button.textContent = '读取失败，重试';
    } finally {
      busy = false;
      button.disabled = false;
    }
  });
}
async function refresh(append = false) {
  const mine = ++generation;
  if (!append) request?.abort();
  if (!append || !request) request = new AbortController();
  const signal = request.signal;
  if (!append) {
    cursor = null;
    seen.clear();
    list.replaceChildren();
    more.hidden = true;
  }
  more.disabled = true;
  status.textContent = '正在读取中心…';
  const values = new FormData($('.matrix-filter'));
  const q = new URLSearchParams();
  for (const k of ['type', 'query']) if (values.get(k)) q.set(k, values.get(k));
  if (projectOnly) q.set('type', 'project');
  if (cursor) q.set('before', cursor);
  try {
    const data = await api('/v1/matrix/topics?' + q, signal);
    if (mine !== generation) return;
    if (
      !Array.isArray(data.items) ||
      data.items.some(
        (item) =>
          !item ||
          typeof item.id !== 'string' ||
          typeof item.title !== 'string' ||
          typeof item.summary !== 'string',
      ) ||
      (data.nextCursor !== null && (!Number.isSafeInteger(data.nextCursor) || data.nextCursor <= 0))
    )
      throw Error('无效话题列表');
    for (const item of data.items) {
      if (seen.has(item.id) || (projectOnly && item.type !== 'project')) continue;
      seen.add(item.id);
      const li = node('li', ''),
        detail = document.createElement('details'),
        summary = document.createElement('summary'),
        content = node('div', '');
      summary.append(
        node(
          'span',
          (labels[item.type] || item.type) + ' · ' + (labels[item.state] || item.state),
          'feedback-meta',
        ),
        node('h2', item.title),
        node('p', item.summary),
      );
      if (item.author?.name) summary.append(node('span', item.author.name, 'feedback-meta'));
      detail.append(summary, content);
      li.append(detail);
      list.append(li);
      let loaded = false;
      detail.addEventListener('toggle', async () => {
        if (!detail.open || loaded) return;
        loaded = true;
        try {
          await details(item, content, signal);
        } catch (e) {
          loaded = false;
          if (e.name !== 'AbortError')
            content.replaceChildren(node('p', '读取失败，请收起后重试。'));
        }
      });
    }
    cursor = data.nextCursor;
    more.hidden = !cursor;
    more.textContent = '查看更多';
    status.textContent = list.children.length
      ? '已展示 ' + list.children.length + (projectOnly ? ' 个项目' : ' 个话题')
      : projectOnly
        ? '暂时没有项目分享。期待 Matrix 的第一份成果。'
        : '暂时没有话题。期待 Matrix 的第一条发现。';
  } catch (e) {
    if (e.name !== 'AbortError') {
      if (mine !== generation) return;
      status.textContent = append
        ? '更多话题暂时无法读取，已显示内容保留。'
        : '暂时无法读取，请使用查找重试。';
      more.hidden = !append;
      more.textContent = '重试读取';
    }
  } finally {
    if (mine === generation) more.disabled = false;
  }
}
$('.matrix-filter').addEventListener('submit', (e) => {
  e.preventDefault();
  refresh();
});
more.addEventListener('click', () => refresh(true));
window.addEventListener('pagehide', () => request?.abort());
refresh();
