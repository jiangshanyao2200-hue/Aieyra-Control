"""Explicit local Matrix RPC connection. No discovery scanning or secret exports."""

from __future__ import annotations

import ctypes
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit


class BridgeError(Exception):
    """Only stable, non-sensitive codes cross the CLI/Node boundary."""


def require(condition, code):
    if not condition:
        raise BridgeError(code)


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def process_executable(pid):
    """Read the executable only; never inspect command lines or process secrets."""
    try:
        if os.name == "nt":
            from ctypes import wintypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.QueryFullProcessImageNameW.argtypes = [
                wintypes.HANDLE,
                wintypes.DWORD,
                wintypes.LPWSTR,
                ctypes.POINTER(wintypes.DWORD),
            ]
            kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(0x1000, False, pid)
            require(bool(handle), "process_unavailable")
            try:
                size = wintypes.DWORD(32768)
                buf = ctypes.create_unicode_buffer(size.value)
                require(
                    bool(kernel.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))),
                    "process_unavailable",
                )
                return str(Path(buf.value).resolve(strict=True))
            finally:
                kernel.CloseHandle(handle)
        if sys.platform.startswith("linux"):
            return str(Path(f"/proc/{pid}/exe").resolve(strict=True))
        require(pid == os.getpid(), "process_verification_unsupported")
        return str(Path(sys.executable).resolve(strict=True))
    except (OSError, ValueError):
        raise BridgeError("process_unavailable") from None


def absolute_path(value):
    require(isinstance(value, str) and Path(value).is_absolute(), "absolute_path_required")
    return Path(value).resolve()


class MatrixClient:
    READS = frozenset(
        {
            "describe",
            "matrix.status",
            "agents.models",
            "agents.list",
            "agents.status",
            "agents.activity",
            "agents.events",
            "agents.wait",
            "humans.list",
            "humans.status",
        }
    )
    WRITES = frozenset(
        {
            "matrix.send",
            "matrix.cancel",
            "agents.send",
            "agents.cancel",
            "agents.create",
            "agents.review",
            "humans.reply",
        }
    )

    def __init__(self, config):
        require(isinstance(config, dict), "invalid_config")
        allowed = {
            "version",
            "mode",
            "discovery_ref",
            "expected_session",
            "expected_pid",
            "expected_executable",
            "expected_created_at",
            "state_dir",
            "work_root",
            "allowed_models",
            "allowed_tools",
            "enable_os_writes",
            "enable_matrix_writes",
            "timeout_seconds",
        }
        require(not set(config) - allowed and config.get("version") == 1, "invalid_config")
        require(config.get("mode", "fixture") in {"fixture", "os"}, "invalid_mode")
        self.config = dict(config)
        self.mode = config.get("mode", "fixture")
        self.reference = absolute_path(config.get("discovery_ref"))
        self.expected_exe = absolute_path(config.get("expected_executable"))
        require(
            type(config.get("expected_pid")) is int and config["expected_pid"] > 0,
            "invalid_expected_pid",
        )
        require(
            isinstance(config.get("expected_session"), str) and config["expected_session"],
            "expected_session_required",
        )
        self.timeout = config.get("timeout_seconds", 60)
        require(type(self.timeout) in (int, float) and 0 < self.timeout <= 65, "invalid_timeout")
        self._secret = ""
        self._binding = None
        self._load_discovery()
        description = self.rpc("describe")
        require(
            isinstance(description, dict)
            and description.get("version") == 1
            and description.get("session_id") == config["expected_session"],
            "describe_identity_mismatch",
        )
        if self.mode == "fixture":
            require(description.get("fixture") is True, "fixture_service_required")
        self.description = description

    @property
    def binding(self):
        return self._binding.copy()

    def clean(self, value):
        # Defense against a faulty endpoint echoing this connection's Bearer.
        if isinstance(value, str):
            return value.replace(self._secret, "[redacted]") if self._secret else value
        if isinstance(value, list):
            return [self.clean(v) for v in value]
        if isinstance(value, dict):
            return {
                self.clean(k): self.clean(v)
                for k, v in value.items()
                if k.lower() not in {"token", "authorization"}
            }
        return value

    def _load_discovery(self):
        try:
            require(self.reference.stat().st_size <= 16384, "invalid_discovery")
            data = json.loads(self.reference.read_text(encoding="utf-8-sig"))
            require(isinstance(data, dict) and data.get("version") == 1, "invalid_discovery")
            require(
                data.get("session_id") == self.config["expected_session"]
                and type(data.get("pid")) is int
                and data["pid"] == self.config["expected_pid"],
                "discovery_identity_mismatch",
            )
            require(
                absolute_path(data.get("executable")) == self.expected_exe,
                "discovery_executable_mismatch",
            )
            require(
                Path(process_executable(data["pid"])) == self.expected_exe, "live_process_mismatch"
            )
            endpoint = urlsplit(data.get("url", ""))
            require(
                endpoint.scheme == "http"
                and endpoint.hostname == "127.0.0.1"
                and endpoint.port
                and endpoint.netloc == f"127.0.0.1:{endpoint.port}"
                and endpoint.path == "/v1/rpc"
                and not endpoint.query
                and not endpoint.fragment,
                "loopback_rpc_required",
            )
            require(
                isinstance(data.get("token"), str) and re.fullmatch(r"[0-9a-f]{64}", data["token"]),
                "invalid_discovery_token",
            )
            require(
                isinstance(data.get("created_at"), str) and data["created_at"], "invalid_discovery"
            )
            if "expected_created_at" in self.config:
                require(
                    data["created_at"] == self.config["expected_created_at"],
                    "runtime_created_at_mismatch",
                )
            if self.mode == "fixture":
                require(data.get("fixture") is True, "fixture_discovery_required")
            binding = {
                k: data[k]
                for k in ("version", "session_id", "pid", "executable", "url", "created_at")
            }
            require(self._binding is None or self._binding == binding, "runtime_binding_changed")
            require(not self._secret or self._secret == data["token"], "runtime_credential_changed")
            self._binding, self._secret = binding, data["token"]
            self._port = endpoint.port
        except (OSError, ValueError, TypeError, KeyError):
            raise BridgeError("invalid_discovery") from None

    def rpc(self, action, arguments=None):
        require(action in self.READS | self.WRITES, "action_not_exposed")
        if action in self.WRITES and self.mode == "os":
            require(self.config.get("enable_os_writes") is True, "os_writes_disabled")
        if action in {"matrix.send", "matrix.cancel"}:
            require(self.config.get("enable_matrix_writes") is True, "matrix_writes_disabled")
        self._load_discovery()
        body = canonical({"action": action, "arguments": arguments or {}}).encode("utf-8")
        require(len(body) <= 1 << 20, "request_too_large")
        require(self._secret.encode() not in body, "credential_in_payload")
        # http.client uses neither environment proxies nor redirect handlers.
        conn = http.client.HTTPConnection("127.0.0.1", self._port, timeout=self.timeout)
        try:
            conn.request(
                "POST",
                "/v1/rpc",
                body,
                {"Content-Type": "application/json", "Authorization": "Bearer " + self._secret},
            )
            response = conn.getresponse()
            # 8 MiB archive row may expand during JSON serialization/redaction.
            raw = response.read((16 << 20) + 1)
            require(len(raw) <= 16 << 20, "response_too_large")
            require(200 <= response.status < 300 or response.status == 400, "rpc_http_failure")
            try:
                data = json.loads(raw)
            except (ValueError, UnicodeError):
                raise BridgeError("invalid_rpc_response") from None
            require(
                isinstance(data, dict) and data.get("ok") is True and "data" in data, "rpc_rejected"
            )
            return self.clean(data["data"])
        except (OSError, http.client.HTTPException):
            # No exception text, raw HTTP body or headers cross the boundary.
            raise BridgeError("rpc_transport_unknown") from None
        finally:
            conn.close()
