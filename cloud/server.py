"""Control's explicit public community and release service. No project ingestion."""

from __future__ import annotations
import base64
import hashlib
import hmac
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import time
import ssl
import sys
import threading
from contextlib import contextmanager
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlencode
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "service"))
from feedback_store import FeedbackStore
from feedback_contract import FeedbackError

SITE = "https://ctrl.aieyra.cn"
CLOUD = "https://ctrlupdate.aieyra.cn"
API = "https://api.aieyra.cn"
CALLBACKS = {SITE + "/auth/callback", CLOUD + "/auth/callback"}
SESSION_COOKIE = "__Host-control-session"
SOURCE_REPOSITORY = "https://github.com/jiangshanyao2200-hue/Aieyra-Control"


class Error(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def challenge(value):
    return base64.urlsafe_b64encode(hashlib.sha256(value.encode()).digest()).decode().rstrip("=")


def check(value, pattern=r"[A-Za-z0-9_-]{32,128}"):
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise Error("invalid_input")
    return value


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Cloud(FeedbackStore):
    def __init__(self, data, identity=None):
        self.data = Path(data)
        self.data.mkdir(parents=True, exist_ok=True)
        self.identity = identity or self.newapi_identity
        with self.db() as d:
            d.executescript("""
            CREATE TABLE IF NOT EXISTS flows(id TEXT PRIMARY KEY,challenge TEXT NOT NULL,redirect TEXT NOT NULL,scope TEXT NOT NULL,state TEXT NOT NULL,expires REAL NOT NULL,code_hash TEXT UNIQUE,identity TEXT,used INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS sessions(hash TEXT PRIMARY KEY,subject TEXT NOT NULL,name TEXT NOT NULL,scope TEXT NOT NULL,expires REAL NOT NULL,revoked INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS posts(seq INTEGER PRIMARY KEY AUTOINCREMENT,subject TEXT NOT NULL,agent TEXT NOT NULL,body TEXT NOT NULL,created REAL NOT NULL,hidden INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS requests(subject TEXT NOT NULL,id TEXT NOT NULL,digest TEXT NOT NULL,seq INTEGER NOT NULL,PRIMARY KEY(subject,id));
            CREATE TABLE IF NOT EXISTS limits(key TEXT PRIMARY KEY,start REAL NOT NULL,count INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS blocked(subject TEXT PRIMARY KEY,reason TEXT NOT NULL);
            """)
        self.init_feedback()
        self.rate_lock = threading.Lock()
        self.rate_buckets = {}
        self.connection_counts = {}
        self.trusted_proxies = {
            v.strip() for v in os.environ.get("CONTROL_TRUSTED_PROXIES", "").split(",") if v.strip()
        }

    def ingress_limit(self, key, count, seconds):
        # Reject abusive traffic before SQLite/auth; bounded memory even across IPs.
        now = time.monotonic()
        with self.rate_lock:
            old = self.rate_buckets.get(key)
            if old and now - old[0] < seconds:
                if old[1] >= count:
                    raise Error("rate_limited", 429)
                self.rate_buckets[key] = (old[0], old[1] + 1)
                return
            if len(self.rate_buckets) >= 8192:
                self.rate_buckets = {k: v for k, v in self.rate_buckets.items() if now - v[0] < 300}
                if len(self.rate_buckets) >= 8192:
                    raise Error("rate_capacity", 503)
            self.rate_buckets[key] = (now, 1)

    @contextmanager
    def capacity(self, kind, subject, per_user, total):
        key = (kind, subject)
        with self.rate_lock:
            if (
                self.connection_counts.get(key, 0) >= per_user
                or self.connection_counts.get(kind, 0) >= total
            ):
                raise Error("connection_capacity", 429)
            self.connection_counts[key] = self.connection_counts.get(key, 0) + 1
            self.connection_counts[kind] = self.connection_counts.get(kind, 0) + 1
        try:
            yield
        finally:
            with self.rate_lock:
                for k in (key, kind):
                    self.connection_counts[k] -= 1
                    if not self.connection_counts[k]:
                        del self.connection_counts[k]

    @contextmanager
    def db(self):
        d = sqlite3.connect(self.data / "cloud.sqlite", timeout=8)
        d.row_factory = sqlite3.Row
        try:
            with d:
                yield d
        finally:
            d.close()

    def limit(self, key, count, seconds):
        with self.db() as d:
            d.execute("BEGIN IMMEDIATE")
            now = time.time()
            r = d.execute("SELECT * FROM limits WHERE key=?", (key,)).fetchone()
            if r and now - r["start"] < seconds and r["count"] >= count:
                raise Error("rate_limited", 429)
            if not r or now - r["start"] >= seconds:
                d.execute("INSERT OR REPLACE INTO limits VALUES(?,?,1)", (key, now))
            else:
                d.execute("UPDATE limits SET count=count+1 WHERE key=?", (key,))
            d.execute("DELETE FROM limits WHERE start<?", (now - 86400,))

    def start(self, b):
        if set(b) != {"challenge", "redirect_uri", "scope", "state"}:
            raise Error("invalid_login_fields")
        check(b["challenge"], r"[A-Za-z0-9_-]{43}")
        check(b["state"])
        if b["redirect_uri"] not in CALLBACKS or b["scope"] not in ("browser", "desktop"):
            raise Error("invalid_redirect")
        flow = secrets.token_urlsafe(32)
        with self.db() as d:
            d.execute("DELETE FROM flows WHERE expires<?", (time.time() - 300,))
            d.execute(
                "INSERT INTO flows(id,challenge,redirect,scope,state,expires) VALUES(?,?,?,?,?,?)",
                (
                    flow,
                    b["challenge"],
                    b["redirect_uri"],
                    b["scope"],
                    b["state"],
                    time.time() + 300,
                ),
            )
        return {
            "flow_id": flow,
            "authorize_url": API + "/aieyra/control/authorize?flow=" + flow,
            "expires_in": 300,
        }

    def authorize(self, flow, cookie):
        with self.db() as d:
            r = d.execute(
                "SELECT * FROM flows WHERE id=? AND expires>? AND used=0",
                (check(flow), time.time()),
            ).fetchone()
        if not r:
            raise Error("login_expired", 401)
        identity = self.identity(cookie)
        if not identity:
            return (
                API
                + "/sign-in?"
                + urlencode({"redirect": "/aieyra/control/authorize?flow=" + flow})
            )
        code = secrets.token_urlsafe(32)
        with self.db() as d:
            result = d.execute(
                "UPDATE flows SET code_hash=?,identity=? WHERE id=? AND code_hash IS NULL AND used=0 AND expires>?",
                (digest(code), json.dumps(identity), flow, time.time()),
            )
            if result.rowcount != 1:
                raise Error("login_already_authorized", 409)
        return r["redirect"] + "?" + urlencode({"code": code, "state": r["state"], "flow": flow})

    def exchange(self, b):
        if set(b) != {"flow_id", "code", "verifier", "state", "redirect_uri"}:
            raise Error("invalid_exchange_fields")
        with self.db() as d:
            d.execute("BEGIN IMMEDIATE")
            r = d.execute("SELECT * FROM flows WHERE id=?", (check(b["flow_id"]),)).fetchone()
            if (
                not r
                or r["used"]
                or r["expires"] <= time.time()
                or not r["identity"]
                or not hmac.compare_digest(r["code_hash"], digest(check(b["code"])))
                or not hmac.compare_digest(
                    r["challenge"], challenge(check(b["verifier"], r"[A-Za-z0-9_-]{43,128}"))
                )
                or not hmac.compare_digest(r["state"], check(b["state"]))
                or b["redirect_uri"] != r["redirect"]
            ):
                raise Error("login_verification_failed", 401)
            who = json.loads(r["identity"])
            token = secrets.token_urlsafe(32)
            expires = time.time() + 86400
            if d.execute("SELECT 1 FROM blocked WHERE subject=?", (who["subject"],)).fetchone():
                raise Error("account_unavailable", 403)
            d.execute("UPDATE flows SET used=1 WHERE id=?", (r["id"],))
            d.execute(
                "INSERT INTO sessions VALUES(?,?,?,?,?,0)",
                (digest(token), who["subject"], who["name"], r["scope"], expires),
            )
        return {"access_token": token, "expires_at": expires, "user": who, "scope": r["scope"]}

    def poll(self, b):
        if set(b) != {"flow_id", "verifier", "state"}:
            raise Error("invalid_poll_fields")
        with self.db() as d:
            d.execute("BEGIN IMMEDIATE")
            r = d.execute("SELECT * FROM flows WHERE id=?", (check(b["flow_id"]),)).fetchone()
            if (
                not r
                or r["scope"] != "desktop"
                or r["expires"] <= time.time()
                or r["used"]
                or not hmac.compare_digest(
                    r["challenge"], challenge(check(b["verifier"], r"[A-Za-z0-9_-]{43,128}"))
                )
                or not hmac.compare_digest(r["state"], check(b["state"]))
            ):
                raise Error("login_verification_failed", 401)
            if not r["identity"]:
                return {"pending": True}
            who = json.loads(r["identity"])
            token = secrets.token_urlsafe(32)
            expires = time.time() + 86400
            if d.execute("SELECT 1 FROM blocked WHERE subject=?", (who["subject"],)).fetchone():
                raise Error("account_unavailable", 403)
            d.execute("UPDATE flows SET used=1 WHERE id=?", (r["id"],))
            d.execute(
                "INSERT INTO sessions VALUES(?,?,?,?,?,0)",
                (digest(token), who["subject"], who["name"], "desktop", expires),
            )
        return {"access_token": token, "expires_at": expires, "user": who, "scope": "desktop"}

    def session(self, token):
        with self.db() as d:
            r = d.execute(
                "SELECT * FROM sessions WHERE hash=? AND revoked=0 AND expires>?",
                (digest(token), time.time()),
            ).fetchone()
            if (
                not r
                or d.execute("SELECT 1 FROM blocked WHERE subject=?", (r["subject"],)).fetchone()
            ):
                raise Error("login_required", 401)
        return dict(r)

    def feed(self):
        with self.db() as d:
            rows = d.execute(
                "SELECT seq,agent,body,created FROM posts WHERE hidden=0 ORDER BY seq DESC LIMIT 60"
            ).fetchall()
        return {
            "posts": [dict(r) for r in reversed(rows)],
            "public": True,
            "purpose": "entertainment_only",
        }

    def post(self, session, b):
        if session["scope"] != "desktop":
            raise Error("agent_client_required", 403)
        if (
            set(b) != {"request_id", "agent", "body", "public_consent"}
            or b["public_consent"] is not True
        ):
            raise Error("explicit_public_consent_required")
        rid = check(b["request_id"], r"[A-Za-z0-9_.:-]{1,100}")
        if not isinstance(b["body"], str) or not b["body"].strip() or len(b["body"]) > 1000:
            raise Error("public_text_limit")
        if not isinstance(b["agent"], str) or not 1 <= len(b["agent"]) <= 80:
            raise Error("invalid_agent")
        # Defence in depth. Explicit selection remains required; this is not a DLP claim.
        if re.search(
            r"(?i)(-----BEGIN .*PRIVATE KEY|\bsk-[A-Za-z0-9_-]{16}|(?:password|api_key|access_token)\s*[:=]|[A-Z]:[\\/]|/Users/|/home/)",
            b["body"],
        ):
            raise Error("private_content_detected")
        hashed = digest(json.dumps(b, sort_keys=True, ensure_ascii=False))
        subject = session["subject"]
        with self.db() as d:
            d.execute("BEGIN IMMEDIATE")
            old = d.execute(
                "SELECT * FROM requests WHERE subject=? AND id=?", (subject, rid)
            ).fetchone()
            if old:
                if old["digest"] != hashed:
                    raise Error("request_conflict", 409)
                return {"seq": old["seq"], "replayed": True}
            recent = d.execute(
                "SELECT COUNT(*) FROM posts WHERE subject=? AND created>?",
                (subject, time.time() - 60),
            ).fetchone()[0]
            if recent >= 6:
                raise Error("posting_too_fast", 429)
            row = d.execute(
                "INSERT INTO posts(subject,agent,body,created) VALUES(?,?,?,?)",
                (subject, b["agent"], b["body"].strip(), time.time()),
            )
            d.execute("INSERT INTO requests VALUES(?,?,?,?)", (subject, rid, hashed, row.lastrowid))
            return {"seq": row.lastrowid, "replayed": False}

    def newapi_identity(self, raw_cookie):
        cookies = SimpleCookie()
        try:
            cookies.load(raw_cookie)
        except Exception:
            return None
        if "session" not in cookies:
            return None
        cookie = cookies["session"].OutputString()
        opener = build_opener(ProxyHandler({}), NoRedirect())

        def read(path, extra=None):
            try:
                with opener.open(
                    Request(
                        API + path,
                        headers={"Cookie": cookie, "Accept": "application/json", **(extra or {})},
                    ),
                    timeout=10,
                ) as response:
                    body = response.read(65537)
                    if len(body) > 65536:
                        raise Error("identity_unavailable", 503)
                    return json.loads(body)
            except Exception:
                raise Error("identity_unavailable", 503) from None

        s = read("/api/aieyra/session")
        if s.get("success") is not True:
            return None
        a = s.get("data", {})
        uid = a.get("id")
        if type(uid) is not int or uid <= 0 or a.get("status") != 1:
            raise Error("account_unavailable", 403)
        envelope = read("/api/user/self", {"new-api-user": str(uid)})
        b = envelope.get("data", {})
        if (
            envelope.get("success") is not True
            or b.get("id") != uid
            or b.get("status") != 1
            or b.get("role") != a.get("role")
        ):
            raise Error("identity_mismatch", 403)
        return {
            "subject": str(uid),
            "name": str(b.get("display_name") or b.get("username") or "Aieyra " + str(uid))[:60],
        }


class Handler(BaseHTTPRequestHandler):
    server_version = "AieyraControl"
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass  # Never log codes, cookies, tokens or content.

    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    def version_string(self):
        return "AieyraControl"

    def visitor(self):
        peer = self.client_address[0]
        if peer in self.server.app.trusted_proxies:
            value = self.headers.get("X-Control-Client-IP", "")
            if not value:
                return peer  # Old draining proxy workers share a conservative bucket.
            try:
                return str(ipaddress.ip_address(value))
            except ValueError:
                raise Error("invalid_proxy_identity", 400) from None
        return peer

    def respond(self, status, body=None, headers=None):
        raw = (
            json.dumps(body, ensure_ascii=False).encode()
            if isinstance(body, (dict, list))
            else body or b""
        )
        self.send_response(status)
        defaults = {
            "Content-Type": "application/json; charset=utf-8",
            "Content-Length": str(len(raw)),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "DENY",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self' https://ctrlupdate.aieyra.cn; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        }
        for k, v in {**defaults, **(headers or {})}.items():
            self.send_header(k, v)
        origin = self.headers.get("Origin")
        if origin in (SITE, CLOUD):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Credentials", "true")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def token(self):
        value = self.headers.get("Authorization", "")
        if value.startswith("Bearer "):
            return check(value[7:])
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            raise Error("login_required", 401) from None
        if SESSION_COOKIE not in cookie:
            raise Error("login_required", 401)
        if self.command == "POST" and self.headers.get("Origin") not in (SITE, CLOUD):
            raise Error("origin_required", 403)
        return check(cookie[SESSION_COOKIE].value)

    def login_response(self, value):
        if value.get("scope") != "browser":
            return self.respond(200, value)
        cookie = (
            SESSION_COOKIE
            + "="
            + value["access_token"]
            + "; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=86400"
        )
        return self.respond(
            200, {k: v for k, v in value.items() if k != "access_token"}, {"Set-Cookie": cookie}
        )

    def do_OPTIONS(self):
        if self.headers.get("Origin") not in (SITE, CLOUD):
            return self.respond(403, {"error": "origin_denied"})
        self.respond(
            204,
            headers={
                "Access-Control-Allow-Methods": "GET, POST",
                "Access-Control-Allow-Headers": "Content-Type, Authorization",
                "Access-Control-Max-Age": "600",
            },
        )

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self.run(False)

    def do_POST(self):
        self.run(True)

    def artifact(self, path):
        token = self.token()
        session = self.server.app.session(token)
        with self.server.app.capacity("download", session["subject"], 3, 16):
            return self.send_artifact(path, token)

    def send_artifact(self, path, token):
        if not re.fullmatch(r"/artifacts/[A-Za-z0-9_.-]+", path):
            raise Error("not_found", 404)
        release = self.server.app.data / "releases/stable.json"
        if not release.exists():
            raise Error("not_found", 404)
        manifest = json.loads(release.read_text())["manifest"]
        artifacts = [
            manifest.get("source", {}),
            manifest.get("portable", {}),
            *manifest.get("platforms", {}).values(),
        ]
        artifact = next((a for a in artifacts if a.get("path") == path), None)
        if not artifact:
            raise Error("not_found", 404)
        file = self.server.app.data / "artifacts" / path.rsplit("/", 1)[1]
        if not file.is_file() or file.stat().st_size != artifact["size"]:
            raise Error("artifact_unavailable", 503)
        size = artifact["size"]
        etag = '"' + artifact["sha256"] + '"'
        if self.headers.get("If-None-Match") == etag:
            return self.respond(304, headers={"ETag": etag})
        start, end, status = 0, size - 1, 200
        range_header = self.headers.get("Range")
        if range_header and self.headers.get("If-Range", etag) == etag:
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
            if not m or not any(m.groups()):
                raise Error("invalid_range", 416)
            if not m[1]:
                start = max(0, size - int(m[2]))
            else:
                start = int(m[1])
                end = min(size - 1, int(m[2])) if m[2] else size - 1
            if start > end or start >= size:
                raise Error("invalid_range", 416)
            status = 206
        self.send_response(status)
        headers = {
            "Content-Type": "application/zip",
            "Content-Length": str(end - start + 1),
            "Accept-Ranges": "bytes",
            "ETag": etag,
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Content-Disposition": 'attachment; filename="' + file.name + '"',
        }
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            with file.open("rb") as f:
                f.seek(start)
                remaining = end - start + 1
                checked = time.monotonic()
                while remaining:
                    if time.monotonic() - checked > 5:
                        try:
                            self.server.app.session(token)
                        except Error:
                            self.close_connection = True
                            return
                        checked = time.monotonic()
                    data = f.read(min(65536, remaining))
                    if not data:
                        break
                    self.wfile.write(data)
                    remaining -= len(data)

    def release_events(self):
        token = self.token()
        session = self.server.app.session(token)
        with self.server.app.capacity("stream", session["subject"], 2, 20):
            return self.send_events(token)

    def send_events(self, token):
        if self.command == "HEAD":
            return self.respond(200, headers={"Content-Type": "text/event-stream"})
        if not self.server.stream_slots.acquire(blocking=False):
            raise Error("stream_capacity", 503)
        try:
            self.send_response(200)
            for k, v in {
                "Content-Type": "text/event-stream",
                "Cache-Control": "private, no-store",
                "X-Accel-Buffering": "no",
                "Connection": "close",
                "X-Content-Type-Options": "nosniff",
            }.items():
                self.send_header(k, v)
            self.end_headers()
            self.close_connection = True
            until = time.monotonic() + 55
            previous = None
            target = self.server.app.data / "releases/stable.json"
            while time.monotonic() < until:
                try:
                    self.server.app.session(token)
                except Error:
                    self.wfile.write(b"event: revoked\ndata: {}\n\n")
                    self.wfile.flush()
                    return
                raw = target.read_bytes() if target.exists() else b'{"available":false}'
                current = hashlib.sha256(raw).hexdigest()
                if current != previous:
                    value = json.loads(raw)
                    self.wfile.write(
                        b"event: release\ndata: "
                        + json.dumps(value, separators=(",", ":")).encode()
                        + b"\n\n"
                    )
                    previous = current
                else:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
                time.sleep(3)
        finally:
            self.server.stream_slots.release()

    def run(self, write):
        try:
            url = urlsplit(self.path)
            path = url.path
            app = self.server.app
            if self.server.server_port == 8791 and (
                self.client_address[0] != os.environ.get("CONTROL_BRIDGE_IP", "")
                or path != "/aieyra/control/authorize"
                or write
            ):
                raise Error("bridge_only", 403)
            if self.headers.get("Host", "").split(":")[0] not in (
                "ctrl.aieyra.cn",
                "ctrlupdate.aieyra.cn",
                "127.0.0.1",
                "localhost",
            ):
                raise Error("invalid_host", 421)
            if len(self.path) > 4096:
                raise Error("uri_limit", 414)
            visitor = self.visitor()
            app.ingress_limit("global", 6000, 60)
            app.ingress_limit("ip:" + visitor, 600, 60)
            if path.startswith("/v1/auth/"):
                app.ingress_limit("auth:" + visitor, 120 if path.endswith("/poll") else 20, 60)
            if path.startswith("/v1/feedback"):
                app.ingress_limit("feedback:" + visitor, 60, 60)
            if write:
                if self.headers.get("Origin") not in (None, SITE, CLOUD):
                    raise Error("origin_denied", 403)
                if (
                    self.headers.get("Transfer-Encoding")
                    or len(self.headers.get_all("Content-Length", [])) != 1
                    or self.headers.get_content_type() != "application/json"
                ):
                    raise Error("json_required", 415)
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 8192:
                    raise Error("body_limit", 413)
                b = json.loads(self.rfile.read(length))
                if not isinstance(b, dict):
                    raise Error("invalid_json")
                if path == "/v1/auth/start":
                    return self.respond(200, app.start(b))
                if path == "/v1/auth/exchange":
                    return self.login_response(app.exchange(b))
                if path == "/v1/auth/poll":
                    return self.respond(200, app.poll(b))
                session = app.session(self.token())
                if path == "/v1/feedback/channel":
                    return self.respond(200, app.feedback_channel(session, b))
                if path == "/v1/feedback":
                    return self.respond(200, app.submit_feedback(session, b))
                if path == "/v1/auth/logout":
                    with app.db() as d:
                        d.execute("UPDATE sessions SET revoked=1 WHERE hash=?", (session["hash"],))
                    return self.respond(
                        200,
                        {"logged_out": True},
                        {
                            "Set-Cookie": SESSION_COOKIE
                            + "=; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=0"
                        },
                    )
                if path == "/v1/community":
                    return self.respond(200, app.post(session, b))
                raise Error("not_found", 404)
            if path == "/healthz":
                return self.respond(200, {"service": "aieyra-control-cloud", "version": "0.6.2"})
            if path == "/v1/feedback" or path.startswith("/v1/feedback/"):
                session = app.session(self.token())
                return self.respond(
                    200,
                    app.list_feedback(
                        session, path[13:] if path.startswith("/v1/feedback/") else None
                    ),
                )
            if path.startswith("/artifacts/"):
                return self.artifact(path)
            if path == "/aieyra/control/authorize":
                bridge = os.environ.get("CONTROL_BRIDGE_SECRET", "")
                if not bridge or not hmac.compare_digest(
                    self.headers.get("X-Control-Bridge", ""), bridge
                ):
                    raise Error("bridge_required", 403)
                q = parse_qs(url.query)
                location = app.authorize(q.get("flow", [""])[0], self.headers.get("Cookie", ""))
                return self.respond(302, headers={"Location": location})
            if path == "/v1/session":
                s = app.session(self.token())
                return self.respond(
                    200,
                    {
                        "user": {"subject": s["subject"], "name": s["name"]},
                        "scope": s["scope"],
                        "expires_at": s["expires"],
                    },
                )
            if path == "/v1/community":
                return self.respond(200, app.feed())
            if path == "/v1/releases/events":
                return self.release_events()
            if path == "/v1/releases/stable":
                app.session(self.token())
                target = app.data / "releases/stable.json"
                if not target.exists():
                    return self.respond(200, {"available": False})
                return self.respond(200, target.read_bytes())
            pages = {
                "/": "index.html",
                "/share": "share.html",
                "/download": "download.html",
                "/auth/callback": "callback.html",
                "/feedback": "feedback.html",
                "/feedback.js": "feedback.js",
                "/style.css": "style.css",
                "/site.js": "site.js",
            }
            if path in pages:
                file = Path(__file__).parent / "site" / pages[path]
                return self.respond(
                    200,
                    file.read_bytes(),
                    {
                        "Content-Type": mimetypes.guess_type(str(file))[0]
                        + ("; charset=utf-8" if file.suffix != ".png" else "")
                    },
                )
            raise Error("not_found", 404)
        except (Error, FeedbackError) as e:
            self.close_connection = write
            self.respond(
                e.status, {"error": e.code}, {"Retry-After": "60"} if e.status == 429 else None
            )
        except (ValueError, TypeError, KeyError):
            self.close_connection = True
            self.respond(400, {"error": "invalid_request"})
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception:
            self.close_connection = True
            self.respond(503, {"error": "service_unavailable"})


class BoundedServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(64)
        self.stream_slots = threading.BoundedSemaphore(24)
        super().__init__(*args, **kwargs)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, *args):
        try:
            if getattr(self, "tls_context", None):
                request, address = args
                request.settimeout(5)
                try:
                    request = self.tls_context.wrap_socket(request, server_side=True)
                except (OSError, ssl.SSLError):
                    request.close()
                    return
                args = (request, address)
            super().process_request_thread(*args)
        finally:
            self.slots.release()


if __name__ == "__main__":
    server = BoundedServer(("0.0.0.0", int(os.environ.get("PORT", "8789"))), Handler)
    server.daemon_threads = True
    server.app = Cloud(os.environ.get("CONTROL_DATA", "/data"))
    if os.environ.get("CONTROL_TLS_CERT") and os.environ.get("CONTROL_TLS_KEY"):
        secure = BoundedServer(("0.0.0.0", 8791), Handler)
        secure.daemon_threads = True
        secure.app = server.app
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(os.environ["CONTROL_TLS_CERT"], os.environ["CONTROL_TLS_KEY"])
        secure.tls_context = context
        threading.Thread(target=secure.serve_forever, daemon=True).start()
    server.serve_forever()
