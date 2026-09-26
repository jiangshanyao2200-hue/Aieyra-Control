"""Engineering requirements and append-only intake evidence in the existing ledger."""
from __future__ import annotations

import json
import re

from .core import Fault, ident, text
from . import library
from . import registry

ACTIONS = {'requirement-put', 'requirement-receipt', 'requirement-receipt-retract', 'task-retire'}
READS = {'/v1/requirements', '/v1/requirement', '/v1/tasks', '/v1/task',
         '/v1/requirement-receipts', '/v1/intake-receipt', '/v1/intake-events'}
STATES = {'active', 'cancelled', 'superseded'}
TASK_STATES = {'planned', 'doing', 'blocked', 'delivered', 'accepted', 'cancelled', 'superseded'}
KINDS = {'dispatched', 'queue_consumed', 'native_received', 'native_started', 'owner_received',
         'progress', 'completed', 'unknown', 'failed', 'independent_review'}


def migrate(database):
    database.executescript('''
    CREATE TABLE IF NOT EXISTS requirements(
      id TEXT PRIMARY KEY,project TEXT NOT NULL REFERENCES registered_projects(id),
      source_system TEXT NOT NULL,source_id TEXT NOT NULL,publisher_actor TEXT NOT NULL,
      version INTEGER NOT NULL,document TEXT NOT NULL,search_text TEXT NOT NULL,
      UNIQUE(source_system,source_id));
    CREATE TABLE IF NOT EXISTS requirement_revisions(
      id TEXT NOT NULL REFERENCES requirements(id),version INTEGER NOT NULL,document TEXT NOT NULL,
      PRIMARY KEY(id,version));
    CREATE TABLE IF NOT EXISTS requirement_receipts(
      seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE NOT NULL,
      requirement_id TEXT NOT NULL,requirement_version INTEGER NOT NULL,
      delivery_id TEXT NOT NULL,kind TEXT NOT NULL,event_id TEXT NOT NULL,
      reporter_actor TEXT NOT NULL,body TEXT NOT NULL,document TEXT NOT NULL,
      FOREIGN KEY(requirement_id,requirement_version) REFERENCES requirement_revisions(id,version),
      UNIQUE(delivery_id,kind,event_id));
    CREATE INDEX IF NOT EXISTS requirement_receipt_page ON requirement_receipts(requirement_id,seq);
    CREATE TABLE IF NOT EXISTS requirement_receipt_retractions(
      receipt_id TEXT PRIMARY KEY REFERENCES requirement_receipts(id),document TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS task_retirements(
      task_id TEXT PRIMARY KEY REFERENCES tasks(id),document TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS intake_writes(
      actor TEXT NOT NULL,request_id TEXT NOT NULL,action TEXT NOT NULL,object_id TEXT NOT NULL,
      created REAL NOT NULL,PRIMARY KEY(actor,request_id));
    ''')


def fields(value, expected):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise Fault('invalid_intake_fields')


def safe(value, maximum=600, empty=False):
    result = text(value, maximum, empty=empty)
    if library.SECRET_METADATA.search(result) or re.search(r'://[^/\s]+@', result):
        raise Fault('possible_secret_use_reference_instead')
    return result


def hash_value(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise Fault('invalid_evidence_hash')
    return value


def choice(value, allowed):
    if not isinstance(value, str) or value not in allowed:
        raise Fault('invalid_intake_state')
    return value


def array(value, maximum=32):
    if not isinstance(value, list) or len(value) > maximum:
        raise Fault('invalid_intake_list')
    return value


def identifiers(value):
    result = [ident(entry) for entry in array(value)]
    if len(result) != len(set(result)):
        raise Fault('duplicate_intake_link')
    return result


def actor_exists(database, identifier):
    if not database.execute('SELECT 1 FROM actors WHERE id=? AND revoked=0', (ident(identifier),)).fetchone():
        raise Fault('intake_target_missing', 404)


def require_intake_manager(database, actor, project):
    if not database.execute('SELECT 1 FROM registered_projects WHERE id=?', (ident(project),)).fetchone():
        raise Fault('unknown_project')
    if not registry.manager(database, actor, 'coordination'):
        registry.require_projects(database, actor, [project])


def get(database, identifier, version=None):
    identifier = ident(identifier)
    if version is None:
        row = database.execute('SELECT document FROM requirements WHERE id=?', (identifier,)).fetchone()
    else:
        registry.integer(version, 1, 2**31 - 1)
        row = database.execute('SELECT document FROM requirement_revisions WHERE id=? AND version=?', (identifier, version)).fetchone()
    if not row:
        raise Fault('requirement_missing', 404)
    return json.loads(row['document'])


def task(database, identifier):
    row = database.execute('SELECT * FROM tasks WHERE id=?', (ident(identifier),)).fetchone()
    if not row:
        raise Fault('task_missing', 404)
    result = dict(row)
    retired = database.execute('SELECT document FROM task_retirements WHERE task_id=?', (identifier,)).fetchone()
    result['retirement'] = json.loads(retired[0]) if retired else None
    result['requirement_ids'] = [entry['id'] for entry in
        (json.loads(row[0]) for row in database.execute('SELECT document FROM requirements ORDER BY id'))
        if any(link['task_id'] == identifier for link in entry['links'])]
    return result


def acyclic(database, identifier, replacements, entity):
    pending = list(replacements)
    visited = set()
    while pending:
        current = pending.pop()
        if current == identifier:
            raise Fault('replacement_cycle', 409)
        if current in visited:
            continue
        visited.add(current)
        if entity == 'requirement':
            pending.extend(get(database, current)['superseded_by'])
        else:
            linked = task(database, current)
            if linked['retirement']:
                pending.extend(linked['retirement']['replacement_ids'])


def put(database, actor, body, now):
    fields(body, {'request_id', 'version', 'item'})
    item = body['item']
    fields(item, {'id', 'project', 'source', 'summary', 'state', 'superseded_by', 'links', 'review'})
    identifier = ident(item['id'])
    require_intake_manager(database, actor, item['project'])
    source = item['source']
    fields(source, {'system', 'id', 'revision', 'ref', 'sha256'})
    ident(source['system']); ident(source['id'])
    registry.integer(source['revision'], 1, 2**31 - 1)
    safe(source['ref']); hash_value(source['sha256'])
    safe(item['summary'], 2000)
    choice(item['state'], STATES)
    replacements = identifiers(item['superseded_by'])
    if bool(replacements) != (item['state'] == 'superseded'):
        raise Fault('invalid_replacement_links')
    acyclic(database, identifier, replacements, 'requirement')
    seen = set()
    for link in array(item['links']):
        if not isinstance(link, dict):
            raise Fault('invalid_intake_fields')
        fields({key: value for key, value in link.items() if key != 'reporter_actors'}, {'task_id', 'relation', 'target_actors', 'target_label'})
        task(database, link['task_id'])
        choice(link['relation'], {'implements', 'depends_on', 'continues'})
        key = (link['task_id'], link['relation'])
        if key in seen:
            raise Fault('duplicate_intake_link')
        seen.add(key)
        for target in identifiers(link['target_actors']):
            actor_exists(database, target)
        for reporter in identifiers(link.get('reporter_actors', [])):
            actor_exists(database, reporter)
        safe(link['target_label'], 240, empty=True)
    review = item['review']
    fields(review, {'delivered_scope', 'remaining_scope', 'next_deliverable', 'evidence_refs', 'human_input'})
    for field in ('delivered_scope', 'remaining_scope', 'next_deliverable', 'human_input'):
        safe(review[field], 2000, empty=True)
    for reference in array(review['evidence_refs']):
        safe(reference)
    previous_row = database.execute('SELECT * FROM requirements WHERE id=?', (identifier,)).fetchone()
    registry.version(body, previous_row)
    previous = json.loads(previous_row['document']) if previous_row else None
    if previous:
        if previous['publisher_actor'] != actor['id']:
            raise Fault('requirement_publisher_required', 403)
        if (previous['project'], previous['source']['system'], previous['source']['id']) != (item['project'], source['system'], source['id']):
            raise Fault('requirement_origin_immutable', 409)
        if source['revision'] < previous['source']['revision']:
            raise Fault('source_revision_regression', 409)
        if source['revision'] == previous['source']['revision'] and source != previous['source']:
            raise Fault('source_revision_conflict', 409)
    else:
        if database.execute('SELECT 1 FROM requirements WHERE source_system=? AND source_id=?', (source['system'], source['id'])).fetchone():
            raise Fault('requirement_source_exists', 409)
    value = {**item, 'version': body['version'] + 1, 'original_source': previous['original_source'] if previous else source,
             'publisher_actor': actor['id'], 'created': previous['created'] if previous else now,
             'center_received_at': previous['center_received_at'] if previous else now, 'updated': now}
    document = json.dumps(value, ensure_ascii=False, sort_keys=True)
    database.execute('''INSERT INTO requirements VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
      version=excluded.version,document=excluded.document,search_text=excluded.search_text''',
      (identifier, item['project'], source['system'], source['id'], actor['id'], value['version'], document,
       (identifier + ' ' + item['summary'] + ' ' + json.dumps(item['links'], ensure_ascii=False)).casefold()))
    database.execute('INSERT INTO requirement_revisions VALUES(?,?,?)', (identifier, value['version'], document))
    return value


def receipt(database, actor, body, now):
    fields(body, {'request_id', 'receipt'})
    value = body['receipt']
    optional = {'task_version', 'queue_id', 'native_turn_id', 'observation_turn_id', 'body_sha256'}
    if not isinstance(value, dict):
        raise Fault('invalid_intake_fields')
    fields({key: entry for key, entry in value.items() if key not in optional}, {'id', 'requirement_id', 'requirement_version', 'task_id', 'target_actor', 'delivery_id',
                   'thread_ref', 'kind', 'event_id', 'observed_at', 'evidence_ref', 'evidence_sha256', 'detail'})
    for field in ('id', 'task_id', 'target_actor', 'delivery_id', 'event_id'):
        ident(value[field])
    kind = choice(value['kind'], KINDS)
    safe(value['thread_ref'], 240)
    safe(value['evidence_ref']); hash_value(value['evidence_sha256'])
    safe(value['detail'], 1500, empty=True)
    if library.timestamp(value['observed_at']) > now + 120:
        raise Fault('future_intake_observation')
    requirement = get(database, value['requirement_id'], value['requirement_version'])
    if 'task_version' in value:
        registry.integer(value['task_version'], 1, task(database, value['task_id'])['version'])
    for field in ('queue_id', 'native_turn_id', 'observation_turn_id'):
        if field in value:
            ident(value[field])
    if 'body_sha256' in value:
        hash_value(value['body_sha256'])
    if kind == 'dispatched' and not {'task_version', 'body_sha256'} <= set(value):
        raise Fault('intake_dispatch_profile_required')
    if not any(link['task_id'] == value['task_id'] and value['target_actor'] in link['target_actors'] for link in requirement['links']):
        raise Fault('requirement_target_mismatch', 409)
    if kind in ('owner_received', 'progress', 'completed') and actor['id'] != value['target_actor']:
        raise Fault('intake_target_identity_required', 403)
    if kind == 'independent_review' and actor['id'] == value['target_actor']:
        raise Fault('independent_reviewer_required', 403)
    nominated = any(link['task_id'] == value['task_id'] and value['target_actor'] in link['target_actors'] and
                    actor['id'] in link.get('reporter_actors', []) for link in requirement['links'])
    if kind == 'independent_review' or (actor['id'] != value['target_actor'] and not nominated):
        require_intake_manager(database, actor, requirement['project'])
    binding = ('requirement_id', 'requirement_version', 'task_id', 'task_version', 'target_actor', 'thread_ref')
    prior = database.execute('SELECT body FROM requirement_receipts WHERE delivery_id=? LIMIT 1', (value['delivery_id'],)).fetchone()
    if prior and any(json.loads(prior[0]).get(field) != value.get(field) for field in binding):
        raise Fault('intake_delivery_binding_conflict', 409)
    for stored in database.execute('SELECT body FROM requirement_receipts WHERE delivery_id=?', (value['delivery_id'],)):
        previous_value = json.loads(stored[0])
        for field in ('queue_id', 'native_turn_id', 'body_sha256'):
            if field in previous_value and field in value and previous_value[field] != value[field]:
                raise Fault('intake_native_binding_conflict', 409)
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True)
    old = database.execute('SELECT * FROM requirement_receipts WHERE id=? OR (delivery_id=? AND kind=? AND event_id=?)',
                           (value['id'], value['delivery_id'], kind, value['event_id'])).fetchall()
    if old:
        previous = json.loads(old[0]['body'])
        same = {key: entry for key, entry in previous.items() if key != 'id'} == {key: entry for key, entry in value.items() if key != 'id'}
        if len(old) != 1 or not same or old[0]['reporter_actor'] != actor['id']:
            raise Fault('intake_receipt_conflict', 409)
        return json.loads(old[0]['document'])
    result = {**value, 'reporter_actor': actor['id'], 'recorded_at': now}
    cursor = database.execute('''INSERT INTO requirement_receipts
      (id,requirement_id,requirement_version,delivery_id,kind,event_id,reporter_actor,body,document)
      VALUES(?,?,?,?,?,?,?,?,?)''', (value['id'], requirement['id'], requirement['version'], value['delivery_id'],
                                  kind, value['event_id'], actor['id'], canonical, '{}'))
    result['seq'] = cursor.lastrowid
    database.execute('UPDATE requirement_receipts SET document=? WHERE seq=?',
                     (json.dumps(result, ensure_ascii=False, sort_keys=True), result['seq']))
    return result


def retire(database, actor, body, now):
    fields(body, {'request_id', 'id', 'version', 'disposition', 'replacement_ids', 'reason', 'evidence_ref', 'evidence_sha256'})
    previous = task(database, body['id'])
    registry.require_projects(database, actor, [previous['project']])
    registry.version(body, previous)
    if previous['status'] in ('delivered', 'accepted', 'cancelled', 'superseded'):
        raise Fault('task_not_retirable', 409)
    if previous['lease_until'] > now and previous['owner'] != actor['id']:
        raise Fault('lease_held', 409)
    disposition = choice(body['disposition'], {'cancelled', 'superseded'})
    replacements = identifiers(body['replacement_ids'])
    if bool(replacements) != (disposition == 'superseded'):
        raise Fault('invalid_replacement_links')
    acyclic(database, previous['id'], replacements, 'task')
    safe(body['reason'], 1500); safe(body['evidence_ref']); hash_value(body['evidence_sha256'])
    record = {field: body[field] for field in ('disposition', 'replacement_ids', 'reason', 'evidence_ref', 'evidence_sha256')}
    record.update(recorded_by=actor['id'], recorded_at=now, previous_status=previous['status'], previous_version=previous['version'])
    database.execute('INSERT INTO task_retirements VALUES(?,?)', (previous['id'], json.dumps(record, ensure_ascii=False)))
    database.execute('UPDATE tasks SET status=?,lease_until=0,version=version+1,updated=? WHERE id=?', (disposition, now, previous['id']))
    return task(database, previous['id'])


def retract(database, actor, body, now):
    fields(body, {'request_id', 'receipt_id', 'reason', 'evidence_ref', 'evidence_sha256'})
    identifier = ident(body['receipt_id'])
    original = database.execute('SELECT * FROM requirement_receipts WHERE id=?', (identifier,)).fetchone()
    if not original:
        raise Fault('intake_receipt_missing', 404)
    if original['reporter_actor'] != actor['id']:
        raise Fault('original_reporter_required', 403)
    safe(body['reason'], 1500); safe(body['evidence_ref']); hash_value(body['evidence_sha256'])
    correction = {key: body[key] for key in ('reason', 'evidence_ref', 'evidence_sha256')}
    previous = database.execute('SELECT document FROM requirement_receipt_retractions WHERE receipt_id=?', (identifier,)).fetchone()
    if previous:
        recorded = json.loads(previous[0])
        if any(recorded[key] != value for key, value in correction.items()):
            raise Fault('intake_retraction_conflict', 409)
    else:
        recorded = {**correction, 'reporter_actor': actor['id'], 'recorded_at': now}
        database.execute('INSERT INTO requirement_receipt_retractions VALUES(?,?)', (identifier, json.dumps(recorded, ensure_ascii=False)))
    return {'id': identifier, 'requirement_id': original['requirement_id'], 'retraction': recorded}


def receipt_public(database, document):
    value = json.loads(document)
    retracted = database.execute('SELECT document FROM requirement_receipt_retractions WHERE receipt_id=?', (value['id'],)).fetchone()
    if retracted:
        value['retraction'] = json.loads(retracted[0])
    return value


def write(database, actor, action, body, now):
    result = {'requirement-put': put, 'requirement-receipt': receipt, 'requirement-receipt-retract': retract, 'task-retire': retire}[action](database, actor, body, now)
    database.execute('INSERT INTO intake_writes VALUES(?,?,?,?,?)',
                     (actor['id'], body['request_id'], action, result.get('requirement_id', result['id']), now))
    return result


def metadata(database, now):
    return {'stream_epoch': database.execute("SELECT value FROM collaboration_meta WHERE key='stream_epoch'").fetchone()[0],
            'cursor': database.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0], 'server_time': now}


def query_fields(query, allowed):
    if set(query) - set(allowed) or any(len(values) != 1 for values in query.values()):
        raise Fault('invalid_intake_query')


def number(query, field, default, minimum=0, maximum=2**63 - 1):
    try:
        return registry.integer(int(query.get(field, [str(default)])[0]), minimum, maximum)
    except ValueError:
        raise Fault('invalid_cursor') from None


def read(database, actor, path, query, now):
    meta = metadata(database, now)
    if path == '/v1/intake-receipt':
        query_fields(query, {'request_id'})
        identifier = ident(query.get('request_id', [''])[0])
        row = database.execute('''SELECT w.*,r.digest,r.response FROM intake_writes w JOIN requests r
          ON w.actor=r.actor AND w.request_id=r.request_id WHERE w.actor=? AND w.request_id=?''', (actor['id'], identifier)).fetchone()
        if not row:
            raise Fault('intake_receipt_missing', 404)
        return {'recorded': True, 'request_id': identifier, 'action': row['action'], 'object_id': row['object_id'],
                'digest': row['digest'], 'response': json.loads(row['response']), 'recorded_at': row['created']}
    if path == '/v1/intake-events':
        query_fields(query, {'after', 'stream_epoch'})
        after = number(query, 'after', 0)
        if query.get('stream_epoch', [''])[0] != meta['stream_epoch'] or after > meta['cursor']:
            raise Fault('intake_snapshot_required', 409)
        rows = database.execute("SELECT seq,action,object_id,created,topic FROM events WHERE seq>? AND topic IN ('requirement','requirement_receipt','task') ORDER BY seq LIMIT 101", (after,)).fetchall()
        return {**meta, 'events': [dict(row) for row in rows[:100]], 'next_cursor': rows[99]['seq'] if len(rows) > 100 else meta['cursor'],
                'has_more': len(rows) > 100, 'semantics': 'invalidate_then_read'}
    if path == '/v1/task':
        query_fields(query, {'id'})
        return task(database, query.get('id', [''])[0])
    if path == '/v1/requirement':
        query_fields(query, {'id', 'history', 'before_version'})
        value = get(database, query.get('id', [''])[0])
        value['tasks'] = [task(database, identifier) for identifier in dict.fromkeys(link['task_id'] for link in value['links'])]
        if query.get('history', ['0'])[0] == '1':
            before = number(query, 'before_version', value['version'] + 1, 1)
            revisions = database.execute('SELECT version,document FROM requirement_revisions WHERE id=? AND version<? ORDER BY version DESC LIMIT 21', (value['id'], before)).fetchall()
            value.update(revisions=[json.loads(row['document']) for row in revisions[:20]], revisions_has_more=len(revisions) > 20,
                         next_before_version=revisions[min(19, len(revisions) - 1)]['version'] if revisions else before)
        return value
    if path == '/v1/requirement-receipts':
        query_fields(query, {'id', 'after', 'limit'})
        value = get(database, query.get('id', [''])[0])
        after = number(query, 'after', 0)
        limit = number(query, 'limit', 20, 1, 100)
        rows = database.execute('SELECT seq,document FROM requirement_receipts WHERE requirement_id=? AND seq>? ORDER BY seq LIMIT ?', (value['id'], after, limit + 1)).fetchall()
        return {'items': [receipt_public(database, row['document']) for row in rows[:limit]], 'has_more': len(rows) > limit,
                'next_cursor': rows[min(limit - 1, len(rows) - 1)]['seq'] if rows else after}
    query_fields(query, {'limit', 'after', 'q', 'project', 'state'} if path == '/v1/requirements' else {'limit', 'after', 'status', 'project'})
    limit = number(query, 'limit', 20, 1, 100)
    after = query.get('after', [''])[0]
    if after:
        ident(after)
    clauses, values = ['id>?'], [after]
    if 'project' in query:
        clauses.append('project=?'); values.append(ident(query['project'][0]))
    if path == '/v1/requirements':
        clauses.append('instr(search_text,?)>0'); values.append(safe(query.get('q', [''])[0], 120, empty=True).casefold())
        if 'state' in query:
            clauses.append("json_extract(document,'$.state')=?"); values.append(choice(query['state'][0], STATES))
        rows = database.execute('SELECT document FROM requirements WHERE ' + ' AND '.join(clauses) + ' ORDER BY id LIMIT ?', (*values, limit + 1)).fetchall()
        items = [json.loads(row[0]) for row in rows[:limit]]
    else:
        if 'status' in query:
            clauses.append('status=?'); values.append(choice(query['status'][0], TASK_STATES))
        rows = database.execute('SELECT id FROM tasks WHERE ' + ' AND '.join(clauses) + ' ORDER BY id LIMIT ?', (*values, limit + 1)).fetchall()
        items = [task(database, row[0]) for row in rows[:limit]]
    return {**meta, 'items': items, 'next_cursor': items[-1]['id'] if items else after, 'has_more': len(rows) > limit}
