"""Control facade over the center's human authority and original product bridge.

The local database holds caches and one-attempt user submission receipts only.
It neither invents human blockers from task labels nor grants callback authority.
"""
import copy
import datetime as dt
import hashlib
import json
import re
import threading
import time

ID = re.compile(r'^[A-Za-z0-9_.:-]{1,100}$')
CATEGORIES = {'information', 'direction', 'authorization', 'environment'}
STATES = {'waiting_user', 'decision_recorded', 'submitting', 'delivered_to_agent', 'resuming', 'resolved', 'failed', 'unknown', 'cancelled', 'expired'}


class HumanError(Exception):
    def __init__(self, code, status=409):
        self.code, self.status = code, status


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def iso(stamp=None):
    return dt.datetime.fromtimestamp(time.time() if stamp is None else stamp, dt.timezone.utc).isoformat()


def identifier(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise HumanError('invalid_human_identifier', 400)
    return value


def decision_shape(value):
    """Bound untrusted input without rewriting the user's answer or IDs."""
    if not isinstance(value, dict) or not value:
        raise HumanError('invalid_human_decision', 400)
    if 'answers' in value:
        answers = value['answers']
        if set(value) != {'answers'} or not isinstance(answers, list) or len(answers) > 12:
            raise HumanError('invalid_human_answer', 400)
        seen = set()
        for answer in answers:
            if not isinstance(answer, dict) or 'question_id' not in answer or set(answer) - {'question_id', 'selected_ids', 'text'}:
                raise HumanError('invalid_human_answer', 400)
            question = identifier(answer['question_id'])
            if question in seen:
                raise HumanError('invalid_human_answer', 400)
            seen.add(question)
            chosen = answer.get('selected_ids', [])
            if not isinstance(chosen, list) or len(chosen) > 12:
                raise HumanError('invalid_human_choice', 400)
            for choice in chosen:
                identifier(choice)
            if len(set(chosen)) != len(chosen):
                raise HumanError('invalid_human_choice', 400)
    else:
        if set(value) - {'option_id', 'text'}:
            raise HumanError('invalid_human_decision', 400)
        if 'option_id' in value:
            identifier(value['option_id'])
        answers = [value]
    for answer in answers:
        if 'text' in answer:
            text = answer['text']
            if not isinstance(text, str) or not text.strip() or len(text) > 2000 or any(ord(c) < 32 and c not in '\n\t\r' for c in text):
                raise HumanError('invalid_human_text', 400)
            try:
                text.encode('utf-8')
            except UnicodeEncodeError:
                raise HumanError('invalid_human_text', 400) from None


def validate_decision(value, item):
    """Match the current center schema before persisting a submission intent."""
    schema = item['input_schema']
    if 'questions' in schema:
        if set(value) != {'answers'}:
            raise HumanError('invalid_human_answer', 400)
        questions = {q['id']: q for q in schema['questions']}
        seen = set()
        for answer in value['answers']:
            question = questions.get(answer['question_id'])
            if question is None:
                raise HumanError('invalid_human_answer', 400)
            seen.add(question['id'])
            chosen = answer.get('selected_ids', [])
            if len(chosen) > (12 if question['multiple'] else 1) or not set(chosen) <= {o['id'] for o in question['options']}:
                raise HumanError('invalid_human_choice', 400)
            if 'text' in answer and not question['allow_text']:
                raise HumanError('human_free_text_not_allowed', 400)
            if not chosen and not answer.get('text'):
                raise HumanError('human_answer_required', 400)
        if any(q['required'] and q['id'] not in seen for q in questions.values()):
            raise HumanError('human_answers_incomplete', 400)
    else:
        if 'answers' in value:
            raise HumanError('invalid_human_answer', 400)
        if 'option_id' in value and value['option_id'] not in {o['id'] for o in item['options']}:
            raise HumanError('invalid_human_choice', 400)
        if 'text' in value and not schema.get('allow_text'):
            raise HumanError('human_free_text_not_allowed', 400)


def project(item):
    """No auth, callback payload, remote paths or arbitrary fields reach the UI."""
    identifier(item['id'])
    if type(item.get('version')) is not int or item['version'] < 1 or item.get('state') not in STATES:
        raise HumanError('invalid_center_human_contract', 503)
    category = item.get('category')
    if category not in CATEGORIES:
        raise HumanError('human_context_contract_not_connected', 503)
    for name in ('recommendation', 'impact', 'resume_summary'):
        if not isinstance(item.get(name), str) or not item[name].strip():
            raise HumanError('human_context_contract_incomplete', 503)
    state = item['state']
    # Desktop schema1 has no submitting label. Retain authoritative state
    # separately while its displayed decision remains explicitly unconfirmed.
    return {'id': item['id'], 'version': item['version'], 'category': category,
            'state': 'decision_recorded' if state == 'submitting' else state, 'callback_state': state,
            'question': item['question'], 'recommendation': item['recommendation'],
            'facts': item.get('facts', ''), 'impact': item['impact'], 'resume_summary': item['resume_summary'],
            'options': copy.deepcopy(item['options']), 'input_schema': copy.deepcopy(item.get('input_schema')) or {'allow_text': bool(item['allow_text'])},
            'origin': {k: item.get(k) for k in ('project', 'host_id', 'adapter_id', 'seat_id', 'runtime_ref', 'agent_id', 'native_task_id', 'task_generation', 'native_request_id', 'job_id')},
            'created_at': iso(item['created']), 'expires_at': iso(item['expires']),
            'observed_at': iso(item['observed_at']) if item.get('observed_at') else None,
            'can_decide': item.get('can_decide') is True, 'binding_current': item.get('binding_current') is True,
            'fresh': item.get('fresh') is True, 'decision': copy.deepcopy(item.get('decision')),
            'error': item.get('error'), 'replay_allowed': False}


class HumanRequests:
    def __init__(self, store, hub, config, product_commands):
        self.store, self.hub, self.products = store, hub, product_commands
        self.projects = config.get('human_projects', [])
        if not isinstance(self.projects, list) or len(self.projects) > 16 or len(set(self.projects)) != len(self.projects):
            raise ValueError('invalid_human_projects')
        for value in self.projects:
            identifier(value)
        self.lock = threading.RLock()
        self.poll_lock = threading.Lock()
        self.decision_lock = threading.Lock()
        self.available, self.reason, self.checked_at = False, 'human_contract_not_connected', None
        self.adapter_status = []
        with self.store.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS human_decisions(
                request_id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,human_id TEXT NOT NULL,
                cloud_request_id TEXT NOT NULL,payload TEXT NOT NULL,state TEXT NOT NULL,
                observation TEXT,error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)''')
            db.execute("UPDATE human_decisions SET state='unknown',error='process_ended_query_only' WHERE state='submitting'")

    def cached(self):
        with self.store.db() as db:
            row = db.execute("SELECT payload FROM cache WHERE id='human_requests'").fetchone()
        return json.loads(row[0]) if row else {'items': [], 'observed_at': None}

    def poll(self):
        if not self.projects:
            return
        with self.poll_lock:
            self.checked_at = iso()
            try:
                # Existing monitor invokes the adapter. This never creates a
                # model executor, and paths come solely from local configuration.
                adapter_status = []
                for source in self.products.sources.values():
                    if source.get('human_host_ref'):
                        try:
                            result = self.products.human_sync(source['id'])
                            errors = result.get('errors') or []
                            adapter_status.append({'source_id': source['id'], 'state': 'degraded' if errors else 'observed',
                                                   'error_count': len(errors), 'checked_at': iso()})
                        except Exception:
                            adapter_status.append({'source_id': source['id'], 'state': 'unavailable',
                                                   'error': 'native_human_sync_unavailable', 'checked_at': iso()})
                self.adapter_status = adapter_status
                items = []
                for name in self.projects:
                    cursor = ''
                    for _ in range(20):
                        page = self.hub.human_snapshot(name, cursor)
                        items.extend(project(item) for item in page['human_requests'])
                        if not page['humans_has_more']:
                            break
                        next_cursor = page['next_human_cursor']
                        if not isinstance(next_cursor, str) or next_cursor <= cursor:
                            raise HumanError('invalid_human_cursor', 503)
                        cursor = next_cursor
                    else:
                        raise HumanError('human_page_limit', 503)
                items.sort(key=lambda x: x['id'])
                if len({x['id'] for x in items}) != len(items):
                    raise HumanError('duplicate_center_human_identity', 503)
                value = {'items': items, 'observed_at': iso()}
                with self.store.db() as db:
                    db.execute('BEGIN IMMEDIATE')
                    old = db.execute("SELECT payload FROM cache WHERE id='human_requests'").fetchone()
                    previous = json.loads(old[0])['items'] if old else []
                    before = {item['id']: item for item in previous}
                    for item in items:
                        if item['id'] in before and item['version'] < before[item['id']]['version']:
                            raise HumanError('human_version_regressed', 503)
                        if item != before.get(item['id']):
                            self.store.event(db, 'human_request.changed', item['id'])
                    for missing in before.keys() - {item['id'] for item in items}:
                        self.store.event(db, 'human_request.changed', missing)
                    db.execute("INSERT OR REPLACE INTO cache VALUES('human_requests',?)", (canonical(value),))
                self.available, self.reason = True, None
            except Exception as error:
                self.available = False
                self.reason = error.code if isinstance(error, HumanError) else 'human_center_unavailable'

    def snapshot(self):
        with self.lock:
            cached = self.cached()
            fresh = bool(cached['observed_at']) and (dt.datetime.now(dt.timezone.utc)-dt.datetime.fromisoformat(cached['observed_at'])).total_seconds() < 120
            items = cached['items']
            # A stale native wait may not trigger desktop attention. Keep facts
            # but suppress notifications for the feed until all waits are current.
            native_fresh = all(x['state'] != 'waiting_user' or x['fresh'] and x['binding_current'] for x in items)
            adapters_fresh = all(x['state'] == 'observed' for x in self.adapter_status)
            return {'schema_version': 1, 'source': 'coordination', 'available': self.available,
                    'stale': not (self.available and fresh and native_fresh and adapters_fresh), 'items': items,
                    'reason': self.reason or (None if adapters_fresh else 'native_human_sync_incomplete'),
                    'adapter_status': copy.deepcopy(self.adapter_status),
                    'observed_at': cached['observed_at'], 'checked_at': self.checked_at}

    def detail(self, ident):
        identifier(ident)
        if not self.projects:
            raise HumanError('human_contract_not_connected', 503)
        try:
            item = self.hub.human_get(ident)
            if item.get('project') not in self.projects:
                raise HumanError('human_project_not_configured', 403)
            return {'schema_version': 1, 'source': 'coordination', 'available': True, 'stale': not item.get('fresh'), 'item': project(item)}
        except HumanError:
            raise
        except Exception:
            raise HumanError('human_center_unavailable', 503) from None

    def decide(self, ident, value):
        with self.decision_lock:
            return self._decide(ident, value)

    def _decide(self, ident, value):
        identifier(ident)
        if not isinstance(value, dict) or set(value) != {'request_id', 'expected_version', 'decision'} or type(value['expected_version']) is not int or value['expected_version'] < 1:
            raise HumanError('invalid_human_decision', 400)
        value = copy.deepcopy(value)
        rid = identifier(value['request_id'])
        decision = value['decision']
        decision_shape(decision)
        payload = {'id': ident, **value}
        with self.store.db() as db:
            old = db.execute('SELECT fingerprint FROM human_decisions WHERE request_id=?', (rid,)).fetchone()
            if old and old[0] != digest(payload):
                raise HumanError('human_decision_id_conflict')
        if old:
            return self.decision_status(rid)
        detail = self.detail(ident)
        item = detail['item']
        if not item['can_decide'] or detail['stale'] or item['version'] != value['expected_version']:
            raise HumanError('human_request_not_decidable')
        validate_decision(decision, item)
        cloud = 'control-human-' + digest([rid, ident])[:64]
        cloud_payload = {'request_id': cloud, 'id': ident, 'version': value['expected_version'], 'decision': decision}
        # coord.call uses this exact UTF-8 encoding, and the center accepts 32 KiB.
        # The generated request ID can make a locally accepted body exceed it.
        if len(json.dumps(cloud_payload, ensure_ascii=False).encode('utf-8')) > 32768:
            raise HumanError('human_decision_body_limit', 413)
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT fingerprint FROM human_decisions WHERE request_id=?', (rid,)).fetchone()
            if old:
                if old[0] != digest(payload):
                    raise HumanError('human_decision_id_conflict')
            else:
                db.execute('INSERT INTO human_decisions VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (rid, digest(payload), ident, cloud, canonical(payload), 'submitting', None, None, iso(), iso()))
                self.store.event(db, 'human_decision.submitting', rid)
        if old:
            return self.decision_status(rid)
        try:
            result = self.hub.human_decide(cloud_payload)
            if result.get('id') != ident or result.get('decision') != decision or result.get('state') == 'waiting_user':
                raise HumanError('human_decision_receipt_mismatch')
            self.record(rid, 'decision_recorded', result)
        except Exception:
            self.record(rid, 'unknown', error='decision_outcome_unknown_query_only')
        return self.decision_status(rid)

    def record(self, rid, state, observation=None, error=None):
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT state,observation,error FROM human_decisions WHERE request_id=?', (rid,)).fetchone()
            if prior['state'] == 'decision_recorded' and state != 'decision_recorded':
                return
            if observation and prior['observation'] and observation.get('version', 0) < json.loads(prior['observation']).get('version', 0):
                return
            if (state, canonical(observation) if observation else prior['observation'], error) == tuple(prior):
                return
            db.execute('UPDATE human_decisions SET state=?,observation=COALESCE(?,observation),error=?,updated_at=? WHERE request_id=?',
                       (state, canonical(observation) if observation else None, error, iso(), rid))
            self.store.event(db, 'human_decision.changed', rid)

    def decision_status(self, rid):
        identifier(rid)
        with self.store.db() as db:
            row = db.execute('SELECT * FROM human_decisions WHERE request_id=?', (rid,)).fetchone()
        if not row:
            raise HumanError('human_decision_not_found', 404)
        row = dict(row)
        try:
            current = self.hub.human_get(row['human_id'])
            if current.get('decision_request_id') == row['cloud_request_id'] and current.get('decision') == json.loads(row['payload'])['decision']:
                self.record(rid, 'decision_recorded', current)
                row.update(state='decision_recorded', observation=canonical(current), error=None)
        except Exception:
            pass
        observation = json.loads(row['observation']) if row['observation'] else None
        return {'request_id': rid, 'human_id': row['human_id'], 'state': row['state'],
                'callback_state': observation.get('state') if observation else None,
                'item': project(observation) if observation else None, 'error': row['error'], 'replay_allowed': False}
