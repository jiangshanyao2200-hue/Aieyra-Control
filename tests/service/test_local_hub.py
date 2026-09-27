import secrets
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from test_control import control
from project_memory import SECTIONS


class LocalOfficeTests(unittest.TestCase):
    def test_offline_enroll_chat_memory_restart(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("socket.socket.connect", side_effect=AssertionError("Network forbidden")),
        ):
            root = Path(tmp)
            config = {
                "coordination_mode": "local",
                "os_runtime_registration": False,
                "resources": [],
                "local_projects": [{"id": "control", "name": "Control", "root": tmp}],
            }
            app = control.Application(config, root)
            token = secrets.token_urlsafe(32)
            app.agent_access.enroll(
                {
                    "request_id": "enroll",
                    "name": "Local leader",
                    "project": "control",
                    "token": token,
                }
            )
            peer = app.agent_access.authenticate(token)
            seat = app.agent_access.seats(peer)["seats"][0]
            app.agent_access.mutate(
                peer,
                "connect",
                {
                    "request_id": "join",
                    "session_id": "one",
                    "seat_id": seat["id"],
                    "seat_epoch": seat["epoch"],
                },
            )
            app.tick()
            self.assertEqual(app.snapshot()["connection"]["state"], "online")
            app.submit({"request_id": "say", "target": peer["actor_id"], "body": "Local message"})
            app.dispatch_one()
            self.assertEqual(
                app.agent_access.inbox(peer, "one")["deliveries"][0]["body"], "Local message"
            )
            self.assertEqual(app.store.delivery("say")["state"], "queued")
            app.agent_access.save_memory(
                peer,
                {
                    "request_id": "checkpoint",
                    "project": "control",
                    "session_id": "one",
                    "version": 0,
                    "summary": "Offline checkpoint",
                    "sections": {key: "Offline " + key for key in SECTIONS},
                },
            )
            app2 = control.Application(config, root)
            self.assertEqual(app2.agent_access.authenticate(token)["actor_id"], peer["actor_id"])
            app2.tick()
            self.assertEqual(
                app2.snapshot()["messages"][0]["body"].split("\n")[-1], "Local message"
            )
            self.assertTrue(app2.hub.is_local)
            self.assertEqual(app2.project_memory.read("control")["memory"]["version"], 1)

    def test_enrolled_project_memory_persists_without_config_whitelist(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = control.Application({"coordination_mode": "local"}, Path(tmp))
            app.hub.client.call(
                "owner",
                "/v1/project-register",
                {
                    "request_id": "new-project",
                    "id": "os",
                    "name": "OS product",
                    "root": "/registered/os",
                    "source": "",
                    "version": 0,
                },
            )
            request = {
                "request_id": "new-seat",
                "name": "OS",
                "project": "os",
                "token": secrets.token_urlsafe(32),
            }
            result = app.agent_access.enroll(request)
            self.assertEqual(app.project_memory.read("os")["memory"]["version"], 0)
            self.assertEqual(
                result["project_memory"]["project"],
                {"id": "os", "name": "OS product", "root": "/registered/os"},
            )
            self.assertEqual(result["project_memory"]["missing_sections"], list(SECTIONS))
            self.assertTrue(app.agent_access.enroll(request)["project_memory"]["ready"])
            reopened = control.Application({"coordination_mode": "local"}, Path(tmp))
            self.assertEqual(reopened.project_memory.project("os")["id"], "os")

    def test_registry_metadata_sync_replay_and_restart_never_rewrite_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = control.Application({"coordination_mode": "local"}, root)
            first = {
                "request_id": "p-first",
                "id": "demo",
                "name": "First",
                "root": "/first",
                "source": "",
                "version": 0,
            }
            app.management("project-register", first)
            saved = app.project_memory.save(
                {
                    "request_id": "save",
                    "project": "demo",
                    "version": 0,
                    "summary": "Keep this checkpoint",
                    "sections": {k: "Keep " + k for k in SECTIONS},
                },
                "local-owner",
            )["memory"]
            token = secrets.token_urlsafe(32)
            app.agent_access.enroll(
                {"request_id": "e", "name": "Worker", "project": "demo", "token": token}
            )
            peer = app.agent_access.authenticate(token)
            app.hub.client.call(
                "owner",
                "/v1/governance-grant",
                {
                    "request_id": "grant",
                    "actor_id": peer["actor_id"],
                    "role": "leader",
                    "projects": ["demo"],
                    "version": 0,
                    "expires_at": 0,
                    "reason": "Fixture",
                },
            )
            second = {
                **first,
                "request_id": "p-second",
                "version": 1,
                "name": "Latest",
                "root": "/latest",
            }
            app.agent_access.center(peer, "project-register", body=second)
            app.management("project-register", first)
            self.assertEqual(app.project_memory.project("demo")["name"], "Latest")
            self.assertEqual(app.project_memory.read("demo")["memory"], saved)
            with app.store.db() as db:
                db.execute("UPDATE memory_projects SET name='demo',root='' WHERE id='demo'")
            restarted = control.Application({"coordination_mode": "local"}, root)
            self.assertEqual(restarted.project_memory.project("demo")["root"], "/latest")
            self.assertEqual(restarted.project_memory.read("demo")["memory"], saved)


if __name__ == "__main__":
    unittest.main()
