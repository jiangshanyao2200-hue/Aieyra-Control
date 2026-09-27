"""Verify pinned Ed25519 release metadata with the packaged Node runtime."""

import json
import os
import shutil
import subprocess
from pathlib import Path


def verify(envelope, root=None):
    root = Path(root or Path(__file__).resolve().parents[1])
    bundled = root / "runtime/electron/electron.exe"
    if not bundled.exists():
        bundled = root / "Aieyra Control.app/Contents/MacOS/Electron"
    executable = os.environ.get("AIEYRA_CONTROL_NODE") or (
        str(bundled) if bundled.exists() else shutil.which("node")
    )
    if not executable:
        raise ValueError("signature_verifier_unavailable")
    env = {**os.environ, "ELECTRON_RUN_AS_NODE": "1"}
    result = subprocess.run(
        [executable, str(root / "scripts/verify-release.cjs")],
        input=json.dumps(envelope),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=12,
        env=env,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    if result.returncode:
        raise ValueError("release_signature_invalid")
    return json.loads(result.stdout)
