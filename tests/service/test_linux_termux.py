"""Headless lifecycle, private station transfer and a real optional Link gateway."""

import importlib.util
import json
import os
from pathlib import Path
import queue
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import ProxyHandler, build_opener

import test_agent_station as stations
import test_device_link as links

ROOT = Path(__file__).resolve().parents[2]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


launcher = load("headless_launcher_tests", "control.py")
transfer = load("station_transfer_tests", "station-transfer.py")


class TransferTests(unittest.TestCase):
    setUp = stations.StationTests.setUp
    close = stations.StationTests.close

    def export(self):
        bundle = self.root / "transfer.json"
        transfer.export_station(self.profile, bundle)
        return bundle

    def test_export_excludes_paths_state_and_native_and_refuses_active_seat(self):
        self.station.ensure("native-desktop")
        with self.assertRaisesRegex(transfer.Error, "finish_the_active_station"):
            self.export()
        self.station.finish("native-desktop")
        bundle = self.export()
        value = json.loads(bundle.read_text())
        self.assertEqual(set(value), {"format", "project", "actor_id", "seat_id", "token"})
        self.assertNotIn("native-desktop", bundle.read_text())
        self.assertNotIn(str(self.project), bundle.read_text())
        if os.name != "nt":
            self.assertEqual(bundle.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            self.export()

    def test_import_rebases_and_never_handoffs_or_enrolls(self):
        self.station.ensure("native-desktop")
        self.station.finish("native-desktop")
        bundle = self.export()
        phone_root = self.root / "phone 项目"
        phone_root.mkdir()
        profile = self.root / "phone.json"
        port = self.server.server_address[1]
        imported = transfer.import_station(bundle, profile, phone_root, "codex", port)
        self.assertFalse(imported["joined"])
        self.assertFalse(imported["handoff_performed"])
        phone = transfer.STATION.Station(profile)
        self.assertEqual(phone.p["actor_id"], self.station.p["actor_id"])
        self.assertEqual(phone.p["seat_id"], self.station.p["seat_id"])
        self.assertEqual(phone.root, phone_root)
        self.assertEqual(phone.p["host"], "codex")
        self.assertEqual(phone.ensure("native-phone")["status"], "handoff_required")
        self.assertEqual(len(self.app.agent_access.listing()["credentials"]), 1)
        # Exact local retry is safe; an existing credential can never be silently repointed.
        self.assertEqual(
            transfer.import_station(bundle, profile, phone_root, "codex", port), imported
        )
        with self.assertRaisesRegex(transfer.Error, "existing_station_import_mismatch"):
            transfer.import_station(bundle, profile, phone_root, "generic", port)
        self.assertEqual(transfer.STATION.Station(profile).p["host"], "codex")

    def test_invalid_transfer_and_wrong_office_never_write_profile(self):
        bundle = self.export()
        value = json.loads(bundle.read_text())
        value["actor_id"] = "wrong-actor"
        stations.adapter.atomic(bundle, value)
        profile = self.root / "invalid.json"
        with self.assertRaisesRegex(transfer.Error, "credential_project_or_seat_mismatch"):
            transfer.import_station(
                bundle, profile, self.project, "codex", self.server.server_address[1]
            )
        self.assertFalse(profile.exists())
        self.assertFalse(profile.with_suffix(".state").exists())
        value["root"] = "unexpected-source-path"
        stations.adapter.atomic(bundle, value)
        with self.assertRaisesRegex(transfer.Error, "invalid_station_transfer"):
            transfer.import_station(bundle, profile, self.project, "codex", 17921)

    @unittest.skipIf(os.name == "nt", "POSIX permission contract")
    def test_insecure_bundle_requires_private_permissions(self):
        bundle = self.export()
        bundle.chmod(0o644)
        with self.assertRaisesRegex(transfer.Error, "private_file_requires_mode_600"):
            transfer.import_station(bundle, self.root / "phone.json", self.project, "codex", 17921)


class LauncherTests(unittest.TestCase):
    def test_termux_selects_android_and_installer_preserves_existing_file(self):
        with (
            patch.dict(os.environ, {"TERMUX_VERSION": "fixture"}),
            patch.object(launcher.platform, "machine", return_value="aarch64"),
        ):
            self.assertEqual(launcher.native_target(), ("android", "arm64"))
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "aieyra-link"
            output.write_bytes(b"user runtime")
            with self.assertRaisesRegex(ValueError, "link_output_exists"):
                launcher.install_link(output)
            self.assertEqual(output.read_bytes(), b"user runtime")

    @unittest.skipIf(os.name == "nt", "Tests POSIX SIGTERM cleanup and permissions")
    def test_headless_start_sigterm_and_restart_with_private_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "private office"
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            for _ in range(2):
                process = subprocess.Popen(
                    [
                        sys.executable,
                        str(ROOT / "scripts/control.py"),
                        "run",
                        "--port",
                        str(port),
                        "--data-root",
                        str(data),
                    ],
                    cwd=directory,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                ready = queue.Queue()
                threading.Thread(
                    target=lambda: ready.put(process.stdout.readline()), daemon=True
                ).start()
                try:
                    event = json.loads(ready.get(timeout=15))
                    self.assertEqual(event["url"], f"http://127.0.0.1:{port}")
                    with build_opener(ProxyHandler({})).open(
                        event["url"] + "/api/health", timeout=3
                    ) as response:
                        self.assertEqual(json.load(response)["service"], "aieyra-control")
                    self.assertEqual((data / "config/control.json").stat().st_mode & 0o777, 0o600)
                    process.send_signal(signal.SIGTERM)
                    process.wait(timeout=12)
                    self.assertEqual(process.returncode, 0, process.stderr.read().decode())
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(5)
                    process.stdout.close()
                    process.stderr.close()


@unittest.skipUnless(links.BINARY, "Set AIEYRA_LINK_TEST_BINARY for encrypted gateway verification")
class NativeTransferTests(unittest.TestCase):
    setUp = links.NativeLinkTests.setUp
    command = links.NativeLinkTests.command
    start_hub = links.NativeLinkTests.start_hub
    stop = links.NativeLinkTests.stop
    station = links.NativeLinkTests.station

    def test_export_import_and_join_through_encrypted_link(self):
        original, _ = self.station("transfer")
        credential = Path(original.p["config_file"])
        stations.adapter.atomic(
            credential, {"url": self.servers[1].origin, "token": original.client.token}
        )
        bundle = self.root / "private-transfer.json"
        transfer.export_station(original.path, bundle)
        phone_profile = self.root / "phone/station.json"
        phone_root = self.root / "phone/project"
        phone_root.mkdir(parents=True)
        port = int(self.gateway.rsplit(":", 1)[1])
        transfer.import_station(bundle, phone_profile, phone_root, "codex", port)
        phone = transfer.STATION.Station(phone_profile)
        joined = phone.ensure("native-phone")
        self.assertEqual(joined["status"], "connected")
        self.assertEqual(phone.p["actor_id"], original.p["actor_id"])
        self.assertEqual(len(self.apps[0].agent_access.listing()["credentials"]), 0)
        self.assertEqual(phone.doctor()["native_session_id"], "native-phone")
        self.assertTrue(phone.finish("native-phone")["lease_released"])
        self.assertEqual(phone.ensure("native-phone-next")["status"], "handoff_required")
        self.assertNotIn(original.client.token, (self.hub_data / "hub.json").read_text())


if __name__ == "__main__":
    unittest.main()
