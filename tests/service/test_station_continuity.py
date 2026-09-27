"""Permanent membership and native context; no model or remote network calls."""

import secrets
import tempfile
import unittest
from pathlib import Path
from test_control import control
from agent_access import AgentError


class StationContinuityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = control.Application(
            {
                "coordination_mode": "local",
                "local_projects": [{"id": "control", "name": "Control", "root": self.tmp.name}],
            },
            Path(self.tmp.name),
        )
        self.access = self.app.agent_access
        self.leader, self.leader_seat = self.enroll("leader")
        self.worker, self.worker_seat = self.enroll("worker")
        self.app.hub.client.call(
            "owner",
            "/v1/governance-grant",
            {
                "request_id": "grant",
                "actor_id": self.leader["actor_id"],
                "role": "leader",
                "projects": ["control"],
                "expires_at": 0,
                "version": 0,
                "reason": "Fixture user appointment",
            },
        )
        self.join(self.leader, self.leader_seat, "leader-one", "native-leader")

    def enroll(self, name):
        token = secrets.token_urlsafe(32)
        self.access.enroll({"request_id": name, "name": name, "project": "control", "token": token})
        peer = self.access.authenticate(token)
        return peer, self.access.seats(peer)["seats"][0]

    def join(self, peer, seat, sid, native=None):
        return self.access.mutate(
            peer,
            "connect",
            {
                "request_id": "join-" + sid,
                "session_id": sid,
                "seat_id": seat["id"],
                "seat_epoch": seat["epoch"],
                **({"native_session_id": native} if native else {}),
            },
        )

    def leave(self, peer, sid):
        self.access.mutate(peer, "disconnect", {"request_id": "leave-" + sid, "session_id": sid})

    def action(self, action, **body):
        return self.access.station_action(
            self.leader,
            action,
            {
                "request_id": action,
                "session_id": "leader-one",
                "reason": "Explicit fixture leadership decision",
                **body,
            },
        )

    def test_two_jobs_reconnect_expiry_restart_preserve_native_thread_and_membership(self):
        self.join(self.worker, self.worker_seat, "transport-one", "native-worker")
        self.app.tick()
        for job in ("job-a", "job-b"):
            self.app.submit({"request_id": job, "target": self.worker["actor_id"], "body": job})
            self.app.dispatch_one()
            delivery = self.access.inbox(self.worker, "transport-one")["deliveries"][0]
            for state in ("received", "running", "completed"):
                self.access.mutate(
                    self.worker,
                    "receipt",
                    {
                        "request_id": job + "-" + state,
                        "session_id": "transport-one",
                        "delivery_id": job,
                        "event_id": job + "-" + state,
                        "body_sha256": delivery["body_sha256"],
                        "state": state,
                        **({"reply": "Fixture complete"} if state == "completed" else {}),
                    },
                )
            self.assertEqual(
                self.access.session(self.worker, "transport-one")["native_session_id"],
                "native-worker",
            )
        with self.app.store.db() as db:
            db.execute("UPDATE agent_sessions SET lease_until=0 WHERE id='transport-one'")
        self.assertEqual(
            self.access.session(self.worker, "transport-one", False)["state"], "expired"
        )
        seat = next(s for s in self.app.registry()["seats"] if s["id"] == self.worker_seat["id"])
        self.assertEqual(seat["membership_state"], "active")
        resumed = self.join(self.worker, self.worker_seat, "transport-two")["session"]
        self.assertEqual(resumed["native_session_id"], "native-worker")
        self.leave(self.worker, "transport-two")
        reopened = control.Application({"coordination_mode": "local"}, Path(self.tmp.name))
        binding = next(s for s in reopened.registry()["seats"] if s["id"] == seat["id"])[
            "station_binding"
        ]
        self.assertEqual(binding["native_session_id"], "native-worker")
        self.assertEqual(reopened.store.delivery("job-a")["state"], "completed")
        self.assertEqual(reopened.store.delivery("job-b")["state"], "completed")
        with self.assertRaisesRegex(AgentError, "native_session_change_requires_handoff"):
            self.join(self.worker, self.worker_seat, "transport-three", "different-native")

    def test_handoff_requires_leader_scope_version_and_disconnected_target(self):
        self.join(self.worker, self.worker_seat, "worker-one", "native-worker")
        binding = self.access.seats(self.worker)["seats"][0]["station_binding"]
        payload = {
            "actor_id": self.worker["actor_id"],
            "native_session_id": "native-replacement",
            "expected_version": binding["version"],
        }
        with self.assertRaisesRegex(AgentError, "disconnect_before_native_handoff"):
            self.action("handoff", **payload)
        with self.assertRaisesRegex(AgentError, "station_leader_required"):
            self.access.station_action(
                self.worker,
                "handoff",
                {"request_id": "fake", "session_id": "worker-one", "reason": "fake", **payload},
            )
        self.leave(self.worker, "worker-one")
        changed = self.action("handoff", **payload)
        self.assertEqual(changed["station_binding"]["native_session_id"], "native-replacement")
        self.assertTrue(self.action("handoff", **payload)["replayed"])
        joined = self.join(self.worker, self.worker_seat, "worker-two")["session"]
        self.assertEqual(joined["native_session_id"], "native-replacement")
        self.assertEqual(
            self.access.session(self.worker, "worker-one", False)["native_session_id"],
            "native-worker",
        )

    def test_runtime_snapshot_separates_released_lease_from_last_report(self):
        self.join(self.worker, self.worker_seat, "worker-one", "native-worker")
        self.access.mutate(
            self.worker,
            "heartbeat",
            {"request_id": "running", "session_id": "worker-one", "runtime_state": "running"},
        )
        live = self.access.runtimes()[self.worker["actor_id"]]["runtime"]
        self.assertGreater(live["lease_until"], 0)
        self.leave(self.worker, "worker-one")
        final = self.access.runtimes()[self.worker["actor_id"]]["runtime"]
        self.assertEqual(final["state"], "unknown")
        self.assertEqual(final["last_reported_state"], "running")
        self.assertEqual(final["lease_until"], 0)
        self.assertEqual(final["session_state"], "disconnected")
        self.assertFalse(final["bound"])

    def test_membership_removal_and_leader_enrollment_preserve_history(self):
        self.join(self.worker, self.worker_seat, "worker-one", "native-worker")
        self.action("retire", credential_id=self.worker["id"])
        self.assertEqual(self.access.session(self.worker, "worker-one", False)["state"], "revoked")
        seat = next(s for s in self.app.registry()["seats"] if s["id"] == self.worker_seat["id"])
        self.assertEqual(seat["membership_state"], "removed")
        self.assertNotIn(self.worker["actor_id"], self.access.runtimes())
        self.assertTrue(self.action("retire", credential_id=self.worker["id"])["replayed"])
        enrolled = self.action(
            "enroll",
            name="New permanent worker",
            project="control",
            token=secrets.token_urlsafe(32),
        )
        seat = next(s for s in self.app.registry()["seats"] if s["id"] == enrolled["seat_id"])
        self.assertEqual(seat["membership_state"], "active")
        with self.app.store.db() as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM agent_station_audit").fetchone()[0], 2
            )

    def test_local_owner_explicitly_recovers_closed_leader_with_version_and_audit(self):
        binding = self.access.seats(self.leader)["seats"][0]["station_binding"]
        body = {
            "request_id": "owner-recovery",
            "actor_id": self.leader["actor_id"],
            "native_session_id": "new-leader-native",
            "expected_version": binding["version"],
            "reason": "User explicitly requested takeover of closed session",
        }
        with self.assertRaisesRegex(AgentError, "disconnect_before_native_handoff"):
            self.access.owner_handoff(body)
        self.leave(self.leader, "leader-one")
        self.access.owner_handoff(body)
        self.assertTrue(self.access.owner_handoff(body)["replayed"])
        self.assertEqual(
            self.join(self.leader, self.leader_seat, "leader-two")["session"]["native_session_id"],
            "new-leader-native",
        )
        with self.app.store.db() as db:
            self.assertEqual(
                db.execute("SELECT action FROM agent_station_audit").fetchone()[0], "owner_handoff"
            )


if __name__ == "__main__":
    unittest.main()
