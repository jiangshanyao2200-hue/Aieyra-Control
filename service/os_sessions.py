"""本产品Matrix的本机登记；只有原Bridge执行器可以消费任务。"""

import hashlib
import json
from pathlib import Path
import re
import threading


def read_json(path):
    with path.open("rb") as stream:
        raw = stream.read(32769)
    if len(raw) > 32768:
        raise ValueError("local_registration_too_large")
    value = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("local_registration_object_required")
    return value, hashlib.sha256(raw).hexdigest()


def bridge_config(source):
    # 登记和策略均固定于一次启动。更换发现文件或改策略不能沿用旧动作身份。
    if source.get("registration_ref"):
        _, checksum = read_json(Path(source["registration_ref"]))
        if checksum != source["registration_sha256"]:
            raise ValueError("os_registration_changed")
    value, checksum = read_json(Path(source["config_ref"]))
    if source.get("config_sha256") and checksum != source["config_sha256"]:
        raise ValueError("os_registration_policy_changed")
    return value


class OSSessions:
    def __init__(self, config, data_dir, product_root, store):
        self.store = store
        self.static = list(config.get("collaboration_sources", []))
        self.directory = Path(data_dir).resolve() / "os-sessions"
        self.adapter = product_root / "service/product_bridge"
        os_root = (
            product_root.parent
            if product_root.name == "CONTROL"
            else product_root.parent / "Aieyra OS"
        )
        self.executables = {str((os_root / "Aieyra OS.exe").resolve()).casefold()}
        for item in config.get("os_runtime_executables", []):
            if not isinstance(item, str) or not Path(item).is_absolute():
                raise ValueError("explicit_os_executable_required")
            self.executables.add(str(Path(item).resolve()).casefold())
        self.enabled = config.get("os_runtime_registration", True) is True
        self.lock = threading.Lock()
        self.dynamic = {}
        if self.enabled:
            with store.db() as db:
                for row in db.execute(
                    "SELECT payload FROM cache WHERE id LIKE 'os-registration:%' ORDER BY id DESC LIMIT 16"
                ):
                    saved = json.loads(row[0])
                    if saved.get("runtime_executable", "").casefold() in self.executables:
                        saved["adapter_dir"] = str(self.adapter)
                        self.dynamic[saved["id"]] = saved
        self.refresh()

    def refresh(self):
        if not self.enabled:
            return
        # 仅本服务既有数据目录，最多保留16个登记；不扫描用户配置或其他进程。
        try:
            paths = sorted(
                self.directory.glob("os-*/source.json"),
                key=lambda p: p.stat().st_mtime_ns,
                reverse=True,
            )[:16]
        except OSError:
            return
        rows = {}
        for path in paths:
            try:
                item, checksum = read_json(path)
                if set(item) != {
                    "version",
                    "id",
                    "session_id",
                    "pid",
                    "executable",
                    "created_at",
                    "config_sha256",
                }:
                    raise ValueError("invalid_os_registration")
                if (
                    item["version"] != 1
                    or not re.fullmatch(r"os-[a-f0-9]{40}", item["id"])
                    or path.parent.name != item["id"]
                ):
                    raise ValueError("invalid_os_registration")
                if (
                    not Path(item["executable"]).is_absolute()
                    or str(Path(item["executable"]).resolve()).casefold() not in self.executables
                ):
                    raise ValueError("foreign_os_executable")
                source = dict(
                    id=item["id"],
                    name="Matrix",
                    project="os",
                    adapter_dir=str(self.adapter),
                    runtime_executable=str(Path(item["executable"]).resolve()),
                    config_ref=str(path.parent / "bridge.json"),
                    config_sha256=item["config_sha256"],
                    registration_ref=str(path),
                    registration_sha256=checksum,
                )
                value = bridge_config(source)
                expected = dict(
                    expected_session=item["session_id"],
                    expected_pid=item["pid"],
                    expected_executable=item["executable"],
                    expected_created_at=item["created_at"],
                    state_dir=str(path.parent / "bridge"),
                    work_root=str(path.parent / "work"),
                )
                if (
                    any(value.get(key) != val for key, val in expected.items())
                    or value.get("mode") != "os"
                ):
                    raise ValueError("os_registration_binding_mismatch")
                # 同一持久登记不能通过服务重启换策略；共用Control原缓存账本。
                key = "os-registration:" + item["id"]
                with self.store.db() as db:
                    db.execute("INSERT OR IGNORE INTO cache VALUES(?,?)", (key, json.dumps(source)))
                    rows[item["id"]] = json.loads(
                        db.execute("SELECT payload FROM cache WHERE id=?", (key,)).fetchone()[0]
                    )
                    rows[item["id"]]["adapter_dir"] = str(self.adapter)
            except (OSError, ValueError, TypeError, KeyError):
                continue
        with self.lock:
            # 原登记损坏/消失仍投影旧事实并标离线，不能静默隐藏在途任务。
            for key, row in rows.items():
                if key not in self.dynamic:
                    self.dynamic[key] = row

    def sources(self):
        with self.lock:
            known = {x["id"] for x in self.static}
            extra = [dict(x) for k, x in self.dynamic.items() if k not in known]
            limit = max(0, 16 - len(self.static))
            return self.static + (extra[-limit:] if limit else [])
