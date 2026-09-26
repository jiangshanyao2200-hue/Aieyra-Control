"""Cached observation of original ProductHost receive passes; no task executor."""
import copy
from datetime import datetime, timezone
import json
import threading


def now():
    return datetime.now(timezone.utc).isoformat()


class ProductJobs:
    def __init__(self, store, products):
        self.store, self.products = store, products
        self.lock = threading.Lock()
        self.checked = set()

    def poll(self):
        for source in list(self.products.sources.values()):
            if not source.get('product_host_ref'):
                continue
            key = 'product_jobs:' + source['id']
            with self.store.db() as db:
                saved = db.execute('SELECT payload FROM cache WHERE id=?', (key,)).fetchone()
            previous = json.loads(saved[0]) if saved else {'source_id': source['id'], 'items': []}
            value = copy.deepcopy(previous)
            value['checked_at'] = now()
            try:
                result = self.products.product_job_sync(source['id'])
                if not isinstance(result, dict) or not isinstance(result.get('items'), list) or not isinstance(result.get('errors'), list):
                    raise ValueError('invalid_product_job_sync')
                items = {x['job_id']: x for x in value['items']}
                for item in result['items']:
                    items[item['job_id']] = dict(item, observed_at=now())
                value.update(items=list(items.values())[-100:], available=True, stale=bool(result['errors']),
                             errors=result['errors'], observed_at=now(), has_more=result.get('has_more', False))
            except Exception:
                value.update(available=False, stale=True, errors=[{'error': 'product_job_receiver_unavailable'}])
            with self.store.db() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute('INSERT OR REPLACE INTO cache VALUES(?,?)', (key, json.dumps(value)))
                # Only changed facts notify, not each idle sampling timestamp.
                facts = lambda v: {k: [{a: b for a, b in x.items() if a != 'observed_at'} for x in v.get(k, [])]
                                   if k == 'items' else v.get(k) for k in ('items', 'available', 'stale', 'errors')}
                if facts(previous) != facts(value):
                    self.store.event(db, 'product_job.changed', source['id'])
            with self.lock:
                self.checked.add(source['id'])

    def snapshot(self):
        rows = []
        with self.lock:
            for source in self.products.sources.values():
                if not source.get('product_host_ref'):
                    continue
                with self.store.db() as db:
                    saved = db.execute('SELECT payload FROM cache WHERE id=?', ('product_jobs:' + source['id'],)).fetchone()
                value = json.loads(saved[0]) if saved else {'source_id': source['id'], 'items': [], 'available': False, 'stale': True}
                try:
                    fresh = (datetime.now(timezone.utc)-datetime.fromisoformat(value['observed_at'])).total_seconds() < 120
                except (ValueError, KeyError, TypeError):
                    fresh = False
                value['stale'] = value.get('stale', True) or source['id'] not in self.checked or not fresh
                for item in value['items']:
                    try:
                        item_fresh = (datetime.now(timezone.utc)-datetime.fromisoformat(item['observed_at'])).total_seconds() < 120
                    except (ValueError, KeyError, TypeError):
                        item_fresh = False
                    item['stale'] = value['stale'] or not item_fresh
                rows.append(value)
        return dict(schema_version=1, source='original_center_product_host', sources=rows,
                    available=any(x['available'] and not x['stale'] for x in rows),
                    stale=not rows or any(x['stale'] for x in rows),
                    reason=None if rows else 'product_host_not_configured',
                    limitations=['center_cancel_support_is_separate', 'at_most_one_new_intent_per_pass', 'last_100_jobs_per_source'])
