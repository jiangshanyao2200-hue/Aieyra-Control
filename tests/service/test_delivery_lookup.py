import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from test_control import control, Hub


class DeliveryLookupTests(unittest.TestCase):
    def test_lookup_original_receipt_outside_snapshot_window_without_replay(self):
        with tempfile.TemporaryDirectory() as temp:
            hub = Hub()
            app = control.Application(
                {"resources": []},
                Path(temp),
                hub=hub,
                queue_runner=lambda *a: self.fail("lookup queued work"),
            )
            original = app.store.submit("original-request", "all", "exact original body")
            for index in range(201):
                app.store.submit(f"later-{index}", "all", f"later {index}")
            self.assertNotIn(original["id"], {row["id"] for row in app.store.deliveries()})
            server = control.Server(0, app)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                for _ in range(2):
                    with urlopen(
                        server.origin + "/api/deliveries/original-request", timeout=3
                    ) as response:
                        value = json.load(response)["delivery"]
                        self.assertEqual(value["id"], original["id"])
                        self.assertEqual(value["body"], "exact original body")
                        self.assertEqual(value["state"], "pending")
                self.assertEqual(hub.calls, [])
                with self.assertRaises(HTTPError) as missing:
                    urlopen(server.origin + "/api/deliveries/not-present", timeout=3)
                self.assertEqual(missing.exception.code, 404)
                self.assertEqual(json.load(missing.exception)["code"], "delivery_not_found")
                request = Request(
                    server.origin + "/api/deliveries/original-request",
                    headers={"Origin": "https://elsewhere.invalid"},
                )
                with self.assertRaises(HTTPError) as foreign:
                    urlopen(request, timeout=3)
                self.assertEqual(foreign.exception.code, 403)
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
