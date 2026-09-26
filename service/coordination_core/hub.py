"""Aieyra coordination plane. No shell, model, business DB or deployment tools."""
from __future__ import annotations
import argparse,hashlib,hmac,json,os,re,secrets,sqlite3,time,uuid,threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import urlparse,parse_qs
from . import registry
from . import mobile
from . import jobs
from . import collaboration
from . import library
from . import intake

VERSION='0.5.5'
PROJECTS={}

from .core import Fault, text, ident, scope, overlaps

class Connection(sqlite3.Connection):
 def __exit__(self,*args):
  try:return super().__exit__(*args)
  finally:self.close()

class Store:
 def __init__(self,path,mobile_origin=None):
  self.path=Path(path);self.schema_lock=threading.Lock();self.schema_ready=False
  self.mobile=mobile.Mobile(self,mobile_origin)
 def db(self):
  d=sqlite3.connect(self.path,timeout=8,factory=Connection);d.row_factory=sqlite3.Row
  d.execute('PRAGMA foreign_keys=ON');d.execute('PRAGMA busy_timeout=8000')
  with self.schema_lock:
   if not self.schema_ready:
    registry.migrate(d, PROJECTS)
    mobile.migrate(d)
    jobs.migrate(d)
    collaboration.migrate(d)
    library.migrate(d)
    intake.migrate(d)
    d.commit();self.schema_ready=True
  return d
 def init(self,owner_token):
  self.path.parent.mkdir(parents=True,exist_ok=True)
  with self.db() as d:
   d.executescript('''PRAGMA journal_mode=WAL;
   CREATE TABLE IF NOT EXISTS actors(id TEXT PRIMARY KEY, name TEXT NOT NULL,role TEXT NOT NULL,project TEXT NOT NULL,token_hash TEXT UNIQUE NOT NULL,created REAL NOT NULL,last_seen REAL NOT NULL DEFAULT 0,revoked INTEGER NOT NULL DEFAULT 0);
   CREATE TABLE IF NOT EXISTS messages(seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE NOT NULL,sender TEXT NOT NULL REFERENCES actors(id),project TEXT NOT NULL,kind TEXT NOT NULL,body TEXT NOT NULL,reply_to TEXT,created REAL NOT NULL);
   CREATE TABLE IF NOT EXISTS receipts(message_id TEXT NOT NULL REFERENCES messages(id),actor_id TEXT NOT NULL REFERENCES actors(id),read_at REAL NOT NULL,PRIMARY KEY(message_id,actor_id));
   CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,project TEXT NOT NULL,title TEXT NOT NULL,scope TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'planned',owner TEXT REFERENCES actors(id),lease_until REAL NOT NULL DEFAULT 0,version INTEGER NOT NULL DEFAULT 1,evidence TEXT NOT NULL DEFAULT '',accepted_by TEXT REFERENCES actors(id),created REAL NOT NULL,updated REAL NOT NULL);
   CREATE TABLE IF NOT EXISTS memories(seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE NOT NULL,project TEXT NOT NULL,kind TEXT NOT NULL,body TEXT NOT NULL,source TEXT NOT NULL,supersedes TEXT REFERENCES memories(id),actor TEXT NOT NULL REFERENCES actors(id),created REAL NOT NULL);
   CREATE TABLE IF NOT EXISTS requests(actor TEXT NOT NULL,request_id TEXT NOT NULL,digest TEXT NOT NULL,response TEXT NOT NULL,PRIMARY KEY(actor,request_id));
   CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,actor TEXT NOT NULL,action TEXT NOT NULL,object_id TEXT,created REAL NOT NULL);
   CREATE INDEX IF NOT EXISTS message_project_seq ON messages(project,seq);
   CREATE INDEX IF NOT EXISTS task_project ON tasks(project,status);
   ''')
   if d.execute('SELECT 1 FROM actors WHERE id=?',('owner',)).fetchone():raise Fault('already_initialized',409)
   d.execute('INSERT INTO actors(id,name,role,project,token_hash,created) VALUES(?,?,?,?,?,?)',('owner','站长','owner','coordination',hashlib.sha256(owner_token.encode()).hexdigest(),time.time()))
 def authenticate(self,token):
  if not token or len(token)>200:raise Fault('unauthorized',401)
  with self.db() as d:a=d.execute('SELECT id,name,role,project FROM actors WHERE token_hash=? AND revoked=0',(hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
  if not a:raise Fault('unauthorized',401)
  return dict(a)
 def can_project(self,a,p,d):
  if not d.execute('SELECT 1 FROM registered_projects WHERE id=?',(ident(p),)).fetchone():raise Fault('unknown_project')
  if a['role']!='owner' and a['project'] not in (p,'coordination'):raise Fault('project_scope_denied',403)
 def read(self,a,path,q):
  with self.db() as d:
   if not d.execute('SELECT 1 FROM actors WHERE id=? AND revoked=0',(a['id'],)).fetchone():raise Fault('unauthorized',401)
   if path=='/v1/registry':
    return registry.snapshot(d,a)
   if path in library.READS:
    d.execute('BEGIN')
    return library.read(d,a,path,q,time.time())
   if path in intake.READS:
    d.execute('BEGIN')
    return intake.read(d,a,path,q,time.time())
   if path in collaboration.READS:
    d.execute('BEGIN IMMEDIATE')
    return collaboration.read(d,a,path,q,time.time())
   if path in ('/v1/product-job','/v1/product-job-outbox','/v1/product-job-receipts'):
    d.execute('BEGIN IMMEDIATE')
    return jobs.read(d,a,path,q,time.time())
   if path=='/v1/devices':
    return mobile.device_list(d,a)
   if path=='/v1/mobile-outbox':
    d.execute('BEGIN IMMEDIATE')
    return mobile.host_outbox(d,a,q.get('host_id',[''])[0])
   if path=='/v1/host-operations':
    host_id=q.get('host_id',[''])[0]
    return registry.host_operations(d,a,host_id)
   if path=='/v1/status':
    agents=[dict(x) for x in d.execute('SELECT id,name,role,project,last_seen,created FROM actors WHERE revoked=0 ORDER BY created')]
    now=time.time()
    for r in agents:r['online']=0<=now-r['last_seen']<180
    return {'version':VERSION,'server_time':now,'actor':a,'projects':registry.list_projects(d),
     'agents':agents,'tasks':[dict(x) for x in d.execute('SELECT * FROM tasks ORDER BY updated DESC LIMIT 100')],
     'messages':self.messages(d,a,'1=1',(),True,100),
     'memories':[dict(x) for x in d.execute('SELECT * FROM memories ORDER BY seq DESC LIMIT 60')],
     'events':[dict(x) for x in d.execute('SELECT * FROM events ORDER BY seq DESC LIMIT 30')],
     'capabilities':{'collaboration':True,'execution':False,'model_chat':False,'customer_feedback_ingestion':False,'apk':False}}
   if path=='/v1/inbox':
    try:after=max(0,int(q.get('after',['0'])[0]));limit=min(100,max(1,int(q.get('limit',['50'])[0])))
    except ValueError:raise Fault('invalid_cursor')
    rows=[dict(x) for x in d.execute('SELECT m.*,a.name AS sender_name,a.role AS sender_role,r.read_at FROM messages m JOIN actors a ON m.sender=a.id LEFT JOIN receipts r ON r.message_id=m.id AND r.actor_id=? WHERE m.seq>? ORDER BY m.seq LIMIT ?',(a['id'],after,limit))]
    return {'messages':rows,'next_cursor':rows[-1]['seq'] if rows else after}
   if path=='/v1/history':
    try:before=max(1,int(q.get('before',['9223372036854775807'])[0]))
    except ValueError:raise Fault('invalid_cursor')
    rows=self.messages(d,a,'m.seq<?',(before,),True,100)
    return {'messages':rows,'has_more':bool(rows and d.execute('SELECT 1 FROM messages WHERE seq<?',(rows[-1]['seq'],)).fetchone())}
   raise Fault('not_found',404)
 def messages(self,d,a,where,args,descending,limit):
  sql='SELECT m.*,a.name AS sender_name,a.role AS sender_role,r.read_at,(SELECT COUNT(*) FROM receipts rr WHERE rr.message_id=m.id AND rr.actor_id!=m.sender) AS receipt_count FROM messages m JOIN actors a ON m.sender=a.id LEFT JOIN receipts r ON r.message_id=m.id AND r.actor_id=? WHERE '+where+' ORDER BY m.seq '+('DESC' if descending else 'ASC')+' LIMIT ?'
  return [dict(x) for x in d.execute(sql,(a['id'],*args,limit))]
 def write(self,a,action,b):
  rid=ident(b.get('request_id'));canonical=json.dumps({'action':action,'body':b},sort_keys=True,separators=(',',':'))
  digest=hashlib.sha256(canonical.encode()).hexdigest()
  d=self.db()
  try:
   d.execute('BEGIN IMMEDIATE')
   current=d.execute('SELECT id,name,role,project FROM actors WHERE id=? AND revoked=0',(a['id'],)).fetchone()
   if not current:raise Fault('unauthorized',401)
   a=dict(current)
   previous=d.execute('SELECT digest,response FROM requests WHERE actor=? AND request_id=?',(a['id'],rid)).fetchone()
   if previous:
    if not hmac.compare_digest(previous['digest'],digest):raise Fault('idempotency_conflict',409)
    # Registration replay never returns a persisted raw token.
    replay=json.loads(previous['response'])
    if action=='product-job-begin':
     replay['dispatch_allowed']=False
     replay['replay_allowed']=False
    if action=='human-request-begin':
     replay['callback_allowed']=False
     replay['replay_allowed']=False
    return replay
   now=time.time();out={};oid=None
   if action in intake.ACTIONS:
    out=intake.write(d,a,action,b,now)
   elif action in library.ACTIONS:
    out=library.write(d,a,action,b,now)
   elif action in collaboration.ACTIONS:
    out=collaboration.write(d,a,action,b,now)
    oid=out.get('id')
   elif action in jobs.ACTIONS:
    out=jobs.write(d,a,action,b,now)
    oid=out.get('id',out.get('job_id'))
   elif action in mobile.ADMIN_ACTIONS:
    if action=='device-invite':self.mobile.enabled()
    out=mobile.admin_write(d,a,action,b,now)
   elif action in registry.ACTIONS:
    out=registry.write(d,a,action,b,now)
   elif action=='heartbeat':
    d.execute('UPDATE actors SET last_seen=? WHERE id=?',(now,a['id']));out={'seen_at':now}
   elif action=='register':
    if a['role']!='owner':raise Fault('owner_required',403)
    name=text(b.get('name'),80);project=b.get('project');self.can_project(a,project,d)
    oid='agent-'+uuid.uuid4().hex[:16];token=secrets.token_urlsafe(32)
    d.execute('INSERT INTO actors(id,name,role,project,token_hash,created,last_seen) VALUES(?,?,?,?,?,?,?)',(oid,name,'agent',project,hashlib.sha256(token.encode()).hexdigest(),now,0))
    out={'id':oid,'name':name,'project':project,'token':token}
   elif action=='revoke':
    if a['role']!='owner':raise Fault('owner_required',403)
    oid=ident(b.get('id'))
    if oid=='owner':raise Fault('cannot_revoke_owner')
    if not d.execute('UPDATE actors SET revoked=1 WHERE id=?',(oid,)).rowcount:raise Fault('actor_missing',404)
    out={'id':oid,'revoked':True}
   elif action=='message':
    p=b.get('project','coordination')
    if not d.execute('SELECT 1 FROM registered_projects WHERE id=?',(ident(p),)).fetchone():raise Fault('unknown_project')
    kind=b.get('kind','discussion')
    if kind not in ('discussion','handoff','progress','blocker','proposal','decision'):raise Fault('invalid_kind')
    if kind=='decision' and a['role']!='owner':raise Fault('owner_required_for_decision',403)
    body=text(b.get('body'));reply=b.get('reply_to')
    if reply and not d.execute('SELECT 1 FROM messages WHERE id=?',(ident(reply),)).fetchone():raise Fault('reply_missing',404)
    oid=uuid.uuid4().hex;d.execute('INSERT INTO messages(id,sender,project,kind,body,reply_to,created) VALUES(?,?,?,?,?,?,?)',(oid,a['id'],p,kind,body,reply,now))
    out={'id':oid,'seq':d.execute('SELECT seq FROM messages WHERE id=?',(oid,)).fetchone()[0]}
   elif action=='ack':
    ids=b.get('ids')
    if not isinstance(ids,list) or len(ids)>100:raise Fault('invalid_receipt')
    for mid in ids:
     if not d.execute('SELECT 1 FROM messages WHERE id=?',(ident(mid),)).fetchone():raise Fault('message_missing',404)
     d.execute('INSERT OR IGNORE INTO receipts VALUES(?,?,?)',(mid,a['id'],now))
    out={'acknowledged':len(set(ids))}
   elif action=='task-create':
    p=b.get('project');self.can_project(a,p,d);title=text(b.get('title'),200);sc=scope(b.get('scope'));oid=ident(b.get('id') or 'task-'+uuid.uuid4().hex[:12])
    d.execute('INSERT INTO tasks(id,project,title,scope,created,updated) VALUES(?,?,?,?,?,?)',(oid,p,title,sc,now,now));out={'id':oid,'version':1}
   elif action in ('task-claim','task-update','task-accept'):
    oid=ident(b.get('id'));t=d.execute('SELECT * FROM tasks WHERE id=?',(oid,)).fetchone()
    if not t:raise Fault('task_missing',404)
    self.can_project(a,t['project'],d)
    if type(b.get('version')) is not int or b['version']!=t['version']:raise Fault('version_conflict',409)
    if action=='task-accept':
     if a['role']!='owner' or t['status']!='delivered':raise Fault('owner_acceptance_required',403)
     evidence=text(b.get('evidence'),1500)
     d.execute("UPDATE tasks SET status='accepted',accepted_by=?,version=version+1,evidence=?,updated=? WHERE id=?",(a['id'],t['evidence']+'\n验收：'+evidence,now,oid))
    elif action=='task-claim':
     if t['status'] in ('accepted','delivered','cancelled','superseded'):raise Fault('task_not_claimable',409)
     if t['lease_until']>now and t['owner']!=a['id']:raise Fault('lease_held',409)
     for other in d.execute('SELECT * FROM tasks WHERE project=? AND id!=? AND lease_until>?',(t['project'],oid,now)):
      if overlaps(t['scope'],other['scope']):raise Fault('scope_already_claimed',409)
     ttl=b.get('ttl',900)
     if type(ttl) is not int or not 30<=ttl<=1800:raise Fault('invalid_lease')
     d.execute("UPDATE tasks SET owner=?,status='doing',lease_until=?,version=version+1,updated=? WHERE id=?",(a['id'],now+ttl,now,oid))
    else:
     if t['owner']!=a['id'] or t['lease_until']<=now:raise Fault('valid_lease_required',409)
     status=b.get('status')
     if status not in ('doing','blocked','delivered'):raise Fault('invalid_task_status')
     evidence=text(b.get('evidence',''),1500,empty=status!='delivered')
     lease=0 if status=='delivered' else t['lease_until']
     d.execute('UPDATE tasks SET status=?,evidence=?,lease_until=?,version=version+1,updated=? WHERE id=?',(status,evidence,lease,now,oid))
    out=dict(d.execute('SELECT * FROM tasks WHERE id=?',(oid,)).fetchone())
   elif action=='memory':
    p=b.get('project');self.can_project(a,p,d);kind=b.get('kind','observation')
    if kind not in ('observation','contract','decision','problem'):raise Fault('invalid_memory_kind')
    if kind=='decision' and a['role']!='owner':raise Fault('owner_required_for_decision',403)
    body=text(b.get('body'));source=text(b.get('source'),600);prev=b.get('supersedes')
    if prev:
     prior=d.execute('SELECT project,kind FROM memories WHERE id=?',(ident(prev),)).fetchone()
     if not prior or prior['project']!=p:raise Fault('memory_reference_mismatch')
     if prior['kind']=='decision' and a['role']!='owner':raise Fault('owner_required_for_decision',403)
     if d.execute('SELECT 1 FROM memories WHERE supersedes=?',(prev,)).fetchone():raise Fault('memory_already_superseded',409)
    oid=uuid.uuid4().hex;d.execute('INSERT INTO memories(id,project,kind,body,source,supersedes,actor,created) VALUES(?,?,?,?,?,?,?,?)',(oid,p,kind,body,source,prev,a['id'],now));out={'id':oid}
   else:raise Fault('not_found',404)
   d.execute('UPDATE actors SET last_seen=? WHERE id=?',(now,a['id']))
   collaboration.record_event(d,a,action,b,out,now)
   persisted={k:v for k,v in out.items() if k not in ('token','code')}
   if action=='register':persisted['token_replay_unavailable']=True
   if action=='device-invite':persisted['code_replay_unavailable']=True
   d.execute('INSERT INTO requests VALUES(?,?,?,?)',(a['id'],rid,digest,json.dumps(persisted,ensure_ascii=False)))
   d.commit();return out
  except sqlite3.IntegrityError:
   d.rollback();raise Fault('duplicate_or_invalid_reference',409)
  except BaseException:d.rollback();raise
  finally:d.close()

class Server(ThreadingHTTPServer):
 daemon_threads=True
 def __init__(self,address,store):super().__init__(address,Handler);self.store=store;self.slots=threading.BoundedSemaphore(24)
 def process_request(self,request,address):
  if not self.slots.acquire(False):self.shutdown_request(request);return
  try:super().process_request(request,address)
  except BaseException:self.slots.release();raise
 def process_request_thread(self,request,address):
  try:super().process_request_thread(request,address)
  finally:self.slots.release()

class Handler(BaseHTTPRequestHandler):
 server_version='AieyraCoordination'
 protocol_version='HTTP/1.0'
 def setup(self):super().setup();self.connection.settimeout(10)
 def log_message(self,*args):pass
 def send(self,status,data):
  raw=json.dumps(data,ensure_ascii=False).encode();self.send_response(status)
  self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Content-Length',str(len(raw)))
  self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(raw)
 def handle_api(self,method):
  try:
   parsed=urlparse(self.path)
   if method=='GET' and parsed.path=='/health':return self.send(200,{'ok':True,'service':'aieyra-coordination','version':VERSION})
   if self.headers.get('Origin'):raise Fault('browser_access_via_local_console_only',403)
   if parsed.path.startswith(('/v1/device/','/v1/mobile/')):
    self.server.store.mobile.enabled()
    sensitive=('Authorization','Content-Length','Content-Type','X-Device-Id','X-Device-Timestamp','X-Device-Nonce','X-Device-Signature')
    if any(len(self.headers.get_all(k,[]))>1 for k in sensitive):raise Fault('duplicate_header')
    if self.headers.get('Transfer-Encoding'):raise Fault('unsupported_transfer_encoding')
    raw=b''
    if method=='POST':
     try:n=int(self.headers.get('Content-Length','0'))
     except ValueError:raise Fault('invalid_length')
     if not 0<n<=32768:raise Fault('body_limit',413)
     if self.headers.get_content_type()!='application/json':raise Fault('json_required',415)
     raw=self.rfile.read(n)
     if len(raw)!=n:raise Fault('incomplete_body')
    elif self.headers.get('Content-Length','0')!='0':raise Fault('unexpected_body')
    data=self.server.store.mobile.handle(method,self.path,self.headers,raw,self.client_address[0])
    return self.send(200,{'ok':True,'data':data})
   token=self.headers.get('Authorization','')
   if not token.startswith('Bearer '):raise Fault('unauthorized',401)
   a=self.server.store.authenticate(token[7:])
   if method=='GET':data=self.server.store.read(a,parsed.path,parse_qs(parsed.query))
   else:
    if self.headers.get('Transfer-Encoding'):raise Fault('unsupported_transfer_encoding')
    try:n=int(self.headers.get('Content-Length','0'))
    except ValueError:raise Fault('invalid_length')
    if not 0<n<=32768:raise Fault('body_limit',413)
    if self.headers.get_content_type()!='application/json':raise Fault('json_required',415)
    b=json.loads(self.rfile.read(n))
    if not isinstance(b,dict):raise Fault('object_required')
    if not parsed.path.startswith('/v1/'):raise Fault('not_found',404)
    data=self.server.store.write(a,parsed.path[4:],b)
   self.send(200,{'ok':True,'data':data})
  except Fault as e:self.send(e.status,{'ok':False,'error':e.code})
  except (ValueError,UnicodeError):self.send(400,{'ok':False,'error':'invalid_json'})
  except (TimeoutError,ConnectionError):pass
  except Exception:self.send(500,{'ok':False,'error':'internal_error'})
 def do_GET(self):self.handle_api('GET')
 def do_POST(self):self.handle_api('POST')

def main():
 p=argparse.ArgumentParser();p.add_argument('action',choices=['init','serve']);p.add_argument('--db',required=True);p.add_argument('--port',type=int,default=18791);a=p.parse_args()
 store=Store(a.db,os.environ.get('AIEYRA_MOBILE_ORIGIN'))
 if a.action=='init':
  import sys
  token=sys.stdin.read().strip()
  if len(token)<32:raise SystemExit('owner_token_required_on_stdin')
  store.init(token);print('initialized')
 else:Server(('127.0.0.1',a.port),store).serve_forever()

if __name__=='__main__':main()
