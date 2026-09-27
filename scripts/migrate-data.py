#!/usr/bin/env python3
"""Copy a stopped legacy installation into portable storage, preserving the original."""

import argparse
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "service"))
from main import InstanceLock


def migrate(source, destination, config=None, desktop=None):
    source = source.resolve()
    destination = destination.resolve()
    if not source.is_dir() or source == destination or source in destination.parents:
        raise ValueError("invalid_migration_paths")
    lock = InstanceLock(source)
    try:
        if destination.exists() and any(destination.iterdir()):
            raise ValueError("destination_must_be_empty")
        destination.mkdir(parents=True, exist_ok=True)
        shared = destination / "shared"
        shared.mkdir()
        for name in ("control.sqlite", "office.sqlite"):
            original = source / name
            if not original.is_file():
                continue
            with sqlite3.connect(original) as src, sqlite3.connect(shared / name) as dst:
                src.backup(dst)
                if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("database_integrity_failed")
        for name in ("agents", "logs", "updates"):
            if (source / name).exists():
                shutil.copytree(source / name, destination / name)
        for name in ("artifacts", "os-sessions"):
            if (source / name).exists():
                shutil.copytree(source / name, shared / name)
        for name in (
            "config",
            "shared",
            "agents",
            "desktop",
            "logs",
            "cache",
            "updates",
            "backups",
        ):
            (destination / name).mkdir(exist_ok=True)
        if config and config.exists():
            shutil.copy2(config, destination / "config/control.json")
        if desktop and desktop.exists():
            shutil.copytree(
                desktop,
                destination / "desktop",
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(
                    "Singleton*", "LOCK", "lockfile", "Cache", "Code Cache", "GPUCache", "Crashpad"
                ),
            )
        receipt = {
            "migrated_at": int(time.time()),
            "source": str(source),
            "destination": str(destination),
            "source_preserved": True,
            "databases_verified": True,
        }
        (destination / "backups/migration.json").write_text(
            json.dumps(receipt, indent=2), encoding="utf-8"
        )
        return receipt
    finally:
        lock.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--from", dest="source", type=Path, required=True)
    p.add_argument("--to", dest="target", type=Path, default=ROOT / "data")
    p.add_argument("--config", type=Path)
    p.add_argument("--desktop", type=Path)
    a = p.parse_args()
    print(json.dumps(migrate(a.source, a.target, a.config, a.desktop), indent=2))
