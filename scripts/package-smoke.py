#!/usr/bin/env python3
"""Exercise a packaged service and native host without displaying a window."""

import argparse
import json
import os
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path


def smoke(root, mac=False):
    with tempfile.TemporaryDirectory(prefix="control-package-") as temporary:
        temporary = Path(temporary)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        python = root / ("runtime/python/bin/python3" if mac else "runtime/python/python.exe")
        executable = root / (
            "Aieyra Control.app/Contents/MacOS/Electron" if mac else "runtime/electron/electron.exe"
        )
        env = {**os.environ, "AIEYRA_CONTROL_HOME": str(temporary), "ELECTRON_RUN_AS_NODE": "1"}
        probe = subprocess.run(
            [
                str(executable),
                "-e",
                "process.stdout.write(JSON.stringify({version:process.versions.electron,platform:process.platform}))",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if probe.returncode:
            raise RuntimeError("packaged_electron_node_failed")
        print("Electron runtime " + probe.stdout, flush=True)
        env.pop("ELECTRON_RUN_AS_NODE")
        env["PYTHONUTF8"] = "1"
        env["AIEYRA_CONTROL_NODE"] = str(executable)
        log = temporary / "service.log"
        with log.open("w") as f:
            process = subprocess.Popen(
                [
                    str(python),
                    str(root / "service/main.py"),
                    "--port",
                    str(port),
                    "--data-dir",
                    str(temporary / "data/shared"),
                    "--config",
                    str(temporary / "data/config/control.json"),
                ],
                env=env,
                stdout=f,
                stderr=f,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            try:
                for _ in range(50):
                    if process.poll() is not None:
                        raise RuntimeError("packaged_service_failed: " + log.read_text()[-2000:])
                    try:
                        value = json.load(
                            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1)
                        )
                        if (
                            value.get("version")
                            != json.loads(
                                (root / "desktop/package.json").read_text(encoding="utf-8")
                            )["version"]
                        ):
                            raise RuntimeError("wrong_version")
                        break
                    except (OSError, ValueError):
                        time.sleep(0.2)
                else:
                    raise RuntimeError("service_start_timeout")
                for route in ("/api/snapshot", "/api/registry", "/api/cloud"):
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}" + route, timeout=5) as r:
                        json.load(r)
                if not (temporary / "data/shared/office.sqlite").exists():
                    raise RuntimeError("shared_data_missing")
                print("PASS packaged Python, SQLite, read APIs and portable storage", flush=True)
            finally:
                process.terminate()
                process.wait(timeout=15)
        if mac:
            # App bootstrap resolves the external portable source tree correctly.
            host_log = (temporary / "host.log").open("w")
            env["AIEYRA_CONTROL_PACKAGE_TEST"] = "1"
            env["AIEYRA_CONTROL_PLATFORM_TEST"] = "1"
            host = subprocess.Popen(
                [
                    str(executable),
                    "--hidden",
                    "--port=" + str(port),
                    "--user-data-dir=" + str(temporary / "host"),
                    "--service-data-dir=" + str(temporary / "host-shared"),
                    "--config=" + str(temporary / "data/config/control.json"),
                ],
                env=env,
                stdout=host_log,
                stderr=host_log,
            )
            try:
                for _ in range(100):
                    if host.poll() is not None:
                        raise RuntimeError(
                            "native_host_exited: " + (temporary / "host.log").read_text()[-6000:]
                        )
                    try:
                        json.load(
                            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1)
                        )
                        break
                    except OSError:
                        time.sleep(0.2)
                else:
                    raise RuntimeError(
                        "native_host_start_failed: " + (temporary / "host.log").read_text()[-6000:]
                    )
                print("PASS native macOS app bootstrap and service lifecycle", flush=True)
            finally:
                host.terminate()
                host.wait(timeout=15)
                host_log.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--mac", action="store_true")
    a = p.parse_args()
    smoke(a.root.resolve(), a.mac)
