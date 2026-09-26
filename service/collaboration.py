"""显式OS工位的只读协作投影；复用平台身份校验，不建立执行队列。"""
from __future__ import annotations

import copy
import datetime as dt
import importlib.util
import json
from pathlib import Path
import re
import threading
from os_sessions import bridge_config

ID = re.compile(r'^[A-Za-z0-9_.:-]{1,100}$')


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def fields(value, names):
    if not isinstance(value, dict):
        raise ValueError('invalid_product_shape')
    return {key: copy.deepcopy(value[key]) for key in names if key in value}


def artifact_rows(delivery):
    if not isinstance(delivery, dict):
        return []
    return [{**fields(item, ('path', 'sha256', 'bytes', 'error')), 'generation': delivery.get('generation'),
             'captured_at': delivery.get('captured_at')} for item in (delivery.get('artifacts') or [])[:32]]


def review_rows(reviews):
    rows = []
    for review in (reviews or [])[-16:]:
        row = fields(review, ('request_id', 'generation', 'verdict', 'created_at', 'evidence'))
        row['checks'] = []
        for check in (review.get('checks') or [])[:24]:
            entry = fields(check, ('criterion', 'verdict', 'evidence'))
            entry['receipts'] = [fields(receipt, ('agent_id', 'session_id', 'expected_exit'))
                                 for receipt in (check.get('receipts') or [])[:32]]
            row['checks'].append(entry)
        rows.append(row)
    return rows


def usage_fields(usage):
    usage = usage or {}
    row = fields(usage, ('prompt_tokens', 'completion_tokens', 'total_tokens', 'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens'))
    for key, name in (('prompt_tokens_details', 'cached_tokens'), ('completion_tokens_details', 'reasoning_tokens')):
        if isinstance(usage.get(key), dict):
            row[key] = fields(usage[key], (name,))
    return row


def runtime_capabilities(client):
    config = getattr(client, 'config', {})
    description = getattr(client, 'description', {})
    actions = description.get('actions', [])
    writes = client.mode == 'fixture' or config.get('enable_os_writes') is True
    models, tools = config.get('allowed_models'), config.get('allowed_tools')
    policy = (writes and isinstance(models, list) and bool(models)
                and all(isinstance(x, str) and x for x in models)
                and isinstance(tools, list) and all(isinstance(x, str) and x for x in tools))
    return {'observe': True, 'dispatch': policy and 'agents.create' in actions,
            'matrix_send': policy and config.get('enable_matrix_writes') is True and 'matrix.send' in actions,
            'matrix_cancel': policy and config.get('enable_matrix_writes') is True and 'matrix.cancel' in actions,
            'agent_send': policy and description.get('generation_guarded_commands') is True and 'agents.send' in actions,
            'agent_cancel': policy and description.get('generation_guarded_commands') is True and 'agents.cancel' in actions,
            'human_callback': False}


def without_observation_time(value):
    if isinstance(value, dict):
        return {k: without_observation_time(v) for k, v in value.items() if k not in ('observed_at', 'checked_at')}
    if isinstance(value, list):
        return [without_observation_time(v) for v in value]
    return value


def expired(stamp, seconds=30):
    try:
        return (dt.datetime.now(dt.timezone.utc)-dt.datetime.fromisoformat(stamp.replace('Z', '+00:00'))).total_seconds() > seconds
    except (AttributeError, TypeError, ValueError):
        return True


class Projection:
    def __init__(self, store, config, client_factory=None, source_provider=None):
        self.store, self.lock = store, threading.RLock()
        self.checked = set()
        self.static_sources = config.get('collaboration_sources', [])
        self.source_provider = source_provider
        if not isinstance(self.sources, list) or len(self.sources) > 16:
            raise ValueError('invalid_collaboration_sources')
        seen = set()
        for source in self.sources:
            if not isinstance(source, dict) or not isinstance(source.get('id'), str) or not ID.fullmatch(source['id']) or source['id'] in seen:
                raise ValueError('invalid_collaboration_source_id')
            if not all(isinstance(source.get(k), str) and Path(source[k]).is_absolute() for k in ('adapter_dir', 'config_ref')):
                raise ValueError('explicit_collaboration_paths_required')
            seen.add(source['id'])
        self.factory = client_factory or self.load_client

    @property
    def sources(self):
        return self.source_provider() if self.source_provider else self.static_sources

    @staticmethod
    def load_client(source):
        # 路径来自本机显式配置，HTTP客户端不能注入模块或发现文件路径。
        module_file = Path(source['adapter_dir']) / 'matrix_client.py'
        spec = importlib.util.spec_from_file_location('control_product_matrix_client', module_file)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        config = bridge_config(source)
        config['timeout_seconds'] = min(3, config.get('timeout_seconds', 3))
        return module.MatrixClient(config)

    def cached(self, key):
        with self.store.db() as db:
            row = db.execute('SELECT payload FROM cache WHERE id=?', (key,)).fetchone()
            return json.loads(row[0]) if row else None

    def save(self, key, value, kind):
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True)
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT payload FROM cache WHERE id=?', (key,)).fetchone()
            previous = json.loads(old[0]) if old else {}
            # 采样时间更新不制造假进度事件，数据变化才推动视图刷新。
            changed = without_observation_time(previous) != without_observation_time(value)
            db.execute('INSERT OR REPLACE INTO cache VALUES(?,?)', (key, raw))
            if changed:
                self.store.event(db, kind, key)

    def poll(self):
        with self.lock:
            for source in self.sources:
                key = 'collaboration:' + source['id']
                try:
                    client = self.factory(source)
                    matrix = client.rpc('matrix.status')
                    if not isinstance(matrix, dict) or type(matrix.get('busy')) is not bool or not isinstance(matrix.get('requests'), list):
                        raise ValueError('invalid_matrix_status')
                    listing = client.rpc('agents.list')
                    if not isinstance(listing, list):
                        raise ValueError('invalid_agent_list')
                    stamp = now()
                    capabilities = runtime_capabilities(client)
                    matrix_id = source['id'] + ':' + str(client.binding['session_id'])
                    rows = []
                    # 限制一次读取，完整历史仍由原OS分页接口持有。
                    ordered = sorted(listing, key=lambda x: (x.get('state') in ('queued', 'running', 'cancelling', 'waiting_user'), x.get('updated', ''), x.get('id', '')), reverse=True)
                    for summary in ordered[:16]:
                        aid = summary.get('id')
                        if not isinstance(aid, str) or not ID.fullmatch(aid):
                            raise ValueError('invalid_agent_id')
                        record = client.rpc('agents.status', {'id': aid})
                        if record.get('id') != aid:
                            raise ValueError('agent_identity_mismatch')
                        row = fields(summary, ('id', 'parent', 'depth', 'title', 'model_id', 'created', 'updated'))
                        row.update(fields(record, ('state', 'acceptance', 'generation', 'requests', 'tool_turns', 'error', 'origin_request')))
                        row.update(id=matrix_id + ':' + aid, agent_id=aid, matrix_id=matrix_id, project=source.get('project', 'os'), observed_at=stamp)
                        row['runtime_station_id'] = row['id']
                        row['task_id'] = row['id'] + ':g' + str(record.get('generation'))
                        parent = record.get('parent', summary.get('parent'))
                        row['parent_id'] = matrix_id if parent == client.binding['session_id'] else matrix_id + ':' + str(parent)
                        spec = record.get('spec') or {}
                        row['assigned_tools'] = [x for x in (spec.get('tools') or [])[:32] if isinstance(x, str)]
                        row['budget'] = fields(spec, ('max_requests', 'timeout_seconds', 'max_tokens'))
                        row['command_target'] = {'source_id': source['id'], 'agent_id': aid, 'generation': record.get('generation')}
                        active = record.get('state') in ('queued', 'running', 'cancelling', 'waiting_user')
                        row['capabilities'] = {
                            'send': capabilities['agent_send'] and not active and type(record.get('generation')) is int
                                    and 1 <= record['generation'] < 16 and spec.get('model_id') in client.config.get('allowed_models', [])
                                    and all(tool in client.config.get('allowed_tools', []) for tool in (spec.get('tools') or [])),
                            'cancel': capabilities['agent_cancel'] and active}
                        contract = spec.get('contract') or {}
                        row['acceptance_criteria'] = [x for x in (contract.get('acceptance') or [])[:24] if isinstance(x, str)]
                        delivery = record.get('delivery')
                        row['artifacts'] = artifact_rows(delivery)
                        row['reviews'] = review_rows(record.get('reviews'))
                        row['history'] = [{**fields(x, ('generation', 'state', 'acceptance')), 'task_id': row['id'] + ':g' + str(x.get('generation')),
                                           'artifacts': artifact_rows(x.get('delivery'))} for x in (record.get('history') or [])[-16:]]
                        activity = client.rpc('agents.activity', {'id': aid, 'limit': 50})
                        row['activity'] = {**fields(activity, ('cursor', 'has_more', 'has_older', 'total', 'page', 'archived_count')), 'items': []}
                        for item in activity.get('items', [])[:50]:
                            row['activity']['items'].append(fields(item, ('id', 'sequence', 'revision', 'parent', 'tool', 'action', 'title', 'targets', 'state', 'detail', 'command', 'session_id', 'cell_id', 'exit_code', 'hidden', 'patch')))
                        rows.append(row)
                    goals = []
                    for exchange in sorted(matrix['requests'], key=lambda x: (x.get('started_at', ''), x.get('request_id', '')))[:64]:
                        rid = exchange.get('request_id')
                        if not isinstance(rid, str) or not rid or len(rid) > 128:
                            raise ValueError('invalid_matrix_request')
                        goals.append({**fields(exchange, ('request_id', 'state', 'model', 'started_at', 'updated_at')),
                                      'usage': usage_fields(exchange.get('usage')),
                                      'id': matrix_id + ':request:' + rid, 'matrix_id': matrix_id,
                                      'runtime_station_ids': [row['id'] for row in rows if row.get('origin_request') == rid]})
                    value = {'id': source['id'], 'matrix_id': matrix_id, 'project': source.get('project', 'os'), 'name': source.get('name', source['id']), 'session_id': client.binding['session_id'], 'source': 'os_runtime_rpc', 'available': True, 'stale': False, 'observed_at': stamp, 'checked_at': stamp, 'tasks': rows, 'has_more_agents': len(listing) > 16, 'total_agents': len(listing), 'fixture': client.mode == 'fixture',
                             'busy': matrix['busy'], **fields(matrix, ('phase', 'model')), 'goals': goals, 'capabilities': capabilities}
                except Exception:
                    value = self.cached(key) or {'id': source['id'], 'project': source.get('project', 'os'), 'name': source.get('name', source['id']), 'tasks': [], 'source': 'os_runtime_rpc', 'observed_at': None}
                    value.update(available=False, stale=True, checked_at=now(), error='product_runtime_unavailable')
                self.save(key, value, 'collaboration.changed')
                self.checked.add(source['id'])

    def snapshot(self):
        matrices, tasks, stations, goals = [], [], [], []
        for source in self.sources:
            value = self.cached('collaboration:' + source['id']) or {'id': source['id'], 'name': source.get('name', source['id']), 'project': source.get('project', 'os'), 'tasks': [], 'available': False, 'stale': True, 'observed_at': None, 'source': 'os_runtime_rpc'}
            value['stale'] = source['id'] not in self.checked or value.get('stale', True) or expired(value.get('observed_at'))
            for item in value.pop('tasks', []):
                item['stale'] = value['stale']
                if value['stale']:
                    item['capabilities'] = {'send': False, 'cancel': False}
                tasks.append(item)
                state = item.get('state')
                stations.append({**fields(item, ('id', 'agent_id', 'matrix_id', 'parent_id', 'title', 'model_id', 'assigned_tools', 'project', 'observed_at', 'stale', 'command_target', 'capabilities')),
                                 'execution_state': state, 'active': state in ('queued', 'running', 'cancelling', 'waiting_user') if not value['stale'] else None,
                                 'current_task_id': item.get('task_id'), 'identity_source': 'os_runtime_agent_not_center_seat'})
            for goal in value.pop('goals', []):
                goal['stale'] = value['stale']
                goals.append(goal)
            if value['stale']:
                value['capabilities'] = {k: False for k in value.get('capabilities', {'observe': False})}
            matrices.append(value)
        caps = {name: any(x.get('capabilities', {}).get(name) for x in matrices) for name in ('observe', 'dispatch', 'matrix_send', 'matrix_cancel', 'agent_send', 'agent_cancel', 'human_callback')}
        return {'schema_version': 1, 'source': 'explicit_os_sessions', 'observed_at': now(), 'matrices': matrices, 'tasks': tasks, 'runtime_stations': stations, 'goals': goals,
                'available': any(x.get('available') and not x['stale'] for x in matrices), 'stale': any(x['stale'] for x in matrices) or not matrices,
                'capabilities': caps, 'limitations': ['maximum_16_agents_per_matrix_latest_50_activity_items', 'execution_completed_is_not_review_accepted', 'runtime_agent_identity_is_not_cloud_seat_registration']}

    @staticmethod
    def human_requests():
        # 中心尚未发布权威契约时明确缺口，不能从旧blocked任务合成人工事项。
        return {'schema_version': 1, 'items': [], 'observed_at': None, 'checked_at': now(), 'stale': True, 'available': False, 'source': 'coordination', 'reason': 'human_contract_not_connected'}
