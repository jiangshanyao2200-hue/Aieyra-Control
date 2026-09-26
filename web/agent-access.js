import {esc} from './model.js';

export function createAgentAccess({api, getLanguage, dialog, refresh}) {
  const L=(zh,en)=>getLanguage()==='en'?en:zh;
  let pending=null,busy=false,config=null,generation=0;
  const current=(view,content)=>view===generation&&dialog.open&&dialog.querySelector('#agent-access-content')===content;
  function error(message){const box=dialog.querySelector('.agent-access-error');if(box){box.hidden=false;box.textContent=message;}}
  function credentialConfig(){return JSON.stringify({protocol:'aieyra-agent/1',url:location.origin,token:pending.token,credential_id:config.credential.id,actor_id:config.credential.actor_id},null,2);}
  async function open(){
    if(busy)return;
    const view=++generation;
    pending=null;config=null;
    dialog.innerHTML=`<div class="detail-header"><h2 id="detail-title">${L('接入 Agent','Connect an agent')}</h2><button class="icon-button" data-dialog-close aria-label="${L('关闭','Close')}">×</button></div><p>${L('支持能调用 HTTP、命令行或 MCP 工具的本机 Agent。Aieyra OS 继续使用原生接入。','Local agents can use HTTP, CLI or MCP tools. Aieyra OS keeps its native integration.')}</p><p class="agent-access-error" role="alert" hidden></p><div id="agent-access-content">${L('读取接入记录…','Loading connections…')}</div>`;
    if(!dialog.open)dialog.showModal();
    const content=dialog.querySelector('#agent-access-content');
    try{
      const [access,snapshot]=await Promise.all([api('/api/agent-access'),api('/api/snapshot')]);
      if(!current(view,content))return;
      const sessions=access.sessions||[];
      dialog.querySelector('#agent-access-content').innerHTML=`<form id="agent-access-form"><label class="management-field"><span>${L('Agent 名称','Agent name')}</span><input name="name" required maxlength="80" placeholder="My local agent"></label><label class="management-field"><span>${L('项目','Project')}</span><select name="project" required>${(snapshot.projects||[]).map(p=>`<option value="${esc(p.id)}">${esc(p.name||p.id)}</option>`).join('')}</select></label><details><summary>${L('复用已有专属身份（可选）','Use an existing dedicated identity (optional)')}</summary><label class="management-field"><span>${L('本机身份别名','Local identity alias')}</span><input name="local_identity" pattern="[A-Za-z0-9_-]{1,70}" autocomplete="off"></label><p>${L('留空会创建身份、适配器和可选席位；已有身份需先被分配 external 席位。','Leave blank to create an identity, adapter and seat. An existing identity needs an assigned external seat.')}</p></details><button type="submit" class="button primary">${L('生成接入配置','Create connection configuration')}</button></form><section class="detail-section"><h3>${L('已授权 Agent','Authorized agents')}</h3>${(access.credentials||[]).map(c=>{const s=sessions.find(s=>s.credential_id===c.id);return `<article class="agent-access-row"><div><strong>${esc(c.name)}</strong><small>${esc(c.project)} · ${esc(c.revoked?L('已撤销','Revoked'):s?.state==='connected'?L('已选席','Connected'):L('等待选席','Waiting for seat'))}</small>${s?`<small>${esc(s.seat_id)} · ${esc(s.state)}</small>`:''}</div>${c.revoked?'':`<button class="button subtle" data-agent-revoke="${esc(c.id)}">${L('撤销接入','Revoke access')}</button>`}</article>`;}).join('')||`<p>${L('尚无外部 Agent。','No external agents yet.')}</p>`}</section><a href="/api/agent-openapi" target="_blank" rel="noopener">${L('查看完整 Agent API 契约','View Agent API contract')}</a>`;
    }catch(e){if(current(view,content))error(L('接入记录暂不可用：','Connections unavailable: ')+e.message);}
  }
  async function submit(form){
    if(busy)return;
    const view=generation,content=dialog.querySelector('#agent-access-content');
    const values=Object.fromEntries(new FormData(form));
    if(!pending){const bytes=crypto.getRandomValues(new Uint8Array(32));pending={request_id:'enroll-'+crypto.randomUUID(),token:[...bytes].map(x=>x.toString(16).padStart(2,'0')).join(''),...values};}
    busy=true;form.querySelectorAll('input,select,button').forEach(e=>e.disabled=true);
    dialog.querySelector('.agent-access-error').hidden=true;
    try{
      const result=await api('/api/agent-access/enroll',pending);
      if(!current(view,content))return;
      config=result;
      dialog.querySelector('#agent-access-content').innerHTML=`<p>${L('身份与席位已准备好。保存配置，然后让 Agent 通过 CLI、HTTP 或 MCP 选择席位。','Identity and seat are ready. Save the configuration, then let the agent select a seat using CLI, HTTP or MCP.')}</p><p>${L('配置包含此 Agent 的访问凭据，仅交给该 Agent；关闭后不再显示。','This configuration contains the agent credential. Share it only with that agent; it will not be shown after closing.')}</p><textarea id="agent-connection-config" readonly rows="8" spellcheck="false" aria-label="${L('Agent 接入配置','Agent connection configuration')}"></textarea><div class="detail-actions"><button class="button primary" data-agent-copy>${L('复制配置','Copy configuration')}</button><button class="button subtle" data-agent-save>${L('保存配置','Save configuration')}</button><button class="button subtle" data-dialog-close>${L('完成','Done')}</button></div><p>${L('保存为 agent.json 后：','After saving as agent.json:')}</p><pre class="agent-access-command">python scripts/agent-client.py --config agent.json seats
python scripts/agent-client.py --config agent.json connect --seat SEAT_ID --epoch 1
python scripts/agent-client.py --config agent.json mcp</pre><p>${L('收到消息不会自动运行模型。执行由接入的 Agent 负责，完成回执与人工验收分开记录。','The attached agent owns execution. Receiving a message does not start a model; completion and owner acceptance are recorded separately.')}</p>`;
      dialog.querySelector('#agent-connection-config').value=credentialConfig();
      // Enrollment is already confirmed; a failed background refresh cannot undo it.
      await refresh().catch(()=>{});
    }catch(e){
      if(!current(view,content))return;
      // Only pre-enrollment validation failures allow a new request. A later
      // center rejection can follow partial provisioning and must keep its id.
      const correctable=!e.uncertain&&e.status===400&&['invalid_agent_text','invalid_agent_identifier','invalid_enrollment_fields','strong_agent_token_required','invalid_local_identity'].includes(e.code);
      if(correctable){pending=null;form.querySelectorAll('input,select,button').forEach(e=>e.disabled=false);error(L('未能生成配置，请检查输入或权限：','Could not create configuration; check inputs or permissions: ')+e.message);}
      else{error(L('尚未确认：','Not confirmed: ')+e.message+L('。可使用同一请求重新核对。','; retry with the same request to reconcile.'));form.querySelector('button[type="submit"]').disabled=false;form.querySelector('button[type="submit"]').textContent=L('重试原请求','Retry original request');}
    }
    finally{busy=false;}
  }
  dialog.addEventListener('submit',e=>{if(e.target.id==='agent-access-form'){e.preventDefault();void submit(e.target);}});
  dialog.addEventListener('click',async e=>{
    const b=e.target.closest('button');if(!b||busy)return;
    if(b.hasAttribute('data-agent-copy')){try{await navigator.clipboard.writeText(credentialConfig());b.textContent=L('已复制','Copied');}catch{error(L('请选中配置手动复制。','Select and copy the configuration manually.'));}}
    if(b.hasAttribute('data-agent-save')){const url=URL.createObjectURL(new Blob([credentialConfig()],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='agent.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
    if(b.dataset.agentRevoke){const view=generation,content=dialog.querySelector('#agent-access-content');busy=true;b.disabled=true;try{await api('/api/agent-access/revoke',{credential_id:b.dataset.agentRevoke});busy=false;if(current(view,content))await open();await refresh().catch(()=>{});}catch(e){if(current(view,content)){error(e.message);b.disabled=false;}}finally{busy=false;}}
  });
  dialog.addEventListener('cancel',e=>{if(busy)e.preventDefault();});
  dialog.addEventListener('close',()=>{if(dialog.open)return;generation++;pending=null;config=null;const box=dialog.querySelector('#agent-connection-config');if(box)box.value='';});
  return {open,isBusy:()=>busy};
}
