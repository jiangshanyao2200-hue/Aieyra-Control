"""Durable product jobs. Execution and private materials remain on the host.

Uses the existing actor/request transaction, registry and mobile runtime guards.
Claims never mean product receipt; product completion never means review acceptance.
"""
from __future__ import annotations

import json
import re
import time

from .core import Fault, ident, text
from . import registry
from .mobile import canonical, digest, generation, runtime_ready

ACTIONS = {'product-job-create', 'product-job-claim', 'product-job-begin',
           'product-job-receipt', 'product-job-cancel', 'product-job-review'}
TERMINAL = {'completed', 'failed', 'cancelled', 'timed_out', 'interrupted', 'persistence_failed'}
OBSERVED = {'received', 'running', 'waiting_user', 'cancelling'} | TERMINAL


def fields(body, required, optional=()):
    if not isinstance(body, dict) or set(body) - set(required) - set(optional) or not set(required) <= set(body):
        raise Fault('invalid_product_fields')


def sha(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise Fault('invalid_product_hash')
    return value


def alpha(value):
    if not isinstance(value, str) or not re.fullmatch(r'alpha-[A-Za-z0-9][A-Za-z0-9_.-]{0,89}', value) or value.endswith('.'):
        raise Fault('invalid_alpha_task_id')
    return value


def migrate(d):
    d.executescript('''
    CREATE TABLE IF NOT EXISTS product_jobs(
      id TEXT PRIMARY KEY, task_id TEXT UNIQUE NOT NULL REFERENCES tasks(id),
      project TEXT NOT NULL REFERENCES registered_projects(id), seat_id TEXT NOT NULL REFERENCES seats(id),
      host_id TEXT NOT NULL REFERENCES registered_hosts(id), adapter_id TEXT NOT NULL REFERENCES registered_adapters(id),
      host_actor TEXT NOT NULL, producer_actor TEXT NOT NULL, adapter_version TEXT NOT NULL,
      seat_epoch INTEGER NOT NULL, runtime_generation TEXT NOT NULL, runtime_ref TEXT NOT NULL,
      input_ref TEXT NOT NULL, input_sha256 TEXT NOT NULL, summary TEXT NOT NULL, criteria TEXT NOT NULL,
      mode TEXT NOT NULL, actions TEXT NOT NULL, authority_ref TEXT NOT NULL, requested_by TEXT NOT NULL REFERENCES actors(id),
      state TEXT NOT NULL, last_fact TEXT, observed_ns TEXT, observed_at REAL, error TEXT,
      ledger_instance TEXT, claim_until REAL, begun_at REAL, agent_id TEXT, product_generation INTEGER,
      received_at REAL, running_at REAL, completed_at REAL, delivery TEXT,
      product_acceptance TEXT NOT NULL DEFAULT 'pending', product_review TEXT,
      review_state TEXT NOT NULL DEFAULT 'pending', reviewed_by TEXT, review TEXT,
      cancel_requested INTEGER NOT NULL DEFAULT 0, recovery_hold INTEGER NOT NULL DEFAULT 0,
      version INTEGER NOT NULL DEFAULT 1, last_seq INTEGER NOT NULL DEFAULT 0,
      created REAL NOT NULL, updated REAL NOT NULL, expires REAL NOT NULL);
    CREATE INDEX IF NOT EXISTS product_jobs_outbox ON product_jobs(host_id,created,id);
    CREATE TABLE IF NOT EXISTS product_receipts(
      host_id TEXT NOT NULL,adapter_id TEXT NOT NULL,ledger_instance TEXT NOT NULL,task_id TEXT NOT NULL,
      seq INTEGER NOT NULL,job_id TEXT NOT NULL REFERENCES product_jobs(id),digest TEXT NOT NULL,
      projection TEXT NOT NULL,ack TEXT NOT NULL,committed REAL NOT NULL,
      PRIMARY KEY(host_id,adapter_id,ledger_instance,task_id,seq));
    ''')
    if 'begun_at' not in {row[1] for row in d.execute('PRAGMA table_info(product_jobs)')}:
        d.execute('ALTER TABLE product_jobs ADD COLUMN begun_at REAL')
    if 'product_request_id' not in {row[1] for row in d.execute('PRAGMA table_info(product_jobs)')}:
        d.execute('ALTER TABLE product_jobs ADD COLUMN product_request_id TEXT')
    d.execute("UPDATE product_jobs SET product_request_id='control:' || task_id WHERE product_request_id IS NULL")
    d.execute('CREATE UNIQUE INDEX IF NOT EXISTS product_native_request ON product_jobs(host_id,adapter_id,runtime_generation,product_request_id)')


def get(d, job_id):
    row = d.execute('SELECT * FROM product_jobs WHERE id=?', (ident(job_id),)).fetchone()
    if not row:
        raise Fault('product_job_missing', 404)
    return dict(row)


def host(d, actor, job):
    current = registry.row(d, 'registered_hosts', job['host_id'])
    if actor['id'] != current['actor_id'] or actor['id'] != job['host_actor']:
        raise Fault('host_identity_required', 403)


def binding(d, job):
    seat = registry.row(d, 'seats', job['seat_id'])
    adapter = registry.row(d, 'registered_adapters', job['adapter_id'])
    h = registry.row(d, 'registered_hosts', job['host_id'])
    return (seat['epoch'] == job['seat_epoch'] and generation(seat) == job['runtime_generation']
            and seat['host_id'] == job['host_id'] and seat['adapter_id'] == job['adapter_id']
            and seat['actor_id'] == job['producer_actor'] and h['actor_id'] == job['host_actor']
            and adapter['version_label'] == job['adapter_version'])


def authorized(d, job, now):
    requester = d.execute('SELECT * FROM actors WHERE id=? AND revoked=0', (job['requested_by'],)).fetchone()
    adapter = registry.row(d, 'registered_adapters', job['adapter_id'])
    seat = registry.row(d, 'seats', job['seat_id'])
    return bool(requester and registry.manager(d, requester, job['project']) and binding(d, job)
                and 'product.dispatch' in json.loads(adapter['actions_json'])
                and runtime_ready(d, seat, now) and job['expires'] > now
                and not job['cancel_requested'] and not job['recovery_hold'])


def refresh(d, job, now):
    """Stop new execution, without turning lack of observation into a runtime fact."""
    reason = None
    if job['state'] not in TERMINAL:
        requester = d.execute('SELECT * FROM actors WHERE id=? AND revoked=0', (job['requested_by'],)).fetchone()
        if job['expires'] <= now:
            reason = 'authorization_expired'
        elif not requester or not registry.manager(d, requester, job['project']):
            reason = 'authority_revoked'
        elif not binding(d, job):
            reason = 'binding_changed'
        elif job['state'] == 'claimed' and job['claim_until'] <= now:
            reason = 'claim_expired'
    if reason and not job['cancel_requested']:
        state = 'cancelled' if job['state'] in ('pending', 'claimed') else 'unknown'
        d.execute('UPDATE product_jobs SET state=?,cancel_requested=1,error=?,version=version+1,updated=? WHERE id=?',
                  (state, reason, now, job['id']))
        job = get(d, job['id'])
        from collaboration import event
        event(d, job['host_actor'], 'product-job-invalidated', job['id'], job['project'], 'product_job', now)
    return job


def public(d, job, now):
    value = dict(job)
    for key in ('criteria', 'actions', 'delivery', 'product_review', 'review'):
        value[key] = json.loads(value[key]) if value[key] else None
    value['product_request_id'] = job['product_request_id']
    value['delivery_sha256'] = digest(job['delivery']) if job['delivery'] else None
    value['fresh'] = bool(job['observed_at'] and 0 <= now - job['observed_at'] < 180)
    value['execution_authorized'] = authorized(d, job, now)
    value['can_claim'] = job['state'] == 'pending' and authorized(d, job, now)
    value['can_begin'] = (job['state'] == 'claimed' and job['claim_until'] > now and authorized(d, job, now))
    value['replay_allowed'] = False
    value['next_action'] = ('claim' if value['can_claim'] else 'begin' if value['can_begin'] else
                            'query' if job['ledger_instance'] and job['state'] not in TERMINAL else 'none')
    value['cancel_supported'] = False  # Current Bridge has no cancel RPC.
    return value


def read(d, actor, path, query, now):
    if path == '/v1/product-job':
        job = get(d, query.get('job_id', [''])[0])
        if actor['id'] not in (job['host_actor'], job['producer_actor']):
            registry.require_manager(d, actor, job['project'])
        return public(d, refresh(d, job, now), now)
    if path == '/v1/product-job-receipts':
        job = get(d, query.get('job_id', [''])[0])
        if actor['id'] not in (job['host_actor'], job['producer_actor']):
            registry.require_manager(d, actor, job['project'])
        try:
            after = max(0, int(query.get('after', ['0'])[0]))
        except ValueError:
            raise Fault('invalid_cursor')
        rows = d.execute('SELECT seq,projection,committed FROM product_receipts WHERE job_id=? AND seq>? ORDER BY seq LIMIT 100', (job['id'], after)).fetchall()
        return {'job_id': job['id'], 'receipts': [dict(r, projection=json.loads(r['projection'])) for r in rows],
                'next_cursor': rows[-1]['seq'] if rows else after}
    h = registry.row(d, 'registered_hosts', query.get('host_id', [''])[0])
    if actor['id'] != h['actor_id']:
        raise Fault('host_identity_required', 403)
    # Keyset pagination includes terminal jobs so an offline host can reconcile them.
    after = query.get('after', [''])[0]
    if after:
        ident(after)
    rows = d.execute('SELECT * FROM product_jobs WHERE host_id=? AND id>? ORDER BY id LIMIT 100', (h['id'], after)).fetchall()
    return {'host_id': h['id'], 'jobs': [public(d, refresh(d, dict(r), now), now) for r in rows],
            'next_cursor': rows[-1]['id'] if rows else after, 'has_more': len(rows) == 100}


def delivery(value):
    fields(value, {'summary', 'artifacts', 'captured_ns'})
    stamp(value['captured_ns'])
    summary = text(value['summary'], 2000, empty=True)
    items = value['artifacts']
    if not isinstance(items, list) or len(items) > 32:
        raise Fault('invalid_artifacts')
    total, refs = 0, set()
    for item in items:
        fields(item, {'ref', 'sha256', 'bytes'})
        ref = ident(item['ref'])
        sha(item['sha256'])
        total += registry.integer(item['bytes'], 0, 64 << 20)
        if ref in refs or total > 256 << 20:
            raise Fault('invalid_artifacts')
        refs.add(ref)
    return {'summary': summary, 'artifacts': items, 'captured_ns': value['captured_ns']}


def stamp(value):
    if not isinstance(value, str) or not re.fullmatch(r'[1-9][0-9]{0,19}', value):
        raise Fault('invalid_observation_time')
    return int(value)


def receipt(d, actor, body, now):
    fields(body, {'request_id', 'job_id', 'version', 'ledger_instance', 'seq', 'input_sha256',
                  'seat_epoch', 'runtime_generation', 'kind', 'observed_ns'},
           {'agent_id', 'generation', 'delivery', 'product_review', 'error', 'product_request_id', 'record_sha256'})
    job = get(d, body['job_id']); host(d, actor, job)
    ledger = ident(body['ledger_instance']); seq = registry.integer(body['seq'], 1, 2**53 - 1)
    projection = {k: v for k, v in body.items() if k not in ('request_id', 'version')}
    fingerprint = digest(canonical(projection))
    key = (job['host_id'], job['adapter_id'], ledger, job['task_id'], seq)
    old = d.execute('SELECT digest,ack FROM product_receipts WHERE host_id=? AND adapter_id=? AND ledger_instance=? AND task_id=? AND seq=?', key).fetchone()
    if old:
        if old['digest'] != fingerprint:
            raise Fault('receipt_id_conflict', 409)
        return json.loads(old['ack'])
    registry.version(body, job)
    if ledger != job['ledger_instance'] or seq <= job['last_seq']:
        raise Fault('receipt_ledger_or_order_conflict', 409)
    if body['input_sha256'] != job['input_sha256'] or body['seat_epoch'] != job['seat_epoch'] or type(body['seat_epoch']) is not int or body['runtime_generation'] != job['runtime_generation']:
        raise Fault('receipt_binding_mismatch', 409)
    if job['begun_at'] is None or not job['ledger_instance']:
        raise Fault('execution_not_begun', 409)
    kind = body['kind']; ns = stamp(body['observed_ns'])
    if not isinstance(kind, str):
        raise Fault('unsupported_product_receipt')
    if ns > int((now + 120) * 1_000_000_000) or (kind != 'observation_failed' and job['observed_ns'] and ns < int(job['observed_ns'])):
        raise Fault('stale_observation', 409)
    if kind == 'observation_failed':
        fields(body, {'request_id', 'job_id', 'version', 'ledger_instance', 'seq', 'input_sha256', 'seat_epoch', 'runtime_generation', 'kind', 'observed_ns', 'error'})
        code = ident(body['error'])
        state = job['state'] if job['last_fact'] in TERMINAL else 'unknown'
        d.execute('UPDATE product_jobs SET state=?,error=?,observed_at=NULL WHERE id=?', (state, code, job['id']))
    elif kind in OBSERVED or kind == 'product_accepted':
        if 'error' in body:
            raise Fault('invalid_product_fields')
        aid = ident(body.get('agent_id')); gen = registry.integer(body.get('generation'), 1, 2**31-1)
        sha(body.get('record_sha256'))
        if body.get('product_request_id') != job['product_request_id']:
            raise Fault('product_request_mismatch', 409)
        if job['agent_id'] not in (None, aid) or job['product_generation'] not in (None, gen):
            raise Fault('product_identity_or_generation_changed', 409)
        fact = 'completed' if kind == 'product_accepted' else kind
        if job['state'] == 'cancelled' and not job['agent_id']:
            raise Fault('cancelled_before_execution', 409)
        if job['last_fact'] in TERMINAL and job['last_fact'] != fact:
            raise Fault('terminal_fact_conflict', 409)
        ranks = {'received': 1, 'running': 2, 'waiting_user': 2, 'cancelling': 3}
        if ranks.get(fact, 4) < ranks.get(job['last_fact'], 0):
            raise Fault('fact_regression', 409)
        delivered = delivery(body['delivery']) if 'delivery' in body else None
        if fact == 'completed' and delivered is None and job['delivery'] is None:
            raise Fault('delivery_required')
        if delivered and fact != 'completed':
            raise Fault('unexpected_delivery')
        if delivered and stamp(delivered['captured_ns']) > ns:
            raise Fault('delivery_after_observation')
        if job['delivery'] and delivered and canonical(delivered) != job['delivery']:
            raise Fault('delivery_changed', 409)
        if kind == 'product_accepted':
            proof = body.get('product_review')
            fields(proof, {'ref', 'sha256', 'generation', 'delivery_sha256'})
            ident(proof['ref']); sha(proof['sha256'])
            if type(proof['generation']) is not int or proof['generation'] != gen or proof['delivery_sha256'] != digest(canonical(delivered) if delivered else job['delivery']):
                raise Fault('product_review_mismatch', 409)
            d.execute("UPDATE product_jobs SET product_acceptance='accepted',product_review=? WHERE id=?", (canonical(proof), job['id']))
        elif 'product_review' in body:
            raise Fault('unexpected_product_review')
        d.execute('''UPDATE product_jobs SET state=?,last_fact=?,agent_id=?,product_generation=?,
                    received_at=COALESCE(received_at,?),delivery=COALESCE(delivery,?),error=NULL,observed_at=? WHERE id=?''',
                  (fact, fact, aid, gen, now, canonical(delivered) if delivered else None, ns / 1_000_000_000, job['id']))
        if fact == 'running':
            d.execute('UPDATE product_jobs SET running_at=COALESCE(running_at,?) WHERE id=?', (now, job['id']))
        if fact == 'completed':
            d.execute('UPDATE product_jobs SET completed_at=COALESCE(completed_at,?) WHERE id=?', (now, job['id']))
    else:
        raise Fault('unsupported_product_receipt')
    d.execute('UPDATE product_jobs SET last_seq=?,observed_ns=?,version=version+1,updated=? WHERE id=?',
              (seq, body['observed_ns'] if kind != 'observation_failed' else job['observed_ns'], now, job['id']))
    ack = {'job_id': job['id'], 'task_id': job['task_id'], 'ledger_instance': ledger, 'ack_seq': seq,
           'committed': True, 'version': job['version'] + 1, 'state': get(d, job['id'])['state']}
    d.execute('INSERT INTO product_receipts VALUES(?,?,?,?,?,?,?,?,?,?)', (*key, job['id'], fingerprint, canonical(projection), canonical(ack), now))
    return ack


def write(d, actor, action, body, now):
    if action == 'product-job-receipt':
        return receipt(d, actor, body, now)
    if action == 'product-job-create':
        fields(body, {'request_id', 'job_id', 'task_id', 'project', 'seat_id', 'input_ref', 'input_sha256',
                      'summary', 'criteria', 'authority_ref', 'mode'}, {'expires_in', 'actions', 'product_request_id'})
        project = ident(body['project']); registry.require_manager(d, actor, project)
        tid = alpha(body['task_id']); task = registry.row(d, 'tasks', tid)
        if task['project'] != project or task['status'] in ('accepted', 'delivered', 'cancelled', 'superseded'):
            raise Fault('product_task_mismatch')
        seat = registry.row(d, 'seats', body['seat_id'])
        h = registry.row(d, 'registered_hosts', seat['host_id']); adapter = registry.row(d, 'registered_adapters', seat['adapter_id'])
        if seat['project'] != project or not seat['actor_id']:
            raise Fault('product_seat_mismatch')
        if not runtime_ready(d, seat, now) or 'product.dispatch' not in json.loads(adapter['actions_json']):
            raise Fault('product_runtime_unavailable', 409)
        actions = body.get('actions', ['dispatch', 'query'])
        if actions != ['dispatch', 'query']:
            raise Fault('invalid_product_actions')
        criteria = body['criteria']
        if not isinstance(criteria, list) or not 1 <= len(criteria) <= 24:
            raise Fault('product_criteria_required')
        criteria = [text(v, 1000) for v in criteria]
        if body['mode'] not in ('fixture', 'os', 'workspace'):
            raise Fault('invalid_product_mode')
        if d.execute("SELECT count(*) FROM product_jobs WHERE host_id=? AND state NOT IN ('completed','failed','cancelled','timed_out','interrupted','persistence_failed')", (h['id'],)).fetchone()[0] >= 256:
            raise Fault('product_host_backlog_limit', 429)
        oid = ident(body['job_id']); ttl = registry.integer(body.get('expires_in', 3600), 30, 86400)
        values = dict(id=oid, task_id=tid, project=project, seat_id=seat['id'], host_id=h['id'], adapter_id=adapter['id'],
                      host_actor=h['actor_id'], producer_actor=seat['actor_id'], adapter_version=adapter['version_label'],
                      seat_epoch=seat['epoch'], runtime_generation=generation(seat), runtime_ref=seat['runtime_ref'],
                      input_ref=ident(body['input_ref']), input_sha256=sha(body['input_sha256']), summary=text(body['summary'], 2000),
                      criteria=canonical(criteria), mode=body['mode'], actions=canonical(actions), authority_ref=text(body['authority_ref'], 600),
                      requested_by=actor['id'], state='pending', created=now, updated=now, expires=now+ttl,
                      product_request_id=ident(body.get('product_request_id', 'control:' + tid)))
        d.execute('INSERT INTO product_jobs (' + ','.join(values) + ') VALUES (' + ','.join('?' for _ in values) + ')', tuple(values.values()))
        return public(d, get(d, oid), now)
    required = {'request_id', 'job_id', 'version'}
    if action == 'product-job-claim':
        fields(body, required | {'ledger_instance', 'input_sha256'}, {'lease_seconds'})
    elif action == 'product-job-begin':
        fields(body, required | {'ledger_instance', 'input_sha256'})
    elif action == 'product-job-cancel':
        fields(body, required | {'reason'})
    elif action == 'product-job-review':
        fields(body, required | {'delivery_sha256', 'generation', 'checks', 'verdict'})
    else:
        raise Fault('not_found', 404)
    job = get(d, body['job_id']); registry.version(body, job)
    if action in ('product-job-claim', 'product-job-begin'):
        host(d, actor, job)
        ledger = ident(body.get('ledger_instance'))
        if body.get('input_sha256') != job['input_sha256']:
            raise Fault('product_input_mismatch', 409)
        if not authorized(d, job, now):
            raise Fault('product_authorization_unavailable', 409)
        if action == 'product-job-claim':
            if job['state'] != 'pending':
                raise Fault('product_not_claimable', 409)
            if d.execute("SELECT 1 FROM product_jobs WHERE seat_id=? AND id!=? AND state NOT IN ('pending','completed','failed','cancelled','timed_out','interrupted','persistence_failed')", (job['seat_id'],job['id'])).fetchone():
                raise Fault('product_seat_busy', 409)
            lease = registry.integer(body.get('lease_seconds', 60), 30, 180)
            d.execute("UPDATE product_jobs SET state='claimed',ledger_instance=?,claim_until=? WHERE id=?", (ledger, now+lease, job['id']))
        else:
            if job['state'] != 'claimed' or job['ledger_instance'] != ledger or job['claim_until'] <= now:
                raise Fault('product_begin_conflict', 409)
            d.execute("UPDATE product_jobs SET state='submitting',begun_at=? WHERE id=?", (now, job['id']))
    elif action == 'product-job-cancel':
        registry.require_manager(d, actor, job['project'])
        reason = text(body.get('reason'), 600)
        state = ('cancelled' if job['state'] in ('pending', 'claimed') else
                 job['state'] if job['state'] in TERMINAL else 'unknown')
        d.execute('UPDATE product_jobs SET cancel_requested=1,state=?,error=? WHERE id=?', (state, reason, job['id']))
    elif action == 'product-job-review':
        registry.require_manager(d, actor, job['project'])
        if actor['id'] in (job['host_actor'], job['producer_actor']):
            raise Fault('independent_reviewer_required', 403)
        if job['last_fact'] != 'completed' or not job['delivery'] or job['review_state'] != 'pending':
            raise Fault('completed_unreviewed_job_required', 409)
        if body.get('delivery_sha256') != digest(job['delivery']) or type(body.get('generation')) is not int or body['generation'] != job['product_generation']:
            raise Fault('review_delivery_mismatch', 409)
        checks = body.get('checks'); criteria = json.loads(job['criteria'])
        if not isinstance(checks, list) or len(checks) != len(criteria):
            raise Fault('all_criteria_required')
        verdict = body.get('verdict')
        if verdict not in ('accepted', 'rejected'):
            raise Fault('invalid_review_verdict')
        seen = set()
        for check in checks:
            fields(check, {'criterion', 'passed', 'evidence_ref', 'evidence_sha256', 'summary'})
            index = registry.integer(check['criterion'], 0, len(criteria)-1)
            if index in seen or type(check['passed']) is not bool or (verdict == 'accepted' and not check['passed']):
                raise Fault('all_criteria_required')
            seen.add(index); ident(check['evidence_ref']); sha(check['evidence_sha256']); text(check['summary'], 1000)
        d.execute('UPDATE product_jobs SET review_state=?,reviewed_by=?,review=? WHERE id=?', (verdict, actor['id'], canonical(checks), job['id']))
    else:
        raise Fault('not_found', 404)
    d.execute('UPDATE product_jobs SET version=version+1,updated=? WHERE id=?', (now, job['id']))
    result = public(d, get(d, job['id']), now)
    if action == 'product-job-begin':
        result['dispatch_permit_until'] = min(now + 30, job['expires'], job['claim_until'])
        result['dispatch_allowed'] = True
    return result


def invalidate_restored_jobs(d, now):
    """Offline restore requires reconciliation with original host ledgers, never dispatch."""
    count = d.execute("SELECT count(*) FROM product_jobs WHERE state NOT IN ('completed','failed','cancelled','timed_out','interrupted','persistence_failed')").fetchone()[0]
    d.execute("""UPDATE product_jobs SET recovery_hold=1,cancel_requested=1,
                 state=CASE WHEN state IN ('pending','claimed') THEN 'cancelled' ELSE 'unknown' END,
                 error='restored_database_query_only',version=version+1,updated=?
                 WHERE state NOT IN ('completed','failed','cancelled','timed_out','interrupted','persistence_failed')""", (now,))
    return {'held_product_jobs': count, 'replay_allowed': False}
