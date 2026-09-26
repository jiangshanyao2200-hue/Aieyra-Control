import {esc} from './model.js';
export function createCloudSettings({api,dialog}){
 let value={enabled:false},message='',busy=false,link='';
 function render(){
  dialog.innerHTML=`<div class="detail-header"><span class="eyebrow">云服务</span><button class="icon-button" data-dialog-close aria-label="关闭">×</button></div><h2 id="detail-title">${value.enabled?'已连接 Aieyra':'本地办公室'}</h2><p>${value.enabled?'登录账号：'+esc(value.user?.name||''):'未登录：群聊、工位和项目记忆保存在本机，不检查云端更新。'}</p><p>登录后启用版本通知与公共娱乐分享。项目记忆和本地聊天不会自动上传。登录状态仅保留在当前服务进程。</p><p role="status">${esc(message)}</p>${link?`<p><a class="button primary" target="_blank" rel="noopener noreferrer" href="${esc(link)}">打开 Aieyra 登录页 ↗</a></p><button class="button" data-cloud="poll" ${busy?'disabled':''}>我已登录，完成连接</button>`:''}<div class="detail-actions">${value.enabled?'<button class="button" data-cloud="check">检查更新</button><button class="button" data-cloud="logout">退出并回到纯本地</button>':`<button class="button primary" data-cloud="login" ${busy?'disabled':''}>登录并启用云服务</button>`}</div>${value.release?.manifest?`<p>有发行版本：${esc(value.release.manifest.version)}。领导工位会收到版本信息，审阅本地修改后决定是否更新。</p>`:''}`;
 }
 async function open(){message='正在读取本地状态…';link='';render();if(!dialog.open)dialog.showModal();try{value=await api('/api/cloud');message='';}catch{message='本地状态读取失败，请重试。';}render();}
 dialog.addEventListener('click',async event=>{const action=event.target.closest('[data-cloud]')?.dataset.cloud;if(!action||busy)return;busy=true;message='正在处理…';render();try{const result=await api('/api/cloud/'+action,{});if(action==='login'){link=result.authorize_url;message='请打开登录页，完成后点击“完成连接”。';}else if(action==='poll'&&result.pending){message='授权还未完成，请先在浏览器登录。';}else{value=await api('/api/cloud');link='';message=action==='check'?'版本信息已读取，更新由领导工位处理。':action==='logout'?'已回到纯本地。':'连接成功。';}}catch(error){message='暂未完成：'+(error.message||'连接失败')+'。本地功能仍可使用。';}finally{busy=false;render();}});
 return {open};
}
