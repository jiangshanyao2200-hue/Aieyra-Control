"""Project pagination through real HTTP, local snapshots and a legacy center."""

import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_agent_station as station_fixture
import test_agent_access as legacy_fixture
import center_messages
import agent_protocol
from local_hub import LocalClient


class LocalFilterTests(unittest.TestCase):
    setUp = station_fixture.StationTests.setUp
    close = station_fixture.StationTests.close

    def message(self, project):
        counter = getattr(self, "counter", 0) + 1
        self.counter = counter
        return self.app.hub.client.call(
            "owner",
            "/v1/message",
            {
                "request_id": "filter-message-" + str(counter),
                "project": project,
                "kind": "progress",
                "body": "PRIVATE filter fixture " + str(counter),
            },
        )

    def page(self, route="inbox", **query):
        return self.station.client.call("center/" + route, query={"project": "demo", **query})

    def test_dense_foreign_pages_and_forward_cursor_do_not_lose_matching_messages(self):
        for _ in range(104):
            self.message("coordination")
        expected = [self.message("demo")["seq"] for _ in range(5)]
        tail = self.message("coordination")["seq"]
        cursor, collected = 0, []
        for _ in range(3):
            page = self.page(after=cursor, limit=2)
            self.assertEqual(page["filter_mode"], "server")
            self.assertLessEqual(len(page["messages"]), 2)
            collected += [r["seq"] for r in page["messages"]]
            cursor = page["next_cursor"]
        self.assertEqual(collected, expected)
        self.assertFalse(page["has_more"])
        self.assertEqual(cursor, tail)
        empty = self.page(after=cursor, limit=2)
        self.assertEqual(empty["messages"], [])
        self.assertEqual(empty["next_cursor"], cursor)
        fresh = self.message("demo")
        self.assertEqual([r["seq"] for r in self.page(after=cursor)["messages"]], [fresh["seq"]])
        self.assertTrue(
            all(r["read_at"] is None and r["receipt_count"] == 0 for r in self.page()["messages"])
        )

    def test_include_coordination_both_orders_and_concurrent_history_insert(self):
        expected = [
            self.message(p)["seq"] for p in ("demo", "coordination", "demo", "coordination")
        ]
        first = self.page("history", include_coordination=1, limit=2)
        self.assertEqual([r["seq"] for r in first["messages"]], expected[::-1][:2])
        self.message("demo")
        second = self.page("history", include_coordination=1, before=first["next_before"], limit=2)
        self.assertEqual([r["seq"] for r in second["messages"]], expected[::-1][2:])
        self.assertFalse(second["has_more"])
        self.assertIsNone(second["next_before"])
        same = self.page(project="coordination", include_coordination=1)
        self.assertEqual([r["seq"] for r in same["messages"]], [expected[1], expected[3]])
        self.assertEqual(self.page("history", before=1)["messages"], [])

    def test_snapshot_head_does_not_skip_insert_during_page_read(self):
        first = self.message("demo")
        tail = self.message("coordination")
        store = self.app.hub.client.store
        original = store.messages
        inserted = []

        def concurrent(*args, **kwargs):
            inserted.append(self.message("demo"))
            return original(*args, **kwargs)

        with patch.object(store, "messages", side_effect=concurrent):
            page = self.page()
        self.assertEqual([r["seq"] for r in page["messages"]], [first["seq"]])
        self.assertEqual(page["snapshot_cursor"], tail["seq"])
        self.assertEqual(page["next_cursor"], tail["seq"])
        after = self.page(after=page["next_cursor"])
        self.assertEqual([r["seq"] for r in after["messages"]], [inserted[0]["seq"]])

    def test_empty_tail_and_ahead_cursor_never_rewind(self):
        self.message("coordination")
        self.assertEqual(self.page()["next_cursor"], 1)
        self.assertEqual(self.page(after=20)["next_cursor"], 20)
        self.assertEqual(self.page(after=2**63 - 1)["next_cursor"], 2**63 - 1)

    def test_invalid_and_duplicate_queries_unknown_project_and_unfiltered_compatibility(self):
        bad = [
            {"project": ""},
            {"project": "unknown"},
            {"project": "bad/path"},
            {"limit": 0},
            {"limit": 101},
            {"limit": True},
            {"after": -1},
            {"after": 2**63},
            {"after": "1.0"},
            {"include_coordination": "true"},
            {"before": 2},
            {"unexpected": "x"},
            {"limit": [1, 2]},
        ]
        for query in bad:
            with self.subTest(query=query), self.assertRaises(station_fixture.adapter.Error):
                self.page(**query)
        with self.assertRaises(station_fixture.adapter.Error):
            self.station.client.call("center/inbox", query={"include_coordination": 1})
        with self.assertRaises(station_fixture.adapter.Error):
            self.page("history", before=0)
        foreign = self.message("coordination")
        own = self.message("demo")
        global_page = self.station.client.call("center/inbox", query={"limit": 2})
        self.assertEqual([r["seq"] for r in global_page["messages"]], [foreign["seq"], own["seq"]])
        self.assertNotIn("filter_mode", global_page)
        self.assertEqual(self.page()["limit"], 50)
        self.assertEqual(self.page("history")["limit"], 20)

    def test_persisted_database_and_enrolled_actor_revocation(self):
        self.message("demo")
        client = LocalClient(self.root / "data", [])
        peer = self.app.agent_access.authenticate(self.token)
        value = center_messages.parse("inbox", {"project": ["demo"]})
        page = center_messages.local_page(client, peer, "inbox", value)
        self.assertEqual(len(page["messages"]), 1)
        with client.store.db() as db:
            db.execute("UPDATE actors SET revoked=1 WHERE id=?", (peer["actor_id"],))
        with self.assertRaises(station_fixture.adapter.Error) as caught:
            self.page()
        self.assertEqual(caught.exception.status, 401)
        wrong = {**peer, "actor_id": "owner"}
        with self.assertRaises(center_messages.PageError):
            center_messages.local_page(client, wrong, "inbox", value)

    def test_real_cli_mcp_and_openapi_contract(self):
        self.message("coordination")
        wanted = self.message("demo")
        client = self.station.client
        reply = station_fixture.adapter.CLIENT.invoke(
            client, "aieyra_center", {"route": "inbox", "query": {"project": "demo", "limit": 1}}
        )
        self.assertEqual([r["seq"] for r in reply["messages"]], [wanted["seq"]])
        proc = subprocess.run(
            [
                sys.executable,
                "-X",
                "utf8",
                str(station_fixture.ROOT / "scripts/agent-client.py"),
                "--config",
                str(self.root / "private.json"),
                "call",
                "center/history",
                "--query",
                '{"project":"demo","limit":1}',
            ],
            capture_output=True,
            text=True,
            encoding="utf8",
            timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["messages"][0]["seq"], wanted["seq"])
        paths = agent_protocol.openapi()["paths"]
        for route in ("inbox", "history"):
            operation = paths["/api/agent/v1/center/" + route]["get"]
            self.assertTrue(
                {"project", "include_coordination"} <= {p["name"] for p in operation["parameters"]}
            )
            self.assertIn("legacy_scan", operation["x-query-contract"])

    def test_explicit_resume_filters_before_limit_without_ack_or_persistence(self):
        self.app.hub.client.call(
            "owner",
            "/v1/project-register",
            {
                "id": "other",
                "name": "Other",
                "root": "",
                "source": "",
                "version": 0,
                "request_id": "other-project",
            },
        )
        for _ in range(4):
            self.message("other")
        expected = [self.message(p)["seq"] for p in ("demo", "coordination", "demo")]
        with patch.object(self.station.client, "call", wraps=self.station.client.call) as calls:
            r = self.station.ensure("native-one", resume=True, limit=2)
        page = r["resume"]["coordination"]
        self.assertEqual([m["seq"] for m in page["messages"]], expected[:2])
        self.assertEqual(page["filter_mode"], "server")
        self.assertTrue(page["has_more"])
        self.assertEqual(sum(c.args[0] == "center/inbox" for c in calls.call_args_list), 1)
        self.assertFalse(any(c.args[0] == "center/ack" for c in calls.call_args_list))
        self.assertNotIn("PRIVATE", self.station.state_file.read_text())
        self.assertNotIn("cursor", self.station.state_file.read_text())

    def test_adapter_old_control_ignored_filter_keeps_global_cursor_and_scan_bound(self):
        original = self.station.client.call

        def old(route, body=None, query=None):
            if route == "center/inbox":
                return {
                    "messages": [{"id": "foreign", "project": "other", "seq": 12}],
                    "next_cursor": 12,
                }
            return original(route, body, query)

        with patch.object(self.station.client, "call", side_effect=old):
            page = self.station.ensure("native-one", resume=True, limit=1)["resume"]["coordination"]
        self.assertEqual(page["messages"], [])
        self.assertEqual(page["next_cursor"], 12)
        self.assertEqual(page["scanned_count"], 1)
        self.assertTrue(page["has_more"])
        self.assertEqual(page["filter_mode"], "legacy_scan")

    def test_adapter_only_known_unsupported_query_retries_once_not_transport_or_auth(self):
        original = self.station.client.call
        for code, status, expected_calls in [
            ("unsupported_query_parameter", 400, 2),
            ("unauthorized", 401, 1),
            ("connection_unconfirmed_query_original_request", 0, 1),
            ("invalid_center_filter_query", 400, 1),
        ]:
            count = []

            def old(route, body=None, query=None):
                if route == "center/inbox":
                    count.append(query)
                    if "project" in query:
                        raise station_fixture.adapter.Error(code, status)
                return original(route, body, query)

            with (
                self.subTest(code=code),
                patch.object(self.station.client, "call", side_effect=old),
            ):
                result = self.station.ensure("native-one", resume=True)
            self.assertEqual(len(count), expected_calls)
            self.assertEqual(
                result["coordination"]["status"],
                "observed" if expected_calls == 2 else "unavailable",
            )


class LegacyFilterTests(unittest.TestCase):
    setUp = legacy_fixture.AgentAccessTests.setUp
    stop_center = legacy_fixture.AgentAccessTests.stop_center
    stop_server = legacy_fixture.AgentAccessTests.stop_server
    owner = legacy_fixture.AgentAccessTests.owner

    def messages(self, projects):
        for i, project in enumerate(projects):
            self.hub.client.call(
                "owner",
                "/v1/message",
                {
                    "request_id": f"legacy-{i}",
                    "project": project,
                    "kind": "progress",
                    "body": "isolated legacy page",
                },
            )

    def test_one_legacy_inbox_page_empty_filter_advances_then_finds_matching(self):
        self.messages(["control", "control", "os", "coordination"])
        with patch.object(
            self.app.agent_access, "remote", wraps=self.app.agent_access.remote
        ) as calls:
            first = self.client.call("center/inbox", query={"project": "os", "limit": 2})
        self.assertEqual(first["messages"], [])
        self.assertEqual(first["filter_mode"], "legacy_scan")
        self.assertEqual(first["scanned_count"], 2)
        self.assertEqual(first["next_cursor"], 2)
        self.assertTrue(first["has_more"])
        self.assertIsNone(first["snapshot_cursor"])
        self.assertEqual([c.args[1] for c in calls.call_args_list], ["registry", "inbox"])
        following = self.client.call(
            "center/inbox",
            query={"project": "os", "include_coordination": 1, "limit": 2, "after": 2},
        )
        self.assertEqual([r["seq"] for r in following["messages"]], [3, 4])
        self.assertFalse(following["has_more"])

    def test_legacy_history_empty_page_preserves_unfiltered_before_cursor(self):
        self.messages(["os", "coordination", "control", "control"])
        first = self.client.call("center/history", query={"project": "os", "limit": 2})
        self.assertEqual(first["messages"], [])
        self.assertEqual(first["next_before"], 3)
        second = self.client.call(
            "center/history",
            query={"project": "os", "include_coordination": 1, "limit": 2, "before": 3},
        )
        self.assertEqual([r["seq"] for r in second["messages"]], [2, 1])
        self.assertFalse(second["has_more"])
        self.assertIsNone(second["next_before"])
        self.assertTrue(all(r["receipt_count"] == 0 for r in second["messages"]))

    def test_legacy_unknown_project_does_not_read_message_page(self):
        with patch.object(
            self.app.agent_access, "remote", wraps=self.app.agent_access.remote
        ) as calls:
            with self.assertRaises(legacy_fixture.client_module.ClientError) as caught:
                self.client.call("center/inbox", query={"project": "missing"})
        self.assertEqual(caught.exception.code, "unknown_project")
        self.assertEqual([c.args[1] for c in calls.call_args_list], ["registry"])


class ResponseValidationTests(unittest.TestCase):
    def test_full_legacy_inbox_boundary_is_conservative_and_next_page_terminates(self):
        value = center_messages.parse("inbox", {"project": ["demo"], "limit": ["100"]})
        rows = [{"seq": n, "project": "other"} for n in range(1, 101)]

        def remote(identity, route, query=None):
            if route == "registry":
                return {"projects": [{"id": "demo"}]}
            after = int(query["after"][0])
            return {"messages": rows if after == 0 else [], "next_cursor": 100}

        page = center_messages.legacy_page(remote, {"identity": "fixture"}, "inbox", value)
        self.assertEqual(page["messages"], [])
        self.assertEqual(page["scanned_count"], 100)
        self.assertTrue(page["has_more"])
        value["after"] = page["next_cursor"]
        end = center_messages.legacy_page(remote, {"identity": "fixture"}, "inbox", value)
        self.assertFalse(end["has_more"])
        self.assertEqual(end["next_cursor"], 100)

    def test_legacy_rejects_nonprogressing_wrong_order_and_malformed_pages(self):
        value = center_messages.parse("inbox", {"project": ["demo"]})
        for raw in [
            {"messages": [{"seq": True, "project": "demo"}], "next_cursor": 1},
            {
                "messages": [{"seq": 2, "project": "demo"}, {"seq": 1, "project": "demo"}],
                "next_cursor": 2,
            },
            {"messages": [], "next_cursor": -1},
            {"messages": [], "next_cursor": 0, "has_more": True},
            {"messages": [{"seq": 1, "project": "demo"}], "next_cursor": 0},
        ]:

            def remote(identity, route, query=None):
                return {"projects": [{"id": "demo"}]} if route == "registry" else raw

            with self.subTest(raw=raw), self.assertRaises(center_messages.PageError):
                center_messages.legacy_page(remote, {"identity": "fixture"}, "inbox", value)
