#!/usr/bin/env python3
"""Export public source from a strict allowlist, never a working-tree archive."""

import argparse
import hashlib
import ipaddress
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BLOCKED = {
    "node_modules",
    "__pycache__",
    "evidence",
    "alpha",
    "messages",
    "data",
    ".git",
    ".runtime",
    "test-output",
    "history",
}
SENSITIVE = re.compile(
    rb"gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\bsk-[A-Za-z0-9_-]{24,}|\bAKIA[A-Z0-9]{16}\b|\bxox[baprs]-[A-Za-z0-9-]{15,}"
)
BINARY_ASSETS = {
    "desktop/assets/icon.png": "f63b931ca21255532059af9e26bcc444ed738bf770ab003ed4b0553ab6a80cba",
    "desktop/assets/icon.ico": "0ca1399c1d88d1caaf8d02cb6c6a6befdfa1490f7289f3c146fc4c551d62f3c3",
}
TEXT_SUFFIXES = {
    "",
    ".py",
    ".js",
    ".json",
    ".cjs",
    ".mjs",
    ".md",
    ".yml",
    ".yaml",
    ".html",
    ".css",
    ".ps1",
    ".vbs",
    ".cs",
    ".toml",
    ".txt",
    ".svg",
    ".pem",
}
WINDOWS_PATH = re.compile(r"(?<![\w\\])([A-Za-z]):[\\/]+([^\s\"'<>`|]+)")
HOME_PATH = re.compile(r"/(?:Users|home)/([A-Za-z0-9_.-]+)(?:[/\\]|\b)")
OPERATIONS_PATH = re.compile(r"/(?:root|srv|opt)/([A-Za-z0-9_.-]+)(?:[/\\]|\b)")
STATION_ID = re.compile(
    r"\b(?:agent-[a-f0-9]{16}|ext-(?:seat|host|adapter)-[a-f0-9]{24}|station-[a-z0-9_.-]+-[a-f0-9]{32})\b",
    re.I,
)
NATIVE_THREAD = re.compile(
    r"\b01[0-9a-f]{6}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I
)
EXAMPLE_ROOTS = {
    "path",
    "private",
    "projects",
    "absolute",
    "windows",
    "untrusted",
    "owner",
    "secret_key",
}
EXAMPLE_USERS = {"example", "user", "private", "test", "fixture", "username"}


def validate_public_files(files):
    """Fail closed on known private metadata forms; never print matched values."""
    for name, raw in files.items():
        parts = Path(name).parts
        if set(parts) & BLOCKED or Path(name).is_absolute() or ".." in parts:
            raise ValueError("private_artifact_path_in_" + name)
        if Path(name).suffix in {".png", ".ico"}:
            if hashlib.sha256(raw).hexdigest() != BINARY_ASSETS.get(name):
                raise ValueError("unreviewed_binary_asset_in_" + name)
            continue
        if SENSITIVE.search(raw):
            raise ValueError("sensitive_content_in_" + name)
        if Path(name).suffix not in TEXT_SUFFIXES:
            raise ValueError("unreviewed_file_type_in_" + name)
        text = raw.decode("utf-8", errors="strict")
        # Decode common source/JSON escapes without evaluating source code.
        text = re.sub(
            r"\\u([0-9a-fA-F]{4})|\\x([0-9a-fA-F]{2})",
            lambda m: chr(int(m.group(1) or m.group(2), 16)),
            text,
        )
        text = text.replace(r"\/", "/")
        if SENSITIVE.search(text.encode("utf-8", errors="surrogatepass")):
            raise ValueError("sensitive_content_in_" + name)
        if any(
            match.group(0).lower() != "01234567-89ab-cdef-0123-456789abcdef"
            for match in NATIVE_THREAD.finditer(text)
        ):
            raise ValueError("native_session_literal_in_" + name)
        if STATION_ID.search(text):
            raise ValueError("station_identity_literal_in_" + name)
        for match in WINDOWS_PATH.finditer(text):
            tail = match.group(2).replace("\\", "/")
            pieces = [p for p in tail.split("/") if p]
            root = pieces[0].casefold() if pieces else ""
            if root == "users":
                if len(pieces) == 1 or pieces[1].casefold() in EXAMPLE_USERS:
                    continue
            if root not in EXAMPLE_ROOTS:
                raise ValueError("local_path_literal_in_" + name)
        for match in HOME_PATH.finditer(text):
            if match.group(1).casefold() not in EXAMPLE_USERS:
                raise ValueError("home_path_literal_in_" + name)
        for match in OPERATIONS_PATH.finditer(text):
            # Public container default, not a machine-specific installation path.
            if match.group(1).casefold() not in {"example", "fixture", "product", "aieyra-control"}:
                raise ValueError("operations_path_literal_in_" + name)
        normalized = text.replace(r"\.", ".").replace("[.]", ".")
        for candidate in re.findall(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])", normalized):
            try:
                address = ipaddress.ip_address(candidate)
            except ValueError:
                continue
            if address.is_global:
                raise ValueError("public_ip_literal_in_" + name)


def sources(include_development=False):
    files = {}
    groups = [
        ("service", {".py", ".json"}),
        ("web", {".js", ".css", ".html", ".svg"}),
        ("desktop", {".cjs", ".html", ".css", ".json", ".png", ".ico"}),
    ]
    if include_development:
        groups += [("cloud", {".py", ".html", ".css", ".js"}), (".github", {".yml", ".yaml"})]
    for folder, suffixes in groups:
        for p in (ROOT / folder).rglob("*"):
            parts = p.relative_to(ROOT).parts
            if (
                p.is_file()
                and p.suffix in suffixes
                and not set(parts) & BLOCKED
                and "test" not in parts
                and p.name != "package-lock.json"
                and ".local." not in p.name
            ):
                files[p.relative_to(ROOT).as_posix()] = p.read_bytes()
    names = [
        "agent-client.py",
        "agent-station.py",
        "enroll-agent.py",
        "update-control.py",
        "verify-release.cjs",
        "matrix-proof.cjs",
        "start-desktop.ps1",
        "migrate-data.py",
        "download-update.py",
    ]
    if include_development:
        names += [
            "build-release.py",
            "export-source.py",
            "build-macos.py",
            "launcher.cs",
            "package-smoke.py",
            "seal-build.py",
            "check-quality.py",
            "check-public-history.py",
        ]
    for name in names:
        files["scripts/" + name] = (ROOT / "scripts" / name).read_bytes()
    for name in ["Start-Control.ps1", "Start-Control.vbs"]:
        files[name] = (ROOT / name).read_bytes()
    for name in ["README.md", "AGENTS.md"]:
        p = ROOT / "docs/distribution" / name
        files[name] = (p if p.exists() else ROOT / name).read_bytes()
    files["config/release-public.pem"] = (ROOT / "config/release-public.pem").read_bytes()
    # Older installed updaters already allow docs/, but not a new root filename.
    security = ROOT / "SECURITY.md"
    if not security.exists():
        security = ROOT / "docs/SECURITY.md"
    files["docs/SECURITY.md"] = security.read_bytes()
    files["docs/FEEDBACK.md"] = (ROOT / "docs/FEEDBACK.md").read_bytes()
    files["docs/agent-access.md"] = (ROOT / "docs/agent-access.md").read_bytes()
    files["docs/AGENT_ADAPTERS.md"] = (ROOT / "docs/AGENT_ADAPTERS.md").read_bytes()
    files["docs/MATRIX_GROWTH.md"] = (ROOT / "docs/MATRIX_GROWTH.md").read_bytes()
    for name in ["DEVELOPMENT.md", "ARCHITECTURE.md", "CHANGELOG.md"]:
        files["docs/" + name] = (ROOT / "docs" / name).read_bytes()
    if include_development:
        files["SECURITY.md"] = security.read_bytes()
        files["docs/SOURCE_PRIVACY.md"] = (ROOT / "docs/SOURCE_PRIVACY.md").read_bytes()
    if include_development and (ROOT / "config/release-baseline.json").exists():
        files["config/release-baseline.json"] = (ROOT / "config/release-baseline.json").read_bytes()
    if include_development and (ROOT / "config/artifact-seal-public.pem").exists():
        files["config/artifact-seal-public.pem"] = (
            ROOT / "config/artifact-seal-public.pem"
        ).read_bytes()
    if include_development:
        files[".gitignore"] = (
            b"data/\nruntime/\n.tools/\n.release/\nbuild/\n__pycache__/\n*.pyc\nnode_modules/\n*.sqlite*\nconfig/*.local.json\n*.pending\n"
        )
    # Signed baselines describe bytes; Git must preserve the exported line endings.
    if include_development:
        files[".gitattributes"] = b"* -text\n"
    if include_development:
        files["cloud/feedback_contract.py"] = (ROOT / "service/feedback_contract.py").read_bytes()
        files["cloud/Dockerfile"] = (ROOT / "cloud/Dockerfile").read_bytes()
        for name in [
            "package.json",
            "package-lock.json",
            "pyproject.toml",
            "requirements-dev.txt",
            ".editorconfig",
            ".prettierrc.json",
            ".prettierignore",
            "desktop/package-lock.json",
            "docs/DEVELOPMENT.md",
            "docs/ARCHITECTURE.md",
            "docs/CHANGELOG.md",
            "tests/web/package.json",
            "tests/web/package-lock.json",
        ]:
            files[name] = (ROOT / name).read_bytes()
        for pattern in [
            "tests/service/test_*.py",
            "tests/service/fixture_os.py",
            "tests/unit/*.test.mjs",
            "tests/desktop-notifications/*.test.cjs",
        ]:
            for path in ROOT.glob(pattern):
                files[path.relative_to(ROOT).as_posix()] = path.read_bytes()
        for name in [
            "office-home.mjs",
            "office-home-live.mjs",
            "cloud-design.mjs",
            "fixture.mjs",
            "run-headless.mjs",
        ]:
            files["tests/web/" + name] = (ROOT / "tests/web" / name).read_bytes()
        for name in ["supervisor.test.cjs", "service-fixture.py"]:
            files["desktop/test/" + name] = (ROOT / "desktop/test" / name).read_bytes()
        files[".gitignore"] += b"tests/test-output/\ndesktop/test-output/\n.ruff_cache/\n"
    validate_public_files(files)
    return files


def export(target):
    if target.exists():
        raise ValueError("export_destination_exists")
    files = sources(True)
    for name, raw in files.items():
        p = target / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(raw)
    return {
        "files": len(files),
        "sha256": {k: hashlib.sha256(v).hexdigest() for k, v in files.items()},
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("target", type=Path)
    args = parser.parse_args()
    print(json.dumps(export(args.target), indent=2))
