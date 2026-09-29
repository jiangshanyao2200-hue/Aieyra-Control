"""User storage never falls back into the software installation."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "service"))
import paths


class StoragePathsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.software = self.base / "software"
        self.software.mkdir()
        self.locator = self.base / "settings/storage.json"
        self.data = self.base / "private/data"
        self.env = {
            "AIEYRA_CONTROL_HOME": str(self.software),
            "AIEYRA_CONTROL_STORAGE": str(self.locator),
            "USERPROFILE": str(self.base / "home"),
            "HOME": str(self.base / "home"),
            "SystemRoot": os.environ.get("SystemRoot", ""),
            "PATH": self.node_path,
            "LOCALAPPDATA": str(self.base / "user"),
            "XDG_DATA_HOME": str(self.base / "user"),
        }
        self.context = patch.dict(os.environ, self.env, clear=True)
        self.context.start()
        self.addCleanup(self.context.stop)

    def write_locator(self, value):
        self.locator.parent.mkdir(exist_ok=True)
        self.locator.write_text(json.dumps(value), encoding="utf-8")

    def test_default_and_initialize_are_external(self):
        root = paths.initialize()
        self.assertFalse(root.is_relative_to(self.software))
        self.assertTrue((root / "config/control.json").is_file())
        self.assertFalse((self.software / "data").exists())

    def test_locator_preserves_existing_data_and_rejects_missing_storage(self):
        self.data.mkdir(parents=True)
        self.write_locator({"schema_version": 1, "data_root": str(self.data)})
        self.assertEqual(paths.data_root(), self.data)
        self.data.rmdir()
        with self.assertRaisesRegex(ValueError, "unavailable"):
            paths.data_root()

    def test_invalid_locator_does_not_create_empty_office(self):
        self.write_locator({"schema_version": 1})
        with self.assertRaisesRegex(ValueError, "invalid_storage_locator"):
            paths.initialize()
        self.assertFalse((self.base / "user").exists())

    def test_legacy_requires_migration(self):
        (self.software / "data").mkdir()
        (self.software / "data/old.sqlite").write_bytes(b"legacy")
        with self.assertRaisesRegex(ValueError, "legacy_storage"):
            paths.initialize()

    def test_explicit_storage_rejects_inside_software_and_relative_paths(self):
        for value in [str(self.software / "data"), str(ROOT / "data"), "relative"]:
            with patch.dict(os.environ, {"AIEYRA_CONTROL_DATA": value}):
                with self.assertRaises(ValueError):
                    paths.initialize()

    def test_desktop_and_service_resolve_identically_and_fail_closed(self):
        node = shutil.which("node", path=self.node_path)
        if not node:
            self.skipTest("Node.js required for desktop parity")
        script = (
            "const p=require(process.argv[1]);"
            "try {const x=p.installationPaths({packaged:false,source:process.argv[2]});"
            "console.log(JSON.stringify({data:x.data}));}"
            "catch(e){console.log(JSON.stringify({error:e.message}));}"
        )

        def desktop():
            result = subprocess.run(
                [node, "-e", script, str(ROOT / "desktop/paths.cjs"), str(self.software)],
                env=dict(os.environ),
                capture_output=True,
                text=True,
                check=True,
            )
            return json.loads(result.stdout)

        self.assertEqual(desktop()["data"], str(paths.data_root()))
        self.data.mkdir(parents=True)
        self.write_locator({"schema_version": 1, "data_root": str(self.data)})
        self.assertEqual(desktop()["data"], str(paths.data_root()))
        self.data.rmdir()
        self.assertEqual(desktop()["error"], "configured_storage_unavailable")
        self.write_locator({"data_root": str(self.data)})
        self.assertEqual(desktop()["error"], "invalid_storage_locator")
        with patch.dict(os.environ, {"AIEYRA_CONTROL_DATA": str(self.software / "data")}):
            self.assertEqual(desktop()["error"], "private_storage_must_be_outside_software")

    node_path = os.environ.get("PATH", "")
