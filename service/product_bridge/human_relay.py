"""One bounded host pass over native human requests, using the original bridge.

Center owns the shared question/decision/outbox; OS alone resumes its task.
All local intent and receipt data lives in bridge.sqlite3, alongside product jobs.
"""

import hashlib
import json
from pathlib import Path
import sys
import time

from bridge import timestamp
from commands import unknown
from human_commands import prepare, deliver_once, observe, match
from matrix_client import BridgeError, canonical, digest, require


class HumanRelay:
    def __init__(self, bridge, host, call):
        self.bridge, self.host, self.call = bridge, host, call
        require(host.get("version") == 1, "invalid_human_host_config")
        for key in ("host_id", "adapter_id", "adapter_version", "seat_id", "runtime_ref"):
            require(isinstance(host.get(key), str) and host[key], "invalid_human_host_config")
        require(
            host["runtime_ref"] == bridge.client.binding["session_id"], "human_session_mismatch"
        )
        require(
            bridge.client.description.get("human_callback_version") == 1,
            "human_callback_not_supported",
        )
        binding = {
            k: host[k]
            for k in ("host_id", "adapter_id", "adapter_version", "seat_id", "runtime_ref")
        }
        with bridge.transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO meta VALUES('human_host_binding',?)", (canonical(binding),)
            )
            require(
                db.execute("SELECT value FROM meta WHERE key='human_host_binding'").fetchone()[0]
                == canonical(binding),
                "human_host_binding_changed",
            )

    def once(self, path, body):
        """Unknown mutating HTTP responses are never retried by a host poll."""
        key = "human_http:" + body["request_id"]
        intent = {"path": path, "body": body}
        with self.bridge.transaction() as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            if row:
                saved = json.loads(row[0])
                require(saved["intent"] == intent, "human_http_identity_conflict")
                return saved.get("ack")
            db.execute(
                "INSERT INTO meta VALUES(?,?)",
                (key, canonical({"intent": intent, "state": "submitting"})),
            )
        try:
            ack = self.call(path, body)
        except Exception:
            with self.bridge.transaction() as db:
                db.execute(
                    "UPDATE meta SET value=? WHERE key=?",
                    (canonical({"intent": intent, "state": "unknown"}), key),
                )
            raise BridgeError("human_center_write_unknown_query_only") from None
        with self.bridge.transaction() as db:
            db.execute(
                "UPDATE meta SET value=? WHERE key=?",
                (canonical({"intent": intent, "state": "observed", "ack": ack}), key),
            )
        return ack

    def binding(self, native, seat):
        return {
            "seat_id": seat["id"],
            "seat_epoch": seat["epoch"],
            "runtime_generation": hashlib.sha256(
                (str(seat["epoch"]) + "\n" + seat["runtime_ref"]).encode()
            ).hexdigest(),
            "runtime_ref": seat["runtime_ref"],
            "agent_id": native["agent_id"],
            "native_task_id": native["agent_id"],
            "task_generation": native["generation"],
            "native_request_id": native["id"],
            "native_request_version": native["version"],
            "request_token_sha256": native["request_token_sha256"],
        }

    def creation(self, native, seat):
        origin = self.binding(native, seat)
        cloud_id = "human-os-" + digest(origin)[:48]
        question = native["question"] + (
            "\n已做事实：" + native["facts"] if native.get("facts") else ""
        )
        options = [
            {"id": x["id"], "label": x["label"] + " — " + x["impact"]} for x in native["options"]
        ]
        require(
            len(question) <= 2000
            and all(len(o["label"]) <= 240 for o in options)
            and all(len(native[k]) <= 1000 for k in ("recommendation", "impact", "resume_summary")),
            "native_human_projection_limit",
        )
        require(
            not options or not native.get("input_schema", {}).get("required"),
            "human_choice_and_required_text_not_supported",
        )
        expires = int(timestamp(native["expires_at"])) / 1e9
        remaining = int(expires - time.time())
        require(remaining >= 30, "native_human_expiring")
        return dict(
            request_id=cloud_id + "-create",
            id=cloud_id,
            origin=origin,
            category=native["category"],
            question=question,
            options=options,
            allow_text=not bool(options),
            expires_in=min(86400, remaining),
            observed_ns=str(time.time_ns()),
            **{k: native[k] for k in ("recommendation", "impact", "resume_summary")},
        )

    def publish(self, native, seat):
        key = "human_create:" + native["id"]
        with self.bridge.transaction() as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            if row:
                body = json.loads(row[0])
                require(body["origin"] == self.binding(native, seat), "human_origin_changed")
            else:
                if native["state"] != "waiting_user":
                    return None
                body = self.creation(native, seat)
                db.execute("INSERT INTO meta VALUES(?,?)", (key, canonical(body)))
        try:
            # Stable create body is persisted before its single HTTP attempt.
            self.once("/v1/human-request-create", body)
        except BridgeError:
            pass
        return self.call("/v1/human-request?id=" + body["id"])

    def receipt(self, remote, native):
        state = native["state"]
        if state == "waiting_user" and remote.get("begun_at") is not None:
            state = "unknown"
        if state == "decision_recorded":
            return  # A saved answer has not yet reached the running archive.
        if state in ("cancelled", "expired") and remote.get("begun_at") is None:
            if remote["state"] not in ("cancelled", "expired"):
                self.once(
                    "/v1/human-request-close",
                    {
                        "request_id": remote["id"] + "-close",
                        "id": remote["id"],
                        "version": remote["version"],
                        "reason": "Original OS request is " + state,
                    },
                )
            return
        if state not in (
            "waiting_user",
            "delivered_to_agent",
            "resuming",
            "resolved",
            "failed",
            "unknown",
            "cancelled",
            "expired",
        ):
            return
        if state == "waiting_user" and remote["state"] != "waiting_user":
            return
        if state in ("cancelled", "expired"):
            state = "failed"
        if (
            state == remote.get("last_fact")
            and state != "waiting_user"
            and int(timestamp(native["updated_at"])) <= int(remote.get("observed_ns") or 0)
        ):
            return
        if state == "unknown" and remote["state"] == "unknown":
            return
        # An actual fresh RPC observation refreshes a waiting item, at most once
        # a minute. It cannot regress a simultaneously committed human decision.
        if state == "waiting_user" and time.time() - (remote.get("observed_at") or 0) < 60:
            return
        evidence = {
            k: native.get(k)
            for k in (
                "id",
                "version",
                "session_id",
                "agent_id",
                "generation",
                "state",
                "updated_at",
                "decision_id",
            )
        }
        evidence_hash = digest(evidence)
        rid = "hr-" + digest([remote["id"], evidence_hash, remote["version"]])[:60]
        body = dict(
            request_id=rid,
            id=remote["id"],
            version=remote["version"],
            receipt_id=rid,
            origin_sha256=remote["origin_sha256"],
            state=state,
            observed_ns=str(time.time_ns()),
            evidence_ref="native-" + digest([native["id"], evidence_hash])[:48],
            evidence_sha256=evidence_hash,
        )
        # Each immutable observation and request is kept in the existing receipts.
        key = "human_fact:" + digest([remote["id"], evidence_hash, remote["version"]])
        with self.bridge.transaction() as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            if row:
                body = json.loads(row[0])
            else:
                db.execute("INSERT INTO meta VALUES(?,?)", (key, canonical(body)))
                self.bridge._receipt(
                    db, "@human:" + native["id"], "human_native_observation", evidence
                )
        self.once("/v1/human-request-receipt", body)

    def process(self, native, seat):
        remote = self.publish(native, seat)
        if remote is None:
            return None
        origin = self.binding(native, seat)
        require(all(remote.get(k) == v for k, v in origin.items()), "human_cloud_origin_mismatch")
        if remote["state"] == "decision_recorded" and remote.get("can_begin") is True:
            reply = dict(
                request_id=remote["decision_request_id"],
                session_id=native["session_id"],
                agent_id=native["agent_id"],
                generation=native["generation"],
                human_id=native["id"],
                expected_request_version=native["version"],
                request_token_sha256=native["request_token_sha256"],
                decision=remote["decision"],
            )
            value, fresh = prepare(self.bridge, reply)
            if fresh:
                try:
                    begun = self.once(
                        "/v1/human-request-begin",
                        dict(
                            request_id=remote["id"] + "-begin",
                            id=remote["id"],
                            version=remote["version"],
                            origin_sha256=remote["origin_sha256"],
                        ),
                    )
                    current = self.call("/v1/human-request?id=" + remote["id"])
                    require(
                        begun
                        and begun.get("callback_allowed") is True
                        and time.time() < begun["callback_permit_until"]
                        and current["version"] == begun["version"]
                        and current["state"] == "submitting"
                        and current.get("binding_current") is True
                        and not current.get("recovery_hold"),
                        "human_permit_expired_or_changed",
                    )
                    latest = self.bridge.client.rpc("humans.status", {"id": native["id"]})
                    match(value, latest)
                    require(latest["state"] == "waiting_user", "human_request_not_waiting")
                    deliver_once(self.bridge, value)
                except Exception:
                    unknown(self.bridge, value, "human_callback_unknown_query_only")
            else:
                observe(self.bridge, value)
        # Even after a lost begin/reply response this is query only. No new task.
        native = self.bridge.client.rpc("humans.status", {"id": native["id"]})
        remote = self.call("/v1/human-request?id=" + remote["id"])
        self.receipt(remote, native)
        return {
            "id": remote["id"],
            "native_request_id": native["id"],
            "native_state": native["state"],
            "center_state": remote["state"],
        }

    def observe_runtime(self):
        native = self.bridge.client.rpc("humans.list")
        matrix = self.bridge.client.rpc("matrix.status")
        agents = self.bridge.client.rpc("agents.list")
        require(
            isinstance(native, list)
            and len(native) <= 2048
            and isinstance(matrix.get("busy"), bool),
            "invalid_human_native_list",
        )
        require(isinstance(agents, list) and len(agents) <= 64, "invalid_native_agent_list")
        registry = self.call("/v1/registry")
        seat = next((s for s in registry["seats"] if s["id"] == self.host["seat_id"]), None)
        require(
            seat is not None
            and all(seat.get(k) == self.host[k] for k in ("host_id", "adapter_id", "runtime_ref")),
            "human_registered_binding_mismatch",
        )
        if time.time() - seat.get("observed_at", 0) >= 60:
            observed = time.time()
            busy = matrix["busy"] or any(
                x.get("state") in ("queued", "running", "cancelling") for x in agents
            )
            evidence = digest(
                {
                    "runtime": self.bridge.client.binding,
                    "busy": busy,
                    "human_ids": [x["id"] for x in native],
                }
            )
            body = dict(
                request_id="os-observe-" + digest([seat["id"], seat["version"]])[:48],
                id=seat["id"],
                version=seat["version"],
                seat_epoch=seat["epoch"],
                runtime_ref=seat["runtime_ref"],
                adapter_version=self.host["adapter_version"],
                observed_state="running" if busy else "idle",
                observed_at=observed,
                evidence_ref="os-observation-" + evidence[:40],
                evidence_sha256=evidence,
            )
            # A lost observation keeps its exact evidence/time under the original ID.
            # It must not conflict with a newly generated timestamp on the next poll.
            with self.bridge.transaction() as db:
                old = db.execute(
                    "SELECT value FROM meta WHERE key=?", ("human_http:" + body["request_id"],)
                ).fetchone()
                if old:
                    body = json.loads(old[0])["intent"]["body"]
            require(
                self.once("/v1/seat-observe", body) is not None,
                "native_observation_unconfirmed_query_only",
            )
        return native, seat

    def run_once(self):
        native, seat = self.observe_runtime()
        results, errors = [], []
        for item in native:
            try:
                result = self.process(item, seat)
                if result:
                    results.append(result)
            except Exception as exc:
                code = str(exc) if isinstance(exc, BridgeError) else "human_sync_unknown_query_only"
                errors.append({"native_request_id": item.get("id"), "error": code})
        return {
            "source": "existing_product_os_bridge",
            "items": results,
            "errors": errors,
            "replay_allowed": False,
        }


def run(bridge, path):
    host = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    client_dir = Path(host["center_client_dir"])
    require(client_dir.is_absolute() and client_dir.is_dir(), "explicit_center_client_required")
    sys.path.insert(0, str(client_dir))
    import coord

    return HumanRelay(
        bridge, host, lambda route, body=None: coord.call(host["center_identity"], route, body)
    ).run_once()
