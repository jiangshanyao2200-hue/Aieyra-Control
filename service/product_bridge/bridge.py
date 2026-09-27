"""Durable alpha task adapter. HTTP failures never replay a mutation."""

from __future__ import annotations

import argparse
import base64
import calendar
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys

from matrix_client import BridgeError, MatrixClient, absolute_path, canonical, digest, require


def now():
    return datetime.now(timezone.utc).isoformat()


def timestamp(value):
    try:
        require(isinstance(value, str), "invalid_timestamp")
        match = re.fullmatch(
            r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.(\d{1,9}))?(?:Z|[+-]\d\d:\d\d)", value
        )
        require(match is not None, "invalid_timestamp")
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(parsed.tzinfo is not None, "invalid_timestamp")
        return calendar.timegm(parsed.astimezone(timezone.utc).timetuple()) * 1000000000 + int(
            (match[1] or "").ljust(9, "0")
        )
    except ValueError:
        raise BridgeError("invalid_timestamp") from None


def relative_file(value):
    require(
        isinstance(value, str) and value and len(value.encode("utf-8")) <= 500,
        "invalid_artifact_path",
    )
    parts = value.replace("\\", "/").split("/")
    require(
        not any(
            p in {"", ".", ".."}
            or p.endswith((".", " "))
            or any(c in p for c in ':\x00<>"|?*')
            or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?", p)
            for p in parts
        ),
        "invalid_artifact_path",
    )
    return "/".join(parts)


def no_links(path):
    for p in [path, *path.parents]:
        s = p.lstat()
        require(
            not stat.S_ISLNK(s.st_mode) and not getattr(s, "st_file_attributes", 0) & 0x400,
            "linked_path_not_allowed",
        )


def opened_path(file):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        import msvcrt

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        fn = kernel.GetFinalPathNameByHandleW
        fn.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        fn.restype = wintypes.DWORD
        buf = ctypes.create_unicode_buffer(32768)
        length = fn(msvcrt.get_osfhandle(file.fileno()), buf, len(buf), 0)
        require(0 < length < len(buf), "artifact_handle_unavailable")
        result = buf.value
        if result.startswith("\\\\?\\UNC\\"):
            result = "\\\\" + result[8:]
        elif result.startswith("\\\\?\\"):
            result = result[4:]
        return Path(result).resolve()
    if sys.platform.startswith("linux"):
        return Path(f"/proc/self/fd/{file.fileno()}").resolve(strict=True)
    raise BridgeError("artifact_handle_verification_unsupported")


def read_artifact(workdir, artifact, max_bytes=64 << 20):
    require(isinstance(artifact, dict) and not artifact.get("error"), "artifact_unavailable")
    relative = relative_file(artifact.get("path"))
    require(
        type(artifact.get("bytes")) is int
        and 0 <= artifact["bytes"] <= max_bytes
        and isinstance(artifact.get("sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"]),
        "invalid_artifact_receipt",
    )
    root = Path(workdir)
    path = root / relative
    try:
        no_links(path)
        require(
            path.resolve().is_relative_to(root) and path.resolve() != root,
            "artifact_outside_workdir",
        )
        with path.open("rb") as file:
            before = os.fstat(file.fileno())
            require(
                stat.S_ISREG(before.st_mode) and before.st_size == artifact["bytes"],
                "artifact_changed",
            )
            require(
                opened_path(file) == path.resolve() and opened_path(file).is_relative_to(root),
                "artifact_outside_workdir",
            )
            data = file.read(max_bytes + 1)
            after = os.fstat(file.fileno())
            current = path.stat()
            no_links(path)
            require(
                opened_path(file) == path.resolve()
                and os.path.samestat(before, current)
                and (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)
                and len(data) == artifact["bytes"],
                "artifact_changed",
            )
        require(hashlib.sha256(data).hexdigest() == artifact["sha256"], "artifact_hash_mismatch")
        return data
    except OSError:
        raise BridgeError("artifact_unavailable") from None


class Bridge:
    def __init__(self, config):
        self.client = MatrixClient(config)
        self.config = config
        self.work_root = absolute_path(config.get("work_root"))
        self.state_dir = absolute_path(config.get("state_dir"))
        require(self.work_root.is_dir(), "work_root_required")
        no_links(self.work_root)
        require(
            not self.state_dir.is_relative_to(self.work_root)
            and not self.work_root.is_relative_to(self.state_dir),
            "state_work_separation_required",
        )
        self.state_dir.mkdir(parents=True, exist_ok=True)
        no_links(self.state_dir)
        self.database = self.state_dir / "bridge.sqlite3"
        if self.database.exists():
            no_links(self.database)
        models, tools = config.get("allowed_models"), config.get("allowed_tools")
        require(
            isinstance(models, list) and models and all(isinstance(m, str) and m for m in models),
            "model_allowlist_required",
        )
        require(
            isinstance(tools, list) and all(isinstance(t, str) and t for t in tools),
            "tool_allowlist_required",
        )
        with self.transaction() as db:
            from commands import initialize

            initialize(db)
            schema = """
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, fingerprint TEXT NOT NULL,
                    spec TEXT NOT NULL, agent_id TEXT, state TEXT NOT NULL, record TEXT,
                    activity TEXT NOT NULL DEFAULT '{}', activity_cursor INTEGER NOT NULL DEFAULT 0,
                    event_cursor INTEGER NOT NULL DEFAULT 0, last_error TEXT);
                CREATE TABLE IF NOT EXISTS operations (
                    task_id TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    generation INTEGER NOT NULL, state TEXT NOT NULL, PRIMARY KEY(task_id,request_id));
                CREATE TABLE IF NOT EXISTS receipts (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                    kind TEXT NOT NULL, at TEXT NOT NULL, payload TEXT NOT NULL);
            """
            for statement in schema.split(";"):
                if statement.strip():
                    db.execute(statement)
            binding = canonical(
                {
                    "runtime": self.client.binding,
                    "work_root": str(self.work_root),
                    "mode": self.client.mode,
                }
            )
            db.execute("INSERT OR IGNORE INTO meta VALUES ('binding', ?)", (binding,))
            require(
                db.execute("SELECT value FROM meta WHERE key='binding'").fetchone()[0] == binding,
                "ledger_runtime_mismatch",
            )

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.database, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            if db.in_transaction:
                db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    def _receipt(self, db, task_id, kind, payload):
        db.execute(
            "INSERT INTO receipts(task_id,kind,at,payload) VALUES (?,?,?,?)",
            (task_id, kind, now(), canonical(self.client.clean(payload))),
        )

    def _row(self, db, task_id):
        row = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        require(row is not None, "task_unknown_locally")
        return row

    def _spec(self, envelope):
        require(isinstance(envelope, dict), "invalid_task")
        require(
            not set(envelope)
            - {
                "version",
                "task_id",
                "model_id",
                "task",
                "prompt",
                "tools",
                "title",
                "max_requests",
                "timeout_seconds",
                "max_tokens",
                "contract",
            }
            and envelope.get("version") == 1,
            "invalid_task",
        )
        tid = envelope.get("task_id")
        require(
            isinstance(tid, str)
            and re.fullmatch(r"alpha-[A-Za-z0-9][A-Za-z0-9_.-]{0,89}", tid)
            and not tid.endswith("."),
            "invalid_alpha_task_id",
        )
        require(envelope.get("model_id") in self.config["allowed_models"], "model_not_allowed")
        tools = envelope.get("tools", [])
        require(
            isinstance(tools, list)
            and all(isinstance(t, str) and t in self.config["allowed_tools"] for t in tools)
            and len(tools) == len(set(tools)),
            "tools_not_allowed",
        )
        spec = {
            "request_id": "control:" + tid,
            "workdir": str(self.work_root / tid),
            "model_id": envelope["model_id"],
            "tools": sorted(tools),
        }
        for key, default, limit in [("task", "", 65536), ("prompt", "", 32768), ("title", "", 256)]:
            value = envelope.get(key, default)
            require(
                isinstance(value, str)
                and len(value.encode("utf-8")) <= limit
                and (key != "task" or value.strip()),
                "invalid_task_text",
            )
            spec[key] = value
        for key, default, minimum, maximum in [
            ("max_requests", 1, 1, 128),
            ("timeout_seconds", 60, 10, 3600),
            ("max_tokens", 10000, 1, 10000000),
        ]:
            value = envelope.get(key, default)
            require(type(value) is int and minimum <= value <= maximum, "invalid_task_budget")
            spec[key] = value
        contract = envelope.get("contract")
        require(
            isinstance(contract, dict)
            and not set(contract) - {"acceptance", "deliverables", "constraints"},
            "contract_required",
        )
        require(
            isinstance(contract.get("acceptance"), list) and 1 <= len(contract["acceptance"]) <= 24,
            "invalid_contract",
        )
        normalized = {}
        for name, cap in [("acceptance", 24), ("constraints", 24), ("deliverables", 32)]:
            values = contract.get(name, [])
            require(isinstance(values, list) and len(values) <= cap, "invalid_contract")
            if name == "deliverables":
                normalized[name] = [relative_file(x) for x in values]
                require(
                    len({x.lower() for x in normalized[name]}) == len(values), "duplicate_artifact"
                )
            else:
                require(
                    all(
                        isinstance(x, str) and x.strip() and len(x.encode("utf-8")) <= 2048
                        for x in values
                    ),
                    "invalid_contract",
                )
                normalized[name] = [x.strip() for x in values]
        require(len(canonical(normalized).encode("utf-8")) <= 32768, "invalid_contract")
        spec["contract"] = normalized
        require(self.client.clean(spec) == spec, "credential_in_payload")
        return tid, spec

    def dispatch(self, envelope):
        tid, spec = self._spec(envelope)
        fresh = False
        with self.transaction() as db:
            row = db.execute("SELECT * FROM tasks WHERE task_id=?", (tid,)).fetchone()
            if row:
                require(row["fingerprint"] == digest(spec), "task_id_conflict")
            else:
                workdir = Path(spec["workdir"])
                # Existing files might belong to a user or a previous lost ledger.
                require(not workdir.exists(), "isolated_workdir_already_exists")
                workdir.mkdir()
                no_links(workdir)
                db.execute(
                    "INSERT INTO tasks(task_id,request_id,fingerprint,spec,state) VALUES (?,?,?,?,?)",
                    (tid, spec["request_id"], digest(spec), canonical(spec), "submitting"),
                )
                self._receipt(
                    db,
                    tid,
                    "local_received",
                    {
                        "spec_sha256": digest(spec),
                        "runtime": self.client.binding,
                        "fixture": self.client.mode == "fixture",
                    },
                )
                self._receipt(db, tid, "submitting", {"request_id": spec["request_id"]})
                fresh = True
        if not fresh:
            return self.status(tid)
        try:
            record = self.client.rpc("agents.create", spec)
            self._ingest(tid, record)
        except BridgeError as exc:
            self._unknown(tid, str(exc))
        return self.snapshot(tid)

    def _unknown(self, tid, code):
        with self.transaction() as db:
            row = self._row(db, tid)
            db.execute(
                "UPDATE tasks SET state=?,last_error=? WHERE task_id=?",
                (row["state"] if row["agent_id"] else "unknown", code, tid),
            )
            self._receipt(db, tid, "observation_failed", {"code": code, "replay_allowed": False})

    @staticmethod
    def _normalized_record_spec(spec):
        require(isinstance(spec, dict), "invalid_agent_spec")
        spec = json.loads(canonical(spec))
        spec.setdefault("title", "")
        if spec.get("tools") is None:
            spec["tools"] = []  # Go append(nil, empty...) serializes as null.
        contract = spec.get("contract")
        if isinstance(contract, dict):
            contract.setdefault("constraints", [])
            contract.setdefault("deliverables", [])
        return spec

    def _ingest(self, tid, record):
        require(isinstance(record, dict), "invalid_agent_record")
        require(
            isinstance(record.get("id"), str)
            and record["id"]
            and record.get("parent") == self.config["expected_session"],
            "agent_identity_mismatch",
        )
        require(
            type(record.get("generation")) is int and record["generation"] >= 1,
            "invalid_generation",
        )
        require(
            record.get("state")
            in {
                "queued",
                "running",
                "cancelling",
                "waiting_user",
                "completed",
                "failed",
                "cancelled",
                "timed_out",
                "interrupted",
                "persistence_failed",
            },
            "unknown_os_state",
        )
        updated = timestamp(record.get("updated"))
        require(record.get("acceptance") in {"pending", "accepted", "rework"}, "invalid_acceptance")
        with self.transaction() as db:
            row = self._row(db, tid)
            require(row["agent_id"] in (None, record["id"]), "agent_identity_mismatch")
            require(
                digest(self._normalized_record_spec(record.get("spec"))) == row["fingerprint"],
                "agent_spec_mismatch",
            )
            old = json.loads(row["record"]) if row["record"] else None
            if old:
                require(record["generation"] >= old["generation"], "stale_generation")
                # Concurrent observers must not overwrite newer server facts.
                if record["generation"] == old["generation"]:
                    require(updated >= timestamp(old["updated"]), "stale_observation")
                    terminal = {
                        "completed",
                        "failed",
                        "cancelled",
                        "timed_out",
                        "interrupted",
                        "persistence_failed",
                    }
                    require(
                        not (old["state"] in terminal and record["state"] not in terminal),
                        "state_regressed",
                    )
                    require(
                        not (old["acceptance"] == "accepted" and record["acceptance"] == "pending"),
                        "acceptance_regressed",
                    )
            phase = "received" if record["state"] == "queued" else record["state"]
            reviews = record.get("reviews") or []
            require(
                isinstance(reviews, list) and all(isinstance(r, dict) for r in reviews),
                "invalid_review_record",
            )
            if record.get("acceptance") == "accepted":
                require(
                    record["state"] == "completed"
                    and isinstance(record.get("delivery"), dict)
                    and record["delivery"].get("generation") == record["generation"]
                    and any(
                        r.get("verdict") == "accepted"
                        and r.get("generation") == record["generation"]
                        for r in reviews
                    ),
                    "acceptance_receipt_missing",
                )
                phase = "accepted"
            projection = {
                k: record.get(k)
                for k in (
                    "id",
                    "state",
                    "acceptance",
                    "generation",
                    "updated",
                    "result",
                    "error",
                    "usage",
                    "delivery",
                    "reviews",
                    "window_error",
                )
            }
            if not row["agent_id"]:
                self._receipt(
                    db,
                    tid,
                    "received",
                    {
                        "agent_id": record["id"],
                        "generation": record["generation"],
                        "os_state": record["state"],
                    },
                )
            if row["state"] != phase or not old or old["generation"] != record["generation"]:
                if phase != "received":
                    self._receipt(
                        db,
                        tid,
                        phase,
                        {
                            "agent_id": record["id"],
                            "generation": record["generation"],
                            "acceptance": record.get("acceptance"),
                        },
                    )
            db.execute(
                "UPDATE tasks SET agent_id=?,state=?,record=?,last_error=NULL WHERE task_id=?",
                (record["id"], phase, canonical(projection), tid),
            )
            for review in reviews:
                db.execute(
                    "UPDATE operations SET state='observed' WHERE task_id=? AND request_id=? AND generation=?",
                    (tid, review.get("request_id"), review.get("generation")),
                )

    def status(self, tid):
        with self.transaction() as db:
            row = dict(self._row(db, tid))
        try:
            if row["agent_id"]:
                record = self.client.rpc("agents.status", {"id": row["agent_id"]})
            else:
                summaries = self.client.rpc("agents.list")
                require(isinstance(summaries, list) and len(summaries) <= 64, "invalid_agent_list")
                matches = []
                for item in summaries:
                    require(
                        isinstance(item, dict) and isinstance(item.get("id"), str),
                        "invalid_agent_list",
                    )
                    candidate = self.client.rpc("agents.status", {"id": item["id"]})
                    require(
                        isinstance(candidate, dict) and isinstance(candidate.get("spec"), dict),
                        "invalid_agent_record",
                    )
                    if (
                        candidate.get("parent") == self.config["expected_session"]
                        and candidate["spec"].get("request_id") == row["request_id"]
                    ):
                        matches.append(candidate)
                require(
                    len(matches) == 1,
                    "request_lookup_ambiguous" if matches else "request_not_observed",
                )
                record = matches[0]
            self._ingest(tid, record)
        except BridgeError as exc:
            self._unknown(tid, str(exc))
        return self.snapshot(tid)

    def snapshot(self, tid):
        with self.transaction() as db:
            row = self._row(db, tid)
            record = json.loads(row["record"]) if row["record"] else {}
            return {
                "version": 1,
                "task_id": tid,
                "request_id": row["request_id"],
                "agent_id": row["agent_id"],
                "state": row["state"],
                "os_received": row["agent_id"] is not None,
                "fixture": self.client.mode == "fixture",
                "source": "os-matrix-rpc",
                "last_error": row["last_error"],
                "record": record,
                "activity_cursor": row["activity_cursor"],
                "event_cursor": row["event_cursor"],
                "review_operations": [
                    dict(x)
                    for x in db.execute(
                        "SELECT request_id,generation,state FROM operations WHERE task_id=?", (tid,)
                    )
                ],
            }

    def progress(self, tid):
        status = self.status(tid)
        require(status["os_received"] and not status["last_error"], "task_not_currently_observed")
        with self.transaction() as db:
            row = self._row(db, tid)
            since, cursor = row["activity_cursor"], row["event_cursor"]
        # One bounded page of each per call; partial tails never busy-loop.
        activity = self.client.rpc(
            "agents.activity", {"id": status["agent_id"], "since": since, "limit": 120}
        )
        events = self.client.rpc(
            "agents.events", {"id": status["agent_id"], "cursor": cursor, "limit": 20}
        )
        require(
            isinstance(activity, dict)
            and type(activity.get("cursor")) is int
            and activity["cursor"] >= since
            and isinstance(activity.get("items"), list),
            "invalid_activity_page",
        )
        require(
            isinstance(events, dict)
            and type(events.get("next_cursor")) is int
            and events["next_cursor"] >= cursor
            and isinstance(events.get("events"), list),
            "invalid_event_page",
        )
        require(not events["events"] or events["next_cursor"] > cursor, "invalid_event_page")
        require(events["events"] or events["next_cursor"] == cursor, "invalid_event_page")
        with self.transaction() as db:
            row = self._row(db, tid)
            require(
                row["activity_cursor"] == since and row["event_cursor"] == cursor,
                "progress_cursor_raced_retry_read",
            )
            merged = json.loads(row["activity"])
            for item in activity["items"]:
                require(
                    isinstance(item, dict)
                    and isinstance(item.get("id"), str)
                    and type(item.get("revision")) is int
                    and 0 < item["revision"] <= activity["cursor"],
                    "invalid_activity_item",
                )
                previous = merged.get(item["id"])
                if not previous or previous["revision"] < item["revision"]:
                    merged[item["id"]] = item
            if activity["items"] or activity["cursor"] != since:
                self._receipt(db, tid, "activity_page", {"from_revision": since, **activity})
            if events["next_cursor"] != cursor:
                self._receipt(db, tid, "event_page", {"from_byte": cursor, **events})
            db.execute(
                "UPDATE tasks SET activity=?,activity_cursor=?,event_cursor=? WHERE task_id=?",
                (canonical(merged), activity["cursor"], events["next_cursor"], tid),
            )
        return {
            "task": self.snapshot(tid),
            "activity": list(merged.values()),
            "activity_has_more": bool(activity.get("has_more")),
            "activity_has_older": bool(activity.get("has_older")),
            "activity_archived_count": activity.get("archived_count", 0),
            "events": events,
            "tail_pending": bool(events.get("has_more") and events["next_cursor"] == cursor),
        }

    def artifact(self, tid, relative, include_content=False):
        state = self.status(tid)
        require(not state["last_error"], "task_not_currently_observed")
        delivery = state["record"].get("delivery")
        require(
            isinstance(delivery, dict)
            and delivery.get("generation") == state["record"]["generation"],
            "delivery_unavailable",
        )
        relative = relative_file(relative)
        with self.transaction() as db:
            spec = json.loads(self._row(db, tid)["spec"])
        require(relative in spec["contract"]["deliverables"], "artifact_not_declared")
        matches = [a for a in delivery.get("artifacts", []) if a.get("path") == relative]
        require(len(matches) == 1, "artifact_unavailable")
        data = read_artifact(
            spec["workdir"], matches[0], (1 << 20) if include_content else (64 << 20)
        )
        result = {
            "task_id": tid,
            "generation": delivery["generation"],
            **matches[0],
            "verified_at": now(),
        }
        if include_content:
            # Only explicit reads return bytes; receipts and center exports never do.
            require(
                self.client.clean(data.decode("utf-8", errors="replace"))
                == data.decode("utf-8", errors="replace"),
                "credential_in_artifact",
            )
            result["content_base64"] = base64.b64encode(data).decode("ascii")
        return result

    def review(self, tid, review):
        require(
            isinstance(review, dict)
            and not set(review) - {"request_id", "generation", "verdict", "evidence", "checks"},
            "invalid_review",
        )
        require(
            isinstance(review.get("request_id"), str)
            and 1 <= len(review["request_id"]) <= 128
            and type(review.get("generation")) is int,
            "invalid_review",
        )
        require(
            review.get("verdict") == "accepted"
            and isinstance(review.get("evidence"), str)
            and review["evidence"].strip(),
            "independent_evidence_required",
        )
        require(
            len(canonical(review).encode("utf-8")) <= 65536 and self.client.clean(review) == review,
            "invalid_review",
        )
        state = self.status(tid)
        require(not state["last_error"], "task_not_currently_observed")
        with self.transaction() as db:
            op = db.execute(
                "SELECT * FROM operations WHERE task_id=? AND request_id=?",
                (tid, review["request_id"]),
            ).fetchone()
            if op:
                require(op["fingerprint"] == digest(review), "review_id_conflict")
                return state
            spec = json.loads(self._row(db, tid)["spec"])
        record = state["record"]
        require(
            record.get("state") == "completed" and record.get("generation") == review["generation"],
            "review_requires_current_completed_generation",
        )
        checks = review.get("checks")
        require(
            isinstance(checks, list) and len(checks) == len(spec["contract"]["acceptance"]),
            "criterion_checks_required",
        )
        seen = set()
        for check in checks:
            require(
                isinstance(check, dict)
                and type(check.get("criterion")) is int
                and 0 <= check["criterion"] < len(checks)
                and check["criterion"] not in seen
                and check.get("verdict") == "passed"
                and isinstance(check.get("evidence"), str)
                and check["evidence"].strip(),
                "invalid_criterion_check",
            )
            seen.add(check["criterion"])
            refs = check.get("receipts")
            require(
                isinstance(refs, list)
                and 1 <= len(refs) <= 8
                and all(
                    isinstance(r, dict)
                    and type(r.get("session_id")) is int
                    and r["session_id"] > 0
                    and type(r.get("expected_exit")) is int
                    and r.get("agent_id") != state["agent_id"]
                    for r in refs
                ),
                "independent_os_receipts_required",
            )
        total = 0
        for path in spec["contract"]["deliverables"]:
            total += self.artifact(tid, path)["bytes"]
            require(total <= 256 << 20, "delivery_too_large")
        with self.transaction() as db:
            op = db.execute(
                "SELECT * FROM operations WHERE task_id=? AND request_id=?",
                (tid, review["request_id"]),
            ).fetchone()
            if op:
                require(op["fingerprint"] == digest(review), "review_id_conflict")
                fresh = False
            else:
                db.execute(
                    "INSERT INTO operations VALUES (?,?,?,?,?)",
                    (tid, review["request_id"], digest(review), review["generation"], "submitting"),
                )
                self._receipt(
                    db,
                    tid,
                    "review_submitting",
                    {
                        "request_id": review["request_id"],
                        "generation": review["generation"],
                        "review_sha256": digest(review),
                    },
                )
                fresh = True
        if fresh:
            try:
                self._ingest(
                    tid, self.client.rpc("agents.review", {"id": state["agent_id"], **review})
                )
            except BridgeError as exc:
                with self.transaction() as db:
                    db.execute(
                        "UPDATE operations SET state='unknown' WHERE task_id=? AND request_id=? AND state!='observed'",
                        (tid, review["request_id"]),
                    )
                    self._receipt(
                        db,
                        tid,
                        "review_unknown",
                        {
                            "request_id": review["request_id"],
                            "code": str(exc),
                            "replay_allowed": False,
                        },
                    )
        return self.snapshot(tid)

    def receipts(self, tid, after=0, limit=100):
        require(
            type(after) is int and after >= 0 and type(limit) is int and 1 <= limit <= 400,
            "invalid_receipt_cursor",
        )
        with self.transaction() as db:
            self._row(db, tid)
            rows = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM receipts WHERE task_id=? AND seq>? ORDER BY seq LIMIT ?",
                    (tid, after, limit),
                )
            ]
        for row in rows:
            row["payload"] = json.loads(row["payload"])
        return {
            "task_id": tid,
            "receipts": rows,
            "next_cursor": rows[-1]["seq"] if rows else after,
            "fixture": self.client.mode == "fixture",
        }


def execute(config, request, human_host_ref=None, product_host_ref=None):
    bridge = Bridge(config)
    require(isinstance(request, dict), "invalid_operation")
    op, tid = request.get("operation"), request.get("task_id")
    if op == "product_job_sync":
        require(
            set(request) == {"operation"} and product_host_ref is not None,
            "product_host_not_configured",
        )
        from product_job_poll import run

        return run(bridge, product_host_ref)
    if op == "human_sync":
        require(
            set(request) == {"operation"} and human_host_ref is not None,
            "human_host_not_configured",
        )
        from human_relay import run

        return run(bridge, human_host_ref)
    if op == "human_list":
        require(set(request) == {"operation"}, "invalid_operation")
        return bridge.client.rpc("humans.list")
    if op == "command":
        from commands import command

        return command(bridge, request.get("command"))
    if op == "command_status":
        from commands import observe

        return observe(bridge, request.get("request_id"))
    if op == "matrix_status":
        return bridge.client.rpc("matrix.status", {"request_id": request.get("request_id", "")})
    if op == "dispatch":
        return bridge.dispatch(request.get("task"))
    if op == "status":
        return bridge.status(tid)
    if op == "progress":
        return bridge.progress(tid)
    if op == "artifact":
        return bridge.artifact(tid, request.get("path"), request.get("include_content") is True)
    if op == "review":
        return bridge.review(tid, request.get("review"))
    if op == "receipts":
        return bridge.receipts(tid, request.get("after", 0), request.get("limit", 100))
    raise BridgeError("operation_not_exposed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", required=True, help="Local config path with discovery reference, never a token"
    )
    parser.add_argument(
        "--human-host-ref",
        help="Explicit local registered host configuration; never an HTTP-supplied path",
    )
    parser.add_argument(
        "--product-host-ref",
        help="Explicit original ProductHost configuration; never an HTTP-supplied path",
    )
    args = parser.parse_args()
    try:
        config = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
        raw = sys.stdin.buffer.read((1 << 20) + 1)
        require(len(raw) <= 1 << 20, "operation_too_large")
        value = execute(config, json.loads(raw), args.human_host_ref, args.product_host_ref)
        print(canonical({"ok": True, "data": value}))
        return 0
    except BridgeError as exc:
        print(canonical({"ok": False, "error": str(exc)}))
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        print(canonical({"ok": False, "error": "invalid_input_or_local_state"}))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
