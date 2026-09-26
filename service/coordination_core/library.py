"""Shared engineering metadata and revisions; file bodies and credentials stay local."""
from __future__ import annotations

import json
import math
import re
from pathlib import PurePosixPath,PureWindowsPath
from datetime import datetime

from .core import Fault, ident, text
from . import registry

ACTIONS = {'library-put'}
READS = {'/v1/library', '/v1/library-item'}
FIELDS = {'id', 'title', 'project', 'owner', 'kind', 'summary', 'purpose', 'tags', 'source_ref',
          'provenance', 'observed_at', 'content_version', 'sha256', 'bytes', 'status', 'access', 'expires_at', 'task_id'}
KINDS = {'document', 'directory', 'receipt', 'resource', 'artifact'}
SHORT = ('id', 'title', 'kind', 'project', 'owner', 'status', 'access', 'content_version', 'observed_at', 'expires_at', 'version')
SECRET_METADATA = re.compile(r'(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|password|secret|token)["\']?\s*[:=：]\s*["\']?[^\s"\']{6,}', re.I)


def migrate(database):
    database.executescript('''
    CREATE TABLE IF NOT EXISTS library_items(
      id TEXT PRIMARY KEY,project TEXT NOT NULL REFERENCES registered_projects(id),publisher_actor TEXT NOT NULL,
      version INTEGER NOT NULL,document TEXT NOT NULL,search_text TEXT NOT NULL,status TEXT NOT NULL,
      expires_at REAL,updated REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS library_revisions(
      id TEXT NOT NULL REFERENCES library_items(id),version INTEGER NOT NULL,document TEXT NOT NULL,
      publisher_actor TEXT NOT NULL,updated REAL NOT NULL,PRIMARY KEY(id,version));
    ''')


def timestamp(value):
    if not isinstance(value, str) or len(value) > 50:
        raise Fault('invalid_library_timestamp')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError()
        result = parsed.timestamp()
    except (ValueError, OverflowError):
        raise Fault('invalid_library_timestamp') from None
    if not math.isfinite(result) or result < 0:
        raise Fault('invalid_library_timestamp')
    return result


def validate(item, now):
    if not isinstance(item, dict) or set(item) != FIELDS:
        raise Fault('invalid_library_fields')
    if any(SECRET_METADATA.search(value) for value in item.values() if isinstance(value, str)):
        raise Fault('possible_secret_use_reference_instead')
    for field in ('id', 'project', 'owner', 'task_id'):
        ident(item[field])
    for field, maximum in (('title', 160), ('summary', 1000), ('purpose', 1000), ('source_ref', 500),
                           ('provenance', 600), ('content_version', 100)):
        if text(item[field], maximum) != item[field]:
            raise Fault('invalid_library_text')
    if item['kind'] not in tuple(KINDS) or item['status'] not in ('active', 'unverified', 'withdrawn', 'expired'):
        raise Fault('invalid_library_state')
    tags = item['tags']
    if not isinstance(tags, list) or len(tags) > 12 or any(text(tag, 40) != tag for tag in tags):
        raise Fault('invalid_library_tags')
    if timestamp(item['observed_at']) > now + 120:
        raise Fault('future_library_observation')
    expiry = timestamp(item['expires_at']) if item['expires_at'] is not None else None
    if item['access'] == 'protected_reference':
        if not re.fullmatch(r'vault:[a-zA-Z0-9_.-]{1,80}', item['source_ref']) or item['sha256'] is not None or item['bytes'] is not None:
            raise Fault('protected_reference_only')
    elif item['access'] == 'shared':
        source = item['source_ref']
        # References are metadata only; never relocate or read project contents here.
        normalized=source.replace('\\','/')
        if not (PurePosixPath(normalized).is_absolute() or PureWindowsPath(normalized).is_absolute()) or any(part in ('.','..') for part in normalized.split('/')):
            raise Fault('invalid_shared_source_reference')
        if item['kind'] == 'directory':
            if item['sha256'] is not None or item['bytes'] is not None:
                raise Fault('directory_reference_only')
        else:
            if not isinstance(item['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', item['sha256']):
                raise Fault('invalid_library_hash')
            registry.integer(item['bytes'], 0, 2**53 - 1)
    else:
        raise Fault('invalid_library_access')
    return expiry


def public(document, now):
    item = json.loads(document)
    item['content_available_in_center'] = False
    if item['expires_at'] is not None and timestamp(item['expires_at']) <= now:
        item['status'] = 'expired'
    return item


def write(database, actor, action, body, now):
    if action not in ACTIONS or set(body) != {'request_id', 'version', 'item'}:
        raise Fault('invalid_library_fields')
    item = body['item']
    expiry = validate(item, now)
    registry.require_projects(database, actor, [item['project']])
    previous = database.execute('SELECT * FROM library_items WHERE id=?', (item['id'],)).fetchone()
    if previous and previous['publisher_actor'] != actor['id']:
        raise Fault('library_publisher_required', 403)
    registry.version(body, previous)
    if previous and previous['project'] != item['project']:
        raise Fault('library_project_immutable', 409)
    revision = body['version'] + 1
    value = {**item, 'version': revision, 'supersedes': revision - 1 if previous else None}
    document = json.dumps(value, ensure_ascii=False, sort_keys=True)
    search = ' '.join(str(item[field]) for field in ('id', 'title', 'summary', 'purpose', 'owner', 'tags', 'provenance')).casefold()
    database.execute('''INSERT INTO library_items VALUES(?,?,?,?,?,?,?,?,?)
      ON CONFLICT(id) DO UPDATE SET version=excluded.version,document=excluded.document,
      search_text=excluded.search_text,status=excluded.status,expires_at=excluded.expires_at,updated=excluded.updated''',
      (item['id'], item['project'], actor['id'], revision, document, search, item['status'], expiry, now))
    database.execute('INSERT INTO library_revisions VALUES(?,?,?,?,?)', (item['id'], revision, document, actor['id'], now))
    return public(document, now)


def read(database, actor, path, query, now):
    if path == '/v1/library-item':
        identifier = ident(query.get('id', [''])[0])
        result = database.execute('SELECT * FROM library_items WHERE id=?', (identifier,)).fetchone()
        if not result:
            raise Fault('library_item_missing', 404)
        value = public(result['document'], now)
        if query.get('history', ['0'])[0] == '1':
            try:
                before = int(query.get('before_version', [str(result['version'] + 1)])[0])
            except ValueError:
                raise Fault('invalid_cursor') from None
            revisions = database.execute('SELECT version,document FROM library_revisions WHERE id=? AND version<? ORDER BY version DESC LIMIT 21', (identifier, before)).fetchall()
            value['revisions'] = [json.loads(row['document']) for row in revisions[:20]]
            value['has_more'] = len(revisions) > 20
            value['next_before_version'] = revisions[min(19, len(revisions) - 1)]['version'] if revisions else before
        return value
    if path != '/v1/library':
        raise Fault('not_found', 404)
    try:
        limit = int(query.get('limit', ['20'])[0])
    except ValueError:
        raise Fault('invalid_limit') from None
    registry.integer(limit, 1, 100)
    after = query.get('after', [''])[0]
    if after:
        ident(after)
    search = text(query.get('q', [''])[0], 120, empty=True).casefold()
    clauses = ['id>?', 'instr(search_text,?)>0']
    values = [after, search]
    if query.get('include_inactive', ['0'])[0] != '1':
        clauses.extend(["status NOT IN ('withdrawn','expired')", '(expires_at IS NULL OR expires_at>?)'])
        values.append(now)
    if 'project' in query:
        clauses.append('project=?')
        values.append(ident(query['project'][0]))
    rows = database.execute('SELECT document FROM library_items WHERE ' + ' AND '.join(clauses) + ' ORDER BY id LIMIT ?', (*values, limit + 1)).fetchall()
    items = [public(row['document'], now) for row in rows[:limit]]
    return {'items': [{field: item[field] for field in SHORT} for item in items],
            'next_cursor': items[-1]['id'] if items else after, 'has_more': len(rows) > limit}
