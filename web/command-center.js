import {resolveMentions, mentionText} from './chat-mentions.js';
import {esc, text, list, timestamp, normalize, taskRecordLabel, runtimeDispatchReady, deliveryStates} from './model.js';
import {createHumanCenter} from './human-center.js';
import {createCommandScene, sceneSeats, visibleStation} from './command-scene.js';
import {stationSignals, channelTimeline, messageExcerpt} from './scene-signals.js';
import {focusVisible, returnFocus} from './focus.js';

const ACTIVE = new Set(['queued', 'running', 'cancelling']);
const TERMINAL = new Set(['completed', 'failed', 'cancelled']);
const object = value => value && typeof value === 'object' && !Array.isArray(value);
const ident = value => typeof value === 'string' && value.length > 0 && value.length <= 400;
const targetId = value => typeof value === 'string' && /^[A-Za-z0-9_.:-]{1,128}$/.test(value);

export function normalizeCollaboration(raw) {
  if (!object(raw) || raw.schema_version !== 1) throw new Error('unsupported_collaboration');
  for (const key of ['matrices', 'runtime_stations', 'tasks', 'goals']) {
    if (!Array.isArray(raw[key]) || raw[key].some(x => !object(x) || !ident(x.id))) throw new Error('invalid_collaboration');
    if (new Set(raw[key].map(x => x.id)).size !== raw[key].length) throw new Error('duplicate_collaboration_id');
  }
  return raw;
}

export function projectionRegressed(before, after) {
  if (!before) return false;
  const tasks = new Map(before.tasks.map(x => [x.id, x]));
  return after.tasks.some(x => {
    const old = tasks.get(x.id);
    return old && Number.isInteger(old.generation) && Number.isInteger(x.generation) &&
      (x.generation < old.generation || (x.generation === old.generation &&
        ((TERMINAL.has(old.state) && !TERMINAL.has(x.state)) || (old.acceptance === 'accepted' && x.acceptance !== 'accepted'))));
  });
}

export function sourceIsFresh(entity, reachable, now = Date.now()) {
  const time = timestamp(entity?.observed_at);
  return reachable && entity?.stale === false && Number.isFinite(time) && now - time <= 30000 && time - now <= 60000;
}

export function stationTask(snapshot, station) {
  return snapshot?.tasks.find(x => x.id === station?.id && x.task_id === station?.current_task_id && x.matrix_id === station?.matrix_id);
}

export function stationPermission(snapshot, station, action, reachable, capturedTarget = null) {
  const task = stationTask(snapshot, station);
  const matrix = snapshot?.matrices.find(x => x.matrix_id === station?.matrix_id);
  const target = station?.command_target;
  if (!sourceIsFresh(station, reachable) || !sourceIsFresh(task, reachable) || !sourceIsFresh(matrix, reachable) || matrix.available !== true) return false;
  if (!object(target) || !targetId(target.source_id) || !targetId(target.agent_id) || target.source_id !== matrix.id ||
      target.agent_id !== station.agent_id || !Number.isSafeInteger(target.generation) || target.generation < 1 || target.generation !== task.generation) return false;
  if (capturedTarget && ['source_id', 'agent_id', 'generation'].some(k => target[k] !== capturedTarget[k])) return false;
  const capability = action === 'agents.send' ? 'send' : action === 'agents.cancel' ? 'cancel' : null;
  if (!capability || matrix.capabilities?.['agent_' + capability] !== true || station.capabilities?.[capability] !== true || task.capabilities?.[capability] !== true) return false;
  return capability === 'cancel' ? station.active === true && ACTIVE.has(task.state) : station.active === false && !ACTIVE.has(task.state);
}

export function createCommandCenter({api, getLanguage, writeReady, onChange, onOpenPanel=()=>{}, getConnection=()=> 'unknown'}) {
  let projection = null, snapshot = null, registry = null, reachable = false, error = '', active = false, refreshing = false, lastRead = 0, cursor = 0;
  let selected = '', selectedMatrix = '', editor = null, draft = '', receipt = null, commandBusy = false, lookupBusy = false;
  let requestedMatrixCancel = null, scene = null, chatDraft = '', chatSending = false, chatPending = null, chatError = '';
  const expandedMessages=new Set();
  let followChat=true,unread=0,messagesObserved=false,messageSequence=0,channelMenu=false,membersOpen=false,chatCollapsed=false,chatScrollTop=0;
  const seenMessages=new Set();
  function observeMessages(messages){
    // Empty, replayed and older pages are observations, not incoming messages.
    let added=0;
    for(const message of messages){
      const id=text(message.id);if(!id)continue;
      const sequenced=Number.isSafeInteger(message.seq)&&message.seq>0;
      if(messagesObserved&&!seenMessages.has(id)&&(!sequenced||message.seq>messageSequence))added++;
      seenMessages.add(id);if(sequenced)messageSequence=Math.max(messageSequence,message.seq);
    }
    while(seenMessages.size>2048)seenMessages.delete(seenMessages.values().next().value);
    if(chatCollapsed||!followChat)unread+=added;
    messagesObserved=true;
  }
  try{chatCollapsed=localStorage.getItem('control-chat-collapsed')==='true';}catch{}
  const setChatCollapsed=value=>{const scroll=document.querySelector('[data-cc-scroll="chat"]');if(!chatCollapsed&&scroll){chatScrollTop=scroll.scrollTop;followChat=scroll.scrollHeight-scroll.clientHeight-scroll.scrollTop<30;}chatCollapsed=value;channelMenu=false;membersOpen=false;if(!value&&followChat)unread=0;try{localStorage.setItem('control-chat-collapsed',String(value));}catch{}onChange();if(!value){const next=document.querySelector('[data-cc-scroll="chat"]');if(next)next.scrollTop=followChat?next.scrollHeight:chatScrollTop;document.querySelector('#cc-chat-message')?.focus({preventScroll:true});}else document.querySelector('[data-cc-action="chat-expand-panel"]')?.focus({preventScroll:true});};
  const allRows=()=>stationSignals(sceneSeats(registry,snapshot,projection,snapshotReachable,reachable),snapshot,projection,humanFeed,getLanguage(),snapshotReachable);
  let panel='',snapshotReachable=false,registryReachable=false,chatTarget='all',chatReceipt=null,chatRevision=0,chatChecking=false,humanFeed=null,noticeDismissed='';
  const storageKey='aieyra-control-composer-v1';
  try { const saved=JSON.parse(sessionStorage.getItem(storageKey)||'null'); if(saved?.version===1){chatDraft=typeof saved.draft==='string'?saved.draft:'';chatTarget=typeof saved.target==='string'?saved.target:'all';chatRevision=Number.isSafeInteger(saved.revision)?saved.revision:0;if(saved.pending&&targetId(saved.pending.request_id)&&typeof saved.pending.body==='string')chatPending=saved.pending;} } catch {}
  const saveChat=()=>{try{sessionStorage.setItem(storageKey,JSON.stringify({version:1,draft:chatDraft,target:chatTarget,revision:chatRevision,pending:chatPending}));}catch{}};
  let panelReturnFocus=null;
  const openPanel=value=>{
    if(!panel)panelReturnFocus=returnFocus(document.activeElement);
    const changed=panel!==value;panel=value;if(value!=='station')selected='';channelMenu=false;membersOpen=false;onOpenPanel();onChange();
    if(changed){const body=document.querySelector('.inspector-body');if(body)body.scrollTop=0;}
    focusVisible('[data-cc-action="close-panel"]');
  };
  const dismissPanel=(restoreFocus=true)=>{if(!panel&&!selected)return false;panel='';selected='';onChange();if(restoreFocus)panelReturnFocus?.();panelReturnFocus=null;return true;};
  const dismissMenus=()=>{
    if(!channelMenu&&!membersOpen)return false;
    const origin=channelMenu?'channel-menu':'members';channelMenu=false;membersOpen=false;onChange();
    focusVisible(`[data-cc-action="${origin}"]`);return true;
  };
  const humanCenter = createHumanCenter({api,getLanguage,writeReady,onChange});
  const expanded = new Map();
  const L = (zh, en) => getLanguage() === 'en' ? en : zh;
  const names = {
    stored:['已存入群聊','Stored in group'],sending:['等待投递','Sending'],queue_submitting:['正在投递','Dispatching'],received:['工位已接收','Received'],interrupted:['本轮已中止','Interrupted'],discussion:['讨论','Discussion'],handoff:['交接','Handoff'],progress:['进展','Progress'],decision:['决定','Decision'],
    running:['执行中','Running'], queued:['等待执行','Queued'], cancelling:['取消进行中','Cancelling'], idle:['空闲','Idle'], offline:['离线','Offline'], paused:['暂停','Paused'],
    completed:['执行已结束','Execution ended'], failed:['执行失败','Failed'], cancelled:['已取消','Cancelled'], unknown:['未知','Unknown'],
    stale:['状态已过期','Stale'], pending:['待独立验收','Awaiting review'], accepted:['已独立验收','Accepted'], rejected:['验收未通过','Review rejected'],
    delivered:['已交付','Delivered'], submitted:['已提交','Submitted'], submitting:['正在提交','Submitting'], observed:['已核实回执','Receipt observed'],
    requested:['已请求，等待执行事实','Requested; awaiting observation'], waiting_user:['等待你的答复','Waiting for you'],
    decision_recorded:['决定已记录','Decision recorded'], delivered_to_agent:['已送达工位','Delivered to agent'], resuming:['正在恢复任务','Resuming'],
    resolved:['已解决','Resolved'], expired:['已过期','Expired'], passed:['通过','Passed'], tools:['调用工具','Using tools'],
    thinking:['思考中','Thinking'], streaming:['正在回答','Responding'], retrying:['等待重试','Waiting to retry'],
  };
  const label = value => names[value] ? L(...names[value]) : L('未知', 'Unknown');
  const tone = value => ['accepted','passed','resolved'].includes(value) ? 'good' : ['failed','rejected'].includes(value) ? 'bad' :
    ['running','tools','streaming'].includes(value) ? 'live' : ['queued','pending','cancelling','waiting_user','unknown','stale'].includes(value) ? 'wait' : 'quiet';
  const pill = (state, override = '') => `<span class="cc-status cc-${tone(state)}">${esc(override || label(state))}</span>`;
  const date = value => Number.isFinite(timestamp(value)) ? new Date(timestamp(value)).toLocaleTimeString(getLanguage() === 'en' ? 'en-GB' : 'zh-CN', {hour12:false}) : L('时间未提供','Time unavailable');
  const time = entity => `<span class="cc-time">${esc(L('观察于 ', 'Observed '))}${esc(date(entity?.observed_at))}</span>`;
  const fresh = entity => sourceIsFresh(entity, reachable);
  const empty = (title, detail = '') => `<div class="cc-empty"><strong>${esc(title)}</strong>${detail ? `<p>${esc(detail)}</p>` : ''}</div>`;
  const encoded = value => esc(text(value));
  const pending = () => receipt && ['unknown','submitting'].includes(receipt.state);
  const matrixKey = matrix => matrix.matrix_id || matrix.id;
  const currentMatrix = () => projection?.matrices.find(x => matrixKey(x) === selectedMatrix) || projection?.matrices[0];
  const getStation = id => projection?.runtime_stations.find(x => x.id === id);
  const terminalEvidence = task => list(task?.artifacts).filter(x => x.generation === task.generation);

  function stationState(station) {
    if (!fresh(station)) return 'stale';
    if (station.active === false) return 'idle';
    if (station.active === true && ACTIVE.has(station.execution_state)) return station.execution_state;
    return 'unknown';
  }

  function activityText(item) {
    return text(item.command) || text(item.title) || text(item.action) || text(item.tool) || L('未提供步骤说明','Step details unavailable');
  }

  function matrixPanel() {
    const matrix = currentMatrix();
    if (!matrix) return empty(L('尚未接入 Matrix','No Matrix connected'), L('接入运行中的会话后，这里会显示它的实际请求和工位。','Requests and workstations appear after a running session is connected.'));
    const goals = projection.goals.filter(x => x.matrix_id === matrix.matrix_id);
    const live = fresh(matrix) && matrix.available === true;
    return `<section class="cc-matrix panel"><div class="cc-section-heading"><div><span class="cc-kicker">MATRIX</span><h2>${encoded(matrix.name || matrix.id)}</h2></div>${pill(!live ? 'stale' : matrix.busy === true ? 'running' : matrix.busy === false ? 'idle' : 'unknown')}</div>
      <div class="cc-matrix-meta"><span>${encoded(matrix.model || L('模型未提供','Model unavailable'))}</span>${time(matrix)}<span>${encoded(matrix.project)}</span></div>
      <div class="cc-matrix-actions"><button class="button primary" data-cc-action="matrix-send" data-id="${encoded(matrix.matrix_id)}" ${!live || matrix.busy !== false || matrix.capabilities?.matrix_send !== true || !writeReady() || pending() ? 'disabled' : ''}>${esc(L('交给 Matrix','Send to Matrix'))}</button><span class="cc-hint">${esc(L('由它规划与委派，沿原请求查看结果。','Let Matrix plan and delegate. Follow results from the original request.'))}</span></div>
      ${goals.length ? `<details class="cc-goals" data-cc-details="goals:${encoded(matrix.matrix_id)}" ${expanded.get('goals:'+matrix.matrix_id) === false ? '' : 'open'} data-default-open="${expanded.get('goals:'+matrix.matrix_id) !== false}"><summary>${esc(L('请求与委派','Requests and delegation'))}<span>${goals.length}</span></summary><div class="cc-goal-list">${goals.map(goal => `<article class="cc-goal" data-goal-id="${encoded(goal.id)}"><div><code>${encoded(goal.request_id)}</code>${pill(live && goal.stale === false ? goal.state : 'stale')}</div><p>${esc(L('关联工位','Linked workstations'))} · ${(Array.isArray(goal.runtime_station_ids) ? goal.runtime_station_ids : []).map(id => getStation(id)?.title || id).map(encoded).join(' · ') || esc(L('未提供关联','No link provided'))}</p></article>`).join('')}</div></details>` : `<p class="cc-hint cc-space">${esc(L('尚无登记的外部请求；主会话输入不会自动变成这里的目标。','No registered external requests. Main-session input is not automatically projected here.'))}</p>`}
      ${matrix.has_more_agents === true ? `<p class="cc-notice">${esc(L('这里只展示最近 16 个工位，完整记录请回原会话查看。','Showing the latest 16 workstations. See the original session for the complete record.'))}</p>` : ''}</section>`;
  }

  function activityMarkup(task) {
    const items = list(task.activity?.items).filter(x => x.hidden !== true);
    if (!items.length) return empty(L('尚无工具过程','No tool activity yet'), L('只有执行器提供了具体步骤，才会显示在这里。','Steps appear when the executor reports them.'));
    return `<ol class="cc-steps">${items.map(item => {
      const key = `${task.task_id}:${item.id}`, isLive = fresh(task) && ACTIVE.has(item.state) && ACTIVE.has(task.state);
      const open = expanded.has(key) ? expanded.get(key) : isLive;
      const state = fresh(task) ? item.state : 'stale';
      const targets = Array.isArray(item.targets) ? item.targets.filter(x => typeof x === 'string').join('\n') : text(item.targets);
      let patch = '';
      if (item.patch) {
        const source = typeof item.patch === 'string' ? item.patch : JSON.stringify(item.patch, null, 2);
        patch = `<pre class="cc-patch">${source.split('\n').map(line => `<span class="${line.startsWith('+') ? 'cc-add' : line.startsWith('-') ? 'cc-delete' : ''}">${esc(line)}\n</span>`).join('')}</pre>`;
      }
      return `<li><details class="cc-step ${isLive ? 'is-live' : ''}" data-process-id="${encoded(item.id)}" data-cc-details="${encoded(key)}" data-default-open="${open}" ${open ? 'open' : ''}><summary><span class="cc-step-dot"></span><span class="cc-step-name"><strong>${encoded(item.tool || item.action || L('步骤','Step'))}</strong><span>${encoded(activityText(item))}</span></span>${pill(state)}${Number.isInteger(item.exit_code) ? `<span class="cc-exit ${item.exit_code === 0 ? 'cc-good' : 'cc-bad'}">exit ${item.exit_code}</span>` : ''}</summary>
      <div class="cc-step-body">${item.command ? `<pre class="cc-command-text">${encoded(item.command)}</pre>` : ''}${targets ? `<pre class="cc-targets">${esc(targets)}</pre>` : ''}${item.detail ? `<pre>${encoded(item.detail)}</pre>` : ''}${patch}</div></details></li>`;
    }).join('')}</ol>${task.activity?.has_more === true || task.activity?.has_older === true ? `<p class="cc-hint cc-space">${esc(L('显示最近一页步骤，完整历史保留在原会话。','Showing the latest page. Full history remains in the original session.'))}</p>` : ''}`;
  }

  function artifactsMarkup(items) {
    return items.length ? `<ul class="cc-artifacts">${items.map(item => `<li><span class="cc-file-icon" aria-hidden="true">↳</span><div><strong>${encoded(item.path)}</strong>${item.error ? `<p class="cc-bad">${encoded(item.error)}</p>` : ''}<details><summary>${esc(L('文件证据','File evidence'))}${typeof item.bytes === 'number' ? ` · ${item.bytes} B` : ''}</summary><code>${encoded(item.sha256 || L('未提供 SHA256','SHA256 unavailable'))}</code></details></div></li>`).join('')}</ul>` : `<p class="cc-hint">${esc(L('尚无本轮产物记录','No artifacts recorded for this turn'))}</p>`;
  }

  function taskPanel() {
    const station = getStation(selected), task = stationTask(projection, station);
    if (!station || !task) return `<section class="cc-task panel">${empty(L('选择工位，查看过程与结果','Select a workstation to inspect its work'), L('任务缺失时不会用另一轮的记录填充。','Missing tasks are not filled with records from another turn.'))}</section>`;
    const artifacts = terminalEvidence(task), reviews = list(task.reviews).filter(x => x.generation === task.generation);
    const state = fresh(task) ? task.state : 'stale';
    return `<section class="cc-task panel" data-current-task="${encoded(task.task_id)}"><div class="cc-section-heading"><div><span class="cc-kicker">${esc(L('过程与结果','PROCESS & RESULTS'))}</span><h2>${encoded(station.title || station.agent_id)}</h2></div>${pill(state)}</div>
      <div class="cc-task-meta"><span>${esc(L('第 ', 'Turn '))}${encoded(task.generation)}${esc(L(' 轮',''))}</span>${time(task)}<span>${encoded(station.model_id)}</span></div>
      ${!fresh(task) ? `<p class="cc-notice" role="status">${esc(L('当前状态无法核实。以下为最后一次记录，操作已暂停。','Current state cannot be verified. These are the last known records; commands are disabled.'))}</p>` : ''}
      ${task.error ? `<p class="cc-error" role="status">${encoded(task.error)}</p>` : ''}
      <div class="cc-task-actions"><button class="button subtle" data-cc-action="agent-send" data-id="${encoded(station.id)}" ${!writeReady() || pending() || !stationPermission(projection, station, 'agents.send', reachable) ? 'disabled' : ''}>${esc(L('安排下一项任务','Assign next task'))}</button><button class="button subtle cc-danger" data-cc-action="agent-cancel" data-id="${encoded(station.id)}" ${!writeReady() || pending() || !stationPermission(projection, station, 'agents.cancel', reachable) ? 'disabled' : ''}>${esc(L('取消本轮','Cancel this turn'))}</button></div>
      <div class="cc-work-section"><h3>${esc(L('工具过程','Tool activity'))}</h3>${activityMarkup(task)}</div>
      <div class="cc-output-grid"><section><h3>${esc(L('交付产物','Artifacts'))}${artifacts.length ? pill('delivered', `${L('已记录 ', 'Recorded ')}${artifacts.length}${L(' 个产物',' artifacts')}`) : ''}</h3>${artifactsMarkup(artifacts)}</section><section class="cc-review"><h3>${esc(L('独立验收','Independent review'))}${pill(task.acceptance || 'unknown')}</h3>
      ${Array.isArray(task.acceptance_criteria) && task.acceptance_criteria.length ? `<ul class="cc-criteria">${task.acceptance_criteria.filter(x => typeof x === 'string').map(x => `<li>${esc(x)}</li>`).join('')}</ul>` : ''}
      ${reviews.length ? reviews.map(review => `<article><p>${encoded(review.evidence || L('未附验收说明','No review explanation'))}</p>${list(review.checks).map(check => `<div class="cc-review-check">${pill(check.verdict)}<span>${encoded(check.evidence)}</span>${list(check.receipts).map(r => `<small>${esc(L('独立执行回执','Independent execution receipt'))} · ${encoded(r.agent_id)} / ${encoded(r.session_id)} · exit ${encoded(r.expected_exit)}</small>`).join('')}</div>`).join('')}</article>`).join('') : `<p class="cc-hint">${esc(L('执行结束不代表已经通过验收。','Execution ending does not establish acceptance.'))}</p>`}</section></div>
      ${list(task.history).length ? `<details class="cc-history" data-cc-details="history:${encoded(station.id)}" data-default-open="${expanded.get('history:'+station.id) === true}" ${expanded.get('history:'+station.id) === true ? 'open' : ''}><summary>${esc(L('之前的任务轮次','Earlier turns'))} · ${task.history.length}</summary>${list(task.history).map(old => `<article><h4>${esc(L('第 ', 'Turn '))}${encoded(old.generation)}${esc(L(' 轮',''))} ${pill(old.state)} ${pill(old.acceptance || 'unknown')}</h4>${artifactsMarkup(list(old.artifacts))}</article>`).join('')}</details>` : ''}
      <details class="cc-source"><summary>${esc(L('身份与来源','Identity and source'))}</summary><dl><dt>Matrix</dt><dd>${encoded(task.matrix_id)}</dd><dt>${esc(L('工位','Workstation'))}</dt><dd>${encoded(station.id)}</dd><dt>${esc(L('任务','Task'))}</dt><dd>${encoded(task.task_id)}</dd><dt>${esc(L('已分配工具','Assigned tools'))}</dt><dd>${(Array.isArray(station.assigned_tools) ? station.assigned_tools : []).map(encoded).join(' · ')}</dd></dl><p>${esc(L('这是 OS 运行时工位，和中心登记席位分别展示。','This is an OS runtime workstation, distinct from a registered center seat.'))}</p></details></section>`;
  }

  function editorAllowed() {
    if (!editor || !writeReady() || pending()) return false;
    if (editor.action.startsWith('agents.')) return stationPermission(projection, getStation(editor.stationId), editor.action, reachable, editor.target);
    const matrix = projection?.matrices.find(x => x.matrix_id === editor.matrixId);
    return fresh(matrix) && matrix.available === true && matrix.id === editor.sourceId &&
      (editor.action === 'matrix.send' ? matrix.capabilities?.matrix_send === true && matrix.busy === false :
        matrix.capabilities?.matrix_cancel === true && requestedMatrixCancel === editor.targetRequest);
  }

  function editorMarkup() {
    if (!editor) return '';
    const cancel = editor.action.endsWith('.cancel');
    return `<section class="cc-editor panel" aria-labelledby="cc-editor-title"><div class="cc-section-heading"><h2 id="cc-editor-title">${esc(cancel ? L('确认取消这个任务','Confirm cancellation') : L('下达明确任务','Give a concrete task'))}</h2><button class="button subtle" data-cc-action="close-editor">${esc(L('关闭','Close'))}</button></div><p>${encoded(editor.name)}${editor.target ? ` · ${esc(L('第 ', 'Turn '))}${editor.target.generation}${esc(L(' 轮',''))}` : ''}</p>
      ${!editorAllowed() ? `<p class="cc-error" role="status">${esc(L('目标轮次、状态或权限已变化。请关闭后重新选择当前任务。','The target turn, state or permissions changed. Close this panel and select the current task again.'))}</p>` : ''}
      <form id="cc-command-form">${cancel ? `<p class="cc-hint">${esc(L('提交取消请求后，仍需等待执行器确认取消结果。','The executor must confirm the result after cancellation is requested.'))}</p>` : `<label for="cc-message">${esc(L('任务内容','Task instructions'))}</label><textarea id="cc-message" data-focus="cc-message" ${commandBusy?'readonly':''} rows="4" maxlength="4000" placeholder="${esc(L('说明目标、范围和验收依据…','Describe the goal, scope and acceptance criteria…'))}">${esc(draft)}</textarea>`}<div class="cc-editor-actions"><span class="cc-hint">${esc(L('沿原运行时执行；未知结果只核对，不重发。','Uses the existing runtime. Unknown outcomes are queried, never replayed.'))}</span><button class="button primary" type="submit" ${!editorAllowed() || commandBusy ? 'disabled' : ''}>${esc(cancel ? L('提交取消请求','Request cancellation') : L('提交任务','Submit task'))}</button></div></form></section>`;
  }

  function receiptMarkup() {
    if (!receipt) return '';
    const result = receipt.result, observed = result?.observation;
    return `<section class="cc-receipt panel" aria-live="polite"><div class="cc-section-heading"><h2>${esc(L('指令回执','Command receipt'))}</h2>${pill(receipt.state)}</div><code>${encoded(receipt.id)}</code><p>${esc(receipt.state === 'unknown' ? L('结果尚未确认。原请求保留，仅查询这条指令的回执。','Outcome unknown. Keep the original request and only query its receipt.') : receipt.state === 'submitting' ? L('等待服务回执；尚不能确认执行。','Waiting for the service receipt; execution is not confirmed.') : L('指令回执与实际执行结果分别核对。','Command acknowledgement and execution results are checked separately.'))}</p>${observed ? `<p>${esc(L('实际观察','Observed execution'))} · ${pill(observed.state || 'unknown')}${Number.isInteger(observed.generation) ? ` · ${esc(L('第 ', 'Turn '))}${observed.generation}${esc(L(' 轮',''))}` : ''}</p>` : ''}
      <div class="cc-receipt-actions"><button class="button subtle" data-cc-action="lookup" ${lookupBusy || commandBusy ? 'disabled' : ''}>${esc(lookupBusy ? L('正在核对','Checking') : L('核对原请求','Check original request'))}</button>${result?.action === 'matrix.send' && receipt.state === 'observed' && ACTIVE.has(observed?.state) ? `<button class="button subtle cc-danger" data-cc-action="matrix-cancel">${esc(L('取消这条 Matrix 请求','Cancel this Matrix request'))}</button>` : ''}</div></section>`;
  }


  function acceptChat(result) {
    const value=result?.delivery||result;
    if(!chatPending||value?.id!==chatPending.request_id||value.target!==chatPending.target||value.body!==chatPending.body||!deliveryStates.has(value.state))return false;
    chatReceipt=value;
    if(value.state==='unknown'){chatError=L('请核对原请求，不重复发送。','Check the original request before sending again.');return true;}
    if(chatRevision===chatPending.revision&&chatDraft.trim()===chatPending.body){chatDraft='';chatRevision++;}
    chatPending=null;chatError='';saveChat();return true;
  }
  async function lookupChat(){
    if(!chatPending||chatChecking||chatSending)return;
    chatChecking=true;onChange();
    try { acceptChat(await api('/api/deliveries/'+encodeURIComponent(chatPending.request_id))); }
    catch {try{const value=await api('/api/snapshot');const found=list(value?.deliveries).find(d=>d.id===chatPending?.request_id);if(found)acceptChat(found);}catch{}}
    finally{chatChecking=false;onChange();}
  }
  function chatMarkup(){
    const timeline=channelTimeline(snapshot,chatReceipt),agents=list(snapshot?.agents);
    const allowed=snapshot?.capabilities?.chat===true&&writeReady()&&!chatPending&&!pending();
    const conn=getConnection(),waiting=list(humanFeed?.items).filter(h=>h.state==='waiting_user').length;
    return `<section id="chat-panel" class="cc-chat" ${chatCollapsed?'hidden':''} aria-labelledby="cc-chat-title"><div class="cc-chat-heading"><button class="chat-collapse" data-cc-action="chat-collapse" aria-label="${esc(L('折叠群聊','Collapse chat'))}" title="${esc(L('折叠群聊','Collapse chat'))}" aria-controls="chat-panel" aria-expanded="true">›</button><h2 id="cc-chat-title">${esc(L('群聊','Group chat'))}<i class="channel-dot ${esc(conn)}" title="${esc(conn==='online'?L('已连接','Connected'):L('连接待恢复','Connection unavailable'))}"></i></h2><div class="channel-actions"><button data-cc-action="members" aria-expanded="${membersOpen}" aria-label="${esc(L('群聊成员','Group members'))}">${esc(L('登记','Registered'))} ${agents.length}</button><button data-cc-action="channel-menu" aria-label="${esc(L('更多','More'))}" aria-expanded="${channelMenu}">···</button></div></div>
    ${conn!=='online'?`<button class="channel-connection-note" data-action="refresh">${esc(conn==='connecting'?L('正在连接…','Connecting…'):L('连接待恢复 · 点击重连','Connection unavailable · reconnect'))}</button>`:''}
    <div class="channel-popover" ${channelMenu?'':'hidden'}><button data-action="search-open">${esc(L('搜索','Search'))}<kbd>Ctrl K</kbd></button><button data-cc-action="open-human">${esc(L('待处理','Attention'))}<span>${waiting||''}</span></button><button data-cc-action="open-matrix">Matrix</button><button data-page="agents">${esc(L('工位信息','Workstations'))}</button><button data-action="cloud-open">${esc(L('账号与云服务','Account and cloud'))}</button><button data-action="language">中文 / English</button><button data-action="refresh">${esc(L('刷新','Refresh'))}</button></div>
    <div class="channel-members" ${membersOpen?'':'hidden'}><p>${esc(L('频道共享给已登记工位；成员数不代表在线或已读。','Shared with registered workstations; membership does not imply online or read.'))}</p>${agents.map(a=>`<button data-cc-action="member-select" data-id="${encoded(a.id)}"><span>${encoded(a.name||a.id)}</span><small>${esc(a.runtime?.bound===true?L('已绑定','Bound'):L('未绑定','Unbound'))}</small></button>`).join('')||empty(L('还没有登记工位','No workstations registered'))}</div>
    <div class="cc-chat-scroll" data-cc-scroll="chat">${timeline.map(entry=>{
      const m=entry.message,d=entry.delivery,body=text(m?.body||d?.body),preview=messageExcerpt(body,112),expanded=expandedMessages.has(entry.id),sender=m?.sender_name||m?.sender||L('你','You');
      return `<article class="cc-chat-message ${d?'has-delivery':''} ${expanded?'expanded':''}" data-key="${encoded(entry.id)}" data-message-id="${encoded(m?.id||'')}"><header><button class="message-author" title="${encoded(sender)}" data-cc-action="member-select" data-id="${encoded(m?.sender||d?.target||'')}">${encoded(sender)}</button><time>${esc(date(entry.at))}</time></header><p>${encoded(expanded?body:preview)}</p><div class="message-foot">${preview!==body?`<button class="chat-expand" data-cc-action="expand-message" data-id="${encoded(entry.id)}" aria-expanded="${expanded}">${esc(expanded?L('收起','Less'):L('全文','More'))}</button>`:''}${d?`<button class="message-receipt" data-detail="delivery" data-id="${encoded(d.id)}">${esc(d.state==='stored'?L('已入群聊','In channel'):d.state==='pending'?L('待投递','Pending'):label(d.state))}</button>`:''}</div>${d?.reply?`<details class="message-reply" data-key="reply:${encoded(entry.id)}" data-preserve-open><summary>${esc(L('工位答复','Reply'))}</summary><p>${encoded(d.reply)}</p><small>${esc(date(d.reply_at))} · ${esc(L('本轮结束，验收另行记录','Turn ended; acceptance is separate'))}</small>${d.reply_truncated?`<small>${esc(L('已截断，完整内容见原会话','Truncated; full reply in original session'))}</small>`:''}</details>`:''}</article>`;
    }).join('')}${!timeline.length?empty(L('和所有工位聊聊','Talk with your workstations')):''}</div>
    ${unread?`<button class="chat-catchup" data-cc-action="chat-bottom">↓ ${unread} ${esc(L('条新消息','new messages'))}</button>`:''}
    <form id="cc-chat-form" class="cc-chat-composer">${chatError&&!chatPending?`<p class="cc-error" role="alert">${esc(chatError)}</p>`:''}${chatPending?`<div class="cc-pending" role="status"><span>${esc(L('结果待确认','Outcome unknown'))}</span><button type="button" class="text-button" data-cc-action="chat-lookup" ${chatChecking||chatSending?'disabled':''}>${esc(L('核对原请求','Check request'))}</button><code>${encoded(chatPending.request_id)}</code></div>`:''}<label class="sr-only" for="cc-chat-message">${esc(L('群聊消息','Group message'))}</label><textarea id="cc-chat-message" data-focus="cc-chat-message" maxlength="4000" rows="2" placeholder="${esc(L('和大家说点什么，或 @领导…','Say something to everyone…'))}" >${esc(chatDraft)}</textarea><div class="cc-composer-route"><span class="cc-route-hint">${esc(L('@工位 · @领导','@workstation · @leader'))}</span><button class="cc-send" type="submit" aria-label="${esc(L('发送','Send'))}" ${!allowed||chatSending?'disabled':''}>${chatSending?'…':'↑'}</button></div></form></section>`;
  }
  function sceneMarkup(){
    return `<section id="command-scene" class="command-scene-viewport" data-command-scene-viewport data-scene-mode="2d"><div id="scene-canvas" data-persistent data-command-scene-canvas></div></section>`;
  }
  async function sendChat(){
    const body=chatDraft.trim();if(chatSending||chatPending||pending()||!body||!writeReady())return;
    if([...body].length>4000){chatError=L('消息最多4000字','Maximum 4000 characters');onChange();return;}
    const route=resolveMentions(body,snapshot,registry,snapshotReachable);
    if(route.error){const labels={mention_unknown:L('没有找到这个工位：','Unknown workstation: '),mention_ambiguous:L('存在同名或多位领导，请用 @工位编号：','Ambiguous name; use @workstation ID: '),mention_unavailable:L('这个工位当前未接入，草稿已保留：','Workstation unavailable; draft retained: '),mention_multiple:L('请每条消息只定向一个工位：','Mention one workstation per message: ')};chatError=labels[route.error]+route.name;onChange();return;}
    const target=route.target;
    if(snapshot?.capabilities?.chat!==true)return;
    chatTarget=target==='all'?'all':'agent:'+target;
    const payload={request_id:`control-command-${crypto.randomUUID()}`,target,body};
    chatPending={...payload,revision:chatRevision};chatSending=true;chatError='';saveChat();onChange();
    try {if(!acceptChat(await api('/api/chat',payload)))throw new Error('invalid_delivery');}
    catch(error){const rejected=error?.status>=400&&error.status<500&&!error.uncertain;if(rejected){chatPending=null;chatError=L('未提交：','Not submitted: ')+text(error.message);saveChat();}else chatError=L('请核对原请求，不重复发送。','Check the original request before sending again.');}
    finally{chatSending=false;onChange();await refresh();}
  }
  function targetAgent(id){const agent=list(snapshot?.agents).find(a=>a.id===id);if(!agent)return;chatDraft=mentionText(agent)+chatDraft;chatRevision++;chatTarget='all';panel='';selected='';panelReturnFocus=null;saveChat();setChatCollapsed(false);focusVisible('#cc-chat-message');}
  function stationMessage(row){
    const signal=row?.signal;
    return signal?`<div class="station-full-message"><small>${encoded(signal.label)} · ${esc(date(signal.at))}</small><p>${encoded(signal.fullText)}</p>${signal.humanId?`<button class="button primary" data-cc-action="open-station-human" data-id="${encoded(signal.humanId)}">${esc(L('答复','Respond'))}</button>`:''}</div>`:'';
  }
  function sceneSelection(){
    const rows=allRows(),row=rows.find(r=>r.id===selected||r.sourceId===selected);
    if(row?.source==='runtime')return `${stationMessage(row)}${taskPanel()}`;
    if(!row)return empty(L('选择一个工位','Select a workstation'));
    const agent=list(snapshot?.agents).find(a=>a.id===row.actor);
    return `<section class="cc-task"><div class="cc-section-heading"><h2>${encoded(row.name)}</h2>${pill(row.state)}</div>${stationMessage(row)}<p class="cc-hint">${encoded(row.project)} · ${encoded(row.source==='registry'?L('中心席位','Registered seat'):L('已登记 Agent','Registered agent'))}</p><dl class="cc-source"><dt>${esc(L('会话','Session'))}</dt><dd>${encoded(agent?.runtime?.thread_id||row.seat?.runtime_ref||L('未绑定','Unbound'))}</dd><dt>${esc(L('最后活动','Last activity'))}</dt><dd>${esc(date(agent?.runtime?.last_activity_at||row.observed))}</dd></dl><div class="cc-task-list">${row.tasks.map(t=>`<button class="cc-task-link" data-detail="task" data-id="${encoded(t.id)}"><strong>${encoded(t.title||t.id)}</strong><span>${encoded(taskRecordLabel(t,getLanguage(),snapshotReachable&&snapshot?.connection?.state==='online'))}</span></button>`).join('')||empty(L('暂无关联任务','No linked task'))}</div><button class="button primary" data-cc-action="target-agent" data-id="${encoded(row.actor)}" ${agent?.runtime?.bound!==true?'disabled':''}>${esc(L('@这个工位','@ this workstation'))}</button></section>`;
  }
  function render(){
    const scroller=document.querySelector('[data-cc-scroll="chat"]');if(!chatCollapsed&&scroller?.getClientRects().length)followChat=scroller.scrollHeight-scroller.clientHeight-scroller.scrollTop<30;
    const matrix=currentMatrix();if(!selectedMatrix&&matrix)selectedMatrix=matrixKey(matrix);
    const rows=allRows().filter(visibleStation),running=rows.filter(r=>['running','busy'].includes(r.state)).length;
    const waiting=list(humanFeed?.items).filter(h=>h.state==='waiting_user'),notice=waiting.map(h=>h.id+':'+h.version).join('|');
    const hasNotice=humanFeed?.available===true&&humanFeed.stale===false&&waiting.length>0&&noticeDismissed!==notice&&waiting.some(h=>!rows.some(r=>r.signal?.humanId===h.id));
    return `<div id="command-root" class="cc-root ${chatCollapsed?'chat-collapsed':''}" data-command-root>${sceneMarkup()}<div class="room-presence" title="${esc(L('已登记工位保留桌位；在线和执行以当前租约及运行回执为准','Registered desks remain visible; online and work counts require current observations'))}"><span>${rows.length} ${esc(L('工位','stations'))}</span><span>${rows.filter(r=>r.expanded===true||r.source==='runtime'&&r.state!=='unknown').length} ${esc(L('在线','online'))}</span>${running?`<span class="presence-working"><i></i>${running} ${esc(L('执行中','working'))}</span>`:''}</div><div class="scene-tools"><button class="scene-control" data-cc-action="scene-reset" aria-label="${esc(L('复位视角','Reset view'))}" title="${esc(L('复位视角','Reset view'))}">⌖</button></div>${chatMarkup()}${chatCollapsed?`<button class="chat-restore" data-cc-action="chat-expand-panel" aria-controls="chat-panel" aria-expanded="false">${esc(L('群聊','Chat'))}${unread?`<span>${unread}</span>`:''} <span aria-hidden="true">‹</span></button>`:''}<aside id="scene-inspector" class="scene-inspector" ${panel?'':'hidden'} aria-label="${esc(L('场景详情','Scene details'))}"><div class="inspector-heading"><span>${esc(panel==='human'?L('需要你处理','Your attention'):panel==='matrix'?'Matrix':L('工位详情','Workstation'))}</span><button class="icon-button" data-cc-action="close-panel" aria-label="${esc(L('关闭','Close'))}">×</button></div><div class="inspector-body">${panel==='human'?humanCenter.render():panel==='matrix'?`${projection?.matrices.length>1?`<select id="cc-matrix-select" aria-label="Matrix">${projection.matrices.map(m=>`<option value="${encoded(matrixKey(m))}" ${selectedMatrix===matrixKey(m)?'selected':''}>${encoded(m.name||'Matrix')} · ${encoded(matrixKey(m).slice(-8))}</option>`).join('')}</select>`:''}${matrixPanel()}${editorMarkup()}${receiptMarkup()}`:panel==='station'?`${sceneSelection()}${editorMarkup()}${receiptMarkup()}`:''}</div></aside>${hasNotice&&!panel?`<div class="human-toast" role="status"><button data-cc-action="open-human"><i></i><span>${waiting.length} ${esc(L('项需要你的决定','decisions need you'))}</span></button><button data-cc-action="defer-notice" aria-label="${esc(L('稍后处理','Later'))}">×</button></div>`:''}</div>`;
  }
  async function refresh() {
    if (refreshing) return;
    refreshing = true; onChange();
    try {
    const results = await Promise.allSettled([api('/api/collaboration'), api('/api/human-requests'), api('/api/snapshot'), api('/api/registry')]);
    try {
      if (results[0].status !== 'fulfilled') throw results[0].reason;
      const next = normalizeCollaboration(results[0].value);
      if (projectionRegressed(projection, next)) throw new Error('regressed_projection');
      projection = next; reachable = true; error = '';
    } catch (failure) { reachable = false; error = failure.status === 404 ? 'unavailable' : 'invalid'; }
    await humanCenter.update(results[1].status === 'fulfilled' ? results[1].value : null, results[1].status === 'fulfilled');
    humanFeed=results[1].status==='fulfilled'?results[1].value:null;
    snapshotReachable=false;registryReachable=results[3].status==='fulfilled';
    if (results[2].status === 'fulfilled') {
      try {const next=normalize(results[2].value);if(next.known.messages)observeMessages(next.messages);snapshot=next;snapshotReachable=true;}
      catch { /* Keep the last readable records; an invalid response is not a live observation. */ }
    }
    if (results[3].status === 'fulfilled' && results[3].value && typeof results[3].value === 'object') registry = results[3].value;
    if(chatPending){const found=list(snapshot?.deliveries).find(d=>d.id===chatPending.request_id);if(found)acceptChat(found);}
    } finally { refreshing = false; lastRead = Date.now(); onChange(); }
  }

  let ticking = false;
  async function tick() {
    if (!active || document.visibilityState !== 'visible' || ticking) return;
    ticking = true;
    try {
      let changed = false;
      try {
        const value = await api(`/api/events?after=${cursor}`);
        if (Array.isArray(value.events) && Number.isSafeInteger(value.next_cursor) && value.next_cursor >= cursor) {
          cursor = value.next_cursor;
          changed = value.events.some(x => ['collaboration.changed','human_request.changed','human_decision.changed'].includes(x.kind));
        }
      } catch { /* Full snapshots remain authoritative if the invalidation feed is unavailable. */ }
      if (changed || Date.now() - lastRead >= 10000) await refresh();
      else onChange();
    } finally { ticking = false; }
  }
  let timer = setInterval(tick, 3000);
  window.addEventListener('pagehide', () => {clearInterval(timer);timer=null;});
  window.addEventListener('pageshow', () => {if(!timer){timer=setInterval(tick,3000);void refresh();}});

  function openEditor(action, id) {
    if (pending()) return;
    if (action.startsWith('agents.')) {
      const station = getStation(id);
      if (!stationPermission(projection, station, action, reachable)) return;
      editor = {action, stationId:id, name:station.title || station.agent_id, target:{...station.command_target}, sourceId:station.command_target.source_id};
    } else {
      const matrix = projection?.matrices.find(x => x.matrix_id === id);
      if (!matrix) return;
      editor = {action, matrixId:id, sourceId:matrix.id, name:matrix.name || matrix.id};
      if (action === 'matrix.cancel') editor.targetRequest = requestedMatrixCancel;
    }
    draft = '';panel=action.startsWith('agents.')?'station':'matrix';onOpenPanel();onChange();
  }

  function rememberRequest(source, id) {
    // Only non-secret references are retained in the navigation URL, never the message or credentials.
    const query = new URLSearchParams(location.hash.split('?')[1] || '');
    query.set('source',source); query.set('request',id);
    history.replaceState(null, '', '#command?' + query);
  }

  function acceptReceipt(value) {
    if (!object(value) || value.request_id !== receipt.id || !['observed','unknown','submitting'].includes(value.state)) throw new Error('invalid_command_receipt');
    receipt = {...receipt, state:value.state, result:value};
  }

  async function submit() {
    if (commandBusy || !editorAllowed()) return;
    const cancel = editor.action.endsWith('.cancel'), message = draft.trim();
    if (!cancel && (!message || [...message].length > 4000)) return;
    const id = 'control-ui-' + crypto.randomUUID(), source = editor.sourceId;
    const command = {action:editor.action, request_id:id};
    if (editor.target) Object.assign(command, {agent_id:editor.target.agent_id, generation:editor.target.generation});
    if (cancel && editor.action === 'matrix.cancel') command.target_request_id = editor.targetRequest;
    if (!cancel) command.message = message;
    receipt = {id, source, state:'submitting', result:null}; commandBusy = true;
    rememberRequest(source, id); onChange();
    try { acceptReceipt(await api('/api/collaboration/command', {source_id:source, command})); editor = null; draft = ''; }
    catch { receipt = {...receipt, state:'unknown'}; }
    finally { commandBusy = false; onChange(); await refresh(); }
  }

  async function lookup() {
    if (!receipt || lookupBusy || commandBusy) return;
    lookupBusy = true; onChange();
    try { acceptReceipt(await api('/api/collaboration/command?' + new URLSearchParams({source_id:receipt.source, request_id:receipt.id}))); }
    catch { receipt = {...receipt, state:'unknown'}; }
    finally { lookupBusy = false; onChange(); }
  }

  document.addEventListener('click', event => {
    // The scene is inert behind an inspector; its uncovered backdrop receives the click.
    if(active&&panel&&event.target.id==='command-root'){dismissPanel(false);return;}
    if(active&&(channelMenu||membersOpen)&&!event.target.closest('.channel-actions,.channel-popover,.channel-members')){channelMenu=false;membersOpen=false;onChange();}
    const button = event.target.closest('[data-cc-action]');
    if (!button || button.disabled || !active) return;
    const action = button.dataset.ccAction, id = button.dataset.id;
    if(!['channel-menu','members'].includes(action))channelMenu=false;
    if (action === 'refresh') { void refresh(); return; }
    if(action==='chat-collapse'){setChatCollapsed(true);return;}
    if(action==='chat-expand-panel'){setChatCollapsed(false);return;}
    if(action==='channel-menu'){channelMenu=!channelMenu;membersOpen=false;onChange();if(channelMenu)focusVisible('.channel-popover button');return;}
    if(action==='members'){membersOpen=!membersOpen;channelMenu=false;onChange();if(membersOpen)focusVisible('.channel-members button');return;}
    if(action==='chat-bottom'){unread=0;const scroll=document.querySelector('[data-cc-scroll="chat"]');if(scroll)scroll.scrollTop=scroll.scrollHeight;onChange();return;}
    if(action==='member-select'){const row=allRows().find(r=>r.actor===id||r.actors?.includes(id));if(row){selected=row.id;membersOpen=false;openPanel('station');}return;}
    if(action==='expand-message'){expandedMessages.has(id)?expandedMessages.delete(id):expandedMessages.add(id);onChange();return;}
    if(action==='open-human'){openPanel('human');return;}
    if(action==='open-station-human'){const signal=allRows().find(row=>row.signal?.humanId===id)?.signal;if(signal){openPanel('human');void humanCenter.open({id:signal.humanId,version:signal.humanVersion});}return;}
    if(action==='open-matrix'){openPanel('matrix');return;}
    if(action==='close-panel'){dismissPanel();return;}
    if(action==='chat-lookup'){void lookupChat();return;}
    if(action==='defer-notice'){noticeDismissed=list(humanFeed?.items).filter(h=>h.state==='waiting_user').map(h=>h.id+':'+h.version).join('|');onChange();return;}
    if(action==='target-agent'){targetAgent(id);return;}
    if (action === 'lookup') { void lookup(); return; }
    if (action === 'select-station') { selected = id;openPanel('station');return; }
    if (action === 'select-scene') { selected = id;openPanel('station');return; }
    if (action === 'scene-reset') { scene?.reset(); return; }
    if (action === 'select-matrix') { selectedMatrix = id; selected = ''; onChange(); return; }
    if (action === 'close-editor') { editor = null; draft = ''; onChange(); return; }
    if (action === 'matrix-send') openEditor('matrix.send', id);
    if (action === 'agent-send') openEditor('agents.send', id);
    if (action === 'agent-cancel') openEditor('agents.cancel', id);
    if (action === 'matrix-cancel' && receipt?.result?.action === 'matrix.send' && receipt.state === 'observed') {
      requestedMatrixCancel = receipt.id;
      const matrix = projection?.matrices.find(x => x.id === receipt.source);
      if (matrix) openEditor('matrix.cancel', matrix.matrix_id);
    }
  });
  document.addEventListener('input', event => { if (event.target.id === 'cc-message') draft = event.target.value; });
  document.addEventListener('input', event => { if (event.target.id === 'cc-chat-message'){chatDraft = event.target.value;chatRevision++;saveChat();} });
  document.addEventListener('submit', event => { if (event.target.id === 'cc-command-form') { event.preventDefault(); void submit(); } });
  document.addEventListener('submit', event => { if (event.target.id !== 'cc-chat-form') return; event.preventDefault(); void sendChat(); });
  document.addEventListener('toggle', event => {
    const details = event.target;
    if (details.matches?.('[data-cc-details]') && String(details.open) !== details.dataset.defaultOpen) expanded.set(details.dataset.ccDetails, details.open);
  }, true);

  document.addEventListener('change',event=>{if(event.target.id==='cc-chat-target'){chatTarget=event.target.value;saveChat();onChange();}if(event.target.id==='cc-matrix-select'){selectedMatrix=event.target.value;onChange();}});
  document.addEventListener('keydown',event=>{if((event.ctrlKey||event.metaKey)&&event.key==='Enter'&&event.target.id==='cc-chat-message'&&!event.isComposing){event.preventDefault();void sendChat();}});
  const saved = new URLSearchParams(location.hash.split('?')[1] || '');
  if (targetId(saved.get('source')) && targetId(saved.get('request'))) receipt = {source:saved.get('source'), id:saved.get('request'), state:'unknown', result:null};
  return {
    render, refresh, dismissMenus, dismissPanel, isChatCollapsed:()=>chatCollapsed,showChat(){setChatCollapsed(false);},closePanel(){panel='';selected='';channelMenu=false;membersOpen=false;panelReturnFocus=null;},closeMenus(){channelMenu=false;membersOpen=false;},
    targetAgent,
    activate(value) {
      if (value && !active) { active = true; void refresh(); if (receipt){panel='matrix';void lookup();}if(chatPending)void lookupChat();if(saved.get('human'))panel='human'; }
      else active = value;
      const chatScroll=document.querySelector('[data-cc-scroll="chat"]');if(!chatCollapsed&&followChat&&chatScroll){chatScroll.scrollTop=chatScroll.scrollHeight;unread=0;}
      const viewport = document.querySelector('[data-command-scene-viewport]');
      const target = viewport?.querySelector('[data-command-scene-canvas]');
      if (viewport && target) {
        if (!scene) scene = createCommandScene();
        scene.mount(target, allRows().filter(visibleStation), {
          selected:panel==='station'?selected:'', language:getLanguage(), emptyUnavailable:!snapshotReachable&&!reachable,
          labels:{running:L('执行中','Running'),busy:L('忙碌','Busy'),queued:L('等待执行','Queued'),idle:L('空闲','Idle'),offline:L('离线','Offline'),paused:L('暂停','Paused'),waiting_user:L('等待','Waiting'),unknown:L('未知','Unknown')},
          onDeselect(){dismissPanel(false);},
          onSignal(row){if(row.signal?.humanId){openPanel('human');void humanCenter.open({id:row.signal.humanId,version:row.signal.humanVersion});}else{selected=row.source==='runtime'?row.sourceId:row.id;if(row.matrixId)selectedMatrix=row.matrixId;openPanel('station');}},
          onSelect(id){const row=allRows().find(r=>r.id===id),next=row?.source==='runtime'?row.sourceId:id;if(panel==='station'&&selected===next){dismissPanel();return;}selected=next;if(row?.matrixId)selectedMatrix=row.matrixId;openPanel('station');}
        });
      }
    },
    async openHuman(reference) {
      openPanel('human');await refresh(); await humanCenter.open(reference);
    },
  };
}
