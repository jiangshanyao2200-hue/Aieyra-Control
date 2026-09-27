"""Opt-in cloud boundary. No calls before explicit sign-in, no project payloads."""

from __future__ import annotations
import base64
import hashlib
import json
import secrets
import socket
import threading
import time
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from urllib.error import HTTPError, URLError
from release_verify import verify

ORIGIN = "https://ctrlupdate.aieyra.cn"


class CloudError(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class CloudLink:
    def __init__(self, transport=None, vault=None):
        self.transport = transport or self.request
        self.lock = threading.RLock()
        self.flow = None
        self.session = None
        self.release = None
        self.last_check = 0
        self.error = None
        self.vault = vault
        self.stream = None
        self.custom_transport = transport is not None
        self.next_stream = 0
        if vault:
            self.session = vault.load()

    def request(self, path, body=None, token=None):
        if not path.startswith("/v1/") or ".." in path or "?" in path:
            raise CloudError("invalid_cloud_route")
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(
            ORIGIN + path,
            data=json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
            if body is not None
            else None,
            headers=headers,
        )
        try:
            with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=15) as r:
                data = r.read(2 * 1024 * 1024 + 1)
                if len(data) > 2 * 1024 * 1024:
                    raise CloudError("cloud_response_limit", 502)
                return json.loads(data)
        except HTTPError as e:
            if token and e.code == 401:
                self.invalidate(token)
            code = "cloud_request_rejected"
            try:
                raw = e.read(4097)
                value = json.loads(raw) if len(raw) <= 4096 else {}
                candidate = value.get("error")
                if (
                    isinstance(candidate, str)
                    and candidate.replace("_", "").isalnum()
                    and len(candidate) < 100
                ):
                    code = candidate
            except (OSError, ValueError):
                pass
            finally:
                e.close()
            raise CloudError(code, e.code) from None
        except (URLError, OSError, ValueError):
            raise CloudError("cloud_unavailable", 503) from None

    def authenticated(self):
        with self.lock:
            if not self.session or self.session["expires_at"] <= time.time():
                self.invalidate()
                raise CloudError("cloud_login_required", 401)
            return self.session

    @staticmethod
    def close_stream(stream):
        if stream is None:
            return
        # Interrupt a blocked urllib read before closing its buffered reader.
        raw = getattr(getattr(stream, "fp", None), "raw", None)
        sock = getattr(raw, "_sock", None)
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        try:
            stream.close()
        except OSError:
            pass

    def invalidate(self, token=None):
        with self.lock:
            if token is not None and (
                not self.session or self.session.get("access_token") != token
            ):
                return
            self.session = None
            self.release = None
            self.last_check = 0
            self.next_stream = 0
            stream = self.stream
            self.stream = None
            if self.vault:
                self.vault.clear()
        self.close_stream(stream)

    def status(self):
        with self.lock:
            active = bool(self.session and self.session["expires_at"] > time.time())
            if self.session and not active:
                self.invalidate()
            return {
                "enabled": active,
                "mode": "cloud_enabled" if active else "local_only",
                "user": self.session["user"] if active else None,
                "pending": bool(self.flow and self.flow["expires"] > time.time()),
                "release": self.release if active else None,
                "last_check": self.last_check if active else None,
                "error": self.error if active else None,
            }

    def start(self):
        verifier = secrets.token_urlsafe(32)
        state = secrets.token_urlsafe(32)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        with self.lock:
            result = self.transport(
                "/v1/auth/start",
                {
                    "challenge": challenge,
                    "state": state,
                    "scope": "desktop",
                    "redirect_uri": ORIGIN + "/auth/callback",
                },
            )
            if not isinstance(result.get("authorize_url"), str) or not result[
                "authorize_url"
            ].startswith("https://api.aieyra.cn/aieyra/control/authorize?flow="):
                raise CloudError("invalid_authorize_url", 502)
            self.flow = {
                "flow_id": result["flow_id"],
                "verifier": verifier,
                "state": state,
                "expires": time.time() + 300,
            }
            return {"authorize_url": result["authorize_url"], "expires_in": 300}

    def poll(self):
        with self.lock:
            if not self.flow or self.flow["expires"] <= time.time():
                raise CloudError("login_expired", 401)
            value = self.transport(
                "/v1/auth/poll", {k: v for k, v in self.flow.items() if k != "expires"}
            )
            if value.get("pending"):
                return {"pending": True}
            if (
                value.get("scope") != "desktop"
                or not isinstance(value.get("access_token"), str)
                or value.get("expires_at", 0) <= time.time()
            ):
                raise CloudError("invalid_cloud_session", 502)
            stream = self.stream
            self.stream = None
            self.session = value
            self.flow = None
            self.release = None
            self.last_check = 0
            self.next_stream = 0
            self.error = None
            if self.vault:
                self.vault.save(value)
        self.close_stream(stream)
        return self.status()

    def logout(self):
        with self.lock:
            token = self.session.get("access_token") if self.session else None
            self.session = None
            self.flow = None
            self.release = None
            self.last_check = 0
            self.error = None
            self.next_stream = 0
            stream = self.stream
            self.stream = None
            if self.vault:
                self.vault.clear()
        self.close_stream(stream)
        # Local disable takes effect even if revocation is temporarily unreachable.
        if token:
            try:
                self.transport("/v1/auth/logout", {}, token)
            except CloudError:
                pass
        return self.status()

    def call(self, path, body=None):
        with self.lock:
            return self.transport(path, body, self.authenticated()["access_token"])

    def check(self):
        with self.lock:
            self.authenticated()
            try:
                envelope = self.transport("/v1/releases/stable", None, self.session["access_token"])
                if envelope.get("available") is not False:
                    try:
                        verify(envelope)
                    except (ValueError, OSError):
                        raise CloudError("release_signature_invalid", 502) from None
                self.release = envelope
                self.error = None
            except CloudError as e:
                self.error = e.code
                raise
            finally:
                self.last_check = time.time()
            return self.release

    def background_check(self):
        # Login may change between status, connection establishment and each event.
        with self.lock:
            try:
                token = self.authenticated()["access_token"]
            except CloudError:
                return
            if time.time() < self.next_stream:
                return
        if self.custom_transport:
            if time.time() - self.last_check >= 900:
                try:
                    self.check()
                except CloudError:
                    pass
            return
        request = Request(
            ORIGIN + "/v1/releases/events",
            headers={"Accept": "text/event-stream", "Authorization": "Bearer " + token},
        )
        response = None
        try:
            with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=65) as response:
                with self.lock:
                    if not self.session or self.session.get("access_token") != token:
                        return
                    self.stream = response
                event = ""
                while True:
                    with self.lock:
                        if not self.session or self.session.get("access_token") != token:
                            break
                        if self.session["expires_at"] <= time.time():
                            self.invalidate(token)
                            break
                    line = response.readline(2 * 1024 * 1024 + 1)
                    if not line:
                        break
                    if len(line) > 2 * 1024 * 1024:
                        raise CloudError("cloud_response_limit", 502)
                    if line.startswith(b"event: "):
                        event = line[7:].strip().decode()
                    elif line.startswith(b"data: "):
                        if event == "revoked":
                            self.invalidate(token)
                            return
                        if event != "release":
                            continue
                        envelope = json.loads(line[6:])
                        if envelope.get("available") is not False:
                            verify(envelope)
                        with self.lock:
                            if self.session and self.session.get("access_token") == token:
                                self.release = envelope
                                self.last_check = time.time()
                                self.error = None
        except HTTPError as e:
            with self.lock:
                if self.session and self.session.get("access_token") == token:
                    if e.code in (401, 403):
                        self.invalidate(token)
                    else:
                        self.error = "cloud_unavailable"
                        self.next_stream = time.time() + 30
        except (OSError, URLError, ValueError, CloudError):
            with self.lock:
                if self.session and self.session.get("access_token") == token:
                    self.error = "cloud_unavailable"
                    self.next_stream = time.time() + 30
        finally:
            with self.lock:
                if self.stream is response:
                    self.stream = None
