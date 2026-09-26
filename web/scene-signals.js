import {list, text, timestamp} from './model.js';

export const excerpt = (value, limit=64) => {
  const line=text(value).replace(/\s+/g,' ').trim();
  return [...line].length>limit?[...line].slice(0,limit-1).join('')+'…':line;
};
// Routing prefixes are useful in the original, but add no progress to a short preview.
export const messageExcerpt=(value,limit=64)=>{
  const original=text(value).trim(),body=original.replace(/^(?:@[A-Za-z0-9_.:/-]+\s+)+/,'');
  return excerpt(body||original,limit);
};
const time=value=>Number.isFinite(timestamp(value))?timestamp(value):0;
const recent=value=>time(value)>0&&Date.now()-time(value)<=300000&&time(value)-Date.now()<60000;
const live=entity=>entity?.stale===false&&recent(entity.observed_at)&&Date.now()-time(entity.observed_at)<=30000;

// Short excerpts of recorded facts. Identity links are explicit, never inferred from names.
export function stationSignals(rows, snapshot, projection, humanFeed, language='zh', snapshotReachable=true) {
  const L=(zh,en)=>language==='en'?en:zh;
  return rows.map(row=>{
    const actors=new Set([row.actor,...(row.actors||[])].filter(Boolean));
    const messages=list(snapshot?.messages).filter(m=>actors.has(m.sender)).sort((a,b)=>time(b.created)-time(a.created)||(b.seq||0)-(a.seq||0));
    const deliveries=list(snapshot?.deliveries).filter(d=>actors.has(d.target)&&text(d.reply)).sort((a,b)=>time(b.reply_at)-time(a.reply_at));
    const task=row.source==='runtime'?list(projection?.tasks).find(t=>t.id===row.sourceId&&t.task_id===row.currentTaskId&&t.matrix_id===row.matrixId):null;
    const attention=humanFeed?.available===true&&humanFeed.stale===false?list(humanFeed.items).find(h=>h.state==='waiting_user'&&h.can_decide===true&&(
      actors.has(h.origin?.actor_id)||actors.has(h.origin?.seat_id)||(row.source==='registry'&&h.origin?.seat_id===row.sourceId)||
      (row.source==='runtime'&&task&&h.origin?.native_task_id===task.task_id&&h.origin?.task_generation===task?.generation)
    )):null;
    const candidates=[];
    if(attention)candidates.push({id:'human:'+attention.id+':'+attention.version,kind:'attention',text:attention.question,at:attention.observed_at,priority:5,label:L('需要你','Needs you'),humanId:attention.id,humanVersion:attention.version,fresh:true});
    if(task?.error&&live(task)&&row.state!=='unknown')candidates.push({id:'error:'+task.task_id+':'+task.error,kind:'issue',text:task.error,at:task.observed_at,priority:4,label:L('遇到问题','Issue'),fresh:true});
    const activity=list(task?.activity?.items).filter(v=>v.hidden!==true).at(-1);
    if(activity&&live(task)&&row.state!=='unknown')candidates.push({id:'step:'+task.task_id+':'+activity.id+':'+activity.state,kind:'work',text:activity.title||activity.action||activity.command||activity.tool,at:task.observed_at,priority:2,label:L('当前步骤','Current step'),fresh:true});
    for(const m of messages.slice(0,8))candidates.push({id:'message:'+m.id,kind:'message',text:m.body,at:m.created,priority:1,label:L('群聊','Group'),messageId:m.id,fresh:snapshotReachable&&snapshot?.connection?.state==='online'&&recent(m.created)});
    for(const d of deliveries.slice(0,4))candidates.push({id:'reply:'+d.id,kind:'reply',text:d.reply,at:d.reply_at,priority:1,label:L('答复','Reply'),deliveryId:d.id,fresh:snapshotReachable&&snapshot?.connection?.state==='online'&&recent(d.reply_at)});
    const blocked=list(row.tasks).find(t=>t.status==='blocked');
    if(blocked)candidates.push({id:'task:'+blocked.id+':'+blocked.version,kind:'issue',text:blocked.title||blocked.evidence,at:blocked.updated||blocked.updated_at,priority:3,label:L('任务受阻','Task blocked'),taskId:blocked.id,fresh:false});
    candidates.sort((a,b)=>b.priority-a.priority||time(b.at)-time(a.at));
    const signal=candidates[0];
    return {...row,signals:candidates.slice(0,12).map(s=>({...s,fullText:text(s.text)})),signal:signal?{...signal,text:['message','reply'].includes(signal.kind)?messageExcerpt(signal.text,78):excerpt(signal.text,78),fullText:text(signal.text)}:null};
  });
}

// Merge exact delivery/message IDs; never merge two merely similar messages.
export function channelTimeline(snapshot, receipt) {
  const deliveries=list(snapshot?.deliveries).slice(0,40);
  if(receipt&&!deliveries.some(d=>d.id===receipt.id))deliveries.unshift(receipt);
  const linked=new Map(deliveries.filter(d=>d.message_id).map(d=>[d.message_id,d]));
  const messages=list(snapshot?.messages).slice(-80);
  const ids=new Set(messages.map(m=>m.id));
  return [...messages.map(m=>({id:'message:'+m.id,at:m.created,message:m,delivery:linked.get(m.id)})),
    ...deliveries.filter(d=>!d.message_id||!ids.has(d.message_id)).map(d=>({id:'delivery:'+d.id,at:d.created_at,delivery:d}))]
    .sort((a,b)=>time(a.at)-time(b.at)||a.id.localeCompare(b.id));
}
