"""Real TLS Link processes and isolated Control offices; never calls a model."""

import json
import os
from pathlib import Path
import queue
import secrets
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import test_agent_station as station_tests
from test_control import control
from device_link import DeviceLink, LinkError


ROOT = Path(__file__).resolve().parents[2]
BINARY = os.environ.get("AIEYRA_LINK_TEST_BINARY")


class ConfigurationTests(unittest.TestCase):
    def test_invalid_configuration_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "link/connections.json"
            path.parent.mkdir()
            for row in ({}, {"name": "Device", "paired": False, "gateways": []}):
                raw = json.dumps({"schema": 1, "connections": {"a" * 32: row}})
                path.write_text(raw, encoding="utf-8")
                with self.assertRaisesRegex(LinkError, "configuration_invalid"):
                    DeviceLink(directory, ROOT)
                self.assertEqual(path.read_text(encoding="utf-8"), raw)

    def test_slow_restore_does_not_block_status(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = DeviceLink(directory, ROOT)
            manager.config["connections"]["a" * 32] = {
                "name": "Device",
                "paired": True,
                "gateways": {},
                "attach": True,
            }
            entered, release = threading.Event(), threading.Event()

            def attach(*args, **kwargs):
                entered.set()
                release.wait(5)

            with patch.object(manager, "attach", side_effect=attach):
                manager.start("http://127.0.0.1:17910")
                try:
                    self.assertTrue(entered.wait(2))
                    before = time.monotonic()
                    self.assertEqual(len(manager.status()["connections"]), 1)
                    self.assertLess(time.monotonic() - before, 0.5)
                finally:
                    release.set()
                    manager.close()


@unittest.skipUnless(BINARY, "Set AIEYRA_LINK_TEST_BINARY to the built Link executable")
class NativeLinkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"AIEYRA_LINK_BINARY": BINARY})
        self.env.start()
        self.addCleanup(self.env.stop)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.hub_data = self.root / "hub"
        self.hub_command = [
            BINARY,
            "host",
            "--data",
            str(self.hub_data),
            "--hub-url",
            f"https://127.0.0.1:{port}",
            "--listen",
            f"127.0.0.1:{port}",
        ]
        self.hub = self.start_hub()
        self.addCleanup(lambda: self.stop(self.hub))
        self.apps, self.servers = [], []
        for name in ("local", "remote"):
            project = self.root / name / "project"
            project.mkdir(parents=True)
            app = control.Application(
                {
                    "coordination_mode": "local",
                    "resources": [],
                    "local_projects": [{"id": "demo", "name": name, "root": str(project)}],
                },
                self.root / name / "data",
            )
            server = control.Server(0, app)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            self.apps.append(app)
            self.servers.append(server)
        self.local, self.remote = (app.device_link for app in self.apps)
        self.keys = []
        for name, manager in zip(("local", "remote"), (self.local, self.remote)):
            code = self.command("code")["device_code"]
            result = manager.action("pair", {"name": name, "code": code})
            self.keys.append(result["connection"])
        self.local_key, self.remote_key = self.keys
        self.remote.action("attach", {"connection": self.remote_key})
        catalog = self.local.action("refresh", {"connection": self.local_key})
        self.service = catalog["services"][0]["id"]
        self.gateway = self.local.action(
            "connect",
            {
                "connection": self.local_key,
                "service": self.service,
            },
        )["url"]

    def command(self, *arguments):
        result = subprocess.run(
            [BINARY, *arguments, "--data", str(self.hub_data)],
            capture_output=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8"))
        return json.loads(result.stdout)

    def start_hub(self):
        child = subprocess.Popen(
            self.hub_command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        ready = queue.Queue()
        threading.Thread(target=lambda: ready.put(child.stdout.readline()), daemon=True).start()
        try:
            self.assertEqual(json.loads(ready.get(timeout=10))["state"], "ready")
        except Exception:
            self.stop(child)
            raise
        return child

    def stop(self, child):
        if child.poll() is None:
            child.terminate()
            child.wait(8)
        if child.stdout:
            child.stdout.close()

    def request(self, path, headers=None, body=None):
        request = Request(
            self.gateway + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers or {},
        )
        try:
            with build_opener(ProxyHandler({})).open(request, timeout=35) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def station(self, name, leader=False):
        adapter = station_tests.adapter
        token = secrets.token_urlsafe(32)
        app = self.apps[1]
        enrolled = app.agent_access.enroll(
            {
                "request_id": name,
                "name": name,
                "project": "demo",
                "token": token,
            }
        )
        peer = app.agent_access.authenticate(token)
        if leader:
            app.hub.client.call(
                "owner",
                "/v1/governance-grant",
                {
                    "request_id": "grant-" + name,
                    "actor_id": peer["actor_id"],
                    "role": "leader",
                    "projects": ["demo"],
                    "expires_at": 0,
                    "version": 0,
                    "reason": "Synthetic integration fixture",
                },
            )
        config = self.root / (name + "-credential.json")
        adapter.atomic(config, {"url": self.gateway, "token": token})
        client = adapter.CLIENT.AgentClient(adapter.read_json(config))
        seat = client.call("seats")["seats"][0]
        profile = self.root / (name + "-station.json")
        adapter.atomic(
            profile,
            {
                "schema": 1,
                "host": "generic",
                "project": "demo",
                "root": str(self.root / "local/project"),
                "config_file": str(config),
                "actor_id": enrolled["credential"]["actor_id"],
                "seat_id": seat["id"],
                "lease": False,
            },
        )
        return adapter.Station(profile), token

    def test_real_station_memory_notifications_handoff_and_finish(self):
        self.assertEqual(self.request("/api/agent/v1/info")[0], 403)
        self.assertEqual(
            self.request("/api/agent/v1/info", {"Authorization": "Bearer " + "0" * 43})[0], 401
        )
        leader, token = self.station("leader", leader=True)
        worker, _ = self.station("worker")
        first = leader.ensure("native-leader")
        second = worker.ensure("native-worker")
        self.assertEqual(first["status"], "connected")
        self.assertEqual(second["status"], "connected")
        self.assertEqual(len(self.apps[0].agent_access.listing()["credentials"]), 0)
        for path in ("/api/config", "/api/link", "/api/agent/enroll"):
            self.assertEqual(self.request(path, {"Authorization": "Bearer " + token})[0], 403)
        self.assertEqual(
            self.request(
                "/api/agent/v1/info",
                {
                    "Authorization": "Bearer " + token,
                    "Origin": self.gateway,
                },
            )[0],
            403,
        )
        sections = {
            key: "Synthetic " + key
            for key in (
                "blueprint",
                "timeline",
                "checkpoint",
                "recovery",
                "index",
            )
        }
        saved = leader.client.call(
            "memory",
            {
                "request_id": "memory-once",
                "session_id": first["session_id"],
                "project": "demo",
                "version": 0,
                "sections": sections,
                "summary": "Link test",
            },
        )
        memory = leader.client.call("memory", query={"project": "demo"})["memory"]
        self.assertEqual(memory["sections"], sections)
        self.assertEqual(memory["sha256"], saved["memory"]["sha256"])
        nid = worker.client.call(
            "station/notify-leader",
            {
                "request_id": "notice-once",
                "body": "Synthetic handoff request",
            },
        )["notification"]["id"]
        notice = leader.client.call("station/notifications/" + nid)["notification"]
        self.assertIsNone(notice["read_at"])
        for state in ("read", "handled"):
            leader.client.call(
                "station/notification-ack",
                {
                    "id": nid,
                    "session_id": first["session_id"],
                    "state": state,
                },
            )
        self.assertTrue(
            worker.client.call("station/notifications/" + nid)["notification"]["handled"]
        )
        self.assertTrue(worker.finish("native-worker")["lease_released"])
        self.assertEqual(worker.ensure("native-next")["status"], "handoff_required")
        binding = worker.seat()["station_binding"]
        leader.client.call(
            "station/handoff",
            {
                "request_id": "handoff-once",
                "session_id": first["session_id"],
                "actor_id": worker.p["actor_id"],
                "native_session_id": "native-next",
                "expected_version": binding["version"],
                "reason": "Synthetic explicit handoff",
            },
        )
        self.assertEqual(worker.ensure("native-next")["status"], "connected")
        self.assertTrue(worker.finish("native-next")["lease_released"])
        self.assertTrue(leader.finish("native-leader")["lease_released"])
        self.assertNotIn(token, (self.hub_data / "hub.json").read_text(encoding="utf-8"))

    def test_view_opt_in_restart_identity_and_revocation(self):
        query = {
            "connection": [self.local_key],
            "service": [self.service],
            "resource": ["snapshot"],
        }
        with self.assertRaises(LinkError) as denied:
            self.local.view(query)
        self.assertIn(denied.exception.status, (400, 403))
        self.remote.action("attach", {"connection": self.remote_key, "share_view": True})
        self.assertIn("agents", self.local.view(query))
        with self.assertRaisesRegex(LinkError, "view_not_allowed"):
            self.local.view({**query, "resource": ["config"]})
        before = (self.local.root / self.local_key / "device.json").read_bytes()
        self.local.close()
        restored = DeviceLink(self.local.root.parent, ROOT)
        self.addCleanup(restored.close)
        restored.start(self.servers[0].origin)
        end = time.monotonic() + 15
        while not restored.status()["connections"][0]["gateways"][0]["running"]:
            self.assertLess(time.monotonic(), end)
            time.sleep(0.1)
        self.assertEqual(restored.status()["connections"][0]["gateways"][0]["url"], self.gateway)
        self.assertEqual((restored.root / self.local_key / "device.json").read_bytes(), before)
        self.assertIn("agents", restored.view(query))
        self.stop(self.hub)
        self.hub = self.start_hub()
        self.assertIn("agents", restored.view(query))
        self.command("revoke", "--id", restored.status()["connections"][0]["device_id"])
        with self.assertRaises(LinkError):
            restored.view(query)


if __name__ == "__main__":
    unittest.main()
