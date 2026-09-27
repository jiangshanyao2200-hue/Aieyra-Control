"""Portable installation paths. Project working directories stay where they are."""

from pathlib import Path
import json
import os

SOURCE_ROOT = Path(__file__).resolve().parents[1]


def installation_root():
    return Path(os.environ.get("AIEYRA_CONTROL_HOME", SOURCE_ROOT)).resolve()


def data_root():
    return installation_root() / "data"


def shared_directory():
    return data_root() / "shared"


def configuration_file():
    return data_root() / "config" / "control.json"


def initialize(configuration=None):
    root = data_root()
    for name in ("config", "shared", "agents", "desktop", "logs", "cache", "updates", "backups"):
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "shared/projects").mkdir(exist_ok=True)
    target = configuration_file()
    if not target.exists():
        value = configuration or {
            "coordination_mode": "local",
            "os_runtime_registration": False,
            "hub_reconnect": False,
            "runtime_bindings": [],
            "human_projects": [],
            "resources": [],
            "local_projects": [
                {
                    "id": "control",
                    "name": "Aieyra Control",
                    "root": str(installation_root()),
                    "source": "README.md",
                }
            ],
        }
        # Exclusive creation preserves a concurrently created user configuration.
        try:
            with target.open("x", encoding="utf-8") as f:
                json.dump(value, f, ensure_ascii=False, indent=2)
        except FileExistsError:
            pass
    return root
