import {esc, text, list, timestamp, safeUrl} from './model.js';

const ID = /^[A-Za-z0-9_.:-]{1,100}$/;
const STATES = new Set(['waiting_user','decision_recorded','submitting','delivered_to_agent','resuming','resolved','failed','unknown','cancelled','expired']);
const CATEGORIES = new Set(['information','direction','authorization','environment']);
const object = value => value && typeof value === 'object' && !Array.isArray(value);
const validId = value => typeof value === 'string' && ID.test(value);
function validSchema(schema) {
  if(!object(schema))return false;
  if(!('questions' in schema))return typeof schema.allow_text==='boolean';
  const rows=schema.questions;
  return Array.isArray(rows)&&rows.length>0&&rows.length<=12&&new Set(rows.map(q=>q?.id)).size===rows.length&&rows.every(q=>object(q)&&validId(q.id)&&typeof q.label==='string'&&['multiple','required','allow_text'].every(k=>typeof q[k]==='boolean')&&Array.isArray(q.options)&&q.options.length<=12&&new Set(q.options.map(o=>o?.id)).size===q.options.length&&q.options.every(o=>object(o)&&validId(o.id)&&typeof o.label==='string'));
}
function validItem(item) {
  return object(item) && validId(item.id) && Number.isSafeInteger(item.version) && item.version > 0 &&
    STATES.has(item.state) && CATEGORIES.has(item.category) && ['question','recommendation','impact','resume_summary'].every(k=>typeof item[k]==='string' && item[k].trim()) &&
    Array.isArray(item.options) && item.options.every(x=>object(x) && validId(x.id) && typeof x.label==='string') &&
    new Set(item.options.map(x=>x.id)).size===item.options.length && validSchema(item.input_schema);
}
const recent = (value, limit) => Number.isFinite(timestamp(value)) && Date.now()-timestamp(value)<=limit && timestamp(value)-Date.now()<=60000;

export function createHumanCenter({api,getLanguage,writeReady,onChange}) {
  let feed=null,feedReachable=false,selected='',detail=null,detailReachable=false,detailReadAt=0,readSequence=0,loading=false;
  let capturedVersion=null,option='',draft='',reference=null,error='',receipt=null,submitting=false,checking=false;
  let answers={};
  const deferred = new Set();
  // Drafts stay in this page's memory while a request is collapsed. They are
  // deliberately not persisted to storage: a later version must pass the
  // existing CAS/version gate before it can be submitted.
  const deferredDrafts = new Map();
  let deferredNotice = false;
  const L=(zh,en)=>getLanguage()==='en'?en:zh;
  const encoded=value=>esc(text(value));
  const linked=value=>{
    const source=text(value);let result='',offset=0;
    for(const match of source.matchAll(/https?:\/\/[^\s<>"'，。；！？、）】》]+/gu)){
      const url=match[0].replace(/[.,;!?，。；！）、）]+$/u,''),safe=safeUrl(url);
      result+=esc(source.slice(offset,match.index));
      result+=safe?`<a class="hc-evidence-link" href="${esc(safe)}" target="_blank" rel="noopener noreferrer" referrerpolicy="no-referrer">${esc(url)}</a>`:esc(url);
      offset=match.index+url.length;
    }
    return result+esc(source.slice(offset));
  };
  const categories={information:['补充信息','Information'],direction:['方向选择','Direction'],authorization:['授权确认','Authorization'],environment:['环境协助','Environment']};
  const labels={waiting_user:['等待你的答复','Waiting for your answer'],decision_recorded:['决定已记录','Decision recorded'],submitting:['正在回传决定','Sending decision'],delivered_to_agent:['已送达原工位','Delivered to original workstation'],resuming:['原任务正在继续','Original task resuming'],resolved:['原任务已报告解决','Original task reports resolved'],failed:['回调或任务失败','Callback or task failed'],unknown:['结果尚未确认','Outcome unknown'],cancelled:['事项已撤销','Request cancelled'],expired:['事项已过期','Request expired']};
  const label=value=>labels[value]?L(...labels[value]):L('未知','Unknown');
  const status=(value,title='')=>`<span class="cc-status ${['failed'].includes(value)?'cc-bad':['resolved'].includes(value)?'cc-good':['resuming','delivered_to_agent'].includes(value)?'cc-live':'cc-wait'}">${esc(title||label(value))}</span>`;
  const connected=()=>feedReachable && feed?.available===true && feed?.stale===false && recent(feed.observed_at,120000);
  const pending=()=>receipt && ['unknown','submitting'].includes(receipt.state);
  const item=()=>detail?.item;
  const date=value=>Number.isFinite(timestamp(value))?new Date(timestamp(value)).toLocaleString(getLanguage()==='en'?'en-GB':'zh-CN',{hour12:false}):L('未提供','Unavailable');
  const questions=()=>item()?.input_schema?.questions;
  const hasAnswer=a=>Boolean(a?.selected_ids?.length||a?.text?.trim());
  const draftValid=()=>questions()?questions().some(q=>hasAnswer(answers[q.id]))&&questions().every(q=>{
    const a=answers[q.id]||{},chosen=a.selected_ids||[],body=a.text?.trim()||'';
    return (!q.required||hasAnswer(a))&&chosen.length<=(q.multiple?12:1)&&chosen.every(id=>q.options.some(o=>o.id===id))&&(!body||q.allow_text)&&[...body].length<=2000;
  }):Boolean((option && item()?.options.some(x=>x.id===option)) || (item()?.input_schema?.allow_text===true && draft.trim())) && [...draft.trim()].length<=2000;
  function canDecide() {
    const value=item(),latest=feed?.items.find(x=>x.id===selected);
    return connected() && detailReachable && detail?.available===true && detail.stale===false && Date.now()-detailReadAt<=30000 && value &&
      value.state==='waiting_user' && value.can_decide===true && value.fresh===true && value.binding_current===true && recent(value.observed_at,120000) &&
      Number.isFinite(timestamp(value.expires_at)) && timestamp(value.expires_at)>Date.now() && capturedVersion===value.version &&
      latest && latest.version<=value.version && writeReady() && !pending() && !submitting;
  }
  function remember(extra) {
    const query=new URLSearchParams(location.hash.split('?')[1]||'');
    for(const [key,value] of Object.entries(extra))value?query.set(key,value):query.delete(key);
    history.replaceState(null,'','#command?'+query);
  }
  async function readDetail(reset=false) {
    if(!validId(selected))return;
    const id=selected,sequence=++readSequence;loading=true;onChange();
    try {
      const value=await api('/api/human-requests/'+encodeURIComponent(id));
      if(sequence!==readSequence || id!==selected)return;
      if(value?.schema_version!==1 || !validItem(value.item) || value.item.id!==id || (item()?.id===id && value.item.version<item().version))throw new Error('invalid_human_detail');
      detail=value;detailReachable=true;detailReadAt=Date.now();error='';
      if(reset){capturedVersion=value.item.version;option='';draft='';answers={};}
    }catch{if(sequence===readSequence){detailReachable=false;error='detail';}}
    finally{if(sequence===readSequence){loading=false;onChange();}}
  }
  async function select(id,version=null) {
    if(!validId(id))return;
    const saved=deferredDrafts.get(id);
    deferred.delete(id);deferredNotice=false;
    selected=id;detail=null;detailReachable=false;capturedVersion=null;option='';draft='';answers={};reference=version;error='';
    remember({human:id});await readDetail(true);
    if(saved && selected===id && item()?.id===id){
      // Keep the captured version from when the draft was made. If the item
      // changed while it was deferred, detailMarkup exposes the review gate.
      capturedVersion=saved.capturedVersion;
      option=saved.option;draft=saved.draft;answers=saved.answers||{};
      onChange();
    }
  }
  function defer() {
    if(!selected||submitting||pending())return;
    deferredDrafts.set(selected,{capturedVersion,option,draft,answers:structuredClone(answers)});
    deferred.add(selected);selected='';detail=null;detailReachable=false;readSequence++;loading=false;
    option='';draft='';answers={};capturedVersion=null;reference=null;deferredNotice=true;
    remember({human:null});onChange();
  }
  function accept(value) {
    if(!object(value) || value.request_id!==receipt.id || value.human_id!==receipt.human || !['decision_recorded','unknown','submitting'].includes(value.state))throw new Error('invalid_human_receipt');
    if(value.callback_state!=null && !STATES.has(value.callback_state))throw new Error('invalid_human_callback');
    if(value.state==='decision_recorded' && (!validItem(value.item) || value.item.id!==receipt.human || value.item.callback_state!==value.callback_state))throw new Error('invalid_human_observation');
    if(receipt.state==='decision_recorded' && value.state!=='decision_recorded')throw new Error('regressed_human_receipt');
    if(value.item && receipt.result?.item && (value.item.version<receipt.result.item.version || (value.item.version===receipt.result.item.version && value.callback_state!==receipt.result.callback_state)))throw new Error('regressed_human_observation');
    receipt={...receipt,state:value.state,result:value,lookupFailed:false};
  }
  async function lookup() {
    if(!receipt || checking || submitting)return;
    checking=true;onChange();
    try{accept(await api('/api/human-decisions/'+encodeURIComponent(receipt.id)));}
    catch{receipt={...receipt,lookupFailed:true};}
    finally{checking=false;onChange();}
  }
  async function submit() {
    if(loading || !canDecide() || !draftValid())return;
    const selectedId=selected,version=capturedVersion,decision={};
    if(questions())decision.answers=questions().filter(q=>hasAnswer(answers[q.id])).map(q=>{
      const a=answers[q.id],row={question_id:q.id};if(a.selected_ids?.length)row.selected_ids=[...a.selected_ids];if(q.allow_text&&a.text?.trim())row.text=a.text.trim();return row;
    });
    else {if(option)decision.option_id=option;if(item().input_schema?.allow_text===true && draft.trim())decision.text=draft.trim();}
    const id='control-human-ui-'+crypto.randomUUID();
    receipt={id,human:selectedId,state:'submitting',result:null};submitting=true;
    remember({human:selectedId,human_decision:id,human_decision_for:selectedId});onChange();
    try{accept(await api('/api/human-requests/'+encodeURIComponent(selectedId)+'/decision',{request_id:id,expected_version:version,decision}));deferredDrafts.delete(selectedId);}
    catch{receipt={...receipt,state:'unknown'};}
    finally{submitting=false;onChange();if(selected===selectedId)await readDetail();}
  }
  function receiptMarkup() {
    if(!receipt)return '';
    const callback=receipt.result?.callback_state;
    return `<section class="hc-receipt" aria-live="polite"><h3>${esc(L('你的答复与后续执行','Your answer and subsequent execution'))}</h3><div class="hc-receipt-states"><div><span>${esc(L('决定保存','Decision storage'))}</span>${status(receipt.state,receipt.state==='submitting'?L('正在提交，尚未确认','Submitting; not confirmed'):'')}</div><div><span>${esc(L('原任务回调','Original task callback'))}</span>${status(callback||'unknown',callback==='decision_recorded'?L('等待回传到原任务','Waiting to return to the original task'):'')}</div></div><p class="cc-hint">${esc(receipt.state==='unknown'?L('保留这次答复的原请求，仅核对结果。不会自动再次提交。','Keep the original answer request and only check its result. It will not be resubmitted automatically.'):L('决定已保存、工位已收到和原任务已继续，是分别核实的进展。','Saving a decision, delivering it and resuming the original task are verified separately.'))}</p>${receipt.lookupFailed?`<p class="cc-notice">${esc(L('暂时无法核对最新回执，保留最后记录。','The latest receipt cannot be checked. The last record is retained.'))}</p>`:''}<code>${encoded(receipt.id)}</code><button class="button subtle" data-human-action="lookup" data-focus="human-lookup" ${checking||submitting?'disabled':''}>${esc(checking?L('正在核对','Checking'):L('核对这次答复','Check this answer'))}</button></section>`;
  }
  function questionMarkup() {
    return questions().map(q=>{const a=answers[q.id]||{};return `<fieldset class="hc-question" data-key="question:${encoded(q.id)}"><legend>${encoded(q.label)}${q.required?' *':''}</legend><div class="hc-options">${q.options.map((o,i)=>`<label class="hc-option ${(a.selected_ids||[]).includes(o.id)?'selected':''}"><input type="${q.multiple?'checkbox':'radio'}" name="human-q-${encoded(q.id)}" data-human-question="${encoded(q.id)}" value="${encoded(o.id)}" ${(a.selected_ids||[]).includes(o.id)?'checked':''}><span>${encoded(o.label)}</span></label>`).join('')}</div>${q.allow_text?`<label class="hc-answer-label" for="human-q-${encoded(q.id)}">${esc(L('文字答复','Written answer'))}</label><textarea id="human-q-${encoded(q.id)}" data-human-question="${encoded(q.id)}" rows="3" maxlength="2000">${esc(a.text||'')}</textarea>`:''}</fieldset>`;}).join('');
  }
  function answerRecord(value) {
    if(!value.decision)return '';
    if(Array.isArray(value.decision.answers))return value.decision.answers.map(a=>{const q=value.input_schema?.questions?.find(q=>q.id===a.question_id);return `<p><strong>${encoded(q?.label||a.question_id)}</strong><br>${(a.selected_ids||[]).map(id=>encoded(q?.options.find(o=>o.id===id)?.label||id)).join(' · ')}${a.text?`<br>${encoded(a.text)}`:''}</p>`;}).join('');
    return `<p>${encoded(value.options.find(x=>x.id===value.decision.option_id)?.label||value.decision.option_id)}${value.decision.text?`<br>${encoded(value.decision.text)}`:''}</p>`;
  }
  function detailMarkup() {
    if(!selected)return '';
    const value=item();
    if(!value)return `<div class="hc-detail"><p class="cc-notice">${esc(loading?L('正在读取对应事项…','Reading the request…'):L('尚未核实对应事项。请稍后更新，当前不会开放选择。','The request could not be verified. Refresh later; choices are unavailable.'))}<code>${encoded(selected)}</code></p></div>`;
    const allowed=canDecide(),versionChanged=capturedVersion!==value.version,expired=timestamp(value.expires_at)<=Date.now();
    const currentState=expired && value.state==='waiting_user'?'expired':value.callback_state||value.state;
    return `<article class="hc-detail" data-human-id="${encoded(value.id)}"><div class="cc-section-heading"><h3>${esc(L(...categories[value.category]))}</h3>${status(currentState)}</div><h4>${encoded(value.question)}</h4>
      ${reference!=null && reference!==value.version?`<p class="cc-notice">${esc(L('通知中的事项已有更新，以下显示重新核实的内容。','The notification has been updated. The current verified details are shown below.'))}</p>`:''}
      ${(!detailReachable || !connected())?`<p class="cc-notice">${esc(L('当前无法核实最新情况，保留上次内容，暂不能提交。','Current facts cannot be verified. Previous details are retained and submissions are paused.'))}</p>`:''}
      <dl class="hc-context"><div><dt>${esc(L('已知情况','Known facts'))}</dt><dd>${linked(value.facts||L('未提供补充情况','No additional facts supplied'))}</dd></div><div><dt>${esc(L('建议','Recommendation'))}</dt><dd>${linked(value.recommendation)}</dd></div><div><dt>${esc(L('影响范围','Impact'))}</dt><dd>${linked(value.impact)}</dd></div><div><dt>${esc(L('答复后继续','After your answer'))}</dt><dd>${linked(value.resume_summary)}</dd></div></dl>
      ${value.state==='waiting_user'?`<form id="human-decision-form"><fieldset ${!allowed?'disabled':''}><legend>${esc(L('你的选择','Your choice'))}</legend>${questions()?questionMarkup():`${value.options.length?`<div class="hc-options">${value.options.map((entry,index)=>`<label class="hc-option ${entry.id===option?'selected':''}"><input type="radio" name="human-option" value="${encoded(entry.id)}" data-focus="human-option-${index}" ${entry.id===option?'checked':''}><span>${encoded(entry.label)}</span></label>`).join('')}</div>`:''}${value.input_schema?.allow_text===true?`<label class="hc-answer-label" for="human-answer">${esc(L('补充答复','Written answer'))}</label><textarea id="human-answer" data-focus="human-answer" rows="3" maxlength="2000" placeholder="${esc(L('写下你的决定或补充信息…','Write your decision or additional information…'))}">${esc(draft)}</textarea>`:''}`}</fieldset><div class="hc-submit"><span class="cc-hint">${esc(L('按你的选择提交，不自动采用建议。','Your choice is submitted; recommendations are not selected automatically.'))}</span><button class="button subtle" type="button" data-human-action="defer" data-focus="human-defer" ${submitting||pending()?'disabled':''}>${esc(L('稍后处理','Handle later'))}</button><button class="button primary" type="submit" data-focus="human-submit" ${loading||!allowed||!draftValid()?'disabled':''}>${esc(L('提交答复','Submit answer'))}</button></div></form>`:''}
      ${versionChanged?`<p class="cc-notice">${esc(L('事项版本发生变化，请查看最新内容后重新选择。','The request changed. Review the latest details and make a new choice.'))}</p><button class="button subtle" data-human-action="review-latest" data-focus="human-review" ${loading||submitting?'disabled':''}>${esc(L('查看最新内容','Review latest details'))}</button>`:''}
      ${value.state==='waiting_user' && !allowed && !versionChanged && detailReachable && connected()?`<p class="cc-hint">${esc(expired?L('有效期已结束，不会自动采用任何选项。','The request expired. No option will be selected automatically.'):L('当前事项暂不能答复，等待任务连接与可用状态重新核实。','This request cannot currently be answered. Its task connection and availability must be verified.'))}</p>`:''}
      ${value.decision?`<div class="hc-answer-record"><strong>${esc(L('已记录的答复','Recorded answer'))}</strong>${answerRecord(value)}</div>`:''}
      <details class="hc-origin"><summary>${esc(L('来源与有效期','Source and expiry'))}</summary><dl>${[['project',L('项目','Project')],['seat_id',L('工位','Workstation')],['native_task_id',L('原任务','Original task')],['task_generation',L('任务轮次','Task generation')]].map(([key,name])=>`<dt>${esc(name)}</dt><dd>${encoded(value.origin?.[key]??L('未提供','Unavailable'))}</dd>`).join('')}<dt>${esc(L('有效至','Expires'))}</dt><dd>${esc(date(value.expires_at))}</dd><dt>${esc(L('版本','Version'))}</dt><dd>${value.version}</dd></dl></details></article>`;
  }
  function render() {
    const rows=feed?.items||[],waiting=rows.filter(x=>x.state==='waiting_user').length;
    return `<section class="cc-human panel" id="cc-human" tabindex="-1"><div class="cc-section-heading"><div><span class="cc-kicker">${esc(L('人类参与','HUMAN PARTICIPATION'))}</span><h2>${esc(L('需要你处理','Your attention'))}</h2></div>${status(connected()?'waiting_user':'unknown',connected()?`${waiting}${L(' 项等待答复',' awaiting answers')}`:L('尚未接通','Not connected'))}</div>
      ${!connected()?`<p>${esc(L('人工协作暂未连接，连接后可在这里处理需要你选择的事项。当前无法确认待办情况。','Human collaboration is not connected yet. Once connected, requests that need your choice will appear here. Pending requests cannot currently be confirmed.'))}</p>`:rows.length?'':`<p class="cc-hint">${esc(L('当前已核实，没有需要你处理的事项。','The current verified list has no requests for your attention.'))}</p>`}
      ${rows.length?`<div class="hc-list">${rows.map(value=>`<button class="hc-request ${value.id===selected?'selected':''}" data-human-action="select" data-id="${encoded(value.id)}" data-focus="human-select-${encoded(value.id)}" aria-pressed="${value.id===selected}"><span class="hc-category">${esc(L(...categories[value.category]))}</span><strong>${encoded(value.question)}</strong>${status(value.callback_state||value.state,deferred.has(value.id)&&value.state==='waiting_user'?L('稍后处理 · 仍待答复','Later · awaiting answer'):'')}</button>`).join('')}</div>`:''}
      ${deferredNotice?`<p class="hc-deferred-notice" role="status">${esc(L('事项已收起，仍保留在待办中；没有提交决定。点击事项即可继续查看和答复。','The request is collapsed and remains pending. No decision was submitted. Open it to continue reviewing and answering.'))}</p>`:''}${detailMarkup()}${receiptMarkup()}${error==='feed'?`<p class="cc-hint">${esc(L('无法读取最新人工事项，保留已核实的记录。','Current human requests could not be read. Verified records are retained.'))}</p>`:''}
      <p class="cc-hint hc-footer">${esc(L('普通执行失败与旧阻塞任务不会自动成为你的待办。','Execution failures and old blocked tasks are not automatically made into requests for you.'))}</p></section>`;
  }
  document.addEventListener('click',event=>{
    const button=event.target.closest('[data-human-action]');if(!button||button.disabled)return;
    if(button.dataset.humanAction==='select')void select(button.dataset.id);
    if(button.dataset.humanAction==='defer')defer();
    if(button.dataset.humanAction==='review-latest')void readDetail(true);
    if(button.dataset.humanAction==='lookup')void lookup();
  });
  document.addEventListener('input',event=>{
    const field=event.target,qid=field.dataset.humanQuestion;
    if(qid&&questions()?.some(q=>q.id===qid)){
      const q=questions().find(q=>q.id===qid),a=answers[qid]||{selected_ids:[],text:''};
      if(field.tagName==='TEXTAREA')a.text=field.value;
      else if(q.options.some(o=>o.id===field.value))a.selected_ids=q.multiple?(field.checked?[...new Set([...a.selected_ids,field.value])]:a.selected_ids.filter(id=>id!==field.value)):[field.value];
      answers[qid]=a;onChange();return;
    }
    if(event.target.name==='human-option'){option=event.target.value;onChange();}
    if(event.target.id==='human-answer'){draft=event.target.value;onChange();}
  });
  document.addEventListener('submit',event=>{if(event.target.id==='human-decision-form'){event.preventDefault();void submit();}});
  const saved=new URLSearchParams(location.hash.split('?')[1]||'');
  if(validId(saved.get('human')))selected=saved.get('human');
  if(validId(saved.get('human_decision'))&&validId(saved.get('human_decision_for')))receipt={id:saved.get('human_decision'),human:saved.get('human_decision_for'),state:'unknown',result:null};
  return {
    render,
    async update(raw,ok) {
      try{
        if(!ok||raw?.schema_version!==1||!Array.isArray(raw.items)||raw.items.some(x=>!validItem(x))||new Set(raw.items.map(x=>x.id)).size!==raw.items.length)throw new Error('invalid_human_feed');
        const prior=new Map((feed?.items||[]).map(x=>[x.id,x.version]));if(raw.items.some(x=>x.version<(prior.get(x.id)||0)))throw new Error('regressed_human_feed');
        feed=raw;feedReachable=true;if(error==='feed')error='';
      }catch{feedReachable=false;error='feed';}
      const reads=[];if(selected)reads.push(readDetail(capturedVersion==null));if(receipt)reads.push(lookup());await Promise.allSettled(reads);onChange();
    },
    async open(referenceValue) {
      if(validId(referenceValue?.id))await select(referenceValue.id,Number.isSafeInteger(referenceValue.version)?referenceValue.version:null);
      onChange();document.querySelector('#cc-human')?.focus({preventScroll:true});
    },
  };
}
