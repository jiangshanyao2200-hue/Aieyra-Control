"""Real loopback Control + isolated real center. No production DB, credentials or model."""

import concurrent.futures
import importlib.util
import io
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

from test_control import control

ROOT = Path(__file__).resolve().parents[2]
CENTER = (
    Path(os.environ["AIEYRA_CENTER_SOURCE"]) if os.environ.get("AIEYRA_CENTER_SOURCE") else None
)
spec = importlib.util.spec_from_file_location(
    "generic_agent_client", ROOT / "scripts/agent-client.py"
)
client_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client_module)


class FixtureClient:
    def __init__(self, url, owner):
        self.url, self.accounts = url, {"owner": {"token": owner}}
        self.opener = build_opener(ProxyHandler({}))

    def save(self, identity, result):
        self.accounts[identity] = dict(result)

    def load(self, identity):
        if identity not in self.accounts:
            raise FileNotFoundError()
        return dict(self.accounts[identity])

    def call(self, identity, path, body=None):
        headers = {"Authorization": "Bearer " + self.accounts[identity]["token"]}
        raw = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            raw = json.dumps(body).encode()
        try:
            with self.opener.open(
                Request(self.url + path, data=raw, headers=headers), timeout=5
            ) as r:
                return json.load(r)["data"]
        except HTTPError as e:
            with e:
                code = str(e.code) + ":" + json.load(e)["error"]
            raise ValueError(code) from None


class AgentAccessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        source = """import sys,json
sys.path.insert(0,sys.argv[1])
if sys.argv[3]=='vendored':
 from coordination_core import hub
else:
 import hub
store=hub.Store(sys.argv[2]);store.init(sys.stdin.readline().strip())
server=hub.Server(('127.0.0.1',0),store)
print(json.dumps({'port':server.server_address[1]}),flush=True)
server.serve_forever()
"""
        owner = secrets.token_urlsafe(32)
        self.center = subprocess.Popen(
            [
                sys.executable,
                "-c",
                source,
                str((CENTER / "service") if CENTER else ROOT / "service"),
                str(self.root / "center.sqlite"),
                "external" if CENTER else "vendored",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self.center.stdin.write(owner + "\n")
        self.center.stdin.flush()
        self.addCleanup(self.stop_center)
        line = self.center.stdout.readline()
        if not line:
            self.fail(self.center.stderr.read())
        center_url = "http://127.0.0.1:" + str(json.loads(line)["port"])
        self.hub = control.Hub.__new__(control.Hub)
        self.hub.config = {"hub_read_identity": "owner", "hub_user_identity": "owner"}
        self.hub.client = FixtureClient(center_url, owner)
        if not CENTER:
            for project in ("coordination", "os", "control"):
                self.hub.client.call(
                    "owner",
                    "/v1/project-register",
                    {
                        "request_id": "fixture-project-" + project,
                        "id": project,
                        "name": "Fixture " + project,
                        "root": "",
                        "source": "",
                        "version": 0,
                    },
                )
        self.app = control.Application(
            {"resources": [], "local_projects": [{"id": "os", "root": str(self.root)}]},
            self.root / "control",
            hub=self.hub,
        )
        self.server = control.Server(0, self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.opener = build_opener(ProxyHandler({}))
        self.token = secrets.token_urlsafe(32)
        self.enrollment = {
            "request_id": "fixture-enroll",
            "name": "Fixture generic agent",
            "project": "os",
            "token": self.token,
        }
        self.enrolled = self.owner("/api/agent-access/enroll", self.enrollment)
        self.client = client_module.AgentClient({"url": self.server.origin, "token": self.token})
        self.seat = self.client.call("seats")["seats"][0]
        self.sid = "session-one"

    def stop_center(self):
        self.center.terminate()
        self.center.wait(timeout=5)
        for stream in (self.center.stdin, self.center.stdout, self.center.stderr):
            stream.close()

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def owner(self, route, body=None, headers=None):
        values = {
            "Origin": self.server.origin,
            "X-Control-CSRF": self.app.csrf,
            "Content-Type": "application/json",
            **(headers or {}),
        }
        request = Request(
            self.server.origin + route,
            data=json.dumps(body).encode() if body is not None else None,
            headers=values,
        )
        try:
            with self.opener.open(request, timeout=8) as response:
                return json.load(response)
        except HTTPError as error:
            error.close()
            raise

    def connect(self, **changes):
        return self.client.call(
            "connect",
            {
                "request_id": "connect-one",
                "session_id": self.sid,
                "seat_id": self.seat["id"],
                "seat_epoch": self.seat["epoch"],
                **changes,
            },
        )

    def deliver(self):
        self.connect()
        value = self.owner(
            "/api/chat",
            {
                "request_id": "delivery-one",
                "target": self.enrolled["credential"]["actor_id"],
                "body": "Execute only this isolated fixture.",
            },
        )
        self.app.dispatch_one()
        rows = self.client.call("inbox", query={"session_id": self.sid})["deliveries"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "queued")
        self.assertEqual(value["delivery"]["target_thread_id"], "bridge:" + self.sid)
        return rows[0]

    def receipt(self, delivery, state, **extra):
        return self.client.call(
            "receipt",
            {
                "request_id": "receipt-" + state,
                "session_id": self.sid,
                "delivery_id": delivery["id"],
                "event_id": "event-" + state,
                "body_sha256": delivery["body_sha256"],
                "state": state,
                **extra,
            },
        )

    def assert_code(self, code, operation):
        with self.assertRaises(client_module.ClientError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)

    def test_full_registration_delivery_result_disconnect_and_center_task_loop(self):
        delivery = self.deliver()
        self.receipt(delivery, "received")
        self.receipt(delivery, "running")
        final = self.receipt(delivery, "completed", reply="Fixture completed with evidence.")
        self.assertFalse(final["accepted"])
        self.assertEqual(
            self.client.call("deliveries/delivery-one")["delivery"]["reply"],
            "Fixture completed with evidence.",
        )
        self.assertEqual(
            self.client.call("inbox", query={"session_id": self.sid})["deliveries"], []
        )
        actor = self.enrolled["credential"]["actor_id"]
        snapshot = self.app.snapshot()
        self.assertTrue(next(a for a in snapshot["agents"] if a["id"] == actor)["runtime"]["bound"])
        task = self.client.call(
            "center/task-create",
            {
                "request_id": "create-task",
                "id": "generic-task",
                "project": "os",
                "title": "Isolated task",
                "scope": "fixture/generic",
            },
        )
        claimed = self.client.call(
            "center/task-claim",
            {"request_id": "claim-task", "id": task["id"], "version": 1, "ttl": 300},
        )
        done = self.client.call(
            "center/task-update",
            {
                "request_id": "finish-task",
                "id": task["id"],
                "version": claimed["version"],
                "status": "delivered",
                "evidence": "isolated assertion passed",
            },
        )
        self.assert_code(
            "owner_acceptance_required",
            lambda: self.client.call(
                "center/task-accept",
                {
                    "request_id": "self-accept",
                    "id": task["id"],
                    "version": done["version"],
                    "evidence": "self",
                },
            ),
        )
        accepted = self.hub.client.call(
            "owner",
            "/v1/task-accept",
            {
                "request_id": "owner-accept",
                "id": task["id"],
                "version": done["version"],
                "evidence": "independent fixture assertions passed",
            },
        )
        self.assertEqual(accepted["status"], "accepted")
        disconnected = self.client.call("disconnect", {"request_id": "bye", "session_id": self.sid})
        self.assertEqual(disconnected["session"]["state"], "disconnected")

    def test_chat_ack_identity_and_native_center_dedup(self):
        msg = {
            "request_id": "say-one",
            "project": "os",
            "kind": "discussion",
            "body": "Generic agent joined fixture.",
        }
        first = self.client.call("center/message", msg)
        self.assertEqual(first, self.client.call("center/message", msg))
        inbox = self.client.call("center/inbox", query={"after": 0})
        self.assertEqual(inbox["messages"][0]["sender"], self.enrolled["credential"]["actor_id"])
        self.assertEqual(
            self.client.call("center/ack", {"request_id": "ack-one", "ids": [first["id"]]})[
                "acknowledged"
            ],
            1,
        )
        self.assert_code(
            "idempotency_conflict",
            lambda: self.client.call("center/message", {**msg, "body": "changed"}),
        )

    def test_history_limits_cursor_validation_and_no_implicit_ack(self):
        sent = []
        for i in range(105):
            sent.append(
                self.client.call(
                    "center/message",
                    {
                        "request_id": "page-message-" + str(i),
                        "project": "os",
                        "kind": "discussion",
                        "body": "Bounded history fixture " + str(i),
                    },
                )["seq"]
            )
        default = self.client.call("center/history")
        self.assertEqual(len(default["messages"]), 20)
        page = self.client.call("center/history", query={"limit": 3})
        self.assertEqual([row["seq"] for row in page["messages"]], sent[-3:][::-1])
        self.assertTrue(page["has_more"])
        self.assertEqual(page["next_before"], sent[-3])
        self.assertTrue(all(row["read_at"] is None for row in page["messages"]))
        self.client.call(
            "center/message",
            {
                "request_id": "new-during-pages",
                "project": "os",
                "kind": "discussion",
                "body": "New fixture arrival",
            },
        )
        seen = [row["seq"] for row in page["messages"]]
        while page["has_more"]:
            page = self.client.call(
                "center/history", query={"before": page["next_before"], "limit": 100}
            )
            self.assertLessEqual(len(page["messages"]), 100)
            seen.extend(row["seq"] for row in page["messages"])
        self.assertEqual(seen, sent[::-1])
        self.assertIsNone(page["next_before"])
        self.assertEqual(self.client.call("center/history", query={"before": 1})["messages"], [])
        for query in (
            {"after": 0},
            {"unknown": 1},
            {"limit": 0},
            {"limit": 101},
            {"limit": True},
            {"limit": "3.0"},
            {"before": -1},
            {"before": 9223372036854775808},
        ):
            with self.subTest(query=query):
                self.assert_code(
                    "invalid_center_history_query",
                    lambda: self.client.call("center/history", query=query),
                )
        self.assert_code(
            "duplicate_query", lambda: self.client.call("center/history", query={"limit": [2, 3]})
        )
        mcp = client_module.invoke(
            self.client, "aieyra_center", {"route": "history", "query": {"limit": 2}}
        )
        self.assertEqual(len(mcp["messages"]), 2)

    def test_memory_handoff_requires_live_session_and_survives_reconnection(self):
        self.assertEqual(self.client.call("memory")["memory"]["version"], 0)
        self.assert_code(
            "memory_project_denied",
            lambda: self.client.call("memory", query={"project": "control"}),
        )
        body = {
            "request_id": "memory-one",
            "session_id": self.sid,
            "project": "os",
            "version": 0,
            "sections": {
                k: "Project data: " + k
                for k in ("blueprint", "timeline", "checkpoint", "recovery", "index")
            },
            "summary": "Durable checkpoint",
        }
        self.assert_code("agent_session_missing", lambda: self.client.call("memory", body))
        self.connect()
        first = self.client.call("memory", body)
        self.assertEqual(first["memory"]["version"], 1)
        self.assertTrue(self.client.call("memory", body)["replayed"])
        self.client.call("disconnect", {"request_id": "disconnect-first", "session_id": self.sid})
        self.assert_code(
            "agent_session_expired",
            lambda: self.client.call("memory", {**body, "request_id": "stale-write"}),
        )
        self.sid = "session-two"
        seat = self.client.call("seats")["seats"][0]
        self.seat = seat
        self.connect(request_id="connect-two")
        current = self.client.call("memory")
        self.assertEqual(current["memory"]["sections"], body["sections"])
        self.assert_code(
            "memory_version_conflict",
            lambda: self.client.call(
                "memory", {**body, "session_id": self.sid, "request_id": "memory-stale"}
            ),
        )
        updated = self.client.call(
            "memory", {**body, "session_id": self.sid, "request_id": "memory-two", "version": 1}
        )
        self.assertEqual(updated["memory"]["version"], 2)
        self.assertEqual(
            self.client.call("memory", query={"history": 1})["revisions"][0]["session_id"], self.sid
        )
        self.assertEqual(
            self.client.call("memory", query={"version": 1})["memory"]["session_id"], "session-one"
        )
        mcp_value = client_module.invoke(self.client, "aieyra_memory", {"history": True})
        self.assertEqual(mcp_value["revisions"][0]["version"], 2)
        config_file = self.root / "agent.json"
        config_file.write_text(json.dumps({"url": self.server.origin, "token": self.token}))
        process = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/agent-client.py"),
                "--config",
                str(config_file),
                "memory",
                "--project",
                "os",
            ],
            capture_output=True,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self.assertEqual(process.returncode, 0)
        self.assertEqual(
            json.loads(process.stdout)["memory"]["sha256"], updated["memory"]["sha256"]
        )

    def test_memory_owner_queries_and_csrf_fail_before_save(self):
        for query in [
            "project=os&project=os",
            "project=os&history=2",
            "project=os&before=1",
            "project=os&version=999999999999999999999999999",
            "project=os&a=1&b=2&c=3&d=4",
        ]:
            with self.subTest(query=query):
                with self.assertRaises(HTTPError) as caught:
                    self.owner("/api/project-memory?" + query)
                self.assertEqual(caught.exception.code, 400)
        with self.assertRaises(HTTPError) as caught:
            self.owner("/api/project-memory", {}, headers={"X-Control-CSRF": "wrong"})
        self.assertEqual(caught.exception.code, 403)
        self.assertEqual(self.client.call("memory")["memory"]["version"], 0)

    def test_memory_rechecks_lease_inside_commit_transaction(self):
        self.connect()
        save = self.app.project_memory.save

        def expire_before_commit(*args, **kwargs):
            with self.app.store.db() as db:
                db.execute("UPDATE agent_sessions SET lease_until=0 WHERE id=?", (self.sid,))
            return save(*args, **kwargs)

        self.app.project_memory.save = expire_before_commit
        body = {
            "request_id": "late-memory",
            "session_id": self.sid,
            "project": "os",
            "version": 0,
            "sections": {
                k: "checkpoint"
                for k in ("blueprint", "timeline", "checkpoint", "recovery", "index")
            },
            "summary": "must not commit",
        }
        self.assert_code("agent_session_expired", lambda: self.client.call("memory", body))
        self.assertEqual(self.client.call("memory")["memory"]["version"], 0)

    def test_enrollment_duplicate_token_hash_and_no_secret_in_read_models(self):
        again = self.owner("/api/agent-access/enroll", self.enrollment)
        self.assertEqual(again["credential"], self.enrolled["credential"])
        public = json.dumps(
            [self.owner("/api/agent-access"), self.app.snapshot(), self.owner("/api/agent-openapi")]
        )
        self.assertNotIn(self.token, public)
        self.assertNotIn("token_hash", public)
        self.assertNotIn("identity", json.dumps(self.owner("/api/agent-access")["credentials"]))
        with self.assertRaises(HTTPError) as caught:
            self.owner("/api/agent-access/enroll", {**self.enrollment, "name": "changed"})
        self.assertEqual(caught.exception.code, 409)

    def test_mutual_exclusion_across_credentials_for_same_actor(self):
        self.connect()
        with self.app.store.db() as db:
            identity = db.execute("SELECT identity FROM agent_credentials").fetchone()[0]
        token2 = secrets.token_urlsafe(32)
        self.owner(
            "/api/agent-access/enroll",
            {
                "request_id": "second-client",
                "name": "Other client",
                "project": "os",
                "token": token2,
                "local_identity": identity,
            },
        )
        second = client_module.AgentClient({"url": self.server.origin, "token": token2})
        self.assert_code(
            "agent_seat_not_selectable",
            lambda: second.call(
                "connect",
                {
                    "request_id": "conflict",
                    "session_id": "session-two",
                    "seat_id": self.seat["id"],
                    "seat_epoch": 1,
                },
            ),
        )
        self.assert_code("agent_session_missing", lambda: second.call("sessions/" + self.sid))

    def test_concurrent_claim_has_one_winner(self):
        def claim(i):
            try:
                self.connect(request_id="claim-" + str(i), session_id="candidate-" + str(i))
                return "won"
            except client_module.ClientError:
                return "conflict"

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            values = list(pool.map(claim, range(4)))
        self.assertEqual(values.count("won"), 1)

    def test_receipt_order_hash_cross_session_and_duplicates(self):
        delivery = self.deliver()
        self.assert_code(
            "agent_receipt_transition_rejected",
            lambda: self.receipt(delivery, "completed", reply="too early"),
        )
        self.assert_code(
            "agent_delivery_hash_mismatch",
            lambda: self.receipt(delivery, "received", body_sha256="0" * 64),
        )
        received = self.receipt(delivery, "received")
        repeated = self.receipt(delivery, "received")
        self.assertTrue(repeated["replayed"])
        self.assertEqual(received["delivery"], repeated["delivery"])
        self.assert_code(
            "request_id_conflict", lambda: self.receipt(delivery, "received", event_id="changed")
        )

    def test_expiry_reconnect_never_replays_old_delivery(self):
        delivery = self.deliver()
        with self.app.store.db() as db:
            db.execute("UPDATE agent_sessions SET lease_until=0")
        old = self.client.call("sessions/" + self.sid)
        self.assertEqual(old["session"]["state"], "expired")
        self.connect(request_id="reconnect", session_id="new-session")
        self.assertEqual(
            self.client.call("inbox", query={"session_id": "new-session"})["deliveries"], []
        )
        self.assertEqual(
            self.client.call("deliveries/" + delivery["id"])["delivery"]["state"], "unknown"
        )
        self.assert_code(
            "agent_session_expired",
            lambda: self.client.call(
                "heartbeat", {"request_id": "late", "session_id": self.sid, "runtime_state": "idle"}
            ),
        )

    def test_restart_preserves_delivery_and_reconciliation(self):
        delivery = self.deliver()
        self.receipt(delivery, "received")
        self.app = control.Application({"resources": []}, self.root / "control", hub=self.hub)
        self.server.app = self.app
        current = self.client.call("inbox", query={"session_id": self.sid})["deliveries"]
        self.assertEqual(current[0]["state"], "received")
        self.assertEqual(
            self.client.call("requests/receipt-received")["receipt"]["delivery"]["id"],
            delivery["id"],
        )
        self.assertTrue(self.connect()["replayed"])

    def test_center_epoch_change_invalidates_and_prevents_delivery(self):
        self.connect()
        seat = self.hub.registry()["seats"][0]
        self.hub.client.call(
            "owner",
            "/v1/seat-update",
            {
                "request_id": "handover",
                "id": seat["id"],
                "version": seat["version"],
                "scope": "new-scope",
            },
        )
        self.assert_code(
            "agent_seat_binding_changed",
            lambda: self.client.call("inbox", query={"session_id": self.sid}),
        )
        self.assertEqual(
            self.client.call("sessions/" + self.sid)["session"]["state"], "invalidated"
        )

    def test_revocation_browser_origin_and_owner_api_are_denied(self):
        self.connect()
        with self.assertRaises(HTTPError) as caught:
            self.owner("/api/agent/v1/seats", headers={"Authorization": "Bearer " + self.token})
        self.assertEqual(caught.exception.code, 403)
        with self.assertRaises(HTTPError) as caught:
            self.owner(
                "/api/management",
                {"action": "seat-create", "payload": {}},
                {"Authorization": "Bearer " + self.token},
            )
        self.assertEqual(caught.exception.code, 403)
        self.owner("/api/agent-access/revoke", {"credential_id": "fixture-enroll"})
        self.assert_code("agent_unauthorized", lambda: self.client.call("seats"))
        self.assertEqual(self.owner("/api/agent-access")["sessions"][0]["state"], "revoked")

    def test_mcp_initialize_tool_catalog_and_real_seat_call(self):
        messages = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-11-25"},
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "aieyra_seats", "arguments": {}},
            },
        ]
        output = io.StringIO()
        client_module.mcp(self.client, io.StringIO("\n".join(map(json.dumps, messages))), output)
        results = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(results), 3)
        self.assertEqual(results[0]["result"]["protocolVersion"], "2025-11-25")
        tool_names = [t["name"] for t in results[1]["result"]["tools"]]
        self.assertEqual(len(tool_names), 22)
        self.assertTrue(
            {"aieyra_notify_leader", "aieyra_leader_notifications", "aieyra_notification_ack"}
            <= set(tool_names)
        )
        self.assertEqual(len(set(tool_names)), len(tool_names))
        self.assertTrue(
            {
                "aieyra_matrix_read",
                "aieyra_matrix_sync",
                "aieyra_matrix_publish",
                "aieyra_growth_status",
                "aieyra_growth_record",
            }
            <= set(tool_names)
        )
        self.assertTrue(
            {"aieyra_feedback", "aieyra_feedback_status", "aieyra_feedback_cancel"}
            <= {t["name"] for t in results[1]["result"]["tools"]}
        )
        self.assertIn("aieyra_memory_save", [t["name"] for t in results[1]["result"]["tools"]])
        self.assertFalse(results[2]["result"]["isError"])
        self.assertEqual(
            json.loads(results[2]["result"]["content"][0]["text"])["seats"][0]["id"],
            self.seat["id"],
        )

    def test_cli_real_wire_and_openapi_discovery(self):
        config = self.root / "agent.json"
        config.write_text(
            json.dumps({"url": self.server.origin, "token": self.token}), encoding="utf-8"
        )
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/agent-client.py"),
                "--config",
                str(config),
                "seats",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(json.loads(result.stdout)["seats"][0]["id"], self.seat["id"])
        contract = self.owner("/api/agent-openapi")
        self.assertEqual(contract["openapi"], "3.1.0")
        self.assertIn("/api/agent/v1/center/task-update", contract["paths"])
        self.assertGreater(len(contract["paths"]), 60)

    def test_lost_enrollment_response_and_partial_center_steps_resume(self):
        original = self.hub.client.call
        once = [True]

        def lose(identity, path, body=None):
            result = original(identity, path, body)
            if path == "/v1/adapter-register" and once[0]:
                once[0] = False
                raise OSError("lost_after_commit")
            return result

        self.hub.client.call = lose
        request = {
            "request_id": "partial",
            "name": "Partial fixture",
            "project": "os",
            "token": secrets.token_urlsafe(32),
        }
        with self.assertRaises(HTTPError) as caught:
            self.owner("/api/agent-access/enroll", request)
        self.assertEqual(caught.exception.code, 503)
        result = self.owner("/api/agent-access/enroll", request)
        self.assertEqual(result["state"], "ready")
        self.assertEqual(len(self.hub.registry()["seats"]), 2)
        self.assertEqual(
            len([a for a in self.hub.status()["agents"] if a["name"] == "Partial fixture"]), 1
        )

    def test_center_unavailable_cannot_extend_lease_or_dispatch(self):
        self.connect()
        prior = self.client.call("sessions/" + self.sid)["session"]["lease_until"]
        self.owner(
            "/api/chat",
            {
                "request_id": "offline-delivery",
                "target": self.enrolled["credential"]["actor_id"],
                "body": "Fixture",
            },
        )
        original = self.hub.agent_call
        self.hub.agent_call = lambda *args: (_ for _ in ()).throw(OSError("offline"))
        self.assert_code(
            "agent_center_unavailable",
            lambda: self.client.call(
                "heartbeat",
                {"request_id": "offline-beat", "session_id": self.sid, "runtime_state": "running"},
            ),
        )
        self.app.dispatch_one()
        self.assertEqual(self.app.store.delivery("offline-delivery")["state"], "pending")
        self.assertEqual(self.client.call("sessions/" + self.sid)["session"]["lease_until"], prior)
        self.hub.agent_call = original
        self.app.next_dispatch = 0
        self.app.dispatch_one()
        self.assertEqual(self.app.store.delivery("offline-delivery")["state"], "queued")

    def test_lost_first_registration_response_retires_undelivered_identity(self):
        original = self.hub.client.call
        once = [True]

        def lose(identity, path, body=None):
            result = original(identity, path, body)
            if path == "/v1/register" and once[0]:
                once[0] = False
                raise OSError("first_response_lost")
            return result

        self.hub.client.call = lose
        request = {
            "request_id": "lost-register",
            "name": "Lost registration fixture",
            "project": "os",
            "token": secrets.token_urlsafe(32),
        }
        with self.assertRaises(HTTPError):
            self.owner("/api/agent-access/enroll", request)
        result = self.owner("/api/agent-access/enroll", request)
        self.assertEqual(result["state"], "ready")
        self.assertEqual(
            len([a for a in self.hub.status()["agents"] if a["name"] == request["name"]]), 1
        )

    def test_disconnect_during_center_write_does_not_requeue(self):
        self.connect()
        self.owner(
            "/api/chat",
            {
                "request_id": "racing-delivery",
                "target": self.enrolled["credential"]["actor_id"],
                "body": "Fixture",
            },
        )
        original = self.hub.message

        def disconnect(delivery):
            result = original(delivery)
            self.client.call(
                "disconnect", {"request_id": "during-dispatch", "session_id": self.sid}
            )
            return result

        self.hub.message = disconnect
        self.app.dispatch_one()
        self.assertEqual(self.app.store.delivery("racing-delivery")["state"], "unknown")

    def test_worker_credentials_cannot_enroll_owner_or_accept_other_scope(self):
        with self.assertRaises(HTTPError) as caught:
            self.owner(
                "/api/agent-access/enroll",
                {
                    "request_id": "owner-identity",
                    "name": "Wrong identity",
                    "project": "coordination",
                    "token": secrets.token_urlsafe(32),
                    "local_identity": "owner",
                },
            )
        self.assertEqual(caught.exception.code, 403)
        self.assert_code(
            "workstation_management_denied",
            lambda: self.client.call(
                "center/project-register",
                {
                    "request_id": "elevate",
                    "id": "new-project",
                    "version": 0,
                    "name": "No authority",
                    "root": "fixture",
                    "source": "fixture",
                },
            ),
        )
        self.assert_code(
            "agent_route_not_supported",
            lambda: self.client.call(
                "center/register", {"request_id": "more", "name": "No", "project": "os"}
            ),
        )

    def test_heartbeat_duplicate_is_not_a_second_lease_renewal(self):
        self.connect()
        request = {
            "request_id": "heartbeat-once",
            "session_id": self.sid,
            "runtime_state": "running",
        }
        self.client.call("heartbeat", request)
        with self.app.store.db() as db:
            db.execute("UPDATE agent_sessions SET lease_until=0")
        result = self.client.call("heartbeat", request)
        self.assertTrue(result["replayed"])
        self.assertEqual(result["session"]["state"], "expired")
        self.assertFalse(self.app.snapshot()["agents"][0]["runtime"]["bound"])

    def test_invalid_fields_and_queries_fail_without_side_effects(self):
        self.assert_code("invalid_agent_fields", lambda: self.connect(unexpected=True))
        self.assert_code(
            "invalid_agent_query",
            lambda: self.client.call("seats", query={str(i): "x" for i in range(20)}),
        )
        self.assert_code(
            "duplicate_query",
            lambda: self.client.call("inbox", query={"session_id": ["one", "two"]}),
        )
        self.assertEqual(self.owner("/api/agent-access")["sessions"], [])

    def test_requirement_dispatch_tracks_external_evidence_without_codex_claim(self):
        from test_requirement_delivery import DeliveryHub

        self.connect()
        actor = self.enrolled["credential"]["actor_id"]
        facts = DeliveryHub()
        facts.item["links"][0]["target_actors"] = [actor]
        facts.task["owner"] = actor
        for method in ("intake_read", "intake_record", "intake_receipt"):
            setattr(self.hub, method, getattr(facts, method))
        delivery = self.owner(
            "/api/requirement-dispatch",
            {
                "request_id": "requirement-agent",
                "requirement_id": "requirement-1",
                "requirement_version": 2,
                "task_id": "task-1",
                "task_version": 3,
                "target_actor": actor,
                "target_thread_id": "bridge:" + self.sid,
                "body": "Fixture requirement",
            },
        )["delivery"]
        self.app.dispatch_one()
        self.receipt(delivery, "received")
        self.receipt(delivery, "running")
        self.receipt(delivery, "completed", reply="Requirement checked")
        self.app.requirement_deliveries.sync()
        self.app.requirement_deliveries.sync()
        kinds = {x["receipt"]["kind"] for x in facts.writes}
        self.assertEqual(kinds, {"dispatched", "native_received", "native_started", "completed"})
        self.assertTrue(all(x["receipt"]["thread_ref"].startswith("bridge:") for x in facts.writes))
        evidence = json.loads(Path(facts.writes[-1]["receipt"]["evidence_ref"]).read_text())
        self.assertEqual(evidence["source"], "original_control_delivery_and_agent_report")

    def test_mcp_runtime_renews_lease_without_model_tool_calls_and_releases(self):
        self.client.managed_keepalive = True
        self.client.keepalive_interval = 0.05
        original = self.hub.agent_call
        renewed = threading.Event()

        def observe(identity, path, body=None):
            result = original(identity, path, body)
            if path == "/v1/heartbeat" and body and body["request_id"].startswith("bridge-"):
                renewed.set()
            return result

        self.connect()
        self.hub.agent_call = observe
        try:
            self.assertTrue(renewed.wait(3), "MCP bridge did not renew the lease")
        finally:
            self.client.close()
        self.assertEqual(
            self.client.call("sessions/" + self.sid)["session"]["state"], "disconnected"
        )

    def test_state_free_heartbeat_preserves_latest_execution_receipt(self):
        delivery = self.deliver()
        self.receipt(delivery, "received")
        self.receipt(delivery, "running")
        result = self.client.call("heartbeat", {"request_id": "renew-only", "session_id": self.sid})
        self.assertEqual(result["session"]["runtime_state"], "running")
        self.receipt(delivery, "completed", reply="Done")
        result = self.client.call(
            "heartbeat", {"request_id": "renew-again", "session_id": self.sid}
        )
        self.assertEqual(result["session"]["runtime_state"], "idle")

    def test_disconnect_syncs_previously_clean_requirement_delivery(self):
        from test_requirement_delivery import DeliveryHub

        self.connect()
        actor = self.enrolled["credential"]["actor_id"]
        facts = DeliveryHub()
        facts.item["links"][0]["target_actors"] = [actor]
        facts.task["owner"] = actor
        for method in ("intake_read", "intake_record", "intake_receipt"):
            setattr(self.hub, method, getattr(facts, method))
        self.owner(
            "/api/requirement-dispatch",
            {
                "request_id": "requirement-disconnect",
                "requirement_id": "requirement-1",
                "requirement_version": 2,
                "task_id": "task-1",
                "task_version": 3,
                "target_actor": actor,
                "target_thread_id": "bridge:" + self.sid,
                "body": "Fixture requirement",
            },
        )
        self.app.dispatch_one()
        self.app.requirement_deliveries.sync()
        self.client.call("disconnect", {"request_id": "bye-requirement", "session_id": self.sid})
        self.app.requirement_deliveries.sync()
        self.assertIn("unknown", {x["receipt"]["kind"] for x in facts.writes})

    def test_slow_enrollment_does_not_block_snapshot_or_access_listing(self):
        started, release = threading.Event(), threading.Event()
        original = self.hub.agent_provision

        def delayed(document):
            started.set()
            release.wait(5)
            return original(document)

        self.hub.agent_provision = delayed
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            enrollment = pool.submit(
                self.owner,
                "/api/agent-access/enroll",
                {
                    **self.enrollment,
                    "request_id": "slow-client",
                    "token": secrets.token_urlsafe(32),
                },
            )
            try:
                self.assertTrue(started.wait(2))
                snapshot = pool.submit(self.app.snapshot)
                listing = pool.submit(self.app.agent_access.listing)
                self.assertIn("agents", snapshot.result(timeout=0.5))
                self.assertIn("credentials", listing.result(timeout=0.5))
            finally:
                release.set()
            self.assertEqual(enrollment.result(timeout=8)["state"], "ready")

    def test_snapshot_expiry_during_slow_heartbeat_cannot_revive_session(self):
        self.connect()
        started, release = threading.Event(), threading.Event()
        original = self.hub.agent_call

        def delayed(identity, path, body=None):
            if path == "/v1/heartbeat":
                started.set()
                release.wait(5)
            return original(identity, path, body)

        self.hub.agent_call = delayed
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            beat = pool.submit(
                self.client.call, "heartbeat", {"request_id": "slow-beat", "session_id": self.sid}
            )
            try:
                self.assertTrue(started.wait(2))
                with self.app.store.db() as db:
                    db.execute("UPDATE agent_sessions SET lease_until=0 WHERE id=?", (self.sid,))
                runtime = pool.submit(self.app.agent_access.runtimes).result(timeout=0.5)
                self.assertFalse(
                    runtime[self.enrolled["credential"]["actor_id"]]["runtime"]["bound"]
                )
            finally:
                release.set()
            self.assert_code("agent_session_expired", lambda: beat.result(timeout=8))
        self.assertEqual(self.client.call("sessions/" + self.sid)["session"]["state"], "expired")


if __name__ == "__main__":
    unittest.main()
