"""Malformed peers and tool calls must not terminate the stdio bridge."""

import importlib.util
import io
import json
from pathlib import Path
import threading
import tempfile
import time
import os
from unittest.mock import patch
from types import SimpleNamespace
import unittest
from http.client import IncompleteRead

spec = importlib.util.spec_from_file_location(
    "audited_agent_client", Path(__file__).resolve().parents[2] / "scripts/agent-client.py"
)
client_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client_module)


class AgentClientTests(unittest.TestCase):
    def client(self):
        return client_module.AgentClient({"token": "f" * 64})

    def test_invalid_config_and_argument_shapes_fail_without_network(self):
        for config in ([], None, {"url": 1}, {"url": "http://127.0.0.1:bad", "token": "f" * 64}):
            with self.subTest(config=config), self.assertRaises(client_module.ClientError):
                client_module.AgentClient(config)
        fake = SimpleNamespace(call=lambda *args, **kw: self.fail("invalid input reached network"))
        cases = [
            ("aieyra_lookup", {"kind": "center/status", "id": "x"}),
            ("aieyra_center", {"route": "message", "body": []}),
            (
                "aieyra_connect",
                {"request_id": "a", "session_id": "b", "seat_id": "c", "seat_epoch": True},
            ),
            (
                "aieyra_heartbeat",
                {"request_id": "a", "session_id": "b", "runtime_state": "invalid"},
            ),
        ]
        for name, args in cases:
            with self.subTest(name=name), self.assertRaises(client_module.ClientError):
                client_module.invoke(fake, name, args)

    def test_invalid_and_truncated_responses_are_sanitized_client_errors(self):
        for raw in (b"not-json", b"[]", b"null", b'{"value":NaN}'):
            with self.subTest(raw=raw):
                client = self.client()
                client.opener = SimpleNamespace(open=lambda *a, **kw: io.BytesIO(raw))
                with self.assertRaises(client_module.ClientError):
                    client.call("info")
        client = self.client()

        def incomplete(*a, **kw):
            raise IncompleteRead(b"private partial data")

        client.opener = SimpleNamespace(open=incomplete)
        with self.assertRaises(client_module.ClientError) as caught:
            client.call("info")
        self.assertNotIn("private", caught.exception.code)

    def test_keepalive_never_writes_a_cached_runtime_state(self):
        client = self.client()
        client.keepalive_interval = 0.001
        client._sessions.add("session-one")
        calls = []

        def call(route, body=None):
            calls.append((route, body))
            if route == "heartbeat":
                client._stop.set()
            return {"session": {"state": "connected", "runtime_state": "idle"}}

        client.call = call
        thread = threading.Thread(target=client._keepalive)
        thread.start()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual([r for r, _ in calls], ["heartbeat"])
        self.assertNotIn("runtime_state", calls[-1][1])

    def test_idle_mcp_process_does_not_keep_station_online_forever(self):
        client = self.client()
        client.keepalive_interval = 0.001
        client._sessions.add("session-one")
        client._activity["session-one"] = (0, 0)
        calls = []

        def call(route, body=None):
            calls.append((route, body))
            client._stop.set()
            return {"session": {"state": "disconnected"}}

        client.call = call
        thread = threading.Thread(target=client._keepalive)
        thread.start()
        thread.join(1)
        self.assertEqual([r for r, _ in calls], ["disconnect"])
        self.assertFalse(client._sessions)

    def test_response_limit_declared_length_and_invalid_session(self):
        for raw, declared in ((b"{}", "9"), (b" " * (8 * 1024 * 1024 + 1), None)):
            with self.subTest(declared=declared):
                stream = io.BytesIO(raw)
                stream.headers = {} if declared is None else {"Content-Length": declared}
                client = self.client()
                client.opener = SimpleNamespace(open=lambda *a, **kw: stream)
                with self.assertRaises(client_module.ClientError):
                    client.call("info")
        for session in (None, [], {"id": "wrong/path", "state": "connected"}):
            with self.subTest(session=session):
                client = self.client()
                client.managed_keepalive = True
                client.opener = SimpleNamespace(
                    open=lambda *a, **kw: io.BytesIO(json.dumps({"session": session}).encode())
                )
                with self.assertRaises(client_module.ClientError):
                    client.call("connect", {})
                self.assertIsNone(client._thread)

    def test_malformed_tool_calls_keep_stdio_alive(self):
        client = self.client()
        client.call = lambda *args, **kw: self.fail("invalid tool arguments reached HTTP")
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "aieyra_center", "arguments": {"route": "message", "body": []}},
            },
            {"jsonrpc": "2.0", "id": 3, "method": "ping"},
        ]
        output = io.StringIO()
        client_module.mcp(client, io.StringIO("\n".join(map(json.dumps, messages))), output)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertTrue(responses[1]["result"]["isError"])
        self.assertEqual(responses[-1], {"jsonrpc": "2.0", "id": 3, "result": {}})

    def test_bounded_lease_preserves_state_and_releases_after_activity_disappears(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "active"
            marker.touch()
            calls = []
            stop = threading.Event()

            def call(route, body=None):
                calls.append((route, body))
                if route == "heartbeat":
                    self.assertNotIn("runtime_state", body)
                    marker.unlink()
                if route.startswith("sessions/"):
                    return {
                        "session": {
                            "state": "disconnected"
                            if any(r == "disconnect" for r, _ in calls)
                            else "connected",
                            "lease_until": 0,
                        }
                    }
                return {}

            with patch.object(stop, "wait", return_value=False):
                result = client_module.keep_lease(
                    SimpleNamespace(call=call), "one", marker, stop=stop
                )
            self.assertEqual(result["reason"], "activity_file_missing")
            self.assertEqual(result["heartbeats"], 1)
            self.assertTrue(result["lease_released"])
            self.assertEqual(sum(r == "disconnect" for r, _ in calls), 1)
            self.assertFalse(any(r == "connect" for r, _ in calls))

    def test_lease_stale_marker_and_invalid_bounds_do_not_touch_session(self):
        fake = SimpleNamespace(call=lambda *a, **k: self.fail("invalid activity reached service"))
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "active"
            marker.touch()
            os.utime(marker, (time.time() - 601, time.time() - 601))
            for kwargs in ({}, {"max_seconds": 0}, {"max_seconds": 14401}, {"idle_seconds": 601}):
                with self.assertRaises(client_module.ClientError):
                    client_module.keep_lease(fake, "one", marker, **kwargs)

    def test_lease_maximum_and_renewal_failure_do_not_reconnect(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "active"
            marker.touch()
            for failure in (False, True):
                calls = []

                def call(route, body=None):
                    calls.append(route)
                    if route == "heartbeat" and failure:
                        raise client_module.ClientError("agent_session_expired", 409)
                    return {
                        "session": {
                            "state": "disconnected" if "disconnect" in calls else "connected",
                            "lease_until": 0,
                        }
                    }

                with (
                    patch.object(client_module.time, "monotonic", side_effect=[0, 0, 30, 30]),
                    patch.object(threading.Event, "wait", return_value=False),
                ):
                    result = client_module.keep_lease(
                        SimpleNamespace(call=call), "one", marker, max_seconds=30
                    )
                self.assertEqual(
                    result["reason"], "renewal_unconfirmed" if failure else "maximum_duration"
                )
                self.assertTrue(result["lease_released"])
                self.assertNotIn("connect", calls)

    def test_lease_reconciles_lost_disconnect_reply_without_reconnect(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "active"
            marker.touch()
            calls = []

            def call(route, body=None):
                calls.append(route)
                if route == "disconnect":
                    raise client_module.ClientError("connection_unconfirmed_query_original_request")
                return {
                    "session": {
                        "state": "disconnected" if "disconnect" in calls else "connected",
                        "lease_until": 0,
                    }
                }

            stop = threading.Event()
            stop.set()
            result = client_module.keep_lease(SimpleNamespace(call=call), "one", marker, stop=stop)
            self.assertTrue(result["lease_released"])
            self.assertEqual(result["error"], "connection_unconfirmed_query_original_request")
            self.assertNotIn("connect", calls)

    def test_finish_reads_memory_and_pending_before_disconnect_without_writing_either(self):
        calls = []
        memory = {
            "memory": {"project": "demo", "version": 7, "sha256": "a" * 64},
            "missing_sections": [],
        }

        def call(route, body=None):
            calls.append(route)
            if route == "memory":
                return memory
            if route == "inbox":
                return {"deliveries": [{"id": "d1", "state": "running", "body": "private text"}]}
            if route.startswith("sessions/"):
                return {
                    "session": {
                        "state": "disconnected" if "disconnect" in calls else "connected",
                        "runtime_state": "running",
                        "lease_until": 0,
                    }
                }
            return {}

        # Adapt the minimal fake to accept the read query too.
        fake = SimpleNamespace(call=lambda route, body=None, query=None: call(route, body))
        result = client_module.finish_session(fake, "one", "finish-one")
        self.assertTrue(result["lease_released"])
        self.assertEqual(result["current_execution_state"], "unknown")
        self.assertEqual(
            result["unconfirmed_before_disconnect"], [{"id": "d1", "state": "running"}]
        )
        self.assertNotIn("private text", json.dumps(result))
        self.assertLess(calls.index("inbox"), calls.index("disconnect"))
        self.assertFalse(result["memory_saved_by_finish"])

    def test_finish_does_not_disconnect_if_presave_read_fails(self):
        calls = []

        def call(route, body=None):
            calls.append(route)
            if route == "memory":
                raise client_module.ClientError("memory_unavailable")
            return {"session": {"state": "connected"}}

        with self.assertRaises(client_module.ClientError):
            client_module.finish_session(SimpleNamespace(call=call), "one", "finish-one")
        self.assertNotIn("disconnect", calls)

    def test_finish_reconciles_lost_reply_and_preserves_failed_release(self):
        for released in (True, False):
            calls = []
            memory = {
                "memory": {"project": "demo", "version": 7, "sha256": "a" * 64},
                "missing_sections": [],
            }

            def call(route, body=None, query=None):
                calls.append(route)
                if route == "memory":
                    return memory
                if route == "inbox":
                    return {"deliveries": [{"id": "pending", "state": "queued"}]}
                if route == "disconnect":
                    raise client_module.ClientError("connection_unconfirmed_query_original_request")
                return {
                    "session": {
                        "state": "disconnected"
                        if released and "disconnect" in calls
                        else "connected",
                        "lease_until": 0 if released and "disconnect" in calls else 99,
                    }
                }

            result = client_module.finish_session(SimpleNamespace(call=call), "one", "finish-one")
            self.assertEqual(result["lease_released"], released)
            self.assertEqual(
                result["disconnect_error"], "connection_unconfirmed_query_original_request"
            )
            self.assertEqual(
                result["unconfirmed_before_disconnect"], [{"id": "pending", "state": "queued"}]
            )
            self.assertEqual(
                calls, ["sessions/one", "memory", "inbox", "disconnect", "sessions/one", "memory"]
            )

    def test_finish_detects_new_revision_with_identical_content(self):
        reads = []

        def call(route, body=None, query=None):
            if route == "memory":
                reads.append(route)
                return {
                    "memory": {"project": "demo", "version": len(reads), "sha256": "a" * 64},
                    "missing_sections": [],
                }
            return {"session": {"state": "disconnected", "lease_until": 0}}

        result = client_module.finish_session(SimpleNamespace(call=call), "one", "finish-one")
        self.assertFalse(result["memory"]["unchanged_during_finish"])

    def test_finish_cli_returns_failure_when_release_is_unconfirmed(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "agent.json"
            config.write_text(json.dumps({"token": "f" * 64}), encoding="utf-8")
            for released in (True, False):
                output = io.StringIO()
                with (
                    patch.object(
                        client_module.sys,
                        "argv",
                        [
                            "agent-client",
                            "--config",
                            str(config),
                            "finish",
                            "--session-id",
                            "one",
                            "--request-id",
                            "finish-one",
                        ],
                    ),
                    patch.object(
                        client_module, "finish_session", return_value={"lease_released": released}
                    ),
                    patch.object(client_module.sys, "stdout", output),
                    patch.object(client_module.sys, "stderr", io.StringIO()),
                ):
                    self.assertEqual(client_module.main(), 0 if released else 1)
                self.assertEqual(json.loads(output.getvalue())["lease_released"], released)


if __name__ == "__main__":
    unittest.main()
