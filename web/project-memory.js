import {esc} from './model.js';

export function createProjectMemory({api,dialog,getLanguage}){
  const sections=[['blueprint','项目蓝图','Blueprint'],['timeline','历史时间线','Timeline'],['checkpoint','当前断点与下一步','Checkpoint'],['recovery','备份与恢复','Recovery'],['index','工程索引','Project index']];
  let generation=0,projects=[],selected='',document=null,history=[],loading=false,error='',historical=false,opened=false,returnFocus=null;
  const L=(zh,en)=>getLanguage()==='en'?en:zh;
  function render(){
    if(!opened)return;
    const memory=document?.memory;
    dialog.innerHTML=`<div class="detail-header"><span class="eyebrow">${esc(L('项目长期记忆','Project memory'))}</span><button class="icon-button" data-dialog-close aria-label="${esc(L('关闭','Close'))}" autofocus>×</button></div><h2 id="detail-title">${esc(L('从这里接手','Start here'))}</h2><p class="muted small">${esc(L('记忆属于项目，工位会话可以更替。先核对当前断点，再继续工作。','Memory belongs to the project across sessions. Check the checkpoint before continuing.'))}</p><div class="memory-toolbar"><label>${esc(L('项目','Project'))}<select data-memory-project ${loading?'disabled':''}>${projects.map(p=>`<option value="${esc(p.id)}" ${p.id===selected?'selected':''}>${esc(p.name||p.id)}</option>`).join('')}</select></label><button class="button subtle" data-memory-refresh ${loading?'disabled':''}>${esc(L('读取当前版本','Read current version'))}</button></div>${error?`<p role="alert" class="cc-error">${esc(error)}</p>`:''}${loading?`<p role="status">${esc(L('正在读取项目记录…','Reading project records…'))}</p>`:''}${memory?`<p class="memory-meta">${esc(document.project?.root||'')}<br>${esc(L('本机持久记录','Stored on this computer'))} · v${memory.version}${historical?` · ${esc(L('历史版本，不能当作当前断点','Historical revision, not the current checkpoint'))}`:''}<br>${esc(memory.created_at||L('尚未建立','Not created yet'))} · ${esc(memory.summary||'')}</p>${sections.map(([key,zh,en],i)=>`<details class="memory-section" ${i===0||key==='checkpoint'?'open':''}><summary>${esc(L(zh,en))}${!memory.sections[key]?.trim()?` · ${esc(L('待补充','Missing'))}`:''}</summary><div class="memory-content">${esc(memory.sections[key]||L('这部分还没有记录。','This section has not been recorded.'))}</div></details>`).join('')}<details class="memory-history"><summary>${esc(L('修订记录','Revision history'))} · ${history.length}</summary>${history.map(h=>`<button class="shared-row" data-memory-version="${h.version}" ${loading?'disabled':''}><span>v${h.version}</span><span><strong>${esc(h.summary)}</strong><small>${esc(h.created_at)} · ${esc(h.actor)}</small></span></button>`).join('')}</details><p class="muted small">${esc(L('通过 Agent 的 memory-save 或 aieyra_memory_save 保存断点；必须携带读取时的版本。历史任务不代表本次执行授权。','Use memory-save or aieyra_memory_save with the version you read to save a checkpoint. Historical tasks do not authorize execution.'))}</p>`:''}`;
  }
  async function read(version){
    const token=++generation,project=selected;loading=true;error='';document=null;render();
    try{
      const [value,revisions]=await Promise.all([api('/api/project-memory?project='+encodeURIComponent(project)+(version?'&version='+version:'')),api('/api/project-memory?project='+encodeURIComponent(project)+'&history=1')]);
      if(token!==generation||!opened)return;
      if(!value?.memory?.sections||value.memory.project!==project||!Array.isArray(revisions.revisions))throw Error('invalid_memory');
      document=value;history=revisions.revisions;historical=Boolean(version&&version!==value.current_version);
    }catch{if(token===generation&&opened)error=L('暂时无法读取，点击“读取当前版本”重试。','Could not read the record. Retry with “Read current version”.');}
    finally{if(token===generation&&opened){loading=false;render();}}
  }
  async function open(project=''){
    returnFocus=window.document.activeElement;opened=true;selected=project;document=null;history=[];projects=[];loading=true;error='';render();if(!dialog.open)dialog.showModal();
    const token=++generation;
    try{const result=await api('/api/project-memory');if(token!==generation||!opened)return;
      if(!Array.isArray(result.projects))throw Error('invalid_projects');projects=result.projects;
      selected=projects.some(p=>p.id===selected)?selected:projects[0]?.id||'';
      if(selected)await read();else{loading=false;error=L('尚未登记本机项目记忆。','No local project memory is configured.');render();}
    }catch{if(token===generation&&opened){loading=false;error=L('项目记忆暂不可用，请关闭后重新打开。','Project memory is unavailable. Close and reopen to retry.');render();}}
  }
  dialog.addEventListener('change',e=>{if(opened&&e.target.hasAttribute('data-memory-project')){selected=e.target.value;void read();}});
  dialog.addEventListener('click',e=>{const b=e.target.closest('button');if(!opened||!b||b.disabled)return;if(b.hasAttribute('data-memory-refresh')){if(selected)void read();else void open();}if(b.dataset.memoryVersion)void read(Number(b.dataset.memoryVersion));});
  dialog.addEventListener('close',()=>{if(opened){opened=false;generation++;returnFocus?.isConnected&&returnFocus.focus({preventScroll:true});}});
  return {open};
}
