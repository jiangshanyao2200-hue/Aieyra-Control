"""Release exports contain only current browser entrypoints and reachable assets."""

import importlib.util
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "source_export_checks", ROOT / "scripts/export-source.py"
)
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)


class SourceExportTests(unittest.TestCase):
    def test_private_metadata_and_encoded_addresses_are_rejected(self):
        octets = [8, 8, 4, 4]
        plain = ".".join(map(str, octets))
        cases = [
            plain,
            plain.replace(".", r"\."),
            plain.replace(".", "[.]"),
            plain.replace(".", r"\u002e"),
            "C:/Users/" + "person-" + "fixture/report.txt",
            "D:/" + "private-installation" + "/source/main.py",
            "/home/" + "person-" + "fixture/report.txt",
            "01" + "abcdef-1234-5678-90ab-cdef01234567",
            "ghp_" + "synthetic" * 4,
        ]
        for value in cases:
            with self.subTest(kind=cases.index(value)):
                with self.assertRaises(ValueError) as caught:
                    exporter.validate_public_files({"docs/example.md": value.encode()})
                self.assertNotIn(value, str(caught.exception))

    def test_documentation_placeholders_and_regex_source_are_allowed(self):
        for value in [
            "C:/Path/To/product.py",
            "E:/Projects/Demo",
            "C:/private/agent.json",
            "/Users/example/Projects/product",
            r"C:\Users\private\code\test.py",
            r"\d\d:\d\d",
            r"/(?:Users|home)/([^\s]+)",
            "127.0.0.1",
            "192.0.2.1",
        ]:
            exporter.validate_public_files({"docs/example.md": value.encode()})

    def test_test_artifacts_and_unreviewed_images_cannot_be_exported(self):
        for name in [
            "desktop/test-output/screen.png",
            "desktop/test-output/receipt.json",
            "desktop/assets/real-workstation.png",
            "data/account.json",
            "desktop/assets/unreviewed.jpg",
            "desktop/assets/icon.png",
        ]:
            with self.subTest(path=name), self.assertRaises(ValueError):
                exporter.validate_public_files({name: b"fixture"})
        exporter.validate_public_files(
            {"desktop/assets/icon.png": (ROOT / "desktop/assets/icon.png").read_bytes()}
        )

    def test_token_scanner_distinguishes_token_prefix_from_task_names(self):
        self.assertIsNone(
            exporter.SENSITIVE.search(b"task-leases-and-past-runtime-remain-distinct")
        )
        self.assertIsNotNone(exporter.SENSITIVE.search(b"sk-" + b"synthetic" * 4))
        self.assertIsNotNone(exporter.SENSITIVE.search(b"ghp_" + b"synthetic" * 4))

    def test_browser_asset_graph_is_complete_and_contains_no_orphans(self):
        assets = {
            p.name: p.read_text(encoding="utf-8")
            for p in (ROOT / "web").iterdir()
            if p.suffix in {".js", ".css", ".html", ".svg"}
        }
        todo, seen = ["index.html"], set()
        while todo:
            name = todo.pop()
            if name in seen:
                continue
            self.assertIn(name, assets, "Missing browser dependency: " + name)
            seen.add(name)
            todo.extend(
                re.findall(
                    r"""(?:from\s*|import\s*\(|(?:src|href)=)["']\./([^"']+)""", assets[name]
                )
            )
        self.assertEqual(
            seen, set(assets), "Unreachable browser assets must be reviewed before release"
        )
        for development in (False, True):
            exported = exporter.sources(development)
            self.assertEqual(
                {p.removeprefix("web/") for p in exported if p.startswith("web/")}, seen
            )
            self.assertIn("docs/agent-access.md", exported)
            self.assertFalse(
                any(
                    set(Path(p).parts)
                    & (
                        {"alpha", "evidence", "data", "__pycache__", "history", "test-output"}
                        | (set() if development else {"test", "tests"})
                    )
                    for p in exported
                )
            )

    def test_browser_runner_points_to_existing_current_suites(self):
        import json

        package = json.loads((ROOT / "tests/web/package.json").read_text(encoding="utf-8"))
        self.assertEqual(set(package["scripts"]), {"test", "test:live", "test:cloud"})
        for name in (
            "run-headless.mjs",
            "office-home.mjs",
            "office-home-live.mjs",
            "cloud-design.mjs",
        ):
            self.assertTrue((ROOT / "tests/web" / name).is_file())


if __name__ == "__main__":
    unittest.main()
