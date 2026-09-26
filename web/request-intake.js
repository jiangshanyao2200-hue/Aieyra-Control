import {esc, text, list, taskRecordLabel, runtimeDispatchReady, deliveryStates} from './model.js';

export function createIntake({api, getSnapshot, getLanguage, writeReady, onChange}) {
  let rows=[],cursor='',more=false,loaded=false,listBusy=false,listError='',busy=false,error='',selected=null,receipts=[],receiptCursor=0,receiptMore=false;
  let revisionBefore=null,revisionMore=false,reading=0,requestedId='',verified=false,draft='',draftRevision=0,target='',sending=false,delivery=null,sendError='',versionNote=false;
  let editSequence=0,failedRead='',automaticLookup='';
  // Unsent text lives only in this page, under its original requirement identity.
  const drafts=new Map();
  const L=(zh,en)=>getLanguage()==='en'?en:zh;
  const storage='control-requirement-dispatch';
  try{const saved=JSON.parse(sessionStorage.getItem(storage)||'null');if(saved?.id&&/^[A-Za-z0-9_.:-]{1,100}$/.test(saved.id))delivery={id:saved.id,state:'unknown'};}catch{}
  const persist=()=>{try{sessionStorage.setItem(storage,JSON.stringify(delivery&&delivery.state!=='rejected'?{id:delivery.id}:null));}catch{}};
  function rememberDraft(){if(selected)drafts.set(selected.id,{draft,target,version:selected.version,revision:draftRevision,versionNote});}
  function restoreDraft(value){
    const saved=drafts.get(value.id),sameVersion=saved?.version===value.version;
    draft=saved?.draft||'';target=sameVersion?saved.target:'';draftRevision=sameVersion?saved.revision:++editSequence;
    versionNote=Boolean(saved&&(!sameVersion||saved.versionNote));
  }
  function clearSubmitted(submission){
    if(!submission)return;
    const saved=drafts.get(submission.id);
    if(!saved||saved.version!==submission.version||saved.revision!==submission.revision||saved.target!==submission.target)return;
    const revision=++editSequence;drafts.set(submission.id,{...saved,draft:'',revision});
    if(selected?.id===submission.id&&selected.version===submission.version&&draftRevision===submission.revision){draft='';draftRevision=revision;}
  }
  const validReceipts=value=>value&&Array.isArray(value.items)&&value.control?.available!==false&&value.control?.stale!==true;
  const validRequest=value=>value&&typeof value.id==='string'&&/^[A-Za-z0-9_.:-]{1,100}$/.test(value.id)&&Number.isSafeInteger(value.version)&&value.version>0&&['active','cancelled','superseded'].includes(value.state);
  function invalidateSelection(){
    verified=false;requestedId=selected.id;failedRead='select';
    error=L('需求记录已变化，请核对最新版本。草稿已保留。','The request record changed. Review the latest version; your draft is preserved.');
  }
  async function refresh(append=false){
    if(listBusy)return;listBusy=true;listError='';onChange();
    try{const value=await api('/api/requirements?'+new URLSearchParams({limit:'24',after:append?cursor:''}));
      if(!validReceipts(value)||!value.items.every(validRequest)||new Set(value.items.map(x=>x.id)).size!==value.items.length)throw Error('invalid_requirements');
      rows=append?[...rows,...value.items.filter(x=>!rows.some(r=>r.id===x.id))]:value.items;cursor=value.next_cursor||'';more=value.has_more===true;loaded=true;
      const current=value.items.find(x=>x.id===selected?.id);if(selected&&current&&(current.version!==selected.version||current.state!==selected.state))invalidateSelection();
    }
    catch{listError=L('需求列表暂不可用，可稍后刷新。','Request list unavailable. Try refreshing later.');}
    finally{listBusy=false;onChange();}
  }
  async function select(id,history=false){
    const focusOrigin=document.activeElement;
    const reveal=!history&&selected?.id!==id&&focusOrigin?.matches?.('[data-intake="select"],[data-intake="retry"]');
    const sequence=++reading;rememberDraft();requestedId=id;busy=true;verified=false;error='';failedRead='';onChange();
    try{const query={id,history:'1'};if(history&&revisionBefore)query.before_version=String(revisionBefore);
      const receiptPath='/api/requirement-receipts?'+new URLSearchParams({id,limit:'30'});
      let [value,facts]=await Promise.all([api('/api/requirement?'+new URLSearchParams(query)),history?null:api(receiptPath)]);
      if(sequence!==reading)return;
      if(!validRequest(value)||value.id!==id||!Array.isArray(value.links)||value.control?.available===false||value.control?.stale===true||value.version<Math.max(drafts.get(id)?.version||0,rows.find(r=>r.id===id)?.version||0))throw Error('invalid_requirement');
      const changed=selected?.id!==id||selected?.version!==value.version;
      if(history&&!changed)value.revisions=[...list(selected.revisions),...list(value.revisions).filter(r=>!list(selected.revisions).some(old=>old.version===r.version))];
      if(history&&changed){facts=await api(receiptPath);if(sequence!==reading)return;}
      if((!history||changed)&&!validReceipts(facts))throw Error('invalid_receipts');
      if(changed){rememberDraft();restoreDraft(value);}
      selected=value;rememberDraft();
      if(!history||changed){receipts=list(facts.items);receiptCursor=facts.next_cursor||0;receiptMore=facts.has_more===true;}
      revisionBefore=value.next_before_version;revisionMore=value.revisions_has_more===true;verified=true;
    }catch{if(sequence===reading){failedRead='select';error=L('未能核实需求和版本，请重试。','Could not verify request and version. Try again.');}}
    finally{if(sequence===reading){busy=false;onChange();
      if(verified&&reveal&&document.activeElement===focusOrigin){
        const detail=document.querySelector('.intake-detail');
        if(detail?.getClientRects().length&&!detail.closest('[hidden],[inert]')){detail.focus({preventScroll:true});detail.scrollIntoView({block:'start',behavior:'instant'});}
      }
    }}
  }
  async function moreReceipts(){
    if(!selected||busy)return;const sequence=++reading,id=selected.id,version=selected.version;
    busy=true;error='';failedRead='';onChange();
    try{const result=await api('/api/requirement-receipts?'+new URLSearchParams({id,limit:'30',after:String(receiptCursor)}));
      if(sequence!==reading||selected?.id!==id||selected.version!==version)return;
      if(!validReceipts(result))throw Error('invalid_receipts');
      receipts.push(...list(result.items).filter(r=>!receipts.some(old=>old.id===r.id)));receiptCursor=result.next_cursor??receiptCursor;receiptMore=result.has_more===true;
    }catch{if(sequence===reading){failedRead='receipts';error=L('回执读取失败，已读内容保留，可重试。','Could not read receipts. Loaded entries are preserved; try again.');}}
    finally{if(sequence===reading){busy=false;onChange();}}
  }
  function routes(){return list(selected?.links).flatMap(link=>(Array.isArray(link.target_actors)?link.target_actors:[]).map(actor=>({task:list(selected.tasks).find(t=>t.id===link.task_id),actor,agent:list(getSnapshot().agents).find(a=>a.id===actor)}))).filter(row=>row.task);}
  const route=()=>routes().find(r=>r.task.id+'|'+r.actor===target);
  const pending=()=>delivery&&['unknown','submitting'].includes(delivery.state);
  const targetReady=agent=>runtimeDispatchReady(agent,writeReady()&&getSnapshot().connection?.state==='online');
  const allowed=()=>{const r=route();return verified&&!busy&&!sending&&!pending()&&writeReady()&&selected?.state==='active'&&targetReady(r?.agent)&&!['delivered','accepted','cancelled','superseded'].includes(r.task.status);};
  async function lookup(){
    if(!delivery||sending)return;sending=true;automaticLookup=delivery.id;sendError='';onChange();try{const value=await api('/api/requirement-dispatch?'+new URLSearchParams({request_id:delivery.id}));if(value.delivery?.id!==delivery.id||!deliveryStates.has(value.delivery.state))throw Error('wrong_receipt');delivery={...delivery,state:value.delivery.state,result:value};if(!pending())clearSubmitted(delivery.submission);persist();}catch{delivery={...delivery,state:'unknown'};sendError=L('暂时无法核对投递结果，请稍后核对原请求。','Dispatch result unavailable. Check the original request again later.');}finally{sending=false;onChange();}
  }
  async function submit(){
    if(!allowed()||!draft.trim()||[...draft.trim()].length>4000)return;const r=route(),submission={id:selected.id,version:selected.version,revision:draftRevision,target};rememberDraft();
    const payload={request_id:'control-req-'+crypto.randomUUID(),requirement_id:selected.id,requirement_version:selected.version,task_id:r.task.id,task_version:r.task.version,target_actor:r.actor,target_thread_id:r.agent.runtime.thread_id,body:draft.trim()};
    delivery={id:payload.request_id,state:'submitting',submission};sending=true;sendError='';persist();onChange();
    try{const value=await api('/api/requirement-dispatch',payload);if(value.delivery?.id!==payload.request_id||!deliveryStates.has(value.delivery.state))throw Error('invalid_delivery');delivery={...delivery,state:value.delivery.state,result:value};if(!pending())clearSubmitted(submission);}
    catch(e){const rejected=e.status>=400&&e.status<500&&!e.uncertain;delivery={...delivery,state:rejected?'rejected':'unknown'};sendError=rejected?L('未提交：','Not submitted: ')+text(e.message):L('结果未知，请核对原请求','Outcome unknown; check original request');}
    finally{sending=false;persist();onChange();}
  }
  const source=value=>`<dl class="intake-source"><dt>${esc(L('来源','Source'))}</dt><dd>${esc(value?.ref||L('未提供','Not supplied'))}</dd><dt>${esc(L('来源修订','Source revision'))}</dt><dd>${esc(value?.revision)}</dd><dt>SHA256</dt><dd><code>${esc(value?.sha256)}</code></dd></dl>`;
  function render(){
    return `<section class="intake"><div class="intake-heading"><h3>${esc(L('需求来源与回执','Requests and receipts'))}</h3><button class="button subtle" data-intake="refresh" ${listBusy?'disabled':''}>${esc(L('刷新','Refresh'))}</button></div>${listError?`<p role="status" class="cc-notice" data-key="intake-list-error">${esc(listError)}</p>`:''}${error?`<p role="status" class="cc-notice" data-key="intake-read-error">${esc(error)} <button class="button subtle" data-intake="retry" ${busy?'disabled':''}>${esc(L('重试','Retry'))}</button></p>`:''}${busy?`<p class="cc-hint" role="status" data-key="intake-loading">${esc(L('正在读取，请稍候…','Loading, please wait…'))}</p>`:''}<div class="intake-list" data-key="intake-list">${rows.map(r=>`<button data-key="request:${esc(r.id)}" data-intake="select" data-id="${esc(r.id)}" aria-pressed="${selected?.id===r.id}" class="intake-row ${selected?.id===r.id?'selected':''}"><strong>${esc(r.summary||r.id)}</strong><small>${esc(r.project)} · v${esc(r.version)} · ${esc(r.state)}</small></button>`).join('')}${loaded&&!rows.length?`<p class="cc-hint">${esc(L('暂无登记需求','No registered requests'))}</p>`:''}${more?`<button class="button subtle" data-intake="more" ${listBusy?'disabled':''}>${esc(L('更多需求','More requests'))}</button>`:''}</div>${selected?`<article class="intake-detail" tabindex="-1" data-key="intake:${esc(selected.id)}:${selected.version}" aria-busy="${busy}"><h3>${esc(selected.summary)}</h3><code>${esc(selected.id)} · v${selected.version}</code>${source(selected.source)}<details data-preserve-open data-key="source:${esc(selected.id)}:${selected.version}"><summary>${esc(L('原始出处与修订','Original source and revisions'))}</summary>${source(selected.original_source)}${list(selected.revisions).map(r=>`<div class="intake-revision"><strong>v${esc(r.version)}</strong><p>${esc(r.summary)}</p>${source(r.source)}</div>`).join('')}${revisionMore?`<button class="button subtle" data-intake="history" ${busy?'disabled':''}>${esc(L('更早修订','Earlier revisions'))}</button>`:''}</details><h4>${esc(L('关联任务与指定工位','Linked tasks and targets'))}</h4>${list(selected.links).map(link=>{const task=list(selected.tasks).find(t=>t.id===link.task_id);return `<div class="intake-link"><strong>${esc(task?.title||link.task_id)}</strong><small>${esc(link.relation)} · v${esc(task?.version)} · ${esc(taskRecordLabel(task,getLanguage(),getSnapshot()?.connection?.state==='online'))}</small><p>${esc(L('指定：','Target: '))}${esc(link.target_label||link.target_actors?.join(', '))}<br>${esc(L('实际负责人：','Claim owner: '))}${esc(task?.owner||L('未接单','Unclaimed'))}</p>${task?.evidence?`<pre>${esc(task.evidence)}</pre>`:''}</div>`;}).join('')}<details open data-preserve-open data-key="receipts:${esc(selected.id)}:${selected.version}"><summary>${esc(L('执行与验收回执','Execution and review receipts'))} · ${receipts.length}</summary>${receipts.map(r=>`<div data-key="receipt:${esc(r.id)}" class="intake-fact ${r.retraction?'retracted':''}"><strong>${esc(r.kind)}</strong><small>${esc(r.target_actor)} · ${esc(r.native_turn_id||r.delivery_id)}</small><p>${esc(r.detail||r.body||r.evidence_ref)}</p>${r.retraction?`<p>${esc(L('已撤回：','Retracted: '))}${esc(r.retraction.reason)}</p>`:''}</div>`).join('')||`<p class="cc-hint">${esc(L('尚无执行回执','No execution receipt'))}</p>`}${receiptMore?`<button class="button subtle" data-intake="receipts" ${busy?'disabled':''}>${esc(L('更多回执','More receipts'))}</button>`:''}</details><form id="intake-dispatch">${!verified?`<p class="cc-hint" data-key="intake-unverified">${esc(L('当前显示上次读取的记录，核验成功后才能提交。','Showing the previous record. Verify it before submitting.'))}</p>`:''}${versionNote?`<p class="cc-hint" data-key="intake-version">${esc(L('需求版本已更新。草稿已保留，请核对正文并重新选择任务与工位。','The request version changed. Your draft is preserved; review it and choose the task and workstation again.'))}</p>`:''}<h4>${esc(L('沿原任务投递','Dispatch original task'))}</h4><select id="intake-target" aria-label="${esc(L('指定任务与工位','Task and workstation'))}"><option value="">${esc(L('选择已绑定工位','Select a bound workstation'))}</option>${routes().map(r=>`<option value="${esc(r.task.id+'|'+r.actor)}" ${target===r.task.id+'|'+r.actor?'selected':''} ${!targetReady(r.agent)?'disabled':''}>${esc(r.task.title||r.task.id)} → ${esc(r.agent?.name||r.actor)}</option>`).join('')}</select><textarea id="intake-message" maxlength="4000" rows="3" aria-label="${esc(L('任务正文','Task instructions'))}" placeholder="${esc(L('完整任务与验收要求','Task and acceptance criteria'))}">${esc(draft)}</textarea><button class="button primary" type="submit" ${!allowed()?'disabled':''}>${esc(L('提交原任务','Submit task'))}</button></form></article>`:''}${delivery?`<div class="intake-delivery" role="status"><strong>${esc(L('投递状态','Dispatch status'))} · ${esc(delivery.state)}</strong><code>${esc(delivery.id)}</code>${sendError?`<p>${esc(sendError)}</p>`:''}<button class="button subtle" data-intake="lookup" ${sending?'disabled':''}>${esc(L('核对原请求','Check request'))}</button></div>`:''}</section>`;
  }
  document.addEventListener('click',e=>{const b=e.target.closest('[data-intake]');if(!b||b.disabled)return;const a=b.dataset.intake;if(a==='refresh')void refresh();if(a==='more')void refresh(true);if(a==='select')void select(b.dataset.id);if(a==='history'&&selected)void select(selected.id,true);if(a==='receipts')void moreReceipts();if(a==='lookup')void lookup();if(a==='retry'){if(failedRead==='receipts')void moreReceipts();else if(requestedId)void select(requestedId);}});
  document.addEventListener('input',e=>{if(e.target.id==='intake-message'){draft=e.target.value;draftRevision=++editSequence;rememberDraft();}});
  document.addEventListener('change',e=>{if(e.target.id==='intake-target'){target=e.target.value;draftRevision=++editSequence;versionNote=false;rememberDraft();onChange();}});
  document.addEventListener('submit',e=>{if(e.target.id==='intake-dispatch'){e.preventDefault();void submit();}});
  return {render,activate(){if(!loaded&&!listBusy&&!listError)void refresh();if(delivery?.state==='unknown'&&!sending&&automaticLookup!==delivery.id)void lookup();}};
}
