"""Backlog pagination and explicit notification review after a native handoff."""

import concurrent.futures
import contextlib
import io
import json
from pathlib import Path
import secrets
import sys
import threading
import unittest
from unittest.mock import patch

from test_station_notifications import StationFixture
import test_agent_station as station_tests
from test_agent_access import client_module
from test_control import control
from agent_access import AgentError
from station_notifications import StationNotifications


class NotificationContinuityTests(StationFixture):
    def setUp(self):
        self.tokens = {}
        super().setUp()
        self.notes = self.app.station_notifications

    def enroll(self, name):
        token = secrets.token_urlsafe(32)
        self.access.enroll({"request_id": name, "name": name, "project": "control", "token": token})
        peer = self.access.authenticate(token)
        self.tokens[peer["actor_id"]] = token
        return peer, self.access.seats(peer)["seats"][0]

    def test_real_http_mcp_and_cli_paginate_and_review_handoff(self):
        server = control.Server(0, self.app)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            leader = client_module.AgentClient(
                {"url": server.origin, "token": self.tokens[self.leader["actor_id"]]}
            )
            sender = client_module.AgentClient(
                {"url": server.origin, "token": self.tokens[self.worker["actor_id"]]}
            )
            first, second = self.submit(), self.submit("two")
            page = client_module.invoke(leader, "aieyra_leader_notifications", {"limit": 1})
            self.assertTrue(page["has_more"])
            following = leader.call("station/notifications", query={"after": page["next_cursor"]})
            self.assertEqual([r["id"] for r in following["notifications"]], [second["id"]])
            sent = client_module.invoke(
                sender, "aieyra_leader_notifications", {"direction": "sent", "status": "all"}
            )
            self.assertEqual(sent["total"], 2)
            for query in ({"limit": [1, 2]}, {"limit": 0}, {"typo": "bad"}):
                with self.subTest(query=query), self.assertRaises(client_module.ClientError):
                    leader.call("station/notifications", query=query)
            with self.assertRaises(client_module.ClientError):
                client_module.invoke(
                    leader, "aieyra_leader_notifications", {"id": first["id"], "limit": 1}
                )
            self.join(self.worker, self.worker_seat, "worker-one", "native-worker")
            with self.assertRaises(client_module.ClientError):
                sender.call(
                    "station/notification-ack",
                    {"id": first["id"], "session_id": "worker-one", "state": "handled"},
                )
            self.handoff()
            args = {
                "id": first["id"],
                "session_id": "leader-two",
                "state": "read",
                "review_previous_binding": True,
                "expected_binding_version": 3,
                "reason": "Reconciled original result",
            }
            result = client_module.invoke(leader, "aieyra_notification_ack", args)["notification"]
            self.assertEqual(result["read_receipt"]["binding_version"], 3)
            root = Path(self.tmp.name)
            station_tests.adapter.atomic(
                root / "credential.json",
                {"url": server.origin, "token": self.tokens[self.leader["actor_id"]]},
            )
            station_tests.adapter.atomic(
                root / "profile.json",
                {
                    "schema": 1,
                    "host": "codex",
                    "project": "control",
                    "root": str(root),
                    "config_file": str(root / "credential.json"),
                    "actor_id": self.leader["actor_id"],
                    "seat_id": self.leader_seat["id"],
                    "lease": False,
                },
            )

            def cli(*arguments):
                output = io.StringIO()
                with (
                    patch.object(
                        sys,
                        "argv",
                        ["agent-station.py", "--profile", str(root / "profile.json"), *arguments],
                    ),
                    contextlib.redirect_stdout(output),
                ):
                    code = station_tests.adapter.main()
                self.assertEqual(code, 0, output.getvalue())
                return json.loads(output.getvalue())

            self.assertEqual(cli("notifications", "--limit", "1")["total"], 2)
            final = cli(
                "notification-ack",
                "--session-id",
                "leader-two",
                "--id",
                first["id"],
                "--state",
                "handled",
                "--review-previous-binding",
                "--expected-binding-version",
                "3",
                "--reason",
                "Verified result",
            )
            self.assertTrue(final["notification"]["handled"])
            self.assertEqual(cli("notifications", "--status", "all")["total"], 2)
            self.assertEqual(
                sender.call("station/notifications/" + first["id"])["notification"][
                    "native_session_id"
                ],
                "native-leader",
            )
        finally:
            server.shutdown()
            server.server_close()
            worker.join(5)

    def submit(self, rid="one", peer=None):
        return self.notes.submit(peer or self.worker, {"request_id": rid, "body": rid})[
            "notification"
        ]

    def ack(self, row, state="read", session="leader-one", **review):
        return self.notes.acknowledge(
            self.leader, {"id": row["id"], "session_id": session, "state": state, **review}
        )["notification"]

    def handoff(self, native="new-native"):
        self.leave(self.leader, "leader-one")
        self.access.owner_handoff(
            {
                "request_id": "handoff",
                "actor_id": self.leader["actor_id"],
                "native_session_id": native,
                "expected_version": 2,
                "reason": "Explicit fixture takeover",
            }
        )
        self.join(self.leader, self.leader_seat, "leader-two", native)

    def test_backlog_over_fifty_has_exact_inbox_count_and_stable_tied_cursor(self):
        ids = []
        for group in range(3):
            peer, _ = self.enroll("sender-" + str(group))
            ids.extend(self.submit(str(i), peer)["id"] for i in range(20))
        with self.app.store.db() as db:
            db.execute("UPDATE station_notifications SET created=12345")
        first = self.notes.read(self.leader)
        self.assertEqual(first["total"], 60)
        self.assertEqual(len(first["notifications"]), 50)
        self.assertTrue(first["has_more"])
        inbox = self.access.inbox(self.leader, "leader-one")
        notice = next(n for n in inbox["notices"] if n["kind"] == "leader_notifications")
        self.assertEqual((notice["pending"], notice["unread"]), (60, 60))
        for row in first["notifications"]:
            self.ack(row, "handled")
        self.notes = StationNotifications(self.app)
        second = self.notes.read(self.leader, query={"after": first["next_cursor"]})
        self.assertEqual(second["total"], 10)
        self.assertFalse(second["has_more"])
        self.assertIsNone(second["next_cursor"])
        actual = [r["id"] for r in first["notifications"] + second["notifications"]]
        self.assertEqual(actual, sorted(ids))
        self.assertTrue(all(r["read_at"] is None for r in second["notifications"]))

    def test_sent_history_is_scoped_and_foreign_cursor_rejected(self):
        first = self.submit()
        other, _ = self.enroll("other")
        foreign = self.submit("other", other)
        self.ack(first, "handled")
        self.assertEqual(self.notes.read(self.worker)["notifications"], [])
        history = self.notes.read(self.worker, query={"direction": "sent", "status": "all"})
        self.assertEqual([r["id"] for r in history["notifications"]], [first["id"]])
        self.assertTrue(history["notifications"][0]["handled"])
        with self.assertRaisesRegex(AgentError, "notification_cursor_missing"):
            self.notes.read(self.worker, query={"direction": "sent", "after": foreign["id"]})

    def test_invalid_filters_and_single_id_filters_are_rejected(self):
        for query in (
            {"limit": "0"},
            {"limit": "101"},
            {"limit": True},
            {"limit": "1.5"},
            {"direction": "all"},
            {"status": "anything"},
            {"after": ""},
            {"typo": "a"},
        ):
            with self.subTest(query=query), self.assertRaises(AgentError):
                self.notes.read(self.leader, query=query)
        with self.assertRaisesRegex(AgentError, "invalid_notification_query"):
            self.notes.read(self.leader, self.submit()["id"], query={"limit": "1"})

    def test_handoff_requires_explicit_review_with_current_version_and_reason(self):
        row = self.submit()
        self.handoff()
        with self.assertRaisesRegex(AgentError, "notification_binding_changed"):
            self.ack(row, session="leader-two")
        for review in (
            {"review_previous_binding": True},
            {"review_previous_binding": True, "expected_binding_version": 2, "reason": "reviewed"},
            {"review_previous_binding": True, "expected_binding_version": 3, "reason": " "},
            {"review_previous_binding": False, "expected_binding_version": 3, "reason": "reviewed"},
        ):
            with self.subTest(review=review), self.assertRaises(AgentError):
                self.ack(row, session="leader-two", **review)
        final = self.ack(
            row,
            "handled",
            "leader-two",
            review_previous_binding=True,
            expected_binding_version=3,
            reason="Read the old notification and verified the result",
        )
        self.assertEqual(final["native_session_id"], "native-leader")
        self.assertEqual(final["binding_version"], 2)
        self.assertIsNone(final["native_receipt"])
        self.assertEqual(final["handled_receipt"]["native_session_id"], "new-native")
        self.assertEqual(final["handled_receipt"]["binding_version"], 3)
        self.assertTrue(final["handled_receipt"]["review_previous_binding"])
        self.assertEqual(self.notes.read(self.leader)["notifications"], [])
        with self.assertRaisesRegex(AgentError, "agent_session_expired"):
            self.ack(row)

    def test_binding_version_changes_even_on_same_native_require_review(self):
        row = self.submit()
        # Simulate a newer binding generation retaining the same native identifier.
        with self.app.store.db() as db:
            db.execute(
                "UPDATE agent_station_bindings SET version=3 WHERE actor_id=?",
                (self.leader["actor_id"],),
            )
        with self.assertRaisesRegex(AgentError, "notification_binding_changed"):
            self.ack(row)

    def test_current_binding_and_current_governance_required(self):
        row = self.submit()
        with patch.object(self.access, "is_leader", return_value=False):
            with self.assertRaisesRegex(AgentError, "notification_leader_required"):
                self.ack(row)
        with self.app.store.db() as db:
            db.execute(
                "UPDATE agent_station_bindings SET native_session_id='different' WHERE actor_id=?",
                (self.leader["actor_id"],),
            )
        with self.assertRaisesRegex(AgentError, "notification_session_binding_changed"):
            self.ack(row)

    def test_ack_retry_preserves_first_audit_and_unknown_never_replays(self):
        row = self.submit()
        self.notes.update([row["id"]], "unknown", "response_lost", receipt={"turn_id": "old"})
        first = self.ack(row)
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda _: self.ack(row, "handled"), range(5)))
        self.assertEqual({r["handled_at"] for r in results}, {results[0]["handled_at"]})
        self.assertTrue(all(r["read_receipt"] == first["read_receipt"] for r in results))
        final = self.notes.read(self.worker, row["id"])["notification"]
        self.assertEqual(final["state"], "unknown")
        self.assertEqual(final["native_receipt"], {"turn_id": "old"})
        self.assertEqual(final["handled_receipt"]["actor_id"], self.leader["actor_id"])
        with patch.object(self.notes.adapter, "notify") as notify:
            self.notes.tick()
            notify.assert_not_called()

    def test_legacy_migration_preserves_unknown_history_without_invented_audit(self):
        row = self.submit()
        with self.app.store.db() as db:
            db.execute("ALTER TABLE station_notifications DROP COLUMN read_receipt")
            db.execute("ALTER TABLE station_notifications DROP COLUMN handled_receipt")
            db.execute("UPDATE station_notifications SET read_at=123,state='unknown'")
        self.notes = StationNotifications(self.app)
        before = self.notes.read(self.leader, row["id"])["notification"]
        self.assertIsNone(before["read_receipt"])
        final = self.ack(row, "handled")
        self.assertEqual(final["read_at"], 123)
        self.assertIsNone(final["read_receipt"])
        self.assertIsNotNone(final["handled_receipt"])
        self.assertEqual(final["state"], "unknown")


if __name__ == "__main__":
    unittest.main()
