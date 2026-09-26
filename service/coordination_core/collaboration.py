"""Shared projections and human decisions; native execution stays on its host.

Uses the existing registry, product jobs, transactions, request deduplication and
audit event sequence. A human answer never grants arbitrary execution authority.
"""
from __future__ import annotations

import json
import time
import uuid

from .core import Fault, ident, text
from . import registry
from . import jobs
from .mobile import canonical, digest, generation, runtime_ready

ACTIONS = {'human-request-create', 'human-request-decide', 'human-request-begin',
           'human-request-receipt', 'human-request-close'}
READS = {'/v1/collaboration/snapshot', '/v1/collaboration/events',
         '/v1/human-request', '/v1/human-request-outbox', '/v1/human-decision'}
FINAL = {'resolved', 'failed', 'cancelled', 'expired'}
FACTS = {'delivered_to_agent': 1, 'resuming': 2, 'resolved': 3}
ORIGIN = {'seat_id', 'seat_epoch', 'runtime_generation', 'runtime_ref', 'agent_id',
          'native_task_id', 'task_generation', 'native_request_id', 'native_request_version', 'request_token_sha256'}


def migrate(d):
    d.executescript('''
    CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,actor TEXT NOT NULL,action TEXT NOT NULL,object_id TEXT,created REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS collaboration_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS human_requests(
      id TEXT PRIMARY KEY,project TEXT NOT NULL REFERENCES registered_projects(id),
      host_id TEXT NOT NULL REFERENCES registered_hosts(id),adapter_id TEXT NOT NULL REFERENCES registered_adapters(id),
      seat_id TEXT NOT NULL REFERENCES seats(id),host_actor TEXT NOT NULL,producer_actor TEXT NOT NULL,
      adapter_version TEXT NOT NULL,seat_epoch INTEGER NOT NULL,runtime_generation TEXT NOT NULL,runtime_ref TEXT NOT NULL,
      agent_id TEXT NOT NULL,native_task_id TEXT NOT NULL,task_generation INTEGER NOT NULL,native_request_id TEXT NOT NULL,
      request_token_sha256 TEXT NOT NULL,native_request_version INTEGER NOT NULL,job_id TEXT REFERENCES product_jobs(id),
      category TEXT NOT NULL,question TEXT NOT NULL,options TEXT NOT NULL,allow_text INTEGER NOT NULL,
      presentation TEXT NOT NULL DEFAULT '{}',
      create_digest TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'waiting_user',last_fact TEXT,
      decision TEXT,decision_by TEXT REFERENCES actors(id),decision_request_id TEXT,begun_at REAL,observed_at REAL,observed_ns TEXT,
      error TEXT,recovery_hold INTEGER NOT NULL DEFAULT 0,version INTEGER NOT NULL DEFAULT 1,
      created REAL NOT NULL,updated REAL NOT NULL,expires REAL NOT NULL,
      UNIQUE(seat_id,seat_epoch,runtime_generation,agent_id,native_task_id,task_generation,native_request_id,native_request_version));
    CREATE INDEX IF NOT EXISTS human_outbox ON human_requests(host_id,id);
    CREATE INDEX IF NOT EXISTS human_project ON human_requests(project,id);
    CREATE TABLE IF NOT EXISTS human_receipts(
      request_id TEXT NOT NULL REFERENCES human_requests(id),receipt_id TEXT NOT NULL,digest TEXT NOT NULL,
      projection TEXT NOT NULL,ack TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(request_id,receipt_id));
    ''')
    columns = {r[1] for r in d.execute('PRAGMA table_info(events)')}
    for name, declaration in (('project', "TEXT NOT NULL DEFAULT ''"), ('topic', "TEXT NOT NULL DEFAULT 'legacy'")):
        if name not in columns:
            d.execute('ALTER TABLE events ADD COLUMN ' + name + ' ' + declaration)
    d.execute('CREATE INDEX IF NOT EXISTS events_project_cursor ON events(project,seq)')
    d.execute('INSERT OR IGNORE INTO collaboration_meta VALUES(?,?)', ('stream_epoch', uuid.uuid4().hex))


def event(d, actor_id, action, object_id, project, topic, now):
    d.execute('INSERT INTO events(actor,action,object_id,created,project,topic) VALUES(?,?,?,?,?,?)',
              (actor_id, action, object_id, now, project, topic))


def record_event(d, actor, action, body, out, now):
    oid = out.get('id', out.get('job_id'))
    if action in jobs.ACTIONS:
        item = jobs.get(d, oid)
        event(d, actor['id'], action, oid, item['project'], 'product_job', now)
    elif action in ACTIONS:
        item = get(d, oid)
        event(d, actor['id'], action, oid, item['project'], 'human_request', now)
    elif action == 'library-put':
        event(d, actor['id'], action, out['id'], out['project'], 'library', now)
    elif action == 'requirement-put':
        event(d, actor['id'], action, out['id'], out['project'], 'requirement', now)
    elif action in ('requirement-receipt', 'requirement-receipt-retract'):
        project = d.execute('SELECT project FROM requirements WHERE id=?', (out['requirement_id'],)).fetchone()[0]
        event(d, actor['id'], action, out['requirement_id'], project, 'requirement_receipt', now)
    elif action in ('task-create', 'task-claim', 'task-update', 'task-accept', 'task-retire'):
        project = d.execute('SELECT project FROM tasks WHERE id=?', (out['id'],)).fetchone()[0]
        event(d, actor['id'], action, out['id'], project, 'task', now)
    elif action in registry.ACTIONS or action == 'revoke':
        # Global invalidation carries no host/actor/private object detail.
        event(d, actor['id'], action, None, '*', 'registry', now)
    else:
        event(d, actor['id'], action, oid, '', 'legacy', now)


def get(d, value):
    r = d.execute('SELECT * FROM human_requests WHERE id=?', (ident(value),)).fetchone()
    if not r:
        raise Fault('human_request_missing', 404)
    return dict(r)


def visible(d, actor, item):
    return actor['id'] in (item['host_actor'], item['producer_actor']) or registry.manager(d, actor, item['project'])


def access(d, actor, item, host_only=False):
    if host_only:
        jobs.host(d, actor, item)
    elif not visible(d, actor, item):
        raise Fault('human_request_access_denied', 403)


def live_binding(d, item, now):
    if not jobs.binding(d, item):
        return False
    seat = registry.row(d, 'seats', item['seat_id'])
    return runtime_ready(d, seat, now, required_action='human.reply')


def refresh(d, item, now):
    if item['state'] in FINAL or item['recovery_hold']:
        return item
    reason = ('human_request_expired' if item['expires'] <= now else
              'native_binding_changed' if not jobs.binding(d, item) else None)
    if reason:
        state = ('expired' if reason == 'human_request_expired' else 'cancelled') if item['begun_at'] is None else 'unknown'
        if item['state'] != state or item['error'] != reason:
            d.execute('UPDATE human_requests SET state=?,error=?,version=version+1,updated=? WHERE id=?', (state,reason,now,item['id']))
            event(d, item['host_actor'], 'human-request-invalidated', item['id'], item['project'], 'human_request', now)
            item = get(d, item['id'])
    return item


def public(d, item, now, actor):
    out = {k:v for k,v in item.items() if k != 'create_digest'}
    out['native_request_id'] = json.loads(item['native_request_id'])
    out.update(json.loads(out.pop('presentation')))
    out['options'] = json.loads(item['options'])
    out['decision'] = json.loads(item['decision']) if item['decision'] else None
    out['origin_sha256'] = digest(canonical({k:out[k] for k in sorted(ORIGIN)}))
    out['fresh'] = bool(item['observed_at'] and 0 <= now-item['observed_at'] < 180)
    current = live_binding(d, item, now) and item['expires'] > now and not item['recovery_hold']
    out['binding_current'] = bool(current)
    out['can_decide'] = bool(actor['role'] == 'owner' and current and out['fresh'] and item['state'] == 'waiting_user')
    out['can_begin'] = bool(actor['id'] == item['host_actor'] and current and item['state'] == 'decision_recorded' and item['begun_at'] is None)
    out['callback_allowed'] = False
    out['replay_allowed'] = False
    out['next_action'] = 'begin' if out['can_begin'] else 'query' if item['begun_at'] and item['state'] not in FINAL else 'none'
    return out


def question_schema(value):
    jobs.fields(value, {'questions'})
    if not isinstance(value['questions'],list) or not 1<=len(value['questions'])<=12:
        raise Fault('invalid_human_input_schema')
    ids=set()
    for q in value['questions']:
        jobs.fields(q, {'id','label','options','allow_text','multiple','required'})
        ident(q['id']);text(q['label'],1000)
        if q['id'] in ids or any(type(q[k]) is not bool for k in ('allow_text','multiple','required')):
            raise Fault('invalid_human_input_schema')
        ids.add(q['id']);validate_options(q['options'],q['allow_text'])
    return value


def validate_options(options, allow_text):
    if not isinstance(options,list) or len(options)>12 or not options and not allow_text:
        raise Fault('invalid_human_options')
    seen=set()
    for o in options:
        jobs.fields(o,{'id','label'});ident(o['id']);text(o['label'],240)
        if o['id'] in seen:raise Fault('invalid_human_options')
        seen.add(o['id'])


def validate_answer(answer, view):
    schema=view.get('input_schema')
    if schema:
        jobs.fields(answer,{'answers'})
        if not isinstance(answer['answers'],list) or len(answer['answers'])>12:raise Fault('invalid_human_answer')
        questions={q['id']:q for q in schema['questions']};seen=set()
        for value in answer['answers']:
            jobs.fields(value,{'question_id'},{'selected_ids','text'})
            ident(value['question_id'])
            q=questions.get(value['question_id'])
            if not q or value['question_id'] in seen:raise Fault('invalid_human_answer')
            seen.add(value['question_id']);chosen=value.get('selected_ids',[])
            if not isinstance(chosen,list) or any(not isinstance(x,str) for x in chosen) or len(set(chosen))!=len(chosen) or len(chosen)>(12 if q['multiple'] else 1) or not set(chosen)<={o['id'] for o in q['options']}:
                raise Fault('invalid_human_choice')
            if 'text' in value:
                if not q['allow_text']:raise Fault('human_free_text_not_allowed')
                text(value['text'],2000)
            if not chosen and not value.get('text'):raise Fault('human_answer_required')
        if any(q['required'] and q['id'] not in seen for q in questions.values()):raise Fault('human_answers_incomplete')
        return
    jobs.fields(answer,set(),{'option_id','text'})
    if not answer:raise Fault('human_answer_required')
    if 'option_id' in answer and (not isinstance(answer['option_id'],str) or answer['option_id'] not in {o['id'] for o in view['options']}):raise Fault('invalid_human_choice')
    if 'text' in answer:
        if not view['allow_text']:raise Fault('human_free_text_not_allowed')
        text(answer['text'],2000)


def create(d, actor, body, now):
    jobs.fields(body, {'request_id', 'id', 'origin', 'category', 'question', 'options', 'allow_text'}, {'job_id','expires_in','observed_ns','input_schema','recommendation','impact','resume_summary'})
    origin = body['origin']; jobs.fields(origin, ORIGIN)
    seat = registry.row(d, 'seats', origin['seat_id'])
    h = registry.row(d, 'registered_hosts', seat['host_id'])
    adapter = registry.row(d, 'registered_adapters', seat['adapter_id'])
    if actor['id'] != h['actor_id']:
        raise Fault('host_identity_required', 403)
    if not seat['actor_id']:
        raise Fault('human_runtime_unbound', 409)
    if (type(origin['seat_epoch']) is not int or origin['seat_epoch'] != seat['epoch'] or
            origin['runtime_generation'] != generation(seat) or origin['runtime_ref'] != seat['runtime_ref']):
        raise Fault('human_origin_mismatch', 409)
    for key in ('agent_id','native_task_id'):
        ident(origin[key])
    native_id=origin['native_request_id']
    if type(native_id) is int:
        registry.integer(native_id,0,2**53-1)
    elif isinstance(native_id,str):
        text(native_id,240)
    else:raise Fault('invalid_native_request_id')
    registry.integer(origin['native_request_version'],1,2**31-1)
    jobs.sha(origin['request_token_sha256'])
    registry.integer(origin['task_generation'], 1, 2**31-1)
    if body['category'] not in ('question','confirmation','selection','information','direction','authorization','environment') or type(body['allow_text']) is not bool:
        raise Fault('invalid_human_question')
    question = text(body['question'], 2000)
    options = body['options']
    presentation={k:text(body[k],1000,empty=True) for k in ('recommendation','impact','resume_summary') if k in body}
    if 'input_schema' in body:
        presentation['input_schema']=question_schema(body['input_schema'])
        if options or body['allow_text']:raise Fault('human_schema_modes_conflict')
    else:validate_options(options,body['allow_text'])
    oid = ident(body['id']); ttl = registry.integer(body.get('expires_in', 3600), 30, 86400)
    observed_ns = body.get('observed_ns', str(int(now*1_000_000_000)))
    ns = jobs.stamp(observed_ns)
    if not now-180 <= ns/1_000_000_000 <= now+120:
        raise Fault('stale_human_observation', 409)
    values = dict(id=oid, project=seat['project'], host_id=h['id'], adapter_id=adapter['id'],
                  host_actor=h['actor_id'],producer_actor=seat['actor_id'],adapter_version=adapter['version_label'],
                  **origin, job_id=body.get('job_id'),category=body['category'],question=question,
                  options=canonical(options),allow_text=int(body['allow_text']),
                  presentation=canonical(presentation),
                  create_digest=digest(canonical({k:v for k,v in body.items() if k != 'request_id'})),
                  state='waiting_user',observed_at=ns/1_000_000_000,observed_ns=observed_ns,
                  created=now,updated=now,expires=now+ttl)
    values['native_request_id']=canonical(native_id)
    existing = d.execute('SELECT * FROM human_requests WHERE id=?', (oid,)).fetchone()
    if existing:
        if existing['create_digest'] != values['create_digest'] or existing['host_actor'] != actor['id']:
            raise Fault('human_request_id_conflict', 409)
        return public(d, refresh(d, dict(existing), now), now, actor)
    if not live_binding(d, values, now):
        raise Fault('human_runtime_unavailable', 409)
    if values['job_id']:
        job = jobs.get(d, values['job_id'])
        if (job['seat_id'] != seat['id'] or job['runtime_generation'] != origin['runtime_generation'] or
            job['agent_id'] != origin['agent_id'] or job['product_generation'] != origin['task_generation'] or
            job['project'] != seat['project'] or job['last_fact'] in jobs.TERMINAL):
            raise Fault('human_job_mismatch', 409)
    prior = d.execute('SELECT * FROM human_requests WHERE seat_id=? AND seat_epoch=? AND runtime_generation=? AND agent_id=? AND native_task_id=?',
                      (seat['id'],seat['epoch'],generation(seat),origin['agent_id'],origin['native_task_id'])).fetchall()
    if any(r['task_generation'] > origin['task_generation'] for r in prior):
        raise Fault('stale_human_generation', 409)
    same_requests = [r for r in prior if r['task_generation']==origin['task_generation'] and r['native_request_id']==canonical(native_id)]
    if any(r['native_request_version']>origin['native_request_version'] for r in same_requests):
        raise Fault('stale_native_request_version',409)
    for r in prior:
        same_request = r['task_generation']==origin['task_generation'] and r['native_request_id']==canonical(native_id)
        if same_request and r['native_request_version']==origin['native_request_version'] and r['request_token_sha256']!=origin['request_token_sha256']:
            raise Fault('native_request_token_changed',409)
        if (r['task_generation'] < origin['task_generation'] or same_request and r['native_request_version']<origin['native_request_version']) and r['state'] not in FINAL:
            d.execute("UPDATE human_requests SET state=?,recovery_hold=1,error='native_generation_superseded',version=version+1,updated=? WHERE id=?", ('unknown' if r['begun_at'] else 'cancelled',now,r['id']))
            event(d, actor['id'], 'human-request-superseded', r['id'], r['project'], 'human_request', now)
    if d.execute("SELECT COUNT(*) FROM human_requests WHERE host_id=? AND state NOT IN ('resolved','failed','cancelled','expired')", (h['id'],)).fetchone()[0] >= 256:
        raise Fault('human_host_backlog_limit', 429)
    d.execute('INSERT INTO human_requests ('+','.join(values)+') VALUES ('+','.join('?' for _ in values)+')', tuple(values.values()))
    return public(d, get(d, oid), now, actor)


def write(d, actor, action, body, now):
    if action == 'human-request-create':
        return create(d, actor, body, now)
    required = {'request_id','id','version'}
    if action == 'human-request-decide':
        jobs.fields(body, required | {'decision'})
    elif action == 'human-request-begin':
        jobs.fields(body, required | {'origin_sha256'})
    elif action == 'human-request-close':
        jobs.fields(body, required | {'reason'})
    elif action == 'human-request-receipt':
        jobs.fields(body, required | {'receipt_id','origin_sha256','state','observed_ns','evidence_ref','evidence_sha256'})
    else:
        raise Fault('not_found', 404)
    item = get(d, body['id'])
    access(d, actor, item, host_only=action in ('human-request-begin','human-request-receipt'))
    if action == 'human-request-receipt':
        receipt_id = ident(body['receipt_id'])
        projection = {k:v for k,v in body.items() if k not in ('request_id','version')}
        fingerprint = digest(canonical(projection))
        old = d.execute('SELECT digest,ack FROM human_receipts WHERE request_id=? AND receipt_id=?', (item['id'],receipt_id)).fetchone()
        if old:
            if old['digest'] != fingerprint:
                raise Fault('human_receipt_id_conflict', 409)
            return json.loads(old['ack'])
    item = refresh(d, item, now)
    registry.version(body, item)
    view = public(d, item, now, actor)
    if action == 'human-request-decide':
        if actor['role'] != 'owner':
            raise Fault('human_owner_decision_required', 403)
        if not view['can_decide']:
            raise Fault('human_request_not_decidable', 409)
        answer = body['decision'];validate_answer(answer,view)
        d.execute("UPDATE human_requests SET state='decision_recorded',decision=?,decision_by=?,decision_request_id=? WHERE id=?", (canonical(answer),actor['id'],body['request_id'],item['id']))
    elif action == 'human-request-begin':
        if body['origin_sha256'] != view['origin_sha256']:
            raise Fault('human_origin_mismatch', 409)
        if not view['can_begin']:
            raise Fault('human_callback_not_allowed', 409)
        d.execute("UPDATE human_requests SET state='submitting',begun_at=? WHERE id=?", (now,item['id']))
    elif action == 'human-request-close':
        if actor['id'] != item['host_actor'] and actor['role'] != 'owner':
            raise Fault('human_close_denied', 403)
        reason = text(body['reason'], 600)
        if item['state'] in FINAL:
            raise Fault('human_request_terminal', 409)
        # Closure prevents new callbacks; a past RPC may still finish.
        state = 'cancelled' if item['begun_at'] is None else 'unknown'
        d.execute('UPDATE human_requests SET state=?,error=?,recovery_hold=1 WHERE id=?', (state,reason,item['id']))
    else:
        if body['origin_sha256'] != view['origin_sha256']:
            raise Fault('human_receipt_binding_mismatch', 409)
        state = body['state']
        if not isinstance(state,str) or state not in {*FACTS, 'unknown','failed','waiting_user'}:
            raise Fault('invalid_human_receipt_state')
        waiting = state == 'waiting_user'
        if waiting:
            if item['state']!='waiting_user' or item['decision'] is not None or item['begun_at'] is not None or not view['binding_current']:
                raise Fault('human_wait_observation_not_allowed',409)
        elif item['begun_at'] is None:
            raise Fault('human_receipt_binding_mismatch',409)
        ns = jobs.stamp(body['observed_ns'])
        if ns > int((now+120)*1_000_000_000) or (state != 'unknown' and item['observed_ns'] and ns < int(item['observed_ns'])):
            raise Fault('stale_human_observation', 409)
        if waiting and ns<int((now-180)*1_000_000_000):
            raise Fault('stale_human_observation',409)
        ident(body['evidence_ref']); jobs.sha(body['evidence_sha256'])
        if item['state'] in FINAL and state not in (item['state'], 'unknown'):
            raise Fault('human_terminal_fact_conflict', 409)
        if state in FACTS and FACTS[state] < FACTS.get(item['last_fact'],0):
            raise Fault('human_fact_regression', 409)
        if waiting:
            d.execute('UPDATE human_requests SET observed_ns=?,observed_at=?,error=NULL WHERE id=?',
                      (body['observed_ns'],ns/1_000_000_000,item['id']))
        elif state == 'unknown':
            d.execute('UPDATE human_requests SET state=?,observed_at=NULL,error=? WHERE id=?',
                      (item['state'] if item['state'] in FINAL else 'unknown','native_observation_unknown',item['id']))
        else:
            d.execute('UPDATE human_requests SET state=?,last_fact=?,observed_ns=?,observed_at=?,error=NULL WHERE id=?',
                      (state,state,body['observed_ns'],ns/1_000_000_000,item['id']))
    d.execute('UPDATE human_requests SET version=version+1,updated=? WHERE id=?', (now,item['id']))
    result = public(d, get(d, item['id']), now, actor)
    if action == 'human-request-begin':
        result['callback_allowed'] = True
        result['callback_permit_until'] = min(now+30,item['expires'])
    if action == 'human-request-receipt':
        result.update(committed=True, receipt_id=receipt_id)
        d.execute('INSERT INTO human_receipts VALUES(?,?,?,?,?,?)', (item['id'],receipt_id,fingerprint,canonical(projection),canonical(result),now))
    return result


def project_access(d, actor, project):
    registry.row(d, 'registered_projects', project)
    if actor['project'] != project and not registry.manager(d, actor, project):
        raise Fault('collaboration_project_denied', 403)


def page(d, actor, table, project, after, now):
    if after:
        ident(after)
    managed = registry.manager(d, actor, project)
    rows = d.execute('SELECT * FROM '+table+' WHERE project=? AND id>? AND (? OR host_actor=? OR producer_actor=?) ORDER BY id LIMIT 101',
                     (project,after,int(managed),actor['id'],actor['id'])).fetchall()
    selected = rows[:100]
    values = [(jobs.public(d,jobs.refresh(d,dict(r),now),now) if table=='product_jobs' else public(d,refresh(d,dict(r),now),now,actor)) for r in selected]
    return values, selected[-1]['id'] if selected else after, len(rows)>100


def read(d, actor, path, query, now):
    if path == '/v1/human-decision':
        if set(query)!={'request_id'}:raise Fault('invalid_human_query')
        if actor['role']!='owner':raise Fault('human_owner_decision_required',403)
        rid=ident(query['request_id'][0])
        found=d.execute('SELECT * FROM human_requests WHERE decision_by=? AND decision_request_id=?',(actor['id'],rid)).fetchone()
        if not found:raise Fault('human_decision_missing',404)
        return {'request_id':rid,'recorded':True,'item':public(d,refresh(d,dict(found),now),now,actor)}
    if path == '/v1/human-request':
        if set(query) != {'id'}:
            raise Fault('invalid_human_query')
        item = get(d, query['id'][0]); access(d,actor,item)
        return public(d,refresh(d,item,now),now,actor)
    if path == '/v1/human-request-outbox':
        if set(query)-{'host_id','after'} or 'host_id' not in query:
            raise Fault('invalid_human_query')
        h = registry.row(d,'registered_hosts',query['host_id'][0])
        if h['actor_id'] != actor['id']:
            raise Fault('host_identity_required',403)
        after=query.get('after',[''])[0]
        if after:ident(after)
        rows=d.execute('SELECT * FROM human_requests WHERE host_id=? AND id>? ORDER BY id LIMIT 101',(h['id'],after)).fetchall()
        return {'host_id':h['id'],'items':[public(d,refresh(d,dict(r),now),now,actor) for r in rows[:100]],
                'next_cursor':rows[min(99,len(rows)-1)]['id'] if rows else after,'has_more':len(rows)>100}
    project=ident(query.get('project',[''])[0]); project_access(d,actor,project)
    epoch=d.execute("SELECT value FROM collaboration_meta WHERE key='stream_epoch'").fetchone()[0]
    if path == '/v1/collaboration/events':
        if set(query)-{'project','after','stream_epoch'}:
            raise Fault('invalid_collaboration_query')
        if query.get('stream_epoch',[''])[0] != epoch:
            raise Fault('collaboration_snapshot_required',409)
        try:after=int(query.get('after',['0'])[0])
        except ValueError:raise Fault('invalid_cursor')
        head=d.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]
        if not 0<=after<=head:raise Fault('collaboration_snapshot_required',409)
        rows=d.execute("SELECT seq,action,object_id,created,topic FROM events WHERE seq>? AND project IN (?, '*') ORDER BY seq LIMIT 101",(after,project)).fetchall()
        out=[]
        for row in rows[:100]:
            item=dict(row)
            if item['topic'] in ('product_job','human_request'):
                obj=(jobs.get(d,item['object_id']) if item['topic']=='product_job' else get(d,item['object_id']))
                if not visible(d,actor,obj):continue
            out.append(item)
        return {'project':project,'stream_epoch':epoch,'events':out,'next_cursor':rows[99]['seq'] if len(rows)>100 else head,
                'has_more':len(rows)>100,'server_time':now,'semantics':'invalidate_then_read'}
    if set(query)-{'project','after_job','after_human'}:
        raise Fault('invalid_collaboration_query')
    snapshot=registry.snapshot(d,actor)
    seats=[s for s in snapshot['seats'] if s['project']==project]
    hosts=[h for h in snapshot['hosts'] if project in h['projects'] or '*' in h['projects']]
    host_ids={h['id'] for h in hosts};seat_ids={s['id'] for s in seats}
    js,jc,jmore=page(d,actor,'product_jobs',project,query.get('after_job',[''])[0],now)
    hs,hc,hmore=page(d,actor,'human_requests',project,query.get('after_human',[''])[0],now)
    head=d.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]
    return {'schema_version':1,'project':project,'stream_epoch':epoch,'cursor':head,'server_time':now,
            'registry':{'hosts':hosts,'adapters':[a for a in snapshot['adapters'] if a['host_id'] in host_ids],
                        'seats':seats,'operations':[o for o in snapshot['operations'] if o['seat_id'] in seat_ids]},
            'jobs':js,'human_requests':hs,'next_job_cursor':jc,'jobs_has_more':jmore,'next_human_cursor':hc,'humans_has_more':hmore,
            'capabilities':{'execution_in_center':False,'native_graph_authority':'product_runtime','human_decide':actor['role']=='owner'}}


def invalidate_restored(d, now):
    epoch=uuid.uuid4().hex
    d.execute("UPDATE collaboration_meta SET value=? WHERE key='stream_epoch'",(epoch,))
    count=d.execute("SELECT COUNT(*) FROM human_requests WHERE state NOT IN ('resolved','failed','cancelled','expired')").fetchone()[0]
    d.execute("UPDATE human_requests SET recovery_hold=1,state=CASE WHEN begun_at IS NULL THEN 'cancelled' ELSE 'unknown' END,error='restored_database_query_only',version=version+1,updated=? WHERE state NOT IN ('resolved','failed','cancelled','expired')",(now,))
    return {'held_human_requests':count,'collaboration_stream_epoch':epoch,'callback_replay_allowed':False}
