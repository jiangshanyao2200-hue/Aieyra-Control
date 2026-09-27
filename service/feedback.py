"""Private, selected diagnostics only. Durable outbox; no filesystem harvesting."""

from __future__ import annotations
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from cloud_link import CloudError

from feedback_contract import FeedbackError, canonical, prepare


class Feedback:
    def __init__(self, app):
        self.app = app
        self.store = app.store
        self.lock = threading.Lock()
        with self.store.db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS feedback_identity(id INTEGER PRIMARY KEY CHECK(id=1),salt TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS feedback_outbox(id TEXT PRIMARY KEY,credential_id TEXT NOT NULL,project TEXT NOT NULL,request_id TEXT NOT NULL,digest TEXT NOT NULL,station TEXT NOT NULL,payload TEXT NOT NULL,subject TEXT,state TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,next_try REAL NOT NULL DEFAULT 0,receipt TEXT,error TEXT,created REAL NOT NULL,updated REAL NOT NULL,UNIQUE(credential_id,request_id));
            """)
            db.execute(
                "INSERT OR IGNORE INTO feedback_identity VALUES(1,?)", (secrets.token_hex(32),)
            )
            self.salt = db.execute("SELECT salt FROM feedback_identity WHERE id=1").fetchone()[0]
        self.installation = hashlib.sha256(self.salt.encode()).hexdigest()

    def station(self, actor):
        return hmac.new(self.salt.encode(), actor.encode(), hashlib.sha256).hexdigest()

    def enqueue(self, peer, value, version):
        if (
            set(value) != {"request_id", "session_id", "report", "privacy_reviewed"}
            or value["privacy_reviewed"] is not True
        ):
            raise FeedbackError("feedback_review_required")
        rid = value["request_id"]
        if not isinstance(rid, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", rid):
            raise FeedbackError("invalid_request_id")
        report, changed = prepare(value["report"])
        payload = {"product": "aieyra-control", "version": version, "report": report}
        hashed = hashlib.sha256(canonical(payload).encode()).hexdigest()
        now = time.time()
        with self.app.cloud.lock:
            session = self.app.cloud.session
            subject = (
                session.get("user", {}).get("subject")
                if session and session.get("expires_at", 0) > now
                else None
            )
        with self.lock, self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT * FROM feedback_outbox WHERE credential_id=? AND request_id=?",
                (peer["id"], rid),
            ).fetchone()
            if old:
                if old["digest"] != hashed:
                    raise FeedbackError("feedback_request_conflict", 409)
                return {**self.view(old), "replayed": True}
            if (
                db.execute(
                    "SELECT count(*) FROM feedback_outbox WHERE state NOT IN ('accepted','cancelled')"
                ).fetchone()[0]
                >= 200
            ):
                raise FeedbackError("feedback_queue_full", 429)
            if (
                db.execute(
                    "SELECT count(*) FROM feedback_outbox WHERE credential_id=? AND created>?",
                    (peer["id"], now - 3600),
                ).fetchone()[0]
                >= 20
            ):
                raise FeedbackError("feedback_too_fast", 429)
            fid = "fb-" + secrets.token_hex(16)
            db.execute(
                "INSERT INTO feedback_outbox(id,credential_id,project,request_id,digest,station,payload,subject,state,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    fid,
                    peer["id"],
                    peer["project"],
                    rid,
                    hashed,
                    self.station(peer["actor_id"]),
                    canonical(payload),
                    subject,
                    "queued",
                    now,
                    now,
                ),
            )
            row = db.execute("SELECT * FROM feedback_outbox WHERE id=?", (fid,)).fetchone()
        return {**self.view(row), "redacted": changed, "replayed": False}

    @staticmethod
    def view(row):
        return {
            "id": row["id"],
            "request_id": row["request_id"],
            "state": row["state"],
            "attempts": row["attempts"],
            "report": json.loads(row["payload"])["report"],
            "receipt": json.loads(row["receipt"]) if row["receipt"] else None,
            "error": row["error"],
            "created": row["created"],
            "updated": row["updated"],
            "private": True,
        }

    def list(self, peer):
        with self.store.db() as db:
            rows = db.execute(
                "SELECT * FROM feedback_outbox WHERE project=? ORDER BY created DESC LIMIT 100",
                (peer["project"],),
            ).fetchall()
        return {
            "feedback": [self.view(r) for r in rows],
            "channel": "private-maintenance",
            "automatic_retry": True,
        }

    def cancel(self, peer, value):
        if set(value) != {"session_id", "id"} or not isinstance(value["id"], str):
            raise FeedbackError("invalid_feedback_cancel")
        with self.lock, self.store.db() as db:
            r = db.execute(
                "SELECT * FROM feedback_outbox WHERE id=? AND project=?",
                (value["id"], peer["project"]),
            ).fetchone()
            if not r:
                raise FeedbackError("feedback_missing", 404)
            if r["state"] == "accepted":
                raise FeedbackError("feedback_already_submitted", 409)
            db.execute(
                "UPDATE feedback_outbox SET state='cancelled',updated=? WHERE id=?",
                (time.time(), r["id"]),
            )
        return {"id": r["id"], "state": "cancelled"}

    def flush(self):
        if not self.lock.acquire(False):
            return
        try:
            with self.app.cloud.lock:
                try:
                    s = self.app.cloud.authenticated()
                    subject = str(s["user"]["subject"])
                    token = s["access_token"]
                except (CloudError, KeyError):
                    return
            with self.store.db() as db:
                rows = db.execute(
                    "SELECT * FROM feedback_outbox WHERE state IN ('queued','accepted') AND next_try<=? AND (subject IS NULL OR subject=?) ORDER BY next_try,created LIMIT 3",
                    (time.time(), subject),
                ).fetchall()
            for r in rows:
                # No network/DB transaction spans one another; retries retain ID.
                try:
                    with self.store.db() as db:
                        peer = db.execute(
                            "SELECT * FROM agent_credentials WHERE id=? AND revoked=0",
                            (r["credential_id"],),
                        ).fetchone()
                    if not peer or not self.app.agent_access.is_leader(dict(peer), r["project"]):
                        if r["state"] == "queued":
                            self.mark(r, "paused", "leader_authorization_expired")
                        else:
                            with self.store.db() as db:
                                db.execute(
                                    "UPDATE feedback_outbox SET next_try=? WHERE id=?",
                                    (time.time() + 300, r["id"]),
                                )
                        continue
                    if r["subject"] and r["subject"] != subject:
                        continue
                    with self.store.db() as db:
                        db.execute(
                            "UPDATE feedback_outbox SET subject=? WHERE id=? AND subject IS NULL",
                            (subject, r["id"]),
                        )
                    with self.app.cloud.lock:
                        current = self.app.cloud.authenticated()
                        if current["access_token"] != token:
                            return
                        if r["state"] == "accepted":
                            old = json.loads(r["receipt"])
                            value = self.app.cloud.transport(
                                "/v1/feedback/" + old["id"], None, token
                            )
                            receipt = value["tickets"][0]
                            if (
                                receipt["id"] != old["id"]
                                or receipt["request_id"] != r["id"]
                                or receipt.get("status")
                                not in (
                                    "received",
                                    "triaged",
                                    "in_progress",
                                    "resolved",
                                    "rejected",
                                )
                            ):
                                raise CloudError("invalid_feedback_receipt", 502)
                            receipt = {
                                k: receipt[k]
                                for k in (
                                    "id",
                                    "request_id",
                                    "status",
                                    "revision",
                                    "note",
                                    "created",
                                    "updated",
                                )
                            }
                            with self.store.db() as db:
                                db.execute(
                                    "UPDATE feedback_outbox SET receipt=?,next_try=?,error=NULL,updated=? WHERE id=?",
                                    (canonical(receipt), time.time() + 300, time.time(), r["id"]),
                                )
                            continue
                        channel = self.app.cloud.transport(
                            "/v1/feedback/channel",
                            {"installation": self.installation, "station": r["station"]},
                            token,
                        )
                        body = {
                            **json.loads(r["payload"]),
                            "request_id": r["id"],
                            "channel_token": channel["channel_token"],
                        }
                        receipt = self.app.cloud.transport("/v1/feedback", body, token)
                    if (
                        receipt.get("request_id") != r["id"]
                        or not re.fullmatch(r"ACF-[a-f0-9]{24}", str(receipt.get("id", "")))
                        or receipt.get("status")
                        not in ("received", "triaged", "in_progress", "resolved", "rejected")
                    ):
                        raise CloudError("invalid_feedback_receipt", 502)
                    with self.store.db() as db:
                        db.execute(
                            "UPDATE feedback_outbox SET state='accepted',receipt=?,attempts=attempts+1,error=NULL,next_try=?,updated=? WHERE id=?",
                            (canonical(receipt), time.time() + 300, time.time(), r["id"]),
                        )
                except Exception as e:
                    status = getattr(e, "status", 503)
                    code = getattr(e, "code", "feedback_unavailable")
                    if status in (400, 403, 404, 409, 413, 422) and r["state"] == "queued":
                        self.mark(r, "rejected", code)
                    else:
                        delay = min(3600, 15 * 2 ** min(r["attempts"], 8)) + secrets.randbelow(10)
                        with self.store.db() as db:
                            db.execute(
                                "UPDATE feedback_outbox SET attempts=attempts+1,next_try=?,error=?,updated=? WHERE id=?",
                                (time.time() + delay, code, time.time(), r["id"]),
                            )
        finally:
            self.lock.release()

    def mark(self, row, state, error):
        with self.store.db() as db:
            db.execute(
                "UPDATE feedback_outbox SET state=?,error=?,updated=? WHERE id=?",
                (state, error, time.time(), row["id"]),
            )
