"""Bounded local Agent discovery; metadata only, never secrets or private sessions."""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
import subprocess


def observed_at():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def discover(config, runner=subprocess.run):
    stamp = observed_at()
    rows = []
    executable = config.get("codex_executable")
    if isinstance(executable, str) and executable and len(executable) <= 1024:
        path = Path(executable)
        row = {"id": "local-codex-cli", "name": "Codex CLI", "kind": "codex-cli",
               "source": "configured executable", "actions": ["inspect"], "observed_at": stamp}
        try:
            if not path.is_file():
                raise OSError("executable_missing")
            result = runner([str(path), "--version"], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=5,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            label = (result.stdout or "").strip().splitlines()[0][:120] if result.returncode == 0 else ""
            if not label:
                raise ValueError("version_unavailable")
            row.update(availability="available", version_label=label)
        except (OSError, ValueError, subprocess.SubprocessError):
            row.update(availability="unavailable", version_label="", reason="配置的 Codex 可执行文件不可核实")
        rows.append(row)
    else:
        rows.append({"id": "local-codex-cli", "name": "Codex CLI", "kind": "codex-cli",
                     "source": "configured executable", "actions": ["inspect"],
                     "availability": "unavailable", "version_label": "",
                     "observed_at": stamp, "reason": "未配置可核实的可执行文件"})

    endpoint = config.get("aieyra_os_adapter")
    os_row = {"id": "local-aieyra-os", "name": "Aieyra OS", "kind": "aieyra-os",
              "source": "public adapter declaration", "actions": ["inspect", "open", "pause", "resume", "interrupt", "close"],
              "version_label": "", "observed_at": stamp}
    if isinstance(endpoint, dict) and endpoint.get("health") and isinstance(endpoint.get("version"), str):
        # A declared URL is still unavailable until the owner adapter performs its
        # own authenticated health check. This endpoint never performs network IO.
        os_row.update(availability="unavailable", version_label=endpoint["version"][:80],
                      reason="已声明端点，等待受控适配器健康回执")
    else:
        os_row.update(availability="unavailable", reason="未提供公开、受控的 Aieyra OS 适配器端点")
    rows.append(os_row)
    return {"schema_version": 1, "observed_at": stamp, "agents": rows,
            "private_data_read": False, "model_started": False}
