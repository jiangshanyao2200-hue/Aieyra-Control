"""Local runtime bridge. Center identities, seats, tasks and messages stay authoritative."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import threading
import time
from urllib.parse import urlencode

ID = re.compile(r'^[A-Za-z0-9_.:-]{1,100}$')
SLOT = re.compile(r'^[A-Za-z0-9_-]{1,70}$')
READS = frozenset(('status', 'registry', 'inbox', 'history', 'host-operations',
    'requirements', 'requirement', 'tasks', 'task', 'requirement-receipts', 'intake-receipt', 'intake-events',
    'library', 'library-item', 'collaboration/snapshot', 'collaboration/events', 'human-request',
    'human-request-outbox', 'human-decision', 'product-job', 'product-job-outbox', 'product-job-receipts'))
WRITES = frozenset(('heartbeat', 'message', 'ack', 'task-create', 'task-claim', 'task-update', 'task-accept',
    'memory', 'project-register', 'governance-grant', 'host-register', 'adapter-register', 'seat-create',
    'seat-update', 'seat-control', 'seat-observe', 'seat-attach', 'operation-receipt', 'library-put',
    'requirement-put', 'requirement-receipt', 'requirement-receipt-retract', 'task-retire',
    'human-request-create', 'human-request-decide', 'human-request-begin', 'human-request-receipt',
    'human-request-close', 'product-job-create', 'product-job-claim', 'product-job-begin',
    'product-job-receipt', 'product-job-cancel', 'product-job-review'))
TERMINAL = {'completed', 'failed', 'interrupted'}
LEASE_SECONDS = 90


def stamp(value=None):
    return dt.datetime.fromtimestamp(time.time() if value is None else value, dt.timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class AgentError(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status


def ident(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise AgentError('invalid_agent_identifier')
    return value


def text(value, limit=100):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise AgentError('invalid_agent_text')
    return value.strip()


class AgentAccess:
    def __init__(self, app):
        self.app, self.store = app, app.store
        self.lock = threading.RLock()
        with self.store.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS agent_enrollments(
                    id TEXT PRIMARY KEY,digest TEXT NOT NULL,document TEXT NOT NULL,state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS agent_credentials(
                    id TEXT PRIMARY KEY,token_hash TEXT NOT NULL UNIQUE,identity TEXT NOT NULL,
                    actor_id TEXT NOT NULL,name TEXT NOT NULL,project TEXT NOT NULL,revoked INTEGER NOT NULL DEFAULT 0,
                    created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS agent_sessions(
                    id TEXT PRIMARY KEY,credential_id TEXT NOT NULL,actor_id TEXT NOT NULL,seat_id TEXT NOT NULL,
                    seat_epoch INTEGER NOT NULL,lease_until REAL NOT NULL,state TEXT NOT NULL,
                    observed REAL NOT NULL,runtime_state TEXT NOT NULL,center_checked REAL NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS agent_seat_live ON agent_sessions(seat_id) WHERE state='connected';
                CREATE UNIQUE INDEX IF NOT EXISTS agent_actor_live ON agent_sessions(actor_id) WHERE state='connected';
                CREATE TABLE IF NOT EXISTS agent_requests(
                    credential_id TEXT NOT NULL,id TEXT NOT NULL,digest TEXT NOT NULL,response TEXT NOT NULL,
                    PRIMARY KEY(credential_id,id));
                CREATE TABLE IF NOT EXISTS agent_station_bindings(
                    actor_id TEXT PRIMARY KEY,seat_id TEXT NOT NULL UNIQUE,
                    native_session_id TEXT NOT NULL,version INTEGER NOT NULL,updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS agent_session_native(
                    session_id TEXT PRIMARY KEY,native_session_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS agent_station_audit(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,actor_id TEXT NOT NULL,
                    action TEXT NOT NULL,document TEXT NOT NULL,created REAL NOT NULL);
            ''')
            # Migrate only the selected seat, never infer a native thread from a lease ID.
            for row in db.execute('SELECT actor_id,seat_id,observed FROM agent_sessions ORDER BY observed DESC').fetchall():
                db.execute('INSERT OR IGNORE INTO agent_station_bindings VALUES(?,?,?,1,?)',
                           (row['actor_id'], row['seat_id'], '', row['observed']))

    def remote(self, identity, route, body=None, query=None):
        if (body is None and route not in READS) or (body is not None and route not in WRITES):
            raise AgentError('agent_route_not_supported', 404)
        try:
            value = self.app.hub.agent_call(identity, '/v1/' + route +
                ('?' + urlencode(query, doseq=True) if query else ''), body)
            if not isinstance(value, dict):
                raise ValueError('invalid_center_response')
            return value
        except AgentError:
            raise
        except Exception as error:
            match = re.fullmatch(r'(400|401|403|404|409|413|415|429):([A-Za-z0-9_.-]{1,80})', str(error))
            if match:
                raise AgentError(match[2], int(match[1])) from None
            raise AgentError('agent_center_unavailable', 503) from None

    def enroll(self, value):
        """Owner-only endpoint. Caller keeps its random token; only its hash is stored."""
        allowed = {'request_id', 'name', 'project', 'token', 'local_identity'}
        if set(value) - allowed:
            raise AgentError('invalid_enrollment_fields')
        rid, name, project = ident(value.get('request_id')), text(value.get('name'), 80), ident(value.get('project'))
        token = value.get('token')
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43,128}', token):
            raise AgentError('strong_agent_token_required')
        identity = value.get('local_identity', '')
        if identity and (not isinstance(identity, str) or not SLOT.fullmatch(identity)):
            raise AgentError('invalid_local_identity')
        document = {'request_id': rid, 'name': name, 'project': project,
                    'token_hash': digest(token), 'local_identity': identity}
        hashed = digest(canonical(document))
        with self.lock:
            with self.store.db() as db:
                used = db.execute('SELECT id FROM agent_credentials WHERE token_hash=? AND id!=?', (digest(token), rid)).fetchone()
                if used:
                    raise AgentError('agent_token_already_used', 409)
                old = db.execute('SELECT * FROM agent_enrollments WHERE id=?', (rid,)).fetchone()
                if old and old['digest'] != hashed:
                    raise AgentError('request_id_conflict', 409)
                if not old:
                    db.execute('INSERT INTO agent_enrollments VALUES(?,?,?,?)', (rid, hashed, canonical(document), 'pending'))
                done = db.execute('SELECT id,actor_id,name,project,revoked FROM agent_credentials WHERE id=?', (rid,)).fetchone()
                if done:
                    return {'credential': dict(done), 'state': 'ready', 'replay_allowed': False}
            try:
                result = self.app.hub.agent_provision(document)
                identity = result['identity']
                current = self.remote(identity, 'status')['actor']
                if current['role'] != 'agent' or current['id'] != result['actor_id'] or current['project'] != project:
                    raise AgentError('dedicated_agent_identity_required', 403)
                if current['id'] in self.app.observer.bindings:
                    raise AgentError('native_runtime_already_bound', 409)
            except AgentError:
                raise
            except Exception as error:
                match = re.fullmatch(r'(400|401|403|404|409):([A-Za-z0-9_.-]{1,80})', str(error))
                raise AgentError(match[2] if match else 'agent_provision_unconfirmed', int(match[1]) if match else 503) from None
            with self.store.db() as db:
                db.execute('BEGIN IMMEDIATE')
                duplicate = db.execute('SELECT id FROM agent_credentials WHERE token_hash=?', (digest(token),)).fetchone()
                if duplicate:
                    raise AgentError('agent_token_already_used', 409)
                db.execute('INSERT INTO agent_credentials VALUES(?,?,?,?,?,?,0,?)',
                    (rid, digest(token), identity, current['id'], name, project, time.time()))
                db.execute("UPDATE agent_enrollments SET state='ready' WHERE id=?", (rid,))
                self.store.event(db, 'agent.enrolled', rid)
                if result.get('seat_id'):
                    db.execute('INSERT OR IGNORE INTO agent_station_bindings VALUES(?,?,?,1,?)',
                               (current['id'], result['seat_id'], '', time.time()))
            self.app.project_memory.register({'id': project})
            return {'credential': {'id': rid, 'actor_id': current['id'], 'name': name, 'project': project, 'revoked': 0},
                    'state': 'ready', 'seat_id': result.get('seat_id'), 'replay_allowed': False}

    def authenticate(self, token):
        if not isinstance(token, str) or not 43 <= len(token) <= 128:
            raise AgentError('agent_unauthorized', 401)
        with self.store.db() as db:
            row = db.execute('SELECT * FROM agent_credentials WHERE token_hash=? AND revoked=0', (digest(token),)).fetchone()
        if row is None:
            raise AgentError('agent_unauthorized', 401)
        return {k: row[k] for k in ('id', 'identity', 'actor_id', 'name', 'project')}

    def expire(self, db):
        for row in db.execute("SELECT id FROM agent_sessions WHERE state='connected' AND lease_until<=?", (time.time(),)).fetchall():
            self.close_session(db, row['id'], 'expired')

    def close_session(self, db, session_id, state):
        db.execute('UPDATE agent_sessions SET state=?,lease_until=0 WHERE id=?', (state, session_id))
        rows = db.execute("SELECT id FROM deliveries WHERE target_thread_id=? AND state IN ('pending','sending','queue_submitting','queued','received','running')", ('bridge:' + session_id,)).fetchall()
        for row in rows:
            db.execute("UPDATE deliveries SET state='unknown',error=?,updated_at=?,intake_dirty=CASE WHEN intake_context IS NULL THEN 0 ELSE 1 END WHERE id=?",
                       ('Agent 会话已退出或失效；保留原记录，不向其它会话重放。', stamp(), row['id']))
            self.store.event(db, 'delivery.unknown', row['id'])
        self.store.event(db, 'agent.' + state, session_id)

    def listing(self):
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self.expire(db)
            credentials = [dict(r) for r in db.execute('SELECT id,actor_id,name,project,revoked,created FROM agent_credentials ORDER BY created')]
            sessions = [dict(r) for r in db.execute('SELECT * FROM agent_sessions ORDER BY observed DESC LIMIT 100')]
            pending = [dict(r) for r in db.execute("SELECT id,state FROM agent_enrollments WHERE state!='ready'")]
        return {'schema_version': 1, 'credentials': credentials, 'sessions': sessions, 'pending': pending,
                'lease_seconds': LEASE_SECONDS, 'protocol': 'aieyra-agent/1', 'os_primary': True}

    def revoke(self, value):
        cid = ident(value.get('credential_id'))
        with self.lock, self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM agent_credentials WHERE id=?', (cid,)).fetchone():
                raise AgentError('agent_credential_missing', 404)
            db.execute('UPDATE agent_credentials SET revoked=1 WHERE id=?', (cid,))
            for row in db.execute("SELECT id FROM agent_sessions WHERE credential_id=? AND state='connected'", (cid,)).fetchall():
                self.close_session(db, row['id'], 'revoked')
            self.store.event(db, 'agent.revoked', cid)
        return {'credential_id': cid, 'revoked': True}

    def seats(self, peer):
        registry = self.remote(peer['identity'], 'registry')
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self.expire(db)
            occupied = {r['seat_id']: r['id'] for r in db.execute("SELECT seat_id,id FROM agent_sessions WHERE state='connected'")}
            bindings = {r['seat_id']: dict(r) for r in db.execute('SELECT * FROM agent_station_bindings')}
        rows = []
        for seat in registry.get('seats', []):
            if seat.get('actor_id') != peer['actor_id']:
                continue
            reason = ('external_seat_required' if seat.get('ownership') != 'external' else
                      'seat_has_runtime' if seat.get('runtime_ref') else
                      'seat_has_pending_operation' if seat.get('pending_operation') else
                      'seat_occupied' if seat['id'] in occupied else '')
            rows.append({**{k: seat.get(k) for k in ('id', 'name', 'project', 'scope', 'epoch', 'host_id', 'adapter_id')},
                         'selectable': not reason, 'reason': reason, 'session_id': occupied.get(seat['id']),
                         'station_binding': bindings.get(seat['id'])})
        return {'seats': rows, 'actor_id': peer['actor_id'], 'observed_at': stamp()}

    def session(self, peer, session_id, require_live=True):
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self.expire(db)
            row = db.execute('SELECT * FROM agent_sessions WHERE id=? AND credential_id=?', (ident(session_id), peer['id'])).fetchone()
            native = db.execute('SELECT native_session_id FROM agent_session_native WHERE session_id=?', (session_id,)).fetchone()
        if not row:
            raise AgentError('agent_session_missing', 404)
        if require_live and row['state'] != 'connected':
            raise AgentError('agent_session_expired', 409)
        return {**dict(row), 'native_session_id': native[0] if native else None}

    def validate_center(self, peer, session):
        status = self.remote(peer['identity'], 'status')
        actor = status.get('actor', {})
        if actor.get('id') != peer['actor_id'] or actor.get('role') != 'agent':
            raise AgentError('agent_identity_changed', 403)
        registry = self.remote(peer['identity'], 'registry')
        seat = next((s for s in registry.get('seats', []) if s['id'] == session['seat_id']), None)
        if not seat or seat.get('actor_id') != peer['actor_id'] or seat.get('epoch') != session['seat_epoch'] or \
                seat.get('ownership') != 'external' or seat.get('runtime_ref') or seat.get('pending_operation'):
            with self.lock, self.store.db() as db:
                self.close_session(db, session['id'], 'invalidated')
            raise AgentError('agent_seat_binding_changed', 409)
        return seat

    def mutate(self, peer, action, value):
        required = {'request_id', 'session_id'}
        optional = set()
        if action == 'connect':
            required |= {'seat_id', 'seat_epoch'}
            optional = {'native_session_id'}
        elif action == 'heartbeat':
            optional = {'runtime_state'}
        elif action == 'receipt':
            required |= {'delivery_id', 'event_id', 'body_sha256', 'state'}
            optional = {'reply'}
        elif action != 'disconnect':
            raise AgentError('agent_action_not_supported', 404)
        if not required <= set(value) or set(value) - required - optional:
            raise AgentError('invalid_agent_fields')
        rid = ident(value.get('request_id'))
        hashed = digest(canonical({'action': action, 'body': value}))
        with self.lock:
            with self.store.db() as db:
                if not db.execute('SELECT 1 FROM agent_credentials WHERE id=? AND revoked=0', (peer['id'],)).fetchone():
                    raise AgentError('agent_unauthorized', 401)
                self.expire(db)
                prior = db.execute('SELECT * FROM agent_requests WHERE credential_id=? AND id=?', (peer['id'], rid)).fetchone()
            if prior:
                if prior['digest'] != hashed:
                    raise AgentError('request_id_conflict', 409)
                result = json.loads(prior['response'])
                result['replayed'] = True
                if 'session' in result:
                    result['session'] = self.session(peer, result['session']['id'], False)
                return result
            if action == 'connect':
                sid, seat_id = ident(value.get('session_id')), ident(value.get('seat_id'))
                if len(sid) > 90:
                    raise AgentError('session_id_too_long')
                if type(value.get('seat_epoch')) is not int:
                    raise AgentError('seat_epoch_required')
                native_id = ident(value['native_session_id']) if 'native_session_id' in value else None
                seats = self.seats(peer)['seats']
                seat = next((s for s in seats if s['id'] == seat_id), None)
                if not seat or not seat['selectable'] or seat['epoch'] != value['seat_epoch']:
                    raise AgentError('agent_seat_not_selectable', 409)
                session = {'id': sid, 'credential_id': peer['id'], 'actor_id': peer['actor_id'], 'seat_id': seat_id,
                           'seat_epoch': seat['epoch'], 'lease_until': time.time() + LEASE_SECONDS, 'state': 'connected',
                           'observed': time.time(), 'runtime_state': 'idle', 'center_checked': time.time()}
                self.validate_center(peer, session)
                self.remote(peer['identity'], 'heartbeat', {'request_id': 'bridge-' + digest(peer['id'] + ':' + rid)})
                with self.store.db() as db:
                    db.execute('BEGIN IMMEDIATE')
                    if db.execute("SELECT 1 FROM agent_sessions WHERE id=? OR (actor_id=? AND state='connected')", (sid, peer['actor_id'])).fetchone():
                        raise AgentError('agent_session_conflict', 409)
                    binding = db.execute('SELECT * FROM agent_station_bindings WHERE actor_id=?', (peer['actor_id'],)).fetchone()
                    if binding and binding['native_session_id'] and native_id and binding['native_session_id'] != native_id:
                        raise AgentError('native_session_change_requires_handoff', 409)
                    native_id = native_id or (binding['native_session_id'] if binding else '')
                    if native_id and db.execute('SELECT 1 FROM agent_station_bindings WHERE native_session_id=? AND actor_id!=?', (native_id,peer['actor_id'])).fetchone():
                        raise AgentError('native_session_already_assigned',409)
                    if not binding:
                        db.execute('INSERT INTO agent_station_bindings VALUES(?,?,?,1,?)', (peer['actor_id'],seat_id,native_id,time.time()))
                    elif binding['seat_id'] != seat_id or binding['native_session_id'] != native_id:
                        db.execute('UPDATE agent_station_bindings SET seat_id=?,native_session_id=?,version=version+1,updated=? WHERE actor_id=?',
                                   (seat_id,native_id,time.time(),peer['actor_id']))
                    db.execute('INSERT INTO agent_sessions VALUES(?,?,?,?,?,?,?,?,?,?)', tuple(session.values()))
                    db.execute('INSERT INTO agent_session_native VALUES(?,?)',(sid,native_id))
                    session['native_session_id'] = native_id or None
                    result = {'session': session, 'heartbeat_interval_seconds': 30,
                              'handoff': {'route': 'memory?project=' + peer['project'],
                                          'read_before_work': True, 'save_before_exit': True,
                                          'historical_tasks_are_not_authorization': True}}
                    self.record(db, peer, rid, hashed, result, 'connected', sid)
                    return result
            sid = ident(value.get('session_id'))
            session = self.session(peer, sid, action != 'disconnect')
            if action != 'disconnect':
                self.validate_center(peer, session)
            if action == 'heartbeat':
                if 'runtime_state' in value and value['runtime_state'] not in ('idle', 'running', 'paused', 'waiting_user'):
                    raise AgentError('invalid_agent_runtime_state')
                self.remote(peer['identity'], 'heartbeat', {'request_id': 'bridge-' + digest(peer['id'] + ':' + rid)})
            # Center checks can take time. A lease that elapsed during I/O cannot revive.
            if action != 'disconnect':
                session = self.session(peer, sid)
            with self.store.db() as db:
                db.execute('BEGIN IMMEDIATE')
                # Snapshot readers can expire a lease while center I/O is pending.
                # Recheck under the write transaction before renewing or recording.
                session = dict(db.execute('SELECT * FROM agent_sessions WHERE id=?', (sid,)).fetchone())
                if action != 'disconnect' and (session['state'] != 'connected' or session['lease_until'] <= time.time()):
                    raise AgentError('agent_session_expired', 409)
                if action == 'heartbeat':
                    state = value.get('runtime_state', session['runtime_state'])
                    if state not in ('idle', 'running', 'paused', 'waiting_user'):
                        raise AgentError('invalid_agent_runtime_state')
                    session.update(runtime_state=state, observed=time.time(), center_checked=time.time(), lease_until=time.time() + LEASE_SECONDS)
                    db.execute('UPDATE agent_sessions SET runtime_state=?,observed=?,center_checked=?,lease_until=? WHERE id=?',
                               (state, session['observed'], session['center_checked'], session['lease_until'], sid))
                    result = {'session': session}
                elif action == 'disconnect':
                    self.close_session(db, sid, 'disconnected')
                    result = {'session': {**session, 'state': 'disconnected', 'lease_until': 0}}
                elif action == 'receipt':
                    result = self.receipt(db, peer, session, value)
                else:
                    raise AgentError('agent_action_not_supported', 404)
                self.record(db, peer, rid, hashed, result, action, sid)
                return result

    def record(self, db, peer, rid, hashed, result, action, oid):
        db.execute('INSERT INTO agent_requests VALUES(?,?,?,?)', (peer['id'], rid, hashed, canonical(result)))
        self.store.event(db, 'agent.' + action, oid)

    def receipt(self, db, peer, session, value):
        row = db.execute('SELECT * FROM deliveries WHERE id=? AND target=? AND target_thread_id=?',
                         (ident(value.get('delivery_id')), peer['actor_id'], 'bridge:' + session['id'])).fetchone()
        if not row:
            raise AgentError('agent_delivery_missing', 404)
        state, old = value.get('state'), row['state']
        if value.get('body_sha256') != row['body_sha256']:
            raise AgentError('agent_delivery_hash_mismatch', 409)
        transitions = {'queued': {'received'}, 'received': {'running', 'failed', 'interrupted'},
                       'running': TERMINAL}
        if state not in transitions.get(old, set()):
            raise AgentError('agent_receipt_transition_rejected', 409)
        reply = text(value.get('reply'), 4000) if state in TERMINAL else None
        native_id = ident(value.get('event_id'))
        db.execute('UPDATE deliveries SET state=?,updated_at=?,reply=?,reply_at=?,turn_id=?,error=NULL,'
                   'native_event_id=COALESCE(native_event_id,?),native_received_at=COALESCE(native_received_at,?),'
                   'native_start_event_id=CASE WHEN ?=\'running\' THEN ? ELSE native_start_event_id END,'
                   'native_started_at=CASE WHEN ?=\'running\' THEN ? ELSE native_started_at END WHERE id=?',
                   (state, stamp(), reply, stamp() if reply else None, session['id'], native_id, stamp(), state, native_id,
                    state, stamp(), row['id']))
        self.store.event(db, 'delivery.' + state, row['id'])
        if state in TERMINAL or state == 'running':
            remaining = db.execute("SELECT 1 FROM deliveries WHERE target_thread_id=? AND state='running' LIMIT 1", ('bridge:' + session['id'],)).fetchone()
            runtime_state = 'running' if remaining else 'idle'
            db.execute('UPDATE agent_sessions SET runtime_state=?,observed=? WHERE id=?', (runtime_state, time.time(), session['id']))
        db.execute('UPDATE deliveries SET intake_dirty=CASE WHEN intake_context IS NULL THEN 0 ELSE 1 END WHERE id=?', (row['id'],))
        return {'delivery': dict(db.execute('SELECT * FROM deliveries WHERE id=?', (row['id'],)).fetchone()),
                'evidence_source': 'agent_report', 'accepted': False}

    def inbox(self, peer, session_id):
        session = self.session(peer, session_id)
        self.validate_center(peer, session)
        self.session(peer, session_id)
        with self.store.db() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM deliveries WHERE target=? AND target_thread_id=? AND state IN ('queued','received','running') ORDER BY created_at LIMIT 100",
                                               (peer['actor_id'], 'bridge:' + session_id))]
        notices=[]
        cloud=self.app.cloud.status()
        if cloud['enabled'] and (cloud.get('release') or {}).get('manifest'):
            registry=self.remote(peer['identity'],'registry')
            leader=any(g.get('active') and g.get('role')=='leader' and g.get('actor_id')==peer['actor_id'] for g in registry.get('governance',[]))
            if leader:
                manifest=cloud['release']['manifest']
                notices.append({'kind':'official_update','version':manifest['version'],'sequence':manifest['sequence'],
                    'signature_verified':True,'action':'review_B_L_N','automatic_apply':False})
        return {'deliveries': rows, 'session_id': session_id, 'ack_required': True,'notices':notices}

    def enqueue(self, binding, delivery, message_id):
        """Publish to an active fixed session atomically against disconnect/revoke."""
        with self.lock, self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self.expire(db)
            row = db.execute("SELECT s.id FROM agent_sessions s JOIN agent_credentials c ON c.id=s.credential_id WHERE s.id=? AND s.actor_id=? AND s.state='connected' AND c.revoked=0",
                             (binding['session']['id'], delivery['target'])).fetchone()
            if not row:
                raise AgentError('agent_session_expired', 409)
            current = db.execute('SELECT state FROM deliveries WHERE id=?', (delivery['id'],)).fetchone()
            if not current or current['state'] != 'sending':
                raise AgentError('agent_delivery_state_changed', 409)
            db.execute("UPDATE deliveries SET state='queued',message_id=?,queue_id=?,queued_at=?,updated_at=?,error=NULL,intake_dirty=CASE WHEN intake_context IS NULL THEN 0 ELSE 1 END WHERE id=?",
                       (message_id, 'bridge:' + delivery['id'], stamp(), stamp(), delivery['id']))
            self.store.event(db, 'delivery.queued', delivery['id'])

    def binding(self, actor):
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self.expire(db)
            row = db.execute("SELECT s.* FROM agent_sessions s JOIN agent_credentials c ON c.id=s.credential_id WHERE s.actor_id=? AND s.state='connected' AND c.revoked=0", (actor,)).fetchone()
        return {'thread_id': 'bridge:' + row['id'], 'bridge': True, 'session': dict(row)} if row else None

    def validate_binding(self, binding):
        session = binding['session']
        with self.store.db() as db:
            r = db.execute('SELECT id,identity,actor_id,name,project FROM agent_credentials WHERE id=? AND revoked=0', (session['credential_id'],)).fetchone()
        if not r:
            raise AgentError('agent_unauthorized', 401)
        self.session(dict(r), session['id'])
        self.validate_center(dict(r), session)

    def runtimes(self):
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self.expire(db)
            rows = [dict(r) for r in db.execute("SELECT s.*,c.name,c.project,c.revoked,n.native_session_id FROM agent_sessions s JOIN agent_credentials c ON c.id=s.credential_id LEFT JOIN agent_session_native n ON n.session_id=s.id WHERE c.revoked=0 ORDER BY s.observed")]
        result = {}
        for row in rows:
            active = row['state'] == 'connected' and not row['revoked']
            fresh = active and time.time() - row['center_checked'] <= LEASE_SECONDS
            result[row['actor_id']] = {'id': row['actor_id'], 'name': row['name'], 'project': row['project'],
                'runtime': {'bound': fresh, 'state': row['runtime_state'] if fresh else 'unknown', 'stale': not fresh,
                    'thread_id': 'bridge:' + row['id'], 'workstation_id': row['seat_id'], 'seat_id': row['seat_id'],
                    'native_session_id': row['native_session_id'],
                    'observed_at': stamp(row['observed']), 'last_activity_at': stamp(row['observed']),
                    'evidence': 'agent_report', 'adapter': 'aieyra-agent/1', 'session_state': row['state']}}
        return result

    def station_registry(self, value):
        """Membership survives lease expiry and missing runtime observations."""
        with self.store.db() as db:
            credentials = [dict(r) for r in db.execute('SELECT actor_id,revoked FROM agent_credentials')]
            bindings = {r['actor_id']:dict(r) for r in db.execute('SELECT * FROM agent_station_bindings')}
        known={r['actor_id'] for r in credentials}
        active={r['actor_id'] for r in credentials if not r['revoked']}
        for seat in value.get('seats',[]):
            actor=seat.get('actor_id');binding=bindings.get(actor)
            if actor in known:
                seat['membership_state']='active' if actor in active and (not binding or binding['seat_id']==seat['id']) else 'removed'
                if binding and binding['seat_id']==seat['id']:seat['station_binding']=binding
        return value

    def station_action(self, peer, action, value):
        fields={'request_id','session_id','reason'}
        extra={'enroll':{'name','project','token'},'retire':{'credential_id'},
               'handoff':{'actor_id','native_session_id','expected_version'}}
        if action not in extra or set(value)!=fields|extra[action]:raise AgentError('invalid_station_action')
        rid=ident(value['request_id']);reason=text(value['reason'],1000)
        hashed=digest(canonical({'station_action':action,'body':value}))
        with self.lock:
            session=self.session(peer,value['session_id']);self.validate_center(peer,session)
            with self.store.db() as db:
                old=db.execute('SELECT * FROM agent_requests WHERE credential_id=? AND id=?',(peer['id'],rid)).fetchone()
                if action=='retire':
                    target=db.execute('SELECT actor_id,project FROM agent_credentials WHERE id=?',(ident(value['credential_id']),)).fetchone()
                elif action=='handoff':
                    target=db.execute('SELECT actor_id,project FROM agent_credentials WHERE actor_id=? AND revoked=0',(ident(value['actor_id']),)).fetchone()
                else:target={'project':ident(value['project'])}
            if not target:raise AgentError('station_missing',404)
            grants=self.remote(peer['identity'],'registry').get('governance',[])
            if not any(g.get('active') and g.get('role')=='leader' and g.get('actor_id')==peer['actor_id'] and
                       (target['project'] in g.get('projects',[]) or '*' in g.get('projects',[])) for g in grants):
                raise AgentError('station_leader_required',403)
            if old:
                if old['digest']!=hashed:raise AgentError('request_id_conflict',409)
                return {**json.loads(old['response']),'replayed':True}
            self.session(peer,value['session_id'])
            if action=='enroll':
                result=self.enroll({'request_id':'station-'+digest(peer['id']+':'+rid)[:48],
                    **{k:value[k] for k in ('name','project','token')}})
                actor=result['credential']['actor_id']
            elif action=='retire':
                actor=target['actor_id']
                if actor==peer['actor_id']:raise AgentError('leader_successor_required',409)
                result=self.revoke({'credential_id':value['credential_id']})
            else:
                actor=target['actor_id'];native=ident(value['native_session_id'])
                with self.store.db() as db:
                    db.execute('BEGIN IMMEDIATE');self.expire(db)
                    binding=db.execute('SELECT * FROM agent_station_bindings WHERE actor_id=?',(actor,)).fetchone()
                    if not binding:raise AgentError('station_binding_missing',404)
                    if type(value['expected_version']) is not int or binding['version']!=value['expected_version']:raise AgentError('station_binding_version_conflict',409)
                    if db.execute("SELECT 1 FROM agent_sessions WHERE actor_id=? AND state='connected'",(actor,)).fetchone():raise AgentError('disconnect_before_native_handoff',409)
                    if db.execute('SELECT 1 FROM agent_station_bindings WHERE native_session_id=? AND actor_id!=?',(native,actor)).fetchone():raise AgentError('native_session_already_assigned',409)
                    db.execute('UPDATE agent_station_bindings SET native_session_id=?,version=version+1,updated=? WHERE actor_id=?',(native,time.time(),actor))
                    result={'station_binding':dict(db.execute('SELECT * FROM agent_station_bindings WHERE actor_id=?',(actor,)).fetchone())}
                    document={'leader':peer['actor_id'],'session_id':session['id'],'request_id':rid,'reason':reason,'result':result}
                    db.execute('INSERT INTO agent_station_audit(actor_id,action,document,created) VALUES(?,?,?,?)',(actor,action,canonical(document),time.time()))
                    self.record(db,peer,rid,hashed,result,'station.'+action,actor)
                    return result
            with self.store.db() as db:
                document={'leader':peer['actor_id'],'session_id':session['id'],'request_id':rid,'reason':reason,'result':result}
                db.execute('INSERT INTO agent_station_audit(actor_id,action,document,created) VALUES(?,?,?,?)',(actor,action,canonical(document),time.time()))
                self.record(db,peer,rid,hashed,result,'station.'+action,actor)
            return result

    def center(self, peer, route, body=None, query=None):
        if body is not None:
            ident(body.get('request_id'))
        # Always use the enrolled worker identity, including for privileged routes.
        return self.remote(peer['identity'], route, body, query)

    def owner_handoff(self,value):
        """Explicit local recovery when the only leader's native session was closed."""
        if set(value)!={'request_id','actor_id','native_session_id','expected_version','reason'}:raise AgentError('invalid_station_action')
        actor=ident(value['actor_id']);native=ident(value['native_session_id']);rid=ident(value['request_id']);reason=text(value['reason'],1000)
        hashed=digest(canonical(value))
        with self.lock,self.store.db() as db:
            db.execute('BEGIN IMMEDIATE');self.expire(db)
            old=db.execute("SELECT * FROM agent_requests WHERE credential_id='local-owner' AND id=?",(rid,)).fetchone()
            if old:
                if old['digest']!=hashed:raise AgentError('request_id_conflict',409)
                return {**json.loads(old['response']),'replayed':True}
            binding=db.execute('SELECT b.* FROM agent_station_bindings b JOIN agent_credentials c ON c.actor_id=b.actor_id WHERE b.actor_id=? AND c.revoked=0',(actor,)).fetchone()
            if not binding:raise AgentError('station_binding_missing',404)
            if type(value['expected_version']) is not int or value['expected_version']!=binding['version']:raise AgentError('station_binding_version_conflict',409)
            if db.execute("SELECT 1 FROM agent_sessions WHERE actor_id=? AND state='connected'",(actor,)).fetchone():raise AgentError('disconnect_before_native_handoff',409)
            if db.execute('SELECT 1 FROM agent_station_bindings WHERE native_session_id=? AND actor_id!=?',(native,actor)).fetchone():raise AgentError('native_session_already_assigned',409)
            db.execute('UPDATE agent_station_bindings SET native_session_id=?,version=version+1,updated=? WHERE actor_id=?',(native,time.time(),actor))
            result={'station_binding':dict(db.execute('SELECT * FROM agent_station_bindings WHERE actor_id=?',(actor,)).fetchone())}
            db.execute('INSERT INTO agent_station_audit(actor_id,action,document,created) VALUES(?,?,?,?)',(actor,'owner_handoff',canonical({'request_id':rid,'authority':'local_owner_csrf','reason':reason,'result':result}),time.time()))
            self.record(db,{'id':'local-owner'},rid,hashed,result,'station.owner_handoff',actor)
            return result

    def save_memory(self, peer, value):
        if value.get('project') != peer['project']:
            raise AgentError('memory_project_denied', 403)
        session_id = value.get('session_id')
        session = self.session(peer, session_id)
        self.validate_center(peer, session)
        # The local mutation does not weaken the existing center seat/lease checks.
        with self.lock:
            self.session(peer, session_id)
            def authorize(db):
                if not db.execute('SELECT 1 FROM agent_credentials WHERE id=? AND revoked=0', (peer['id'],)).fetchone():
                    raise AgentError('agent_unauthorized', 401)
                if not db.execute("SELECT 1 FROM agent_sessions WHERE id=? AND credential_id=? AND state='connected' AND lease_until>?",
                                  (session_id, peer['id'], time.time())).fetchone():
                    raise AgentError('agent_session_expired', 409)
            return self.app.project_memory.save({k: v for k, v in value.items() if k != 'session_id'},
                                                peer['actor_id'], session_id, authorize=authorize)

    def cloud_action(self, peer, action, value):
        session=self.session(peer,value.get('session_id'))
        self.validate_center(peer,session)
        if action=='share':
            if set(value)!={'request_id','session_id','body','public_consent'}:
                raise AgentError('invalid_public_share_fields')
            return self.app.cloud.call('/v1/community',{'request_id':ident(value['request_id']),
                'agent':peer['name'],'body':value['body'],'public_consent':value['public_consent']})
        if action=='check':return self.app.cloud.check()
        raise AgentError('cloud_action_not_supported',404)
