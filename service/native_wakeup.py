"""Bounded notifications to an existing Codex daemon; never create a new host/thread."""

import base64
import hashlib
import json
import os
from pathlib import Path
import queue
import struct
import subprocess
import threading
import time


class WakeError(Exception):
    pass


class CodexConnection:
    """Codex proxy forwards raw WebSocket bytes, not JSON-lines, on Windows too."""

    def __init__(self, executable, timeout=8):
        if not Path(executable).is_file() or not Path(executable).is_absolute():
            raise WakeError("native_executable_unavailable")
        self.timeout = timeout
        self.buffer = bytearray()
        self.chunks = queue.Queue(maxsize=64)
        self.closed = threading.Event()
        self.sequence = 0
        self.process = subprocess.Popen(
            [executable, "app-server", "proxy"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS)
            if os.name == "nt"
            else 0,
        )
        threading.Thread(target=self._reader, daemon=True).start()
        try:
            key = base64.b64encode(os.urandom(16)).decode()
            self._write(
                (
                    "GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                    "Connection: Upgrade\r\nSec-WebSocket-Key: "
                    + key
                    + "\r\nSec-WebSocket-Version: 13\r\n\r\n"
                ).encode()
            )
            deadline = time.monotonic() + timeout
            header = b""
            while not header.endswith(b"\r\n\r\n") and len(header) < 8192:
                header += self._read(1, deadline)
            lines = header.decode("ascii").split("\r\n")
            headers = {
                line.split(":", 1)[0].lower(): line.split(":", 1)[1].strip()
                for line in lines[1:]
                if ":" in line
            }
            accept = base64.b64encode(
                hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
            ).decode()
            if (
                lines[0] != "HTTP/1.1 101 Switching Protocols"
                or headers.get("sec-websocket-accept", "") != accept
            ):
                raise WakeError("native_handshake_invalid")
            self.call(
                "initialize",
                {
                    "clientInfo": {"name": "aieyra_control_station", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            self.send({"method": "initialized"})
        except Exception:
            self.close()
            raise

    def _reader(self):
        try:
            while not self.closed.is_set():
                chunk = self.process.stdout.read1(16384)
                while not self.closed.is_set():
                    try:
                        self.chunks.put(chunk, timeout=0.2)
                        break
                    except queue.Full:
                        continue
                if not chunk:
                    break
        except OSError:
            self.closed.set()

    def _read(self, count, deadline):
        while len(self.buffer) < count:
            if time.monotonic() >= deadline:
                raise WakeError("native_timeout")
            if self.closed.is_set():
                raise WakeError("native_connection_closed")
            try:
                chunk = self.chunks.get(timeout=max(0.001, deadline - time.monotonic()))
            except queue.Empty:
                raise WakeError("native_timeout") from None
            if not chunk:
                raise WakeError("native_connection_closed")
            self.buffer.extend(chunk)
        data = bytes(self.buffer[:count])
        del self.buffer[:count]
        return data

    def _write(self, data):
        self.process.stdin.write(data)
        self.process.stdin.flush()

    def _frame(self, payload, opcode=1):
        mask = os.urandom(4)
        length = len(payload)
        head = (
            bytes([128 | opcode, 128 | length])
            if length < 126
            else (
                bytes([128 | opcode, 254]) + struct.pack("!H", length)
                if length <= 65535
                else bytes([128 | opcode, 255]) + struct.pack("!Q", length)
            )
        )
        self._write(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def send(self, value):
        self._frame(json.dumps(value, ensure_ascii=False).encode())

    def receive(self, deadline):
        body = bytearray()
        started = False
        while True:
            first, second = self._read(2, deadline)
            opcode, length = first & 15, second & 127
            if first & 112 or second & 128:
                raise WakeError("native_frame_invalid")
            if length == 126:
                length = struct.unpack("!H", self._read(2, deadline))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read(8, deadline))[0]
            if length + len(body) > 2 * 1024 * 1024:
                raise WakeError("native_response_limit")
            payload = self._read(length, deadline)
            if opcode == 8:
                raise WakeError("native_connection_closed")
            if opcode in (9, 10):
                if not first & 128 or length > 125:
                    raise WakeError("native_frame_invalid")
                if opcode == 9:
                    self._frame(payload, 10)
                continue
            if opcode not in (0, 1) or (opcode == 0) != started:
                raise WakeError("native_frame_invalid")
            started = True
            body.extend(payload)
            if first & 128:
                return json.loads(body)

    def call(self, method, params):
        self.sequence += 1
        self.send({"id": self.sequence, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout
        for _ in range(200):
            value = self.receive(deadline)
            if value.get("id") == self.sequence and "method" not in value:
                if "error" in value:
                    raise WakeError("native_rpc_rejected")
                if not isinstance(value.get("result"), dict):
                    raise WakeError("native_receipt_invalid")
                return value["result"]
            # Never approve tools or permissions on behalf of the leader.
            if "id" in value and "method" in value:
                self.send(
                    {
                        "id": value["id"],
                        "error": {
                            "code": -32601,
                            "message": "Notification client cannot approve requests",
                        },
                    }
                )
        raise WakeError("native_event_limit")

    def close(self):
        self.closed.set()
        try:
            self.process.stdin.close()
            self.process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            if self.process.poll() is None:
                self.process.terminate()  # Only our short-lived proxy, never the daemon.
                self.process.wait(timeout=2)
        self.process.stdout.close()


class NativeWakeup:
    def __init__(self, config, connection=CodexConnection):
        self.config, self.connection = config, connection

    def notify(self, target, prompt, message_id, before_send):
        executable = self.config.get("executable", "")
        if not self.config.get("enabled"):
            raise WakeError("native_adapter_disabled")
        client = self.connection(executable)
        try:
            tid = target["native_session_id"]
            thread = client.call("thread/read", {"threadId": tid, "includeTurns": False})["thread"]
            if thread.get("id") != tid:
                raise WakeError("native_identity_mismatch")
            status = thread.get("status", {})
            if status.get("type") not in ("active", "idle", "notLoaded"):
                raise WakeError("native_waiting_or_failed")
            if "archived_sessions" in str(thread.get("path", "")).replace("\\", "/").split("/"):
                raise WakeError("native_handoff_or_user_resume_required")
            if status.get("type") == "systemError" or status.get("activeFlags"):
                raise WakeError("native_waiting_or_failed")
            if status.get("type") != "active":
                turns = client.call(
                    "thread/turns/list", {"threadId": tid, "limit": 1, "itemsView": "notLoaded"}
                )
                last = (turns.get("data") or [{}])[0]
                if last.get("status") != "completed":
                    raise WakeError("native_handoff_or_user_resume_required")
                # Resume only the already bound thread; no model/policy/cwd overrides.
                thread = client.call("thread/resume", {"threadId": tid, "excludeTurns": True})[
                    "thread"
                ]
                if (
                    thread.get("id") != tid
                    or thread.get("status", {}).get("activeFlags")
                    or thread.get("status", {}).get("type") not in ("idle", "active")
                ):
                    raise WakeError("native_identity_or_wait_state_changed")
            before_send()
            result = client.call(
                "turn/start",
                {
                    "threadId": tid,
                    "clientUserMessageId": message_id,
                    "input": [{"type": "text", "text": prompt, "text_elements": []}],
                },
            )
            turn = result.get("turn", {})
            if not isinstance(turn.get("id"), str) or not turn["id"]:
                raise WakeError("native_receipt_invalid")
            return {
                "thread_id": tid,
                "turn_id": turn["id"],
                "status": turn.get("status"),
                "message_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            }
        finally:
            client.close()


class ResourceGate:
    """Sample host CPU/RAM before one notification; not a machine-wide hard cap."""

    def __init__(self):
        self.previous = None

    def __call__(self):
        if os.name != "nt":
            try:
                values = dict(
                    line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines()
                )
                available = int(values["MemAvailable"].split()[0]) / int(
                    values["MemTotal"].split()[0]
                )
                ticks = list(map(int, Path("/proc/stat").read_text().splitlines()[0].split()[1:9]))
                idle, total = ticks[3] + ticks[4], sum(ticks)
            except (OSError, KeyError, ValueError):
                return False
        else:
            import ctypes
            from ctypes import wintypes

            class Memory(ctypes.Structure):
                _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD)] + [
                    (key, ctypes.c_ulonglong)
                    for key in (
                        "total",
                        "available",
                        "page_total",
                        "page_available",
                        "virtual_total",
                        "virtual_available",
                        "extended",
                    )
                ]

            memory = Memory()
            memory.length = ctypes.sizeof(memory)
            times = [ctypes.c_ulonglong() for _ in range(3)]
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(
                ctypes.byref(memory)
            ) or not ctypes.windll.kernel32.GetSystemTimes(*(ctypes.byref(x) for x in times)):
                return False
            available = memory.available / memory.total
            idle, total = times[0].value, times[1].value + times[2].value
        previous, self.previous = self.previous, (idle, total)
        if previous is None or total <= previous[1]:
            return False
        cpu = 1 - (idle - previous[0]) / (total - previous[1])
        return available >= 0.20 and cpu < 0.80
