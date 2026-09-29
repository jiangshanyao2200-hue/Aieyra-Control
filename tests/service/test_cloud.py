import importlib.util
import io
import json
import secrets
import tempfile
import threading
import time
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from test_control import ROOT
from cloud_link import CloudLink, CloudError

spec = importlib.util.spec_from_file_location("control_cloud", ROOT / "cloud/server.py")
cloud = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cloud)


class CloudTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = cloud.Cloud(
            self.tmp.name,
            identity=lambda cookie: {"subject": "fixture", "name": "Fixture"}
            if cookie == "valid"
            else None,
        )

    def flow(self, scope="desktop"):
        verifier = secrets.token_urlsafe(32)
        state = secrets.token_urlsafe(32)
        redirect = cloud.CLOUD + "/auth/callback"
        started = self.app.start(
            {
                "challenge": cloud.challenge(verifier),
                "state": state,
                "scope": scope,
                "redirect_uri": redirect,
            }
        )
        location = self.app.authorize(started["flow_id"], "valid")
        code = parse_qs(urlsplit(location).query)["code"][0]
        return {
            "flow_id": started["flow_id"],
            "verifier": verifier,
            "state": state,
            "redirect_uri": redirect,
            "code": code,
        }

    def test_pkce_state_redirect_replay_and_scopes(self):
        value = self.flow()
        for field, wrong in [
            ("verifier", secrets.token_urlsafe(32)),
            ("state", secrets.token_urlsafe(32)),
            ("redirect_uri", "https://evil.invalid/"),
        ]:
            with self.assertRaises(cloud.Error):
                self.app.exchange({**value, field: wrong})
        result = self.app.exchange(value)
        with self.assertRaises(cloud.Error):
            self.app.exchange(value)
        self.assertEqual(self.app.session(result["access_token"])["subject"], "fixture")
        browser = self.app.session(self.app.exchange(self.flow("browser"))["access_token"])
        with self.assertRaises(cloud.Error):
            self.app.post(browser, {})

    def test_retired_community_cannot_publish_even_with_desktop_session(self):
        session = self.app.session(self.app.exchange(self.flow())["access_token"])
        with self.assertRaises(cloud.Error) as error:
            self.app.post(session, {})
        self.assertEqual(error.exception.status, 410)
        self.assertEqual(error.exception.code, "community_write_retired_use_matrix")
        self.assertEqual(self.app.feed()["posts"], [])

    def test_desktop_poll_requires_initiating_secret(self):
        value = self.flow()
        poll = {k: v for k, v in value.items() if k in ("flow_id", "verifier", "state")}
        with self.assertRaises(cloud.Error):
            self.app.poll({**poll, "verifier": secrets.token_urlsafe(32)})
        result = self.app.poll(poll)
        self.assertEqual(result["scope"], "desktop")
        with self.assertRaises(cloud.Error):
            self.app.poll(poll)

    def test_expired_official_cookie_returns_to_login(self):
        self.app.identity = self.app.newapi_identity
        with patch.object(cloud, "build_opener") as factory:
            factory.return_value.open.side_effect = HTTPError(
                cloud.API + "/api/aieyra/session", 401, "expired", {}, io.BytesIO(b"{}")
            )
            started = self.app.start(
                {
                    "challenge": cloud.challenge(secrets.token_urlsafe(32)),
                    "state": secrets.token_urlsafe(32),
                    "scope": "desktop",
                    "redirect_uri": cloud.CLOUD + "/auth/callback",
                }
            )
            location = self.app.authorize(started["flow_id"], "session=expired-fixture")
            self.assertEqual(urlsplit(location).path, "/sign-in")
            self.assertEqual(
                parse_qs(urlsplit(location).query)["redirect"],
                ["/aieyra/control/authorize?flow=" + started["flow_id"]],
            )

    def test_identity_outage_is_not_treated_as_expired_cookie(self):
        with patch.object(cloud, "build_opener") as factory:
            factory.return_value.open.side_effect = HTTPError(
                cloud.API + "/api/aieyra/session", 503, "offline", {}, io.BytesIO(b"{}")
            )
            with self.assertRaises(cloud.Error) as error:
                self.app.newapi_identity("session=expired-fixture")
            self.assertEqual(error.exception.status, 503)

    def test_pending_login_recovery_and_repeated_local_poll(self):
        calls = []

        def transport(path, body=None, token=None):
            calls.append(path)
            return self.app.start(body) if path.endswith("/start") else self.app.poll(body)

        link = CloudLink(transport)
        started = link.start()
        pending = link.status()
        self.assertEqual(pending["authorize_url"], started["authorize_url"])
        self.assertGreater(pending["expires_in"], 0)
        self.assertNotIn("verifier", json.dumps(pending))
        self.app.authorize(link.flow["flow_id"], "valid")
        current = link.poll()
        self.assertTrue(current["enabled"])
        self.assertEqual(link.poll(), current)
        self.assertEqual(calls, ["/v1/auth/start", "/v1/auth/poll"])

    def test_unsigned_outbound_disabled_until_login_and_after_logout(self):
        calls = []

        def transport(path, body=None, token=None):
            calls.append(path)
            if path.endswith("/start"):
                return self.app.start(body)
            if path.endswith("/poll"):
                return self.app.poll(body)
            if path.endswith("/logout"):
                return {}
            return {"available": False}

        link = CloudLink(transport)
        with patch("socket.socket.connect", side_effect=AssertionError("network forbidden")):
            for _ in range(3):
                link.status()
                link.background_check()
            self.assertEqual(calls, [])
            with self.assertRaises(CloudError):
                link.check()
            link.start()
            self.app.authorize(link.flow["flow_id"], "valid")
            link.poll()
            link.check()
            self.assertTrue(link.status()["enabled"])
            link.logout()
            before = len(calls)
            for _ in range(3):
                link.background_check()
            self.assertEqual(len(calls), before)
            self.assertFalse(link.status()["enabled"])


class CloudConnectionRaceTests(unittest.TestCase):
    def link(self):
        link = CloudLink()
        link.session = {
            "access_token": "old",
            "expires_at": time.time() + 3600,
            "user": {"name": "Old"},
        }
        return link

    def switched(self, link):
        link.session = {
            "access_token": "new",
            "expires_at": time.time() + 3600,
            "user": {"name": "New"},
        }
        link.error = None
        link.next_stream = 0

    def test_logout_between_connection_start_and_response_drops_stream(self):
        link = self.link()
        entered = threading.Event()
        resume = threading.Event()
        errors = []

        class Response:
            closed = False

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.closed = True

            def readline(self, *args):
                raise AssertionError("logged-out stream must not be read")

        response = Response()

        class Opener:
            def open(self, *args, **kwargs):
                entered.set()
                assert resume.wait(3)
                return response

        def run():
            try:
                link.background_check()
            except BaseException as error:
                errors.append(error)

        with (
            patch("cloud_link.build_opener", return_value=Opener()),
            patch.object(link, "transport", return_value={}),
        ):
            worker = threading.Thread(target=run)
            worker.start()
            try:
                self.assertTrue(entered.wait(3))
                link.logout()
            finally:
                resume.set()
                worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(response.closed)
        self.assertIsNone(link.stream)
        self.assertFalse(link.status()["enabled"])

    def test_stale_http_failure_does_not_revoke_new_login(self):
        for error in (
            HTTPError("https://example.invalid", 401, "revoked", {}, None),
            URLError("old connection"),
        ):
            with self.subTest(error=type(error).__name__):
                link = self.link()

                class Opener:
                    def open(inner, *args, **kwargs):
                        self.switched(link)
                        raise error

                with patch("cloud_link.build_opener", return_value=Opener()):
                    link.background_check()
                self.assertEqual(link.session["access_token"], "new")
                self.assertIsNone(link.error)
                self.assertEqual(link.next_stream, 0)

    def test_stale_revocation_cannot_clear_new_session_or_its_stream(self):
        link = self.link()
        new_stream = object()

        class Response:
            calls = 0

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def readline(inner, *args):
                inner.calls += 1
                if inner.calls == 1:
                    return b"event: revoked\n"
                self.switched(link)
                link.stream = new_stream
                return b"data: {}\n"

        class Opener:
            def open(self, *args, **kwargs):
                return Response()

        with patch("cloud_link.build_opener", return_value=Opener()):
            link.background_check()
        self.assertEqual(link.session["access_token"], "new")
        self.assertIs(link.stream, new_stream)

    def test_expired_login_closes_stream_and_stays_local(self):
        link = self.link()
        closed = []

        class Stream:
            def close(self):
                closed.append(True)

        link.stream = Stream()
        link.session["expires_at"] = 0
        with patch("cloud_link.build_opener", side_effect=AssertionError("network forbidden")):
            link.background_check()
        self.assertEqual(closed, [True])
        self.assertIsNone(link.stream)
        self.assertIsNone(link.session)


if __name__ == "__main__":
    unittest.main()
