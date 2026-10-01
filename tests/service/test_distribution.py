import http.client
import json
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from test_cloud import cloud, CloudTests


class DistributionTests(CloudTests):
    def setUp(self):
        super().setUp()
        self.server = cloud.BoundedServer(("127.0.0.1", 0), cloud.Handler)
        self.server.app = self.app
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        release = self.app.data / "releases"
        release.mkdir()
        assets = self.app.data / "artifacts"
        assets.mkdir()
        self.path = "/artifacts/test-windows.zip"
        (assets / "test-windows.zip").write_bytes(b"1234567890")
        self.manifest = {
            "manifest": {
                "sequence": 1,
                "portable": {"path": self.path, "size": 10, "sha256": "a" * 64},
                "source": {"path": "/artifacts/source.zip", "size": 1, "sha256": "b" * 64},
            }
        }
        (release / "stable.json").write_text(json.dumps(self.manifest))

    def request(self, path, headers=None, body=None, method="GET"):
        c = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        try:
            c.request(
                method,
                path,
                body=json.dumps(body) if body is not None else None,
                headers=headers or {},
            )
            r = c.getresponse()
            return r.status, dict(r.getheaders()), r.read()
        finally:
            c.close()

    def token(self):
        return self.app.exchange(self.flow())["access_token"]

    def test_anonymous_cannot_read_manifest_artifact_head_range_or_events(self):
        for path in ("/v1/releases/stable", self.path, "/v1/releases/events"):
            for method in ("GET", "HEAD"):
                status, headers, _ = self.request(path, {"Range": "bytes=0-2"}, method=method)
                self.assertEqual(status, 401)
                self.assertEqual(headers["Cache-Control"], "no-store")

    def test_authorized_range_then_revocation_blocks_cache_probe(self):
        token = self.token()
        headers = {"Authorization": "Bearer " + token, "Range": "bytes=2-4"}
        status, h, raw = self.request(self.path, headers)
        self.assertEqual((status, raw), (206, b"345"))
        self.assertEqual(h["Cache-Control"], "private, no-store")
        status, _, _ = self.request(
            "/v1/auth/logout",
            {"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            body={},
            method="POST",
        )
        self.assertEqual(status, 200)
        headers["If-None-Match"] = '"' + "a" * 64 + '"'
        self.assertEqual(self.request(self.path, headers)[0], 401)

    def test_range_errors_suffix_if_range_and_head(self):
        auth = {"Authorization": "Bearer " + self.token()}
        for value in ("bytes=10-", "bytes=8-2", "bytes=-0", "bytes=0-1,4-5"):
            with self.subTest(value=value):
                status, headers, _ = self.request(self.path, {**auth, "Range": value})
                self.assertEqual(status, 416)
                self.assertEqual(headers["Content-Range"], "bytes */10")
        status, headers, raw = self.request(self.path, {**auth, "Range": "bytes=-3"})
        self.assertEqual((status, raw, headers["Content-Range"]), (206, b"890", "bytes 7-9/10"))
        status, _, raw = self.request(
            self.path, {**auth, "Range": "bytes=2-4", "If-Range": '"old"'}
        )
        self.assertEqual((status, raw), (200, b"1234567890"))
        status, headers, raw = self.request(
            self.path, {**auth, "Range": "bytes=2-4"}, method="HEAD"
        )
        self.assertEqual((status, raw, headers["Content-Length"]), (200, b"", "10"))

    def check_parallel_downloads(self, users, per_user):
        tokens = []
        for user in range(users + 1):
            self.app.identity = lambda cookie, user=user: {
                "subject": "download-fixture-" + str(user),
                "name": "Fixture",
            }
            tokens.append(self.token())
        count = users * per_user
        entered = 0
        condition = threading.Condition()
        release = threading.Event()
        original = cloud.Handler.send_artifact

        def held_download(handler, path, token):
            nonlocal entered
            with condition:
                entered += 1
                condition.notify_all()
            if not release.wait(15):
                raise RuntimeError("download fixture timed out")
            return original(handler, path, token)

        with (
            patch.object(cloud.Handler, "send_artifact", held_download),
            ThreadPoolExecutor(max_workers=count) as workers,
        ):
            futures = [
                workers.submit(
                    self.request,
                    self.path,
                    {"Authorization": "Bearer " + tokens[user], "Range": "bytes=2-4"},
                )
                for user in range(users)
                for _ in range(per_user)
            ]
            try:
                with condition:
                    self.assertTrue(condition.wait_for(lambda: entered == count, timeout=8))
                # Per-account and global pressure must not consume API/page capacity.
                rejected = tokens[0] if users == 1 else tokens[-1]
                status, headers, body = self.request(
                    self.path, {"Authorization": "Bearer " + rejected}
                )
                self.assertEqual(status, 429)
                self.assertEqual(json.loads(body)["error"], "connection_capacity")
                self.assertIn("Retry-After", headers)
                auth = {"Authorization": "Bearer " + tokens[0]}
                for path in ("/", "/v1/session", "/v1/releases/stable"):
                    self.assertEqual(self.request(path, auth)[0], 200)
                login = {
                    "challenge": cloud.challenge("fixture-verifier"),
                    "state": "s" * 43,
                    "scope": "desktop",
                    "redirect_uri": cloud.CLOUD + "/auth/callback",
                }
                self.assertEqual(
                    self.request(
                        "/v1/auth/start",
                        {"Content-Type": "application/json"},
                        body=login,
                        method="POST",
                    )[0],
                    200,
                )
            finally:
                release.set()
            for future in futures:
                status, headers, body = future.result(timeout=10)
                self.assertEqual(
                    (status, body, headers["Content-Range"]), (206, b"345", "bytes 2-4/10")
                )
        self.assertEqual(self.app.connection_counts, {})
        self.assertEqual(self.request(self.path, auth)[0], 200)

    def test_eight_parallel_downloads_per_account_keep_login_and_pages_available(self):
        self.check_parallel_downloads(users=1, per_user=8)

    def test_32_parallel_downloads_keep_login_and_pages_available(self):
        self.check_parallel_downloads(users=4, per_user=8)

    def test_static_conditional_cache_and_head_keep_security_headers(self):
        for path in (
            "/",
            "/center",
            "/style.css",
            "/transitions.js",
            "/assets/scene-03-clean.webp",
        ):
            with self.subTest(path=path):
                status, headers, raw = self.request(path)
                self.assertEqual(status, 200)
                self.assertGreater(len(raw), 0)
                self.assertEqual(headers["Cache-Control"], "public, max-age=0, must-revalidate")
                etag = headers["ETag"]
                for condition in (etag, "W/" + etag, '"old", ' + etag, "*"):
                    status, cached, body = self.request(path, {"If-None-Match": condition})
                    self.assertEqual((status, body), (304, b""))
                    self.assertEqual(cached["ETag"], etag)
                    self.assertEqual(cached["X-Content-Type-Options"], "nosniff")
                    self.assertNotIn("Content-Length", cached)
                status, head, body = self.request(path, method="HEAD")
                self.assertEqual((status, body), (200, b""))
                self.assertEqual(head["Content-Length"], str(len(raw)))

    def test_legacy_public_write_returns_gone_before_auth_or_database(self):
        status, _, raw = self.request("/v1/community", body={}, method="POST")
        self.assertEqual(status, 410)
        self.assertEqual(json.loads(raw)["error"], "community_write_retired_use_matrix")
        self.assertEqual(self.app.feed()["posts"], [])

    def test_browser_cookie_is_httponly_token_not_returned_and_csrf_rejected(self):
        status, h, raw = self.request(
            "/v1/auth/exchange",
            {"Origin": cloud.SITE, "Content-Type": "application/json"},
            body=self.flow("browser"),
            method="POST",
        )
        self.assertEqual(status, 200)
        self.assertNotIn("access_token", json.loads(raw))
        cookie = h["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        self.assertEqual(h["Access-Control-Allow-Credentials"], "true")
        headers = {"Cookie": cookie.split(";")[0]}
        self.assertEqual(self.request(self.path, headers)[0], 200)
        self.assertEqual(
            self.request(
                "/v1/auth/logout",
                {**headers, "Content-Type": "application/json"},
                body={},
                method="POST",
            )[0],
            403,
        )
        self.assertEqual(
            self.request(
                "/v1/auth/logout",
                {**headers, "Origin": cloud.SITE, "Content-Type": "application/json"},
                body={},
                method="POST",
            )[0],
            200,
        )
        self.assertEqual(self.request(self.path, headers)[0], 401)

    def test_update_stream_delivers_changes_and_closes_on_revocation(self):
        token = self.token()
        c = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=8)
        self.addCleanup(c.close)
        c.request("GET", "/v1/releases/events", headers={"Authorization": "Bearer " + token})
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        self.assertEqual(r.readline(), b"event: release\n")
        self.assertIn(b'"sequence":1', r.readline())
        r.readline()
        self.manifest["manifest"]["sequence"] = 2
        (self.app.data / "releases/stable.json").write_text(json.dumps(self.manifest))
        deadline = time.monotonic() + 7
        changed = False
        while time.monotonic() < deadline:
            line = r.readline()
            if b'"sequence":2' in line:
                changed = True
                break
        self.assertTrue(changed)
        with self.app.db() as db:
            db.execute("UPDATE sessions SET revoked=1 WHERE hash=?", (cloud.digest(token),))
        self.assertIn(b"event: revoked", r.read())


class PortablePathsTests(unittest.TestCase):
    def test_project_references_accept_windows_and_macos_without_moving_files(self):
        from coordination_core.library import validate
        from coordination_core.core import Fault
        from datetime import datetime, timezone

        value = {
            "id": "doc",
            "title": "Doc",
            "project": "control",
            "owner": "owner",
            "kind": "directory",
            "summary": "Local reference",
            "purpose": "Test",
            "tags": [],
            "source_ref": "/Users/example/Projects/product",
            "provenance": "local",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "content_version": "1",
            "sha256": None,
            "bytes": None,
            "status": "active",
            "access": "shared",
            "expires_at": None,
            "task_id": "task",
        }
        for ref in (
            "/Users/example/Projects/product",
            "D:/Projects/product",
            "D:\\Projects\\product",
        ):
            validate({**value, "source_ref": ref}, time.time())
        for ref in ("relative/path", "/tmp/../private"):
            with self.assertRaises(Fault):
                validate({**value, "source_ref": ref}, time.time())


if __name__ == "__main__":
    unittest.main()
