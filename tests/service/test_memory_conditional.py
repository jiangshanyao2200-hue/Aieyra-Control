"""Conditional handoff reads through SQLite, real HTTP, CLI/MCP and finish."""

from contextlib import contextmanager
import copy
import json
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import build_opener, ProxyHandler

import test_project_memory as memory_fixture
import test_agent_station as station_fixture
import test_agent_client as client_fixture

MemoryError = memory_fixture.MemoryError
SECTIONS = memory_fixture.SECTIONS
client = client_fixture.client_module


class ConditionalStorageTests(unittest.TestCase):
    setUp = memory_fixture.ProjectMemoryTests.setUp

    def saved(self):
        return self.memory.save(self.body, "leader")["memory"]

    def test_match_omits_body_without_parsing_or_selecting_sections(self):
        saved = self.saved()
        statements = []
        original_db = self.store.db

        @contextmanager
        def traced_db():
            with original_db() as db:
                db.set_trace_callback(statements.append)
                yield db

        with (
            patch.object(self.store, "db", traced_db),
            patch.object(
                self.memory, "document", side_effect=AssertionError("parsed unchanged sections")
            ),
        ):
            value = self.memory.read("control", if_version=1, if_sha256=saved["sha256"])
        self.assertTrue(value["not_modified"])
        self.assertEqual(value["conditional_read"], "version_and_sha256")
        self.assertNotIn("sections", value["memory"])
        self.assertNotIn("missing_sections", value)
        self.assertNotIn("内容", json.dumps(value, ensure_ascii=False))
        selects = [q.upper() for q in statements if "FROM project_memory_revisions" in q]
        self.assertTrue(selects)
        self.assertTrue(all("SECTIONS" not in q and "SELECT *" not in q for q in selects))
        self.assertFalse(
            any(q.lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE")) for q in statements)
        )
        self.assertEqual(value["memory"], {k: v for k, v in saved.items() if k != "sections"})

    def test_version_and_hash_are_both_required_and_revision_change_is_visible(self):
        first = self.saved()
        for version, digest in [(2, first["sha256"]), (1, "0" * 64)]:
            result = self.memory.read("control", if_version=version, if_sha256=digest)
            self.assertFalse(result["not_modified"])
            self.assertEqual(result["memory"], first)
        second = self.memory.save({**self.body, "request_id": "next", "version": 1}, "leader")[
            "memory"
        ]
        self.assertEqual(first["sha256"], second["sha256"])
        result = self.memory.read("control", if_version=1, if_sha256=first["sha256"])
        self.assertFalse(result["not_modified"])
        self.assertEqual(result["memory"]["version"], 2)
        self.assertEqual(self.memory.read("control", 1)["memory"], first)
        self.assertNotIn("not_modified", self.memory.read("control"))

    def test_empty_project_and_missing_sections_are_not_empty_success(self):
        empty = self.memory.read("control", if_version=1, if_sha256="0" * 64)
        self.assertFalse(empty["not_modified"])
        self.assertEqual(empty["memory"]["version"], 0)
        self.assertEqual(empty["missing_sections"], list(SECTIONS))
        saved = self.memory.save({**self.body, "sections": {k: "" for k in SECTIONS}}, "leader")[
            "memory"
        ]
        match = self.memory.read("control", if_version=1, if_sha256=saved["sha256"])
        self.assertTrue(match["not_modified"])
        self.assertNotIn("missing_sections", match)

    def test_condition_validation_is_strict_and_never_mutates_memory(self):
        saved = self.saved()
        bad = [
            {"if_version": 1},
            {"if_sha256": saved["sha256"]},
            {"if_version": True, "if_sha256": saved["sha256"]},
            {"if_version": 0, "if_sha256": saved["sha256"]},
            {"if_version": 2**80, "if_sha256": saved["sha256"]},
            {"if_version": 1, "if_sha256": "A" * 64},
            {"if_version": 1, "if_sha256": ""},
            {"if_version": 1, "if_sha256": ["a" * 64]},
            {"version": 1, "if_version": 1, "if_sha256": saved["sha256"]},
        ]
        for kwargs in bad:
            with self.subTest(kwargs=kwargs), self.assertRaises(MemoryError):
                self.memory.read("control", **kwargs)
        with self.assertRaises(MemoryError):
            self.memory.read("missing", if_version=1, if_sha256=saved["sha256"])
        self.assertEqual(self.memory.read("control")["memory"], saved)

    def test_project_metadata_refreshes_even_when_content_matches(self):
        saved = self.saved()
        self.memory.register({"id": "control", "name": "Renamed", "root": "/new"})
        result = self.memory.read("control", if_version=1, if_sha256=saved["sha256"])
        self.assertTrue(result["not_modified"])
        self.assertEqual(result["project"]["name"], "Renamed")
        self.assertEqual(result["project"]["root"], "/new")

    def test_concurrent_revision_after_snapshot_is_seen_on_next_read(self):
        saved = self.saved()
        actual_db = self.store.db
        wrote = False

        class Interleave:
            def __init__(self, db):
                self.db = db

            def execute(inner, sql, args=()):
                nonlocal wrote
                if "SELECT project,version,sha256" in sql and not wrote:
                    wrote = True
                    with patch.object(self.store, "db", actual_db):
                        self.memory.save(
                            {**self.body, "request_id": "concurrent", "version": 1}, "leader"
                        )
                return inner.db.execute(sql, args)

        @contextmanager
        def interleaved_db():
            with actual_db() as db:
                yield Interleave(db)

        with patch.object(self.store, "db", interleaved_db):
            value = self.memory.read("control", if_version=1, if_sha256=saved["sha256"])
        self.assertTrue(wrote)
        self.assertTrue(value["not_modified"])
        self.assertEqual(value["current_version"], 1)
        next_read = self.memory.read("control", if_version=1, if_sha256=saved["sha256"])
        self.assertFalse(next_read["not_modified"])
        self.assertEqual(next_read["current_version"], 2)


class ConditionalHTTPTests(unittest.TestCase):
    setUp = station_fixture.StationTests.setUp
    close = station_fixture.StationTests.close

    def saved(self):
        join = self.station.ensure("native-one")
        self.sid = join["session_id"]
        body = {
            "project": "demo",
            "version": 0,
            "request_id": "memory-one",
            "session_id": self.sid,
            "sections": {k: "PRIVATE SYNTHETIC CONTENT " + k + ("界" * 300) for k in SECTIONS},
            "summary": "checkpoint",
        }
        self.body = body
        return self.station.client.call("memory", body)

    def test_real_http_match_reduces_body_and_preserves_cas_history(self):
        before = self.saved()
        client = self.station.client
        query = {"if_version": 1, "if_sha256": before["memory"]["sha256"]}
        match = client.call("memory", query=query)
        self.assertTrue(match["not_modified"])
        self.assertLess(len(json.dumps(match)), len(json.dumps(before)) / 3)
        self.assertNotIn("PRIVATE SYNTHETIC CONTENT", json.dumps(match))
        second = client.call("memory", {**self.body, "request_id": "next", "version": 1})
        changed = client.call("memory", query=query)
        self.assertFalse(changed["not_modified"])
        self.assertEqual(changed["memory"], second["memory"])
        self.assertEqual(changed["memory"]["sha256"], before["memory"]["sha256"])
        with self.assertRaises(station_fixture.adapter.CLIENT.ClientError) as err:
            client.call("memory", {**self.body, "request_id": "stale"})
        self.assertEqual(err.exception.code, "memory_version_conflict")
        self.assertEqual(client.call("memory", query={"version": 1})["memory"], before["memory"])
        self.assertEqual(len(client.call("memory", query={"history": 1})["revisions"]), 2)

    def test_real_owner_route_and_cli_mcp_openapi_expose_same_contract(self):
        before = self.saved()
        query = {"project": "demo", "if_version": 1, "if_sha256": before["memory"]["sha256"]}
        opener = build_opener(ProxyHandler({}))
        with opener.open(
            self.server.origin + "/api/project-memory?" + urlencode(query), timeout=5
        ) as r:
            self.assertEqual(r.status, 200)
            self.assertTrue(json.load(r)["not_modified"])
        result = station_fixture.adapter.CLIENT.invoke(self.station.client, "aieyra_memory", query)
        self.assertTrue(result["not_modified"])
        config = self.station.p["config_file"]
        process = subprocess.run(
            [
                sys.executable,
                str(station_fixture.ROOT / "scripts/agent-client.py"),
                "--config",
                config,
                "memory",
                "--project",
                "demo",
                "--if-version",
                "1",
                "--if-sha256",
                query["if_sha256"],
            ],
            capture_output=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertTrue(json.loads(process.stdout)["not_modified"])
        self.assertEqual(
            self.station.client.call("info")["project_memory"]["conditional_read"],
            "version_and_sha256",
        )
        with opener.open(self.server.origin + "/api/agent-openapi", timeout=5) as r:
            api = json.load(r)
        for route in ["/api/agent/v1/memory", "/api/project-memory"]:
            params = {p["name"] for p in api["paths"][route]["get"]["parameters"]}
            self.assertTrue({"if_version", "if_sha256"} <= params)

    def test_malformed_conditions_and_owner_combinations_rejected(self):
        before = self.saved()
        digest = before["memory"]["sha256"]
        q = {"if_version": 1, "if_sha256": digest}
        cases = [
            {"if_version": 1},
            {"if_sha256": digest},
            {**q, "if_version": 0},
            {**q, "if_version": "01"},
            {**q, "if_version": "+1"},
            {**q, "if_version": 2**80},
            {**q, "if_version": [1, 1]},
            {**q, "if_sha256": "A" * 64},
            {**q, "if_sha256": ""},
            {**q, "history": 1},
            {**q, "version": 1},
            {**q, "before": 1},
            {**q, "extra": 1},
        ]
        opener = build_opener(ProxyHandler({}))
        for query in cases:
            with self.subTest(query=query):
                with self.assertRaises(station_fixture.adapter.CLIENT.ClientError) as err:
                    self.station.client.call("memory", query=query)
                self.assertEqual(err.exception.status, 400)
                with self.assertRaises(HTTPError) as err:
                    opener.open(
                        self.server.origin
                        + "/api/project-memory?"
                        + urlencode({"project": "demo", **query}, doseq=True),
                        timeout=5,
                    )
                self.assertEqual(err.exception.code, 400)
        self.assertEqual(self.station.client.call("memory")["memory"], before["memory"])

    def test_project_and_revoked_identity_are_checked_before_matching_hash(self):
        before = self.saved()
        q = {"if_version": 1, "if_sha256": before["memory"]["sha256"]}
        with self.assertRaises(station_fixture.adapter.CLIENT.ClientError) as err:
            self.station.client.call("memory", query={"project": "other", **q})
        self.assertEqual(err.exception.code, "memory_project_denied")
        with self.app.store.db() as db:
            db.execute("UPDATE agent_credentials SET revoked=1")
        with self.assertRaises(station_fixture.adapter.CLIENT.ClientError) as err:
            self.station.client.call("memory", query=q)
        self.assertEqual(err.exception.status, 401)

    def test_real_finish_conditional_read_does_not_write_memory_or_ack_deliveries(self):
        before = self.saved()
        actual = self.station.client.call
        observations = []

        def call(route, body=None, query=None):
            result = actual(route, body, query)
            observations.append((route, body, query, result))
            return result

        with patch.object(self.station.client, "call", side_effect=call):
            result = self.station.finish("native-one")
        reads = [x for x in observations if x[0] == "memory"]
        self.assertEqual(len(reads), 2)
        self.assertIsNone(reads[0][2])
        self.assertTrue(reads[1][3]["not_modified"])
        self.assertNotIn("sections", reads[1][3]["memory"])
        self.assertTrue(result["lease_released"])
        self.assertTrue(result["memory"]["unchanged_during_finish"])
        self.assertEqual(result, self.station.state()["finish"])
        self.assertEqual(before["memory"], actual("memory")["memory"])
        self.assertFalse(
            any(route in ["receipt", "center/ack", "connect"] for route, _, _, _ in observations)
        )

    def test_real_finish_observes_new_revision_during_disconnect(self):
        before = self.saved()
        actual = self.station.client.call

        def call(route, body=None, query=None):
            response = actual(route, body, query)
            if route == "disconnect":
                body = {k: v for k, v in self.body.items() if k != "session_id"}
                self.app.project_memory.save(
                    {**body, "version": 1, "request_id": "concurrent-owner"}, "local-owner"
                )
            return response

        with patch.object(self.station.client, "call", side_effect=call):
            result = self.station.finish("native-one")
        self.assertTrue(result["lease_released"])
        self.assertFalse(result["memory"]["unchanged_during_finish"])
        self.assertEqual(result["memory"]["version"], 2)
        self.assertEqual(result["memory"]["sha256"], before["memory"]["sha256"])


class ConditionalClientTests(unittest.TestCase):
    def setUp(self):
        self.previous = {
            "memory": {
                "project": "demo",
                "version": 1,
                "sha256": "a" * 64,
                "sections": {k: "private" for k in SECTIONS},
            },
            "missing_sections": [],
        }
        self.match = {
            "memory": {"project": "demo", "version": 1, "sha256": "a" * 64},
            "current_version": 1,
            "not_modified": True,
            "conditional_read": "version_and_sha256",
        }

    def test_match_reuses_only_this_operations_memory_and_does_not_mutate_input(self):
        original = copy.deepcopy(self.previous)
        with patch.object(client.AgentClient, "call", return_value=self.match) as call:
            result = client.memory_readback(client.AgentClient({"token": "a" * 64}), self.previous)
        self.assertEqual(result["memory"]["sections"], self.previous["memory"]["sections"])
        self.assertEqual(self.previous, original)
        call.assert_called_once_with(
            "memory", query={"project": "demo", "if_version": 1, "if_sha256": "a" * 64}
        )

    def test_only_explicit_old_service_rejection_allows_one_fallback(self):
        for code, status, fallback in [
            ("invalid_memory_query", 400, True),
            ("invalid_memory_query", 403, False),
            ("agent_unauthorized", 401, False),
            ("connection_unconfirmed_query_original_request", 0, False),
            ("invalid_memory_condition", 400, False),
        ]:
            calls = []

            def call(route, body=None, query=None):
                calls.append(query)
                if query:
                    raise client.ClientError(code, status)
                return self.previous

            with self.subTest(code=code, status=status):
                if fallback:
                    self.assertEqual(
                        client.memory_readback(SimpleNamespace(call=call), self.previous),
                        self.previous,
                    )
                else:
                    with self.assertRaises(client.ClientError):
                        client.memory_readback(SimpleNamespace(call=call), self.previous)
                self.assertEqual(len(calls), 2 if fallback else 1)

    def test_malformed_not_modified_metadata_cannot_claim_unchanged(self):
        cases = [
            {**self.match, "not_modified": "true"},
            {**self.match, "current_version": 2},
            {**self.match, "conditional_read": "unknown"},
            {**self.match, "missing_sections": []},
            {**self.match, "memory": {**self.match["memory"], "project": "other"}},
            {**self.match, "memory": {**self.match["memory"], "sha256": "b" * 64}},
            {**self.match, "memory": {**self.match["memory"], "version": True}},
            {**self.match, "memory": {**self.match["memory"], "sections": {}}},
            {**self.match, "not_modified": False},
        ]
        for value in cases:
            calls = []

            def call(*args, **kwargs):
                calls.append(args)
                return value

            with self.subTest(value=value), self.assertRaises(client.ClientError):
                client.memory_readback(SimpleNamespace(call=call), self.previous)
            self.assertEqual(len(calls), 1)

    def test_initial_version_zero_keeps_full_read(self):
        with patch.object(client.AgentClient, "call", return_value=self.previous) as call:
            result = client.memory_readback(
                client.AgentClient({"token": "a" * 64}),
                {"memory": {"project": "demo", "version": 0, "sha256": None}},
            )
        self.assertEqual(result, self.previous)
        call.assert_called_once_with("memory")

    def test_changed_response_requires_complete_valid_metadata_and_sections(self):
        full = {
            **self.previous,
            "current_version": 1,
            "not_modified": False,
            "conditional_read": "version_and_sha256",
        }
        self.assertEqual(
            client.memory_readback(SimpleNamespace(call=lambda *a, **k: full), self.previous), full
        )
        for value in [
            None,
            [],
            {**full, "current_version": True},
            {**full, "memory": {**full["memory"], "sha256": None}},
            {**full, "memory": {**full["memory"], "version": -1}, "current_version": -1},
            {**full, "missing_sections": ["blueprint"]},
        ]:
            with self.subTest(value=value), self.assertRaises(client.ClientError):
                client.memory_readback(SimpleNamespace(call=lambda *a, **k: value), self.previous)

    def test_finish_does_not_persist_or_claim_success_after_conditional_error(self):
        calls = []

        def call(route, body=None, query=None):
            calls.append(route)
            if route == "memory":
                if query:
                    raise client.ClientError("connection_unconfirmed_query_original_request")
                return self.previous
            if route == "inbox":
                return {"deliveries": [{"id": "pending", "state": "queued"}]}
            if route == "disconnect":
                return {}
            return {
                "session": {
                    "state": "disconnected" if "disconnect" in calls else "connected",
                    "lease_until": 0,
                }
            }

        with self.assertRaises(client.ClientError):
            client.finish_session(SimpleNamespace(call=call), "one", "finish")
        self.assertEqual(
            calls, ["sessions/one", "memory", "inbox", "disconnect", "sessions/one", "memory"]
        )


if __name__ == "__main__":
    unittest.main()
