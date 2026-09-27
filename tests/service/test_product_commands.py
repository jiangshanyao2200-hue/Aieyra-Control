import importlib.util
import http.client
import json
from pathlib import Path
import sys
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[2]
ADAPTER = ROOT / "service/product_bridge"
sys.path.insert(0, str(ADAPTER))
from fixture_os import FakeOS, sample_task
from bridge import Bridge

spec = importlib.util.spec_from_file_location("product_command_service", ROOT / "service/main.py")
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)


class ProductHTTPTests(unittest.TestCase):
    def setUp(self):
        self.fixture = FakeOS()
        self.addCleanup(self.fixture.close)
        self.fixture.config["enable_matrix_writes"] = True
        self.fixture.config_path.write_text(json.dumps(self.fixture.config), encoding="utf-8")
        self.app = control.Application(
            {
                "resources": [],
                "collaboration_sources": [
                    {
                        "id": "isolated",
                        "adapter_dir": str(ADAPTER),
                        "config_ref": str(self.fixture.config_path),
                    }
                ],
            },
            self.fixture.root / "service",
            hub=object(),
        )
        self.server = control.Server(0, self.app, self.fixture.root)
        self.addCleanup(self.server.server_close)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.value = {
            "source_id": "isolated",
            "command": {
                "action": "matrix.send",
                "request_id": "alpha-http-1",
                "message": "Read only fixture.",
            },
        }

    def post(self, value, path="/api/collaboration/command", csrf=True):
        headers = {"Content-Type": "application/json", "Origin": self.server.origin}
        if csrf:
            headers["X-Control-CSRF"] = self.app.csrf
        request = urllib.request.Request(
            self.server.origin + path, data=json.dumps(value).encode(), headers=headers
        )
        with urllib.request.urlopen(request) as response:
            return json.load(response)

    def get(self, path):
        with urllib.request.urlopen(self.server.origin + path) as response:
            return json.load(response)

    def test_real_subprocess_write_status_and_duplicate_share_bridge_ledger(self):
        first = self.post(self.value)
        self.assertEqual(first["state"], "observed")
        self.assertTrue(self.get("/api/collaboration/matrix?source_id=isolated")["busy"])
        status = self.get("/api/collaboration/command?source_id=isolated&request_id=alpha-http-1")
        self.assertEqual(status["observation"]["request_id"], "alpha-http-1")
        self.post(self.value)
        self.assertEqual(self.fixture.calls["matrix.send"], 1)

    def test_csrf_and_source_paths_rejected_before_runtime_calls(self):
        with self.assertRaises(urllib.error.HTTPError) as denied:
            self.post(self.value, csrf=False)
        self.assertEqual(denied.exception.code, 403)
        for value in (
            self.value | {"source_id": "unknown"},
            self.value | {"config_ref": "C:/untrusted"},
        ):
            with self.assertRaises(urllib.error.HTTPError) as rejected:
                self.post(value)
            self.assertEqual(rejected.exception.code, 409)
        self.assertEqual(self.fixture.calls["matrix.send"], 0)

    def test_unknown_response_query_does_not_repost(self):
        self.fixture.drop_after["matrix.send"] = 1
        first = self.post(self.value)
        self.assertEqual(first["state"], "unknown")
        self.assertEqual(self.post(self.value)["state"], "observed")
        self.assertEqual(self.fixture.calls["matrix.send"], 1)

    def test_disconnected_post_keeps_committed_receipt_without_second_response(self):
        attempts = []
        handler = control.Handler

        class AbortedWriter:
            def __init__(self, original):
                self.original = original

            def __getattr__(self, name):
                return getattr(self.original, name)

            def write(self, _):
                raise ConnectionAbortedError("fixture client disconnected")

        class AbortedResponseHandler(handler):
            def send_response(self, status, message=None):
                if self.command == "POST":
                    attempts.append(status)
                return super().send_response(status, message)

            def end_headers(self):
                if self.command == "POST" and self.server.abort_phase == "headers":
                    raise ConnectionAbortedError("fixture client disconnected")
                super().end_headers()
                if self.command == "POST":
                    self.wfile = AbortedWriter(self.wfile)

        self.server.RequestHandlerClass = AbortedResponseHandler
        self.server.handle_error = Mock()
        for phase in ("headers", "body"):
            with self.subTest(phase=phase):
                attempts.clear()
                self.server.abort_phase = phase
                value = {
                    "source_id": "isolated",
                    "command": dict(self.value["command"], request_id="abort-" + phase),
                }
                with self.assertRaises(
                    (http.client.RemoteDisconnected, http.client.IncompleteRead)
                ):
                    self.post(value)
                result = self.get(
                    "/api/collaboration/command?source_id=isolated&request_id=abort-" + phase
                )
                self.assertEqual(result["state"], "observed")
                self.assertEqual(result["observation"]["request_id"], "abort-" + phase)
                self.assertEqual(attempts, [200])
                self.server.handle_error.assert_not_called()
                # Release fixture mother before the independent next request.
                self.fixture.exchanges["abort-" + phase]["state"] = "completed"
        self.assertEqual(self.fixture.calls["matrix.send"], 2)

    def test_dispatch_returns_received_not_accepted(self):
        result = self.post(
            {"source_id": "isolated", "task": sample_task()}, "/api/collaboration/dispatch"
        )
        self.assertTrue(result["os_received"])
        self.assertEqual(result["state"], "received")
        self.assertEqual(result["record"]["acceptance"], "pending")

    def test_matrix_created_station_projection_then_continue_and_cancel_through_http(self):
        # Model-created workers bypass bridge.dispatch; the local task ledger is empty.
        bridge = Bridge(self.fixture.config)
        _, spec = bridge._spec(sample_task("alpha-model-created"))
        spec["request_id"] = "model-request"
        Path(spec["workdir"]).mkdir()
        aid = bridge.client.rpc("agents.create", spec)["id"]
        self.fixture.complete(aid)
        self.app.collaboration.poll()
        station = self.get("/api/collaboration")["runtime_stations"][0]
        self.assertEqual(
            station["command_target"], {"source_id": "isolated", "agent_id": aid, "generation": 1}
        )
        self.assertTrue(station["capabilities"]["send"])
        value = {
            "source_id": "isolated",
            "command": {
                "action": "agents.send",
                "agent_id": aid,
                "generation": 1,
                "request_id": "alpha-http-child",
                "message": "Another explicit task.",
            },
        }
        result = self.post(value)
        self.assertEqual(result["observation"]["generation"], 2)
        self.post(value)
        self.assertEqual(self.fixture.calls["agents.send"], 1)
        self.app.collaboration.poll()
        task = self.get("/api/collaboration")["tasks"][0]
        self.assertEqual(task["task_id"], station["id"] + ":g2")
        self.assertEqual(task["acceptance"], "pending")
        cancel = {
            "source_id": "isolated",
            "command": {
                "action": "agents.cancel",
                "agent_id": aid,
                "generation": 2,
                "request_id": "alpha-http-child-stop",
            },
        }
        self.assertEqual(self.post(cancel)["observation"]["state"], "cancelled")
        with bridge.transaction() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
