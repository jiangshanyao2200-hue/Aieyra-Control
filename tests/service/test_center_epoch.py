"""Persistent, view-bound message cursors through real HTTP and station resume."""

import concurrent.futures
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import test_agent_station as station_fixture
import test_agent_access as legacy_fixture
import agent_protocol
import center_messages
from local_hub import LocalClient


class EpochTests(unittest.TestCase):
    setUp = station_fixture.StationTests.setUp
    close = station_fixture.StationTests.close

    def page(self, route="inbox", **query):
        return self.station.client.call("center/" + route, query={"project": "demo", **query})

    def message(self, project="demo"):
        with self.app.hub.client.store.db() as db:
            count = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        return self.app.hub.client.call(
            "owner",
            "/v1/message",
            {
                "request_id": f"epoch-message-{count}",
                "project": project,
                "kind": "progress",
                "body": "Isolated epoch test",
            },
        )

    def rejected(self, code, query, status=409):
        with self.assertRaises(station_fixture.adapter.Error) as caught:
            self.page(**query)
        self.assertEqual((caught.exception.code, caught.exception.status), (code, status))

    def test_empty_stream_then_append_and_history_share_epoch_without_ack(self):
        first = self.page()
        epoch = first["stream_epoch"]
        self.assertRegex(epoch, r"^[a-f0-9]{64}$")
        self.assertFalse(first["epoch_checked"])
        self.assertEqual(first["snapshot_cursor"], 0)
        one, two = self.message(), self.message()
        page = self.page(after=0, limit=1, stream_epoch=epoch)
        self.assertTrue(page["epoch_checked"])
        self.assertEqual(page["next_cursor"], one["seq"])
        self.assertTrue(page["has_more"])
        tail = self.page(after=page["next_cursor"], stream_epoch=epoch)
        self.assertEqual([r["seq"] for r in tail["messages"]], [two["seq"]])
        history = self.page("history", before=two["seq"], stream_epoch=epoch)
        self.assertEqual(history["stream_epoch"], epoch)
        self.assertEqual([r["seq"] for r in history["messages"]], [one["seq"]])
        self.assertTrue(all(r["read_at"] is None for r in history["messages"]))
        with self.app.hub.client.store.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0], 0)

    def test_same_database_reopen_and_concurrent_initialization_keep_identity(self):
        epoch = self.page()["stream_epoch"]
        data = self.app.hub.client.store.path.parent
        peer = self.app.agent_access.authenticate(self.token)
        value = center_messages.parse("inbox", {"project": ["demo"], "stream_epoch": [epoch]})

        def reopen(_):
            return center_messages.local_page(LocalClient(data, []), peer, "inbox", value)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            pages = list(pool.map(reopen, range(2)))
        self.assertTrue(all(p["epoch_checked"] and p["stream_epoch"] == epoch for p in pages))
        with self.app.hub.client.store.db() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM control_message_stream").fetchone()[0], 1
            )

    def test_independent_database_differs_but_intact_database_copy_preserves_identity(self):
        epoch = self.page()["stream_epoch"]
        source = self.app.hub.client.store
        peer = self.app.agent_access.authenticate(self.token)
        value = center_messages.parse("inbox", {"project": ["demo"]})
        with tempfile.TemporaryDirectory() as tmp:
            copy_dir, fresh_dir = Path(tmp) / "copy", Path(tmp) / "fresh"
            copy_dir.mkdir()
            fresh_dir.mkdir()
            with source.db() as old, closing(sqlite3.connect(copy_dir / "office.sqlite")) as new:
                old.backup(new)
            copied = center_messages.local_page(LocalClient(copy_dir, []), peer, "inbox", value)
            self.assertEqual(copied["stream_epoch"], epoch)
            fresh = LocalClient(fresh_dir, [])
            with source.db() as old, fresh.store.db() as new:
                self.assertNotEqual(
                    old.execute("SELECT epoch FROM control_message_stream").fetchone()[0],
                    new.execute("SELECT epoch FROM control_message_stream").fetchone()[0],
                )

    def test_migration_of_existing_database_keeps_messages_and_creates_epoch_once(self):
        message = self.message()
        store = self.app.hub.client.store
        with store.db() as db:
            before = db.execute("SELECT * FROM messages ORDER BY seq").fetchall()
            db.execute("DROP TABLE control_message_stream")
        peer = self.app.agent_access.authenticate(self.token)
        value = center_messages.parse("inbox", {"project": ["demo"]})
        migrated = LocalClient(store.path.parent, [])
        page = center_messages.local_page(migrated, peer, "inbox", value)
        self.assertEqual([r["id"] for r in page["messages"]], [message["id"]])
        with store.db() as db:
            self.assertEqual(db.execute("SELECT * FROM messages ORDER BY seq").fetchall(), before)
        reopened = center_messages.local_page(
            LocalClient(store.path.parent, []), peer, "inbox", value
        )
        self.assertEqual(page["stream_epoch"], reopened["stream_epoch"])

    def test_epoch_binds_project_selection_but_not_limit_or_direction(self):
        epoch = self.page(limit=1)["stream_epoch"]
        self.assertEqual(self.page("history", limit=3)["stream_epoch"], epoch)
        self.rejected(
            "center_stream_epoch_mismatch", {"stream_epoch": epoch, "include_coordination": 1}
        )
        self.rejected(
            "center_stream_epoch_mismatch", {"stream_epoch": epoch, "project": "coordination"}
        )
        coor = self.page(project="coordination")["stream_epoch"]
        # These two filters select precisely the same view.
        self.assertEqual(
            self.page(project="coordination", include_coordination=1)["stream_epoch"], coor
        )

    def test_wrong_epoch_fails_before_rows_without_implicit_reset(self):
        self.message()
        with patch.object(
            self.app.hub.client.store, "messages", side_effect=AssertionError("must not read rows")
        ):
            self.rejected("center_stream_epoch_mismatch", {"after": 0, "stream_epoch": "0" * 64})

    def test_checked_ahead_cursor_fails_and_old_unbound_behavior_stays_compatible(self):
        self.message()
        epoch = self.page()["stream_epoch"]
        self.rejected("center_cursor_ahead_of_stream", {"after": 20, "stream_epoch": epoch})
        unbound = self.page(after=20)
        self.assertEqual(unbound["next_cursor"], 20)
        self.assertFalse(unbound["epoch_checked"])

    def test_epoch_head_and_rows_use_same_snapshot_during_concurrent_change(self):
        one = self.message()
        epoch = self.page()["stream_epoch"]
        store = self.app.hub.client.store
        original = store.messages

        def insert(db, actor, where, args, descending, limit):
            with store.db() as writer:
                writer.execute("UPDATE control_message_stream SET epoch=? WHERE id=1", ("f" * 64,))
            self.message()
            return original(db, actor, where, args, descending, limit)

        with patch.object(store, "messages", side_effect=insert):
            page = self.page(stream_epoch=epoch)
        self.assertEqual(page["stream_epoch"], epoch)
        self.assertEqual(page["snapshot_cursor"], one["seq"])
        self.assertEqual([r["seq"] for r in page["messages"]], [one["seq"]])
        self.rejected(
            "center_stream_epoch_mismatch", {"after": page["next_cursor"], "stream_epoch": epoch}
        )

    def test_checked_read_is_read_only_and_revocation_precedes_epoch(self):
        epoch = self.page()["stream_epoch"]
        peer = self.app.agent_access.authenticate(self.token)
        store = self.app.hub.client.store
        original = store.db
        statements = []

        def observed():
            db = original()
            db.set_trace_callback(statements.append)
            return db

        value = center_messages.parse("inbox", {"project": ["demo"], "stream_epoch": [epoch]})
        with patch.object(store, "db", side_effect=observed):
            center_messages.local_page(self.app.hub.client, peer, "inbox", value)
        self.assertFalse(
            any(
                s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE"))
                for s in statements
            )
        )
        with store.db() as db:
            db.execute("UPDATE actors SET revoked=1 WHERE id=?", (peer["actor_id"],))
        self.rejected("unauthorized", {"stream_epoch": "0" * 64}, 401)

    def test_malformed_epoch_unknown_project_and_missing_project_are_rejected(self):
        for value in ("", "A" * 64, "a" * 63, "a" * 65, "z" * 64, True, 7):
            with self.subTest(value=value):
                self.rejected("invalid_center_stream_epoch", {"stream_epoch": value}, 400)
        self.rejected("unknown_project", {"project": "missing", "stream_epoch": "0" * 64}, 400)
        with self.assertRaises(station_fixture.adapter.Error) as caught:
            self.station.client.call("center/inbox", query={"stream_epoch": "0" * 64})
        self.assertEqual(caught.exception.code, "invalid_center_filter_query")

    def test_resume_transmits_checked_epoch_and_does_not_save_cursor_or_ack(self):
        self.message()
        epoch = self.page(include_coordination=1)["stream_epoch"]
        result = self.station.ensure("native-one", resume=True, stream_epoch=epoch)
        self.assertTrue(result["resume"]["coordination"]["epoch_checked"])
        self.assertEqual(result["resume"]["coordination"]["stream_epoch"], epoch)
        self.assertFalse(result["resume"]["cursor_persisted"])
        state = self.station.state_file.read_text()
        self.assertNotIn(epoch, state)
        self.assertNotIn("cursor", state)
        with self.app.hub.client.store.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0], 0)

    def test_resume_wrong_epoch_reports_unavailable_and_keeps_established_connection(self):
        result = self.station.ensure("native-one", resume=True, stream_epoch="0" * 64)
        self.assertEqual(result["status"], "connected")
        self.assertEqual(
            result["coordination"],
            {"status": "unavailable", "code": "center_stream_epoch_mismatch"},
        )
        self.assertIsNone(result["resume"]["coordination"])
        self.assertFalse(result["tasks_replayed"])

    def test_resume_does_not_accept_ignored_epoch_or_fallback_after_errors(self):
        original = self.station.client.call
        epoch = self.page(include_coordination=1)["stream_epoch"]
        for mode in ("ignored", "wrong", "unsupported", "auth", "transport"):
            calls = []

            def old(route, body=None, query=None):
                if route != "center/inbox":
                    return original(route, body, query)
                calls.append(query)
                if mode in ("ignored", "wrong"):
                    result = original(route, body, query)
                    result.pop("epoch_checked")
                    if mode == "wrong":
                        result["epoch_checked"] = True
                        result["stream_epoch"] = "0" * 64
                    return result
                code, status = {
                    "unsupported": ("unsupported_query_parameter", 400),
                    "auth": ("unauthorized", 401),
                    "transport": ("connection_unconfirmed_query_original_request", 0),
                }[mode]
                raise station_fixture.adapter.Error(code, status)

            with (
                self.subTest(mode=mode),
                patch.object(self.station.client, "call", side_effect=old),
            ):
                result = self.station.ensure("native-one", resume=True, stream_epoch=epoch)
            self.assertEqual(len(calls), 1)
            self.assertEqual(result["coordination"]["status"], "unavailable")
            self.assertIsNone(result["resume"]["coordination"])
            self.assertNotEqual(result["coordination"]["code"], "")

    def test_invalid_resume_epoch_fails_before_join_network_or_state(self):
        for kwargs in (
            {"stream_epoch": "0" * 64},
            {"resume": True, "stream_epoch": "bad"},
            {"resume": True, "stream_epoch": True},
        ):
            with patch.object(
                self.station.client, "call", side_effect=AssertionError("no network")
            ):
                with self.assertRaises(station_fixture.adapter.Error) as caught:
                    self.station.ensure("native-one", **kwargs)
            self.assertEqual(caught.exception.code, "invalid_resume_stream_epoch")
        self.assertFalse(self.station.state_file.exists())

    def test_cli_mcp_and_openapi_expose_the_same_checked_contract(self):
        epoch = self.page(include_coordination=1)["stream_epoch"]
        command = [
            sys.executable,
            "-X",
            "utf8",
            str(station_fixture.ROOT / "scripts/agent-station.py"),
            "--profile",
            str(self.profile),
            "join",
            "--native-session-id",
            "native-one",
            "--resume",
            "--stream-epoch",
            epoch,
        ]
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf8",
            timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(json.loads(proc.stdout)["resume"]["coordination"]["epoch_checked"])
        page = station_fixture.adapter.CLIENT.invoke(
            self.station.client,
            "aieyra_center",
            {
                "route": "inbox",
                "query": {"project": "demo", "include_coordination": 1, "stream_epoch": epoch},
            },
        )
        self.assertTrue(page["epoch_checked"])
        paths = agent_protocol.openapi()["paths"]
        for route in ("inbox", "history"):
            names = {p["name"] for p in paths["/api/agent/v1/center/" + route]["get"]["parameters"]}
            self.assertIn("stream_epoch", names)
        self.assertEqual(
            self.station.client.call("info")["center_stream"]["epoch"],
            "persistent_database_and_view",
        )


class LegacyEpochTests(unittest.TestCase):
    setUp = legacy_fixture.AgentAccessTests.setUp
    stop_center = legacy_fixture.AgentAccessTests.stop_center
    stop_server = legacy_fixture.AgentAccessTests.stop_server
    owner = legacy_fixture.AgentAccessTests.owner

    def test_legacy_reports_unavailable_and_never_reads_messages_with_expected_epoch(self):
        for route in ("inbox", "history"):
            page = self.client.call("center/" + route, query={"project": "os"})
            self.assertIsNone(page["stream_epoch"])
            self.assertFalse(page["epoch_checked"])
            with patch.object(
                self.app.agent_access, "remote", wraps=self.app.agent_access.remote
            ) as calls:
                with self.assertRaises(legacy_fixture.client_module.ClientError) as caught:
                    self.client.call(
                        "center/" + route, query={"project": "os", "stream_epoch": "0" * 64}
                    )
            self.assertEqual(
                (caught.exception.code, caught.exception.status),
                ("center_stream_epoch_unavailable", 501),
            )
            self.assertEqual([c.args[1] for c in calls.call_args_list], ["registry"])


if __name__ == "__main__":
    unittest.main()
