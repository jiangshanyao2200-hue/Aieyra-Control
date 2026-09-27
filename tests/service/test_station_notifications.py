"""Offline leadership notifications: real local identities/HTTP, fake native host only."""

import concurrent.futures
import json
import secrets
import threading
import unittest
from unittest.mock import patch

import test_station_continuity as continuity
from test_control import control
from test_agent_access import client_module
from agent_access import AgentError
from native_wakeup import NativeWakeup, WakeError
from station_notifications import StationNotifications


class StationFixture(unittest.TestCase):
    enroll = continuity.StationContinuityTests.enroll
    join = continuity.StationContinuityTests.join
    leave = continuity.StationContinuityTests.leave

    def setUp(self):
        continuity.StationContinuityTests.setUp(self)


class NotificationsTests(StationFixture):
    def setUp(self):
        super().setUp()
        self.notifications = self.app.station_notifications
        self.calls = []
        self.notifications.config.update(enabled=True, actors=[self.leader["actor_id"]])
        self.notifications.resources = lambda: True
        self.notifications.adapter = self

    def notify(self, target, prompt, message_id, before_send):
        before_send()
        self.calls.append((dict(target), prompt, message_id))
        return {
            "thread_id": target["native_session_id"],
            "turn_id": "native-turn-1",
            "status": "inProgress",
        }

    def submit_notice(self, rid="request-1", body="Please verify explicit handoff"):
        return self.notifications.submit(self.worker, {"request_id": rid, "body": body})[
            "notification"
        ]

    def test_offline_leader_message_wakes_fixed_native_without_connect_or_handoff(self):
        self.leave(self.leader, "leader-one")
        row = self.submit_notice()
        self.notifications.tick()
        final = self.notifications.read(self.worker, row["id"])["notification"]
        self.assertEqual(final["state"], "notified")
        self.assertEqual(final["native_receipt"]["turn_id"], "native-turn-1")
        self.assertEqual(self.calls[0][0]["native_session_id"], "native-leader")
        self.assertIsNone(final["read_at"])
        self.assertFalse(final["handled"])
        self.assertEqual(
            self.access.session(self.leader, "leader-one", False)["state"], "disconnected"
        )
        self.assertNotIn("Please verify explicit handoff", self.calls[0][1])
        self.assertIn("非用户新增权限", self.calls[0][1])
        self.assertEqual(
            self.access.seats(self.leader)["seats"][0]["station_binding"]["version"], 2
        )
        self.notifications.tick()
        self.assertEqual(len(self.calls), 1)

    def test_concurrency_and_lost_response_use_one_message_and_wakeup(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            rows = list(pool.map(lambda _: self.submit_notice(), range(10)))
        self.assertEqual(len({r["id"] for r in rows}), 1)
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(lambda _: self.notifications.tick(), range(5)))
        self.assertEqual(len(self.calls), 1)
        again = self.submit_notice()
        self.assertEqual(again["state"], "notified")
        messages = self.access.center(self.worker, "history")["messages"]
        self.assertEqual(sum(again["id"] in m["body"] for m in messages), 1)
        with self.assertRaisesRegex(AgentError, "request_id_conflict"):
            self.submit_notice(body="Changed body")

    def test_native_response_loss_and_restart_never_replay(self):
        row = self.submit_notice()

        def lost(target, prompt, nid, before):
            before()
            raise WakeError("native_timeout")

        with patch.object(self, "notify", side_effect=lost):
            self.notifications.tick()
        self.assertEqual(
            self.notifications.read(self.worker, row["id"])["notification"]["state"], "unknown"
        )
        restored = StationNotifications(self.app, adapter=self, resource_gate=lambda: True)
        restored.tick()
        self.assertEqual(self.calls, [])
        with self.app.store.db() as db:
            db.execute("UPDATE station_notifications SET state='submitting'")
        restored = StationNotifications(self.app, adapter=self, resource_gate=lambda: True)
        restored.tick()
        self.assertEqual(
            restored.read(self.worker, row["id"])["notification"]["error"],
            "interrupted_native_submission",
        )
        self.assertEqual(self.calls, [])

    def test_disconnected_sender_can_request_but_cannot_choose_another_project_or_self(self):
        other, _ = self.enroll("unrelated")
        with self.assertRaisesRegex(AgentError, "notification_missing"):
            self.notifications.read(other, self.submit_notice()["id"])
        with self.assertRaisesRegex(AgentError, "leader_project_denied"):
            self.notifications.submit(
                self.worker,
                {"request_id": "bad", "body": "test", "leader_actor_id": other["actor_id"]},
            )
        with self.assertRaisesRegex(AgentError, "self_notification_denied"):
            self.notifications.submit(self.leader, {"request_id": "self", "body": "test"})

    def test_binding_handoff_fences_queued_wake(self):
        row = self.submit_notice()
        self.leave(self.leader, "leader-one")
        self.access.owner_handoff(
            {
                "request_id": "replace-leader",
                "actor_id": self.leader["actor_id"],
                "native_session_id": "new-native",
                "expected_version": 2,
                "reason": "Explicit fixture takeover",
            }
        )
        self.notifications.tick()
        self.assertEqual(
            self.notifications.read(self.worker, row["id"])["notification"]["state"], "superseded"
        )
        self.assertFalse(self.calls)

    def test_binding_change_at_send_boundary_blocks_native_input(self):
        row = self.submit_notice()

        def changed(target, prompt, nid, before):
            with self.app.store.db() as db:
                db.execute(
                    "UPDATE agent_station_bindings SET version=version+1 WHERE actor_id=?",
                    (self.leader["actor_id"],),
                )
            before()

        with patch.object(self, "notify", side_effect=changed):
            self.notifications.tick()
        self.assertEqual(
            self.notifications.read(self.worker, row["id"])["notification"]["state"],
            "needs_attention",
        )
        self.assertFalse(self.calls)

    def test_resource_gate_and_adapter_offline_retain_messages(self):
        row = self.submit_notice()
        self.notifications.resources = lambda: False
        self.notifications.tick()
        final = self.notifications.read(self.worker, row["id"])["notification"]
        self.assertEqual(final["state"], "waiting_resource")
        self.assertIsNotNone(final["message_id"])
        self.assertFalse(self.calls)
        with self.app.store.db() as db:
            db.execute("UPDATE station_notifications SET next_attempt=0")
        self.notifications.config["enabled"] = False
        self.notifications.tick()
        self.assertEqual(
            self.notifications.read(self.worker, row["id"])["notification"]["state"],
            "waiting_adapter",
        )
        self.assertFalse(self.calls)

    def test_batch_cooldown_and_hourly_rate_limit(self):
        first = self.submit_notice()
        self.submit_notice("request-2")
        self.notifications.tick()
        self.assertEqual(len(self.calls), 1)
        self.assertIn("有2条", self.calls[0][1])
        third = self.submit_notice("request-3")
        self.notifications.tick()
        self.assertEqual(
            self.notifications.read(self.worker, third["id"])["notification"]["state"], "cooldown"
        )
        self.assertEqual(
            self.notifications.read(self.worker, first["id"])["notification"]["state"], "notified"
        )
        for i in range(4, 21):
            self.submit_notice("request-" + str(i))
        with self.assertRaisesRegex(AgentError, "notification_rate_limit"):
            self.submit_notice("too-many")

    def test_read_and_handled_are_explicit_and_no_wakeup_after_read(self):
        row = self.submit_notice()
        self.assertIsNone(self.notifications.read(self.leader)["notifications"][0]["read_at"])
        self.notifications.acknowledge(
            self.leader, {"id": row["id"], "session_id": "leader-one", "state": "read"}
        )
        self.notifications.tick()
        self.assertFalse(self.calls)
        self.assertFalse(self.notifications.read(self.worker, row["id"])["notification"]["handled"])
        self.notifications.acknowledge(
            self.leader, {"id": row["id"], "session_id": "leader-one", "state": "handled"}
        )
        self.assertTrue(self.notifications.read(self.worker, row["id"])["notification"]["handled"])
        self.assertEqual(self.notifications.read(self.leader)["notifications"], [])

    def test_real_http_submit_query_ack_and_browser_guard(self):
        server = control.Server(0, self.app)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            token = secrets.token_urlsafe(32)
            self.access.enroll(
                {"request_id": "http", "name": "HTTP station", "project": "control", "token": token}
            )
            client = client_module.AgentClient({"url": server.origin, "token": token})
            result = client.call(
                "station/notify-leader", {"request_id": "http", "body": "new worker request"}
            )
            nid = result["notification"]["id"]
            self.assertEqual(
                client.call("station/notifications/" + nid)["notification"]["body"],
                "new worker request",
            )
            self.assertEqual(client.call("station/notifications")["notifications"], [])
            self.assertTrue(
                client.call(
                    "station/notify-leader", {"request_id": "http", "body": "new worker request"}
                )["replayed"]
            )
            from urllib.request import Request, urlopen
            from urllib.error import HTTPError

            request = Request(
                server.origin + "/api/agent/v1/station/notify-leader",
                data=json.dumps({"request_id": "browser", "body": "bad"}).encode(),
                headers={
                    "Origin": server.origin,
                    "Authorization": "Bearer " + token,
                    "Content-Type": "application/json",
                },
            )
            with self.assertRaises(HTTPError) as caught:
                urlopen(request)
            self.assertEqual(caught.exception.code, 403)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(5)


class NativeAdapterTests(StationFixture):
    def setUp(self):
        super().setUp()
        self.events = []
        self.status = "idle"
        self.last = "completed"
        self.adapter = NativeWakeup(
            {"enabled": True, "executable": "fixture"}, connection=lambda _: self
        )

    def call(self, method, params):
        self.events.append((method, params))
        if method in ("thread/read", "thread/resume"):
            return {"thread": {"id": "native-leader", "status": {"type": self.status}}}
        if method == "thread/turns/list":
            return {"data": [{"status": self.last}]}
        return {"turn": {"id": "turn-1", "status": "inProgress"}}

    def close(self):
        self.events.append(("close", {}))

    def run_native(self):
        return self.adapter.notify(
            {"native_session_id": "native-leader"},
            "notification",
            "notice-1",
            lambda: self.events.append(("before_send", {})),
        )

    def test_idle_resume_turn_without_permission_model_or_cwd_overrides(self):
        self.assertEqual(self.run_native()["turn_id"], "turn-1")
        methods = [x[0] for x in self.events]
        self.assertEqual(
            methods,
            [
                "thread/read",
                "thread/turns/list",
                "thread/resume",
                "before_send",
                "turn/start",
                "close",
            ],
        )
        params = self.events[-2][1]
        self.assertEqual(set(params), {"threadId", "clientUserMessageId", "input"})
        self.assertEqual(self.events[2][1], {"threadId": "native-leader", "excludeTurns": True})

    def test_active_thread_uses_native_start_steer_without_resuming(self):
        self.status = "active"
        self.run_native()
        self.assertEqual(
            [x[0] for x in self.events], ["thread/read", "before_send", "turn/start", "close"]
        )

    def test_failed_or_interrupted_leader_needs_explicit_recovery(self):
        for last in ("failed", "interrupted", "inProgress"):
            self.events = []
            self.last = last
            with self.assertRaisesRegex(WakeError, "native_handoff_or_user_resume_required"):
                self.run_native()
            self.assertNotIn("turn/start", [x[0] for x in self.events])
