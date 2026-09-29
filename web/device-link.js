import { esc } from './model.js';

export function createLinkPanel({ request, onChange, onSelect }) {
  let state = { connections: [], installed: true },
    busy = false,
    error = '',
    selected = null;
  const draft = { name: '', code: '' },
    shares = new Map();
  function capture(target) {
    if (target?.closest('#link-pair-form') && target.name in draft)
      draft[target.name] = target.value;
    if (target?.dataset.share) shares.set(target.dataset.share, target.checked);
  }
  const rows = () => state.connections || [];
  async function refresh() {
    try {
      state = await request('/api/link');
    } catch {
      error = '连接信息暂时不可读取';
    }
    onChange();
  }
  function markup() {
    return `<section class="link-panel"><p>将 Link 部署到自己的电脑或私人服务器，使用一次性设备码连接。无需官方账号。</p>
      ${!state.installed ? '<p role="status">请安装包含 Link 的 Control 发行包。源码安装可配置 AIEYRA_LINK_BINARY。</p>' : ''}
      <p role="status">${esc(error || (busy ? '正在连接，请稍候…' : ''))}</p>
      <form id="link-pair-form"><label>本设备名称<input name="name" value="${esc(draft.name)}" maxlength="120" required autocomplete="off"></label><label>设备码<textarea name="code" maxlength="4096" required spellcheck="false" autocomplete="off">${esc(draft.code)}</textarea></label><button ${busy ? 'disabled' : ''}>连接设备</button></form>
      <button data-link="local" ${busy ? 'disabled' : ''}>查看本地办公室</button>
      ${rows()
        .map(
          (
            r,
          ) => `<article><h3>${esc(r.name)}</h3><p>${esc(r.hub_url)} · ${r.paired ? '已配对' : '配对未确认，保留原身份'}</p><p>${esc(r.error || '')}</p>
        ${
          !r.paired
            ? `<button data-link="retry" data-connection="${esc(r.id)}" ${busy ? 'disabled' : ''}>用上方设备码恢复配对</button>`
            : `<div class="link-actions"><button data-link="refresh" data-connection="${esc(r.id)}" ${busy ? 'disabled' : ''}>刷新远端办公室</button><button data-link="attach" data-connection="${esc(r.id)}" ${busy ? 'disabled' : ''}>${r.attached ? '更新本地共享' : '接入本地工位'}</button><label><input type="checkbox" data-share="${esc(r.id)}" ${(shares.get(r.id) ?? r.share_view) ? 'checked' : ''}>向已配对设备共享本地只读办公室</label>${r.attached ? `<button data-link="detach" data-connection="${esc(r.id)}" ${busy ? 'disabled' : ''}>停止本地共享</button>` : ''}</div>
        ${(r.services || [])
          .map((s) => {
            const g = (r.gateways || []).find((g) => g.service_id === s.id);
            return `<div class="link-service"><strong>${esc(s.name)}</strong><span>${s.state === 'online' ? '通信在线' : '离线或待同步'}</span><code>${esc(s.id)}</code><button data-link="connect" data-connection="${esc(r.id)}" data-service="${esc(s.id)}" ${busy ? 'disabled' : ''}>${g?.running ? '查看远端办公室' : '连接办公室'}</button>${g?.running ? `<p>本机 Agent 接入地址：<code>${esc(g.url)}</code></p><button data-link="disconnect" data-connection="${esc(r.id)}" data-service="${esc(s.id)}" ${busy ? 'disabled' : ''}>断开办公室</button>` : ''}</div>`;
          })
          .join('')}`
        }</article>`,
        )
        .join('')}
      <p>设备配对只建立通道。Agent 仍需远端办公室签发的凭据及原工位交接；不会自动取得领导权限。局域网与 Wi‑Fi 使用相同通道；USB 需要网络共享或已配置的端口转发。</p></section>`;
  }
  async function action(button, form) {
    if (busy) return;
    const action = button?.dataset.link || 'pair',
      connection = button?.dataset.connection,
      service = button?.dataset.service;
    if (action === 'local') {
      selected = null;
      onSelect(null);
      return;
    }
    const input = form || document.querySelector('#link-pair-form');
    const values = Object.fromEntries(new FormData(input));
    const share = document.querySelector(`[data-share="${connection}"]`)?.checked || false;
    busy = true;
    error = '';
    onChange();
    try {
      let result;
      if (action === 'pair' || action === 'retry') {
        result = await request('/api/link/pair', {
          name: action === 'retry' ? rows().find((r) => r.id === connection)?.name : values.name,
          code: values.code,
          ...(connection ? { connection } : {}),
        });
        draft.code = '';
        await request('/api/link/refresh', { connection: result.connection });
      } else if (action === 'attach')
        await request('/api/link/attach', { connection, share_view: share });
      else if (action === 'detach' || action === 'disconnect') {
        await request('/api/link/disconnect', { connection, ...(service ? { service } : {}) });
        if (selected?.connection === connection && selected?.service === service) {
          selected = null;
          onSelect(null);
        }
      } else if (action === 'refresh') await request('/api/link/refresh', { connection });
      else if (action === 'connect') {
        await request('/api/link/connect', { connection, service });
        selected = {
          connection,
          service,
          name:
            rows()
              .find((r) => r.id === connection)
              ?.services?.find((s) => s.id === service)?.name || '远端办公室',
        };
        onSelect(selected);
      }
    } catch (e) {
      const messages = {
        link_not_installed: '尚未安装 Link。',
        link_remote_view_not_shared: '远端尚未授权共享只读办公室。',
        link_result_unconfirmed_preserve_identity:
          '配对结果未确认。请保留当前连接并恢复配对，不要新建身份。',
        link_command_failed_check_connection: '连接未完成，请检查设备码是否过期、服务器是否可达。',
      };
      error = messages[e.code] || '连接暂未完成；原身份已保留，请检查服务器及连接状态。';
    } finally {
      busy = false;
      await refresh();
    }
  }
  return { markup, refresh, action, capture };
}
