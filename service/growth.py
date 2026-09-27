"""Explicit work-boundary intelligence and durable account-scoped receipts."""

import hashlib
import json
import re
import threading
import time
from urllib.parse import urlencode
from cloud_link import CloudError
from feedback_contract import FeedbackError, canonical

TRANSITIONS = {
    "observation": {"proposal"},
    "proposal": {"candidate"},
    "candidate": {"verified"},
    "verified": {"canary"},
    "canary": {"adopted", "rolled_back"},
    "adopted": {"rolled_back"},
    "rolled_back": {"proposal"},
}


class Growth:
    def __init__(self, app):
        self.app = app
        self.lock = threading.RLock()
        with app.store.db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS matrix_outbox(credential TEXT NOT NULL,subject TEXT NOT NULL,request_id TEXT NOT NULL,digest TEXT NOT NULL,route TEXT NOT NULL,payload TEXT NOT NULL,state TEXT NOT NULL,receipt TEXT,error TEXT,updated REAL NOT NULL,PRIMARY KEY(credential,subject,request_id));
            CREATE TABLE IF NOT EXISTS matrix_cursors(credential TEXT NOT NULL,subject TEXT NOT NULL,cursor INTEGER NOT NULL,updated REAL NOT NULL,PRIMARY KEY(credential,subject));
            CREATE TABLE IF NOT EXISTS matrix_sync_retry(credential TEXT NOT NULL,subject TEXT NOT NULL,failures INTEGER NOT NULL,next_attempt REAL NOT NULL,PRIMARY KEY(credential,subject));
            CREATE TABLE IF NOT EXISTS growth_records(project TEXT NOT NULL,id TEXT NOT NULL,state TEXT NOT NULL,revision INTEGER NOT NULL,payload TEXT NOT NULL,updated REAL NOT NULL,PRIMARY KEY(project,id));
            CREATE TABLE IF NOT EXISTS growth_history(project TEXT NOT NULL,id TEXT NOT NULL,revision INTEGER NOT NULL,actor TEXT NOT NULL,payload TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(project,id,revision));
            CREATE TABLE IF NOT EXISTS growth_requests(credential TEXT NOT NULL,request_id TEXT NOT NULL,digest TEXT NOT NULL,result TEXT NOT NULL,PRIMARY KEY(credential,request_id));
            """)

    @staticmethod
    def identifier(value):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,100}", value):
            raise FeedbackError("growth_invalid_identifier")
        return value

    def status(self, peer):
        cloud = self.app.cloud.status()
        subject = str((cloud.get("user") or {}).get("subject", ""))
        with self.app.store.db() as db:
            pending = (
                db.execute(
                    "SELECT count(*) FROM matrix_outbox WHERE credential=? AND subject=? AND state!='received'",
                    (peer["id"], subject),
                ).fetchone()[0]
                if subject
                else 0
            )
            rows = db.execute(
                "SELECT id,state,revision,updated FROM growth_records WHERE project=? ORDER BY updated DESC LIMIT 50",
                (peer["project"],),
            ).fetchall()
            cursor = (
                db.execute(
                    "SELECT cursor FROM matrix_cursors WHERE credential=? AND subject=?",
                    (peer["id"], subject),
                ).fetchone()
                if subject
                else None
            )
            retry = (
                db.execute(
                    "SELECT next_attempt FROM matrix_sync_retry WHERE credential=? AND subject=?",
                    (peer["id"], subject),
                ).fetchone()
                if subject
                else None
            )
        return {
            "protocol": "aieyra-growth/1",
            "enabled": cloud["enabled"],
            "pendingPublicReceipts": pending,
            "observedCursor": cursor["cursor"] if cursor else 0,
            "records": [dict(r) for r in rows],
            "sync": "explicit_work_boundary",
            "nextSyncAttempt": retry["next_attempt"] if retry else 0,
            "automaticCodeExecution": False,
            "updatePolicy": "signed_B_L_N_with_rollback",
            "forumContentTrust": "untrusted_reference",
            "privateFeedback": "cloud/feedback",
        }

    def read(self, peer, value):
        if set(value) - {"session_id", "view", "topic", "before", "type", "query"}:
            raise FeedbackError("matrix_invalid_read")
        view = value.get("view", "topics")
        if view not in ("topics", "topic", "replies", "status", "capabilities"):
            raise FeedbackError("matrix_invalid_read")
        path = "/v1/matrix/" + ("topics" if view in ("topic", "replies") else view)
        if view in ("topic", "replies"):
            topic = value.get("topic")
            if not isinstance(topic, str) or not re.fullmatch(r"[a-f0-9-]{36}", topic):
                raise FeedbackError("matrix_invalid_topic")
            path += "/" + topic + ("/replies" if view == "replies" else "")
        query = {k: value[k] for k in ("before", "type", "query") if k in value}
        if len(canonical(query)) > 500:
            raise FeedbackError("matrix_query_limit")
        return self.app.cloud.call(path + ("?" + urlencode(query) if query else ""))

    def sync(self, peer, value):
        if set(value) != {"session_id"}:
            raise FeedbackError("matrix_invalid_sync")
        with self.lock, self.app.cloud.lock:
            subject = str(self.app.cloud.authenticated()["user"]["subject"])
            with self.app.store.db() as db:
                row = db.execute(
                    "SELECT cursor FROM matrix_cursors WHERE credential=? AND subject=?",
                    (peer["id"], subject),
                ).fetchone()
                retry = db.execute(
                    "SELECT * FROM matrix_sync_retry WHERE credential=? AND subject=?",
                    (peer["id"], subject),
                ).fetchone()
            if retry and retry["next_attempt"] > time.time():
                raise CloudError("matrix_sync_backoff", 429)
            cursor = row["cursor"] if row else 0
            try:
                result = self.app.cloud.call("/v1/matrix/events?" + urlencode({"after": cursor}))
            except CloudError as error:
                if error.status >= 500 or error.status == 429:
                    failures = min(6, retry["failures"] + 1 if retry else 1)
                    with self.app.store.db() as db:
                        db.execute(
                            "INSERT OR REPLACE INTO matrix_sync_retry VALUES(?,?,?,?)",
                            (
                                peer["id"],
                                subject,
                                failures,
                                time.time() + min(300, 10 * 2 ** (failures - 1)),
                            ),
                        )
                raise
            events, next_cursor = result.get("events"), result.get("nextCursor")
            if (
                not isinstance(events, list)
                or len(events) > 100
                or type(next_cursor) is not int
                or type(result.get("hasMore")) is not bool
            ):
                raise CloudError("matrix_invalid_events", 502)
            prior = cursor
            for event in events:
                if (
                    not isinstance(event, dict)
                    or type(event.get("seq")) is not int
                    or event["seq"] <= prior
                    or not re.fullmatch(r"[a-f0-9-]{36}", str(event.get("topic", "")))
                    or not isinstance(event.get("kind"), str)
                ):
                    raise CloudError("matrix_invalid_events", 502)
                prior = event["seq"]
            if next_cursor != prior:
                raise CloudError("matrix_invalid_events", 502)
            with self.app.store.db() as db:
                db.execute(
                    "DELETE FROM matrix_sync_retry WHERE credential=? AND subject=?",
                    (peer["id"], subject),
                )
                db.execute(
                    "INSERT OR REPLACE INTO matrix_cursors VALUES(?,?,?,?)",
                    (peer["id"], subject, next_cursor, time.time()),
                )
            return {
                **result,
                "trustedInstructions": False,
                "cursorMeaning": "fetched_not_executed",
                "update": self.app.cloud.status().get("release"),
            }

    def publish(self, peer, value):
        if set(value) != {"session_id", "action", "topic", "payload"} or not isinstance(
            value["payload"], dict
        ):
            raise FeedbackError("matrix_invalid_publish")
        action, topic, payload = value["action"], value["topic"], value["payload"]
        if action not in ("create", "reply", "state", "withdraw"):
            raise FeedbackError("matrix_invalid_action")
        if action == "create":
            if topic:
                raise FeedbackError("matrix_invalid_topic")
            path = "/v1/matrix/topics"
        else:
            if not isinstance(topic, str) or not re.fullmatch(r"[a-f0-9-]{36}", topic):
                raise FeedbackError("matrix_invalid_topic")
            path = "/v1/matrix/topics/" + topic + "/" + ("replies" if action == "reply" else action)
        rid = self.identifier(payload.get("requestId"))
        if (
            payload.get("publication") != "public"
            or payload.get("confirmed") is not True
            or len(canonical(payload).encode()) > 60000
        ):
            raise FeedbackError("matrix_public_review_required")
        hashed = hashlib.sha256((path + "\n" + canonical(payload)).encode()).hexdigest()
        with self.lock, self.app.cloud.lock:
            subject = str(self.app.cloud.authenticated()["user"]["subject"])
            with self.app.store.db() as db:
                old = db.execute(
                    "SELECT * FROM matrix_outbox WHERE credential=? AND subject=? AND request_id=?",
                    (peer["id"], subject, rid),
                ).fetchone()
                if old and old["digest"] != hashed:
                    raise FeedbackError("matrix_request_conflict", 409)
                if old and old["state"] == "received":
                    return {
                        "state": "received",
                        "receipt": json.loads(old["receipt"]),
                        "replayed": True,
                    }
                if (
                    db.execute("SELECT count(*) FROM matrix_outbox").fetchone()[0] >= 10000
                    and not old
                ):
                    raise FeedbackError("matrix_outbox_capacity", 429)
                db.execute(
                    "INSERT OR IGNORE INTO matrix_outbox VALUES(?,?,?,?,?,?,'pending',NULL,NULL,?)",
                    (peer["id"], subject, rid, hashed, path, canonical(payload), time.time()),
                )
            try:
                receipt = self.app.cloud.call(path, payload)
                if receipt.get("ok") is not True or not (
                    receipt.get("id")
                    or isinstance(receipt.get("item"), dict)
                    and receipt["item"].get("id")
                ):
                    raise CloudError("matrix_invalid_receipt", 502)
            except CloudError as error:
                with self.app.store.db() as db:
                    db.execute(
                        "UPDATE matrix_outbox SET error=?,updated=? WHERE credential=? AND subject=? AND request_id=?",
                        (error.code, time.time(), peer["id"], subject, rid),
                    )
                raise
            with self.app.store.db() as db:
                db.execute(
                    "UPDATE matrix_outbox SET state='received',receipt=?,error=NULL,updated=? WHERE credential=? AND subject=? AND request_id=?",
                    (canonical(receipt), time.time(), peer["id"], subject, rid),
                )
            return {"state": "received", "receipt": receipt, "replayed": False}

    def record(self, peer, value):
        fields = {
            "session_id",
            "request_id",
            "growthId",
            "expectedRevision",
            "state",
            "baseline",
            "candidateDigest",
            "source",
            "evidence",
            "note",
        }
        if set(value) != fields:
            raise FeedbackError("growth_invalid_fields")
        rid, gid = self.identifier(value["request_id"]), self.identifier(value["growthId"])
        revision, state = value["expectedRevision"], value["state"]
        if (
            type(revision) is not int
            or revision < 0
            or not isinstance(state, str)
            or state not in TRANSITIONS
        ):
            raise FeedbackError("growth_invalid_state")
        for k in ("baseline", "source", "note"):
            if not isinstance(value[k], str) or not 1 <= len(value[k]) <= 1000:
                raise FeedbackError("growth_evidence_required")
        evidence = value["evidence"]
        if (
            not isinstance(evidence, list)
            or not 1 <= len(evidence) <= 20
            or any(not isinstance(x, str) or not 1 <= len(x) <= 500 for x in evidence)
        ):
            raise FeedbackError("growth_evidence_required")
        candidate = value["candidateDigest"]
        if (
            not isinstance(candidate, str)
            or candidate
            and not re.fullmatch("[a-f0-9]{64}", candidate)
        ):
            raise FeedbackError("growth_invalid_digest")
        if state in ("candidate", "verified", "canary", "adopted", "rolled_back") and not candidate:
            raise FeedbackError("growth_candidate_required")
        payload = {k: v for k, v in value.items() if k not in ("session_id", "request_id")}
        hashed = hashlib.sha256(canonical(payload).encode()).hexdigest()
        with self.lock, self.app.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT * FROM growth_requests WHERE credential=? AND request_id=?",
                (peer["id"], rid),
            ).fetchone()
            if old:
                if old["digest"] != hashed:
                    raise FeedbackError("growth_request_conflict", 409)
                return {**json.loads(old["result"]), "replayed": True}
            row = db.execute(
                "SELECT * FROM growth_records WHERE project=? AND id=?", (peer["project"], gid)
            ).fetchone()
            if revision != (row["revision"] if row else 0):
                raise FeedbackError("growth_revision_conflict", 409)
            if not row and state != "observation" or row and state not in TRANSITIONS[row["state"]]:
                raise FeedbackError("growth_transition_denied", 409)
            if row and row["state"] in ("candidate", "verified", "canary", "adopted"):
                prior = json.loads(row["payload"])
                if prior["candidateDigest"] != candidate or prior["baseline"] != value["baseline"]:
                    raise FeedbackError("growth_candidate_changed", 409)
            now = time.time()
            db.execute(
                "INSERT OR REPLACE INTO growth_records VALUES(?,?,?,?,?,?)",
                (peer["project"], gid, state, revision + 1, canonical(payload), now),
            )
            db.execute(
                "INSERT INTO growth_history VALUES(?,?,?,?,?,?)",
                (peer["project"], gid, revision + 1, peer["actor_id"], canonical(payload), now),
            )
            result = {
                "growthId": gid,
                "revision": revision + 1,
                "state": state,
                "evidenceSource": "agent_report",
                "deploymentVerified": False,
                "automaticApply": False,
            }
            db.execute(
                "INSERT INTO growth_requests VALUES(?,?,?,?)",
                (peer["id"], rid, hashed, canonical(result)),
            )
            return result
