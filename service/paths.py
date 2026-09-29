"""Software and private user storage have separate roots."""

from pathlib import Path
import json
import os
import sys

SOURCE_ROOT = Path(__file__).resolve().parents[1]


def installation_root():
    return Path(os.environ.get("AIEYRA_CONTROL_HOME", SOURCE_ROOT)).resolve()


def user_storage_base():
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))


def storage_locator():
    override = os.environ.get("AIEYRA_CONTROL_STORAGE")
    if override:
        target = Path(override)
        if not target.is_absolute():
            raise ValueError("storage_locator_must_be_absolute")
        return target
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "Aieyra Control/storage.json"


def external_path(value):
    target = Path(value)
    if not target.is_absolute():
        raise ValueError("private_storage_must_be_absolute")
    target = target.resolve()
    for software in (installation_root(), SOURCE_ROOT.resolve()):
        if target == software or target.is_relative_to(software):
            raise ValueError("private_storage_must_be_outside_software")
    return target


def data_root():
    override = os.environ.get("AIEYRA_CONTROL_DATA")
    if override:
        return external_path(override)
    locator = storage_locator()
    if locator.exists():
        try:
            value = json.loads(locator.read_text(encoding="utf-8-sig"))
            if value.get("schema_version") != 1 or not isinstance(value.get("data_root"), str):
                raise ValueError()
            target = external_path(value["data_root"])
        except (OSError, ValueError, AttributeError, TypeError) as error:
            raise ValueError("invalid_storage_locator") from error
        if not target.is_dir():
            raise ValueError("configured_storage_unavailable")
        return target
    legacy = installation_root() / "data"
    if legacy.exists() and any(legacy.iterdir()):
        raise ValueError("legacy_storage_requires_explicit_migration")
    return external_path(user_storage_base() / "Aieyra Control/data")


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
