"""A clean current tree cannot hide a private artifact in older revisions."""

import importlib.util
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "public_history_checks", ROOT / "scripts/check-public-history.py"
)
history = importlib.util.module_from_spec(spec)
spec.loader.exec_module(history)


@unittest.skipUnless(shutil.which("git"), "Git is required for public history checks")
class PublicHistoryTests(unittest.TestCase):
    def test_deleted_private_artifact_is_still_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)

            def git(*args):
                subprocess.run(
                    ["git", "-C", str(repo), *args],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=True,
                )

            git("init")
            git("config", "user.name", "Synthetic Test")
            git("config", "user.email", "test@example.invalid")
            screenshot = repo / "desktop/test-output/screen.png"
            screenshot.parent.mkdir(parents=True)
            screenshot.write_bytes(b"synthetic image bytes")
            git("add", ".")
            git("commit", "-m", "fixture with forbidden output")
            screenshot.unlink()
            (repo / "README.md").write_text("Public documentation", encoding="utf-8")
            git("add", "-A")
            git("commit", "-m", "clean current tree")
            result = history.check_history(repo)
            self.assertEqual(result["commits"], 2)
            self.assertEqual(len(result["failures"]), 1)
            self.assertEqual(result["failures"][0]["path"], "desktop/test-output/screen.png")


if __name__ == "__main__":
    unittest.main()
