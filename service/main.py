"""本机协作客户端网关：中心是共享事实来源，本地保存资源与投递收据。"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import importlib
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from catalog import collect
from monitor import collect_monitors
from evidence import read_brief
from discovery import discover
from collaboration import Projection
from product_commands import ProductCommands, ProductCommandError
from human_requests import HumanRequests, HumanError
from os_sessions import OSSessions
from product_jobs import ProductJobs
from request_intake import RequestIntake, IntakeError, ROUTES as INTAKE_READS
from requirement_delivery import RequirementDeliveries, RequirementDeliveryError, canonical
from agent_access import AgentAccess, AgentError, READS as AGENT_READS, WRITES as AGENT_WRITES
from agent_protocol import openapi as agent_openapi
from project_memory import ProjectMemory, MemoryError
from cloud_link import CloudLink, CloudError
from cloud_session import SessionVault
from local_hub import create_local_hub
from paths import shared_directory, configuration_file, initialize as initialize_paths

VERSION = "0.6.0"
REGISTRY_ACTIONS = frozenset({"project-register", "governance-grant", "host-register", "adapter-register", "seat-create", "seat-update", "seat-control", "operation-receipt"})
ROOT = Path(__file__).resolve().parent.parent
ID = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")
MARKER = re.compile(r"^\[control-delivery:([A-Za-z0-9_.:-]{1,100})\]\n【用户通过协作管理客户端发给本工位的消息】\n")


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def delivery_message(delivery):
    return "[control-delivery:" + delivery["id"] + "]\n【用户通过协作管理客户端发给本工位的消息】\n" + delivery["body"]


class Problem(Exception):
    def __init__(self, code, message, status=400):
        self.code, self.message, self.status = code, message, status


class Store:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS resources(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS deliveries(id TEXT PRIMARY KEY,target TEXT NOT NULL,body TEXT NOT NULL,
                    state TEXT NOT NULL,message_id TEXT,queue_id TEXT,error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT NOT NULL,object_id TEXT NOT NULL,created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cache(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(deliveries)")}
            for column, kind in (("reply", "TEXT"), ("reply_at", "TEXT"), ("reply_truncated", "INTEGER"), ("turn_id", "TEXT"),
                                 ("target_thread_id", "TEXT"), ("intake_context", "TEXT"), ("body_sha256", "TEXT"),
                                 ("queued_at", "TEXT"), ("native_event_id", "TEXT"), ("native_received_at", "TEXT"),
                                 ("native_start_event_id", "TEXT"), ("native_started_at", "TEXT"), ("intake_dirty", "INTEGER DEFAULT 0")):
                if column not in columns:
                    db.execute("ALTER TABLE deliveries ADD COLUMN " + column + " " + kind)

    def db(self):
        class Connection(sqlite3.Connection):
            def __exit__(self, *args):
                try:
                    return super().__exit__(*args)
                finally:
                    self.close()
        db = sqlite3.connect(self.path, timeout=10, factory=Connection)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def event(db, kind, object_id):
        db.execute("INSERT INTO events(kind,object_id,created_at) VALUES(?,?,?)", (kind, object_id, now()))

    def submit(self, request_id, target, body, target_thread_id=None, intake_context=None):
        if not isinstance(request_id, str) or not ID.fullmatch(request_id):
            raise Problem("invalid_id", "请使用有效的消息编号。")
        if not isinstance(body, str) or not body.strip() or len(body) > 4000:
            raise Problem("invalid_body", "消息需为1至4000字。")
        context = canonical(intake_context) if intake_context is not None else None
        body_hash = hashlib.sha256(delivery_message({'id': request_id, 'body': body}).encode('utf-8')).hexdigest()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM deliveries WHERE id=?", (request_id,)).fetchone()
            if old:
                if (old["target"] != target or old["body"] != body or old['target_thread_id'] != target_thread_id
                        or old['intake_context'] != context):
                    raise Problem("id_conflict", "该消息编号已用于不同内容。", 409)
                return dict(old)
            stamp = now()
            db.execute("INSERT INTO deliveries(id,target,body,state,created_at,updated_at,target_thread_id,intake_context,body_sha256,intake_dirty) VALUES(?,?,?,'pending',?,?,?,?,?,?)",
                       (request_id, target, body, stamp, stamp, target_thread_id, context, body_hash, int(context is not None)))
            self.event(db, "delivery.pending", request_id)
            return dict(db.execute("SELECT * FROM deliveries WHERE id=?", (request_id,)).fetchone())

    def delivery(self, request_id):
        with self.db() as db:
            row = db.execute('SELECT * FROM deliveries WHERE id=?', (request_id,)).fetchone()
        return dict(row) if row else None

    def claim_pending(self):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM deliveries WHERE state='pending' ORDER BY created_at LIMIT 1").fetchone()
            if not row:
                return None
            db.execute("UPDATE deliveries SET state='sending',updated_at=?,error=NULL WHERE id=?", (now(), row["id"]))
            self.event(db, "delivery.sending", row["id"])
            return dict(row)

    def transition(self, request_id, state, **fields):
        allowed = {k: v for k, v in fields.items() if k in ("message_id", "queue_id", "error", "reply", "reply_at", "reply_truncated", "turn_id",
                   "queued_at", "native_event_id", "native_received_at", "native_start_event_id", "native_started_at")}
        with self.db() as db:
            values = {"state": state, "updated_at": now(), **allowed}
            db.execute("UPDATE deliveries SET " + ",".join(k + "=?" for k in values) + ",intake_dirty=CASE WHEN intake_context IS NULL THEN 0 ELSE 1 END WHERE id=?", [*values.values(), request_id])
            self.event(db, "delivery." + state, request_id)

    def recover(self):
        with self.db() as db:
            # 云端message有幂等ID，CLI投递没有永久幂等；未知写入绝不自动重放。
            db.execute("UPDATE deliveries SET state='pending' WHERE state='sending'")
            rows = db.execute("SELECT id FROM deliveries WHERE state='queue_submitting'").fetchall()
            for row in rows:
                db.execute("UPDATE deliveries SET state='unknown',error=?,updated_at=?,intake_dirty=CASE WHEN intake_context IS NULL THEN 0 ELSE 1 END WHERE id=?",
                           ("上次投递中断，等待运行时证据核对；未自动重发。", now(), row["id"]))
                self.event(db, "delivery.unknown", row["id"])

    def deliveries(self, states=None):
        with self.db() as db:
            if states:
                return [dict(x) for x in db.execute("SELECT * FROM deliveries WHERE state IN (" + ",".join("?" for _ in states) + ") ORDER BY created_at", states)]
            return [dict(x) for x in db.execute("SELECT * FROM deliveries ORDER BY created_at DESC LIMIT 200")]

    def cache(self, value=None):
        with self.db() as db:
            if value is not None:
                db.execute("INSERT OR REPLACE INTO cache VALUES('hub',?)", (json.dumps(value, ensure_ascii=False),))
            row = db.execute("SELECT payload FROM cache WHERE id='hub'").fetchone()
            return json.loads(row[0]) if row else None


class RuntimeObserver:
    """只投影生命周期和本客户端消息标记；不返回用户正文或模型思考。"""
    def __init__(self, config, delivery_lookup=None):
        self.bindings = {x["actor_id"]: dict(x) for x in config.get("runtime_bindings", [])}
        self.delivery_lookup = delivery_lookup
        self.sessions_root = Path(config.get("runtime_sessions_dir", ""))
        self.states = {}
        self.receipts = {}

    def abandon_turn(self, actor, state):
        for delivery in state["active"]:
            receipt = self.receipts[(actor, delivery)]
            receipt.update(state="unknown", error="未收到原轮次的结束证据，已停止关联后续答复。")
            for key in ("reply", "reply_at", "reply_truncated"):
                receipt.pop(key, None)
        state.update(active=[], turn_id=None, start_event_id=None, started_at=None)

    def observe(self):
        for actor, binding in self.bindings.items():
            state = self.states.setdefault(actor, {"state": "unknown", "offset": 0, "active": [], "thread_id": binding["thread_id"]})
            if state['thread_id'] != binding['thread_id']:
                self.abandon_turn(actor, state)
                state = self.states[actor] = {"state": "unknown", "offset": 0, "active": [], "thread_id": binding['thread_id']}
            path = Path(binding.get("rollout", ""))
            if not path.is_file() and time.monotonic() >= state.get("next_search", 0):
                matches = list(self.sessions_root.glob("*/*/*/*" + binding["thread_id"] + ".jsonl")) if self.sessions_root.is_dir() else []
                if len(matches) == 1:
                    binding["rollout"] = str(matches[0]); path = matches[0]
                state["next_search"] = time.monotonic() + 30
            if not path.is_file():
                state["state"] = "unknown"
                continue
            try:
                if path.stat().st_size < state["offset"]:
                    self.abandon_turn(actor, state)
                    state.update(offset=0, state="unknown")
                with path.open("rb") as source:
                    source.seek(state["offset"])
                    for _ in range(200000):
                        line = source.readline()
                        if not line or not line.endswith(b"\n"):
                            break
                        state["offset"] = source.tell()
                        try:
                            event = json.loads(line)
                        except ValueError:
                            self.abandon_turn(actor, state)
                            state["state"] = "unknown"
                            continue
                        payload = event.get("payload", {})
                        if not isinstance(payload, dict):
                            continue
                        kind = payload.get("type")
                        if event.get("type") in ("response_item", "event_msg"):
                            state["last_activity_at"] = event.get("timestamp")
                        if event.get("type") == "event_msg":
                            if kind == "task_started":
                                turn_id = payload.get("turn_id")
                                if not isinstance(turn_id, str) or not turn_id:
                                    turn_id = None
                                if turn_id is None or turn_id != state.get("turn_id"):
                                    self.abandon_turn(actor, state)
                                    if turn_id:
                                        state.update(start_event_id='codex-event:' + hashlib.sha256(line).hexdigest(), started_at=event.get('timestamp'))
                                state.update(state="running", observed_at=event.get("timestamp"), turn_id=turn_id)
                            elif kind in ("task_complete", "task_completed", "turn_aborted"):
                                if not state.get("turn_id") or payload.get("turn_id") != state["turn_id"]:
                                    continue
                                state.update(state="idle", observed_at=event.get("timestamp"))
                                for delivery in state["active"]:
                                    self.receipts[(actor, delivery)]["state"] = "completed" if kind != "turn_aborted" else "interrupted"
                                state.update(active=[], turn_id=None, start_event_id=None, started_at=None)
                        if event.get("type") == "response_item" and kind == "message" and payload.get("role") == "user":
                            for part in payload.get("content", []):
                                text = part.get("text", "") if isinstance(part, dict) else ''
                                if not isinstance(text, str):
                                    continue
                                for delivery in MARKER.findall(text):
                                    body_hash = hashlib.sha256(text.encode('utf-8')).hexdigest()
                                    event_id = 'codex-event:' + hashlib.sha256(line).hexdigest()
                                    if self.delivery_lookup:
                                        original = self.delivery_lookup(delivery)
                                        if (not original or original['target'] != actor or original.get('target_thread_id') != binding['thread_id']
                                                or body_hash != hashlib.sha256(delivery_message(original).encode('utf-8')).hexdigest()
                                                or original.get('native_event_id') not in (None, event_id)
                                                or original.get('turn_id') not in (None, state.get('turn_id'))):
                                            continue
                                    # A duplicate marker never adopts a later turn or erases
                                    # the first receipt. Forged body hashes are not receipts.
                                    previous = self.receipts.get((actor, delivery))
                                    if previous and previous['message_hash'] == body_hash:
                                        continue
                                    self.receipts[(actor, delivery)] = {"state": "received", "message_hash": body_hash,
                                        "turn_id": state.get("turn_id"), "thread_id": binding['thread_id'],
                                        "native_event_id": event_id, "native_received_at": event.get('timestamp'),
                                        "native_start_event_id": state.get('start_event_id'), "native_started_at": state.get('started_at')}
                                    if state.get("turn_id") and delivery not in state["active"]:
                                        state["active"].append(delivery)
                        if (event.get("type") == "response_item" and kind == "message" and
                                payload.get("role") == "assistant" and payload.get("phase") == "final_answer" and state["active"]):
                            if payload.get("turn_id", state["turn_id"]) != state["turn_id"]:
                                continue
                            answer = "\n".join(part["text"] for part in payload.get("content", [])
                                               if isinstance(part, dict) and part.get("type") == "output_text" and isinstance(part.get("text"), str))
                            if answer:
                                for delivery in state["active"]:
                                    self.receipts[(actor, delivery)].update(reply=answer[:16000], reply_at=event.get("timestamp"), reply_truncated=len(answer) > 16000)
            except OSError:
                state["state"] = "unknown"

    def snapshot(self, actor):
        state = self.states.get(actor, {})
        if not state:
            return {"state": "unknown", "bound": actor in self.bindings}
        result = {k: state.get(k) for k in ("state", "observed_at", "thread_id", "last_activity_at")}
        result["evidence"] = "recorded_lifecycle"
        result["bound"] = actor in self.bindings
        result["workstation_id"] = state.get("thread_id")
        result["last_known_state"] = state["state"]
        try:
            age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(state["last_activity_at"].replace("Z", "+00:00"))).total_seconds()
        except (ValueError, TypeError, KeyError):
            age = None
        result["stale"] = age is None or age > 180
        if result["stale"]:
            result["state"] = "unknown"
        return result


class Hub:
    def __init__(self, config):
        self.config = config
        sys.path.insert(0, config["hub_client_dir"])
        self.client = importlib.import_module("coord")

    def status(self):
        return self.client.call(self.config["hub_read_identity"], "/v1/status")

    def chat_history(self, before=None):
        if getattr(self, 'is_local', False):
            return self.client.chat_history(before)
        result=self.client.call(self.config['hub_read_identity'], '/v1/history'+('?before='+str(before) if before else ''))
        end=time.time();start=end-86400
        raw=result.get('messages')
        if not isinstance(raw,list):
            raise ValueError('invalid_chat_history')
        rows=[m for m in raw if isinstance(m,dict) and isinstance(m.get('created'),(int,float)) and start<=m['created']<=end]
        more=bool(raw and result.get('has_more') and raw[-1].get('created',0)>=start)
        return {'messages':list(reversed(rows)), 'has_more':more,
                'next_before':raw[-1]['seq'] if more else None,
                'window_start':start,'window_end':end,'retention_hours':24}

    def agent_call(self, identity, path, body=None):
        return self.client.call(identity, path, body)

    def agent_provision(self, document):
        """Keep center credentials in the existing OS-user-bound vault."""
        identity = document['local_identity']
        if identity:
            actor = self.client.call(identity, '/v1/status')['actor']
            return {'identity': identity, 'actor_id': actor['id']}
        seed = hashlib.sha256(document['request_id'].encode()).hexdigest()[:24]
        identity = 'control-agent-' + seed
        owner = self.config.get('hub_user_identity')
        if not owner:
            raise ValueError('503:agent_owner_identity_not_configured')
        # Registration replay intentionally omits the token. Reuse the vault first.
        try:
            result = self.client.load(identity)
        except FileNotFoundError:
            result = None
        if result is None:
            for generation in range(4):
                result = self.client.call(owner, '/v1/register', {'request_id': 'control-enroll-' + seed + '-' + str(generation),
                    'name': document['name'], 'project': document['project']})
                if result.get('token'):
                    self.client.save(identity, result)
                    break
                # First registration response was lost before DPAPI persistence.
                # Retire that undelivered identity before allocating its replacement.
                self.client.call(owner, '/v1/revoke', {'request_id': 'control-orphan-' + seed + '-' + str(generation), 'id': result['id']})
            else:
                raise ValueError('503:agent_registration_recovery_exhausted')
        actor = self.client.call(identity, '/v1/status')['actor']
        if actor['id'] != result['id'] or actor['role'] != 'agent':
            raise ValueError('403:dedicated_agent_identity_required')
        host, adapter, seat = 'ext-host-' + seed, 'ext-adapter-' + seed, 'ext-seat-' + seed
        operations = [
            ('host-register', {'id': host, 'version': 0, 'name': document['name'], 'actor_id': actor['id'],
                               'projects': [document['project']], 'max_active': 1, 'max_seats': 8}),
            ('adapter-register', {'id': adapter, 'version': 0, 'host_id': host, 'name': document['name'],
                                  'kind': 'generic-agent', 'version_label': 'aieyra-agent/1',
                                  'actions': ['inspect', 'dispatch'], 'availability': 'available'}),
            ('seat-create', {'id': seat, 'version': 0, 'name': document['name'], 'project': document['project'],
                             'scope': 'external/' + seed, 'actor_id': actor['id'], 'host_id': host,
                             'adapter_id': adapter, 'ownership': 'external'})]
        for action, payload in operations:
            self.client.call(owner, '/v1/' + action, {**payload, 'request_id': 'control-' + seed + '-' + action})
        return {'identity': identity, 'actor_id': actor['id'], 'seat_id': seat}

    def intake_read(self, route, fields):
        from urllib.parse import urlencode
        if route not in INTAKE_READS:
            raise ValueError('invalid_intake_route')
        query = urlencode(fields)
        return self.client.call(self.config['hub_read_identity'], '/v1/' + route + ('?' + query if query else ''))

    def intake_record(self, payload):
        return self.client.call(self.config['hub_read_identity'], '/v1/requirement-receipt', payload)

    def intake_receipt(self, request_id):
        return self.intake_read('intake-receipt', {'request_id': request_id})

    def human_snapshot(self, project, after=''):
        identity = self.config.get('hub_user_identity', self.config['hub_read_identity'])
        return self.client.call(identity, '/v1/collaboration/snapshot?project=' + project + ('&after_human=' + after if after else ''))

    def human_get(self, ident):
        identity = self.config.get('hub_user_identity', self.config['hub_read_identity'])
        return self.client.call(identity, '/v1/human-request?id=' + ident)

    def human_decide(self, payload):
        identity = self.config.get('hub_user_identity')
        if not identity:
            raise HumanError('human_owner_identity_not_configured', 503)
        return self.client.call(identity, '/v1/human-request-decide', payload)

    def registry(self):
        # The registry includes capability flags and host-management visibility.
        # Keep the identity explicit and backend-only; default to the management
        # identity so the browser does not see a read-only projection while the
        # same local console is authorized to manage seats.
        identity = self.config.get("hub_registry_identity", self.config.get("hub_management_identity", self.config["hub_read_identity"]))
        return self.client.call(identity, "/v1/registry")

    def host_operations(self, host_id):
        if not isinstance(host_id, str) or not ID.fullmatch(host_id):
            raise Problem("invalid_host_id", "工位主机编号无效。")
        identity = self.config.get("hub_operations_identity", self.config.get("hub_management_identity", self.config.get("hub_read_identity")))
        return self.client.call(identity, "/v1/host-operations?host_id=" + host_id)

    def management(self, action, payload):
        # The browser never chooses an arbitrary cloud path. The center remains the
        # authority for role, scope, capacity, epoch and receipt validation.
        if action not in REGISTRY_ACTIONS:
            raise Problem("unsupported_management_action", "管理动作不在白名单内。", 400)
        if not isinstance(payload, dict):
            raise Problem("invalid_management_payload", "管理动作需为对象。", 400)
        identity = self.config.get("hub_management_identity", self.config.get("hub_user_identity"))
        return self.client.call(identity, "/v1/" + action, payload)

    def reconnect(self):
        # Reuse the center's authenticated tunnel recovery, including its existing fallback.
        return self.client.start()

    def message(self, delivery):
        # 此入口由本机用户主动发送，身份沿已有站长控制台；Agent后台汇报使用各自身份。
        if delivery.get('intake_context'):
            # Requirement delivery keeps its private body in the original local
            # ledger/native thread. The maintenance actor reports metadata only.
            return self.client.call(self.config['hub_read_identity'], '/v1/message', {
                'project': 'coordination', 'kind': 'progress', 'request_id': 'control:' + delivery['id'],
                'body': canonical({'control_delivery': delivery['id'], 'context': json.loads(delivery['intake_context']),
                                   'body_sha256': delivery['body_sha256'], 'state': 'dispatch_intent'})})
        return self.client.call(self.config["hub_user_identity"], "/v1/message", {
            "project": "coordination", "kind": "discussion", "request_id": "control:" + delivery["id"],
            "body": ("[发送给 " + delivery["target"] + "]\n" if delivery["target"] != "all" else "") + delivery["body"],
        })


def validate_resource(item):
    if not isinstance(item, dict):
        raise Problem("invalid_resource", "资源需为JSON对象。")
    row = {k: str(item.get(k, "")).strip() for k in ("id", "name", "kind", "project", "location", "summary")}
    if not ID.fullmatch(row["id"]) or not row["name"] or len(row["name"]) > 120 or len(row["summary"]) > 1000:
        raise Problem("invalid_resource", "请填写有效资源编号、名称和简短说明。")
    if row["kind"] not in ("document", "directory", "repository", "server", "github") or not row["location"] or len(row["location"]) > 2048:
        raise Problem("invalid_resource", "资源类型或位置无效。")
    if "\n" in row["location"] or "\r" in row["location"]:
        raise Problem("invalid_resource", "资源位置不能换行。")
    if "://" in row["location"]:
        url = urlsplit(row["location"])
        if url.scheme not in (("https", "ssh") if row["kind"] == "server" else ("https",)) or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise Problem("invalid_resource_url", "请填写不含凭据、查询参数的HTTPS资源地址。")
    elif not Path(row["location"]).is_absolute():
        raise Problem("invalid_resource_path", "本地资源需使用绝对路径。")
    row["sensitivity"] = "metadata_only"
    for key in ("source_ref", "source_sha256"):
        if key in item:
            row[key] = str(item[key])[:2048]
    if isinstance(item.get("details"), dict):
        row["details"] = {str(k)[:80]: str(v)[:500] for k, v in list(item["details"].items())[:20]}
    return row


class Application:
    def __init__(self, config, data_dir, hub=None, queue_runner=None):
        self.config = config
        self.cloud = CloudLink(vault=SessionVault((data_dir.parent if data_dir.name=='shared' else data_dir)/'config'))
        self.store = Store(data_dir / "control.sqlite")
        self.store.recover()
        self.os_sessions = OSSessions(config, data_dir, ROOT, self.store)
        self.collaboration = Projection(self.store, config, source_provider=self.os_sessions.sources)
        self.product_commands = ProductCommands(self.os_sessions.sources)
        self.product_jobs = ProductJobs(self.store, self.product_commands)
        self.hub = hub or (create_local_hub(config, data_dir, Hub) if config.get('coordination_mode', 'local') == 'local' else Hub(config))
        self.intake = RequestIntake(self.hub)
        self.humans = HumanRequests(self.store, self.hub, config, self.product_commands)
        self.observer = RuntimeObserver(config, self.store.delivery)
        self.requirement_deliveries = RequirementDeliveries(self.store, self.hub, self.delivery_bindings, data_dir)
        self.queue_runner = queue_runner or self.run_queue
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.csrf = secrets.token_urlsafe(32)
        self.hub_snapshot = self.store.cache() or {}
        self.connection = {"state": "offline", "last_success_at": self.hub_snapshot.get("_fetched_at"), "error": "等待连接协作中心", "source": "coordination"}
        self.resource_rows = []
        self.next_dispatch = 0
        self.next_resource_refresh = 0
        self.delivery_failures = 0
        self.metrics = {}
        self._discovery = None
        self._discovery_at = 0.0
        self.agent_access = AgentAccess(self)
        self.project_memory = ProjectMemory(self.store, config.get('local_projects', []))
        for credential in self.agent_access.listing()['credentials']:
            if not credential['revoked']:
                self.project_memory.register({'id': credential['project']})
        configured_watchdog = config.get("watchdog_state_path")
        self.watchdog_state_path = Path(configured_watchdog) if configured_watchdog else None
        with self.store.db() as db:
            for resource in config.get("resources", []):
                row = validate_resource(resource)
                db.execute("INSERT OR IGNORE INTO resources VALUES(?,?)", (row["id"], json.dumps(row, ensure_ascii=False)))
        self.refresh_resources()

    def refresh_resources(self):
        with self.store.db() as db:
            rows = [json.loads(x[0]) for x in db.execute("SELECT payload FROM resources ORDER BY id")]
        imported, errors = collect(self.config.get("resource_sources", []))
        rows.extend(validate_resource(x) for x in imported)
        for row in rows:
            row.update(observed_at=now(), size=None)
            row["brief_available"] = row["id"] in self.config.get("briefs", {})
            if "://" in row["location"]:
                row["status"] = "registered"
            else:
                path = Path(row["location"])
                try:
                    stat = path.stat()
                    row.update(status="available", size=stat.st_size if path.is_file() else None,
                               modified_at=dt.datetime.fromtimestamp(stat.st_mtime, dt.timezone.utc).isoformat())
                except OSError:
                    row["status"] = "missing"
        with self.lock:
            self.resource_rows = rows
            self.resource_errors = errors
        return copy.deepcopy(rows)

    def register_resource(self, item):
        row = validate_resource(item)
        with self.store.db() as db:
            old = db.execute("SELECT payload FROM resources WHERE id=?", (row["id"],)).fetchone()
            if old and json.loads(old[0]) != row:
                raise Problem("resource_conflict", "该编号已登记其他资源，请换一个编号。", 409)
            db.execute("INSERT OR IGNORE INTO resources VALUES(?,?)", (row["id"], json.dumps(row, ensure_ascii=False)))
            self.store.event(db, "resource.registered", row["id"])
        return next(x for x in self.refresh_resources() if x["id"] == row["id"])

    @staticmethod
    def hub_failure(error, fallback_code, fallback_message):
        # coord.py deliberately exposes only status:code; preserve safe center
        # conflicts (403/409) for the UI without leaking tracebacks or payloads.
        match = re.fullmatch(r"([1-5][0-9]{2}):([A-Za-z0-9_.-]{1,80})", str(error))
        if match:
            status = int(match[1])
            code = match[2]
            if status in (400, 401, 403, 404, 409, 413, 415, 429):
                return Problem(code, "中心拒绝或未接受该管理请求。", status)
        return Problem(fallback_code, fallback_message, 503)

    def brief(self, resource_id):
        configuration = self.config.get("briefs", {}).get(resource_id)
        if not configuration:
            raise Problem("brief_not_registered", "该资源未登记可查看的工程简报。", 404)
        try:
            return read_brief(configuration)
        except (OSError, ValueError, TypeError, KeyError):
            raise Problem("brief_unavailable", "简报暂不可用，请检查登记的来源文件。", 503) from None

    def watchdog(self):
        """Return a persisted, metadata-only watchdog state if configured."""
        if self.watchdog_state_path is None:
            return {"schema_version": 1, "state": "not_configured", "enabled": False,
                    "observed_at": now(), "seats": [], "active_trigger": None}
        try:
            value = json.loads(self.watchdog_state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {"schema_version": 1, "state": "unknown", "enabled": None,
                    "observed_at": now(), "reason": "watchdog_state_unavailable",
                    "seats": [], "active_trigger": None}
        if not isinstance(value, dict):
            return {"schema_version": 1, "state": "unknown", "enabled": None,
                    "observed_at": now(), "reason": "watchdog_state_invalid",
                    "seats": [], "active_trigger": None}
        status = value.get("status") if isinstance(value.get("status"), dict) else {}
        # The public state contains lifecycle timestamps and IDs only; the
        # watchdog never writes message text, keys, or reasoning into it.
        return {"schema_version": value.get("schema_version", 1),
                "state": status.get("state", "unknown"),
                "enabled": status.get("enabled"),
                "observed_at": status.get("observed_at", value.get("updated_at", now())),
                "reason": status.get("reason"),
                "quota": status.get("quota", {"state": "unknown", "reason": "not_reported"}),
                "remaining_work": status.get("remaining_work"),
                "leader_target": status.get("leader_target"),
                "trigger_count": status.get("trigger_count", value.get("trigger_count", 0)),
                "last_trigger_at": status.get("last_trigger_at", value.get("last_trigger_at")),
                "active_trigger": status.get("active_trigger", value.get("active_trigger")),
                "seats": status.get("seats", [])}

    def snapshot(self):
        with self.lock:
            value = copy.deepcopy(self.hub_snapshot)
            agents = value.get("agents", [])
            for actor in agents:
                actor["runtime"] = self.observer.snapshot(actor["id"])
                actor["heartbeat_observed_at"] = value.get("_fetched_at")
                if self.connection["state"] != "online":
                    actor["last_known_online"] = actor.get("online")
                    actor["online"] = None
            for actor_id, external in self.agent_access.runtimes().items():
                existing = next((a for a in agents if a['id'] == actor_id), None)
                if existing:
                    existing['runtime'] = external['runtime']
                else:
                    agents.append({**external, 'role': 'agent', 'online': None})
            projects = value.get("projects", [])
            known_projects = {item["id"] for item in projects}
            projects.extend(copy.deepcopy(item) for item in self.config.get("local_projects", []) if item["id"] not in known_projects)
            governance = copy.deepcopy(self.config.get("governance", {}))
            tasks = {item["id"]: item for item in value.get("tasks", [])}
            for assignment in governance.get("assignments", []):
                task = tasks.get(assignment.get("task_id"))
                if task:
                    assignment.update(status=task["status"], task_owner=task.get("owner"), observed_at=value.get("_fetched_at"))
            result = {"version": VERSION, "observed_at": now(), "connection": dict(self.connection),
                "projects": projects, "agents": agents, "tasks": value.get("tasks", []),
                "messages": value.get("messages", []), "resources": copy.deepcopy(self.resource_rows),
                "resource_errors": copy.deepcopy(self.resource_errors),
                "watchdog": self.watchdog(),
                "governance": governance, "deliveries": self.store.deliveries(),
                "capabilities": {"chat": True, "runtime_dispatch": True, "resource_refresh": True,
                                 "resource_register": True, "agent_access": True, "project_memory": True, "mobile_pairing": False, "remote_execution": False}}
            for resource in result["resources"]:
                if resource["location"] in self.metrics:
                    metric = copy.deepcopy(self.metrics[resource["location"]])
                    try:
                        age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(metric["observed_at"])).total_seconds()
                    except (ValueError, KeyError, TypeError):
                        age = float("inf")
                    if metric.get("state") == "sampled" and age > max(120, self.config.get("monitoring", {}).get("interval_seconds", 60) * 2 + 12):
                        metric = {key: metric.get(key) for key in ("observed_at", "source")}
                        metric.update(state="unavailable", stale=True, error="采样记录已过期，等待下一次更新。")
                    resource["metrics"] = metric
        return result

    def registry(self):
        try:
            value = self.hub.registry()
        except Exception as error:
            raise self.hub_failure(error, "registry_unavailable", "中心工位注册表暂不可用。") from None
        if not isinstance(value, dict) or not isinstance(value.get("hosts"), list) or not isinstance(value.get("seats"), list):
            raise Problem("registry_invalid", "中心返回的工位注册表无效。", 502)
        value["connection"] = dict(self.connection)
        value["observed_at"] = now()
        return self.agent_access.station_registry(value)

    def local_agents(self):
        with self.lock:
            if self._discovery is not None and time.monotonic() - self._discovery_at < 30:
                return copy.deepcopy(self._discovery)
        value = discover(self.config)
        with self.lock:
            self._discovery = value
            self._discovery_at = time.monotonic()
        return copy.deepcopy(value)

    def host_operations(self, host_id):
        if not isinstance(host_id, str) or not ID.fullmatch(host_id):
            raise Problem("invalid_host_id", "工位主机编号无效。", 400)
        try:
            value = self.hub.host_operations(host_id)
        except Problem:
            raise
        except Exception as error:
            raise self.hub_failure(error, "host_operations_unavailable", "主机待处理操作暂不可用。") from None
        if not isinstance(value, dict) or not isinstance(value.get("operations"), list):
            raise Problem("host_operations_invalid", "中心返回的操作队列无效。", 502)
        value["observed_at"] = now()
        return value

    def management(self, action, payload):
        if action not in REGISTRY_ACTIONS:
            raise Problem("unsupported_management_action", "管理动作不在白名单内。", 400)
        if not isinstance(payload.get("request_id"), str) or not ID.fullmatch(payload["request_id"]):
            raise Problem("invalid_request_id", "管理请求必须携带固定 request_id。", 400)
        try:
            return self.hub.management(action, payload)
        except Problem:
            raise
        except Exception as error:
            raise self.hub_failure(error, "management_unavailable", "中心管理接口暂不可用；未确认写入结果。") from None

    def submit(self, value):
        target = value.get("target", "all")
        external = self.agent_access.binding(target) if target != 'all' else None
        if target != "all" and target not in self.observer.bindings and not external:
            raise Problem("unbound_runtime", "该工位尚未绑定可投递会话。", 409)
        if external:
            self.agent_access.validate_binding(external)
        thread_id = None if target == 'all' else (external or self.observer.bindings[target]).get('thread_id')
        if target != 'all' and (not isinstance(thread_id, str) or not ID.fullmatch(thread_id)):
            raise Problem('unbound_runtime', '该工位尚未绑定有效的目标会话。', 409)
        return self.store.submit(value.get("request_id"), target, value.get("body"), thread_id)

    def delivery_bindings(self):
        bindings = dict(self.observer.bindings)
        for actor, value in self.agent_access.runtimes().items():
            if value['runtime']['bound']:
                binding = self.agent_access.binding(actor)
                if binding:
                    bindings[actor] = binding
        return bindings

    def delivery_binding(self, delivery):
        binding = self.agent_access.binding(delivery['target']) if str(delivery.get('target_thread_id', '')).startswith('bridge:') else self.observer.bindings.get(delivery['target'])
        if not delivery.get('target_thread_id') or not binding or binding.get('thread_id') != delivery['target_thread_id']:
            self.store.transition(delivery['id'], 'unknown', error='原投递缺少固定会话或工位绑定已变化；未向新会话投递，只查询原记录。')
            return None
        if delivery.get('body_sha256') and delivery['body_sha256'] != hashlib.sha256(delivery_message(delivery).encode('utf-8')).hexdigest():
            self.store.transition(delivery['id'], 'unknown', error='投递正文摘要与原意图不符，已停止；未重新投递。')
            return None
        return dict(binding)

    def requirement_current(self, delivery):
        if not delivery.get('intake_context'):
            return True
        try:
            self.requirement_deliveries.check(json.loads(delivery['intake_context']))
            return True
        except RequirementDeliveryError as error:
            self.store.transition(delivery['id'], 'pending' if error.status == 503 else 'unknown', error=error.code)
            if error.status == 503:
                self.next_dispatch = time.monotonic() + 10
            return False

    def run_queue(self, binding, delivery):
        message = delivery_message(delivery)
        result = subprocess.run([self.config["codex_executable"], "queue", "--thread", binding["thread_id"], "--message", message],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        match = re.search(r"Queued message ([A-Za-z0-9-]+) for thread ([A-Za-z0-9-]+)", result.stdout)
        if result.returncode != 0 or not match or match[2] != binding["thread_id"]:
            raise Problem("queue_unconfirmed", "运行时未返回可核对的队列回执，未自动重发。", 502)
        return match[1]

    def dispatch_one(self):
        if time.monotonic() < self.next_dispatch:
            return
        delivery = self.store.claim_pending()
        if delivery is None:
            return
        if delivery['target'] != 'all' and self.delivery_binding(delivery) is None:
            return
        if str(delivery.get('target_thread_id', '')).startswith('bridge:'):
            binding = self.delivery_binding(delivery)
            if binding is None:
                return
            try:
                self.agent_access.validate_binding(binding)
            except AgentError as error:
                self.store.transition(delivery['id'], 'pending' if error.status == 503 else 'unknown', error=error.code)
                self.next_dispatch = time.monotonic() + 10
                return
        if not self.requirement_current(delivery):
            return
        try:
            receipt = self.hub.message(delivery)
            if not isinstance(receipt, dict) or not isinstance(receipt.get("id"), str) or not receipt["id"]:
                raise ValueError("invalid_hub_receipt")
        except Exception:
            # 沿用同一云端request_id，下一次工作循环可对账重试。
            self.store.transition(delivery["id"], "pending", error="协作中心暂不可达，已保存在本机待发送。")
            self.delivery_failures += 1
            self.next_dispatch = time.monotonic() + min(60, 2 ** min(self.delivery_failures, 6))
            return
        self.delivery_failures = 0
        self.next_dispatch = 0
        if delivery["target"] == "all":
            self.store.transition(delivery["id"], "stored", message_id=receipt["id"], error=None)
            return
        binding = self.delivery_binding(delivery)
        if binding is None or not self.requirement_current(delivery):
            return
        if binding.get('bridge'):
            try:
                self.agent_access.validate_binding(binding)
                self.agent_access.enqueue(binding, delivery, receipt['id'])
            except AgentError as error:
                self.store.transition(delivery['id'], 'pending' if error.status == 503 else 'unknown', message_id=receipt['id'], error=error.code)
                self.next_dispatch = time.monotonic() + 10
                return
            return
        self.store.transition(delivery["id"], "queue_submitting", message_id=receipt["id"], error=None)
        try:
            queue_id = self.queue_runner(binding, delivery)
            if not isinstance(queue_id, str) or not ID.fullmatch(queue_id):
                raise ValueError('invalid_queue_receipt')
            self.store.transition(delivery["id"], "queued", queue_id=queue_id, queued_at=now(), error=None)
        except Exception:
            self.store.transition(delivery["id"], "unknown", error="运行时投递结果待核对，未自动重发。")

    def tick(self):
        try:
            snapshot = self.hub.status()
            snapshot["_fetched_at"] = now()
            self.store.cache(snapshot)
            with self.lock:
                self.hub_snapshot = snapshot
                self.connection = {"state": "online", "last_success_at": snapshot["_fetched_at"], "error": None, "source": "coordination"}
        except Exception:
            with self.lock:
                self.connection.update(state="offline", error="协作中心暂时不可达，显示最近成功数据。")
        with self.lock:
            self.observer.observe()
            receipts = dict(self.observer.receipts)
        for delivery in self.store.deliveries(("queued", "received", "unknown")):
            receipt = receipts.get((delivery["target"], delivery["id"]), {})
            state = receipt.get("state") if (delivery.get('target_thread_id') and receipt.get('thread_id') == delivery['target_thread_id']
                and receipt.get("message_hash") == hashlib.sha256(delivery_message(delivery).encode()).hexdigest()) else None
            if delivery.get('turn_id') and receipt.get('turn_id') != delivery['turn_id']:
                continue
            if delivery.get('native_event_id') and receipt.get('native_event_id') != delivery['native_event_id']:
                continue
            evidence = {key: receipt[key] for key in ('native_event_id', 'native_received_at', 'native_start_event_id', 'native_started_at')
                        if receipt.get(key) is not None}
            if state and (state != delivery["state"] or receipt.get("turn_id") != delivery.get("turn_id")
                          or any(delivery.get(key) != value for key, value in evidence.items())):
                reply = {key: receipt[key] for key in ("reply", "reply_at", "reply_truncated") if key in receipt} if state in ("completed", "interrupted") else {}
                self.store.transition(delivery["id"], state, turn_id=receipt.get("turn_id"), error=receipt.get("error"), **reply, **evidence)
        if time.monotonic() >= self.next_resource_refresh:
            self.refresh_resources()
            self.next_resource_refresh = time.monotonic() + 30
        self.dispatch_one()
        self.requirement_deliveries.sync()

    def run(self):
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception:
                with self.lock:
                    self.connection.update(error="本地同步暂时失败，将保留记录并重试。")
            self.stop.wait(3)

    def cloud_monitor(self):
        while not self.stop.is_set():
            self.cloud.background_check()
            self.stop.wait(15)

    def monitor(self):
        configuration = self.config.get("monitoring")
        if not configuration:
            return
        while not self.stop.is_set():
            try:
                metrics = collect_monitors(configuration)
                with self.lock:
                    self.metrics = metrics
            except Exception:
                pass
            self.stop.wait(max(30, configuration.get("interval_seconds", 60)))

    def reconnect_hub(self):
        if not self.config.get("hub_reconnect", False) or not hasattr(self.hub, "reconnect"):
            return
        while not self.stop.wait(30):
            with self.lock:
                offline = self.connection["state"] == "offline"
            if offline:
                try:
                    self.hub.reconnect()
                except Exception:
                    pass
                self.stop.wait(30)

    def monitor_collaboration(self):
        while not self.stop.is_set():
            try:
                self.os_sessions.refresh()
                self.collaboration.poll()
                self.humans.poll()
                self.product_jobs.poll()
            except Exception:
                pass
            self.stop.wait(3)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, port, application, web_root=None):
        self.app = application
        self.web_root = (web_root or ROOT / "web").resolve()
        super().__init__(("127.0.0.1", port), Handler)
        self.origin = "http://127.0.0.1:" + str(self.server_address[1])


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def finish(self):
        try:
            if getattr(self, '_unread_post_body', False):
                # 先交付拒绝响应，再有界丢弃上传。未读正文直接关连接会让
                # Windows 客户端收到连接中止，丢掉已经发送的403/400。
                self.wfile.flush()
                self.connection.shutdown(socket.SHUT_WR)
                deadline = time.monotonic() + 0.25
                remaining = 65536
                while remaining > 0:
                    timeout = deadline - time.monotonic()
                    if timeout <= 0:
                        break
                    self.connection.settimeout(timeout)
                    chunk = self.rfile.read1(min(8192, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
        except OSError:
            pass
        finally:
            super().finish()

    def send(self, status, data, content_type="application/json; charset=utf-8"):
        raw = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            if self.close_connection:
                self.send_header("Connection", "close")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(raw)
        except (ConnectionError, TimeoutError):
            # The operation may already be committed. A disconnected response
            # cannot become a second write_failed response or undo its receipt.
            self.close_connection = True

    def guard(self, write=False):
        if any(len(self.headers.get_all(key, [])) > 1 for key in
               ('Host', 'Origin', 'Sec-Fetch-Site', 'X-Control-CSRF', 'Authorization', 'Content-Length', 'Content-Type', 'Transfer-Encoding')):
            raise Problem('duplicate_header', '请求头不能重复。')
        if self.headers.get("Host") != self.server.origin.removeprefix("http://") or self.headers.get("Origin") not in (None, self.server.origin):
            raise Problem("origin_rejected", "来源不受信任。", 403)
        if self.headers.get("Sec-Fetch-Site") in ("cross-site", "same-site"):
            raise Problem("origin_rejected", "来源不受信任。", 403)
        if write and (self.headers.get("Origin") != self.server.origin or not secrets.compare_digest(self.headers.get("X-Control-CSRF", ""), self.server.app.csrf)):
            raise Problem("csrf_rejected", "会话已失效，请刷新后重试。", 403)

    def agent_guard(self):
        self.guard()
        if self.headers.get('Origin') or self.headers.get('Sec-Fetch-Site'):
            raise AgentError('agent_browser_access_denied', 403)
        authorization = self.headers.get('Authorization', '')
        if not authorization.startswith('Bearer '):
            raise AgentError('agent_unauthorized', 401)
        return self.server.app.agent_access.authenticate(authorization[7:])

    def agent_get(self, path, peer):
        access = self.server.app.agent_access
        try:
            query = parse_qs(urlsplit(self.path).query, keep_blank_values=True, max_num_fields=16)
        except ValueError:
            raise AgentError('invalid_agent_query') from None
        if any(len(values) != 1 for values in query.values()):
            raise AgentError('duplicate_query')
        if path == 'info':
            return {'protocol': 'aieyra-agent/1', 'actor_id': peer['actor_id'], 'project': peer['project'],
                    'center_reads': sorted(AGENT_READS), 'center_writes': sorted(AGENT_WRITES),
                    'os_primary': True, 'lease_seconds': 90, 'openapi': '/api/agent-openapi',
                    'project_memory': {'read': 'memory?project=' + peer['project'], 'write': 'memory',
                                       'history': 'memory?project=' + peer['project'] + '&history=1', 'storage': 'local_control'}}
        if path == 'cloud/status':
            return self.server.app.cloud.status()
        if path == 'cloud/community':
            return self.server.app.cloud.call('/v1/community')
        if path == 'memory':
            project = query.get('project', [peer['project']])[0]
            if project != peer['project']:
                raise AgentError('memory_project_denied', 403)
            return self.memory_get(query, project)
        if path == 'seats':
            return access.seats(peer)
        if path == 'inbox':
            if set(query) != {'session_id'}:
                raise AgentError('session_id_required')
            return access.inbox(peer, query['session_id'][0])
        if path.startswith('sessions/'):
            return {'session': access.session(peer, path[9:], False)}
        if path.startswith('deliveries/'):
            row = access.store.delivery(path[11:])
            if not row or row['target'] != peer['actor_id'] or not str(row.get('target_thread_id', '')).startswith('bridge:'):
                raise AgentError('agent_delivery_missing', 404)
            session = access.session(peer, row['target_thread_id'][7:], False)
            return {'delivery': row, 'session_state': session['state'], 'evidence_source': 'agent_report'}
        if path.startswith('requests/'):
            with access.store.db() as db:
                row = db.execute('SELECT response FROM agent_requests WHERE credential_id=? AND id=?', (peer['id'], path[9:])).fetchone()
            if not row:
                raise AgentError('agent_request_missing', 404)
            return {'receipt': json.loads(row['response']), 'replay_allowed': False}
        if path.startswith('center/'):
            return access.center(peer, path[7:], query=query)
        raise AgentError('agent_route_not_supported', 404)

    def memory_get(self, query, project=None):
        if set(query) - {'project', 'version', 'history', 'before'} or any(len(v) != 1 for v in query.values()):
            raise MemoryError('invalid_memory_query')
        memory = self.server.app.project_memory
        project = project or query.get('project', [''])[0]
        if not project:
            if query:
                raise MemoryError('memory_project_required')
            return memory.listing()
        try:
            if 'history' in query:
                if query['history'] != ['1'] or 'version' in query:
                    raise ValueError()
                return memory.history(project, int(query['before'][0]) if 'before' in query else None)
            if 'before' in query:
                raise ValueError()
            return memory.read(project, int(query['version'][0]) if 'version' in query else None)
        except ValueError:
            raise MemoryError('invalid_memory_query') from None

    def do_GET(self):
        try:
            self.guard()
            path = unquote(urlsplit(self.path).path)
            if path.startswith('/api/agent/v1/'):
                return self.send(200, self.agent_get(path[14:], self.agent_guard()))
            if self.headers.get('Authorization'):
                raise AgentError('agent_token_wrong_endpoint', 403)
            if path == '/api/agent-access':
                return self.send(200, self.server.app.agent_access.listing())
            if path == '/api/cloud':
                return self.send(200,self.server.app.cloud.status())
            if path == '/api/project-memory':
                try:
                    query = parse_qs(urlsplit(self.path).query, keep_blank_values=True, max_num_fields=4)
                except ValueError:
                    raise MemoryError('invalid_memory_query') from None
                return self.send(200, self.memory_get(query))
            if path == '/api/agent-openapi':
                return self.send(200, agent_openapi(self.server.origin))
            if path in ("/api/health", "/health"):
                return self.send(200, {"service": "aieyra-control", "version": VERSION})
            if path == "/api/session":
                return self.send(200, {"csrf": self.server.app.csrf, "version": VERSION})
            if path == "/api/snapshot":
                return self.send(200, self.server.app.snapshot())
            if path == '/api/chat-history':
                try:
                    query=parse_qs(urlsplit(self.path).query,keep_blank_values=True,max_num_fields=1)
                    if set(query)-{'before'} or any(len(v)!=1 for v in query.values()):
                        raise ValueError('query')
                    raw=query.get('before',[None])[0]
                    if raw is not None and not re.fullmatch(r'[1-9][0-9]{0,18}',raw):
                        raise ValueError('cursor')
                    before=int(raw) if raw else None
                    if before is not None and before>9223372036854775807:raise ValueError('cursor')
                except ValueError:
                    raise Problem('invalid_chat_cursor','聊天历史游标无效。') from None
                try:
                    return self.send(200,self.server.app.hub.chat_history(before))
                except Exception as error:
                    raise self.server.app.hub_failure(error,'chat_history_unavailable','群聊记录暂时无法读取。') from None
            match = re.fullmatch(r'/api/deliveries/([A-Za-z0-9_.:-]{1,128})', path)
            if match:
                delivery = self.server.app.store.delivery(match[1])
                if delivery is None:
                    raise Problem('delivery_not_found', '尚未查到这条请求。', 404)
                return self.send(200, {'delivery': delivery})
            if path == '/api/requirement-dispatch':
                try:
                    query = parse_qs(urlsplit(self.path).query, keep_blank_values=True, max_num_fields=2)
                except ValueError:
                    raise RequirementDeliveryError('invalid_requirement_dispatch_query', 400) from None
                if set(query) != {'request_id'} or len(query['request_id']) != 1:
                    raise RequirementDeliveryError('invalid_requirement_dispatch_query', 400)
                return self.send(200, self.server.app.requirement_deliveries.status(query['request_id'][0]))
            if path.startswith('/api/') and path[5:] in INTAKE_READS:
                try:
                    query = parse_qs(urlsplit(self.path).query, keep_blank_values=True, max_num_fields=16)
                except ValueError:
                    raise IntakeError('invalid_intake_query') from None
                return self.send(200, self.server.app.intake.read(path[5:], query))
            if path == "/api/collaboration":
                return self.send(200, self.server.app.collaboration.snapshot())
            if path == "/api/product-jobs":
                return self.send(200, self.server.app.product_jobs.snapshot())
            if path in ("/api/collaboration/command", "/api/collaboration/matrix"):
                query = parse_qs(urlsplit(self.path).query)
                source = query.get('source_id', [''])[0]
                commands = self.server.app.product_commands
                result = commands.matrix_status(source) if path.endswith('/matrix') else commands.status(source, query.get('request_id', [''])[0])
                return self.send(200, result)
            if path == "/api/human-requests":
                return self.send(200, self.server.app.humans.snapshot())
            match = re.fullmatch(r'/api/human-requests/([A-Za-z0-9_.:-]{1,100})', path)
            if match:
                return self.send(200, self.server.app.humans.detail(match[1]))
            match = re.fullmatch(r'/api/human-decisions/([A-Za-z0-9_.:-]{1,100})', path)
            if match:
                return self.send(200, self.server.app.humans.decision_status(match[1]))
            if path == "/api/registry":
                return self.send(200, self.server.app.registry())
            if path == "/api/local-agents":
                return self.send(200, self.server.app.local_agents())
            if path == "/api/host-operations":
                host_id = parse_qs(urlsplit(self.path).query).get("host_id", [""])[0]
                return self.send(200, self.server.app.host_operations(host_id))
            if path == "/api/watchdog":
                return self.send(200, self.server.app.watchdog())
            match = re.fullmatch(r"/api/resources/([A-Za-z0-9_.:-]{1,100})/brief", path)
            if match:
                return self.send(200, self.server.app.brief(match[1]))
            if path == "/api/events":
                try:
                    after = max(0, int(parse_qs(urlsplit(self.path).query).get("after", ["0"])[0]))
                except ValueError:
                    raise Problem("invalid_cursor", "事件游标无效。")
                with self.server.app.store.db() as db:
                    rows = [dict(x) for x in db.execute("SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT 100", (after,))]
                return self.send(200, {"events": rows, "next_cursor": rows[-1]["seq"] if rows else after})
            if path.startswith("/api/"):
                raise Problem("not_found", "接口不存在。", 404)
            file = (self.server.web_root / ("index.html" if path == "/" else path.lstrip("/"))).resolve()
            if not file.is_relative_to(self.server.web_root) or not file.is_file():
                raise Problem("not_found", "页面尚未就绪或不存在。", 404)
            kind = mimetypes.guess_type(str(file))[0] or "application/octet-stream"
            return self.send(200, file.read_bytes(), kind + ("; charset=utf-8" if kind.startswith("text/") or kind == "application/javascript" else ""))
        except CloudError as error:
            self.send(error.status,{'error':error.code,'code':error.code})
        except MemoryError as error:
            self.send(error.status, {'code': error.code, 'error': error.code, 'replay_allowed': False})
        except AgentError as error:
            self.send(error.status, {'code': error.code, 'error': error.code, 'replay_allowed': False})
        except RequirementDeliveryError as error:
            self.send(error.status, {'error': '需求投递状态未确认，请查询原请求。', 'code': error.code, 'replay_allowed': False})
        except IntakeError as error:
            self.send(error.status, {'error': '需求入口暂不可读取，请保留原记录并查询。', 'code': error.code,
                                     'available': False, 'stale': True})
        except HumanError as error:
            self.send(error.status, {"error": "人工事项状态未确认，请查询原请求。", "code": error.code})
        except ProductCommandError as error:
            self.send(409, {"error": "产品动作状态未确认，请查看原请求记录。", "code": str(error)})
        except Problem as error:
            self.send(error.status, {"error": error.message, "code": error.code})
        except (ConnectionError, TimeoutError):
            self.close_connection = True
        except Exception:
            self.send(500, {"error": "读取失败，请稍后重试。", "code": "read_failed"})

    def do_POST(self):
        self._unread_post_body = True
        try:
            path = urlsplit(self.path).path
            self.guard()
            peer = self.agent_guard() if path.startswith('/api/agent/v1/') else None
            if peer is None:
                if self.headers.get('Authorization'):
                    raise AgentError('agent_token_wrong_endpoint', 403)
                self.guard(write=True)
            if urlsplit(self.path).query:
                raise Problem('unexpected_query', '写入接口不接受查询参数。')
            if self.headers.get("Transfer-Encoding") or self.headers.get_content_type() != "application/json":
                raise Problem("json_required", "请使用JSON请求。")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise Problem("invalid_length", "请求长度无效。")
            if not 0 < length <= 32768:
                raise Problem("body_limit", "请求过长。", 413)
            raw = self.rfile.read(length)
            self._unread_post_body = False
            if len(raw) != length:
                raise Problem('incomplete_body', '请求内容未完整送达。')
            def unique_object(pairs):
                value = {}
                for key, item in pairs:
                    if key in value:
                        raise ValueError('duplicate_json_key')
                    value[key] = item
                return value
            def invalid_constant(value):
                raise ValueError('nonfinite_json_number')
            value = json.loads(raw, object_pairs_hook=unique_object, parse_constant=invalid_constant)
            if not isinstance(value, dict):
                raise Problem("invalid_json", "请求需为JSON对象。")
            if peer:
                action = path[14:]
                result = (self.server.app.agent_access.station_action(peer, action[8:], value) if action.startswith('station/') else
                          self.server.app.agent_access.cloud_action(peer, action[6:], value) if action.startswith('cloud/') else
                          self.server.app.agent_access.save_memory(peer, value) if action == 'memory' else
                          self.server.app.agent_access.center(peer, action[7:], body=value) if action.startswith('center/')
                          else self.server.app.agent_access.mutate(peer, action, value))
                return self.send(200, result)
            if path.startswith('/api/cloud/'):
                methods={'login':self.server.app.cloud.start,'poll':self.server.app.cloud.poll,
                         'logout':self.server.app.cloud.logout,'check':self.server.app.cloud.check}
                if value or path[11:] not in methods:raise CloudError('invalid_cloud_action')
                return self.send(200,methods[path[11:]]())
            if path == '/api/project-memory':
                return self.send(200, self.server.app.project_memory.save(value, 'local-owner'))
            if self.path == '/api/agent-access/enroll':
                return self.send(200, self.server.app.agent_access.enroll(value))
            if self.path == '/api/agent-access/revoke':
                return self.send(200, self.server.app.agent_access.revoke(value))
            if self.path == '/api/agent-access/handoff':
                return self.send(200, self.server.app.agent_access.owner_handoff(value))
            if self.path == "/api/chat":
                return self.send(200, {"delivery": self.server.app.submit(value)})
            if self.path == '/api/requirement-dispatch':
                return self.send(200, {'delivery': self.server.app.requirement_deliveries.submit(value)})
            match = re.fullmatch(r'/api/human-requests/([A-Za-z0-9_.:-]{1,100})/decision', self.path)
            if match:
                return self.send(200, self.server.app.humans.decide(match[1], value))
            if self.path == "/api/collaboration/command":
                return self.send(200, self.server.app.product_commands.command(value))
            if self.path == "/api/collaboration/dispatch":
                return self.send(200, self.server.app.product_commands.dispatch(value))
            if self.path == "/api/resources/refresh":
                return self.send(200, {"resources": self.server.app.refresh_resources()})
            if self.path == "/api/resources":
                return self.send(200, {"resource": self.server.app.register_resource(value)})
            if self.path == "/api/management":
                action = value.get("action")
                payload = value.get("payload")
                if not isinstance(action, str) or not isinstance(payload, dict):
                    raise Problem("invalid_management_request", "管理请求需包含动作和对象参数。")
                return self.send(200, {"result": self.server.app.management(action, payload)})
            raise Problem("not_found", "接口不存在。", 404)
        except CloudError as error:
            self.send(error.status,{'error':error.code,'code':error.code})
        except MemoryError as error:
            self.send(error.status, {'code': error.code, 'error': error.code, 'replay_allowed': False})
        except AgentError as error:
            self.send(error.status, {'code': error.code, 'error': error.code, 'replay_allowed': False})
        except RequirementDeliveryError as error:
            self.send(error.status, {'error': '需求投递未获确认，请查询原请求。', 'code': error.code, 'replay_allowed': False})
        except HumanError as error:
            self.send(error.status, {"error": "人工决定尚未确认，请查询原请求。", "code": error.code})
        except ProductCommandError as error:
            self.send(409, {"error": "产品动作状态未确认，请查看原请求记录。", "code": str(error)})
        except Problem as error:
            self.send(error.status, {"error": error.message, "code": error.code})
        except (ConnectionError, TimeoutError):
            self.close_connection = True
        except (ValueError, TypeError):
            self.send(400, {"error": "请求内容无效。", "code": "invalid_json"})
        except Exception:
            self.send(500, {"error": "保存失败，请稍后重试。", "code": "write_failed"})


class InstanceLock:
    """跨进程保护数据目录，第二次启动不能把在途投递当崩溃恢复。"""
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        self.file = (directory / "instance.lock").open("a+b")
        try:
            if os.fstat(self.file.fileno()).st_size == 0:
                self.file.write(b"0"); self.file.flush()
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise Problem("already_running", "该数据目录已由运行中的协作台占用。", 409) from None

    def close(self):
        self.file.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=17910)
    parser.add_argument("--config", type=Path, default=configuration_file())
    parser.add_argument("--data-dir", type=Path, default=shared_directory())
    args = parser.parse_args()
    if args.config == configuration_file():
        initialize_paths()
    try:
        instance = InstanceLock(args.data_dir)
    except Problem as error:
        print(json.dumps({"error": error.message, "code": error.code}, ensure_ascii=False), flush=True)
        return 1
    try:
        config = json.loads(args.config.read_text(encoding="utf-8-sig")) if args.config.exists() else {
            'coordination_mode':'local','os_runtime_registration':False,'hub_reconnect':False,
            'runtime_bindings':[],'human_projects':[], 'resources':[],
            'local_projects':[{'id':'control','name':'Aieyra Control','root':str(ROOT),'source':'docs/START_HERE.md'}]}
        application = Application(config, args.data_dir)
        server = Server(args.port, application)
    except Exception:
        instance.close()
        raise
    thread = threading.Thread(target=application.run, name="control-sync", daemon=True)
    thread.start()
    threading.Thread(target=application.cloud_monitor, name='control-cloud-opt-in', daemon=True).start()
    threading.Thread(target=application.monitor, name="control-monitor", daemon=True).start()
    threading.Thread(target=application.reconnect_hub, name="control-hub-reconnect", daemon=True).start()
    threading.Thread(target=application.monitor_collaboration, name="control-collaboration", daemon=True).start()
    print(json.dumps({"service": "aieyra-control", "url": server.origin, "pid": os.getpid()}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        application.stop.set()
        server.server_close()
        instance.close()


if __name__ == "__main__":
    raise SystemExit(main())
