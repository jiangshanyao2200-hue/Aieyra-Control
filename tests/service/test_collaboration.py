import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("collaboration_control", ROOT / "service/main.py")
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)


class Client:
    mode = "fixture"
    binding = {"session_id": "matrix-fixture", "token": "DO_NOT_EXPOSE"}

    def __init__(self):
        self.state = "running"
        self.acceptance = "pending"
        self.offline = False
        self.config = {
            "enable_os_writes": True,
            "enable_matrix_writes": True,
            "allowed_models": ["go-fixture"],
            "allowed_tools": ["exec_command"],
        }
        self.description = {
            "actions": [
                "agents.create",
                "agents.send",
                "agents.cancel",
                "matrix.send",
                "matrix.cancel",
            ],
            "generation_guarded_commands": True,
        }
        self.generation = 1
        self.history = None
        self.contract = None
        self.reviews = None
        self.parent = "matrix-fixture"
        self.exchanges = [
            {
                "request_id": "goal1",
                "state": "running",
                "model": "go-fixture",
                "reply": "PRIVATE_REPLY",
                "message": "PRIVATE_MESSAGE",
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 4,
                    "total_tokens": 16,
                    "prompt_tokens_details": {"cached_tokens": None, "private": "PRIVATE_USAGE"},
                },
            }
        ]

    def rpc(self, action, args=None):
        if self.offline:
            raise OSError("private transport text")
        if action == "matrix.status":
            return {
                "busy": self.state == "running",
                "phase": "tools",
                "model": "go-fixture",
                "requests": copy.deepcopy(self.exchanges),
            }
        if action == "agents.list":
            return [
                {
                    "id": "worker1",
                    "title": "Read fixture",
                    "state": self.state,
                    "parent": "matrix-fixture",
                }
            ]
        if action == "agents.status":
            return {
                "id": "worker1",
                "parent": self.parent,
                "state": self.state,
                "acceptance": self.acceptance,
                "generation": self.generation,
                "origin_request": "goal1",
                "spec": {
                    "model_id": "go-fixture",
                    "prompt": "PRIVATE_PROMPT",
                    "api_key": "PRIVATE_KEY",
                    "tools": ["exec_command"],
                    "max_requests": 8,
                    "contract": self.contract,
                },
                "result": "PRIVATE_RESULT",
                "delivery": {
                    "generation": self.generation,
                    "artifacts": [{"path": "result.txt", "sha256": "a" * 64, "bytes": 4}],
                },
                "reviews": self.reviews,
                "history": self.history,
            }
        if action == "agents.activity":
            return {
                "items": [
                    {
                        "id": "tool1",
                        "state": self.state,
                        "tool": "exec_command",
                        "command": "Get-Content fixture.txt",
                        "revision": 1,
                    }
                ],
                "cursor": 1,
                "has_more": False,
                "has_older": False,
            }
        raise AssertionError(action)


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = control.Store(self.root / "state.sqlite")
        self.config = {
            "collaboration_sources": [
                {
                    "id": "os-lab",
                    "project": "os",
                    "adapter_dir": str(self.root),
                    "config_ref": str(self.root / "bridge.json"),
                }
            ]
        }
        self.client = Client()
        self.projection = control.Projection(self.store, self.config, lambda s: self.client)

    def test_real_fields_private_data_and_completion_review_are_separate(self):
        self.projection.poll()
        s = self.projection.snapshot()
        task = s["tasks"][0]
        self.assertTrue(s["available"])
        self.assertEqual(task["state"], "running")
        self.assertEqual(task["id"], "os-lab:matrix-fixture:worker1")
        self.assertEqual(task["activity"]["items"][0]["command"], "Get-Content fixture.txt")
        self.assertNotIn("PRIVATE_", json.dumps(s))
        self.assertNotIn("DO_NOT_EXPOSE", json.dumps(s))
        self.client.state = "completed"
        self.projection.poll()
        task = self.projection.snapshot()["tasks"][0]
        self.assertEqual(task["state"], "completed")
        self.assertEqual(task["acceptance"], "pending")
        self.assertEqual(task["artifacts"][0]["generation"], 1)

    def test_matrix_goal_station_and_task_generation_are_linked(self):
        self.client.contract = {"acceptance": ["Read fixture correctly"]}
        self.projection.poll()
        s = self.projection.snapshot()
        task = s["tasks"][0]
        station = s["runtime_stations"][0]
        self.assertEqual(task["parent_id"], "os-lab:matrix-fixture")
        self.assertEqual(task["assigned_tools"], ["exec_command"])
        self.assertEqual(task["budget"], {"max_requests": 8})
        self.assertEqual(task["acceptance_criteria"], ["Read fixture correctly"])
        self.assertEqual(station["current_task_id"], task["id"] + ":g1")
        self.assertTrue(station["active"])
        self.assertEqual(
            station["command_target"],
            {"source_id": "os-lab", "agent_id": "worker1", "generation": 1},
        )
        self.assertEqual(station["capabilities"], {"send": False, "cancel": True})
        self.assertEqual(s["goals"][0]["runtime_station_ids"], [station["id"]])
        self.assertEqual(
            s["goals"][0]["usage"],
            {
                "prompt_tokens": 12,
                "completion_tokens": 4,
                "total_tokens": 16,
                "prompt_tokens_details": {"cached_tokens": None},
            },
        )
        self.assertTrue(s["matrices"][0]["busy"])
        self.assertEqual(s["matrices"][0]["model"], "go-fixture")
        self.client.generation = 2
        self.client.history = [
            {
                "generation": 1,
                "state": "completed",
                "acceptance": "accepted",
                "result": "PRIVATE_OLD_RESULT",
                "delivery": {
                    "generation": 1,
                    "artifacts": [{"path": "old.txt", "sha256": "b" * 64, "bytes": 3}],
                },
            }
        ]
        self.projection.poll()
        new = self.projection.snapshot()
        self.assertEqual(new["runtime_stations"][0]["id"], station["id"])
        self.assertEqual(new["tasks"][0]["task_id"], task["id"] + ":g2")
        self.assertEqual(new["tasks"][0]["history"][0]["task_id"], task["id"] + ":g1")
        self.assertEqual(new["tasks"][0]["history"][0]["artifacts"][0]["sha256"], "b" * 64)
        self.assertNotIn("PRIVATE_", json.dumps(new))

    def test_reviews_expose_criteria_and_shell_receipts_without_extra_payload(self):
        self.client.state = "completed"
        self.client.acceptance = "accepted"
        self.client.reviews = [
            {
                "request_id": "review1",
                "generation": 1,
                "verdict": "accepted",
                "evidence": "independent hash matched",
                "private": "PRIVATE_REVIEW",
                "checks": [
                    {
                        "criterion": 0,
                        "verdict": "passed",
                        "evidence": "shell result equals reference",
                        "private": "PRIVATE_CHECK",
                        "receipts": [
                            {
                                "agent_id": "matrix-fixture",
                                "session_id": 10,
                                "expected_exit": 0,
                                "private": "PRIVATE_RECEIPT",
                            }
                        ],
                    }
                ],
            }
        ]
        self.projection.poll()
        s = self.projection.snapshot()
        review = s["tasks"][0]["reviews"][0]
        self.assertEqual(review["evidence"], "independent hash matched")
        self.assertEqual(
            review["checks"][0]["receipts"],
            [{"agent_id": "matrix-fixture", "session_id": 10, "expected_exit": 0}],
        )
        self.assertNotIn("PRIVATE_", json.dumps(s))
        self.assertFalse(s["runtime_stations"][0]["active"])
        self.assertEqual(s["runtime_stations"][0]["capabilities"], {"send": True, "cancel": False})

    def test_capabilities_require_current_protocol_and_explicit_write_permissions(self):
        self.client.mode = "os"
        self.projection.poll()
        self.assertTrue(self.projection.snapshot()["capabilities"]["agent_cancel"])
        self.client.config["enable_os_writes"] = False
        self.projection.poll()
        caps = self.projection.snapshot()["capabilities"]
        self.assertTrue(caps["observe"])
        self.assertFalse(any(v for k, v in caps.items() if k != "observe"))
        self.client.config["enable_os_writes"] = True
        self.client.config["enable_matrix_writes"] = False
        self.client.description["generation_guarded_commands"] = False
        self.projection.poll()
        caps = self.projection.snapshot()["capabilities"]
        self.assertTrue(caps["dispatch"])
        for name in (
            "matrix_send",
            "matrix_cancel",
            "agent_send",
            "agent_cancel",
            "human_callback",
        ):
            self.assertFalse(caps[name], name)
        self.client.description["actions"] = []
        self.projection.poll()
        self.assertFalse(self.projection.snapshot()["capabilities"]["dispatch"])

    def test_invalid_allowlists_do_not_advertise_dispatch(self):
        for model, tools in (
            ([], []),
            ("fake", ["exec_command"]),
            (["go-fixture"], None),
            (["go-fixture"], [None]),
        ):
            self.client.config.update(allowed_models=model, allowed_tools=tools)
            self.projection.poll()
            self.assertFalse(self.projection.snapshot()["capabilities"]["dispatch"])

    def test_unordered_matrix_requests_do_not_generate_false_progress(self):
        self.client.exchanges.append({"request_id": "goal2", "state": "completed", "usage": None})
        self.projection.poll()
        self.client.exchanges.reverse()
        self.projection.poll()
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)

    def test_nested_parent_remains_in_same_matrix(self):
        self.client.parent = "worker-parent"
        self.projection.poll()
        self.assertEqual(
            self.projection.snapshot()["tasks"][0]["parent_id"],
            "os-lab:matrix-fixture:worker-parent",
        )

    def test_malformed_matrix_status_keeps_last_fact_stale(self):
        self.projection.poll()
        rpc = self.client.rpc
        self.client.rpc = (
            lambda action, args=None: {"busy": "unknown", "requests": []}
            if action == "matrix.status"
            else rpc(action, args)
        )
        self.projection.poll()
        s = self.projection.snapshot()
        self.assertTrue(s["stale"])
        self.assertEqual(s["tasks"][0]["state"], "running")
        self.assertIsNone(s["runtime_stations"][0]["active"])
        self.assertFalse(any(s["capabilities"].values()))
        self.assertFalse(any(s["runtime_stations"][0]["capabilities"].values()))

    def test_sampling_does_not_emit_fake_progress_and_offline_restart_stays_stale(self):
        self.projection.poll()
        self.projection.poll()
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
        restart = control.Projection(self.store, self.config, lambda s: self.client)
        self.assertTrue(restart.snapshot()["stale"])
        self.assertFalse(restart.snapshot()["available"])
        self.assertFalse(any(restart.snapshot()["capabilities"].values()))
        self.assertIsNone(restart.snapshot()["runtime_stations"][0]["active"])
        self.client.offline = True
        self.projection.poll()
        s = self.projection.snapshot()
        self.assertTrue(s["stale"])
        self.assertFalse(s["available"])
        self.assertEqual(s["tasks"][0]["state"], "running")
        self.assertTrue(s["tasks"][0]["stale"])
        self.assertNotIn("private transport", json.dumps(s))
        self.assertTrue(s["goals"][0]["stale"])
        self.assertFalse(any(s["capabilities"].values()))
        self.client.offline = False
        self.client.state = "cancelled"
        restart.poll()
        self.assertEqual(restart.snapshot()["tasks"][0]["state"], "cancelled")

    def test_unconnected_human_contract_does_not_revive_blocked_history(self):
        data = self.projection.human_requests()
        self.assertFalse(data["available"])
        self.assertTrue(data["stale"])
        self.assertEqual(data["items"], [])

    def test_http_read_endpoints_and_origin_guard(self):
        app = control.Application({"resources": []}, self.root / "app", hub=object())
        app.collaboration = self.projection
        self.projection.poll()
        web = self.root / "web"
        web.mkdir()
        server = control.Server(0, app, web)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        for path in ("/api/collaboration", "/api/human-requests"):
            with urllib.request.urlopen(server.origin + path) as response:
                self.assertEqual(json.load(response)["schema_version"], 1)
        request = urllib.request.Request(
            server.origin + "/api/collaboration", headers={"Origin": "https://other.example"}
        )
        with self.assertRaises(urllib.error.HTTPError) as rejected:
            urllib.request.urlopen(request)
        self.assertEqual(rejected.exception.code, 403)


if __name__ == "__main__":
    unittest.main()
