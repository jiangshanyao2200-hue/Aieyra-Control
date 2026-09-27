import concurrent.futures
import tempfile
import unittest
from pathlib import Path
from test_control import control
from project_memory import ProjectMemory, MemoryError, SECTIONS


class ProjectMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = control.Store(Path(self.tmp.name) / "memory.sqlite")
        self.projects = [{"id": "control", "name": "Control", "root": self.tmp.name}]
        self.memory = ProjectMemory(self.store, self.projects)
        self.body = {
            "request_id": "save-one",
            "project": "control",
            "version": 0,
            "sections": {k: "内容 / " + k for k in SECTIONS},
            "summary": "Checkpoint",
        }

    def test_revisions_replay_restart_and_history_cursor(self):
        self.assertEqual(self.memory.read("control")["missing_sections"], list(SECTIONS))
        first = self.memory.save(self.body, "leader", "session-one")
        for v in range(1, 24):
            self.memory.save(
                {**self.body, "request_id": "save-" + str(v), "version": v}, "leader", "session-two"
            )
        reopened = ProjectMemory(self.store, self.projects)
        replay = reopened.save(self.body, "leader", "session-one")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["memory"], first["memory"])
        self.assertEqual(replay["current_version"], 24)
        page = reopened.history("control")
        self.assertEqual(len(page["revisions"]), 20)
        self.assertEqual(
            [r["version"] for r in reopened.history("control", page["next_before"])["revisions"]],
            [4, 3, 2, 1],
        )
        self.assertEqual(reopened.listing()["projects"][0]["version"], 24)

    def test_two_sessions_only_one_writer_wins(self):
        def write(i):
            try:
                self.memory.save(
                    {**self.body, "request_id": "write-" + str(i)}, "leader", "session-" + str(i)
                )
                return "saved"
            except MemoryError as e:
                return e.code

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(write, range(2)))
        self.assertCountEqual(results, ["saved", "memory_version_conflict"])

    def test_metadata_sync_preserves_revisions_and_id_only_registration(self):
        saved = self.memory.save(self.body, "leader")["memory"]
        self.memory.register({"id": "control", "name": "Current name", "root": "/new/project"})
        self.memory.register({"id": "control"})
        self.assertEqual(
            self.memory.project("control"),
            {"id": "control", "name": "Current name", "root": "/new/project"},
        )
        self.assertEqual(self.memory.read("control")["memory"], saved)
        self.memory.register({"id": "control", "root": ""})
        self.assertEqual(self.memory.project("control")["root"], "")
        self.assertEqual(self.memory.project("control")["name"], "Current name")

    def test_history_and_revision_bounds_reject_oversized_sqlite_numbers(self):
        for number in [0, -1, True, 2**80]:
            with self.assertRaises(MemoryError):
                self.memory.read("control", number)
            with self.assertRaises(MemoryError):
                self.memory.history("control", number)

    def test_invalid_input_and_conflicting_replay_do_not_change_checkpoint(self):
        self.memory.save(self.body, "leader")
        invalid = [
            {**self.body, "summary": "changed"},
            {**self.body, "request_id": "bad", "version": True},
            {**self.body, "request_id": "bad", "sections": {"checkpoint": "partial overwrite"}},
            {**self.body, "request_id": "bad", "sections": {k: "界" * 2000 for k in SECTIONS}},
            {**self.body, "request_id": "bad", "project": "../outside"},
        ]
        for value in invalid:
            with self.subTest(value=value.get("request_id")):
                with self.assertRaises(MemoryError):
                    self.memory.save(value, "leader")
        self.assertEqual(self.memory.read("control")["memory"]["version"], 1)
