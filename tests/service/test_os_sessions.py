import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "service/product_bridge"))
from fixture_os import FakeOS
from matrix_client import MatrixClient, BridgeError

spec = importlib.util.spec_from_file_location("registered_control", ROOT / "service/main.py")
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.runtime = FakeOS()
        self.addCleanup(self.runtime.close)
        self.data = self.runtime.root / "control"
        self.source_id = "os-" + "a" * 40
        self.local = self.data / "os-sessions" / self.source_id
        self.local.mkdir(parents=True)
        descriptor = json.loads(self.runtime.discovery.read_text())
        self.config = dict(
            self.runtime.config,
            mode="os",
            enable_os_writes=True,
            enable_matrix_writes=True,
            expected_created_at=descriptor["created_at"],
            state_dir=str(self.local / "bridge"),
            work_root=str(self.local / "work"),
        )
        Path(self.config["work_root"]).mkdir()
        self.source = dict(
            version=1,
            id=self.source_id,
            session_id=self.config["expected_session"],
            pid=self.config["expected_pid"],
            executable=self.config["expected_executable"],
            created_at=descriptor["created_at"],
        )
        self.save()
        self.app_config = {
            "resources": [],
            "os_runtime_executables": [self.config["expected_executable"]],
        }
        self.app = control.Application(self.app_config, self.data, hub=object())

    def save(self):
        raw = json.dumps(self.config).encode()
        (self.local / "bridge.json").write_bytes(raw)
        self.source["config_sha256"] = hashlib.sha256(raw).hexdigest()
        (self.local / "source.json").write_text(json.dumps(self.source), encoding="utf-8")

    def test_startup_registers_real_rpc_and_dispatch_uses_existing_ledger(self):
        self.app.collaboration.poll()
        projection = self.app.collaboration.snapshot()
        self.assertTrue(projection["available"])
        self.assertEqual(projection["matrices"][0]["id"], self.source_id)
        self.assertEqual(projection["tasks"], [])
        command = {
            "source_id": self.source_id,
            "command": {
                "action": "matrix.send",
                "request_id": "auto-runtime",
                "message": "fixture",
            },
        }
        self.assertEqual(self.app.product_commands.command(command)["state"], "observed")
        # 重启Control继续同一桥账本，重复请求不执行第二次。
        restarted = control.Application(self.app_config, self.data, hub=object())
        self.assertEqual(restarted.product_commands.command(command)["state"], "observed")
        self.assertEqual(self.runtime.calls["matrix.send"], 1)
        rendered = json.dumps(projection)
        for private in ("config_ref", "registration_ref", self.runtime._token):
            self.assertNotIn(private, rendered)

    def test_late_registration_is_seen_without_restarting_service(self):
        empty = control.Application(self.app_config, self.runtime.root / "late", hub=object())
        self.assertEqual(empty.collaboration.sources, [])
        empty.os_sessions.directory = self.data / "os-sessions"
        empty.os_sessions.refresh()
        empty.collaboration.poll()
        self.assertTrue(empty.collaboration.snapshot()["available"])

    def test_cached_registration_uses_bundled_adapter_without_changing_policy(self):
        with self.app.store.db() as db:
            key = "os-registration:" + self.source_id
            saved = json.loads(
                db.execute("SELECT payload FROM cache WHERE id=?", (key,)).fetchone()[0]
            )
            saved["adapter_dir"] = str(ROOT / "scripts/alpha/product-os")
            original = json.dumps(saved)
            db.execute("UPDATE cache SET payload=? WHERE id=?", (original, key))
        restarted = control.Application(self.app_config, self.data, hub=object())
        self.assertEqual(
            restarted.os_sessions.sources()[0]["adapter_dir"], str(ROOT / "service/product_bridge")
        )
        restarted.collaboration.poll()
        self.assertTrue(restarted.collaboration.snapshot()["available"])
        with restarted.store.db() as db:
            self.assertEqual(
                db.execute("SELECT payload FROM cache WHERE id=?", (key,)).fetchone()[0], original
            )

    def test_policy_changed_or_registration_removed_preserves_stale_facts_and_blocks_commands(self):
        self.app.collaboration.poll()
        self.config["allowed_tools"].append("computer")
        self.save()
        self.app.os_sessions.refresh()
        self.app.collaboration.poll()
        view = self.app.collaboration.snapshot()
        self.assertTrue(view["stale"])
        self.assertEqual(len(view["matrices"]), 1)
        with self.assertRaises(control.ProductCommandError):
            self.app.product_commands.matrix_status(self.source_id)
        restarted = control.Application(self.app_config, self.data, hub=object())
        restarted.collaboration.poll()
        self.assertTrue(restarted.collaboration.snapshot()["stale"])
        with self.assertRaises(control.ProductCommandError):
            restarted.product_commands.matrix_status(self.source_id)
        (self.local / "source.json").unlink()
        self.app.os_sessions.refresh()
        self.assertEqual(len(self.app.os_sessions.sources()), 1)
        self.assertEqual(self.runtime.calls["matrix.send"], 0)

    def test_new_client_rejects_pid_reuse_or_discovery_rotation_before_rpc(self):
        first = MatrixClient(self.config)
        count = self.runtime.calls["describe"]
        descriptor = json.loads(self.runtime.discovery.read_text())
        descriptor["created_at"] = "2099-01-01T00:00:00Z"
        self.runtime.discovery.write_text(json.dumps(descriptor))
        with self.assertRaisesRegex(BridgeError, "runtime_created_at_mismatch"):
            MatrixClient(self.config)
        with self.assertRaises(BridgeError):
            first.rpc("matrix.status")
        self.assertEqual(self.runtime.calls["describe"], count)
        self.app.collaboration.poll()
        self.assertFalse(self.app.collaboration.snapshot()["available"])

    def test_unrecognized_executable_or_binding_does_not_register(self):
        untrusted = control.Application({"resources": []}, self.data, hub=object())
        self.assertEqual(untrusted.os_sessions.sources(), [])
        self.source["session_id"] = "some-other-session"
        self.save()
        mismatch = control.OSSessions(
            self.app_config, self.data, ROOT, control.Store(self.runtime.root / "new-store.sqlite")
        )
        self.assertEqual(mismatch.sources(), [])


if __name__ == "__main__":
    unittest.main()
