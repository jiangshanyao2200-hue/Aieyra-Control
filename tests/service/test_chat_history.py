import json
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from test_control import control


class ChatHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.app = control.Application(
            {"coordination_mode": "local", "resources": []}, self.root / "data"
        )
        self.addCleanup(self.app.stop.set)

    def seed(self, count=205):
        now = time.time()
        with self.app.hub.client.store.db() as db:
            for i in range(count):
                db.execute(
                    "INSERT INTO messages(id,sender,project,kind,body,created) VALUES(?,?,?,?,?,?)",
                    (
                        "m" + str(i),
                        "owner",
                        "coordination",
                        "progress",
                        "record " + str(i),
                        now - (count - i) * 10,
                    ),
                )
            for name, at in [("old", now - 90000), ("future", now + 3600)]:
                db.execute(
                    "INSERT INTO messages(id,sender,project,kind,body,created) VALUES(?,?,?,?,?,?)",
                    (name, "owner", "coordination", "progress", name, at),
                )

    def test_24_hour_cursor_pages_complete_without_deleting_history(self):
        self.seed()
        seen = []
        before = None
        for _ in range(3):
            page = self.app.hub.chat_history(before)
            self.assertEqual(page["retention_hours"], 24)
            self.assertEqual(page["window_end"] - page["window_start"], 86400)
            self.assertLessEqual(len(page["messages"]), 100)
            seen.extend(m["id"] for m in page["messages"])
            before = page["next_before"]
        self.assertEqual(len(set(seen)), 205)
        self.assertEqual(len(seen), 205)
        self.assertFalse(page["has_more"])
        self.assertIsNone(before)
        with self.app.hub.client.store.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM messages").fetchone()[0], 207)

    def test_cursor_excludes_new_arrivals(self):
        self.seed()
        first = self.app.hub.chat_history()
        with self.app.hub.client.store.db() as db:
            db.execute(
                "INSERT INTO messages(id,sender,project,kind,body,created) VALUES(?,?,?,?,?,?)",
                ("new", "owner", "coordination", "progress", "new", time.time()),
            )
        older = self.app.hub.chat_history(first["next_before"])
        self.assertFalse(
            set(m["id"] for m in first["messages"]) & set(m["id"] for m in older["messages"])
        )
        self.assertNotIn("new", [m["id"] for m in older["messages"]])
        self.assertEqual(self.app.hub.chat_history()["messages"][-1]["id"], "new")

    def test_empty_and_http_query_origin_boundaries(self):
        self.assertEqual(self.app.hub.chat_history()["messages"], [])
        web = self.root / "web"
        web.mkdir()
        server = control.Server(0, self.app, web)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with urllib.request.urlopen(server.origin + "/api/chat-history") as r:
            self.assertEqual(json.load(r)["retention_hours"], 24)
        for q in [
            "before=-1",
            "before=0",
            "before=abc",
            "before=",
            "before=1&before=2",
            "hours=99",
            "before=9223372036854775808",
        ]:
            with self.assertRaises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(server.origin + "/api/chat-history?" + q)
            self.assertEqual(err.exception.code, 400, q)
        with self.assertRaises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(
                urllib.request.Request(
                    server.origin + "/api/chat-history",
                    headers={"Origin": "https://untrusted.invalid"},
                )
            )
        self.assertEqual(err.exception.code, 403)


if __name__ == "__main__":
    unittest.main()
