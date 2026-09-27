import json
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("control_discovery", ROOT / "service/discovery.py")
discovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(discovery)


class DiscoveryTests(unittest.TestCase):
    def test_codex_metadata_and_os_unavailable_without_private_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "codex.exe"
            executable.write_text("fixture", encoding="utf-8")
            calls = []

            def runner(args, **kwargs):
                calls.append((args, kwargs))
                return SimpleNamespace(returncode=0, stdout="codex 0.155.1\n")

            result = discovery.discover({"codex_executable": str(executable)}, runner)
            self.assertEqual(result["agents"][0]["availability"], "available")
            self.assertEqual(result["agents"][0]["version_label"], "codex 0.155.1")
            self.assertEqual(result["agents"][1]["kind"], "aieyra-os")
            self.assertEqual(result["agents"][1]["availability"], "unavailable")
            self.assertFalse(result["private_data_read"])
            self.assertFalse(result["model_started"])
            self.assertEqual(calls[0][0][-1], "--version")

    def test_failed_version_and_no_config_are_explicit(self):
        result = discovery.discover(
            {}, lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError())
        )
        self.assertEqual(result["agents"][0]["availability"], "unavailable")
        self.assertEqual(result["agents"][1]["availability"], "unavailable")
        self.assertNotIn("password", json.dumps(result, ensure_ascii=False).lower())


if __name__ == "__main__":
    unittest.main()
