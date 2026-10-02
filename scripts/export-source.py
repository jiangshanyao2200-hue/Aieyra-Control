#!/usr/bin/env python3
"""Export public source from a strict allowlist, never a working-tree archive."""

import argparse
import hashlib
import ipaddress
import json
import re
from pathlib import Path
from urllib.parse import unquote

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
    "私有数据",
    "Aieyra 共享库",
    ".codex",
}
SENSITIVE = re.compile(
    rb"gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----|\bsk-[A-Za-z0-9_-]{24,}|\bAKIA[A-Z0-9]{16}\b|\bxox[baprs]-[A-Za-z0-9-]{15,}"
)
BINARY_ASSETS = {
    "cloud/site/assets/scene-01-clean.webp": "999c963607004dcbb0eb67a38011fb85495c58626dcd87a94fc64f494735ba07",
    "cloud/site/assets/scene-01-clean-small.webp": "858bf4b27f889ffa2346a20ed004e93e62cc29eaf09b70f47bdad63657eab81f",
    "cloud/site/assets/scene-02-clean.webp": "d2b2c487940f2287b94454a7c545246fbb58ce386516cff5eb88ef0f49c65790",
    "cloud/site/assets/scene-02-clean-small.webp": "eb803fdb989e021d53ccc476657b9db3c5fb1b1359d2ef866252c86057defee2",
    "cloud/site/assets/scene-03-clean.webp": "0a39b664249239eaac7f90ff3051abcbd3b046b24cde7e542b446858060adb06",
    "cloud/site/assets/scene-03-clean-small.webp": "34bc27876e601460af612aaf7d11070f67bba3ff0640ff5e4b3185c716fb6316",
    "cloud/site/assets/scene-05-clean.webp": "cfb6f1c4fb833e182f67882d5b980824d930797d22a9fca5a6a3d4fcb6af83ee",
    "cloud/site/assets/scene-05-clean-small.webp": "6f061c3acd3face43650ec341f0211bae03d03681310b79ecde6e7e949514b91",
    "cloud/site/assets/scene-01.webp": "1981185c3ba4cdb358262fb361f41b7efeb01dfc82b444a141a16f02cc985e43",
    "cloud/site/assets/scene-01-small.webp": "c7444bcfe7505e0cd8745d58d5b389f590c3cf008dc90426c09d4e2a8f7686a6",
    "cloud/site/assets/scene-02.webp": "ba11f7a0369c909f424f036f72cb89539b773e1f5c9b16ad07e2f4a69167d2da",
    "cloud/site/assets/scene-02-small.webp": "233a17da2787b216fefdc8919e91907fa577046ad15c6d24a6e0894861b19d44",
    "cloud/site/assets/scene-03.webp": "fe3ddc5af78d42ff7fd14484275e6816a22204d81dca5027ad0e4ff24cca0d0c",
    "cloud/site/assets/scene-03-small.webp": "906ee25f17c2f7b270944966e4ecdfe007649339139d03a02ae7e1b2b5a7f908",
    "cloud/site/assets/scene-04.webp": "6b50ad3a52dcaf34eaca6175818ae4ac318473f55d7c098d1da3753036fdbd26",
    "cloud/site/assets/scene-04-small.webp": "c6f82a3c1903d971e1410c91d9548b112d52f8e96677aa1a93b90ddb0b9c55e5",
    "cloud/site/assets/scene-05.webp": "d49411c91a42cab47ca5df51493a03725633d8fa9ab0d61ead0460d6d3cd85ed",
    "cloud/site/assets/scene-05-small.webp": "a3eaf88dad3567b6e3413ee6a6e9119fc6348e88048defc762c7110c5e81d390",
    "cloud/site/assets/scene-06.webp": "4f0ddc3d8ffb75f972bba0ef158764e3272a1804634e34aec00a0b8ecfb773f4",
    "cloud/site/assets/scene-06-small.webp": "3b77b9b02602c5d286739de9651892cbcb59d152c29a2127cea4e5bc318a07fc",
    "cloud/site/assets/scene-07.webp": "36aed7d9382aa4c7ab168193a1f77ddbd3b096d91e54f1694e6f2694a061b1b6",
    "cloud/site/assets/scene-07-small.webp": "c80d6fd4d16efaae02fbafea9e169ba0408629a3d0f50cb065f8eabb70897162",
    "cloud/site/assets/scene-08.webp": "8683f65f159174c73d0b21533406b9e46f0041d849678f6d009166f81393bb00",
    "cloud/site/assets/scene-08-small.webp": "035e9a3ae5e06c0922b22b4478a4655b3ebbb6c6eb07a370bf2286401a1af023",
    "cloud/site/assets/collaboration.webp": "5b1d27f46b6210d5ec5ee6f765ef6ac1af071fa6c584df2ba84d1b98b0ca5569",
    "cloud/site/assets/collaboration-small.webp": "74fd0b63211c029acfd194e8fec0239fce53ff5114ac64d4928cbe5146cf7788",
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
DOCUMENTATION_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "2001:db8::/32")
)
IPV6_LITERAL = re.compile(
    r"(?<![\w:])[0-9a-fA-F]*(?::[0-9a-fA-F]*){2,}(?:\.[0-9.]+)?"
    r"(?:%[A-Za-z0-9_.-]+)?(?![\w:])"
)


def is_documentation_address(address):
    """Only loopback, wildcard binds and reserved documentation are exempt."""
    if address.version == 6 and address.ipv4_mapped:
        return is_documentation_address(address.ipv4_mapped)
    return (
        address.is_loopback
        or address.is_unspecified
        or any(
            address.version == network.version and address in network
            for network in DOCUMENTATION_NETWORKS
        )
    )


def validate_public_files(files):
    """Fail closed on known private metadata forms; never print matched values."""
    for name, raw in files.items():
        parts = Path(name).parts
        if any(part.casefold() == ".env" or part.casefold().startswith(".env.") for part in parts):
            raise ValueError("private_environment_path_in_" + name)
        if set(parts) & BLOCKED or Path(name).is_absolute() or ".." in parts:
            raise ValueError("private_artifact_path_in_" + name)
        if Path(name).suffix in {".png", ".ico", ".webp"}:
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
        text = unquote(text.replace(r"\/", "/"))
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
            if not is_documentation_address(address):
                raise ValueError("unreviewed_ip_literal_in_" + name)
        for match in IPV6_LITERAL.finditer(normalized):
            try:
                address = ipaddress.IPv6Address(match.group())
            except ValueError:
                continue
            if not is_documentation_address(address):
                raise ValueError("unreviewed_ip_literal_in_" + name)


def sources(include_development=False):
    files = {}
    groups = [
        ("service", {".py", ".json"}),
        ("web", {".js", ".css", ".html", ".svg"}),
        ("desktop", {".cjs", ".html", ".css", ".json", ".png", ".ico"}),
    ]
    if include_development:
        groups += [
            ("cloud", {".py", ".html", ".css", ".js", ".webp"}),
            (".github", {".yml", ".yaml"}),
        ]
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
                if not p.resolve().is_relative_to(ROOT.resolve()):
                    raise ValueError("source_link_outside_software")
                files[p.relative_to(ROOT).as_posix()] = p.read_bytes()
    names = [
        "agent-client.py",
        "agent-station.py",
        "control.py",
        "station-transfer.py",
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
            "link_runtime.py",
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
    files["docs/LINK.md"] = (ROOT / "docs/LINK.md").read_bytes()
    files["docs/LINUX_TERMUX.md"] = (ROOT / "docs/LINUX_TERMUX.md").read_bytes()
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
        files["cloud/release-public.pem"] = (ROOT / "config/release-public.pem").read_bytes()
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
