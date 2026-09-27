"""Account-private maintenance tickets. Never imported into the public feed."""

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from feedback_contract import FeedbackError, canonical, prepare


class FeedbackStore:
    @staticmethod
    def feedback_capabilities():
        return {
            "contract": "aieyra-private-feedback/2",
            "products": ["aieyra-control", "aieyra-os"],
            "auth_scope_by_product": {"aieyra-control": "desktop", "aieyra-os": "feedback"},
            "private": True,
            "report_max_bytes": 6000,
            "request_max_bytes": 8192,
            "channel_expires_in": 600,
        }

    @staticmethod
    def feedback_product(session):
        if session["scope"] == "desktop":
            return "aieyra-control"
        if session["scope"] == "feedback" and session.get("product") == "aieyra-os":
            return "aieyra-os"
        raise FeedbackError("agent_client_required", 403)

    def init_feedback(self):
        with self.db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS feedback_keys(id INTEGER PRIMARY KEY CHECK(id=1),secret TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS feedback(id TEXT PRIMARY KEY,subject TEXT NOT NULL,request_id TEXT NOT NULL,digest TEXT NOT NULL,installation TEXT NOT NULL,station TEXT NOT NULL,product TEXT NOT NULL,version TEXT NOT NULL,report TEXT NOT NULL,status TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,note TEXT NOT NULL DEFAULT '',created REAL NOT NULL,updated REAL NOT NULL,UNIQUE(subject,request_id));
            CREATE INDEX IF NOT EXISTS feedback_owner_created ON feedback(subject,created);
            CREATE TABLE IF NOT EXISTS feedback_audit(seq INTEGER PRIMARY KEY AUTOINCREMENT,ticket TEXT NOT NULL,revision INTEGER NOT NULL,status TEXT NOT NULL,note TEXT NOT NULL,created REAL NOT NULL);
            """)
            db.execute("INSERT OR IGNORE INTO feedback_keys VALUES(1,?)", (secrets.token_hex(32),))
            self.feedback_key = (
                db.execute("SELECT secret FROM feedback_keys WHERE id=1").fetchone()[0].encode()
            )

    def feedback_channel(self, session, body):
        product = self.feedback_product(session)
        allowed = ({"installation", "station", "product"},)
        if product == "aieyra-control":
            allowed += ({"installation", "station"},)
        if set(body) not in allowed or any(
            not isinstance(body.get(k), str) or not re.fullmatch("[a-f0-9]{64}", body[k])
            for k in ("installation", "station")
        ):
            raise FeedbackError("invalid_feedback_channel")
        if body.get("product", "aieyra-control") != product:
            raise FeedbackError("feedback_product_mismatch", 403)
        self.limit("feedback-channel:" + session["subject"], 20, 60)
        claims = {
            **body,
            "product": product,
            "session": session["hash"],
            "expires": int(time.time()) + 600,
            "scope": "feedback:create",
        }
        raw = base64.urlsafe_b64encode(canonical(claims).encode()).decode().rstrip("=")
        signature = hmac.new(self.feedback_key, raw.encode(), hashlib.sha256).hexdigest()
        return {
            "channel_token": raw + "." + signature,
            "expires_in": 600,
            "scope": "feedback:create",
            "private": True,
            "product": product,
            "identity_assurance": "authenticated_account; product_scoped_feedback",
        }

    def verify_channel(self, session, token):
        try:
            if not isinstance(token, str) or len(token) > 1024:
                raise ValueError()
            raw, sig = token.split(".")
            if not hmac.compare_digest(
                sig, hmac.new(self.feedback_key, raw.encode(), hashlib.sha256).hexdigest()
            ):
                raise ValueError()
            claims = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
            product = self.feedback_product(session)
            if (
                claims["session"] != session["hash"]
                or claims["expires"] <= time.time()
                or claims["scope"] != "feedback:create"
                or claims.get("product", "aieyra-control") != product
            ):
                raise ValueError()
            return claims
        except (ValueError, KeyError, TypeError):
            raise FeedbackError("feedback_channel_invalid", 403) from None

    @staticmethod
    def feedback_receipt(row):
        return {
            k: row[k]
            for k in (
                "id",
                "request_id",
                "product",
                "version",
                "status",
                "revision",
                "note",
                "created",
                "updated",
            )
        }

    def submit_feedback(self, session, body):
        product = self.feedback_product(session)
        if set(body) != {"request_id", "channel_token", "product", "version", "report"}:
            raise FeedbackError("invalid_feedback_fields")
        claims = self.verify_channel(session, body["channel_token"])
        if (
            body["product"] not in ("aieyra-control", "aieyra-os")
            or not isinstance(body["version"], str)
            or not re.fullmatch(r"\d{1,4}\.\d{1,4}\.\d{1,4}", body["version"])
        ):
            raise FeedbackError("invalid_feedback_product")
        if body["product"] != product:
            raise FeedbackError("feedback_product_mismatch", 403)
        rid = body["request_id"]
        if not isinstance(rid, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", rid):
            raise FeedbackError("invalid_request_id")
        report, changed = prepare(body["report"])
        hashed = hashlib.sha256(
            canonical({k: body[k] for k in ("product", "version", "report")}).encode()
        ).hexdigest()
        now = time.time()
        subject = session["subject"]
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT * FROM feedback WHERE subject=? AND request_id=?", (subject, rid)
            ).fetchone()
            if old:
                if old["digest"] != hashed:
                    raise FeedbackError("feedback_request_conflict", 409)
                return {**self.feedback_receipt(old), "replayed": True, "private": True}
            if (
                db.execute(
                    "SELECT count(*) FROM feedback WHERE subject=? AND created>?",
                    (subject, now - 86400),
                ).fetchone()[0]
                >= 20
            ):
                raise FeedbackError("feedback_daily_limit", 429)
            if (
                db.execute("SELECT count(*) FROM feedback WHERE subject=?", (subject,)).fetchone()[
                    0
                ]
                >= 500
            ):
                raise FeedbackError("feedback_account_capacity", 429)
            if db.execute("SELECT count(*) FROM feedback").fetchone()[0] >= 100000:
                raise FeedbackError("feedback_capacity", 503)
            fid = "ACF-" + secrets.token_hex(12)
            db.execute(
                "INSERT INTO feedback(id,subject,request_id,digest,installation,station,product,version,report,status,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    fid,
                    subject,
                    rid,
                    hashed,
                    claims["installation"],
                    claims["station"],
                    body["product"],
                    body["version"],
                    canonical(report),
                    "received",
                    now,
                    now,
                ),
            )
            row = db.execute("SELECT * FROM feedback WHERE id=?", (fid,)).fetchone()
        return {
            **self.feedback_receipt(row),
            "replayed": False,
            "redacted": changed,
            "private": True,
        }

    def list_feedback(self, session, ticket=None):
        product = None if session["scope"] == "browser" else self.feedback_product(session)
        product_filter = " AND product=?" if product else ""
        product_args = (product,) if product else ()
        with self.db() as db:
            if ticket:
                if not re.fullmatch("ACF-[a-f0-9]{24}", ticket):
                    raise FeedbackError("feedback_missing", 404)
                rows = db.execute(
                    "SELECT * FROM feedback WHERE subject=? AND id=?" + product_filter,
                    (session["subject"], ticket, *product_args),
                ).fetchall()
                if not rows:
                    raise FeedbackError("feedback_missing", 404)
            else:
                rows = db.execute(
                    "SELECT * FROM feedback WHERE subject=?"
                    + product_filter
                    + " ORDER BY created DESC LIMIT 50",
                    (session["subject"], *product_args),
                ).fetchall()
        return {
            "tickets": [
                {
                    **self.feedback_receipt(r),
                    "version": r["version"],
                    "report": json.loads(r["report"]),
                }
                for r in rows
            ],
            "private": True,
        }
