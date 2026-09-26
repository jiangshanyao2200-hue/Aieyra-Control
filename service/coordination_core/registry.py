"""Persistent workstation control plane; execution stays with authenticated hosts."""
from __future__ import annotations
import json,math,time,uuid
from .core import Fault,ident,text,scope,overlaps

ACTIONS={'project-register','governance-grant','host-register','adapter-register','seat-create','seat-update','seat-control','seat-observe','seat-attach','operation-receipt'}
OPERATIONS={'inspect','open','pause','resume','interrupt','close'}
ADAPTER_CAPABILITIES=OPERATIONS | {'dispatch', 'product.dispatch', 'human.reply'}
STATES={'registered','idle','running','paused','closed','unknown','unavailable'}

def integer(v,lo,hi):
 if type(v) is not int or not lo<=v<=hi:raise Fault('invalid_integer')
 return v

def names(v,allowed=None,limit=32):
 if not isinstance(v,list) or not v or len(v)>limit or len(set(str(x) for x in v))!=len(v):raise Fault('invalid_list')
 out=[ident(x) for x in v]
 if allowed is not None and not set(out)<=set(allowed):raise Fault('unsupported_capability')
 return out

def row(d,table,key):
 r=d.execute('SELECT * FROM '+table+' WHERE id=?',(ident(key),)).fetchone()
 if not r:raise Fault('registry_item_missing',404)
 return dict(r)

def version(b,r):
 if type(b.get('version')) is not int or b['version']!=(r['version'] if r else 0):raise Fault('version_conflict',409)

def safe_ref(value):
 v=text(value,240,empty=True)
 if '\n' in v or '\r' in v:raise Fault('invalid_reference')
 return v

def public(r):
 out=dict(r)
 for k in ('projects_json','actions_json'):
  if k in out:out[k.removesuffix('_json')]=json.loads(out.pop(k))
 return out

def migrate(d,projects):
 d.executescript('''
 CREATE TABLE IF NOT EXISTS registered_projects(id TEXT PRIMARY KEY,name TEXT NOT NULL,root TEXT NOT NULL,source TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,updated REAL NOT NULL);
 CREATE TABLE IF NOT EXISTS governance(actor_id TEXT PRIMARY KEY REFERENCES actors(id),role TEXT NOT NULL,projects_json TEXT NOT NULL,expires_at REAL NOT NULL,version INTEGER NOT NULL,reason TEXT NOT NULL,granted_by TEXT NOT NULL REFERENCES actors(id),updated REAL NOT NULL);
 CREATE TABLE IF NOT EXISTS registered_hosts(id TEXT PRIMARY KEY,name TEXT NOT NULL,actor_id TEXT UNIQUE NOT NULL REFERENCES actors(id),projects_json TEXT NOT NULL,max_active INTEGER NOT NULL,max_seats INTEGER NOT NULL,version INTEGER NOT NULL,last_seen REAL NOT NULL DEFAULT 0,updated REAL NOT NULL);
 CREATE TABLE IF NOT EXISTS registered_adapters(id TEXT PRIMARY KEY,host_id TEXT NOT NULL REFERENCES registered_hosts(id),name TEXT NOT NULL,kind TEXT NOT NULL,version_label TEXT NOT NULL,actions_json TEXT NOT NULL,availability TEXT NOT NULL,version INTEGER NOT NULL,observed_at REAL NOT NULL);
 CREATE TABLE IF NOT EXISTS seats(id TEXT PRIMARY KEY,name TEXT NOT NULL,project TEXT NOT NULL REFERENCES registered_projects(id),scope TEXT NOT NULL,actor_id TEXT REFERENCES actors(id),host_id TEXT NOT NULL REFERENCES registered_hosts(id),adapter_id TEXT NOT NULL REFERENCES registered_adapters(id),ownership TEXT NOT NULL,desired_state TEXT NOT NULL,observed_state TEXT NOT NULL,runtime_ref TEXT NOT NULL DEFAULT '',version INTEGER NOT NULL,epoch INTEGER NOT NULL,observed_at REAL NOT NULL DEFAULT 0,created REAL NOT NULL,updated REAL NOT NULL);
 CREATE TABLE IF NOT EXISTS seat_operations(id TEXT PRIMARY KEY,seat_id TEXT NOT NULL REFERENCES seats(id),host_id TEXT NOT NULL REFERENCES registered_hosts(id),seat_epoch INTEGER NOT NULL,operation TEXT NOT NULL,state TEXT NOT NULL,requested_by TEXT NOT NULL REFERENCES actors(id),runtime_ref TEXT NOT NULL DEFAULT '',observed_state TEXT NOT NULL DEFAULT 'unknown',evidence TEXT NOT NULL DEFAULT '',created REAL NOT NULL,updated REAL NOT NULL);
 CREATE INDEX IF NOT EXISTS operations_host_state ON seat_operations(host_id,state,created);
 CREATE INDEX IF NOT EXISTS seats_host ON seats(host_id);
 ''')
 for key,(name,root,source) in projects.items():
  d.execute('INSERT OR IGNORE INTO registered_projects VALUES(?,?,?,?,1,?)',(key,name,root,source,time.time()))

def manager(d,a,project=None):
 if not d.execute('SELECT 1 FROM actors WHERE id=? AND revoked=0',(a['id'],)).fetchone():return False
 if a['role']=='owner':return True
 r=d.execute('SELECT * FROM governance WHERE actor_id=?',(a['id'],)).fetchone()
 if not r or r['role'] not in ('leader','deputy') or (r['expires_at'] and r['expires_at']<=time.time()):return False
 projects=json.loads(r['projects_json'])
 return project is None or project in projects or '*' in projects

def require_manager(d,a,project=None):
 if not manager(d,a,project):raise Fault('workstation_management_denied',403)

def require_projects(d,a,projects):
 for p in projects:
  if p!='*' and not d.execute('SELECT 1 FROM registered_projects WHERE id=?',(p,)).fetchone():raise Fault('unknown_project')
  require_manager(d,a,None if p=='*' else p)
  if p=='*' and a['role']!='owner':
   g=d.execute('SELECT projects_json FROM governance WHERE actor_id=?',(a['id'],)).fetchone()
   if not g or '*' not in json.loads(g[0]):raise Fault('workstation_management_denied',403)

def host_access(d,a,h):
 if a['id']==h['actor_id']:return
 require_projects(d,a,json.loads(h['projects_json']))

def requester_authorized(d,op,project):
 a=d.execute('SELECT * FROM actors WHERE id=? AND revoked=0',(op['requested_by'],)).fetchone()
 return bool(a and manager(d,a,project))

def host_manageable(d,a,projects):
 if a['role']=='owner':return True
 if '*' in projects:
  g=d.execute('SELECT projects_json FROM governance WHERE actor_id=?',(a['id'],)).fetchone()
  return bool(manager(d,a) and g and '*' in json.loads(g[0]))
 return all(manager(d,a,p) for p in projects)

def active_actor(d,value,project=None):
 aid=ident(value);r=d.execute('SELECT * FROM actors WHERE id=? AND revoked=0',(aid,)).fetchone()
 if not r or r['role']=='owner':raise Fault('worker_identity_required',400)
 if project and r['project'] not in ('coordination',project):raise Fault('worker_project_mismatch',403)
 return aid

def list_projects(d):return [dict(x) for x in d.execute('SELECT * FROM registered_projects ORDER BY id')]

def snapshot(d,a):
 now=time.time();hosts=[public(x) for x in d.execute('SELECT * FROM registered_hosts ORDER BY id')]
 for h in hosts:
  h['online']=0<now-h['last_seen']<180
  h['active_reservations']=d.execute("SELECT count(*) FROM seats WHERE host_id=? AND (desired_state='active' OR runtime_ref!='')",(h['id'],)).fetchone()[0]
  h['can_manage']=host_manageable(d,a,h['projects'])
 seats=[dict(x) for x in d.execute('SELECT * FROM seats ORDER BY created')]
 for s in seats:
  s['stale']=not s['observed_at'] or now-s['observed_at']>=180
  s['last_known_state']=s['observed_state']
  if s['stale']:s['observed_state']='unknown'
  s['can_manage']=manager(d,a,s['project'])
  s['pending_operation']=d.execute("SELECT id FROM seat_operations WHERE seat_id=? AND state IN ('pending','accepted','unknown') ORDER BY created DESC LIMIT 1",(s['id'],)).fetchone()
  s['pending_operation']=s['pending_operation'][0] if s['pending_operation'] else None
 grants=[public(x) for x in d.execute('SELECT g.*,a.name,a.revoked AS actor_revoked FROM governance g JOIN actors a ON a.id=g.actor_id ORDER BY g.updated')]
 for g in grants:g['active']=not g['actor_revoked'] and g['role']!='revoked' and not (g['expires_at'] and g['expires_at']<=now)
 return {'version':1,'server_time':now,'projects':list_projects(d),'governance':grants,'hosts':hosts,'adapters':[public(x) for x in d.execute('SELECT * FROM registered_adapters ORDER BY host_id,id')],'seats':seats,'operations':[dict(x) for x in d.execute('SELECT * FROM seat_operations ORDER BY created DESC LIMIT 100')],'capabilities':{'manage_workstations':manager(d,a),'appoint_leaders':a['role']=='owner','execution_in_center':False,'automatic_expansion':False}}

def host_operations(d,a,host_id):
 h=row(d,'registered_hosts',host_id)
 if a['id']!=h['actor_id'] and a['role']!='owner':raise Fault('host_identity_required',403)
 # Reading the queue never claims or executes it. Unknown operations require
 # reconciliation of the same ID; they must not be treated as new work.
 items=[dict(x) for x in d.execute("SELECT o.*,s.adapter_id,s.project,s.scope,s.ownership,s.runtime_ref AS current_runtime_ref,s.desired_state FROM seat_operations o JOIN seats s ON s.id=o.seat_id WHERE o.host_id=? AND o.state IN ('pending','accepted','unknown') ORDER BY o.created LIMIT 100",(h['id'],))]
 for op in items:op['requester_authorized']=requester_authorized(d,op,op['project'])
 return {'host_id':h['id'],'operations':items}

def write(d,a,action,b,now):
 if action=='seat-attach':
  required={'request_id','id','version','seat_epoch','runtime_ref','adapter_version','observed_state','observed_at','evidence_ref','evidence_sha256'}
  if set(b)!=required:raise Fault('invalid_seat_attachment')
  s=row(d,'seats',b['id']);h=row(d,'registered_hosts',s['host_id']);adapter=row(d,'registered_adapters',s['adapter_id'])
  if a['id']!=h['actor_id']:raise Fault('host_identity_required',403)
  version(b,s)
  if type(b['seat_epoch']) is not int or b['seat_epoch']!=s['epoch'] or b['adapter_version']!=adapter['version_label']:raise Fault('seat_observation_binding_mismatch',409)
  if s['ownership']!='managed' or s['runtime_ref'] or s['desired_state']!='registered':raise Fault('seat_not_attachable',409)
  if d.execute("SELECT 1 FROM seat_operations WHERE seat_id=? AND state IN ('pending','accepted','unknown')",(s['id'],)).fetchone():raise Fault('seat_has_pending_operation',409)
  if 'inspect' not in json.loads(adapter['actions_json']) or adapter['availability']=='unsupported':raise Fault('adapter_operation_unavailable',409)
  ref=safe_ref(b['runtime_ref'])
  if not ref:raise Fault('runtime_reference_required')
  state=b['observed_state'];stamp=b['observed_at']
  if state not in ('idle','running','paused') or type(stamp) not in (int,float) or not math.isfinite(stamp) or not now-180<=stamp<=now+120:raise Fault('invalid_seat_attachment')
  ident(b['evidence_ref'])
  import re
  if not isinstance(b['evidence_sha256'],str) or not re.fullmatch('[0-9a-f]{64}',b['evidence_sha256']):raise Fault('invalid_seat_attachment')
  if s['actor_id']:active_actor(d,s['actor_id'],s['project'])
  used=d.execute("SELECT count(*) FROM seats WHERE host_id=? AND id!=? AND (desired_state='active' OR runtime_ref!='')",(h['id'],s['id'])).fetchone()[0]
  if used>=h['max_active']:raise Fault('active_capacity_exceeded',409)
  if d.execute('SELECT 1 FROM seats WHERE host_id=? AND id!=? AND runtime_ref=?',(h['id'],s['id'],ref)).fetchone():raise Fault('runtime_already_bound',409)
  for other in d.execute("SELECT scope FROM seats WHERE project=? AND id!=? AND (desired_state='active' OR runtime_ref!='')",(s['project'],s['id'])):
   if overlaps(s['scope'],other['scope']):raise Fault('seat_scope_conflict',409)
  desired='paused' if state=='paused' else 'active'
  d.execute('UPDATE seats SET runtime_ref=?,desired_state=?,observed_state=?,observed_at=?,version=version+1,updated=? WHERE id=?',(ref,desired,state,stamp,now,s['id']))
  d.execute('UPDATE registered_hosts SET last_seen=? WHERE id=?',(now,h['id']))
  d.execute('UPDATE registered_adapters SET observed_at=? WHERE id=?',(stamp,adapter['id']))
  return {'seat':row(d,'seats',s['id']),'evidence_ref':b['evidence_ref'],'evidence_sha256':b['evidence_sha256']}
 if action=='seat-observe':
  required={'request_id','id','version','seat_epoch','runtime_ref','adapter_version','observed_state','observed_at','evidence_ref','evidence_sha256'}
  if set(b)!=required:raise Fault('invalid_seat_observation')
  s=row(d,'seats',b['id']);h=row(d,'registered_hosts',s['host_id']);adapter=row(d,'registered_adapters',s['adapter_id'])
  if a['id']!=h['actor_id']:raise Fault('host_identity_required',403)
  version(b,s)
  if not s['runtime_ref'] or b['runtime_ref']!=s['runtime_ref'] or type(b['seat_epoch']) is not int or b['seat_epoch']!=s['epoch'] or b['adapter_version']!=adapter['version_label']:raise Fault('seat_observation_binding_mismatch',409)
  if 'inspect' not in json.loads(adapter['actions_json']):raise Fault('adapter_operation_unavailable',409)
  state=b['observed_state'];stamp=b['observed_at']
  if state not in ('idle','running','paused','unknown','unavailable') or type(stamp) not in (int,float) or not math.isfinite(stamp) or not max(s['observed_at'],now-180)<=stamp<=now+120:raise Fault('invalid_seat_observation')
  ident(b['evidence_ref'])
  import re
  if not isinstance(b['evidence_sha256'],str) or not re.fullmatch('[0-9a-f]{64}',b['evidence_sha256']):raise Fault('invalid_seat_observation')
  d.execute('UPDATE seats SET observed_state=?,observed_at=?,version=version+1,updated=? WHERE id=?',(state,stamp,now,s['id']))
  d.execute('UPDATE registered_hosts SET last_seen=? WHERE id=?',(now,h['id']))
  d.execute('UPDATE registered_adapters SET observed_at=? WHERE id=?',(stamp,adapter['id']))
  # The response in the existing durable request ledger retains evidence attribution.
  return {'seat':row(d,'seats',s['id']),'evidence_ref':b['evidence_ref'],'evidence_sha256':b['evidence_sha256']}
 if action=='project-register':
  oid=ident(b.get('id'));require_manager(d,a,oid)
  old=d.execute('SELECT * FROM registered_projects WHERE id=?',(oid,)).fetchone();version(b,old)
  name=text(b.get('name'),100);root=safe_ref(b.get('root',''));source=safe_ref(b.get('source',''))
  d.execute('INSERT INTO registered_projects VALUES(?,?,?,?,1,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,root=excluded.root,source=excluded.source,version=registered_projects.version+1,updated=excluded.updated',(oid,name,root,source,now))
  return row(d,'registered_projects',oid)
 if action=='governance-grant':
  if a['role']!='owner':raise Fault('owner_required',403)
  aid=active_actor(d,b.get('actor_id'));old=d.execute('SELECT * FROM governance WHERE actor_id=?',(aid,)).fetchone();version(b,old)
  role=b.get('role')
  if role not in ('leader','deputy','revoked'):raise Fault('invalid_governance_role')
  projects=b.get('projects',[])
  if projects==['*']:pass
  else:projects=names(projects)
  require_projects(d,a,projects)
  exp=b.get('expires_at',0)
  if type(exp) not in (int,float) or not math.isfinite(exp) or exp<0 or (exp and exp<=now):raise Fault('invalid_expiry')
  reason=text(b.get('reason'),600)
  d.execute('INSERT INTO governance VALUES(?,?,?,?,1,?,?,?) ON CONFLICT(actor_id) DO UPDATE SET role=excluded.role,projects_json=excluded.projects_json,expires_at=excluded.expires_at,version=governance.version+1,reason=excluded.reason,granted_by=excluded.granted_by,updated=excluded.updated',(aid,role,json.dumps(projects),exp,reason,a['id'],now))
  return public(d.execute('SELECT * FROM governance WHERE actor_id=?',(aid,)).fetchone())
 if action=='host-register':
  oid=ident(b.get('id'));old=d.execute('SELECT * FROM registered_hosts WHERE id=?',(oid,)).fetchone();version(b,old)
  if old:host_access(d,a,old)
  projects=b.get('projects',[])
  if projects!=['*']:projects=names(projects)
  require_projects(d,a,projects)
  aid=active_actor(d,b.get('actor_id'));name=text(b.get('name'),100)
  active=integer(b.get('max_active',4),1,32);seats=integer(b.get('max_seats',16),active,256)
  if old:
   if aid!=old['actor_id'] and d.execute("SELECT 1 FROM seat_operations WHERE host_id=? AND state IN ('pending','accepted','unknown')",(oid,)).fetchone():raise Fault('host_has_pending_operations',409)
   if aid!=old['actor_id'] and d.execute("SELECT 1 FROM seats WHERE host_id=? AND (desired_state='active' OR runtime_ref!='')",(oid,)).fetchone():raise Fault('host_has_bound_runtime',409)
   count=d.execute('SELECT count(*) FROM seats WHERE host_id=?',(oid,)).fetchone()[0]
   reserved=d.execute("SELECT count(*) FROM seats WHERE host_id=? AND (desired_state='active' OR runtime_ref!='')",(oid,)).fetchone()[0]
   if count>seats or reserved>active:raise Fault('capacity_below_existing_work',409)
   for seat in d.execute('SELECT project FROM seats WHERE host_id=?',(oid,)):
    if '*' not in projects and seat['project'] not in projects:raise Fault('host_project_in_use',409)
  d.execute('INSERT INTO registered_hosts(id,name,actor_id,projects_json,max_active,max_seats,version,updated) VALUES(?,?,?,?,?,?,1,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,actor_id=excluded.actor_id,projects_json=excluded.projects_json,max_active=excluded.max_active,max_seats=excluded.max_seats,version=registered_hosts.version+1,updated=excluded.updated',(oid,name,aid,json.dumps(projects),active,seats,now))
  if old and aid!=old['actor_id']:
   d.execute("UPDATE registered_adapters SET availability='unavailable',version=version+1,observed_at=0 WHERE host_id=?",(oid,))
   d.execute('UPDATE registered_hosts SET last_seen=0 WHERE id=?',(oid,))
  return public(row(d,'registered_hosts',oid))
 if action=='adapter-register':
  oid=ident(b.get('id'));h=row(d,'registered_hosts',b.get('host_id'));host_access(d,a,h)
  old=d.execute('SELECT * FROM registered_adapters WHERE id=?',(oid,)).fetchone();version(b,old)
  if old and old['host_id']!=h['id']:raise Fault('adapter_host_immutable',409)
  actions=names(b.get('actions'),ADAPTER_CAPABILITIES);availability=b.get('availability')
  if availability not in ('available','unavailable','unsupported'):raise Fault('invalid_availability')
  d.execute('INSERT INTO registered_adapters(id,host_id,name,kind,version_label,actions_json,availability,version,observed_at) VALUES(?,?,?,?,?,?,?,1,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,kind=excluded.kind,version_label=excluded.version_label,actions_json=excluded.actions_json,availability=excluded.availability,version=registered_adapters.version+1,observed_at=excluded.observed_at',(oid,h['id'],text(b.get('name'),100),ident(b.get('kind')),text(b.get('version_label',''),80,empty=True),json.dumps(actions),availability,now))
  # Registration observes capabilities; it does not imply any model is running.
  if a['id']==h['actor_id']:d.execute('UPDATE registered_hosts SET last_seen=? WHERE id=?',(now,h['id']))
  return public(row(d,'registered_adapters',oid))
 if action=='seat-create':
  oid=ident(b.get('id'));project=ident(b.get('project'));require_manager(d,a,project);row(d,'registered_projects',project)
  h=row(d,'registered_hosts',b.get('host_id'));adapter=row(d,'registered_adapters',b.get('adapter_id'))
  if adapter['host_id']!=h['id']:raise Fault('adapter_host_mismatch')
  if '*' not in json.loads(h['projects_json']) and project not in json.loads(h['projects_json']):raise Fault('host_project_denied',403)
  if d.execute('SELECT count(*) FROM seats WHERE host_id=?',(h['id'],)).fetchone()[0]>=h['max_seats']:raise Fault('seat_capacity_exceeded',409)
  aid=active_actor(d,b['actor_id'],project) if b.get('actor_id') else None
  ownership=b.get('ownership','managed')
  if ownership not in ('managed','external'):raise Fault('invalid_ownership')
  ref=safe_ref(b.get('runtime_ref',''))
  if ownership=='managed' and ref:raise Fault('new_managed_seat_cannot_adopt_session')
  if ref and d.execute('SELECT 1 FROM seats WHERE host_id=? AND runtime_ref=?',(h['id'],ref)).fetchone():raise Fault('runtime_already_bound',409)
  if ref:
   used=d.execute("SELECT count(*) FROM seats WHERE host_id=? AND (desired_state='active' OR runtime_ref!='')",(h['id'],)).fetchone()[0]
   if used>=h['max_active']:raise Fault('active_capacity_exceeded',409)
   for other in d.execute("SELECT scope FROM seats WHERE project=? AND (desired_state='active' OR runtime_ref!='')",(project,)):
    if overlaps(scope(b.get('scope')),other['scope']):raise Fault('seat_scope_conflict',409)
  d.execute('INSERT INTO seats VALUES(?,?,?,?,?,?,?,?,?,?,?,1,1,0,?,?)',(oid,text(b.get('name'),100),project,scope(b.get('scope')),aid,h['id'],adapter['id'],ownership,'registered','unknown',ref,now,now))
  return row(d,'seats',oid)
 if action=='seat-update':
  s=row(d,'seats',b.get('id'));require_manager(d,a,s['project']);version(b,s)
  if d.execute("SELECT 1 FROM seat_operations WHERE seat_id=? AND state IN ('pending','accepted','unknown')",(s['id'],)).fetchone():raise Fault('seat_has_pending_operation',409)
  aid=active_actor(d,b['actor_id'],s['project']) if b.get('actor_id') else (None if 'actor_id' in b else s['actor_id'])
  new_scope=scope(b.get('scope',s['scope']))
  if (aid!=s['actor_id'] or new_scope!=s['scope']) and (s['desired_state']=='active' or s['runtime_ref']):raise Fault('close_managed_runtime_before_handover',409)
  handover=aid!=s['actor_id'] or new_scope!=s['scope']
  d.execute('UPDATE seats SET name=?,scope=?,actor_id=?,epoch=epoch+?,version=version+1,updated=? WHERE id=?',(text(b.get('name',s['name']),100),new_scope,aid,int(handover),now,s['id']))
  if handover:d.execute("UPDATE seats SET observed_state='unknown',observed_at=0 WHERE id=?",(s['id'],))
  return row(d,'seats',s['id'])
 if action=='seat-control':
  s=row(d,'seats',b.get('id'));require_manager(d,a,s['project']);version(b,s)
  op=b.get('operation')
  if op not in OPERATIONS:raise Fault('unsupported_operation')
  if d.execute("SELECT 1 FROM seat_operations WHERE seat_id=? AND state IN ('pending','accepted','unknown')",(s['id'],)).fetchone():raise Fault('seat_has_pending_operation',409)
  h=row(d,'registered_hosts',s['host_id']);adapter=row(d,'registered_adapters',s['adapter_id'])
  if adapter['availability']!='available' or op not in json.loads(adapter['actions_json']):raise Fault('adapter_operation_unavailable',409)
  if s['ownership']=='external' and op in ('open','close'):raise Fault('external_runtime_not_owned',403)
  if op=='open' and s['runtime_ref']:raise Fault('runtime_already_open',409)
  if op in ('resume','interrupt','close') and not s['runtime_ref']:raise Fault('runtime_not_bound',409)
  if op in ('open','resume'):
   if s['actor_id']:active_actor(d,s['actor_id'],s['project'])
   # A paused but existing runtime still occupies capacity.
   used=d.execute("SELECT count(*) FROM seats WHERE host_id=? AND id!=? AND (desired_state='active' OR runtime_ref!='')",(h['id'],s['id'])).fetchone()[0]
   if used>=h['max_active']:raise Fault('active_capacity_exceeded',409)
   for other in d.execute("SELECT * FROM seats WHERE project=? AND id!=? AND (desired_state='active' OR runtime_ref!='')",(s['project'],s['id'])):
    if overlaps(s['scope'],other['scope']):raise Fault('seat_scope_conflict',409)
  desired={'open':'active','resume':'active','pause':'paused','close':'closed'}.get(op,s['desired_state'])
  oid='op-'+uuid.uuid4().hex
  d.execute('UPDATE seats SET desired_state=?,version=version+1,updated=? WHERE id=?',(desired,now,s['id']))
  d.execute('INSERT INTO seat_operations(id,seat_id,host_id,seat_epoch,operation,state,requested_by,created,updated) VALUES(?,?,?,?,?,\'pending\',?,?,?)',(oid,s['id'],h['id'],s['epoch'],op,a['id'],now,now))
  return {'operation':row(d,'seat_operations',oid),'seat':row(d,'seats',s['id'])}
 if action=='operation-receipt':
  op=row(d,'seat_operations',b.get('operation_id'));h=row(d,'registered_hosts',op['host_id'])
  if a['id']!=h['actor_id']:raise Fault('host_identity_required',403)
  s=row(d,'seats',op['seat_id'])
  if type(b.get('seat_epoch')) is not int or b['seat_epoch']!=s['epoch'] or op['seat_epoch']!=s['epoch']:raise Fault('stale_seat_epoch',409)
  state=b.get('state');observed=b.get('observed_state','unknown');ref=safe_ref(b.get('runtime_ref',s['runtime_ref']));evidence=text(b.get('evidence',''),1500,empty=True)
  if state not in ('accepted','succeeded','failed','unknown') or observed not in STATES:raise Fault('invalid_receipt_state')
  if state=='accepted' and not requester_authorized(d,op,s['project']):raise Fault('operation_authorization_revoked',403)
  if op['state'] in ('succeeded','failed'):
   if state==op['state'] and ref==op['runtime_ref'] and observed==op['observed_state'] and evidence==op['evidence']:return {'operation':op,'seat':s}
   raise Fault('operation_terminal',409)
  if state=='accepted' and op['state']!='pending':raise Fault('receipt_transition_conflict',409)
  if op['state']=='pending' and state=='succeeded':raise Fault('operation_not_accepted',409)
  if state=='succeeded':
   if not evidence:raise Fault('runtime_evidence_required')
   if op['operation'] in ('open','resume') and (not ref or observed not in ('idle','running')):raise Fault('runtime_evidence_required')
   if op['operation']=='close' and (ref or observed!='closed'):raise Fault('closed_runtime_required')
   if op['operation']=='pause' and observed!='paused':raise Fault('paused_runtime_required')
   if op['operation']=='interrupt' and observed not in ('idle','paused'):raise Fault('interrupted_runtime_required')
   if op['operation'] not in ('open','close') and ref!=s['runtime_ref']:raise Fault('runtime_reference_immutable',409)
   if ref and d.execute('SELECT 1 FROM seats WHERE host_id=? AND id!=? AND runtime_ref=?',(h['id'],s['id'],ref)).fetchone():raise Fault('runtime_already_bound',409)
  d.execute('UPDATE seat_operations SET state=?,runtime_ref=?,observed_state=?,evidence=?,updated=? WHERE id=?',(state,ref,observed,evidence,now,op['id']))
  if state=='succeeded':
   d.execute('UPDATE seats SET runtime_ref=?,observed_state=?,observed_at=?,version=version+1,updated=? WHERE id=?',(ref,observed,now,now,s['id']))
  elif state=='unknown':d.execute("UPDATE seats SET observed_state='unknown',observed_at=0,version=version+1,updated=? WHERE id=?",(now,s['id']))
  elif state=='failed':
   desired='registered' if not s['runtime_ref'] else 'paused'
   d.execute("UPDATE seats SET desired_state=?,observed_state='unknown',observed_at=0,version=version+1,updated=? WHERE id=?",(desired,now,s['id']))
  d.execute('UPDATE registered_hosts SET last_seen=? WHERE id=?',(now,h['id']))
  return {'operation':row(d,'seat_operations',op['id']),'seat':row(d,'seats',s['id'])}
 raise Fault('not_found',404)
