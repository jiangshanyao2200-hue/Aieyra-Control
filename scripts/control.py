#!/usr/bin/env python3
"""Headless Control and the native Link runtime, without a desktop dependency."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
LINK_REPOSITORY = "https://github.com/jiangshanyao2200-hue/Aieyra-OS.git"
LINK_REVISION = "ed6201cfe44cbfbfb51915e38d37bbfdea410248"
LINK_VERSION = "0.3.0"
sys.path.insert(0, str(ROOT / "service"))
from paths import data_root, external_path


def native_target():
    android = hasattr(sys, "getandroidapilevel") or bool(os.environ.get("TERMUX_VERSION"))
    system = (
        "android"
        if android
        else {"win32": "windows", "darwin": "darwin"}.get(sys.platform, sys.platform)
    )
    machine = platform.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)
    if system not in {"linux", "android", "windows", "darwin"} or arch is None:
        raise ValueError("native_link_requires_a_supported_64_bit_platform")
    return system, arch


def link_path():
    override = os.environ.get("AIEYRA_LINK_BINARY")
    target = (
        Path(override)
        if override
        else ROOT / "runtime/link" / ("aieyra-link.exe" if os.name == "nt" else "aieyra-link")
    )
    if not target.is_absolute():
        raise ValueError("link_binary_must_be_absolute")
    return target


def command(argv, *, cwd=None, env=None, timeout=600):
    result = subprocess.run(
        [str(item) for item in argv],
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        # Do not echo environment, credentials, Git helpers, or arbitrary subprocess output.
        raise ValueError(Path(str(argv[0])).stem + "_failed_check_installed_toolchain_and_network")
    return result.stdout.strip()


def verify_link(target):
    version = command([target, "version"], timeout=15)
    if version != "Aieyra Link " + LINK_VERSION:
        raise ValueError("link_version_mismatch")
    return {"version": LINK_VERSION, "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}


def install_link(output=None):
    """Build the pinned public implementation for this machine; never replace a runtime."""
    system, arch = native_target()
    target = Path(output).expanduser().absolute() if output else link_path()
    if target.exists() or target.is_symlink():
        raise ValueError("link_output_exists_preserve_it_or_choose_a_new_output")
    git, go = shutil.which("git"), shutil.which("go")
    if not git or not go:
        raise ValueError("install_git_and_go_first")
    env = dict(os.environ, CGO_ENABLED="0", GOOS=system, GOARCH=arch, GOFLAGS="", GOWORK="off")
    if system == "android":
        # Official Go auto-downloads are not a replacement for Termux's patched toolchain.
        env["GOTOOLCHAIN"] = "local"
    with tempfile.TemporaryDirectory(prefix="aieyra-link-build-") as directory:
        source = Path(directory) / "source"
        source.mkdir()
        command([git, "init", "-q"], cwd=source)
        command(
            [
                git,
                "sparse-checkout",
                "set",
                "CORE/link",
                "CORE/internal/privatefile",
                "CORE/cmd/aieyra-link",
            ],
            cwd=source,
        )
        command(
            [git, "fetch", "--depth=1", "--filter=blob:none", LINK_REPOSITORY, LINK_REVISION],
            cwd=source,
        )
        command([git, "checkout", "--detach", "FETCH_HEAD"], cwd=source)
        if command([git, "rev-parse", "HEAD"], cwd=source) != LINK_REVISION:
            raise ValueError("link_source_revision_mismatch")
        candidate = Path(directory) / target.name
        command(
            [go, "build", "-mod=readonly", "-trimpath", "-o", candidate, "./cmd/aieyra-link"],
            cwd=source / "CORE",
            env=env,
        )
        verified = verify_link(candidate)
        target.parent.mkdir(parents=True, exist_ok=True)
        # A same-directory candidate plus exclusive link protects an existing installation.
        fd, name = tempfile.mkstemp(prefix=".link-", dir=target.parent)
        staging = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(candidate.read_bytes())
                stream.flush()
                os.fsync(stream.fileno())
            staging.chmod(0o755)
            os.link(staging, target)
        finally:
            staging.unlink(missing_ok=True)
    return {
        "status": "installed",
        "binary": str(target),
        "platform": system,
        "arch": arch,
        "source_revision": LINK_REVISION,
        **verified,
    }


def doctor():
    import sqlite3

    result = {
        "status": "ready",
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "platform": native_target()[0],
        "architecture": native_target()[1],
        "data_root": str(data_root()),
        "desktop_required": False,
        "link": {"status": "not_installed", "required_for_lan": True},
    }
    binary = link_path()
    if binary.is_file():
        try:
            result["link"] = {"status": "ready", "binary": str(binary), **verify_link(binary)}
        except (OSError, ValueError, subprocess.SubprocessError):
            result["link"] = {"status": "unusable", "binary": str(binary)}
            result["status"] = "attention_required"
    return result


def run_service(port, storage=None):
    if not 1 <= port <= 65535:
        raise ValueError("invalid_port")
    if storage is not None:
        os.environ["AIEYRA_CONTROL_DATA"] = str(
            external_path(Path(storage).expanduser().absolute())
        )
    if os.name != "nt":
        os.umask(0o077)

    def terminate(_signum, _frame):
        raise KeyboardInterrupt

    # The service's existing finally block closes Link children, workers and its data lock.
    signal.signal(signal.SIGTERM, terminate)
    spec = importlib.util.spec_from_file_location(
        "headless_control_service", ROOT / "service/main.py"
    )
    service = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(service)
    sys.argv = [str(ROOT / "service/main.py"), "--port", str(port)]
    return service.main() or 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("doctor", help="Check Python, private storage and the optional Link runtime")
    run = sub.add_parser("run", help="Run the loopback office in the foreground; Ctrl-C to stop")
    run.add_argument("--port", type=int, default=17910)
    run.add_argument("--data-root", type=Path)
    install = sub.add_parser(
        "install-link", help="Build pinned Link source with Git and Go 1.26.8+"
    )
    install.add_argument("--output", type=Path)
    link = sub.add_parser("link", help="Run the installed native Link CLI in the foreground")
    link.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        if sys.version_info < (3, 11):
            raise ValueError("python_3_11_or_newer_required")
        if args.action == "run":
            return run_service(args.port, args.data_root)
        if args.action == "link":
            binary = link_path()
            verify_link(binary)
            arguments = args.arguments
            if arguments[:1] == ["--"]:
                arguments = arguments[1:]
            os.execv(str(binary), [str(binary), *(arguments or ["help"])])
        result = install_link(args.output) if args.action == "install-link" else doctor()
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["status"] != "attention_required" else 1
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        code = str(error) if isinstance(error, ValueError) else "local_runtime_or_build_unavailable"
        print(json.dumps({"status": "error", "code": code}), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
