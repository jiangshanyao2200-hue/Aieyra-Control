#!/usr/bin/env python3
"""Build a portable macOS distribution on a native Mac runner."""

import argparse
import hashlib
import importlib.util
import json
import platform
import plistlib
import shutil
import subprocess
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ELECTRON = "42.6.1"
RUNTIMES = {
    "arm64": {
        "electron": "52d96caea8edf9fab6f8c2c2ec77357f907146a60a1a1360f9b9079ab0df57b8",
        "python": "064afb7c2fc0bbf511d886288adf98696af5105e36c138cdf2c199c0146fcf68",
        "target": "aarch64",
    },
    "x64": {
        "electron": "1c2856c659089734004b6abcffa216a9b68dbbad2f683d8e611f122e01b06ada",
        "python": "327814efd865a0b6a99c149b12a261e9d0ad409183515c745d41bda2d07282e9",
        "target": "x86_64",
    },
}


def fetch(url, target, sha):
    with urllib.request.urlopen(url, timeout=90) as r, target.open("wb") as f:
        shutil.copyfileobj(r, f)
    if hashlib.sha256(target.read_bytes()).hexdigest() != sha:
        raise ValueError("runtime_hash_mismatch")


def build(output, arch, baseline=None):
    if platform.system() != "Darwin" or (platform.machine() == "arm64") != (arch == "arm64"):
        raise ValueError("native_mac_runner_required")
    spec = importlib.util.spec_from_file_location(
        "source_export", ROOT / "scripts/export-source.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    output.mkdir(parents=True, exist_ok=True)
    stage = output / "Aieyra Control"
    if stage.exists():
        raise ValueError("output_exists")
    stage.mkdir()
    files = mod.sources()
    for name, data in files.items():
        p = stage / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    if baseline:
        expected = json.loads(baseline.read_text(encoding="utf-8"))["manifest"]["files"]
        if set(files) != set(expected):
            raise ValueError("baseline_file_set_mismatch")
        for name, raw in files.items():
            if (
                len(raw) != expected[name]["size"]
                or hashlib.sha256(raw).hexdigest() != expected[name]["sha256"]
            ):
                raise ValueError("baseline_source_mismatch_" + name)
        shutil.copy2(baseline, stage / "config/release-baseline.json")
    downloads = output / "downloads"
    downloads.mkdir()
    runtime = RUNTIMES[arch]
    electron = downloads / "electron.zip"
    fetch(
        f"https://github.com/electron/electron/releases/download/v{ELECTRON}/electron-v{ELECTRON}-darwin-{arch}.zip",
        electron,
        runtime["electron"],
    )
    subprocess.run(["ditto", "-x", "-k", str(electron), str(downloads / "electron")], check=True)
    app = stage / "Aieyra Control.app"
    shutil.move(str(downloads / "electron/Electron.app"), app)
    for name in ("LICENSE", "LICENSES.chromium.html", "version"):
        if (downloads / "electron" / name).exists():
            shutil.copy2(downloads / "electron" / name, stage / ("Electron-" + name))
    plist = app / "Contents/Info.plist"
    with plist.open("rb") as f:
        info = plistlib.load(f)
    info.update(
        CFBundleDisplayName="Aieyra Control",
        CFBundleName="Aieyra Control",
        CFBundleIdentifier="cn.aieyra.control",
        CFBundleShortVersionString=json.loads((ROOT / "desktop/package.json").read_text())[
            "version"
        ],
    )
    with plist.open("wb") as f:
        plistlib.dump(info, f)
    (app / "Contents/Resources/default_app.asar").unlink(missing_ok=True)
    bootstrap = app / "Contents/Resources/app"
    bootstrap.mkdir()
    (bootstrap / "package.json").write_text(
        json.dumps(
            {
                "name": "aieyra-control",
                "version": info["CFBundleShortVersionString"],
                "main": "main.cjs",
            }
        )
    )
    (bootstrap / "main.cjs").write_text(
        "require(require('node:path').resolve(process.resourcesPath, '../../../desktop/main.cjs'));\n"
    )
    python = downloads / "python.tar.gz"
    fetch(
        "https://github.com/astral-sh/python-build-standalone/releases/download/20260924/cpython-3.13.15%2B20260924-"
        + runtime["target"]
        + "-apple-darwin-install_only_stripped.tar.gz",
        python,
        runtime["python"],
    )
    (stage / "runtime").mkdir()
    with tarfile.open(python) as t:
        t.extractall(stage / "runtime", filter="data")
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(app)], check=True)
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
    subprocess.run(
        [
            str(stage / "runtime/python/bin/python3"),
            str(ROOT / "scripts/package-smoke.py"),
            "--root",
            str(stage),
            "--mac",
        ],
        check=True,
    )
    for p in stage.rglob("__pycache__"):
        shutil.rmtree(p)
    if (stage / "data").exists():
        shutil.rmtree(stage / "data")
    # Tests use temporary data and never enter the distributed archive.
    version = info["CFBundleShortVersionString"]
    archive = output / f"aieyra-control-{version}-macos-{arch}.zip"
    subprocess.run(
        ["ditto", "-c", "-k", "--keepParent", "--sequesterRsrc", str(stage), str(archive)],
        check=True,
    )
    receipt = {
        "platform": "macos-" + arch,
        "version": version,
        "native_runner": platform.machine(),
        "signed": "ad-hoc",
        "notarized": False,
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "size": archive.stat().st_size,
        "startup_verified": True,
    }
    (output / "build-receipt.json").write_text(json.dumps(receipt, indent=2))
    return receipt


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--arch", choices=RUNTIMES, required=True)
    p.add_argument("--baseline", type=Path)
    a = p.parse_args()
    print(json.dumps(build(a.output, a.arch, a.baseline), indent=2))
