"""Five-seat liveness watchdog.

This process is deliberately independent from a model turn. It reads only
Codex lifecycle metadata from the configured rollout files and uses the local
Control gateway for a durable, CSRF-protected Trigger delivery. It never
reads message text, reasoning, credentials, or kills a process.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import os
from pathlib import Path
import signal
import threading
import urllib.request


SCHEMA_VERSION = 1
TERMINAL_DELIVERY = {"completed", "interrupted"}
UNKNOWN_DELIVERY = {"unknown"}


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(value: dt.datetime | None = None) -> str:
    return (value or utc_now()).isoformat().replace("+00:00", "Z")


def parse_time(value: object) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def age_seconds(value: object, current: dt.datetime | None = None) -> float | None:
    parsed = parse_time(value)
    if parsed is None:
        return None
    return max(0.0, ((current or utc_now()) - parsed).total_seconds())


def atomic_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


class InstanceLock:
    """Hold an OS file lock so two watchdogs cannot emit duplicate triggers."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                self.file.seek(0)
                if os.fstat(self.file.fileno()).st_size == 0:
                    self.file.write(b"0")
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError("watchdog_already_running") from None

    def close(self) -> None:
        self.file.close()


def lifecycle_state(
    seat: dict, previous: dict | None = None, current: dt.datetime | None = None
) -> dict:
    """Consume only event type/timestamps from one rollout file."""
    current = current or utc_now()
    result = copy.deepcopy(previous or {})
    result.update(
        {
            "seat_id": seat.get("seat_id"),
            "thread_id": seat.get("thread_id"),
            "label": seat.get("label"),
            "state": result.get("state", "unknown"),
            "last_error": None,
        }
    )
    path = Path(str(seat.get("rollout", "")))
    if not path.is_file():
        result.update(state="unknown", last_error="rollout_missing")
        return result
    try:
        size = path.stat().st_size
        offset = int(result.get("offset", 0))
        if size < offset:
            offset = 0
            result.update(state="unknown", turn_id=None)
        with path.open("rb") as source:
            source.seek(offset)
            while True:
                line = source.readline()
                if not line or not line.endswith(b"\n"):
                    break
                offset = source.tell()
                try:
                    event = json.loads(line)
                except (TypeError, ValueError):
                    result.update(state="unknown", last_error="invalid_lifecycle_line")
                    continue
                if not isinstance(event, dict):
                    continue
                timestamp = event.get("timestamp")
                event_type = event.get("type")
                payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
                kind = payload.get("type")
                if isinstance(timestamp, str):
                    result["last_activity_at"] = timestamp
                result["last_event_type"] = kind or event_type
                if event_type == "event_msg" and kind == "task_started":
                    result.update(state="running", turn_id=payload.get("turn_id"))
                elif event_type == "event_msg" and kind in {
                    "task_complete",
                    "task_completed",
                    "turn_aborted",
                }:
                    result.update(state="idle", turn_id=None)
                elif event_type in {"event_msg", "response_item"} and result.get("state") != "idle":
                    result["state"] = "running"
    except OSError as error:
        result.update(state="unknown", last_error=type(error).__name__)
        return result
    result["offset"] = offset
    result["observed_at"] = iso(current)
    activity_age = age_seconds(result.get("last_activity_at"), current)
    result["silence_seconds"] = activity_age
    if result.get("state") == "running" and (
        activity_age is None or activity_age > float(seat.get("stale_seconds", 180))
    ):
        result.update(state="unknown", last_error="lifecycle_stale")
    return result


def read_quota(config: dict, current: dt.datetime | None = None) -> dict:
    current = current or utc_now()
    path = Path(str(config.get("quota_file", "")))
    if not path.is_file():
        return {"state": "unknown", "reason": "quota_evidence_missing", "observed_at": None}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"state": "unknown", "reason": "quota_evidence_invalid", "observed_at": None}
    if not isinstance(value, dict) or value.get("scope") != "sub2_5x_team":
        observed = value.get("observed_at") if isinstance(value, dict) else None
        return {"state": "unknown", "reason": "quota_scope_unverified", "observed_at": observed}
    observed_age = age_seconds(value.get("observed_at"), current)
    if observed_age is None or observed_age > float(config.get("quota_stale_seconds", 300)):
        return {
            "state": "unknown",
            "reason": "quota_evidence_stale",
            "observed_at": value.get("observed_at"),
        }
    if value.get("exhausted") is True and value.get("route_verified") is True:
        return {
            "state": "exhausted",
            "reason": value.get("reason", "verified_zero"),
            "observed_at": value.get("observed_at"),
        }
    if value.get("exhausted") is False:
        return {
            "state": "available",
            "reason": value.get("reason", "verified_available"),
            "observed_at": value.get("observed_at"),
        }
    return {
        "state": "unknown",
        "reason": "quota_exhaustion_not_proven",
        "observed_at": value.get("observed_at"),
    }


def remaining_work(snapshot: dict, config: dict) -> bool:
    if config.get("work_remaining") is True:
        return True
    for task in snapshot.get("tasks", []) if isinstance(snapshot, dict) else []:
        if isinstance(task, dict) and task.get("status") in {"planned", "doing", "blocked"}:
            return True
    return False


def evaluate(
    seats: list[dict], snapshot: dict, config: dict, state: dict, current: dt.datetime | None = None
) -> dict:
    current = current or utc_now()
    quota = read_quota(config, current)
    result = {
        "schema_version": SCHEMA_VERSION,
        "observed_at": iso(current),
        "enabled": bool(config.get("enabled", False)),
        "quota": quota,
        "remaining_work": remaining_work(snapshot, config),
        "leader_target": config.get("leader_target"),
        "seats": seats,
        "active_trigger": state.get("active_trigger"),
        "last_error": None,
    }
    if not result["enabled"]:
        result.update(state="disabled", reason="watchdog_disabled")
        return result
    if quota["state"] == "exhausted":
        result.update(state="quota_exhausted", reason="verified_sub2_5x_zero")
        return result
    if not result["remaining_work"]:
        result.update(state="idle_no_work", reason="no_open_task")
        return result
    if result["active_trigger"]:
        delivery_state = result["active_trigger"].get("delivery_state")
        if delivery_state in UNKNOWN_DELIVERY:
            result.update(state="trigger_unknown", reason="trigger_delivery_unknown")
        elif delivery_state in TERMINAL_DELIVERY:
            result.update(state="cooldown", reason="trigger_completed")
        else:
            result.update(state="trigger_pending", reason="awaiting_leader_receipt")
        return result
    if not seats or any(seat.get("state") != "idle" for seat in seats):
        result.update(state="observing", reason="not_all_seats_explicitly_idle")
        return result
    min_silence = min((seat.get("silence_seconds") or 0) for seat in seats)
    if min_silence < float(config.get("silence_seconds", 60)):
        result.update(state="observing", reason="silence_threshold_not_reached")
        return result
    last_trigger_at = age_seconds(state.get("last_trigger_at"), current)
    if last_trigger_at is not None and last_trigger_at < float(config.get("cooldown_seconds", 300)):
        result.update(state="cooldown", reason="trigger_cooldown")
        return result
    reason = "all_seats_idle_with_open_work"
    if quota["state"] == "unknown":
        reason += "_quota_unknown"
    result.update(state="trigger_ready", reason=reason)
    return result


def request_json(
    url: str, method: str = "GET", data: dict | None = None, headers: dict | None = None
) -> dict:
    body = None if data is None else json.dumps(data, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    with urllib.request.urlopen(request, timeout=20) as response:
        value = json.loads(response.read().decode("utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("invalid_json_response")
    return value


def poll_local(config: dict) -> tuple[dict, dict]:
    base = str(config.get("base_url", "http://127.0.0.1:17910")).rstrip("/")
    session = request_json(base + "/api/session")
    snapshot = request_json(base + "/api/snapshot")
    return snapshot, {"base_url": base, "csrf": session.get("csrf")}


def deliver_trigger(config: dict, http: dict, status: dict, counter: int) -> dict:
    request_id = f"five-seat-watchdog-trigger-{counter:06d}"
    body = (
        "[Five-seat watchdog Trigger]\n"
        "五个既有工位均已明确 idle，仍有未完成任务。请领导读取 docs/five-seat-plan.md，"
        "核对每席实际回执后分派下一批可验收工作；不要把沉默、入队或未知结果当作完成。"
        f"\nwatchdog_state={status.get('state')} observed_at={status.get('observed_at')}"
    )
    result = request_json(
        str(http["base_url"]) + "/api/chat",
        method="POST",
        data={"request_id": request_id, "target": config["leader_target"], "body": body},
        headers={
            "Content-Type": "application/json",
            "Origin": http["base_url"],
            "X-Control-CSRF": http["csrf"],
        },
    )
    delivery = result.get("delivery")
    if not isinstance(delivery, dict) or not isinstance(delivery.get("id"), str):
        raise RuntimeError("trigger_delivery_missing")
    return {
        "request_id": request_id,
        "delivery_id": delivery["id"],
        "delivery_state": delivery.get("state", "pending"),
        "created_at": iso(),
    }


def update_delivery(snapshot: dict, active: dict | None) -> dict | None:
    if not active:
        return None
    delivery_id = active.get("delivery_id")
    for item in snapshot.get("deliveries", []) if isinstance(snapshot, dict) else []:
        if isinstance(item, dict) and item.get("id") == delivery_id:
            active = copy.deepcopy(active)
            active["delivery_state"] = item.get("state", "unknown")
            active["last_checked_at"] = iso()
            return active
    return active


def run_once(config: dict, state: dict) -> dict:
    current = utc_now()
    try:
        snapshot, http = poll_local(config)
        previous = {
            item.get("seat_id"): item for item in state.get("seats", []) if isinstance(item, dict)
        }
        seats = [
            lifecycle_state(seat, previous.get(seat.get("seat_id")), current)
            for seat in config.get("seats", [])
        ]
        state = {**state, "seats": seats}
        status = evaluate(seats, snapshot, config, state, current)
        if state.get("active_trigger"):
            status["active_trigger"] = update_delivery(snapshot, state["active_trigger"])
            state["active_trigger"] = status["active_trigger"]
        if (
            status["state"] == "cooldown"
            and state.get("active_trigger", {}).get("delivery_state") in TERMINAL_DELIVERY
        ):
            state["last_trigger_at"] = state["active_trigger"].get("created_at")
            state["active_trigger"] = None
        if status["state"] == "trigger_ready" and not state.get("active_trigger"):
            counter = int(state.get("trigger_count", 0)) + 1
            active = deliver_trigger(config, http, status, counter)
            state.update(
                active_trigger=active, trigger_count=counter, last_trigger_at=active["created_at"]
            )
            status["state"] = "trigger_pending"
            status["reason"] = "trigger_queued"
            status["active_trigger"] = active
        status["trigger_count"] = state.get("trigger_count", 0)
        status["last_trigger_at"] = state.get("last_trigger_at")
        status["active_trigger"] = state.get("active_trigger")
        state["last_error"] = None
        state["updated_at"] = iso(current)
        return {"state": state, "status": status}
    except Exception as error:
        state = {**state, "updated_at": iso(current), "last_error": type(error).__name__}
        return {
            "state": state,
            "status": {
                "schema_version": SCHEMA_VERSION,
                "observed_at": iso(current),
                "state": "unknown",
                "reason": type(error).__name__,
                "seats": state.get("seats", []),
            },
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Aieyra Control five-seat lifecycle watchdog")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    state_path = Path(config.get("state_path", args.config.parent / ".runtime/watchdog/state.json"))
    lock = InstanceLock(Path(config.get("lock_path", str(state_path) + ".lock")))
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(signum, lambda *_: stop.set())
        except (ValueError, OSError):
            pass
    try:
        try:
            state = (
                json.loads(state_path.read_text(encoding="utf-8"))
                if state_path.is_file()
                else {"schema_version": SCHEMA_VERSION, "trigger_count": 0, "active_trigger": None}
            )
        except (OSError, ValueError):
            state = {
                "schema_version": SCHEMA_VERSION,
                "trigger_count": 0,
                "active_trigger": None,
                "last_error": "state_invalid",
            }
        while True:
            result = run_once(config, state)
            state = result["state"]
            atomic_write(state_path, {**state, "status": result["status"]})
            print(json.dumps(result["status"], ensure_ascii=False), flush=True)
            if args.once or stop.wait(max(5, int(config.get("poll_seconds", 30)))):
                break
    finally:
        lock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
