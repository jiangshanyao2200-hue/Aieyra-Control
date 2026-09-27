"""Explicit turns and cancellation in the existing bridge SQLite ledger.

A committed intent is attempted once. Every subsequent call only observes.
"""

import hashlib
import json
import re

from matrix_client import BridgeError, canonical, digest, require


def identifier(value):
    require(
        isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value),
        "invalid_command_id",
    )
    return value


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS commands (
        request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, payload TEXT NOT NULL,
        state TEXT NOT NULL, observation TEXT, last_error TEXT)""")
    if "target" not in {row[1] for row in db.execute("PRAGMA table_info(commands)")}:
        db.execute("ALTER TABLE commands ADD COLUMN target TEXT")


def snapshot(bridge, request_id):
    with bridge.transaction() as db:
        row = db.execute(
            "SELECT * FROM commands WHERE request_id=?", (identifier(request_id),)
        ).fetchone()
        require(row is not None, "command_unknown_locally")
        payload = json.loads(row["payload"])
        return {
            "version": 1,
            "request_id": request_id,
            "action": payload["action"],
            "task_id": payload.get("task_id"),
            "agent_id": payload.get("agent_id"),
            "target_request_id": payload.get("target_request_id"),
            "generation": payload.get("generation"),
            "state": row["state"],
            "observation": json.loads(row["observation"]) if row["observation"] else None,
            "last_error": row["last_error"],
            "replay_allowed": False,
            "fixture": bridge.client.mode == "fixture",
        }


def normalized(value):
    require(isinstance(value, dict), "invalid_command")
    action = value.get("action")
    allowed = {"action", "request_id"}
    if action == "matrix.send":
        allowed |= {"message"}
    elif action == "matrix.cancel":
        allowed |= {"target_request_id"}
    elif action in {"agents.send", "agents.cancel"}:
        require(("task_id" in value) != ("agent_id" in value), "exactly_one_agent_target_required")
        allowed |= {"task_id" if "task_id" in value else "agent_id", "generation"}
        if action == "agents.send":
            allowed |= {"message"}
    else:
        raise BridgeError("command_not_exposed")
    require(set(value) == allowed, "invalid_command")
    identifier(value["request_id"])
    if "target_request_id" in value:
        identifier(value["target_request_id"])
    if "task_id" in value:
        require(isinstance(value["task_id"], str), "invalid_task_id")
    if "agent_id" in value:
        identifier(value["agent_id"])
    if "generation" in value:
        require(
            type(value["generation"]) is int and 1 <= value["generation"] <= 16,
            "invalid_generation",
        )
    if "message" in value:
        require(
            isinstance(value["message"], str)
            and value["message"].strip()
            and len(value["message"].encode("utf-8")) <= 65536,
            "invalid_message",
        )
    return dict(value)


def target_key(value):
    return value.get("task_id") or (
        "@agent:" + value["agent_id"] if value.get("agent_id") else "@matrix"
    )


def runtime_target(bridge, agent_id, record, expected=None):
    # Matrix-created agents have no bridge job. Bind their existing runtime identity,
    # without manufacturing a dispatch/task receipt or copying their private prompt.
    from bridge import timestamp

    require(isinstance(record, dict) and record.get("id") == agent_id, "agent_identity_mismatch")
    require(
        type(record.get("generation")) is int and 1 <= record["generation"] <= 16,
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
        "invalid_agent_state",
    )
    require(record.get("acceptance") in {"pending", "accepted", "rework"}, "invalid_acceptance")
    if record["acceptance"] == "accepted":
        delivery, reviews = record.get("delivery"), record.get("reviews") or []
        require(
            record["state"] == "completed"
            and isinstance(delivery, dict)
            and delivery.get("generation") == record["generation"]
            and isinstance(reviews, list)
            and any(
                isinstance(r, dict)
                and r.get("verdict") == "accepted"
                and r.get("generation") == record["generation"]
                for r in reviews
            ),
            "acceptance_receipt_missing",
        )
    timestamp(record.get("updated"))
    spec = record.get("spec")
    require(isinstance(spec, dict), "invalid_agent_spec")
    binding = {
        "id": agent_id,
        "parent": record.get("parent"),
        "depth": record.get("depth"),
        "origin_request": record.get("origin_request"),
        "spec_sha256": digest(spec),
    }
    if expected is not None:
        require(binding == expected, "agent_target_changed")
        return binding
    listing = bridge.client.rpc("agents.list")
    require(
        isinstance(listing, list)
        and any(isinstance(row, dict) and row.get("id") == agent_id for row in listing),
        "agent_not_owned",
    )
    parent, seen = record.get("parent"), {agent_id}
    for _ in range(2):
        if parent == bridge.client.binding["session_id"]:
            return binding
        require(isinstance(parent, str) and parent and parent not in seen, "agent_not_owned")
        seen.add(parent)
        ancestor = bridge.client.rpc("agents.status", {"id": parent})
        require(isinstance(ancestor, dict) and ancestor.get("id") == parent, "agent_not_owned")
        parent = ancestor.get("parent")
    raise BridgeError("agent_not_owned")


def command(bridge, value):
    value = normalized(value)
    require(bridge.client.clean(value) == value, "credential_in_payload")
    rid, action = value["request_id"], value["action"]
    with bridge.transaction() as db:
        row = db.execute("SELECT fingerprint FROM commands WHERE request_id=?", (rid,)).fetchone()
        if row:
            require(row["fingerprint"] == digest(value), "command_id_conflict")
    if row:
        return observe(bridge, rid)
    target = None
    if action.startswith("matrix."):
        require(bridge.config.get("enable_matrix_writes") is True, "matrix_writes_disabled")
        if action == "matrix.cancel":
            with bridge.transaction() as db:
                target_row = db.execute(
                    "SELECT payload FROM commands WHERE request_id=?", (value["target_request_id"],)
                ).fetchone()
                require(
                    target_row is not None and json.loads(target_row[0])["action"] == "matrix.send",
                    "matrix_request_not_owned",
                )
    else:
        require(
            bridge.client.description.get("generation_guarded_commands") is True,
            "generation_guard_not_supported",
        )
        if "agent_id" in value:
            record = bridge.client.rpc("agents.status", {"id": value["agent_id"]})
            target = runtime_target(bridge, value["agent_id"], record)
            state = {"agent_id": value["agent_id"], "record": record}
            if action == "agents.send":
                spec = record["spec"]
                require(
                    spec.get("model_id") in bridge.config["allowed_models"], "model_not_allowed"
                )
                tools = spec.get("tools") or []
                require(
                    isinstance(tools, list)
                    and all(t in bridge.config["allowed_tools"] for t in tools),
                    "tools_not_allowed",
                )
        else:
            state = bridge.status(value["task_id"])
            require(state["os_received"] and not state["last_error"], "task_not_currently_observed")
        require(state["record"]["generation"] == value["generation"], "stale_generation")
        if action == "agents.send":
            require(
                state["record"]["state"] not in {"queued", "running", "cancelling", "waiting_user"},
                "agent_busy",
            )
    if bridge.client.mode == "os":
        require(bridge.config.get("enable_os_writes") is True, "os_writes_disabled")
    fresh = False
    with bridge.transaction() as db:
        row = db.execute("SELECT fingerprint FROM commands WHERE request_id=?", (rid,)).fetchone()
        if row:
            require(row["fingerprint"] == digest(value), "command_id_conflict")
        else:
            db.execute(
                "INSERT INTO commands(request_id,fingerprint,payload,state,target) VALUES (?,?,?,?,?)",
                (
                    rid,
                    digest(value),
                    canonical(value),
                    "submitting",
                    canonical(target) if target else None,
                ),
            )
            bridge._receipt(
                db,
                target_key(value),
                "command_submitting",
                {
                    "request_id": rid,
                    "action": action,
                    "generation": value.get("generation"),
                    "payload_sha256": digest(value),
                },
            )
            fresh = True
    if not fresh:
        return observe(bridge, rid)
    try:
        if action == "matrix.send":
            args = {"request_id": rid, "message": value["message"]}
        elif action == "matrix.cancel":
            args = {"request_id": value["target_request_id"]}
        else:
            args = {"id": state["agent_id"], "generation": value["generation"]}
            if action == "agents.send":
                args.update(request_id=rid, message=value["message"])
        result = bridge.client.rpc(action, args)
        # Cancel's RPC acknowledges intent; only status establishes its outcome.
        if action == "agents.cancel":
            require(
                isinstance(result, dict)
                and result.get("id") == args["id"]
                and result.get("requested") is True,
                "invalid_cancel_receipt",
            )
            return observe(bridge, rid)
        return ingest(bridge, value, result)
    except BridgeError as exc:
        return unknown(bridge, value, str(exc))


def unknown(bridge, value, code):
    with bridge.transaction() as db:
        db.execute(
            "UPDATE commands SET state=CASE WHEN state='observed' THEN state ELSE 'unknown' END,last_error=? WHERE request_id=?",
            (code, value["request_id"]),
        )
        bridge._receipt(
            db,
            target_key(value),
            "command_observation_failed",
            {"request_id": value["request_id"], "code": code, "replay_allowed": False},
        )
    return snapshot(bridge, value["request_id"])


def ingest(bridge, value, record):
    require(isinstance(record, dict), "invalid_command_observation")
    action = value["action"]
    if action.startswith("matrix."):
        target = value
        if action == "matrix.cancel":
            with bridge.transaction() as db:
                target = json.loads(
                    db.execute(
                        "SELECT payload FROM commands WHERE request_id=?",
                        (value["target_request_id"],),
                    ).fetchone()[0]
                )
        require(
            record.get("request_id") == target["request_id"]
            and record.get("message_hash")
            == hashlib.sha256(target["message"].encode()).hexdigest(),
            "matrix_exchange_mismatch",
        )
        require(
            record.get("state")
            in {
                "running",
                "completed",
                "failed",
                "cancelled",
                "interrupted",
                "paused",
                "persistence_failed",
            },
            "invalid_matrix_state",
        )
        if action == "matrix.cancel":
            require(record["state"] == "cancelled", "cancellation_not_observed")
        observation = {
            k: record[k]
            for k in (
                "request_id",
                "message_hash",
                "state",
                "started_at",
                "updated_at",
                "model",
                "usage",
                "reply",
                "error",
            )
            if k in record
        }
    else:
        # Existing identity/spec/generation checks remain authoritative.
        if "agent_id" in value:
            with bridge.transaction() as db:
                target = db.execute(
                    "SELECT target FROM commands WHERE request_id=?", (value["request_id"],)
                ).fetchone()[0]
            require(target is not None, "agent_target_missing")
            runtime_target(bridge, value["agent_id"], record, json.loads(target))
        else:
            bridge._ingest(value["task_id"], record)
        if action == "agents.send":
            require(
                isinstance(record.get("turns"), dict)
                and record["turns"].get(value["request_id"]) == value["message"]
                and record["generation"] > value["generation"],
                "continuation_not_observed",
            )
        else:
            require(
                record["generation"] == value["generation"]
                and record["state"] in {"cancelling", "cancelled"},
                "cancellation_not_observed",
            )
        observation = {
            k: record[k]
            for k in ("id", "generation", "state", "acceptance", "updated", "usage")
            if k in record
        }
    with bridge.transaction() as db:
        previous = db.execute(
            "SELECT observation FROM commands WHERE request_id=?", (value["request_id"],)
        ).fetchone()[0]
        # Preserve the newest fact under concurrent HTTP/status readers.
        if previous:
            from bridge import timestamp

            before = json.loads(previous)
            old_time, new_time = (
                before.get("updated_at", before.get("updated")),
                observation.get("updated_at", observation.get("updated")),
            )
            require(timestamp(new_time) >= timestamp(old_time), "stale_observation")
            if "generation" in observation and "generation" in before:
                require(observation["generation"] >= before["generation"], "stale_generation")
                if observation["generation"] == before["generation"]:
                    terminal = {
                        "completed",
                        "failed",
                        "cancelled",
                        "timed_out",
                        "interrupted",
                        "persistence_failed",
                    }
                    require(
                        not (
                            before.get("state") in terminal
                            and observation.get("state") not in terminal
                        ),
                        "state_regressed",
                    )
                    require(
                        not (
                            before.get("acceptance") == "accepted"
                            and observation.get("acceptance") == "pending"
                        ),
                        "acceptance_regressed",
                    )
        db.execute(
            "UPDATE commands SET state='observed',observation=?,last_error=NULL WHERE request_id=?",
            (canonical(observation), value["request_id"]),
        )
        if previous != canonical(observation):
            bridge._receipt(
                db,
                target_key(value),
                "command_observed",
                {"request_id": value["request_id"], "observation": observation},
            )
    return snapshot(bridge, value["request_id"])


def observe(bridge, request_id):
    with bridge.transaction() as db:
        row = db.execute(
            "SELECT payload FROM commands WHERE request_id=?", (identifier(request_id),)
        ).fetchone()
        require(row is not None, "command_unknown_locally")
        value = json.loads(row[0])
    if value["action"] == "humans.reply":
        from human_commands import observe as observe_human

        return observe_human(bridge, value)
    try:
        if value["action"].startswith("matrix."):
            record = bridge.client.rpc(
                "matrix.status", {"request_id": value.get("target_request_id", request_id)}
            )
        elif "agent_id" in value:
            record = bridge.client.rpc("agents.status", {"id": value["agent_id"]})
        else:
            state = bridge.snapshot(value["task_id"])
            record = bridge.client.rpc("agents.status", {"id": state["agent_id"]})
        return ingest(bridge, value, record)
    except BridgeError as exc:
        return unknown(bridge, value, str(exc))
