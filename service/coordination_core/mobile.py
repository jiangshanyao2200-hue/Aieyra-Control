"""Device authentication and durable mobile outbox. No model or shell execution.

The mobile surface is disabled unless the operator configures a canonical HTTPS
origin. Device credentials are distinct from maintainer actors. cryptography is
loaded lazily, so a disabled mobile surface adds no runtime dependency.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
import uuid
from urllib.parse import urlsplit, parse_qs

from .core import Fault, ident, text

ADMIN_ACTIONS = {"device-invite", "device-invite-revoke", "device-revoke", "mobile-host-receipt"}
SCOPES = {"mobile.read", "mobile.dispatch"}
ACCESS_TTL = 900
REFRESH_TTL = 86400
DEVICE_TTL = 30 * 86400
RECOVERY_TTL = 120
PROOF_WINDOW = 120
ACTIVE_DEVICE_LIMIT = 200
FINAL_STATES = {"completed", "interrupted", "cancelled", "failed"}


def digest(value: str | bytes) -> str:
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def unb64(value, maximum=512) -> bytes:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise Fault("invalid_base64url")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError):
        raise Fault("invalid_base64url") from None
    if b64(raw) != value:
        raise Fault("noncanonical_base64url")
    return raw


def origin(value: str) -> str:
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        if (parsed.scheme != "https" or not hostname or parsed.username or parsed.password
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment
                or hostname == "localhost" or hostname.endswith(".localhost")
                or any(ord(c) < 33 or ord(c) > 126 for c in value)):
            raise ValueError()
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            pass
        else:
            raise ValueError()
        if not re.fullmatch(r"[a-z0-9]+(?:[a-z0-9.-]*[a-z0-9])?", hostname):
            raise ValueError()
        port = parsed.port
        if port == 0 or any(not label or len(label) > 63 or label.startswith('-') or label.endswith('-') for label in hostname.split('.')):
            raise ValueError()
        return "https://" + hostname + (":" + str(port) if port and port != 443 else "")
    except (ValueError, TypeError):
        raise Fault("invalid_mobile_origin") from None


def public_key(value):
    from cryptography.hazmat.primitives.serialization import load_der_public_key
    from cryptography.hazmat.primitives.asymmetric import ec
    try:
        raw = unb64(value, 256)
        key = load_der_public_key(raw)
        if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
            raise ValueError()
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        if key.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo) != raw:
            raise ValueError()
        return key, digest(raw)
    except (ValueError, TypeError):
        raise Fault("invalid_device_public_key") from None


def verify(key_text, signature, payload):
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, utils
    try:
        key, _ = public_key(key_text)
        raw = unb64(signature, 128)
        r, s = utils.decode_dss_signature(raw)
        if utils.encode_dss_signature(r, s) != raw:
            raise ValueError()
        key.verify(raw, payload, ec.ECDSA(hashes.SHA256()))
    except (InvalidSignature, ValueError, TypeError, Fault):
        raise Fault("invalid_device_signature", 401) from None


def pair_bytes(server_origin, invitation, request_id):
    return ("AIEYRA-PAIR-1\n" + "\n".join([
        server_origin, invitation["id"], invitation["challenge_id"], invitation["challenge"],
        invitation["device_id"], invitation["fingerprint"], digest(invitation["device_name"]), request_id,
    ])).encode("utf-8")


def proof_bytes(server_origin, method, target, device_id, token_kind, token, stamp, nonce, raw):
    return ("AIEYRA-DEVICE-1\n" + "\n".join([
        server_origin, method, target, device_id, token_kind, digest(token), str(stamp), nonce, digest(raw),
    ])).encode("utf-8")


def fields(body, required, optional=()):
    if not isinstance(body, dict) or set(body) - set(required) - set(optional) or not set(required) <= set(body):
        raise Fault("invalid_device_fields")


def string_list(value, maximum=100):
    if not isinstance(value, list) or not 1 <= len(value) <= maximum:
        raise Fault("invalid_device_scope")
    result = [ident(x) for x in value]
    if len(set(result)) != len(result) or "*" in result:
        raise Fault("invalid_device_scope")
    return sorted(result)


def migrate(d):
    d.executescript("""
    CREATE TABLE IF NOT EXISTS device_invites(
      id TEXT PRIMARY KEY,code_hash TEXT NOT NULL,scopes TEXT NOT NULL,projects TEXT NOT NULL,seats TEXT NOT NULL,
      created_by TEXT NOT NULL,created REAL NOT NULL,expires REAL NOT NULL,revoked INTEGER NOT NULL DEFAULT 0,
      challenge_id TEXT,challenge TEXT,challenge_request TEXT,challenge_digest TEXT,challenge_expires REAL,
      device_id TEXT,device_name TEXT,public_key TEXT,fingerprint TEXT,consumed INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS devices(
      id TEXT PRIMARY KEY,name TEXT NOT NULL,public_key TEXT NOT NULL,fingerprint TEXT NOT NULL,
      scopes TEXT NOT NULL,projects TEXT NOT NULL,seats TEXT NOT NULL,created_by TEXT NOT NULL,
      created REAL NOT NULL,expires REAL NOT NULL,revoked INTEGER NOT NULL DEFAULT 0,version INTEGER NOT NULL DEFAULT 1,
      access_hash TEXT NOT NULL UNIQUE,access_expires REAL NOT NULL,refresh_hash TEXT NOT NULL UNIQUE,
      refresh_expires REAL NOT NULL,credential_generation INTEGER NOT NULL,last_seen REAL NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS device_replays(
      device_id TEXT NOT NULL,request_id TEXT NOT NULL,kind TEXT NOT NULL,digest TEXT NOT NULL,
      prior_refresh_hash TEXT,credential_generation INTEGER NOT NULL,ciphertext TEXT NOT NULL,expires REAL NOT NULL,
      PRIMARY KEY(device_id,request_id));
    CREATE TABLE IF NOT EXISTS device_nonces(
      device_id TEXT NOT NULL,nonce TEXT NOT NULL,expires REAL NOT NULL,PRIMARY KEY(device_id,nonce));
    CREATE TABLE IF NOT EXISTS device_limits(bucket TEXT PRIMARY KEY,window INTEGER NOT NULL,hits INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS device_requests(
      device_id TEXT NOT NULL,request_id TEXT NOT NULL,digest TEXT NOT NULL,response TEXT NOT NULL,
      PRIMARY KEY(device_id,request_id));
    CREATE TABLE IF NOT EXISTS device_events(
      seq INTEGER PRIMARY KEY AUTOINCREMENT,device_id TEXT NOT NULL,kind TEXT NOT NULL,object_id TEXT NOT NULL,created REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS mobile_deliveries(
      id TEXT PRIMARY KEY,device_id TEXT NOT NULL REFERENCES devices(id),seat_id TEXT NOT NULL REFERENCES seats(id),
      host_id TEXT NOT NULL,seat_epoch INTEGER NOT NULL,runtime_generation TEXT NOT NULL,body TEXT NOT NULL,
      body_hash TEXT NOT NULL,state TEXT NOT NULL,turn_id TEXT,reply TEXT,evidence TEXT NOT NULL DEFAULT '',
      cancel_requested INTEGER NOT NULL DEFAULT 0,version INTEGER NOT NULL DEFAULT 1,
      received_at REAL,started_at REAL,
      created REAL NOT NULL,updated REAL NOT NULL,expires REAL NOT NULL);
    CREATE INDEX IF NOT EXISTS mobile_outbox_host ON mobile_deliveries(host_id,state,created);
    CREATE INDEX IF NOT EXISTS mobile_outbox_device ON mobile_deliveries(device_id,created);
    CREATE INDEX IF NOT EXISTS mobile_events_device ON device_events(device_id,seq);
    """)
    columns = {row[1] for row in d.execute('PRAGMA table_info(mobile_deliveries)')}
    for name in ('received_at', 'started_at'):
        if name not in columns:
            d.execute('ALTER TABLE mobile_deliveries ADD COLUMN ' + name + ' REAL')


def event(d, device_id, kind, object_id, now):
    d.execute("INSERT INTO device_events(device_id,kind,object_id,created) VALUES(?,?,?,?)",
              (device_id, kind, object_id, now))


def revoke(d, device_id, now):
    if not d.execute("UPDATE devices SET revoked=1,version=version+1 WHERE id=? AND revoked=0", (device_id,)).rowcount:
        if not d.execute("SELECT 1 FROM devices WHERE id=?", (device_id,)).fetchone():
            raise Fault("device_missing", 404)
    rows = d.execute("SELECT * FROM mobile_deliveries WHERE device_id=? AND state NOT IN ('completed','failed','interrupted','cancelled')", (device_id,)).fetchall()
    for item in rows:
        state = "cancelled" if item["state"] == "pending" else "unknown"
        d.execute("UPDATE mobile_deliveries SET state=?,cancel_requested=1,version=version+1,updated=? WHERE id=?",
                  (state, now, item["id"]))
        event(d, device_id, "delivery." + state, item["id"], now)
    d.execute("DELETE FROM device_replays WHERE device_id=?", (device_id,))
    event(d, device_id, "device.revoked", device_id, now)
    return {"device_id": device_id, "state": "revoked"}


def invalidate_restored_devices(d, now):
    """Run only on an offline restored copy, before it may serve requests."""
    rows = d.execute('SELECT id FROM devices').fetchall()
    for row in rows:
        revoke(d, row['id'], now)
    d.execute('UPDATE device_invites SET revoked=1')
    d.execute('DELETE FROM device_replays')
    return {'revoked_devices': len(rows), 'repair_required': True}


def admin_write(d, actor, action, body, now):
    if action == "mobile-host-receipt":
        return host_receipt(d, actor, body, now)
    if actor["role"] != "owner":
        raise Fault("owner_required", 403)
    if action == "device-invite":
        scopes = string_list(body.get("scopes", ["mobile.read"]))
        if not set(scopes) <= SCOPES or "mobile.read" not in scopes:
            raise Fault("invalid_device_scope")
        projects = string_list(body.get("projects"))
        for project in projects:
            if not d.execute("SELECT 1 FROM registered_projects WHERE id=?", (project,)).fetchone():
                raise Fault("unknown_project")
        seats = string_list(body["seats"]) if body.get("seats") else []
        for seat in seats:
            row = d.execute("SELECT project FROM seats WHERE id=?", (seat,)).fetchone()
            if not row or row["project"] not in projects:
                raise Fault("device_seat_scope_denied", 403)
        if "mobile.dispatch" in scopes and not seats:
            raise Fault("dispatch_seat_allowlist_required")
        if d.execute('SELECT count(*) FROM devices WHERE revoked=0 AND expires>?', (now,)).fetchone()[0] >= ACTIVE_DEVICE_LIMIT:
            raise Fault('active_device_capacity', 429)
        ttl = body.get("expires_in", 600)
        if type(ttl) is not int or not 60 <= ttl <= 600:
            raise Fault("invalid_invite_ttl")
        if d.execute("SELECT count(*) FROM device_invites WHERE expires>? AND consumed=0 AND revoked=0", (now,)).fetchone()[0] >= 20:
            raise Fault("too_many_active_invites", 429)
        invitation, code = "invite-" + uuid.uuid4().hex, secrets.token_urlsafe(24)
        d.execute("INSERT INTO device_invites(id,code_hash,scopes,projects,seats,created_by,created,expires) VALUES(?,?,?,?,?,?,?,?)",
                  (invitation, digest(code), canonical(scopes), canonical(projects), canonical(seats), actor["id"], now, now + ttl))
        return {"invite_id": invitation, "code": code, "expires_at": now + ttl, "scopes": scopes, "projects": projects, "seats": seats}
    if action == "device-invite-revoke":
        iid = ident(body.get("invite_id"))
        if not d.execute("UPDATE device_invites SET revoked=1 WHERE id=?", (iid,)).rowcount:
            raise Fault("invite_missing", 404)
        return {"invite_id": iid, "state": "revoked"}
    if action == "device-revoke":
        return revoke(d, ident(body.get("device_id")), now)
    raise Fault("not_found", 404)


def device_list(d, actor):
    if actor["role"] != "owner":
        raise Fault("owner_required", 403)
    result = []
    for row in d.execute("SELECT id,name,fingerprint,scopes,projects,seats,created,expires,revoked,version,last_seen FROM devices ORDER BY created DESC LIMIT 200"):
        value = dict(row)
        for key in ("scopes", "projects", "seats"):
            value[key] = json.loads(value[key])
        value["state"] = "revoked" if value["revoked"] else "expired" if value["expires"] <= time.time() else "paired"
        result.append(value)
    return {"devices": result}


def generation(seat):
    return digest(str(seat["epoch"]) + "\n" + seat["runtime_ref"])


def runtime_ready(d, seat, now, *, required_action="dispatch"):
    if not seat or not seat['runtime_ref'] or seat['desired_state'] in ('paused', 'closed'):
        return False
    if seat['observed_state'] not in ('idle', 'running') or not 0 <= now - seat['observed_at'] < 180:
        return False
    host = d.execute('SELECT * FROM registered_hosts WHERE id=?', (seat['host_id'],)).fetchone()
    adapter = d.execute('SELECT * FROM registered_adapters WHERE id=?', (seat['adapter_id'],)).fetchone()
    if (not host or not 0 <= now - host['last_seen'] < 180 or not adapter
            or adapter['availability'] != 'available' or required_action not in json.loads(adapter['actions_json'])
            or not 0 <= now - adapter['observed_at'] < 180):
        return False
    for aid in (host['actor_id'], seat['actor_id']):
        if aid and not d.execute('SELECT 1 FROM actors WHERE id=? AND revoked=0', (aid,)).fetchone():
            return False
    return not d.execute("SELECT 1 FROM seat_operations WHERE seat_id=? AND state IN ('pending','accepted','unknown')", (seat['id'],)).fetchone()


def issuer_active(d, device):
    return bool(d.execute("SELECT 1 FROM actors WHERE id=? AND role='owner' AND revoked=0", (device['created_by'],)).fetchone())


def authorized_delivery(d, item, now):
    device = d.execute("SELECT * FROM devices WHERE id=?", (item["device_id"],)).fetchone()
    seat = d.execute("SELECT * FROM seats WHERE id=?", (item["seat_id"],)).fetchone()
    return bool(device and not device["revoked"] and device["expires"] > now and issuer_active(d, device)
                and "mobile.dispatch" in json.loads(device["scopes"])
                and item["seat_id"] in json.loads(device["seats"])
                and seat and seat["project"] in json.loads(device["projects"])
                and seat["epoch"] == item["seat_epoch"] and generation(seat) == item["runtime_generation"]
                and seat['host_id'] == item['host_id'] and runtime_ready(d, seat, now)
                and not item["cancel_requested"] and item["expires"] > now)


def expire_deliveries(d, now, device_id=None, host_id=None):
    # A timeout cancels unsent work, but cannot prove an in-flight turn stopped.
    clause, args = ('device_id=?', [device_id]) if device_id else ('host_id=?', [host_id])
    for item in d.execute("SELECT * FROM mobile_deliveries WHERE " + clause + " AND expires<=? AND cancel_requested=0 AND state NOT IN ('completed','failed','interrupted','cancelled')", (*args, now)).fetchall():
        state = 'cancelled' if item['state'] == 'pending' else 'unknown'
        d.execute('UPDATE mobile_deliveries SET state=?,cancel_requested=1,version=version+1,updated=? WHERE id=?', (state, now, item['id']))
        event(d, item['device_id'], 'delivery.' + state, item['id'], now)


def host_outbox(d, actor, host_id):
    host_id = ident(host_id)
    host = d.execute("SELECT * FROM registered_hosts WHERE id=?", (host_id,)).fetchone()
    if not host or actor["id"] != host["actor_id"]:
        raise Fault("host_identity_required", 403)
    now = time.time()
    expire_deliveries(d, now, host_id=host_id)
    items = []
    for row in d.execute("SELECT * FROM mobile_deliveries WHERE host_id=? AND state NOT IN ('completed','failed','interrupted','cancelled') ORDER BY created LIMIT 100", (host_id,)):
        value = dict(row)
        value["requester_authorized"] = authorized_delivery(d, row, now)
        seat = d.execute('SELECT * FROM seats WHERE id=?', (row['seat_id'],)).fetchone()
        value['runtime_ref'] = seat['runtime_ref'] if seat and generation(seat) == row['runtime_generation'] else None
        value['adapter_id'] = seat['adapter_id'] if seat else None
        if not value["requester_authorized"]:
            value.pop("body", None)
        items.append(value)
    return {"host_id": host_id, "deliveries": items}


def host_receipt(d, actor, body, now):
    fields(body, {'request_id','delivery_id','version','seat_epoch','runtime_generation','state','evidence'}, {'turn_id','body_hash','reply'})
    item = d.execute("SELECT * FROM mobile_deliveries WHERE id=?", (ident(body.get("delivery_id")),)).fetchone()
    if not item:
        raise Fault("delivery_missing", 404)
    host = d.execute("SELECT actor_id FROM registered_hosts WHERE id=?", (item["host_id"],)).fetchone()
    if not host or actor["id"] != host["actor_id"]:
        raise Fault("host_identity_required", 403)
    if type(body.get("version")) is not int or body["version"] != item["version"]:
        raise Fault("version_conflict", 409)
    if (body.get("seat_epoch") != item["seat_epoch"] or type(body.get("seat_epoch")) is not int
            or body.get("runtime_generation") != item["runtime_generation"]):
        raise Fault("stale_runtime_generation", 409)
    transitions = {
        "pending": {"accepted", "cancelled", "failed"},
        "accepted": {"queued", "unknown", "cancelled", "failed"},
        "queued": {"received", "unknown", "cancelled"},
        "received": {"started", "unknown", "interrupted"},
        "started": {"completed", "interrupted", "unknown"},
        "unknown": {"received", "started", "completed", "interrupted", "cancelled", "failed"},
    }
    state = body.get("state")
    if state not in transitions.get(item["state"], set()):
        raise Fault("receipt_transition_conflict", 409)
    authorized = authorized_delivery(d, item, now)
    if not authorized and state not in ("unknown", "cancelled", "failed", "completed", "interrupted"):
        raise Fault("delivery_authorization_revoked", 403)
    evidence = text(body.get("evidence"), 1500)
    turn_id = item['turn_id']
    if state in ('accepted', 'queued') and body.get('body_hash') != item['body_hash']:
        raise Fault('delivery_content_mismatch', 409)
    if state in ("received", "started", "completed", "interrupted"):
        turn_id = ident(body.get('turn_id', turn_id))
        if body.get("body_hash") != item["body_hash"]:
            raise Fault("delivery_content_mismatch", 409)
        if item["turn_id"] and item["turn_id"] != turn_id:
            raise Fault("delivery_turn_mismatch", 409)
        if state in ("started", "completed", "interrupted") and not item["received_at"]:
            raise Fault("received_turn_required", 409)
        if state == 'completed' and not item['started_at']:
            raise Fault('started_turn_required', 409)
    elif 'turn_id' in body:
        raise Fault('turn_only_with_runtime_receipt')
    reply = body.get("reply")
    if reply is not None:
        if state != "completed":
            raise Fault("completed_reply_required")
        reply = text(reply, 16000, empty=True)
    d.execute("UPDATE mobile_deliveries SET state=?,turn_id=?,reply=?,evidence=?,version=version+1,updated=? WHERE id=?",
              (state, turn_id, reply, evidence, now, item["id"]))
    if state == 'received':
        d.execute('UPDATE mobile_deliveries SET received_at=COALESCE(received_at,?) WHERE id=?', (now,item['id']))
    if state == 'started':
        d.execute('UPDATE mobile_deliveries SET started_at=COALESCE(started_at,?) WHERE id=?', (now,item['id']))
    event(d, item["device_id"], "delivery." + state, item["id"], now)
    return {"id": item["id"], "state": state, "version": item["version"] + 1, "requester_authorized": authorized}


class Mobile:
    def __init__(self, store, server_origin):
        self.store = store
        self.origin = origin(server_origin) if server_origin else None
        self._cipher = None
        self._key_lock = threading.Lock()

    def enabled(self):
        if not self.origin:
            raise Fault("mobile_not_configured", 503)
        try:
            from cryptography.fernet import Fernet
        except ImportError:
            raise Fault("mobile_dependency_unavailable", 503) from None
        return Fernet

    def cipher(self):
        Fernet = self.enabled()
        with self._key_lock:
            if self._cipher is None:
                path = self.store.path.with_name("mobile-credentials.key")
                if not path.exists():
                    key = Fernet.generate_key()
                    try:
                        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    except FileExistsError:
                        pass
                    else:
                        with os.fdopen(descriptor, "wb") as file:
                            file.write(key)
                            file.flush()
                            os.fsync(file.fileno())
                if path.is_symlink() or (os.name != "nt" and path.stat().st_mode & 0o077):
                    raise Fault("mobile_key_permissions", 503)
                try:
                    self._cipher = Fernet(path.read_bytes())
                except (OSError, ValueError):
                    raise Fault('mobile_recovery_key_unavailable', 503) from None
            return self._cipher

    def rate(self, peer, limit=60):
        now = int(time.time())
        bucket = digest(str(peer))
        window = now // 60
        with self.store.db() as d:
            d.execute("BEGIN IMMEDIATE")
            d.execute("DELETE FROM device_limits WHERE window<?", (window - 2,))
            row = d.execute("SELECT * FROM device_limits WHERE bucket=?", (bucket,)).fetchone()
            count = row["hits"] + 1 if row and row["window"] == window else 1
            d.execute("INSERT INTO device_limits VALUES(?,?,?) ON CONFLICT(bucket) DO UPDATE SET window=excluded.window,hits=excluded.hits", (bucket, window, count))
        if count > limit:
            raise Fault("device_rate_limited", 429)

    def handle(self, method, target, headers, raw=b"", peer="loopback"):
        self.enabled()
        self.rate(peer)
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc or parsed.fragment or any(ord(c) < 33 or ord(c) > 126 for c in target):
            raise Fault("invalid_device_path")
        pairs = parse_qs(parsed.query, keep_blank_values=True)
        if any(len(v) != 1 for v in pairs.values()):
            raise Fault("invalid_device_query")
        if method == "POST":
            if len(raw) > 32768:
                raise Fault("body_limit", 413)
            try:
                def unique(items):
                    result = {}
                    for key, value in items:
                        if key in result:
                            raise ValueError()
                        result[key] = value
                    return result
                body = json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                                  parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            except (ValueError, UnicodeError, RecursionError):
                raise Fault("invalid_json") from None
            if not isinstance(body, dict) or pairs:
                raise Fault("invalid_device_fields")
        elif method == "GET" and not raw:
            body = {}
        else:
            raise Fault("device_method_not_allowed", 405)
        with self.store.db() as d:
            d.execute("BEGIN IMMEDIATE")
            now = time.time()
            d.execute("DELETE FROM device_nonces WHERE expires<?", (now,))
            d.execute("DELETE FROM device_replays WHERE expires<?", (now,))
            if method == "POST" and parsed.path == "/v1/device/pair/challenge":
                return self.challenge(d, body, now)
            if method == "POST" and parsed.path == "/v1/device/pair":
                return self.pair(d, body, now)
            routes = {("GET", "/v1/mobile/snapshot"), ("GET", "/v1/mobile/delivery"),
                      ("GET", "/v1/mobile/events"), ("POST", "/v1/mobile/dispatch"),
                      ("POST", "/v1/device/refresh"), ("POST", "/v1/device/revoke")}
            if (method, parsed.path) not in routes:
                raise Fault("device_endpoint_denied", 403)
            if method == "GET":
                allowed = {"/v1/mobile/snapshot": set(), "/v1/mobile/delivery": {"id"}, "/v1/mobile/events": {"after"}}[parsed.path]
                if set(pairs) - allowed:
                    raise Fault("invalid_device_query")
            is_refresh = parsed.path == "/v1/device/refresh"
            device, credential = self.authenticate(d, method, target, headers, raw, body, now, is_refresh)
            expire_deliveries(d, now, device_id=device['id'])
            d.execute("UPDATE devices SET last_seen=? WHERE id=?", (now, device["id"]))
            if is_refresh:
                return self.refresh(d, device, credential, body, now)
            if "mobile.read" not in json.loads(device["scopes"]):
                raise Fault("device_scope_denied", 403)
            if method == "GET":
                return self.read(d, device, parsed.path, pairs, now)
            if parsed.path == "/v1/device/revoke":
                fields(body, {"request_id"})
                ident(body["request_id"])
                return revoke(d, device["id"], now)
            return self.dispatch(d, device, body, now)

    def challenge(self, d, body, now):
        fields(body, {"request_id", "invite_id", "code", "device_id", "device_name", "public_key"})
        rid, iid, did = (ident(body[k]) for k in ("request_id", "invite_id", "device_id"))
        invitation = d.execute("SELECT * FROM device_invites WHERE id=?", (iid,)).fetchone()
        if (not invitation or invitation["revoked"] or invitation["consumed"] or invitation["expires"] <= now or not issuer_active(d, invitation)
                or not isinstance(body["code"], str) or len(body["code"]) > 100
                or not hmac.compare_digest(invitation["code_hash"], digest(body["code"]))):
            raise Fault("invalid_or_expired_invitation", 401)
        _, fingerprint = public_key(body["public_key"])
        name = text(body["device_name"], 80)
        request_digest = digest(canonical({**body, "device_name": name}))
        if invitation["challenge_id"]:
            if invitation["challenge_request"] != rid or invitation["challenge_digest"] != request_digest:
                raise Fault("invitation_already_bound", 409)
            if invitation["challenge_expires"] <= now:
                raise Fault("pair_challenge_expired", 409)
        else:
            if d.execute("SELECT 1 FROM devices WHERE id=?", (did,)).fetchone():
                raise Fault("device_id_unavailable", 409)
            d.execute("UPDATE device_invites SET challenge_id=?,challenge=?,challenge_request=?,challenge_digest=?,challenge_expires=?,device_id=?,device_name=?,public_key=?,fingerprint=? WHERE id=?",
                      ("challenge-" + uuid.uuid4().hex, secrets.token_urlsafe(32), rid, request_digest, min(now + 180, invitation["expires"]),
                       did, name, body["public_key"], fingerprint, iid))
            invitation = d.execute("SELECT * FROM device_invites WHERE id=?", (iid,)).fetchone()
        return {"invite_id": iid, "challenge_id": invitation["challenge_id"], "challenge": invitation["challenge"],
                "device_id": did, "key_fingerprint": fingerprint, "device_name_hash": digest(name),
                "expires_at": invitation["challenge_expires"], "server_time": now, "origin": self.origin}

    def pair(self, d, body, now):
        fields(body, {"request_id", "invite_id", "challenge_id", "device_id", "signature"})
        rid, iid, did = (ident(body[k]) for k in ("request_id", "invite_id", "device_id"))
        invitation = d.execute("SELECT * FROM device_invites WHERE id=?", (iid,)).fetchone()
        if (not invitation or invitation["revoked"] or not invitation["challenge_id"] or not issuer_active(d, invitation)
                or invitation["device_id"] != did or invitation["challenge_id"] != body["challenge_id"]):
            raise Fault("invalid_pair_challenge", 401)
        verify(invitation["public_key"], body["signature"], pair_bytes(self.origin, invitation, rid))
        fingerprint = digest(canonical({k: v for k, v in body.items() if k != "signature"}))
        if invitation["consumed"]:
            device = d.execute("SELECT * FROM devices WHERE id=?", (did,)).fetchone()
            if not device or device["revoked"] or device["expires"] <= now:
                raise Fault("device_revoked_or_expired", 401)
            return self.recover(d, device, rid, "pair", fingerprint, now)
        if invitation["expires"] <= now or invitation["challenge_expires"] <= now:
            raise Fault("pair_challenge_expired", 401)
        # An invitation may predate other pairings. Enforce capacity again in
        # the consuming transaction; existing pair recovery remains available.
        if d.execute('SELECT count(*) FROM devices WHERE revoked=0 AND expires>?', (now,)).fetchone()[0] >= ACTIVE_DEVICE_LIMIT:
            raise Fault('active_device_capacity', 429)
        access, refresh = "da_" + secrets.token_urlsafe(32), "dr_" + secrets.token_urlsafe(32)
        try:
            d.execute("INSERT INTO devices(id,name,public_key,fingerprint,scopes,projects,seats,created_by,created,expires,access_hash,access_expires,refresh_hash,refresh_expires,credential_generation) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                      (did, invitation["device_name"], invitation["public_key"], invitation["fingerprint"],
                       invitation["scopes"], invitation["projects"], invitation["seats"], invitation["created_by"], now,
                       now + DEVICE_TTL, digest(access), now + ACCESS_TTL, digest(refresh), now + REFRESH_TTL))
        except sqlite3.IntegrityError:
            raise Fault("device_id_unavailable", 409) from None
        d.execute("UPDATE device_invites SET consumed=1 WHERE id=?", (iid,))
        response = {"device_id": did, "access_token": access, "refresh_token": refresh, "token_type": "Device",
                    "access_expires_at": now + ACCESS_TTL, "refresh_expires_at": now + REFRESH_TTL,
                    "device_expires_at": now + DEVICE_TTL, "credential_generation": 1,
                    "scopes": json.loads(invitation["scopes"]), "projects": json.loads(invitation["projects"]), "seats": json.loads(invitation["seats"])}
        self.save_replay(d, did, rid, "pair", fingerprint, None, response, now)
        event(d, did, "device.paired", did, now)
        return response

    def save_replay(self, d, did, rid, kind, fingerprint, prior_hash, response, now):
        encrypted = self.cipher().encrypt(canonical(response).encode("utf-8")).decode("ascii")
        d.execute("INSERT INTO device_replays VALUES(?,?,?,?,?,?,?,?)",
                  (did, rid, kind, fingerprint, prior_hash, response["credential_generation"], encrypted, now + RECOVERY_TTL))

    def recover(self, d, device, rid, kind, fingerprint, now):
        row = d.execute("SELECT * FROM device_replays WHERE device_id=? AND request_id=?", (device["id"], rid)).fetchone()
        if not row or row["expires"] <= now:
            raise Fault("credential_replay_expired_repair_required", 409)
        if row["kind"] != kind or row["digest"] != fingerprint:
            raise Fault("idempotency_conflict", 409)
        if row["credential_generation"] != device["credential_generation"]:
            raise Fault("credential_generation_changed", 409)
        from cryptography.fernet import InvalidToken
        try:
            return json.loads(self.cipher().decrypt(row["ciphertext"].encode("ascii")))
        except (InvalidToken, ValueError):
            raise Fault('credential_recovery_unavailable', 503) from None

    def authenticate(self, d, method, target, headers, raw, body, now, refresh):
        headers = {key.lower(): value for key, value in headers.items()}
        prefix = "DeviceRefresh " if refresh else "Device "
        authorization = headers.get("authorization", "")
        if not authorization.startswith(prefix) or len(authorization) > 200:
            raise Fault("device_unauthorized", 401)
        token = authorization[len(prefix):]
        did = ident(headers.get("x-device-id"))
        device = d.execute("SELECT * FROM devices WHERE id=?", (did,)).fetchone()
        if not device or device["revoked"] or device["expires"] <= now or not issuer_active(d, device):
            raise Fault("device_revoked_or_expired", 401)
        token_hash = digest(token)
        if refresh:
            fields(body, {"request_id"})
            rid = ident(body["request_id"])
            if token_hash != device["refresh_hash"]:
                old = d.execute("SELECT * FROM device_replays WHERE device_id=? AND request_id=? AND kind='refresh'", (did, rid)).fetchone()
                if not old or old["prior_refresh_hash"] != token_hash or old["expires"] <= now or old["credential_generation"] != device["credential_generation"]:
                    raise Fault("refresh_rejected", 401)
            elif device["refresh_expires"] <= now:
                raise Fault("refresh_expired_repair_required", 401)
        elif token_hash != device["access_hash"] or device["access_expires"] <= now:
            raise Fault("device_access_expired", 401)
        stamp = headers.get("x-device-timestamp", "")
        nonce = headers.get("x-device-nonce", "")
        if not re.fullmatch(r"[0-9]{10}", stamp) or abs(now - int(stamp)) > PROOF_WINDOW:
            raise Fault("device_proof_expired", 401)
        if not 16 <= len(unb64(nonce, 86)) <= 64:
            raise Fault("invalid_device_nonce")
        verify(device["public_key"], headers.get("x-device-signature"),
               proof_bytes(self.origin, method, target, did, "refresh" if refresh else "access", token, stamp, nonce, raw))
        try:
            d.execute("INSERT INTO device_nonces VALUES(?,?,?)", (did, nonce, now + 2 * PROOF_WINDOW))
        except sqlite3.IntegrityError:
            raise Fault("device_nonce_replayed", 409) from None
        return device, token_hash

    def refresh(self, d, device, prior_hash, body, now):
        rid = ident(body["request_id"])
        fingerprint = digest(canonical(body))
        old = d.execute("SELECT * FROM device_replays WHERE device_id=? AND request_id=?", (device["id"], rid)).fetchone()
        if old:
            if old["prior_refresh_hash"] != prior_hash:
                raise Fault("idempotency_conflict", 409)
            return self.recover(d, device, rid, "refresh", fingerprint, now)
        if d.execute('SELECT 1 FROM device_requests WHERE device_id=? AND request_id=?', (device['id'],rid)).fetchone():
            raise Fault('credential_replay_expired_repair_required',409)
        self.request_capacity(d, device)
        access, refresh = "da_" + secrets.token_urlsafe(32), "dr_" + secrets.token_urlsafe(32)
        access_expires, refresh_expires = min(now + ACCESS_TTL, device["expires"]), min(now + REFRESH_TTL, device["expires"])
        response = {"device_id": device["id"], "access_token": access, "refresh_token": refresh, "token_type": "Device",
                    "access_expires_at": access_expires, "refresh_expires_at": refresh_expires,
                    "device_expires_at": device["expires"], "credential_generation": device["credential_generation"] + 1}
        d.execute("UPDATE devices SET access_hash=?,access_expires=?,refresh_hash=?,refresh_expires=?,credential_generation=credential_generation+1 WHERE id=?",
                  (digest(access), access_expires, digest(refresh), refresh_expires, device["id"]))
        self.save_replay(d, device["id"], rid, "refresh", fingerprint, prior_hash, response, now)
        d.execute('INSERT INTO device_requests VALUES(?,?,?,?)', (device['id'],rid,fingerprint,canonical({'kind':'refresh','credential_generation':response['credential_generation']})))
        event(d, device["id"], "device.refreshed", device["id"], now)
        return response

    def read(self, d, device, path, query, now):
        if path == "/v1/mobile/delivery":
            item = d.execute("SELECT * FROM mobile_deliveries WHERE id=? AND device_id=?", (ident(query.get("id", [None])[0]), device["id"])).fetchone()
            if not item:
                raise Fault("delivery_missing", 404)
            return {"delivery": self.delivery_view(item)}
        if path == "/v1/mobile/events":
            cursor = query.get("after", ["0"])[0]
            if not re.fullmatch(r"[0-9]{1,18}", cursor):
                raise Fault("invalid_cursor")
            rows = [dict(x) for x in d.execute("SELECT seq,kind,object_id,created FROM device_events WHERE device_id=? AND seq>? ORDER BY seq LIMIT 101", (device["id"], int(cursor)))]
            return {"events": rows[:100], "next_cursor": rows[min(99, len(rows) - 1)]["seq"] if rows else int(cursor), "has_more": len(rows) > 100}
        projects = json.loads(device["projects"])
        placeholders = ",".join("?" for _ in projects)
        project_rows = [dict(x) for x in d.execute("SELECT id,name,version FROM registered_projects WHERE id IN (" + placeholders + ") ORDER BY id", projects)]
        seats = []
        for row in d.execute("SELECT * FROM seats WHERE project IN (" + placeholders + ") ORDER BY id LIMIT 200", projects):
            v = {k: row[k] for k in ("id", "name", "project", "epoch", "version", "desired_state", "observed_at")}
            v.update(observed_state=row["observed_state"] if 0 <= now - row["observed_at"] < 180 else "unknown",
                     runtime_generation=generation(row), runtime_bound=bool(row["runtime_ref"]),
                     dispatch_allowed="mobile.dispatch" in json.loads(device["scopes"]) and row["id"] in json.loads(device["seats"]))
            v['runtime_ready'] = runtime_ready(d,row,now)
            v['can_dispatch'] = v['dispatch_allowed'] and v['runtime_ready']
            seats.append(v)
        tasks = [dict(x) for x in d.execute("SELECT id,project,title,status,version,updated FROM tasks WHERE project IN (" + placeholders + ") ORDER BY updated DESC LIMIT 100", projects)]
        deliveries = [self.delivery_view(x) for x in d.execute("SELECT * FROM mobile_deliveries WHERE device_id=? ORDER BY created DESC LIMIT 100", (device["id"],))]
        return {"schema_version": 1, "observed_at": now, "projects": project_rows, "seats": seats, "tasks": tasks,
                "deliveries": deliveries, "device": {"id": device["id"], "scopes": json.loads(device["scopes"]), "expires_at": device["expires"]},
                "capabilities": {"snapshot": True, "dispatch": "mobile.dispatch" in json.loads(device["scopes"]),
                                 "execution_in_center": False, "maintenance_api": False, "app_update": False}}

    @staticmethod
    def delivery_view(item):
        return {k: item[k] for k in ("id", "seat_id", "state", "turn_id", "reply", "cancel_requested", "version", "created", "updated", "expires")}

    def dispatch(self, d, device, body, now):
        fields(body, {"request_id", "target_seat_id", "seat_epoch", "runtime_generation", "body"})
        rid = ident(body["request_id"])
        if "mobile.dispatch" not in json.loads(device["scopes"]):
            raise Fault("device_scope_denied", 403)
        fingerprint = digest(canonical({"path": "/v1/mobile/dispatch", "body": body}))
        old = d.execute("SELECT * FROM device_requests WHERE device_id=? AND request_id=?", (device["id"], rid)).fetchone()
        if old:
            if old["digest"] != fingerprint:
                raise Fault("idempotency_conflict", 409)
            return json.loads(old["response"])
        self.request_capacity(d, device)
        seat_id = ident(body["target_seat_id"])
        seat = d.execute("SELECT * FROM seats WHERE id=?", (seat_id,)).fetchone()
        if not seat or seat_id not in json.loads(device["seats"]) or seat["project"] not in json.loads(device["projects"]):
            raise Fault("device_seat_scope_denied", 403)
        if type(body["seat_epoch"]) is not int or seat["epoch"] != body["seat_epoch"] or generation(seat) != body["runtime_generation"]:
            raise Fault("stale_runtime_generation", 409)
        if not runtime_ready(d,seat,now):
            raise Fault("runtime_dispatch_unavailable", 409)
        if d.execute("SELECT count(*) FROM mobile_deliveries WHERE device_id=? AND state NOT IN ('completed','failed','interrupted','cancelled')", (device['id'],)).fetchone()[0] >= 20:
            raise Fault('device_backlog_full',429)
        if d.execute("SELECT count(*) FROM mobile_deliveries WHERE seat_id=? AND state NOT IN ('completed','failed','interrupted','cancelled')", (seat_id,)).fetchone()[0] >= 20:
            raise Fault('seat_backlog_full',429)
        if d.execute('SELECT count(*) FROM mobile_deliveries WHERE device_id=? AND created>?', (device['id'],now-3600)).fetchone()[0] >= 120:
            raise Fault('device_dispatch_rate_limited',429)
        content = text(body["body"], 4000)
        delivery_id = "delivery-" + uuid.uuid4().hex
        d.execute("INSERT INTO mobile_deliveries(id,device_id,seat_id,host_id,seat_epoch,runtime_generation,body,body_hash,state,created,updated,expires) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                  (delivery_id, device["id"], seat_id, seat["host_id"], seat["epoch"], generation(seat), content, digest(content), "pending", now, now, now + 600))
        response = {"delivery": {"id": delivery_id, "state": "pending", "version": 1}}
        d.execute("INSERT INTO device_requests VALUES(?,?,?,?)", (device["id"], rid, fingerprint, canonical(response)))
        event(d, device["id"], "delivery.pending", delivery_id, now)
        return response

    @staticmethod
    def request_capacity(d, device):
        # Never purge deduplication records while a device credential is valid.
        if d.execute('SELECT count(*) FROM device_requests WHERE device_id=?', (device['id'],)).fetchone()[0] >= 10000:
            raise Fault('device_request_capacity_repair_required', 429)
