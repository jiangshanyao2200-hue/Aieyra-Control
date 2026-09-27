import datetime as dt
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("control_watchdog", ROOT / "service/watchdog.py")
watchdog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watchdog)


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = dt.datetime(2026, 9, 22, 5, 20, tzinfo=dt.timezone.utc)
        self.quota = self.root / "quota.json"
        self.quota.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "scope": "sub2_5x_team",
                    "observed_at": self.now.isoformat().replace("+00:00", "Z"),
                    "exhausted": False,
                    "route_verified": True,
                }
            ),
            encoding="utf-8",
        )
        self.config = {
            "enabled": True,
            "work_remaining": True,
            "quota_file": str(self.quota),
            "silence_seconds": 60,
            "cooldown_seconds": 300,
            "leader_target": "leader",
        }

    def seat(self, name, event_kind="task_complete", age=120):
        path = self.root / f"{name}.jsonl"
        timestamp = (self.now - dt.timedelta(seconds=age)).isoformat().replace("+00:00", "Z")
        path.write_text(
            json.dumps(
                {
                    "type": "event_msg",
                    "timestamp": timestamp,
                    "payload": {"type": event_kind, "turn_id": "turn-1"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return {"seat_id": name, "thread_id": name, "label": name, "rollout": str(path)}

    def test_unknown_quota_does_not_claim_zero_and_still_keeps_alive(self):
        self.quota.unlink()
        seats = [watchdog.lifecycle_state(self.seat(str(i)), current=self.now) for i in range(5)]
        result = watchdog.evaluate(
            seats, {"tasks": [{"status": "doing"}]}, self.config, {}, self.now
        )
        self.assertEqual(result["state"], "trigger_ready")
        self.assertEqual(result["reason"], "all_seats_idle_with_open_work_quota_unknown")
        self.assertEqual(result["quota"]["state"], "unknown")

    def test_only_all_explicit_idle_can_trigger(self):
        seats = [watchdog.lifecycle_state(self.seat(str(i)), current=self.now) for i in range(5)]
        result = watchdog.evaluate(
            seats, {"tasks": [{"status": "doing"}]}, self.config, {}, self.now
        )
        self.assertEqual(result["state"], "trigger_ready")

        running = [
            watchdog.lifecycle_state(self.seat(str(i), "task_started"), current=self.now)
            for i in range(5)
        ]
        result = watchdog.evaluate(
            running, {"tasks": [{"status": "doing"}]}, self.config, {}, self.now
        )
        self.assertEqual(result["state"], "observing")

    def test_verified_zero_is_locked_stop(self):
        self.quota.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "scope": "sub2_5x_team",
                    "observed_at": self.now.isoformat().replace("+00:00", "Z"),
                    "exhausted": True,
                    "route_verified": True,
                    "reason": "verified_zero",
                }
            ),
            encoding="utf-8",
        )
        seats = [watchdog.lifecycle_state(self.seat(str(i)), current=self.now) for i in range(5)]
        result = watchdog.evaluate(
            seats, {"tasks": [{"status": "doing"}]}, self.config, {}, self.now
        )
        self.assertEqual(result["state"], "quota_exhausted")

    def test_unknown_delivery_does_not_resend(self):
        seats = [watchdog.lifecycle_state(self.seat(str(i)), current=self.now) for i in range(5)]
        state = {"active_trigger": {"delivery_id": "d1", "delivery_state": "unknown"}}
        result = watchdog.evaluate(
            seats, {"tasks": [{"status": "doing"}]}, self.config, state, self.now
        )
        self.assertEqual(result["state"], "trigger_unknown")


if __name__ == "__main__":
    unittest.main()
