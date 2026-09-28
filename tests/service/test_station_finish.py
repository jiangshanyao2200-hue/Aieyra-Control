"""Finish receipt, disk consistency and failure semantics through isolated HTTP."""

import io
import json
import unittest
from unittest.mock import patch

import test_agent_station as fixtures

adapter = fixtures.adapter


class StationFinishTests(unittest.TestCase):
    setUp = fixtures.StationTests.setUp
    close = fixtures.StationTests.close

    def assert_receipt(self, result, sid, status):
        state = self.station.state()
        self.assertEqual(result, state["finish"])
        self.assertEqual(state["status"], status)
        self.assertEqual(result["status"], status)
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["operation"], "finish")
        self.assertEqual(result["project"], "demo")
        self.assertEqual(result["actor_id"], self.station.p["actor_id"])
        self.assertEqual(result["seat_id"], self.station.p["seat_id"])
        self.assertEqual(result["host"], "claude")
        self.assertEqual(result["native_session_id"], "native-one")
        self.assertEqual(result["transport_session_id"], sid)
        self.assertEqual(result["session_id"], sid)
        self.assertNotIn(self.token, json.dumps(state))
        self.assertFalse(result["memory_saved_by_finish"])
        self.assertFalse(result["tasks_completed_by_finish"])

    def test_http_finish_persists_exact_returned_receipt_and_keeps_memory(self):
        sid = self.station.ensure("native-one")["session_id"]
        before = self.station.client.call("memory")
        result = self.station.finish("native-one")
        self.assert_receipt(result, sid, "closed")
        self.assertEqual(before, self.station.client.call("memory"))
        self.assertTrue(result["memory"]["unchanged_during_finish"])
        self.assertEqual(result["communication_state"], "disconnected")
        self.assertEqual(result["current_execution_state"], "unknown")
        self.assertFalse((self.station.state_dir / (sid + ".active")).exists())

    def test_repeat_finish_reads_terminal_session_without_another_disconnect(self):
        sid = self.station.ensure("native-one")["session_id"]
        self.station.finish("native-one")
        actual = self.station.client.call
        with patch.object(self.station.client, "call", wraps=actual) as calls:
            result = self.station.finish("native-one")
        self.assert_receipt(result, sid, "closed")
        self.assertFalse(result["pending_deliveries_read"])
        self.assertNotIn("disconnect", [call.args[0] for call in calls.call_args_list])

    def test_lost_disconnect_response_reconciles_without_replay(self):
        sid = self.station.ensure("native-one")["session_id"]
        actual = self.station.client.call
        writes = []

        def lost(route, body=None, query=None):
            result = actual(route, body, query)
            if body is not None:
                writes.append(route)
            if route == "disconnect":
                raise adapter.Error("connection_unconfirmed_query_original_request")
            return result

        with patch.object(self.station.client, "call", side_effect=lost):
            result = self.station.finish("native-one")
        self.assert_receipt(result, sid, "closed")
        self.assertTrue(result["lease_released"])
        self.assertEqual(writes, ["disconnect"])
        self.assertEqual(
            result["disconnect_error"], "connection_unconfirmed_query_original_request"
        )

    def test_unconfirmed_disconnect_is_not_closed_and_retains_exact_receipt(self):
        sid = self.station.ensure("native-one")["session_id"]
        actual = self.station.client.call
        attempted = []

        def unavailable(route, body=None, query=None):
            if route == "disconnect":
                attempted.append(route)
                raise adapter.Error("connection_unconfirmed_query_original_request")
            return actual(route, body, query)

        with patch.object(self.station.client, "call", side_effect=unavailable):
            result = self.station.finish("native-one")
        self.assert_receipt(result, sid, "release_unconfirmed")
        self.assertFalse(result["lease_released"])
        self.assertEqual(result["communication_state"], "connected")
        self.assertEqual(attempted, ["disconnect"])
        self.assertEqual(actual("sessions/" + sid)["session"]["state"], "connected")

    def test_failed_post_disconnect_read_does_not_persist_success(self):
        sid = self.station.ensure("native-one")["session_id"]
        state_bytes = self.station.state_file.read_bytes()
        actual = self.station.client.call
        disconnected = False

        def failed(route, body=None, query=None):
            nonlocal disconnected
            if disconnected and route == "sessions/" + sid:
                raise adapter.Error("connection_unconfirmed_query_original_request")
            result = actual(route, body, query)
            if route == "disconnect":
                disconnected = True
            return result

        with patch.object(self.station.client, "call", side_effect=failed):
            with self.assertRaises(adapter.Error):
                self.station.finish("native-one")
        self.assertEqual(state_bytes, self.station.state_file.read_bytes())
        self.assertNotIn("finish", self.station.state())
        self.assertEqual(actual("sessions/" + sid)["session"]["state"], "disconnected")
        # A later explicit finish reads the same terminal handle, with no new write.
        with patch.object(self.station.client, "call", wraps=actual) as calls:
            result = self.station.finish("native-one")
        self.assert_receipt(result, sid, "closed")
        self.assertNotIn("disconnect", [call.args[0] for call in calls.call_args_list])

    def test_expired_transport_can_be_verified_without_reconnecting(self):
        sid = self.station.ensure("native-one")["session_id"]
        with self.app.store.db() as db:
            db.execute("UPDATE agent_sessions SET lease_until=0 WHERE id=?", (sid,))
        actual = self.station.client.call
        with patch.object(self.station.client, "call", wraps=actual) as calls:
            result = self.station.finish("native-one")
        self.assert_receipt(result, sid, "closed")
        self.assertEqual(result["communication_state"], "expired")
        self.assertFalse(result["pending_deliveries_read"])
        self.assertFalse(
            any(
                call.args[0] in ("connect", "disconnect", "heartbeat")
                for call in calls.call_args_list
            )
        )

    def test_foreign_native_cannot_change_owned_state_or_release_transport(self):
        sid = self.station.ensure("native-one")["session_id"]
        before = self.station.state_file.read_bytes()
        actual = self.station.client.call
        with patch.object(self.station.client, "call", wraps=actual) as calls:
            result = self.station.finish("native-two")
        self.assertEqual(result["status"], "no_owned_transport")
        self.assertIsNone(result["session_id"])
        self.assertIsNone(result["transport_session_id"])
        self.assertEqual(result["native_session_id"], "native-two")
        self.assertFalse(result["lease_released"])
        self.assertEqual(calls.call_count, 0)
        self.assertEqual(before, self.station.state_file.read_bytes())
        self.assertEqual(actual("sessions/" + sid)["session"]["state"], "connected")

    def test_no_transport_and_invalid_native_leave_no_state(self):
        result = self.station.finish("native-one")
        self.assertEqual(result["status"], "no_owned_transport")
        self.assertFalse(result["lease_released"])
        self.assertFalse(self.station.state_file.exists())
        for native in (None, "", "not a native", 5):
            with self.assertRaises(adapter.Error):
                self.station.finish(native)
        self.assertFalse(self.station.state_file.exists())

    def test_explicit_cli_exit_matches_release_and_stdout_equals_disk(self):
        self.station.ensure("native-one")
        output = io.StringIO()
        argv = [
            "station",
            "--profile",
            str(self.profile),
            "finish",
            "--native-session-id",
            "native-one",
        ]
        with patch.object(adapter.sys, "argv", argv), patch.object(adapter.sys, "stdout", output):
            self.assertEqual(adapter.main(), 0)
        self.assertEqual(json.loads(output.getvalue()), self.station.state()["finish"])
        for status in ("no_owned_transport", "release_unconfirmed"):
            output = io.StringIO()
            receipt = {"status": status, "lease_released": False}
            with (
                patch.object(adapter.sys, "argv", argv),
                patch.object(adapter.sys, "stdout", output),
                patch.object(adapter.Station, "finish", return_value=receipt),
            ):
                self.assertEqual(adapter.main(), 1)
            self.assertEqual(json.loads(output.getvalue()), receipt)

    def test_hooks_remain_nonblocking_and_store_the_same_success_receipt(self):
        sid = self.station.ensure("native-one")["session_id"]
        payload = {"hook_event_name": "Stop", "session_id": "native-one", "cwd": str(self.project)}
        result, output = self.station.hook(payload)
        self.assertEqual(output, {})
        self.assert_receipt(result, sid, "closed")
        self.assertEqual(
            adapter.read_json(self.station.state_dir / "last-hook.json")["result"], result
        )
        for failed in (False, True):
            stream = type("Input", (), {"buffer": io.BytesIO(json.dumps(payload).encode())})()
            out = io.StringIO()
            behavior = (
                {"side_effect": adapter.Error("readback_unavailable")}
                if failed
                else {"return_value": {"status": "release_unconfirmed", "lease_released": False}}
            )
            with (
                patch.object(
                    adapter.sys, "argv", ["station", "--profile", str(self.profile), "hook"]
                ),
                patch.object(adapter.sys, "stdin", stream),
                patch.object(adapter.sys, "stdout", out),
                patch.object(adapter.sys, "stderr", io.StringIO()),
                patch.object(adapter.Station, "finish", **behavior),
            ):
                self.assertEqual(adapter.main(), 0)
            self.assertEqual(json.loads(out.getvalue()), {})
