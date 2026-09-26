import {esc, text, list, timestamp, taskRecordState} from './model.js';
import {createIntake} from './request-intake.js';

const STATES = new Set(['planned', 'doing', 'blocked', 'delivered', 'accepted', 'cancelled', 'superseded']);
const ORDER = {unassigned:0, blocked:1, review:2, expired:3, unconfirmed:4, doing:5, planned:6, accepted:7, cancelled:8, superseded:9, unknown:10};

// 中心状态只说明登记事实，不能推导原生会话已经收到或独立验收已经发生。
export function taskBucket(task, reachable = true) {
  if (!STATES.has(task.status)) return 'unknown';
  if (['accepted','cancelled','superseded'].includes(task.status)) return task.status;
  if (task.status === 'delivered') return 'review';
  if (task.status === 'blocked') return 'blocked';
  if (!text(task.owner)) return 'unassigned';
  return task.status === 'doing' ? taskRecordState(task, reachable) : 'planned';
}

export function dashboardTasks(snapshot, reachable = true) {
  const rows = new Map();
  for (const value of list(snapshot?.tasks)) {
    const id = text(value.id);
    if (!id) continue;
    const previous = rows.get(id);
    if (previous && Number(previous.version) > Number(value.version)) continue;
    rows.set(id, {...value, id, bucket:taskBucket(value, reachable)});
  }
  return [...rows.values()].sort((a,b) => ORDER[a.bucket] - ORDER[b.bucket] ||
    (timestamp(b.updated) || 0) - (timestamp(a.updated) || 0) || a.id.localeCompare(b.id));
}

export function createRequestDashboard({getSnapshot, getLanguage, getReachable, onChange, api, writeReady}) {
  let active = false, selectedId = '', category = 'all', query = '', project = 'all', limit = 24, sourcesOpen=false;
  const intake=createIntake({api,getSnapshot,getLanguage,writeReady,onChange});
  const L = (zh,en) => getLanguage() === 'zh' ? zh : en;
  const label = key => ({all:L('全部任务','All tasks'), unassigned:L('未接单','Unclaimed'),
    doing:L('有效接单','Valid claims'), expired:L('接单已过期','Claim expired'), unconfirmed:L('执行待核对','Execution unconfirmed'), blocked:L('阻塞','Blocked'), review:L('待验收','Awaiting review'),
    accepted:L('已验收','Accepted'), planned:L('待启动','Planned'), cancelled:L('已取消','Cancelled'), superseded:L('已替代','Superseded'), unknown:L('未知','Unknown')})[key] || key;
  const date = value => Number.isFinite(timestamp(value)) ? new Intl.DateTimeFormat(getLanguage()==='zh'?'zh-CN':'en-GB',
    {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(timestamp(value)) : L('未提供时间','Time not provided');
  const ownerName = (id,snapshot) => list(snapshot.agents).find(agent=>agent.id===id)?.name || text(id) || L('尚无负责人','No owner assigned');
  const projectName = (id,snapshot) => list(snapshot.projects).find(item=>item.id===id)?.name || text(id) || L('未提供项目','Project not provided');
  const pill = bucket => `<span class="rd-pill rd-${bucket}">${esc(label(bucket))}</span>`;
  const field = (title,value,extra='') => `<div${extra}><dt>${esc(title)}</dt><dd>${esc(value)}</dd></div>`;

  function detail(task,snapshot,live) {
    if (!task) return `<section class="panel rd-detail rd-empty"><h2>${esc(L('选择一项，查看交接与依据','Select a task to inspect its handoff'))}</h2><p>${esc(L('来源、负责人、交付和验收在同一处核对。','Inspect source, owner, delivery and review together.'))}</p></section>`;
    const lease = timestamp(task.lease_until), leaseExpired = Number.isFinite(lease) && lease > 0 && lease < Date.now() && task.status==='doing';
    const status = L({planned:'中心登记：计划中',doing:'中心原记录：doing（不代表正在执行）',blocked:'中心登记：受阻',delivered:'已交付，等待验收',accepted:'中心记录已验收',cancelled:'中心记录已取消',superseded:'中心记录已替代'}[task.status] || '中心未提供已知状态',
      {planned:'Planned in center',doing:'Center record: doing (not proof of execution)',blocked:'Blocked in center',delivered:'Delivered; review pending',accepted:'Acceptance recorded in center',cancelled:'Cancellation recorded in center',superseded:'Replacement recorded in center'}[task.status] || 'Unrecognized center state');
    return `<section class="panel rd-detail" id="rd-detail" tabindex="-1" data-focus="rd-detail" aria-labelledby="rd-detail-title">
      <div class="rd-detail-top"><button class="text-button" data-rd-back="${esc(task.id)}">← ${esc(L('返回任务列表','Back to tasks'))}</button><span class="rd-kicker">${esc(L('任务详情','TASK DETAILS'))}</span>${pill(task.bucket)}</div>
      <h2 id="rd-detail-title">${esc(task.title || task.id)}</h2><details class="task-record-meta" data-key="record:${esc(task.id)}" data-preserve-open><summary>${esc(L('记录信息','Record details'))}</summary><code class="rd-id">${esc(task.id)}</code></details>
      <div class="rd-ownership"><span class="rd-owner-mark" aria-hidden="true">${esc((ownerName(task.owner,snapshot) || '?').slice(0,1))}</span><div><small>${esc(L('负责人','Owner'))}</small><strong>${esc(ownerName(task.owner,snapshot))}</strong></div></div>
      <dl class="rd-fields">${field(L('项目','Project'),projectName(task.project,snapshot))}${field(L('执行范围','Scope'),task.scope || L('未提供','Not provided'))}${field(L('中心状态','Center state'),status)}${field(L('最近变更','Last update'),date(task.updated))}${field(L('任务版本','Task version'),Number.isInteger(task.version)?String(task.version):L('未知','Unknown'))}</dl>
      ${!live?`<p class="rd-notice">${esc(L('当前连接不可用，以上为最近一次记录。','Connection unavailable. These are the last recorded facts.'))}</p>`:''}
      ${task.bucket==='unconfirmed'?`<p class="rd-notice">${esc(L('尚无可核实的有效接单租约，不能据此判断当前正在执行。','No verifiable active claim lease; current execution is unconfirmed.'))}</p>`:''}
      ${leaseExpired?`<p class="rd-notice rd-alert">${esc(L('接单租约已过期，当前执行情况需要负责人确认。','The claim lease has expired. Confirm current work with the owner.'))}</p>`:''}
      <details class="task-record-meta" data-key="acceptance:${esc(task.id)}" data-preserve-open><summary>${esc(L('执行与验收依据','Execution and acceptance'))}</summary><div class="rd-checkpoints" aria-label="${esc(L('交付与验收','Delivery and review'))}">
        <div><span>${esc(L('原生执行回执','Runtime receipt'))}</span><strong class="rd-muted">${esc(L('未连接','Not connected'))}</strong><p>${esc(L('已领单或进行中不代表原会话已实际收到。','Claimed or in progress does not establish receipt by the runtime.'))}</p></div>
        <div><span>${esc(L('验收记录','Acceptance'))}</span><strong>${esc(task.status==='accepted' ? L('中心已登记验收','Acceptance recorded') : task.status==='delivered' ? L('等待独立验收','Awaiting independent review') : L('尚未验收','Not accepted'))}</strong><p>${esc(task.accepted_by ? L('验收人：','Reviewer: ')+ownerName(task.accepted_by,snapshot) : L('未提供验收人','Reviewer not provided'))}</p></div>
      </div>
      </details><div class="rd-evidence"><h3>${esc(L('交付与问题证据','Delivery and issue evidence'))}</h3>${text(task.evidence)?`<pre>${esc(task.evidence)}</pre>`:`<p class="rd-muted">${esc(L('尚未附交付或阻塞证据。','No delivery or blocker evidence attached.'))}</p>`}</div>
      <details class="rd-provenance" data-key="provenance:${esc(task.id)}" data-preserve-open><summary>${esc(L('来源与变更','Source and changes'))}</summary><dl class="rd-fields">${field(L('记录来源','Record source'),L('中心任务登记','Center task registry'))}${field(L('创建时间','Created'),date(task.created))}${field(L('需求原文与版本链','Request source and revisions'),L('请查阅上方需求记录','See request records above'))}${field(L('关联需求与拆分任务','Related request and subtasks'),L('以需求记录的明确关联为准','Use explicit links in request records'))}</dl></details>
      <button class="button subtle rd-task-record" data-detail="task" data-id="${esc(task.id)}">${esc(L('查看原始任务记录','Open original task record'))}</button>
    </section>`;
  }

  function render() {
    const snapshot=getSnapshot(), known=snapshot.known?.tasks===true;
    const live=getReachable() && snapshot.connection?.state==='online';
    const all=dashboardTasks(snapshot,live);
    const words=query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
    const scoped=all.filter(task=>(project==='all'||task.project===project) && words.every(word=>
      [task.id,task.title,task.scope,task.evidence,ownerName(task.owner,snapshot),projectName(task.project,snapshot)].map(text).join(' ').toLocaleLowerCase().includes(word)));
    const counts=Object.fromEntries(Object.keys(ORDER).map(key=>[key,scoped.filter(task=>task.bucket===key).length]));
    const filtered=scoped.filter(task=>category==='all'||task.bucket===category);
    if (!filtered.slice(0,limit).some(task=>task.id===selectedId)) selectedId='';
    const selected=filtered.find(task=>task.id===selectedId);
    return `<div class="rd-dashboard" data-request-dashboard><details class="task-sources" ${sourcesOpen?'open':''}><summary>${esc(L('需求来源与回执','Requests and receipts'))}</summary>${intake.render()}</details>
      <div class="rd-metrics" role="group" aria-label="${esc(L('按任务状态查看','Filter by task state'))}">${['all','doing','blocked','review','accepted'].map(key=>`<button class="rd-metric rd-${key} ${category===key?'selected':''}" data-rd-filter="${key}" data-focus="rd-filter-${key}" aria-pressed="${category===key}"><span>${esc(label(key))}</span><strong>${known?(key==='all'?scoped.length:counts[key]):'—'}</strong></button>`).join('')}</div>
      <div class="rd-toolbar"><label class="rd-search"><span class="sr-only">${esc(L('搜索需求与任务','Search requests and tasks'))}</span><input type="search" id="rd-search" data-focus="rd-search" value="${esc(query)}" placeholder="${esc(L('搜索任务、负责人、证据…','Search tasks, owners, evidence…'))}"></label><label class="select-wrap"><select id="rd-project" data-focus="rd-project" aria-label="${esc(L('筛选项目','Filter project'))}"><option value="all">${esc(L('全部项目','All projects'))}</option>${list(snapshot.projects).map(item=>`<option value="${esc(item.id)}" ${project===item.id?'selected':''}>${esc(item.name||item.id)}</option>`).join('')}</select></label><label class="select-wrap"><select id="rd-state" data-focus="rd-state" aria-label="${esc(L('筛选任务状态','Filter task state'))}">${['all',...Object.keys(ORDER)].map(key=>`<option value="${key}" ${category===key?'selected':''}>${esc(label(key))}</option>`).join('')}</select></label></div>
      <div class="rd-list-heading"><h2>${esc(L('任务','Tasks'))}<span>${known?filtered.length:'—'}</span></h2><small>${esc(live?L('中心记录 · 当前快照','Center records · current snapshot'):L('最近一次记录 · 待重新核对','Last recorded snapshot · needs reconnection'))}</small></div>
      ${!known?`<section class="panel rd-empty"><h3>${esc(L('任务数据尚未接通','Task data not connected'))}</h3><p>${esc(L('接口没有提供任务列表，不能判断当前是否有待办。','The endpoint has not supplied a task list. Pending work is unknown.'))}</p></section>`:filtered.length?`<div class="rd-layout"><div class="rd-list" aria-label="${esc(L('已登记任务','Registered tasks'))}">${filtered.slice(0,limit).map(task=>`<button class="rd-card ${selectedId===task.id?'selected':''}" data-key="task:${esc(task.id)}" data-rd-task="${esc(task.id)}" data-focus="rd-task-${encodeURIComponent(task.id)}" aria-pressed="${selectedId===task.id}"><div class="rd-card-top"><span>${esc(projectName(task.project,snapshot))}</span>${pill(task.bucket)}</div><h3>${esc(task.title||task.id)}</h3><div class="rd-card-foot"><strong>${esc(ownerName(task.owner,snapshot))}</strong><time>${esc(date(task.updated))}</time></div></button>`).join('')}${filtered.length>limit?`<button class="button subtle rd-more" data-rd-more data-focus="rd-more">${esc(L('加载更多任务','Load more tasks'))} · ${filtered.length-limit}</button>`:''}</div>${selected?detail(selected,snapshot,live):''}</div>`:`<section class="panel rd-empty"><h3>${esc(all.length?L('没有匹配的任务','No matching tasks'):L('中心尚未登记任务','No tasks registered in the center'))}</h3><p>${esc(all.length?L('调整项目、状态或搜索条件。','Adjust project, state or search filters.'):L('这不代表全部用户要求都已经处理。','This does not establish that every user request has been handled.'))}</p></section>`}
    </div>`;
  }

  document.addEventListener('toggle',event=>{if(event.target.matches?.('.task-sources')){sourcesOpen=event.target.open;if(sourcesOpen)intake.activate();}},true);
  const changed = () => {limit=24;onChange();};
  document.addEventListener('input',event=>{if(active&&event.target.id==='rd-search'){query=event.target.value;changed();}});
  document.addEventListener('change',event=>{
    if(!active)return;
    if(event.target.id==='rd-project'){project=event.target.value;changed();}
    if(event.target.id==='rd-state'){category=event.target.value;changed();}
  });
  document.addEventListener('click',event=>{
    if(!active)return;
    const button=event.target.closest('button');if(!button||button.disabled)return;
    if(button.dataset.rdFilter){category=category===button.dataset.rdFilter?'all':button.dataset.rdFilter;changed();}
    if(button.dataset.rdBack){const card=document.querySelector(`[data-rd-task="${CSS.escape(button.dataset.rdBack)}"]`);card?.focus({preventScroll:true});card?.scrollIntoView({block:'nearest',behavior:'instant'});}
    if(button.dataset.rdTask){selectedId=button.dataset.rdTask;onChange();const detail=document.getElementById('rd-detail');detail?.focus({preventScroll:true});detail?.scrollIntoView({block:'start',behavior:'instant'});}
    if(button.hasAttribute('data-rd-more')){limit+=24;onChange();}
  });
  return {render,activate(value){if(value&&!active){active=true;intake.activate();}else active=value;}};
}
