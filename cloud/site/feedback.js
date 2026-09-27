const status = document.querySelector('[role=status]'),
  list = document.querySelector('[data-feedback-list]');
const labels = {
  received: '已受理',
  triaged: '已分诊',
  in_progress: '处理中',
  resolved: '已解决',
  rejected: '已关闭',
};
async function load() {
  document.querySelector('[data-feedback-retry]').disabled = true;
  try {
    const response = await fetch('https://ctrlupdate.aieyra.cn/v1/feedback', {
      credentials: 'include',
      cache: 'no-store',
      headers: { Accept: 'application/json' },
    });
    if (response.status === 401) {
      status.textContent = '登录后查看所属账号的私密反馈。';
      document.querySelector('[data-login]').hidden = false;
      list.replaceChildren();
      return;
    }
    if (!response.ok) throw Error('unavailable');
    const value = await response.json();
    if (!Array.isArray(value.tickets)) throw Error('invalid');
    list.replaceChildren();
    for (const ticket of value.tickets) {
      const item = document.createElement('li'),
        detail = document.createElement('details'),
        title = document.createElement('summary'),
        meta = document.createElement('p');
      title.textContent = ticket.report.title;
      meta.className = 'feedback-meta';
      meta.textContent = `${ticket.id} · ${labels[ticket.status] || '待核对'} · ${ticket.version}`;
      detail.append(title, meta);
      const names = {
        summary: '问题',
        steps: '复现',
        expected: '预期',
        actual: '实际',
        diagnostics: '脱敏诊断',
        fix_summary: '修复建议',
        verification: '验证',
      };
      for (const [key, label] of Object.entries(names)) {
        if (!ticket.report[key]) continue;
        const h = document.createElement('strong'),
          p = document.createElement('p');
        h.textContent = label;
        p.textContent = ticket.report[key];
        detail.append(h, p);
      }
      if (ticket.note) {
        const p = document.createElement('p');
        p.textContent = '维护回复：' + ticket.note;
        detail.append(p);
      }
      item.append(detail);
      list.append(item);
    }
    document.querySelector('[data-login]').hidden = true;
    status.textContent = value.tickets.length
      ? '仅你和维护人员可见 · 受理不代表已修复'
      : '暂无反馈。工位领导发现产品问题后可通过专用协议提交。';
  } catch {
    status.textContent = '暂时无法获取反馈，请稍后重试。';
  } finally {
    document.querySelector('[data-feedback-retry]').disabled = false;
  }
}
document.querySelector('[data-feedback-retry]').addEventListener('click', load);
document
  .querySelector('[data-login]')
  .addEventListener('click', () => sessionStorage.setItem('control-return', '/feedback'));
load();
