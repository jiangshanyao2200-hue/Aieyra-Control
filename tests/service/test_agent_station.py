"""Real HTTP lifecycle and vendor event/config contracts, without model calls."""

import concurrent.futures
import importlib.util
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from test_control import control

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("station_tests", ROOT / "scripts/agent-station.py")
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


class StationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project space Unicode 项目"
        self.project.mkdir()
        app = control.Application(
            {
                "coordination_mode": "local",
                "resources": [],
                "local_projects": [{"id": "demo", "name": "Demo", "root": str(self.project)}],
            },
            self.root / "data",
        )
        self.app = app
        token = secrets.token_urlsafe(32)
        enrolled = app.agent_access.enroll(
            {"request_id": "enroll", "name": "Fixture", "project": "demo", "token": token}
        )
        self.server = control.Server(0, app)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.close)
        config = self.root / "private.json"
        adapter.atomic(config, {"url": self.server.origin, "token": token})
        self.token = token
        client = adapter.CLIENT.AgentClient(adapter.read_json(config))
        seat = client.call("seats")["seats"][0]
        self.profile = self.root / "profile.json"
        adapter.atomic(
            self.profile,
            {
                "schema": 1,
                "host": "claude",
                "project": "demo",
                "root": str(self.project),
                "config_file": str(config),
                "actor_id": enrolled["credential"]["actor_id"],
                "seat_id": seat["id"],
                "lease": False,
            },
        )
        self.station = adapter.Station(self.profile)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(5)

    def payload(self, event="SessionStart", native="native-one"):
        return {
            "session_id": native,
            "cwd": str(self.project),
            "hook_event_name": event,
            "prompt": "PRIVATE PROMPT MUST NOT BE STORED",
            "transcript_path": "/never/read/me",
        }

    def test_join_resume_stop_new_turn_same_stable_station(self):
        first, output = self.station.hook(self.payload())
        resumed, _ = self.station.hook(self.payload("PostCompact"))
        self.assertEqual(first["session_id"], resumed["session_id"])
        self.assertIn("additionalContext", output["hookSpecificOutput"])
        self.assertNotIn("PRIVATE", self.station.state_file.read_text())
        self.assertNotIn("PRIVATE", (self.station.state_dir / "last-hook.json").read_text())
        stopped, output = self.station.hook(self.payload("Stop"))
        self.assertTrue(stopped["lease_released"])
        self.assertFalse(stopped["memory_saved_by_finish"])
        self.assertEqual(output, {})
        next_turn, _ = self.station.hook(self.payload("UserPromptSubmit"))
        self.assertNotEqual(first["session_id"], next_turn["session_id"])
        self.assertEqual(first["seat_id"], next_turn["seat_id"])

    def test_native_change_requires_handoff_without_takeover(self):
        first = self.station.ensure("native-one")
        self.station.finish("native-one")
        second = self.station.ensure("native-two")
        self.assertEqual(second["status"], "handoff_required")
        self.assertFalse(second["automatic_handoff"])
        self.assertEqual(self.station.state()["session_id"], first["session_id"])
        self.assertEqual(second["handoff_notice"], "sent")
        again = self.station.ensure("native-two")
        self.assertEqual(again["handoff_notice"], "sent")
        history = self.station.client.call("center/history")["messages"]
        self.assertEqual(sum("requests explicit CAS handoff" in x["body"] for x in history), 1)

    def test_join_surfaces_coordination_without_read_receipts_or_private_bodies(self):
        token = secrets.token_urlsafe(32)
        self.app.agent_access.enroll(
            {"request_id": "colleague", "name": "Colleague", "project": "demo", "token": token}
        )
        colleague = adapter.CLIENT.AgentClient({"url": self.server.origin, "token": token})
        message = colleague.call(
            "center/message",
            {
                "request_id": "coordination-notice",
                "project": "demo",
                "kind": "progress",
                "body": "PRIVATE COORDINATION BODY",
            },
        )
        result = self.station.ensure("native-one")
        self.assertEqual(result["unconfirmed_deliveries"], 0)
        self.assertEqual(result["coordination"]["unread_in_recent_window"], 1)
        self.assertNotIn("PRIVATE", json.dumps(result))
        history = self.station.client.call("center/history")["messages"]
        readback = next(item for item in history if item["id"] == message["id"])
        self.assertIsNone(readback["read_at"])
        self.assertEqual(readback["receipt_count"], 0)
        actual = self.station.client.call

        def unavailable(route, body=None, query=None):
            if route == "center/history":
                raise adapter.Error("coordination_temporarily_unavailable")
            return actual(route, body, query)

        with patch.object(self.station.client, "call", side_effect=unavailable):
            resumed = self.station.ensure("native-one")
        self.assertEqual(resumed["session_id"], result["session_id"])
        self.assertEqual(resumed["coordination"]["status"], "unavailable")

    def test_handoff_automatically_persists_leader_notice_once_after_response_loss(self):
        token = secrets.token_urlsafe(32)
        access = self.app.agent_access
        access.enroll(
            {"request_id": "leader-fixture", "name": "Leader", "project": "demo", "token": token}
        )
        leader = access.authenticate(token)
        seat = access.seats(leader)["seats"][0]
        self.app.hub.client.call(
            "owner",
            "/v1/governance-grant",
            {
                "request_id": "leader-grant",
                "actor_id": leader["actor_id"],
                "role": "leader",
                "projects": ["demo"],
                "expires_at": 0,
                "version": 0,
                "reason": "Fixture user appointment",
            },
        )
        access.mutate(
            leader,
            "connect",
            {
                "request_id": "leader-join",
                "session_id": "leader-session",
                "seat_id": seat["id"],
                "seat_epoch": seat["epoch"],
                "native_session_id": "leader-native",
            },
        )
        self.station.ensure("native-one")
        self.station.finish("native-one")
        original = self.station.client.call

        def lost(route, body=None, query=None):
            result = original(route, body, query)
            if route == "station/notify-leader":
                raise adapter.Error("network_response_lost")
            return result

        with patch.object(self.station.client, "call", side_effect=lost):
            first = self.station.ensure("native-two")
        self.assertEqual(first["leader_notification_error"], "network_response_lost")
        second = self.station.ensure("native-two")
        third = self.station.ensure("native-two")
        self.assertEqual(second["leader_notification_id"], third["leader_notification_id"])
        rows = self.app.station_notifications.read(leader)["notifications"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["native_session_id"], "leader-native")
        self.assertEqual(self.station.doctor()["native_session_id"], "native-one")

    def test_concurrent_hooks_create_one_connection(self):
        def join(_):
            return adapter.Station(self.profile).ensure("native-one")["session_id"]

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(join, range(5)))
        self.assertEqual(len(set(results)), 1)

    def test_explicit_resume_reuses_bounded_reads_without_ack_or_cursor_persistence(self):
        first = self.station.ensure("native-one")
        client = self.station.client
        for index in range(3):
            client.call(
                "center/message",
                {
                    "request_id": "resume-message-" + str(index),
                    "project": "demo",
                    "kind": "progress",
                    "body": "PRIVATE handoff " + str(index),
                },
            )
        with patch.object(client, "call", wraps=client.call) as calls:
            result = self.station.ensure("native-one", resume=True, after=0, limit=2)
        self.assertEqual(result["project"], "demo")
        self.assertEqual(result["transport_session_id"], first["session_id"])
        bundle = result["resume"]
        self.assertEqual(bundle["read_order"], ["memory", "runtime_inbox", "coordination"])
        self.assertEqual(bundle["memory"]["memory"]["version"], 0)
        self.assertEqual(len(bundle["coordination"]["messages"]), 2)
        cursor = bundle["coordination"]["next_cursor"]
        next_result = self.station.ensure("native-one", resume=True, after=cursor, limit=2)
        self.assertEqual(len(next_result["resume"]["coordination"]["messages"]), 1)
        routes = [c.args[0] for c in calls.call_args_list]
        for route in ("memory", "inbox", "center/inbox"):
            self.assertEqual(routes.count(route), 1)
        self.assertNotIn("center/ack", routes)
        self.assertNotIn("center/history", routes)
        self.assertTrue(bundle["ack_required_after_read"])
        self.assertFalse(bundle["cursor_persisted"])
        state = self.station.state_file.read_text()
        self.assertNotIn("PRIVATE", state)
        self.assertNotIn("cursor", state)
        messages = client.call("center/history")["messages"]
        self.assertTrue(all(row["receipt_count"] == 0 for row in messages))

    def test_resume_rejects_invalid_bounds_before_connecting(self):
        for after, limit in ((-1, 20), (True, 20), (0, 0), (0, 101), (2**63, 20)):
            with self.subTest(after=after, limit=limit), self.assertRaises(adapter.Error):
                self.station.ensure("native-one", resume=True, after=after, limit=limit)
        self.assertFalse(self.station.state_file.exists())

    def test_resume_oversized_chat_returns_explicit_separate_read(self):
        original = self.station.client.call

        def large(route, body=None, query=None):
            value = original(route, body, query)
            if route == "center/inbox":
                value["messages"] = [
                    {"id": "large", "seq": 1, "project": "demo", "body": "x" * 140000}
                ]
            return value

        with patch.object(self.station.client, "call", side_effect=large):
            result = self.station.ensure("native-one", resume=True)
        self.assertEqual(
            result["resume"]["coordination"], {"truncated": True, "read_separately": "center/inbox"}
        )

    def test_lost_connect_response_reads_back_without_replay(self):
        actual = self.station.client.call
        connects = []

        def lost(route, body=None, query=None):
            result = actual(route, body, query)
            if route == "connect":
                connects.append(body)
                raise adapter.Error("connection_unconfirmed_query_original_request")
            return result

        with patch.object(self.station.client, "call", side_effect=lost):
            result = self.station.ensure("native-one")
        self.assertEqual(result["status"], "connected")
        self.assertEqual(len(connects), 1)

    def test_unknown_connect_preserves_intent_and_does_not_retry(self):
        actual = self.station.client.call
        connects = []

        def unknown(route, body=None, query=None):
            if route == "connect":
                connects.append(body)
                raise adapter.Error("connection_unconfirmed_query_original_request")
            return actual(route, body, query)

        with patch.object(self.station.client, "call", side_effect=unknown):
            first = self.station.ensure("native-one")
            again = self.station.ensure("native-one")
        self.assertEqual(first["status"], "connect_unconfirmed")
        self.assertEqual(again["request_id"], first["request_id"])
        self.assertEqual(len(connects), 1)

    def test_subagents_and_wrong_project_do_not_join(self):
        result, output = self.station.hook({**self.payload(), "agent_id": "child"})
        self.assertEqual(result["status"], "subagent_ignored")
        self.assertEqual(output, {})
        with self.assertRaises(adapter.Error):
            self.station.hook({**self.payload(), "cwd": str(self.root)})
        self.assertFalse(self.station.state_file.exists())

    def test_other_native_cannot_finish_owned_transport(self):
        first = self.station.ensure("native-one")
        result = self.station.finish("native-two")
        self.assertFalse(result["lease_released"])
        self.assertEqual(
            self.station.client.call("sessions/" + first["session_id"])["session"]["state"],
            "connected",
        )

    def test_cursor_payload_and_nonblocking_prompt_output(self):
        self.station.p["host"] = "cursor"
        payload = {
            "conversation_id": "conv-one",
            "session_id": "conv-one",
            "workspace_roots": [str(self.project)],
            "hook_event_name": "sessionStart",
        }
        first, output = self.station.hook(payload)
        self.assertIn("additional_context", output)
        _, output = self.station.hook({**payload, "hook_event_name": "beforeSubmitPrompt"})
        self.assertEqual(output, {"continue": True})
        result, output = self.station.hook({**payload, "hook_event_name": "stop", "loop_count": 0})
        self.assertTrue(result["lease_released"])
        self.assertNotIn("followup_message", output)
        self.assertEqual(first["native_session_id"], "conv-one")

    @unittest.skipUnless(os.name == "nt", "Windows Cursor shell invocation")
    def test_generated_cursor_windows_command_consumes_unicode_stdin(self):
        self.station.p["host"] = "cursor"
        adapter.atomic(self.profile, self.station.p)
        adapter.configure(self.station)
        command = adapter.read_json(self.project / ".cursor/hooks.json")["hooks"]["sessionStart"][
            0
        ]["command"]
        payload = {
            "hook_event_name": "sessionStart",
            "conversation_id": "cursor-native",
            "workspace_roots": [str(self.project)],
            "prompt": "中文秘密不要保存",
        }
        result = subprocess.run(
            command.split(),
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("additional_context", json.loads(result.stdout))
        self.assertEqual(self.station.state()["native_session_id"], "cursor-native")
        self.assertNotIn("秘密", self.station.state_file.read_text(encoding="utf-8"))

    def test_real_bounded_helper_releases_after_maximum_duration(self):
        self.station.p.update(lease=True, max_seconds=30, idle_seconds=30)
        adapter.atomic(self.profile, self.station.p)
        first = self.station.ensure("native-one")
        sid = first["session_id"]
        deadline = time.monotonic() + 38
        session = {}
        while time.monotonic() < deadline:
            session = self.station.client.call("sessions/" + sid)["session"]
            if session["state"] != "connected":
                break
            time.sleep(0.25)
        self.assertEqual(session["state"], "disconnected")
        self.assertEqual(session["lease_until"], 0)
        worker = self.station.state_dir / (sid + ".worker")
        deadline = time.monotonic() + 2
        while worker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(worker.exists())

    def test_install_twice_uninstall_preserves_unrelated_settings(self):
        for host in ("claude", "cursor", "codex"):
            with self.subTest(host=host):
                p = adapter.read_json(self.profile)
                p["host"] = host
                adapter.atomic(self.profile, p)
                station = adapter.Station(self.profile)
                agents = self.project / "AGENTS.md"
                agents.write_text("# User rules\nKeep my instructions.\n", encoding="utf8")
                if host == "claude":
                    path = self.project / ".claude/settings.local.json"
                    adapter.atomic(
                        path,
                        {
                            "permissions": {"allow": ["Read"]},
                            "hooks": {
                                "Stop": [{"hooks": [{"type": "command", "command": "echo user"}]}]
                            },
                        },
                    )
                first = adapter.configure(station)
                self.assertIn("AGENTS.md", first["changed"])
                second = adapter.configure(station)
                self.assertEqual(second["changed"], [])
                self.assertNotIn(self.token, agents.read_text())
                self.assertIn("Keep my instructions", agents.read_text())
                adapter.configure(station, remove=True)
                self.assertEqual(agents.read_text(), "# User rules\nKeep my instructions.\n")
                if host == "claude":
                    settings = adapter.read_json(path)
                    self.assertEqual(settings["permissions"], {"allow": ["Read"]})
                    self.assertEqual(len(settings["hooks"]["Stop"]), 1)

    def test_install_refuses_manually_modified_owned_hook_before_writes(self):
        adapter.configure(self.station)
        settings = self.project / ".claude/settings.local.json"
        current = adapter.read_json(settings)
        current["hooks"]["SessionStart"][0]["hooks"][0]["timeout"] = 777
        adapter.atomic(settings, current)
        agents = (self.project / "AGENTS.md").read_bytes()
        with self.assertRaises(adapter.Error):
            adapter.configure(self.station, remove=True)
        self.assertEqual((self.project / "AGENTS.md").read_bytes(), agents)
        self.assertEqual(
            adapter.read_json(settings)["hooks"]["SessionStart"][0]["hooks"][0]["timeout"], 777
        )

    def test_install_accepts_windows_line_endings_and_preserves_user_bytes(self):
        self.station.p["host"] = "generic"
        adapter.configure(self.station)
        path = self.project / "AGENTS.md"
        rule = path.read_bytes().replace(b"\n", b"\r\n")
        user = "# User edits\r\nKeep 用户 text unchanged.\r\n".encode("utf-8")
        path.write_bytes(rule + user)
        adapter.configure(self.station)
        self.assertTrue(path.read_bytes().endswith(user))
        adapter.configure(self.station, remove=True)
        self.assertEqual(path.read_bytes(), user)

    def test_doctor_is_readonly(self):
        result = self.station.doctor()
        self.assertEqual(result["status"], "ready")
        self.assertFalse(self.station.state_file.exists())
        self.assertIsNone(self.station.seat()["session_id"])

    def test_os_doctor_absence_is_not_runtime_success(self):
        result = self.station.os_doctor()
        self.assertEqual(result["status"], "no_verified_runtime")
        self.assertEqual(result["runtimes"], [])
        self.assertFalse(result["model_started"])
        self.assertFalse(result["private_sessions_scanned"])

    def test_profile_identity_mismatch_is_rejected(self):
        self.station.p["actor_id"] = "not-my-actor"
        with self.assertRaises(adapter.Error):
            self.station.ensure("native-one")
        self.assertFalse(self.station.state_file.exists())

    def test_credentials_cannot_be_embedded_in_profile(self):
        profile = adapter.read_json(self.profile)
        profile["token"] = self.token
        adapter.atomic(self.profile, profile)
        with self.assertRaises(adapter.Error):
            adapter.Station(self.profile)

    def create(self, **changes):
        options = {
            "profile_path": self.root / "new-station.json",
            "name": "New Agent 新成员",
            "project": "demo",
            "root": self.project,
            "host": "generic",
            "native": "new-native",
            "port": self.server.server_address[1],
            "lease": False,
        }
        options.update(changes)
        return adapter.create_station(**options)

    def test_create_enrolls_own_station_and_joins_without_installing(self):
        result = self.create()
        self.assertEqual(result["status"], "connected")
        self.assertEqual(result["native_session_id"], "new-native")
        self.assertNotEqual(result["actor_id"], self.station.p["actor_id"])
        self.assertNotEqual(result["seat_id"], self.station.p["seat_id"])
        profile = adapter.read_json(result["profile"])
        config = adapter.read_json(result["config_file"])
        self.assertNotIn("token", profile)
        self.assertNotIn(config["token"], json.dumps(result))
        self.assertNotIn(config["token"], (self.root / "new-station.state/create.json").read_text())
        self.assertFalse((self.project / "AGENTS.md").exists())
        self.assertEqual(len(self.app.agent_access.listing()["credentials"]), 2)
        same = self.create(resume=True)
        self.assertEqual(same["session_id"], result["session_id"])
        self.assertEqual(same["enrollment_request_id"], result["enrollment_request_id"])
        adapter.Station(result["profile"]).finish("new-native")
        rejoined = adapter.Station(result["profile"]).ensure("new-native")
        self.assertEqual(rejoined["seat_id"], result["seat_id"])
        self.assertEqual(len(self.app.agent_access.listing()["credentials"]), 2)

    def test_create_lost_enrollment_response_resumes_original_identity(self):
        enroll = adapter.ENROLL.enroll

        def lost(*args, **kwargs):
            enroll(*args, **kwargs)
            raise OSError("response lost")

        with patch.object(adapter.ENROLL, "enroll", side_effect=lost):
            with self.assertRaisesRegex(adapter.Error, "retry_create_with_resume"):
                self.create()
        config = self.root / "new-station.state/credential.json"
        original = config.read_bytes()
        self.assertFalse((self.root / "new-station.json").exists())
        result = self.create(resume=True)
        self.assertEqual(result["status"], "connected")
        self.assertEqual(config.read_bytes(), original)
        self.assertEqual(len(self.app.agent_access.listing()["credentials"]), 2)

    def test_create_resumes_after_profile_publication_failure(self):
        save = adapter.ENROLL.save_new

        def fail_profile(path, value):
            if Path(path).name == "new-station.json":
                raise OSError("disk unavailable")
            save(path, value)

        with patch.object(adapter.ENROLL, "save_new", side_effect=fail_profile):
            with self.assertRaises(OSError):
                self.create()
        self.assertFalse((self.root / "new-station.json").exists())
        result = self.create(resume=True)
        self.assertEqual(result["status"], "connected")
        self.assertEqual(len(self.app.agent_access.listing()["credentials"]), 2)

    def test_create_resumes_after_join_interruption_without_enrolling(self):
        with patch.object(adapter.Station, "ensure", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                self.create()
        profile = (self.root / "new-station.json").read_bytes()
        with patch.object(adapter.ENROLL, "enroll", side_effect=AssertionError("duplicate enroll")):
            result = self.create(resume=True)
        self.assertEqual(result["status"], "connected")
        self.assertEqual((self.root / "new-station.json").read_bytes(), profile)

    def test_create_keeps_unknown_connect_intent_without_replay(self):
        call = adapter.CLIENT.AgentClient.call
        attempts = []

        def unavailable(client, route, body=None, query=None):
            if route == "connect" or route.startswith(("sessions/", "requests/")):
                attempts.append(route)
                raise adapter.Error("connection_unconfirmed_query_original_request")
            return call(client, route, body, query)

        with patch.object(adapter.CLIENT.AgentClient, "call", new=unavailable):
            first = self.create()
            second = self.create(resume=True)
        self.assertEqual(first["status"], "connect_unconfirmed")
        self.assertEqual(second["request_id"], first["request_id"])
        self.assertEqual(second["session_id"], first["session_id"])
        self.assertEqual(attempts.count("connect"), 1)
        self.assertEqual(len(self.app.agent_access.listing()["credentials"]), 2)

    def test_create_concurrent_resumes_share_one_identity_and_connection(self):
        with patch.object(adapter.ENROLL, "enroll", side_effect=OSError("offline")):
            with self.assertRaises(adapter.Error):
                self.create()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.create(resume=True), range(2)))
        self.assertEqual(results[0]["session_id"], results[1]["session_id"])
        self.assertEqual(len(self.app.agent_access.listing()["credentials"]), 2)

    def test_create_refuses_existing_profile_and_changed_retry_parameters(self):
        original = self.profile.read_bytes()
        with self.assertRaisesRegex(adapter.Error, "station_profile_exists"):
            self.create(profile_path=self.profile)
        with self.assertRaisesRegex(adapter.Error, "intent_missing"):
            self.create(profile_path=self.profile, resume=True)
        self.assertEqual(self.profile.read_bytes(), original)
        self.create()
        for change in ({"name": "Other"}, {"host": "codex"}, {"native": "other-native"}):
            with self.subTest(change=change):
                with self.assertRaisesRegex(adapter.Error, "resume_mismatch"):
                    self.create(resume=True, **change)
        with self.assertRaisesRegex(adapter.Error, "station_profile_exists"):
            self.create()
        self.assertEqual(len(self.app.agent_access.listing()["credentials"]), 2)

    def test_create_refuses_modified_profile_on_resume(self):
        result = self.create()
        profile = adapter.read_json(result["profile"])
        profile["actor_id"] = self.station.p["actor_id"]
        adapter.atomic(result["profile"], profile)
        with self.assertRaisesRegex(adapter.Error, "profile_conflict"):
            self.create(resume=True)

    def test_created_station_requires_explicit_handoff_for_new_native(self):
        result = self.create()
        station = adapter.Station(result["profile"])
        station.finish("new-native")
        self.assertEqual(station.ensure("different-native")["status"], "handoff_required")
        self.assertEqual(station.seat()["station_binding"]["native_session_id"], "new-native")

    def test_new_os_agents_create_distinct_stations_without_existing_credentials(self):
        first = self.create(host="os", native="os-runtime-new-agent-one")
        second = self.create(
            profile_path=self.root / "os-agent-two.json",
            name="OS Agent Two",
            host="os",
            native="os-runtime-new-agent-two",
        )
        self.assertEqual(first["status"], "connected")
        self.assertEqual(second["status"], "connected")
        self.assertNotEqual(first["actor_id"], second["actor_id"])
        self.assertNotEqual(first["seat_id"], second["seat_id"])
        self.assertNotEqual(first["session_id"], second["session_id"])
        self.assertTrue(first["session_id"].startswith("station-os-"))
        for result in (first, second):
            station = adapter.Station(result["profile"])
            own = station.client.call("info")
            self.assertEqual(own["actor_id"], result["actor_id"])
            self.assertEqual(
                station.client.call("inbox", query={"session_id": result["session_id"]})[
                    "deliveries"
                ],
                [],
            )
            self.assertEqual(
                station.client.call("memory", query={"project": "demo"})["memory"]["version"], 0
            )
            self.assertEqual(len(station.client.call("seats")["seats"]), 1)
            self.assertTrue(station.finish(result["native_session_id"])["lease_released"])
        self.assertEqual(len(self.app.agent_access.listing()["credentials"]), 3)

    def test_create_rejects_invalid_arguments_before_enrollment(self):
        for change in (
            {"native": ""},
            {"project": "bad/project"},
            {"port": 65536},
            {"name": " "},
            {"host": "unsupported"},
            {"root": self.root / "missing"},
        ):
            with self.subTest(change=change):
                with self.assertRaises(adapter.Error):
                    self.create(**change)
        self.assertFalse((self.root / "new-station.state").exists())
        self.assertEqual(len(self.app.agent_access.listing()["credentials"]), 1)

    def test_create_reports_unknown_project_without_registering_or_joining(self):
        with self.assertRaises(adapter.Error) as raised:
            self.create(project="unregistered")
        self.assertEqual(raised.exception.code, "unknown_project")
        self.assertFalse((self.root / "new-station.json").exists())
        self.assertEqual(len(self.app.agent_access.listing()["credentials"]), 1)

    def test_create_allows_explicit_worktree_without_changing_project_registration(self):
        worktree = self.root / "another worktree"
        worktree.mkdir()
        result = self.create(root=worktree)
        self.assertEqual(adapter.Station(result["profile"]).root, worktree)
        projects = self.station.client.call("center/registry")["projects"]
        project = next(p for p in projects if p["id"] == "demo")
        self.assertEqual(Path(project["root"]), self.project)

    def test_private_json_publication_failure_leaves_no_partial_credential(self):
        with patch.object(adapter.ENROLL.os, "link", side_effect=OSError("unavailable")):
            with self.assertRaises(adapter.Error):
                self.create()
        self.assertFalse((self.root / "new-station.state/credential.json").exists())
        self.assertEqual(list((self.root / "new-station.state").glob("*.tmp")), [])
        self.assertEqual(len(self.app.agent_access.listing()["credentials"]), 1)
        self.assertEqual(self.create(resume=True)["status"], "connected")

    def test_create_cli_uses_new_profile_and_reports_no_token(self):
        args = [
            os.sys.executable,
            "-X",
            "utf8",
            str(ROOT / "scripts/agent-station.py"),
            "--profile",
            str(self.root / "cli-station.json"),
            "create",
            "--name",
            "CLI Agent",
            "--project",
            "demo",
            "--root",
            str(self.project),
            "--host",
            "generic",
            "--native-session-id",
            "cli-native",
            "--port",
            str(self.server.server_address[1]),
            "--no-lease",
        ]
        run = subprocess.run(
            args,
            capture_output=True,
            timeout=20,
            text=True,
            encoding="utf-8",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(run.returncode, 0, run.stderr + run.stdout)
        result = json.loads(run.stdout)
        self.assertEqual(result["status"], "connected")
        token = adapter.read_json(result["config_file"])["token"]
        self.assertNotIn(token, run.stdout + run.stderr)
        again = subprocess.run(
            args + ["--resume"],
            capture_output=True,
            timeout=20,
            text=True,
            encoding="utf-8",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(again.returncode, 0, again.stderr + again.stdout)
        self.assertEqual(json.loads(again.stdout)["session_id"], result["session_id"])

    def test_enrollment_cli_still_resumes_original_request(self):
        args = [
            os.sys.executable,
            "-X",
            "utf8",
            str(ROOT / "scripts/enroll-agent.py"),
            "--name",
            "Bootstrap",
            "--project",
            "demo",
            "--output",
            str(self.root / "cli.json"),
            "--port",
            str(self.server.server_address[1]),
        ]
        outputs = []
        for suffix in ([], ["--resume"]):
            run = subprocess.run(
                args + suffix,
                capture_output=True,
                timeout=20,
                text=True,
                encoding="utf-8",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.assertEqual(run.returncode, 0, run.stderr + run.stdout)
            outputs.append(json.loads(run.stdout))
        self.assertEqual(outputs[0]["credential"], outputs[1]["credential"])
        self.assertEqual(len(self.app.agent_access.listing()["credentials"]), 2)

    def test_private_json_publication_never_overwrites_existing_bytes(self):
        path = self.root / "existing.json"
        path.write_bytes(b"original")
        with self.assertRaises(FileExistsError):
            adapter.ENROLL.save_new(path, {"token": "replacement"})
        self.assertEqual(path.read_bytes(), b"original")
        self.assertEqual(list(self.root.glob("existing.json.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
