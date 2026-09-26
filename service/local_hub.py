"""Local transport for the pinned coordination engine. No sockets or cloud calls."""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from urllib.parse import urlsplit, parse_qs
from coordination_core import hub as engine
from coordination_core.core import Fault


class LocalClient:
    def __init__(self, data_dir, projects):
        self.store = engine.Store(data_dir / 'office.sqlite')
        try:
            self.store.init(secrets.token_urlsafe(32))
        except Fault as error:
            if error.code != 'already_initialized':
                raise
        with self.store.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS local_identities(alias TEXT PRIMARY KEY,actor_id TEXT NOT NULL REFERENCES actors(id))')
            db.execute("INSERT OR IGNORE INTO local_identities VALUES('owner','owner')")
            db.execute('CREATE INDEX IF NOT EXISTS control_message_created ON messages(created,seq)')
        for project in [{'id':'coordination','name':'本地群聊','root':'','source':''}, *projects]:
            with self.store.db() as db:
                old = db.execute('SELECT version FROM registered_projects WHERE id=?', (project['id'],)).fetchone()
            if old:
                continue
            self.call('owner', '/v1/project-register', {**project, 'version':0,
                'request_id':'local-project-'+project['id']})

    def load(self, identity):
        with self.store.db() as db:
            row = db.execute('SELECT actor_id FROM local_identities WHERE alias=?', (identity,)).fetchone()
        if not row:
            raise FileNotFoundError(identity)
        return {'id': row['actor_id']}

    def save(self, identity, result):
        with self.store.db() as db:
            db.execute('INSERT INTO local_identities VALUES(?,?)', (identity, result['id']))

    def call(self, identity, path, body=None):
        try:
            aid = self.load(identity)['id']
            with self.store.db() as db:
                actor = db.execute('SELECT id,name,role,project FROM actors WHERE id=? AND revoked=0', (aid,)).fetchone()
            if not actor:
                raise Fault('unauthorized',401)
            parsed = urlsplit(path)
            return (self.store.read(dict(actor), parsed.path, parse_qs(parsed.query, keep_blank_values=True))
                    if body is None else self.store.write(dict(actor), parsed.path.removeprefix('/v1/'), body))
        except Fault as error:
            raise ValueError(f'{error.status}:{error.code}') from None

    def start(self):
        return {'local':True}

    def chat_history(self, before=None):
        end=time.time();start=end-86400
        # Display window only: old messages remain in the local office database.
        with self.store.db() as db:
            actor=dict(db.execute("SELECT id,name,role,project FROM actors WHERE id='owner' AND revoked=0").fetchone())
            rows=self.store.messages(db,actor,'m.created>=? AND m.created<=? AND m.seq<?',
                (start,end,before or 9223372036854775807),True,101)
        return {'messages':list(reversed(rows[:100])), 'has_more':len(rows)>100,
                'next_before':rows[99]['seq'] if len(rows)>100 else None,
                'window_start':start,'window_end':end,'retention_hours':24}


def create_local_hub(config, data_dir, hub_type):
    value = hub_type.__new__(hub_type)
    value.config = {**config, 'hub_read_identity':'owner', 'hub_user_identity':'owner',
                    'hub_management_identity':'owner','hub_registry_identity':'owner','hub_operations_identity':'owner'}
    value.client = LocalClient(data_dir, config.get('local_projects', []))
    value.is_local = True
    return value
