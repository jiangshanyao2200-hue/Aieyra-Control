"""Exercise lifecycle helpers against an isolated real local Control HTTP server."""

import importlib.util
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from test_control import control

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("lifecycle_client", ROOT / "scripts/agent-client.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


class LifecycleHttpTests(unittest.TestCase):
    def test_lease_and_finish_preserve_memory_runtime_and_unconfirmed_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = control.Application(
                {
                    "coordination_mode": "local",
                    "resources": [],
                    "local_projects": [{"id": "demo", "name": "Demo", "root": "/fixture"}],
                },
                root / "data",
            )
            token = secrets.token_urlsafe(32)
            enrolled = app.agent_access.enroll(
                {"request_id": "enroll", "name": "Fixture", "project": "demo", "token": token}
            )
            server = control.Server(0, app)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                config = {"url": server.origin, "token": token}
                client = cli.AgentClient(config)
                seat = client.call("seats")["seats"][0]

                def connect(sid):
                    return client.call(
                        "connect",
                        {
                            "request_id": "connect-" + sid,
                            "session_id": sid,
                            "seat_id": seat["id"],
                            "seat_epoch": seat["epoch"],
                        },
                    )

                connect("lease-one")
                client.call(
                    "heartbeat",
                    {
                        "request_id": "running",
                        "session_id": "lease-one",
                        "runtime_state": "running",
                    },
                )
                marker = root / "active"
                marker.touch()
                stop = threading.Event()

                def activity_ends(_):
                    marker.unlink()
                    return False

                with patch.object(stop, "wait", side_effect=activity_ends):
                    result = cli.keep_lease(client, "lease-one", marker, stop=stop)
                self.assertTrue(result["lease_released"])
                self.assertEqual(result["heartbeats"], 1)
                session = client.call("sessions/lease-one")["session"]
                self.assertEqual(session["runtime_state"], "running")
                self.assertEqual(session["lease_until"], 0)

                connect("finish-one")
                before = client.call(
                    "memory",
                    {
                        "request_id": "save",
                        "session_id": "finish-one",
                        "project": "demo",
                        "version": 0,
                        "summary": "Fixture checkpoint",
                        "sections": {
                            key: "Preserve " + key
                            for key in ("blueprint", "timeline", "checkpoint", "recovery", "index")
                        },
                    },
                )
                app.tick()
                app.submit(
                    {
                        "request_id": "pending",
                        "target": enrolled["credential"]["actor_id"],
                        "body": "Do not execute this fixture.",
                    }
                )
                app.dispatch_one()
                pending = client.call("inbox", query={"session_id": "finish-one"})["deliveries"]
                self.assertEqual(len(pending), 1)
                config_file = root / "agent.json"
                config_file.write_text(json.dumps(config), encoding="utf-8")
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-X",
                        "utf8",
                        str(ROOT / "scripts/agent-client.py"),
                        "--config",
                        str(config_file),
                        "finish",
                        "--session-id",
                        "finish-one",
                        "--request-id",
                        "finish-request",
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=15,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
                receipt = json.loads(completed.stdout)
                self.assertTrue(receipt["lease_released"])
                self.assertTrue(receipt["pending_deliveries_read"])
                self.assertEqual(
                    receipt["unconfirmed_before_disconnect"],
                    [{"id": pending[0]["id"], "state": "queued"}],
                )
                self.assertNotIn("Do not execute", completed.stdout)
                self.assertEqual(client.call("memory")["memory"], before["memory"])
                after = client.call("deliveries/" + pending[0]["id"])["delivery"]
                self.assertEqual(after["state"], "unknown")
                again = cli.finish_session(client, "finish-one", "finish-recheck")
                self.assertFalse(again["pending_deliveries_read"])
                self.assertTrue(again["lease_released"])
            finally:
                server.shutdown()
                server.server_close()
                worker.join(5)


if __name__ == "__main__":
    unittest.main()
