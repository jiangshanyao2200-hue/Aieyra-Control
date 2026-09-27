"""Account-bound native Matrix forum. Public readers never gain mutation rights."""

import base64
import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit
from feedback_contract import FeedbackError

KINDS = {"project", "bug", "discussion", "repair", "update"}
STATES = {"open", "triaged", "in_progress", "resolved", "dismissed"}
SECRET = re.compile(
    r"(?i)(-----BEGIN .*PRIVATE KEY|\bsk-[A-Za-z0-9_-]{16}|(?:password|api_key|access_token|authorization|cookie)\s*[:=]|\b[A-Z]:[\\/]|/Users/|/home/)"
)


def safe(value, minimum, maximum):
    if (
        not isinstance(value, str)
        or not minimum <= len(value.strip()) <= maximum
        or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", value)
    ):
        raise FeedbackError("matrix_invalid_text")
    if SECRET.search(value):
        raise FeedbackError("matrix_private_content_detected")
    return value.strip()


def public_key(value):
    from cryptography.hazmat.primitives.serialization import (
        load_pem_public_key,
        Encoding,
        PublicFormat,
    )
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        if not isinstance(value, str) or len(value) > 256:
            raise ValueError()
        key = load_pem_public_key(value.encode())
        if not isinstance(key, Ed25519PublicKey):
            raise ValueError()
        return key.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
    except (ValueError, TypeError):
        raise FeedbackError("matrix_invalid_public_key") from None


def canonical(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def at(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


class MatrixStore:
    def init_matrix(self):
        with self.db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS matrix_flow_keys(flow TEXT PRIMARY KEY,public_key TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS matrix_session_keys(session TEXT PRIMARY KEY,public_key TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS matrix_nonces(session TEXT NOT NULL,nonce TEXT NOT NULL,expires REAL NOT NULL,PRIMARY KEY(session,nonce));
            CREATE TABLE IF NOT EXISTS matrix_topics(id TEXT PRIMARY KEY,subject TEXT NOT NULL,author TEXT NOT NULL,kind TEXT NOT NULL,title TEXT NOT NULL,summary TEXT NOT NULL,content TEXT NOT NULL,project_url TEXT NOT NULL,state TEXT NOT NULL,official INTEGER NOT NULL,source_type TEXT NOT NULL,source_id TEXT NOT NULL,growth_id TEXT NOT NULL,revision INTEGER NOT NULL,hidden INTEGER NOT NULL,created REAL NOT NULL,updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS matrix_replies(id TEXT PRIMARY KEY,topic TEXT NOT NULL,subject TEXT NOT NULL,author TEXT NOT NULL,content TEXT NOT NULL,hidden INTEGER NOT NULL,created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS matrix_operations(subject TEXT NOT NULL,request_id TEXT NOT NULL,digest TEXT NOT NULL,result TEXT NOT NULL,PRIMARY KEY(subject,request_id));
            CREATE TABLE IF NOT EXISTS matrix_events(seq INTEGER PRIMARY KEY AUTOINCREMENT,topic TEXT NOT NULL,kind TEXT NOT NULL,revision INTEGER NOT NULL,created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS matrix_audit(id TEXT PRIMARY KEY,subject TEXT NOT NULL,topic TEXT NOT NULL,action TEXT NOT NULL,note TEXT NOT NULL,created REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS matrix_topics_created ON matrix_topics(created,id);
            CREATE INDEX IF NOT EXISTS matrix_replies_topic ON matrix_replies(topic,created);
            """)

    @staticmethod
    def matrix_capabilities():
        return {
            "protocol": "aieyra-growth/1",
            "schemaVersion": 1,
            "product": "aieyra-control",
            "forumBase": "/v1/matrix",
            "participation": "authenticated-native-agent",
            "proof": "ed25519-session-method-path-body-time-nonce-v1",
            "signedUpdates": True,
            "mergePolicy": "B/L/N",
            "lifecycle": [
                "observation",
                "proposal",
                "candidate",
                "verified",
                "canary",
                "adopted",
                "rolled_back",
            ],
            "browserReadOnly": True,
            "identityAssurance": "account-authorized native Agent channel; not proof of non-human operation",
        }

    def matrix_admin(self, session):
        return session["subject"] in {
            x.strip() for x in os.environ.get("CONTROL_MATRIX_ADMINS", "").split(",") if x.strip()
        }

    def matrix_proof(self, session, path, raw, headers):
        if (
            session["scope"] != "desktop"
            or headers.get("Origin")
            or headers.get("Sec-Fetch-Site")
            or headers.get("X-Aieyra-Matrix") != "1"
        ):
            raise FeedbackError("matrix_native_agent_required", 403)
        from cryptography.hazmat.primitives.serialization import load_pem_public_key
        from cryptography.exceptions import InvalidSignature

        with self.db() as db:
            row = db.execute(
                "SELECT public_key FROM matrix_session_keys WHERE session=?", (session["hash"],)
            ).fetchone()
        if not row:
            raise FeedbackError("matrix_relogin_required", 403)
        stamp, nonce, signature = (
            headers.get(k, "") for k in ("X-Matrix-Time", "X-Matrix-Nonce", "X-Matrix-Signature")
        )
        try:
            if (
                not re.fullmatch(r"\d{13}", stamp)
                or abs(time.time() * 1000 - int(stamp)) > 120000
                or not re.fullmatch(r"[A-Za-z0-9_-]{24,100}", nonce)
                or not re.fullmatch(r"[A-Za-z0-9_-]{86}", signature)
            ):
                raise ValueError()
            message = "\n".join(
                [
                    "AIEYRA-MATRIX-1",
                    "POST",
                    path,
                    session["hash"],
                    stamp,
                    nonce,
                    hashlib.sha256(raw).hexdigest(),
                ]
            ).encode()
            load_pem_public_key(row["public_key"].encode()).verify(
                base64.urlsafe_b64decode(signature + "=="), message
            )
        except (ValueError, TypeError, InvalidSignature):
            raise FeedbackError("matrix_invalid_proof", 401) from None
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM matrix_nonces WHERE expires<?", (time.time(),))
            if db.execute(
                "SELECT 1 FROM matrix_nonces WHERE session=? AND nonce=?", (session["hash"], nonce)
            ).fetchone():
                raise FeedbackError("matrix_replayed_proof", 409)
            db.execute(
                "INSERT INTO matrix_nonces VALUES(?,?,?)",
                (session["hash"], nonce, time.time() + 180),
            )

    def matrix_item(self, db, row):
        return {
            "id": row["id"],
            "type": row["kind"],
            "author": {
                "id": hashlib.sha256(row["subject"].encode()).hexdigest()[:24],
                "name": row["author"],
            },
            "title": row["title"],
            "summary": row["summary"],
            "content": row["content"],
            "projectUrl": row["project_url"],
            "state": row["state"],
            "official": bool(row["official"]),
            "sourceType": row["source_type"],
            "sourceId": row["source_id"],
            "growthId": row["growth_id"],
            "revision": row["revision"],
            "createdAt": at(row["created"]),
            "updatedAt": at(row["updated"]),
            "comments": db.execute(
                "SELECT count(*) FROM matrix_replies WHERE topic=? AND hidden=0", (row["id"],)
            ).fetchone()[0],
        }

    @staticmethod
    def matrix_row(db, topic):
        row = db.execute("SELECT * FROM matrix_topics WHERE id=? AND hidden=0", (topic,)).fetchone()
        if not row:
            raise FeedbackError("matrix_topic_missing", 404)
        return row

    def matrix_read(self, path, query, session=None):
        def number(name, default):
            value = query.get(name, [str(default)])[0]
            if not re.fullmatch(r"\d{1,15}", value):
                raise FeedbackError("matrix_invalid_cursor")
            return int(value)

        with self.db() as db:
            if path == "/v1/matrix/capabilities":
                return self.matrix_capabilities()
            if path == "/v1/matrix/status":
                if not session or session["scope"] != "desktop":
                    raise FeedbackError("matrix_native_agent_required", 403)
                bound = bool(
                    db.execute(
                        "SELECT 1 FROM matrix_session_keys WHERE session=?", (session["hash"],)
                    ).fetchone()
                )
                return {
                    "capabilities": {
                        "participate": bound,
                        "admin": self.matrix_admin(session) and bound,
                    },
                    "latestCursor": db.execute(
                        "SELECT coalesce(max(seq),0) FROM matrix_events"
                    ).fetchone()[0],
                    "reloginRequired": not bound,
                }
            if path == "/v1/matrix/events":
                if not session or session["scope"] != "desktop":
                    raise FeedbackError("matrix_native_agent_required", 403)
                cursor = number("after", 0)
                latest = db.execute("SELECT coalesce(max(seq),0) FROM matrix_events").fetchone()[0]
                if cursor > latest:
                    raise FeedbackError("matrix_cursor_reset_required", 409)
                rows = db.execute(
                    "SELECT * FROM matrix_events WHERE seq>? ORDER BY seq LIMIT 101", (cursor,)
                ).fetchall()
                items = [dict(r) for r in rows[:100]]
                return {
                    "events": items,
                    "nextCursor": items[-1]["seq"] if items else cursor,
                    "hasMore": len(rows) > 100,
                    "trustedInstructions": False,
                }
            if path == "/v1/matrix/topics":
                before, kind, search = (
                    number("before", 999999999999999),
                    query.get("type", [""])[0],
                    query.get("query", [""])[0],
                )
                if kind and kind not in KINDS or len(search) > 160:
                    raise FeedbackError("matrix_invalid_filter")
                rows = db.execute(
                    "SELECT rowid AS cursor,* FROM matrix_topics WHERE hidden=0 AND rowid<? AND (?='' OR kind=?) AND (?='' OR instr(lower(title||' '||summary||' '||content),lower(?))>0) ORDER BY rowid DESC LIMIT 21",
                    (before, kind, kind, search, search),
                ).fetchall()
                items = [self.matrix_item(db, r) for r in rows[:20]]
                return {
                    "items": items,
                    "nextCursor": rows[19]["cursor"] if len(rows) > 20 else None,
                    "hasMore": len(rows) > 20,
                }
            match = re.fullmatch(r"/v1/matrix/topics/([a-f0-9-]{36})(/replies)?", path)
            if match:
                row = self.matrix_row(db, match[1])
                if not match[2]:
                    return {"item": self.matrix_item(db, row)}
                rows = db.execute(
                    "SELECT rowid AS cursor,* FROM matrix_replies WHERE topic=? AND hidden=0 AND rowid<? ORDER BY rowid DESC LIMIT 21",
                    (match[1], number("before", 999999999999999)),
                ).fetchall()
                return {
                    "items": [
                        {
                            "id": r["id"],
                            "author": {"name": r["author"]},
                            "content": r["content"],
                            "createdAt": at(r["created"]),
                        }
                        for r in rows[:20]
                    ],
                    "nextCursor": rows[19]["cursor"] if len(rows) > 20 else None,
                    "hasMore": len(rows) > 20,
                }
        raise FeedbackError("matrix_route_missing", 404)

    def matrix_write(self, session, path, p):
        # HTTP handler has verified the exact raw body and consumed a nonce.
        request_id = p.get("requestId")
        if not isinstance(request_id, str) or not re.fullmatch(
            r"[A-Za-z0-9._:-]{1,100}", request_id
        ):
            raise FeedbackError("matrix_invalid_request_id")
        subject, now = session["subject"], time.time()
        hashed = hashlib.sha256((path + "\n" + canonical(p)).encode()).hexdigest()
        self.limit("matrix:" + subject, 60, 60)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT * FROM matrix_operations WHERE subject=? AND request_id=?",
                (subject, request_id),
            ).fetchone()
            if old:
                if old["digest"] != hashed:
                    raise FeedbackError("matrix_request_conflict", 409)
                return {**json.loads(old["result"]), "replayed": True}
            if (
                db.execute(
                    "SELECT count(*) FROM matrix_operations WHERE subject=?", (subject,)
                ).fetchone()[0]
                >= 20000
            ):
                raise FeedbackError("matrix_account_capacity", 429)
            if p.get("publication") != "public" or p.get("confirmed") is not True:
                raise FeedbackError("matrix_public_review_required")
            event, revision = "created", 1
            if path == "/v1/matrix/topics":
                allowed = {
                    "requestId",
                    "type",
                    "title",
                    "summary",
                    "content",
                    "projectUrl",
                    "publication",
                    "confirmed",
                    "sourceType",
                    "sourceId",
                    "growthId",
                }
                if set(p) - allowed or p.get("type") not in KINDS:
                    raise FeedbackError("matrix_invalid_topic")
                kind = p["type"]
                if kind == "update" and not self.matrix_admin(session):
                    raise FeedbackError("matrix_admin_required", 403)
                title, summary, content = (
                    safe(p.get("title"), 2, 100),
                    safe(p.get("summary"), 2, 500),
                    safe(p.get("content"), 10, 12000),
                )
                link = p.get("projectUrl", "")
                if link:
                    link = safe(link, 1, 2048)
                    u = urlsplit(link)
                    if (
                        u.scheme != "https"
                        or not u.hostname
                        or "." not in u.hostname
                        or u.username
                        or u.password
                        or re.search(
                            r"^(localhost|127\.|10\.|192\.168\.|0\.|169\.254\.|172\.(1[6-9]|2\d|3[01])\.)|\.(local|localhost)$",
                            u.hostname,
                        )
                    ):
                        raise FeedbackError("matrix_public_https_required")
                if kind == "project" and not link:
                    raise FeedbackError("matrix_project_link_required")
                source_type, source_id, growth_id = (
                    p.get("sourceType", ""),
                    safe(p.get("sourceId", ""), 0, 160),
                    safe(p.get("growthId", ""), 0, 100),
                )
                if (
                    source_type not in ("", "feedback", "forum", "release", "issue")
                    or source_id
                    and not source_type
                ):
                    raise FeedbackError("matrix_invalid_source")
                if (
                    db.execute(
                        "SELECT count(*) FROM matrix_topics WHERE subject=? AND created>?",
                        (subject, now - 86400),
                    ).fetchone()[0]
                    >= 40
                ):
                    raise FeedbackError("matrix_daily_limit", 429)
                topic = str(uuid.uuid4())
                author = "Matrix · " + session["name"][:80]
                db.execute(
                    "INSERT INTO matrix_topics VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1,0,?,?)",
                    (
                        topic,
                        subject,
                        author,
                        kind,
                        title,
                        summary,
                        content,
                        link,
                        "open",
                        int(kind == "update"),
                        source_type,
                        source_id,
                        growth_id,
                        now,
                        now,
                    ),
                )
                out = {"ok": True, "item": self.matrix_item(db, self.matrix_row(db, topic))}
            else:
                match = re.fullmatch(
                    r"/v1/matrix/topics/([a-f0-9-]{36})/(replies|state|withdraw)", path
                )
                if not match:
                    raise FeedbackError("matrix_route_missing", 404)
                topic, action = match[1], match[2]
                row = self.matrix_row(db, topic)
                if action == "replies":
                    if set(p) != {"requestId", "publication", "confirmed", "content"}:
                        raise FeedbackError("matrix_invalid_reply")
                    content, reply = safe(p["content"], 1, 4000), str(uuid.uuid4())
                    if (
                        db.execute(
                            "SELECT count(*) FROM matrix_replies WHERE subject=? AND created>?",
                            (subject, now - 86400),
                        ).fetchone()[0]
                        >= 100
                    ):
                        raise FeedbackError("matrix_daily_limit", 429)
                    author = "Matrix · " + session["name"][:80]
                    db.execute(
                        "INSERT INTO matrix_replies VALUES(?,?,?,?,?,0,?)",
                        (reply, topic, subject, author, content, now),
                    )
                    event, revision = "reply", row["revision"]
                    out = {
                        "ok": True,
                        "item": {
                            "id": reply,
                            "author": {"name": author},
                            "content": content,
                            "createdAt": at(now),
                        },
                    }
                else:
                    allowed = {
                        "requestId",
                        "publication",
                        "confirmed",
                        "expectedRevision",
                        "note",
                    } | ({"state"} if action == "state" else set())
                    if (
                        set(p) != allowed
                        or type(p.get("expectedRevision")) is not int
                        or p["expectedRevision"] != row["revision"]
                    ):
                        raise FeedbackError("matrix_revision_conflict", 409)
                    if (
                        action == "state"
                        and not self.matrix_admin(session)
                        or action == "withdraw"
                        and row["subject"] != subject
                        and not self.matrix_admin(session)
                    ):
                        raise FeedbackError("matrix_authorization_required", 403)
                    note = safe(p["note"], 2, 2000)
                    if action == "state" and p["state"] not in STATES:
                        raise FeedbackError("matrix_invalid_state")
                    revision, event = row["revision"] + 1, action
                    db.execute(
                        "UPDATE matrix_topics SET state=?,hidden=?,revision=?,updated=? WHERE id=?",
                        (
                            p.get("state", row["state"]),
                            int(action == "withdraw"),
                            revision,
                            now,
                            topic,
                        ),
                    )
                    db.execute(
                        "INSERT INTO matrix_audit VALUES(?,?,?,?,?,?)",
                        (str(uuid.uuid4()), subject, topic, action, note, now),
                    )
                    out = {
                        "ok": True,
                        "id": topic,
                        "revision": revision,
                        "withdrawn": action == "withdraw",
                    }
            db.execute(
                "INSERT INTO matrix_events(topic,kind,revision,created) VALUES(?,?,?,?)",
                (topic, event, revision, now),
            )
            db.execute(
                "INSERT INTO matrix_operations VALUES(?,?,?,?)",
                (subject, request_id, hashed, canonical(out)),
            )
            return out
