import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import urlopen

from test_control import control, Hub
from request_intake import RequestIntake, IntakeError


class IntakeHub(Hub):
    def __init__(self):
        super().__init__()
        self.reads = []
        self.result = {
            "items": [],
            "next_cursor": "",
            "has_more": False,
            "stream_epoch": "epoch-1",
            "cursor": 4,
        }

    def intake_read(self, route, query):
        self.reads.append((route, query))
        return self.result


class IntakeTests(unittest.TestCase):
    def test_preserves_first_page_cursor_history_and_distinct_target_owner(self):
        hub = IntakeHub()
        hub.result["items"] = [
            {
                "id": "requirement-1",
                "version": 3,
                "source": {"revision": 2},
                "links": [{"task_id": "task-1", "target_actors": ["worker-a"]}],
                "tasks": [{"id": "task-1", "owner": None, "status": "planned"}],
            }
        ]
        before = copy.deepcopy(hub.result)
        result = RequestIntake(hub).read("requirements", {"after": [""], "limit": ["20"]})
        self.assertEqual(result["items"], before["items"])
        self.assertEqual(result["cursor"], 4)
        self.assertEqual(hub.result, before)
        self.assertEqual(hub.calls, [])
        self.assertTrue(result["control"]["available"])

    def test_query_rejects_arbitrary_route_duplicate_id_and_invalid_cursor(self):
        hub = IntakeHub()
        reader = RequestIntake(hub)
        for route, query in [
            ("task-retire", {}),
            ("task", {"id": ["x", "y"]}),
            ("tasks", {"url": ["https://elsewhere.invalid"]}),
            ("tasks", {"limit": ["101"]}),
            ("requirement", {"id": ["x"], "history": ["2"]}),
            ("intake-events", {"after": ["nan"]}),
            ("requirement-receipts", {}),
            ("requirements", {"state": ["completed"]}),
        ]:
            with self.subTest(route=route, query=query), self.assertRaises(IntakeError):
                reader.read(route, query)
        self.assertFalse(hub.reads)

    def test_remote_not_connected_and_unknown_errors_do_not_expose_details(self):
        reader = RequestIntake(Hub())
        with self.assertRaises(IntakeError) as missing:
            reader.read("tasks", {})
        self.assertEqual(
            (missing.exception.code, missing.exception.status), ("intake_not_connected", 503)
        )
        hub = IntakeHub()
        for text, code, status in [
            ("404:not_found", "intake_not_connected", 503),
            ("409:stream_epoch_mismatch", "stream_epoch_mismatch", 409),
            ("403:project_scope_denied", "project_scope_denied", 403),
            ("transport error with PRIVATE_SENTINEL", "intake_unavailable", 503),
        ]:

            def fail(*args):
                raise RuntimeError(text)

            hub.intake_read = fail
            with self.subTest(code=code), self.assertRaises(IntakeError) as rejected:
                RequestIntake(hub).read("tasks", {})
            self.assertEqual((rejected.exception.code, rejected.exception.status), (code, status))

    def test_hub_uses_only_read_identity_and_encodes_search(self):
        called = []

        class Client:
            def call(self, identity, path):
                called.append((identity, path))
                return {"items": []}

        hub = control.Hub.__new__(control.Hub)
        hub.config = {"hub_read_identity": "read-station", "hub_user_identity": "human-user"}
        hub.client = Client()
        hub.intake_read("requirements", {"q": "a&project=private", "limit": "20"})
        self.assertEqual(
            called, [("read-station", "/v1/requirements?q=a%26project%3Dprivate&limit=20")]
        )
        with self.assertRaises(ValueError):
            hub.intake_read("../task-retire", {})
        self.assertEqual(len(called), 1)

    def test_real_http_routes_remain_read_only_and_unavailable_is_explicit(self):
        with tempfile.TemporaryDirectory() as temp:
            hub = IntakeHub()
            queued = []
            app = control.Application(
                {"resources": []}, Path(temp), hub=hub, queue_runner=lambda *a: queued.append(a)
            )
            server = control.Server(0, app)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                for route in (
                    "requirements",
                    "tasks",
                    "requirement?id=req",
                    "task?id=task",
                    "requirement-receipts?id=req",
                    "intake-receipt?request_id=write",
                    "intake-events?after=0",
                ):
                    with urlopen(server.origin + "/api/" + route, timeout=3) as response:
                        self.assertTrue(json.load(response)["control"]["available"])
                self.assertEqual(len(hub.reads), 7)
                self.assertEqual(app.store.deliveries(), [])
                self.assertFalse(queued)

                def fail(*args):
                    raise RuntimeError("404:not_found")

                hub.intake_read = fail
                with self.assertRaises(HTTPError) as rejected:
                    urlopen(server.origin + "/api/requirements", timeout=3)
                value = json.load(rejected.exception)
                self.assertEqual(rejected.exception.code, 503)
                self.assertEqual(value["code"], "intake_not_connected")
                self.assertTrue(value["stale"])
                self.assertFalse(value["available"])
                with self.assertRaises(HTTPError) as duplicate:
                    urlopen(server.origin + "/api/task?id=a&id=b", timeout=3)
                self.assertEqual(duplicate.exception.code, 400)
            finally:
                server.shutdown()
                server.server_close()
                worker.join(3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
