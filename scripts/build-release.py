#!/usr/bin/env python3
"""Build explicit, sanitized source + Windows portable artifacts with Ed25519 metadata."""

import argparse
import base64
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import time
import zipfile
from pathlib import Path
from cryptography.hazmat.primitives import serialization

ROOT = Path(__file__).resolve().parents[1]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def build(output, key_path, python_dir, electron_dir, sequence, version=None):
    package_version = json.loads((ROOT / "desktop/package.json").read_text(encoding="utf-8"))[
        "version"
    ]
    version = version or package_version
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("invalid_release_version")
    if version != package_version:
        raise ValueError("release_version_must_match_desktop")
    service_version = re.search(
        r'^VERSION = "([^"]+)"', (ROOT / "service/main.py").read_text(encoding="utf-8"), re.M
    )
    if not service_version or service_version[1] != version:
        raise ValueError("release_version_must_match_service")
    if (output / "stable.json").exists():
        raise ValueError("release_output_already_exists")
    output.mkdir(parents=True, exist_ok=True)
    spec = importlib.util.spec_from_file_location(
        "source_export", ROOT / "scripts/export-source.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    files = module.sources()
    source = output / f"aieyra-control-{version}-source.zip"
    with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED, compresslevel=7) as z:
        for name, data in sorted(files.items()):
            z.writestr(name, data)
    manifest = {
        "schema": 1,
        "product": "aieyra-control",
        "version": version,
        "sequence": sequence,
        "created_at": int(time.time()),
        "minimum_updater": 1,
        "notes": "项目记忆与工位租约状态同步；修复账号异步刷新、反馈校验与断开核验；精简界面残留并统一源码、测试和开发规范。",
        "source": {
            "path": "/artifacts/" + source.name,
            "sha256": digest(source.read_bytes()),
            "size": source.stat().st_size,
        },
        "files": {
            name: {"sha256": digest(data), "size": len(data)}
            for name, data in sorted(files.items())
        },
    }
    key = serialization.load_pem_private_key(key_path.read_bytes(), None)

    def envelope(value):
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        return {
            "payload": base64.b64encode(raw).decode(),
            "signature": base64.b64encode(key.sign(raw)).decode(),
            "manifest": value,
        }

    baseline = json.dumps(envelope(manifest), ensure_ascii=False, indent=2).encode()
    (output / "release-baseline.json").write_bytes(baseline)
    compiler = (
        Path(os.environ.get("WINDIR", "C:/Windows"))
        / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    )
    launcher = output / "Aieyra Control.exe"
    subprocess.run(
        [
            str(compiler),
            "/nologo",
            "/target:winexe",
            "/platform:anycpu",
            "/reference:System.Windows.Forms.dll",
            "/reference:System.Core.dll",
            "/win32icon:" + str(ROOT / "desktop/assets/icon.ico"),
            "/out:" + str(launcher),
            str(ROOT / "scripts/launcher.cs"),
        ],
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    portable = output / f"aieyra-control-{version}-windows-x64.zip"
    with zipfile.ZipFile(portable, "w", zipfile.ZIP_DEFLATED, compresslevel=5) as z:
        for name, data in sorted(files.items()):
            z.writestr("Aieyra Control/" + name, data)
        z.writestr("Aieyra Control/config/release-baseline.json", baseline)
        z.write(launcher, "Aieyra Control/Aieyra Control.exe")
        for dirname, origin in [("python", python_dir), ("electron", electron_dir)]:
            for p in origin.rglob("*"):
                if p.is_file() and "__pycache__" not in p.parts:
                    z.write(
                        p,
                        "Aieyra Control/runtime/"
                        + dirname
                        + "/"
                        + p.relative_to(origin).as_posix(),
                    )
    manifest["portable"] = {
        "path": "/artifacts/" + portable.name,
        "sha256": digest(portable.read_bytes()),
        "size": portable.stat().st_size,
    }
    manifest["platforms"] = {"windows-x64": manifest["portable"]}
    (output / "stable.json").write_text(
        json.dumps(envelope(manifest), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    receipt = {
        "version": version,
        "source_files": len(files),
        "source": manifest["source"],
        "portable": manifest["portable"],
    }
    (output / "build-receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    return receipt


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--key", type=Path, required=True)
    p.add_argument("--python-dir", type=Path, required=True)
    p.add_argument("--electron-dir", type=Path, required=True)
    p.add_argument("--sequence", type=int, required=True)
    p.add_argument("--version", help="Defaults to desktop/package.json; must match service")
    a = p.parse_args()
    print(
        json.dumps(
            build(a.output, a.key, a.python_dir, a.electron_dir, a.sequence, a.version),
            ensure_ascii=False,
        )
    )
