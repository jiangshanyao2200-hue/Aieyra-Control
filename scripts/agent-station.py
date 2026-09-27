#!/usr/bin/env python3
"""Restore a stable Control station from explicit local host lifecycle events.

No model calls, transcript reads, task replay, automatic enrollment or handoff.
Profiles contain credential file references, never copied tokens.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
from urllib.request import Request
from urllib.error import URLError

SPEC = importlib.util.spec_from_file_location(
    "station_client", Path(__file__).with_name("agent-client.py")
)
CLIENT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLIENT)
Error = CLIENT.ClientError
ID = re.compile(r"[A-Za-z0-9_.:-]{1,100}\Z")
HOSTS = ("codex", "claude", "cursor", "os", "generic")
EVENTS = {
    "claude": {
        "SessionStart": "join",
        "UserPromptSubmit": "work",
        "PreToolUse": "work",
        "PostToolUse": "work",
        "PostToolUseFailure": "work",
        "PreCompact": "work",
        "PostCompact": "join",
        "Stop": "finish",
        "StopFailure": "finish",
        "SessionEnd": "finish",
    },
    "cursor": {
        "sessionStart": "join",
        "beforeSubmitPrompt": "work",
        "preToolUse": "work",
        "postToolUse": "work",
        "postToolUseFailure": "work",
        "preCompact": "work",
        "stop": "finish",
        "sessionEnd": "finish",
    },
}


def read_json(path):
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise Error("invalid_json_object")
    return value


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with tmp.open("x", encoding="utf-8", newline="\n") as stream:
            os.chmod(tmp, 0o600)
            stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


@contextmanager
def lock(path, seconds=5):
    """Kernel lock, released even on process death; no stale PID deletion."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        deadline = time.monotonic() + seconds
        while True:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise Error("station_busy_retry_at_next_work_boundary") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class Station:
    def __init__(self, profile_path):
        self.path = Path(profile_path).resolve()
        self.p = read_json(self.path)
        required = {"schema", "host", "project", "root", "config_file", "actor_id", "seat_id"}
        if (
            not required <= self.p.keys()
            or set(self.p) - required - {"max_seconds", "idle_seconds", "lease"}
            or self.p["schema"] != 1
            or self.p["host"] not in HOSTS
            or type(self.p.get("lease", True)) is not bool
        ):
            raise Error("invalid_station_profile")
        for key in ("project", "actor_id", "seat_id"):
            if not isinstance(self.p[key], str) or not ID.fullmatch(self.p[key]):
                raise Error("invalid_station_profile")
        for key in ("root", "config_file"):
            if not isinstance(self.p[key], str) or not Path(self.p[key]).is_absolute():
                raise Error("absolute_profile_paths_required")
        self.root = Path(self.p["root"]).resolve()
        if not self.root.is_dir():
            raise Error("project_root_missing")
        self.state_dir = self.path.parent / (self.path.stem + ".state")
        self.state_file = self.state_dir / "connection.json"
        self.client = CLIENT.AgentClient(read_json(self.p["config_file"]))
        self.client.timeout = 3
        for key, default, low, high in (
            ("max_seconds", 3600, 30, 14400),
            ("idle_seconds", 300, 30, 600),
        ):
            value = self.p.get(key, default)
            if type(value) is not int or not low <= value <= high:
                raise Error("lease_duration_out_of_bounds")
            self.p[key] = value

    def state(self):
        return read_json(self.state_file) if self.state_file.exists() else {}

    def seat(self):
        result = self.client.call("seats")
        if result.get("actor_id") != self.p["actor_id"]:
            raise Error("credential_actor_mismatch")
        seat = next((s for s in result["seats"] if s["id"] == self.p["seat_id"]), None)
        if not seat or seat.get("project") != self.p["project"]:
            raise Error("credential_project_or_seat_mismatch")
        return seat

    def doctor(self):
        seat = self.seat()
        binding = seat.get("station_binding") or {}
        memory = self.client.call("memory", query={"project": self.p["project"]})
        return {
            "status": "ready" if seat["selectable"] else seat["reason"],
            "host": self.p["host"],
            "actor_id": self.p["actor_id"],
            "seat_id": seat["id"],
            "session_id": seat.get("session_id"),
            "binding_version": binding.get("version"),
            "native_session_id": binding.get("native_session_id"),
            "memory_version": memory["memory"]["version"],
            "missing_sections": memory["missing_sections"],
            "last_adapter_result": self.state().get("status"),
            "execution_state": "unknown",
            "automatic_wakeup": False,
        }

    def os_doctor(self):
        """Read the existing explicit runtime projection; do not discover private sessions."""
        try:
            with self.client.opener.open(
                Request(self.client.url + "/api/collaboration"), timeout=3
            ) as response:
                snapshot = self.client.read_response(response)
        except (OSError, URLError):
            raise Error("os_projection_unavailable") from None
        matrices = snapshot.get("matrices")
        if not isinstance(matrices, list):
            raise Error("invalid_os_projection")
        rows = []
        for item in matrices[:16]:
            if not isinstance(item, dict):
                raise Error("invalid_os_projection")
            fresh = item.get("available") is True and item.get("stale") is False
            advertised = item.get("capabilities", {})
            rows.append(
                {
                    "id": item.get("id"),
                    "available": fresh,
                    "stale": not fresh,
                    "capabilities": {
                        k: fresh and advertised.get(k) is True
                        for k in (
                            "observe",
                            "dispatch",
                            "matrix_send",
                            "matrix_cancel",
                            "agent_send",
                            "agent_cancel",
                        )
                    },
                }
            )
        return {
            "status": "available" if any(x["available"] for x in rows) else "no_verified_runtime",
            "source": "explicit_os_sessions",
            "runtimes": rows,
            "observed_at": snapshot.get("observed_at"),
            "model_started": False,
            "private_sessions_scanned": False,
            "note": "OS runtime registrations and development-agent office memberships are distinct.",
        }

    def context(self, result):
        return (
            "Aieyra Control station: "
            + json.dumps(result, ensure_ascii=False)
            + ". Read project AGENTS.md and memory, runtime inbox, and center/history or center/inbox "
            "through agent-client.py. Coordination chat is separate from runtime deliveries. "
            "Read relevant messages before center/ack; reply with verified outcomes when requested. "
            "Save the five memory sections with CAS before finishing. Report confirmed station defects. "
            "A new native identity requires explicit handoff; never replay old deliveries. "
            "Communication presence is not proof of execution or authorization."
        )

    def ensure(self, native, working=False):
        if not isinstance(native, str) or not ID.fullmatch(native):
            raise Error("real_native_session_id_required")
        with lock(self.state_dir / "station.lock"):
            seat = self.seat()
            binding = seat.get("station_binding") or {}
            if binding.get("native_session_id") and binding["native_session_id"] != native:
                result = {
                    "status": "handoff_required",
                    "actor_id": self.p["actor_id"],
                    "seat_id": seat["id"],
                    "requested_native_session_id": native,
                    "expected_version": binding["version"],
                    "automatic_handoff": False,
                }
                # Announce once, persist intent before sending. This is a request
                # for explicit governance, never an implicit change of identity.
                request = self.state_dir / (
                    "handoff-"
                    + hashlib.sha256((native + ":" + str(binding["version"])).encode()).hexdigest()[
                        :24
                    ]
                    + ".json"
                )
                if not request.exists():
                    intent = {
                        "status": "pending",
                        "request_id": "handoff-request-" + uuid.uuid4().hex,
                        "requested_native_session_id": native,
                        "expected_version": binding["version"],
                    }
                    atomic(request, intent)
                    try:
                        receipt = self.client.call(
                            "center/message",
                            {
                                "request_id": intent["request_id"],
                                "project": self.p["project"],
                                "kind": "blocker",
                                "body": "Station recovery requests explicit CAS handoff: "
                                + json.dumps(result)
                                + ". Verify the current owner and pending deliveries; no automatic takeover or task replay.",
                            },
                        )
                        intent.update(status="sent", receipt=receipt)
                        atomic(request, intent)
                    except Error:
                        pass
                result["handoff_notice"] = read_json(request)["status"]
                return result
            state = self.state()
            sid = seat.get("session_id")
            if sid:
                session = self.client.call("sessions/" + sid)["session"]
                if session.get("native_session_id") != native or session["seat_id"] != seat["id"]:
                    return {"status": "occupied_by_other_session", "automatic_takeover": False}
            else:
                if state.get("status") == "connect_pending":
                    # Persisted intent survives response loss and process death. Query only.
                    try:
                        self.client.call("requests/" + state["request_id"])
                        session = self.client.call("sessions/" + state["session_id"])["session"]
                    except Error:
                        return {
                            "status": "connect_unconfirmed",
                            "request_id": state["request_id"],
                            "session_id": state["session_id"],
                            "automatic_replay": False,
                        }
                    if session["state"] == "connected":
                        sid = session["id"]
                    else:
                        state["status"] = "closed"
                        atomic(self.state_file, state)
                if not sid:
                    if not seat["selectable"]:
                        return {"status": seat["reason"], "automatic_takeover": False}
                    sid = "station-" + self.p["host"] + "-" + uuid.uuid4().hex
                    state = {
                        "session_id": sid,
                        "native_session_id": native,
                        "request_id": "join-" + uuid.uuid4().hex,
                        "status": "connect_pending",
                    }
                    atomic(self.state_file, state)
                    try:
                        session = self.client.call(
                            "connect",
                            {
                                "request_id": state["request_id"],
                                "session_id": sid,
                                "seat_id": seat["id"],
                                "seat_epoch": seat["epoch"],
                                "native_session_id": native,
                            },
                        )["session"]
                    except Error:
                        try:
                            session = self.client.call("sessions/" + sid)["session"]
                        except Error:
                            return {
                                "status": "connect_unconfirmed",
                                "request_id": state["request_id"],
                                "session_id": sid,
                                "automatic_replay": False,
                            }
                    if session.get("state") != "connected":
                        raise Error("connect_not_live")
            if session.get("native_session_id") != native:
                raise Error("native_readback_mismatch")
            state.update(session_id=sid, native_session_id=native, status="connected")
            atomic(self.state_file, state)
            # These are real host activity events, not a timer manufacturing presence.
            self.client.call(
                "heartbeat",
                {
                    "request_id": "event-" + uuid.uuid4().hex,
                    "session_id": sid,
                    "runtime_state": "running" if working else "idle",
                },
            )
            marker = self.state_dir / (sid + ".active")
            marker.touch()
            if self.p.get("lease", True) and sid.startswith("station-"):
                self.start_lease(sid, state)
            memory = self.client.call("memory", query={"project": self.p["project"]})
            inbox = self.client.call("inbox", query={"session_id": sid})
            coordination = {"status": "unavailable"}
            try:
                history = self.client.call("center/history")
                messages = history.get("messages", [])
                coordination = {
                    "status": "observed",
                    "unread_in_recent_window": sum(
                        item.get("sender") != self.p["actor_id"]
                        and not item.get("read_at")
                        and item.get("project") in (self.p["project"], "coordination")
                        for item in messages
                    ),
                    "window_size": len(messages),
                    "latest_seq": max((item["seq"] for item in messages), default=0),
                    "older_messages_available": bool(history.get("has_more")),
                }
            except Error:
                # Chat availability must not turn an established connection into a failure.
                pass
            return {
                "status": "connected",
                "session_id": sid,
                "seat_id": seat["id"],
                "native_session_id": native,
                "memory_version": memory["memory"]["version"],
                "unconfirmed_deliveries": len(inbox.get("deliveries", [])),
                "coordination": coordination,
                "tasks_replayed": False,
                "memory_saved": False,
            }

    def start_lease(self, sid, state):
        alive = self.state_dir / (sid + ".worker")
        if alive.exists() and time.time() - alive.stat().st_mtime < 55:
            return
        if state.get("helper_session") == sid and time.time() - state.get("helper_started", 0) < 10:
            return
        with (self.state_dir / (sid + ".lease.log")).open("ab") as log:
            helper = subprocess.Popen(
                [
                    sys.executable,
                    "-X",
                    "utf8",
                    str(Path(__file__).resolve()),
                    "--profile",
                    str(self.path),
                    "watch",
                    "--session-id",
                    sid,
                ],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                start_new_session=os.name != "nt",
            )
        threading.Thread(target=helper.wait, daemon=True, name="station-lease-reaper").start()
        state.update(helper_session=sid, helper_started=time.time(), helper_pid=helper.pid)
        atomic(self.state_file, state)

    def finish(self, native):
        with lock(self.state_dir / "station.lock"):
            state = self.state()
            if not state or state.get("native_session_id") != native:
                return {"status": "no_owned_transport", "lease_released": False}
            sid = state["session_id"]
            (self.state_dir / (sid + ".active")).unlink(missing_ok=True)
            result = CLIENT.finish_session(self.client, sid, "finish-" + sid)
            if result["lease_released"]:
                state["status"] = "closed"
            state["finish"] = result
            atomic(self.state_file, state)
            return {"status": state["status"], **result}

    def hook(self, payload):
        if not isinstance(payload, dict):
            raise Error("invalid_hook_payload")
        host = self.p["host"]
        event = payload.get("hook_event_name")
        action = EVENTS.get(host, {}).get(event)
        if not action:
            raise Error("unsupported_hook_event")
        if (
            payload.get("agent_id")
            or payload.get("subagent_id")
            or payload.get("parent_conversation_id")
        ):
            return {"status": "subagent_ignored"}, {}
        if host == "claude":
            native = payload.get("session_id")
            cwd = payload.get("cwd")
            if (
                not isinstance(cwd, str)
                or not Path(cwd).is_absolute()
                or not Path(cwd).resolve().is_relative_to(self.root)
            ):
                raise Error("hook_project_mismatch")
        else:
            native = payload.get("conversation_id")
            roots = payload.get("workspace_roots")
            if not isinstance(roots, list) or str(self.root) not in [
                str(Path(x).resolve())
                for x in roots
                if isinstance(x, str) and Path(x).is_absolute()
            ]:
                raise Error("hook_project_mismatch")
            if payload.get("session_id") and payload["session_id"] != native:
                raise Error("native_hook_identity_mismatch")
        if not isinstance(native, str) or not ID.fullmatch(native):
            raise Error("real_native_session_id_required")
        result = (
            self.finish(native) if action == "finish" else self.ensure(native, action == "work")
        )
        output = {}
        if host == "claude" and event in (
            "SessionStart",
            "UserPromptSubmit",
            "PreToolUse",
            "PostToolUse",
            "PostToolUseFailure",
            "PostCompact",
        ):
            output = {
                "hookSpecificOutput": {
                    "hookEventName": event,
                    "additionalContext": self.context(result),
                }
            }
        elif host == "cursor" and event in ("sessionStart", "postToolUse", "postToolUseFailure"):
            output = {"additional_context": self.context(result)}
        elif host == "cursor" and event == "beforeSubmitPrompt":
            output = {"continue": True}
        # Never block a prompt, inject a followup, auto-run tasks or claim memory was saved.
        atomic(
            self.state_dir / "last-hook.json",
            {"host": host, "event": event, "result": result, "observed_at": time.time()},
        )
        return result, output

    def watch(self, sid):
        if not re.fullmatch(r"station-[A-Za-z0-9-]{1,70}", sid):
            raise Error("invalid_adapter_session")
        with lock(self.state_dir / (sid + ".watch.lock"), seconds=0):
            worker = self.state_dir / (sid + ".worker")

            class ActivityStop(threading.Event):
                def wait(self, timeout=None):
                    worker.touch()
                    return super().wait(timeout)

            worker.touch()
            try:
                return CLIENT.keep_lease(
                    self.client,
                    sid,
                    self.state_dir / (sid + ".active"),
                    self.p["max_seconds"],
                    self.p["idle_seconds"],
                    ActivityStop(),
                )
            finally:
                worker.unlink(missing_ok=True)


def configure(station, remove=False):
    """Merge only our entries, preserve unrelated settings, refuse changed ownership."""
    p, root = station.p, station.root
    key = "aieyra-control-" + hashlib.sha256(str(station.path).encode()).hexdigest()[:12]
    script = str(Path(__file__).resolve())
    args = ["-X", "utf8", script, "--profile", str(station.path), "hook"]
    mcp = {
        "command": sys.executable,
        "args": [
            "-X",
            "utf8",
            str(Path(script).with_name("agent-client.py")),
            "--config",
            p["config_file"],
            "mcp",
        ],
    }
    contributions = {}
    if p["host"] == "claude":
        # Official exec form avoids shell expansion of paths on every platform.
        handler = {"type": "command", "command": sys.executable, "args": args, "timeout": 30}
        contributions[".claude/settings.local.json"] = {
            "hooks": {event: [{"hooks": [handler]}] for event in EVENTS["claude"]}
        }
        contributions[".mcp.json"] = {"mcpServers": {key: mcp}}
    elif p["host"] == "cursor":
        # Cursor only supports command strings. Encode static Windows PowerShell
        # invocation; untrusted hook payload always stays on stdin, never in shell.
        if os.name == "nt":
            import base64

            def psquote(value):
                return "'" + value.replace("'", "''") + "'"

            # ProcessStartInfo avoids PowerShell 5.1 native-pipeline/window-style
            # stalls, explicitly closes stdin, and keeps both processes hidden.
            command = (
                "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; "
                "[Console]::InputEncoding=[System.Text.Encoding]::UTF8; [Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
                "$stationInput=[Console]::In.ReadToEnd(); "
                "$s=New-Object System.Diagnostics.ProcessStartInfo; "
                "$s.FileName=" + psquote(sys.executable) + "; "
                "$s.Arguments=" + psquote(subprocess.list2cmdline(args)) + "; "
                "$s.UseShellExecute=$false; $s.CreateNoWindow=$true; "
                "$s.RedirectStandardInput=$true; $s.RedirectStandardOutput=$true; "
                "$s.RedirectStandardError=$true; "
                "$u=New-Object System.Text.UTF8Encoding($false); "
                "$s.StandardOutputEncoding=$u; $s.StandardErrorEncoding=$u; "
                "$p=[System.Diagnostics.Process]::Start($s); "
                "$out=$p.StandardOutput.ReadToEndAsync(); $err=$p.StandardError.ReadToEndAsync(); "
                "$bytes=$u.GetBytes($stationInput); $p.StandardInput.BaseStream.Write($bytes,0,$bytes.Length); $p.StandardInput.Close(); "
                "$p.WaitForExit(); [Console]::Out.Write($out.Result); [Console]::Error.Write($err.Result); "
                "exit $p.ExitCode"
            )
            encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
            command = "powershell.exe -NoProfile -NonInteractive -EncodedCommand " + encoded
        else:
            command = shlex.join([sys.executable, *args])
        contributions[".cursor/hooks.json"] = {
            "version": 1,
            "hooks": {event: [{"command": command}] for event in EVENTS["cursor"]},
        }
        contributions[".cursor/mcp.json"] = {"mcpServers": {key: mcp}}

    text_parts = {
        "AGENTS.md": (
            "## Persistent Aieyra Control station\n\n"
            f"Project `{p['project']}`, host `{p['host']}`, actor `{p['actor_id']}`, seat `{p['seat_id']}`.\n"
            "Use this profile only when continuing this assigned responsibility; if several station blocks exist, "
            "select your own role rather than joining all of them.\n"
            f"Private profile reference: `{station.path}` (never copy credentials into documentation).\n"
            f"At session start, resume and after compaction, run `{sys.executable}` with arguments "
            f"`-X utf8`, `{script}`, `--profile`, `{station.path}`, `join`, `--native-session-id`, "
            "and the real current native session ID. Do not reuse an old transport ID.\n"
            "Then read project memory, runtime inbox, and coordination chat via agent-client.py "
            "call center/history or call center/inbox (continue with next_cursor as after). "
            "Only after reading relevant messages use call center/ack with their IDs; reply with "
            "verified outcomes when requested. Chat reading does not execute tasks. A changed native ID "
            "requires explicit CAS handoff by the leader; an occupied station must not be taken over. "
            "Read this profile's state/connection.json for the actual transport ID; refresh through join "
            "at real work boundaries only. Before final response save five memory sections using current "
            "version/CAS, then station finish with the same native ID. Report confirmed station problems "
            "through the existing Control channel. This adds no model, production or deployment authority.\n"
        )
    }
    if p["host"] == "claude":
        text_parts["CLAUDE.md"] = (
            "Read and follow the persistent Control station instructions:\n\n@AGENTS.md\n"
        )
    if p["host"] == "cursor":
        text_parts[".cursor/rules/" + key + ".mdc"] = (
            "---\ndescription: Persistent Aieyra Control station\nalwaysApply: true\n---\n"
            "Read @AGENTS.md at each new or resumed conversation and after compaction. "
            "Use the real conversation_id for station recovery. Save project memory before finishing.\n"
        )
    if p["host"] == "codex":
        import tomllib

        target = root / ".codex/config.toml"
        existing = target.read_text(encoding="utf-8-sig") if target.exists() else ""
        parsed = tomllib.loads(existing)
        if (
            key in parsed.get("mcp_servers", {})
            and not (station.state_dir / "installation.json").exists()
        ):
            raise Error("existing_mcp_entry_not_owned")
        text_parts[".codex/config.toml"] = (
            f"[mcp_servers.{key}]\ncommand = {json.dumps(mcp['command'])}\n"
            f"args = {json.dumps(mcp['args'], ensure_ascii=False)}\n"
        )

    def merge(current, wanted, delete=False):
        if not isinstance(current, dict):
            raise Error("configuration_shape_conflict")
        for name, value in wanted.items():
            if isinstance(value, dict):
                if name not in current:
                    if delete:
                        continue
                    current[name] = {}
                merge(current[name], value, delete)
                if delete and not current[name]:
                    del current[name]
            elif isinstance(value, list):
                found = current.setdefault(name, [])
                if not isinstance(found, list):
                    raise Error("configuration_shape_conflict")
                for item in value:
                    if delete:
                        if item not in found:
                            raise Error("managed_hook_changed_preserve_manual_edit")
                        found.remove(item)
                    elif item not in found:
                        found.append(item)
                if delete and not found:
                    del current[name]
            elif delete:
                # Keep shared schema version, remove only owned scalar leaves.
                if name == "version":
                    continue
                if current.get(name) != value:
                    raise Error("managed_setting_changed_preserve_manual_edit")
                del current[name]
            else:
                if name in current and current[name] != value:
                    raise Error("existing_setting_conflict")
                current[name] = value

    manifest_path = station.state_dir / "installation.json"
    with lock(station.state_dir / "install.lock"):
        manifest = read_json(manifest_path) if manifest_path.exists() else {"files": {}}
        if remove and not manifest["files"]:
            return {"status": "not_installed", "changed": []}
        changes = []
        updated = {"host": p["host"], "key": key, "files": {}}
        for rel in sorted(set(contributions) | set(text_parts) | set(manifest["files"])):
            target = root / rel
            if not target.resolve().is_relative_to(root):
                raise Error("configuration_path_escapes_project")
            before = target.read_bytes() if target.exists() else None
            text_before = before.decode("utf-8-sig") if before is not None else ""
            old = manifest["files"].get(rel)
            if rel.endswith(".mdc"):
                if old and text_before != old["full"]:
                    raise Error("managed_rule_changed_preserve_manual_edit")
                if before is not None and not old:
                    raise Error("existing_rule_not_owned")
                after = "" if remove else text_parts[rel]
                if not remove:
                    updated["files"][rel] = {"full": after}
            elif rel.endswith(".json"):
                data = json.loads(text_before) if text_before.strip() else {}
                if old:
                    merge(data, old["owned"], True)
                elif rel in contributions:
                    servers = data.get("mcpServers", {})
                    if key in servers:
                        raise Error("existing_mcp_entry_not_owned")
                if not remove and rel in contributions:
                    merge(data, contributions[rel])
                    updated["files"][rel] = {"owned": contributions[rel]}
                after = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
            else:
                toml = rel.endswith(".toml")
                start = ("# " if toml else "<!-- ") + "BEGIN " + key + ("" if toml else " -->")
                end = ("# " if toml else "<!-- ") + "END " + key + ("" if toml else " -->")
                base = text_before
                if start in base or end in base:
                    if not old or base.count(start) != 1 or base.count(end) != 1:
                        raise Error("unowned_or_ambiguous_instruction_block")
                    a, b = base.index(start), base.index(end) + len(end)
                    if base[a:b].replace("\r\n", "\n") != old["owned"].replace("\r\n", "\n"):
                        raise Error("managed_instructions_changed_preserve_manual_edit")
                    base = base[:a] + base[b:].removeprefix("\r\n\r\n").removeprefix("\n\n")
                elif old:
                    raise Error("managed_instructions_missing")
                if not remove and rel in text_parts:
                    owned = start + "\n" + text_parts[rel] + end
                    # Cursor frontmatter must begin at byte zero.
                    after = owned + "\n\n" + base
                    updated["files"][rel] = {"owned": owned}
                else:
                    after = base
                if rel.endswith(".toml"):
                    import tomllib

                    tomllib.loads(after)
            after_bytes = after.encode("utf-8")
            if before != after_bytes:
                changes.append((target, before, after_bytes))
        # Validate every intended edit before the first write; keep exact original bytes.
        backup = station.state_dir / "backups" / uuid.uuid4().hex
        backup.mkdir(parents=True)
        journal = []
        for target, before, after in changes:
            if (target.read_bytes() if target.exists() else None) != before:
                raise Error("configuration_changed_during_install")
            rel = target.relative_to(root).as_posix()
            if before is not None:
                saved = backup / rel
                saved.parent.mkdir(parents=True, exist_ok=True)
                saved.write_bytes(before)
            journal.append(
                {
                    "path": rel,
                    "existed": before is not None,
                    "before_sha256": hashlib.sha256(before).hexdigest()
                    if before is not None
                    else None,
                    "after_sha256": hashlib.sha256(after).hexdigest(),
                }
            )
        atomic(backup / "journal.json", {"changes": journal})
        for target, before, after in changes:
            if (target.read_bytes() if target.exists() else None) != before:
                raise Error("configuration_changed_during_install")
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
            tmp.write_bytes(after)
            os.replace(tmp, target)
        atomic(manifest_path, updated if not remove else {"files": {}})
        return {
            "status": "uninstalled" if remove else "configured",
            "host": p["host"],
            "changed": [x["path"] for x in journal],
            "backup": str(backup),
            "actual_host_verified": False,
            "requires_trusted_project": True,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor")
    commands.add_parser("os-doctor")
    commands.add_parser("hook")
    for command in ("join", "finish"):
        p = commands.add_parser(command)
        p.add_argument("--native-session-id", required=True)
    p = commands.add_parser("watch")
    p.add_argument("--session-id", required=True)
    for command in ("install", "uninstall"):
        commands.add_parser(command)
    p = commands.add_parser("codex", help="Explicit project-scoped MCP configuration for Codex CLI")
    p.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        station = Station(args.profile)
        if args.command == "codex":
            if station.p["host"] != "codex":
                raise Error("codex_profile_required")
            executable = shutil.which("codex")
            if not executable:
                raise Error("codex_executable_missing")
            key = "aieyra-control-" + hashlib.sha256(str(station.path).encode()).hexdigest()[:12]
            import tomllib

            config = tomllib.loads(
                (station.root / ".codex/config.toml").read_text(encoding="utf-8-sig")
            )
            server = config.get("mcp_servers", {}).get(key)
            if not server:
                raise Error("install_station_configuration_first")
            argv = [executable]
            for name, value in server.items():
                argv += ["-c", f"mcp_servers.{key}.{name}=" + json.dumps(value, ensure_ascii=False)]
            trailing = args.arguments
            if trailing and trailing[0] == "--":
                trailing = trailing[1:]
            argv += trailing
            result = subprocess.run(
                argv,
                cwd=station.root,
                stdin=sys.stdin,
                stdout=sys.stdout,
                stderr=sys.stderr,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            return result.returncode
        if args.command == "hook":
            raw = sys.stdin.buffer.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise Error("hook_payload_too_large")
            _, output = station.hook(json.loads(raw))
        elif args.command == "doctor":
            output = station.doctor()
        elif args.command == "os-doctor":
            output = station.os_doctor()
        elif args.command == "join":
            output = station.ensure(args.native_session_id, True)
        elif args.command == "finish":
            output = station.finish(args.native_session_id)
        elif args.command == "watch":
            output = station.watch(args.session_id)
        else:
            output = configure(station, remove=args.command == "uninstall")
        print(json.dumps(output, ensure_ascii=False))
        return 0
    except (Error, OSError, ValueError, KeyError) as error:
        code = (
            error.code if isinstance(error, Error) else "invalid_or_unavailable_local_configuration"
        )
        if args.command == "hook":
            print("{}")
            print(
                "Aieyra Control: " + code + "; use station doctor at the next work boundary.",
                file=sys.stderr,
            )
            return 0
        print(json.dumps({"status": "error", "code": code}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
