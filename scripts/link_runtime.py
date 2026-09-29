"""Verify an explicitly selected Link runtime before adding it to a release."""

import hashlib
import re
import subprocess
from pathlib import Path

LINK_VERSION = "0.3.0"


def verify_link(binary, expected_sha256):
    binary = Path(binary)
    if not binary.is_absolute() or not binary.is_file():
        raise ValueError("link_runtime_absolute_file_required")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256 or ""):
        raise ValueError("link_runtime_expected_sha256_required")
    raw = binary.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected_sha256:
        raise ValueError("link_runtime_hash_mismatch")
    result = subprocess.run(
        [str(binary), "version"],
        capture_output=True,
        text=True,
        timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode or result.stdout.strip() != "Aieyra Link " + LINK_VERSION:
        raise ValueError("link_runtime_version_mismatch")
    return {"version": LINK_VERSION, "sha256": actual, "size": len(raw)}
