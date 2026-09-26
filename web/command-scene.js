import {esc, list, text, runtimeState, runtimePresent, timestamp} from './model.js';
import {updateHTML} from './dom.js';

// A registry seat is an identity, never an assertion of runtime activity.
export function sceneSeats(registry, snapshot, projection, reachable, productReachable) {
  const agents = list(snapshot?.agents), rows = [], seen = new Set();
  for (const seat of list(registry?.seats)) {
    if (!seat.id || seat.membership_state==='removed') continue;
    const candidate = agents.find(a => a.id === seat.actor_id);
    const agent = candidate?.runtime?.adapter === 'aieyra-agent/1' && candidate.runtime.seat_id !== seat.id ? null : candidate;
    const runtime = runtimeState(agent || {}, reachable);
    const key = agent?.runtime?.thread_id || seat.runtime_ref || `seat:${seat.id}`;
    const present = runtimePresent(agent || {}, reachable);
    if (present && seen.has(key)) continue;
    if (present) seen.add(key);
    const persistent=seat.membership_state==='active'||seat.ownership==='external'&&seat.membership_state!=='removed'&&(!seat.station_binding||seat.station_binding.seat_id===seat.id);
    rows.push({id:`registry:${seat.id}`, name:text(seat.name || seat.id), state:present?runtime:persistent?'offline':runtime,persistent,
      source:'registry', sourceId:seat.id, actor:seat.actor_id || '', actors:agents.filter(a=>a.id===seat.actor_id||(agent?.runtime?.thread_id&&a.runtime?.thread_id===agent.runtime.thread_id)).map(a=>a.id), observed:agent?.runtime?.observed_at,
      expanded:present,project:seat.project, scope:seat.scope, tasks:list(snapshot?.tasks).filter(t => seat.actor_id && t.owner === seat.actor_id), seat});
  }
  for (const agent of agents) {
    if (list(registry?.seats).some(seat => seat.actor_id === agent.id)) continue;
    const key=agent.runtime?.thread_id || `agent:${agent.id}`;
    const present=runtimePresent(agent,reachable);
    if(present&&seen.has(key))continue;if(present)seen.add(key);
    rows.push({id:`agent:${agent.id}`,name:text(agent.name||agent.id),state:runtimeState(agent,reachable),expanded:runtimePresent(agent,reachable),source:'agent',sourceId:agent.id,actor:agent.id,actors:agents.filter(a=>a.id===agent.id||(agent.runtime?.thread_id&&a.runtime?.thread_id===agent.runtime.thread_id)).map(a=>a.id),project:agent.project,observed:agent.runtime?.observed_at,tasks:list(snapshot?.tasks).filter(t=>t.owner===agent.id)});
  }
  for (const station of list(projection?.runtime_stations)) {
    const matrix = list(projection?.matrices).find(m => m.matrix_id === station.matrix_id);
    const at = timestamp(station.observed_at), age = Date.now() - at;
    const fresh = productReachable && matrix?.available === true && matrix.stale === false && Number.isFinite(timestamp(matrix.observed_at)) && Date.now()-timestamp(matrix.observed_at)<=30000 && timestamp(matrix.observed_at)-Date.now()<=60000 && station.stale === false && Number.isFinite(at) && age >= -60000 && age <= 30000;
    const state = fresh ? station.active === false ? 'idle' : station.execution_state || 'unknown' : 'unknown';
    rows.push({id:`runtime:${station.id}`,name:text(station.title || station.id),state, source:'runtime',sourceId:station.id,
      matrixId:station.matrix_id,currentTaskId:station.current_task_id,observed:station.observed_at, actor:'', project:station.project, tasks:[]});
  }
  return rows;
}

export const visibleStation=row=>row.source==='runtime'?row.state!=='unknown':row.expanded===true||row.persistent===true;

// The floor and desks share one fitted grid, including narrow and collapsed layouts.
export function stationLayout(count,width,height,rowHeight=175){
  const viewHeight=1360*Math.max(height,1)/Math.max(width,1),unit=1360/Math.max(width,1);
  const top=Math.min(90*unit,viewHeight*.18),bottom=Math.min(95*unit,viewHeight*.2);
  const previewSpace=width>=700?344*unit:0;
  const areaWidth=1190-previewSpace,areaHeight=Math.max(180,viewHeight-top-bottom),maxScale=width<560?3:count<=2?2.5:count<=6?1.95:1.6;
  let best={cols:1,lines:1,scale:1,score:0};
  for(let cols=1;cols<=Math.min(Math.max(count,1),width<560?2:6);cols++){
    const lines=Math.ceil(Math.max(count,1)/cols),scale=Math.min(maxScale,areaWidth/(cols*215),areaHeight/(lines*rowHeight));
    const score=scale-(cols*lines-count)*.018;
    if(score>best.score)best={cols,lines,scale,score};
  }
  const {cols,lines,scale}=best,centerY=top+areaHeight/2-22*scale;
  return {scale,centerY,viewHeight,top:top-32*unit,bottom:viewHeight-bottom+35*unit,cols,lines,viewBox:`0 0 1360 ${viewHeight}`,positions:Array.from({length:count},(_,i)=>{
    const row=Math.floor(i/cols),n=Math.min(cols,count-row*cols);
    return [680-previewSpace/2+(i%cols-(n-1)/2)*215*scale,centerY+(row-(lines-1)/2)*rowHeight*scale];
  })};
}
const palette=['#b5c788','#91bed6','#d4a28d','#b6a3d5','#81c6b3','#d1bd7f','#d698b4','#92b29d','#93a6d8','#c2af98','#a8ccbb','#b4c5d0'];
export function stationColor(id){let hash=0;for(const c of id)hash=(hash*31+c.codePointAt(0))>>>0;return palette[hash%palette.length];}

export function createCommandScene(){
  let host,rows=[],options={},zoom=1,pan={x:0,y:0},drag=null,centerY=370,lastSignals=new Map(),initialized=false;
  let resizeObserver,events,frame=0,lastMarkup='',hovered='',hideTimer;
  const previewId=id=>'station-preview-'+encodeURIComponent(id);
  function showPreview(id){clearTimeout(hideTimer);hovered=id;layoutNotes();}
  function hidePreview(){clearTimeout(hideTimer);hovered='';layoutNotes();}
  function delayHide(){clearTimeout(hideTimer);hideTimer=setTimeout(hidePreview,240);}
  const measure=document.createElement('canvas').getContext('2d'),nameCache=new Map(),colors=new Map();
  function nameLines(name){
    if(nameCache.has(name))return nameCache.get(name);
    measure.font=`500 12px ${getComputedStyle(host).fontFamily}`;
    const lines=[];let line='';
    for(const letter of name){if(line&&measure.measureText(line+letter).width>164){lines.push(line);line='';}line+=letter;}
    lines.push(line);if(nameCache.size>256)nameCache.clear();nameCache.set(name,lines);return lines;
  }
  const scheduleCamera=()=>{if(!frame)frame=requestAnimationFrame(()=>{frame=0;camera();});};
  const motion=matchMedia('(prefers-reduced-motion: reduce)'),eventAnimations=new Set();
  const reduced=()=>motion.matches;
  // 系统偏好在运行中改变时，立即结束本场景已经启动的提示动画。
  motion.addEventListener('change',()=>{if(reduced()){for(const animation of eventAnimations)animation.cancel();eventAnimations.clear();}});
  function signalAnimation(node,frames,timing){
    if(!node)return;const animation=node.animate(frames,timing);eventAnimations.add(animation);
    const done=()=>eventAnimations.delete(animation);animation.finished.then(done,done);
  }
  function layoutNotes(){
    const layer=host?.querySelector('.station-notes');if(!layer)return;
    const frame=host.getBoundingClientRect(),links=[];
    const seats=[...host.querySelectorAll('[data-floor-id]')].map(node=>{const r=node.getBoundingClientRect();node.setAttribute('aria-expanded',String(node.dataset.floorId===hovered));return {node,x:r.left-frame.left,y:r.top-frame.top,width:r.width,height:r.height};});
    const overlaps=(a,b,gap=8)=>a.x<b.x+b.width+gap&&a.x+a.width+gap>b.x&&a.y<b.y+b.height+gap&&a.y+a.height+gap>b.y;
    for(const node of layer.querySelectorAll('[data-preview-seat]')){
      const seat=seats.find(s=>s.node.dataset.floorId===node.dataset.previewSeat);
      node.hidden=!seat||node.dataset.previewSeat!==hovered;if(node.hidden)continue;
      const width=Math.min(320,frame.width-24);node.style.width=width+'px';node.style.maxHeight=Math.min(340,frame.height-64)+'px';
      const height=node.offsetHeight,anchor={x:seat.x+seat.width/2,y:seat.y+12},candidates=[];
      const clamp=(x,y)=>({x:Math.max(12,Math.min(frame.width-width-12,x)),y:Math.max(42,Math.min(frame.height-height-12,y)),width,height});
      const add=(x,y)=>{const box=clamp(x,y);if(!seats.some(s=>overlaps(box,s,6)))candidates.push(box);};
      add(anchor.x-width/2,seat.y-height-16);add(seat.x-width-16,seat.y);add(seat.x+seat.width+16,seat.y);add(anchor.x-width/2,seat.y+seat.height+16);
      for(let y=42;y<frame.height-height-10;y+=42)for(let x=12;x<frame.width-width;x+=48)add(x,y);
      candidates.sort((a,b)=>Math.hypot(a.x+width/2-anchor.x,a.y+height/2-anchor.y)-Math.hypot(b.x+width/2-anchor.x,b.y+height/2-anchor.y));
      const box=candidates[0]||clamp(anchor.x-width/2,seat.y+seat.height+12);
      node.style.left=box.x+'px';node.style.top=box.y+'px';
      const end={x:Math.max(box.x+8,Math.min(box.x+width-8,anchor.x)),y:Math.max(box.y+8,Math.min(box.y+height-8,anchor.y))};
      links.push(`<path d="M${anchor.x} ${anchor.y} L${end.x} ${end.y}"/>`);
    }
    const wires=layer.querySelector('.station-links');if(wires){wires.setAttribute('viewBox',`0 0 ${frame.width} ${frame.height}`);wires.innerHTML=links.join('');}
  }
  const camera=()=>{host?.querySelector('#floor-camera')?.setAttribute('transform',`translate(${pan.x} ${pan.y}) translate(680 ${centerY}) scale(${zoom}) translate(-680 ${-centerY})`);layoutNotes();};
  function draw(){
    const L=(zh,en)=>options.language==='en'?en:zh;
    const names=rows.map(row=>nameLines(row.name)),extraHeight=Math.max(0,...names.map(lines=>(lines.length-1)*16));
    const layout=stationLayout(rows.length,host.clientWidth,host.clientHeight,175+extraHeight);
    centerY=layout.centerY;
    const used=new Set(colors.values());for(const row of [...rows].sort((a,b)=>a.id.localeCompare(b.id))){if(colors.has(row.id))continue;let c=stationColor(row.id);if(used.has(c))c=palette.find(p=>!used.has(p))||c;colors.set(row.id,c);used.add(c);}
    const gridLines=Math.max(2,layout.lines*2+1);
    const grid=Array.from({length:11},(_,i)=>`<path d="M${140+i*108} ${layout.top+22} V${layout.bottom-18}"/>`).join('')+Array.from({length:gridLines},(_,i)=>`<path d="M72 ${layout.top+30+i*(layout.bottom-layout.top-50)/(gridLines-1)} H1288"/>`).join('');
    const desks=rows.map((row,i)=>{const [x,y]=layout.positions[i],label=row.state==='unknown'?L('状态待更新','Status unavailable'):options.labels?.[row.state]||L('未知','Unknown'),selected=options.selected===row.id||options.selected===row.sourceId;
      const lines=names[i],extra=(lines.length-1)*16;
      return `<g data-key="${esc(row.id)}" style="--station-accent:${colors.get(row.id)}" class="floor-seat seat-${esc(row.state)} ${selected?'selected':''}" transform="translate(${x} ${y}) scale(${layout.scale})" data-floor-id="${esc(row.id)}" role="button" tabindex="0" aria-pressed="${selected}" aria-controls="${esc(previewId(row.id))}" aria-expanded="false" aria-label="${esc(row.name+' · '+label)}"><title>${esc(row.name+' · '+label)}</title><ellipse class="work-orbit" cx="0" cy="23" rx="69" ry="34"/><ellipse class="seat-shadow" cx="0" cy="29" rx="60" ry="19"/><path class="seat-plinth" d="M-62 8 0-24 64 8 1 43Z"/><path class="desk-side" d="M-49-1 0 23 49-1V8L0 33-49 8Z"/><path class="desk-top" d="M-49-1 0-26 49-1 0 23Z"/><path class="desk-leg" d="M-41 11v21m82-21v21"/><path class="monitor-back" d="M-25-43 13-25 13 4-25-14Z"/><path class="monitor-screen" d="M-20-35 8-21 8-4-20-18Z"/><path class="monitor-lines" d="M-16-29 4-19 M-16-24-2-17 M-16-19-5-13"/><path class="keyboard" d="M-2 5 12-2 26 5 12 12Z"/><path class="seat-chair" d="M-11 27 2 20 18 28 5 35Z M-11 27v12l16 8 13-8V28"/><circle class="seat-signal" cx="42" cy="-3" r="3"/>${row.signal?`<g class="seat-message-marker signal-${esc(row.signal.kind)}" transform="translate(27 -52)"><circle r="9"/><text text-anchor="middle" y="4">${row.signal.kind==='attention'?'!':'···'}</text></g>`:''}<g class="seat-caption" transform="translate(0 65)"><rect x="-91" y="-14" width="182" height="${43+extra}" rx="7"/><text text-anchor="middle" class="seat-name">${lines.map((line,n)=>`<tspan x="0" y="${n*16}">${esc(line)}</tspan>`).join('')}</text><text text-anchor="middle" y="${19+extra}" class="seat-state">${esc(label)}</text></g></g>`;
    }).join('');
    const signalTime=signal=>{const at=timestamp(signal.at);return Number.isFinite(at)?new Intl.DateTimeFormat(options.language==='en'?'en-GB':'zh-CN',Date.now()-at>86400000?{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}:{hour:'2-digit',minute:'2-digit',hour12:false}).format(at):'';};
    const notesMarkup=`<div class="station-notes"><svg class="station-links" aria-hidden="true"></svg>${rows.map(row=>`<section hidden id="${esc(previewId(row.id))}" class="station-note signal-${esc(row.signal?.kind||'none')}" data-key="note:${esc(row.id)}" data-preview-seat="${esc(row.id)}" style="--station-accent:${colors.get(row.id)}" aria-label="${esc(row.name+' · '+L('最近动态','Recent activity'))}"><div class="station-note-meta"><strong>${esc(row.name)}</strong><span>${esc(options.labels?.[row.state]||L('未知','Unknown'))}</span></div><div class="station-note-context">${esc(row.project||L('未标注项目','Project not specified'))} · ${esc(L('最近动态','Recent activity'))}</div><div class="station-note-scroll" tabindex="0" aria-label="${esc(L('可滚动的工位动态','Scrollable workstation activity'))}">${(row.signals?.length?row.signals:row.signal?[row.signal]:[]).map(s=>`<article data-key="preview:${esc(s.id)}"><div class="station-note-event"><small>${esc(s.label)}${s.fresh?'':L(' · 记录',' · recorded')}</small><time>${esc(signalTime(s))}</time></div><p class="station-note-body">${esc(s.fullText||s.text)}</p></article>`).join('')||`<p class="station-note-body">${esc(L('暂无公开动态。点击工位查看详情。','No activity recorded. Select the workstation for details.'))}</p>`}</div><button class="station-note-more" data-signal-seat="${esc(row.id)}">${esc(row.signal?.humanId?L('查看待处理事项 →','Review request →'):L('查看全文与工位详情 →','Full message and workstation →'))}</button></section>`).join('')}</div>`;
    const markup=`<svg id="command-floor" viewBox="${layout.viewBox}" role="group" aria-label="${esc(L('指挥室工位现场','Command room workstations'))}"><defs><linearGradient id="floor-fill" x2="0" y2="1"><stop stop-color="#222932"/><stop offset="1" stop-color="#141a22"/></linearGradient><linearGradient id="wall-fill" x2="1" y2="1"><stop stop-color="#242e39"/><stop offset="1" stop-color="#141c24"/></linearGradient><radialGradient id="floor-halo"><stop stop-color="#64c6ba" stop-opacity=".14"/><stop offset="1" stop-color="#64c6ba" stop-opacity="0"/></radialGradient></defs><g id="floor-camera" class="${rows.some(r=>['running','busy'].includes(r.state))?'has-work':''}"><g class="room-scenery"><rect class="room-base" x="52" y="${layout.top+12}" width="1256" height="${layout.bottom-layout.top}" rx="26"/><rect class="room-floor" x="52" y="${layout.top}" width="1256" height="${layout.bottom-layout.top}" rx="26"/><g class="floor-grid">${grid}</g><path class="wall-edge" d="M78 ${layout.top+1}H1282"/><g class="mission-wall" transform="translate(680 ${layout.top-18})"><text text-anchor="middle" class="mission-word">A I E Y R A</text></g></g>${desks}</g></svg>${notesMarkup}${!rows.length?`<div class="floor-empty" role="status"><strong>${esc(options.emptyUnavailable?L('现场暂不可用','Live view unavailable'):L('等待工位接入','Waiting for workstations'))}</strong><p>${esc(options.emptyUnavailable?L('连接恢复后，这里会自动更新','This view updates when the connection returns'):L('接入后，当前工作会在这里展开','Current work appears here once connected'))}</p><button class="text-button" data-page="agents">${esc(L('查看工位资料','View workstations'))}<span aria-hidden="true"> →</span></button></div>`:''}`;
    if(markup!==lastMarkup){updateHTML(host,markup);lastMarkup=markup;camera();}
    if(initialized&&!reduced()&&document.visibilityState==='visible')for(const row of rows){if(row.signal?.fresh&&lastSignals.has(row.id)&&lastSignals.get(row.id)!==row.signal.id){const node=[...host.querySelectorAll('[data-signal-seat]')].find(n=>n.dataset.signalSeat===row.id);signalAnimation(node,[{opacity:.2,transform:'translateY(9px)'},{opacity:1,transform:'translateY(0)'}],{duration:320,easing:'cubic-bezier(.2,.8,.2,1)'});const seat=[...host.querySelectorAll('[data-floor-id]')].find(n=>n.dataset.floorId===row.id);signalAnimation(seat?.querySelector('.seat-message-marker'),[{opacity:.25},{opacity:1}],{duration:650});}}
    lastSignals=new Map(rows.map(r=>[r.id,r.signal?.id]));initialized=true;
  }
  return {mount(target,next,config){rows=next;options=config;if(host!==target){
    resizeObserver?.disconnect();events?.abort();if(frame)cancelAnimationFrame(frame);frame=0;
    host=target;lastMarkup='';hovered='';clearTimeout(hideTimer);events=new AbortController();const signal=events.signal;
    let size='';resizeObserver=new ResizeObserver(()=>{const next=host.clientWidth+':'+host.clientHeight;if(next!==size){size=next;lastMarkup='';draw();}});resizeObserver.observe(host);
    host.addEventListener('click',e=>{if(drag?.moved)return;const note=e.target.closest('[data-signal-seat]');if(note){const row=rows.find(r=>r.id===note.dataset.signalSeat);if(row){hidePreview();options.onSignal?.(row);}return;}if(e.target.closest('[data-preview-seat]'))return;const seat=e.target.closest('[data-floor-id]');if(seat){hidePreview();options.onSelect(seat.dataset.floorId);}else if(!e.target.closest('button,a,input,textarea,select'))options.onDeselect?.();},{signal});
    document.addEventListener('keydown',e=>{if(!e.isComposing&&e.key==='Escape'&&hovered){hidePreview();e.stopImmediatePropagation();}},{capture:true,signal});
    host.addEventListener('keydown',e=>{if(e.isComposing)return;const seat=e.target.closest('[data-floor-id]');if(seat&&['Enter',' '].includes(e.key)){e.preventDefault();hidePreview();options.onSelect(seat.dataset.floorId);}},{signal});
    host.addEventListener('pointerover',e=>{if(e.pointerType==='touch')return;const node=e.target.closest('[data-floor-id],[data-preview-seat]');if(node)showPreview(node.dataset.floorId||node.dataset.previewSeat);},{signal});
    host.addEventListener('pointerout',e=>{if(e.pointerType==='touch')return;if(!e.target.closest('[data-floor-id],[data-preview-seat]'))return;const next=e.relatedTarget?.closest?.('[data-floor-id],[data-preview-seat]');if(next)showPreview(next.dataset.floorId||next.dataset.previewSeat);else delayHide();},{signal});
    host.addEventListener('focusin',e=>{const node=e.target.closest('[data-floor-id],[data-preview-seat]');if(node)showPreview(node.dataset.floorId||node.dataset.previewSeat);},{signal});
    host.addEventListener('focusout',e=>{if(!host.contains(e.relatedTarget))delayHide();},{signal});
    host.addEventListener('wheel',e=>{if(e.ctrlKey||e.target.closest('.floor-empty,[data-preview-seat]'))return;e.preventDefault();zoom=Math.max(.65,Math.min(1.8,zoom*(e.deltaY>0?.93:1.07)));scheduleCamera();},{passive:false,signal});
    host.addEventListener('pointerdown',e=>{if(e.button!==0||!e.isPrimary||e.target.closest('[data-floor-id],[data-preview-seat],button'))return;drag={id:e.pointerId,x:e.clientX,y:e.clientY,px:pan.x,py:pan.y,moved:false};host.setPointerCapture(e.pointerId);},{signal});
    host.addEventListener('pointermove',e=>{if(!drag||drag.id!==e.pointerId||!(e.buttons&1))return;if(!drag.moved&&Math.hypot(e.clientX-drag.x,e.clientY-drag.y)<4)return;const scale=1360/Math.max(host.clientWidth,1);pan={x:Math.max(-450,Math.min(450,drag.px+(e.clientX-drag.x)*scale)),y:Math.max(-260,Math.min(260,drag.py+(e.clientY-drag.y)*scale))};drag.moved=true;scheduleCamera();},{signal});
    host.addEventListener('pointerup',()=>{const finished=drag;setTimeout(()=>{if(drag===finished)drag=null;},0);},{signal});
    host.addEventListener('pointercancel',()=>{drag=null;},{signal});
    host.addEventListener('lostpointercapture',()=>{drag=null;},{signal});
  }draw();},reset(){hidePreview();zoom=1;pan={x:0,y:0};drag=null;camera();},setMode(){},getMode:()=> '2d'};
}
