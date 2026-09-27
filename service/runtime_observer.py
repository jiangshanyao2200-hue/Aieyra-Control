"""Read only native runtime evidence and match it to existing deliveries."""

import datetime as dt
import hashlib
import json
import time
from pathlib import Path
from service_common import MARKER, delivery_message


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
            state = self.states.setdefault(
                actor,
                {"state": "unknown", "offset": 0, "active": [], "thread_id": binding["thread_id"]},
            )
            if state["thread_id"] != binding["thread_id"]:
                self.abandon_turn(actor, state)
                state = self.states[actor] = {
                    "state": "unknown",
                    "offset": 0,
                    "active": [],
                    "thread_id": binding["thread_id"],
                }
            path = Path(binding.get("rollout", ""))
            if not path.is_file() and time.monotonic() >= state.get("next_search", 0):
                matches = (
                    list(self.sessions_root.glob("*/*/*/*" + binding["thread_id"] + ".jsonl"))
                    if self.sessions_root.is_dir()
                    else []
                )
                if len(matches) == 1:
                    binding["rollout"] = str(matches[0])
                    path = matches[0]
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
                                        state.update(
                                            start_event_id="codex-event:"
                                            + hashlib.sha256(line).hexdigest(),
                                            started_at=event.get("timestamp"),
                                        )
                                state.update(
                                    state="running",
                                    observed_at=event.get("timestamp"),
                                    turn_id=turn_id,
                                )
                            elif kind in ("task_complete", "task_completed", "turn_aborted"):
                                if (
                                    not state.get("turn_id")
                                    or payload.get("turn_id") != state["turn_id"]
                                ):
                                    continue
                                state.update(state="idle", observed_at=event.get("timestamp"))
                                for delivery in state["active"]:
                                    self.receipts[(actor, delivery)]["state"] = (
                                        "completed" if kind != "turn_aborted" else "interrupted"
                                    )
                                state.update(
                                    active=[], turn_id=None, start_event_id=None, started_at=None
                                )
                        if (
                            event.get("type") == "response_item"
                            and kind == "message"
                            and payload.get("role") == "user"
                        ):
                            for part in payload.get("content", []):
                                text = part.get("text", "") if isinstance(part, dict) else ""
                                if not isinstance(text, str):
                                    continue
                                for delivery in MARKER.findall(text):
                                    body_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
                                    event_id = "codex-event:" + hashlib.sha256(line).hexdigest()
                                    if self.delivery_lookup:
                                        original = self.delivery_lookup(delivery)
                                        if (
                                            not original
                                            or original["target"] != actor
                                            or original.get("target_thread_id")
                                            != binding["thread_id"]
                                            or body_hash
                                            != hashlib.sha256(
                                                delivery_message(original).encode("utf-8")
                                            ).hexdigest()
                                            or original.get("native_event_id")
                                            not in (None, event_id)
                                            or original.get("turn_id")
                                            not in (None, state.get("turn_id"))
                                        ):
                                            continue
                                    # A duplicate marker never adopts a later turn or erases
                                    # the first receipt. Forged body hashes are not receipts.
                                    previous = self.receipts.get((actor, delivery))
                                    if previous and previous["message_hash"] == body_hash:
                                        continue
                                    self.receipts[(actor, delivery)] = {
                                        "state": "received",
                                        "message_hash": body_hash,
                                        "turn_id": state.get("turn_id"),
                                        "thread_id": binding["thread_id"],
                                        "native_event_id": event_id,
                                        "native_received_at": event.get("timestamp"),
                                        "native_start_event_id": state.get("start_event_id"),
                                        "native_started_at": state.get("started_at"),
                                    }
                                    if state.get("turn_id") and delivery not in state["active"]:
                                        state["active"].append(delivery)
                        if (
                            event.get("type") == "response_item"
                            and kind == "message"
                            and payload.get("role") == "assistant"
                            and payload.get("phase") == "final_answer"
                            and state["active"]
                        ):
                            if payload.get("turn_id", state["turn_id"]) != state["turn_id"]:
                                continue
                            answer = "\n".join(
                                part["text"]
                                for part in payload.get("content", [])
                                if isinstance(part, dict)
                                and part.get("type") == "output_text"
                                and isinstance(part.get("text"), str)
                            )
                            if answer:
                                for delivery in state["active"]:
                                    self.receipts[(actor, delivery)].update(
                                        reply=answer[:16000],
                                        reply_at=event.get("timestamp"),
                                        reply_truncated=len(answer) > 16000,
                                    )
            except OSError:
                state["state"] = "unknown"

    def snapshot(self, actor):
        state = self.states.get(actor, {})
        if not state:
            return {"state": "unknown", "bound": actor in self.bindings}
        result = {
            k: state.get(k) for k in ("state", "observed_at", "thread_id", "last_activity_at")
        }
        result["evidence"] = "recorded_lifecycle"
        result["bound"] = actor in self.bindings
        result["workstation_id"] = state.get("thread_id")
        result["last_known_state"] = state["state"]
        try:
            age = (
                dt.datetime.now(dt.timezone.utc)
                - dt.datetime.fromisoformat(state["last_activity_at"].replace("Z", "+00:00"))
            ).total_seconds()
        except (ValueError, TypeError, KeyError):
            age = None
        result["stale"] = age is None or age > 180
        if result["stale"]:
            result["state"] = "unknown"
        return result
