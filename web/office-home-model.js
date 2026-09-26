import {list,text,timestamp} from './model.js';

// Codes derive from immutable seat identities, never current sort positions.
export function stationCodes(rows){
 const map=new Map(),used=new Set();
 for(const row of [...rows].sort((a,b)=>a.id.localeCompare(b.id))){
  const declared=text(row.code||row.alias).trim(),named=text(row.name).match(/(?:^|[\s·])((?:W|S)-?\d{2,6})(?:$|\s)/i)?.[1];
  let hash=2166136261;for(const c of row.id){hash^=c.codePointAt(0);hash=Math.imul(hash,16777619)>>>0;}
  const generated='S-'+hash.toString(36).toUpperCase().padStart(7,'0'),base=declared||named||generated;
  let code=base;if(used.has(code))code=generated;if(used.has(code))code=generated+'-'+row.id;
  used.add(code);map.set(row.id,code);
 }
 return map;
}
export function recentMessages(items,now=Date.now()){
 const map=new Map();for(const m of list(items)){const at=timestamp(m.created);if(text(m.id)&&Number.isSafeInteger(m.seq)&&Number.isFinite(at)&&at>=now-86400000&&at<=now)map.set(m.id,m);}
 return [...map.values()].sort((a,b)=>a.seq-b.seq);
}
export function homeLayout(count,width,height){
 const narrow=width<600,available=Math.max(130,width-(narrow?88:144)),min=narrow?130:175,max=narrow?185:260,gap=narrow?16:38;
 const maxCols=Math.max(1,Math.floor((available+gap)/(min+gap)));let best={columns:1,size:min,score:-Infinity};
 for(let cols=1;cols<=Math.min(Math.max(1,count),maxCols);cols++){
  const lines=Math.ceil(Math.max(1,count)/cols),size=Math.max(min,Math.min(max,(available-(cols-1)*gap)/cols,(height-160-(lines-1)*24)/lines-78));
  const overflow=Math.max(0,lines*(size+78)+(lines-1)*24-(height-160));
  const score=size-overflow*.35-(cols*lines-count)*7;
  if(score>best.score)best={columns:cols,size,score};
 }
 return {...best,gap};
}
