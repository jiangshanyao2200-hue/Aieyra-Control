import base64
import hashlib
import hmac
import json
import secrets
import subprocess
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import test_cloud
import test_distribution
from feedback_contract import FeedbackError, canonical

cloud = test_cloud.cloud
REPORT = {
    "category": "bug",
    "severity": "normal",
    "title": "Synthetic interrupted task",
    "summary": "The task stopped; selected results were preserved.",
}


class ProductAuth:
    def begin(self, product="aieyra-os", scope="feedback"):
        verifier, state = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        body = {
            "challenge": cloud.challenge(verifier),
            "state": state,
            "redirect_uri": cloud.CLOUD + "/auth/callback",
            "scope": scope,
        }
        if scope == "feedback":
            body["product"] = product
        started = self.app.start(body)
        return started, {"flow_id": started["flow_id"], "verifier": verifier, "state": state}

    def login(self, scope="feedback", cookie="valid"):
        started, poll = self.begin(scope=scope)
        location = self.app.authorize(started["flow_id"], cookie)
        if scope == "browser":
            result = self.app.exchange(
                {
                    **poll,
                    "code": parse_qs(urlsplit(location).query)["code"][0],
                    "redirect_uri": cloud.CLOUD + "/auth/callback",
                }
            )
        else:
            result = self.app.poll(poll)
        return result, self.app.session(result["access_token"])

    def report(self, session, rid="os-feedback-fixture"):
        product = session["product"]
        channel = self.app.feedback_channel(
            session,
            {
                "installation": "a" * 64,
                "station": "b" * 64,
                "product": product,
            },
        )
        return {
            "request_id": rid,
            "channel_token": channel["channel_token"],
            "product": product,
            "version": "0.2.20" if product == "aieyra-os" else "0.6.4",
            "report": REPORT,
        }


class FeedbackProductTests(ProductAuth, unittest.TestCase):
    def setUp(self):
        test_cloud.CloudTests.setUp(self)

    def test_auth_pending_pkce_state_expiry_and_replay(self):
        started, poll = self.begin()
        self.assertEqual((started["scope"], started["product"]), ("feedback", "aieyra-os"))
        self.assertEqual(self.app.poll(poll), {"pending": True})
        for field in ("state", "verifier"):
            with self.assertRaises(cloud.Error):
                self.app.poll({**poll, field: secrets.token_urlsafe(32)})
        self.app.authorize(started["flow_id"], "valid")
        result = self.app.poll(poll)
        self.assertEqual(result["product"], "aieyra-os")
        self.assertEqual(result["scope"], "feedback")
        self.assertLessEqual(result["expires_at"], time.time() + 86400)
        with self.assertRaises(cloud.Error):
            self.app.poll(poll)
        started, poll = self.begin()
        with self.app.db() as db:
            db.execute("UPDATE flows SET expires=0 WHERE id=?", (started["flow_id"],))
        with self.assertRaises(cloud.Error):
            self.app.poll(poll)

    def test_auth_product_validation_and_missing_bindings_fail_closed(self):
        for product in (None, [], {}, "aieyra-control", "unknown"):
            with self.subTest(product=product), self.assertRaises(cloud.Error):
                self.begin(product=product)
        started, poll = self.begin()
        self.app.authorize(started["flow_id"], "valid")
        with self.app.db() as db:
            db.execute("DELETE FROM auth_flow_products WHERE flow=?", (started["flow_id"],))
        with self.assertRaises(cloud.Error):
            self.app.poll(poll)
        result, session = self.login()
        with self.app.db() as db:
            db.execute("DELETE FROM auth_session_products WHERE session=?", (session["hash"],))
        with self.assertRaises(cloud.Error):
            self.app.session(result["access_token"])

    def test_exchange_persists_product_and_legacy_migration(self):
        started, poll = self.begin()
        location = self.app.authorize(started["flow_id"], "valid")
        result = self.app.exchange(
            {
                **poll,
                "code": parse_qs(urlsplit(location).query)["code"][0],
                "redirect_uri": cloud.CLOUD + "/auth/callback",
            }
        )
        self.app = cloud.Cloud(self.tmp.name)
        self.assertEqual(self.app.session(result["access_token"])["product"], "aieyra-os")
        self.app.identity = lambda cookie: {"subject": "fixture", "name": "Fixture"}
        old = test_cloud.CloudTests.flow(self)
        with self.app.db() as db:
            db.execute("DELETE FROM auth_flow_products WHERE flow=?", (old["flow_id"],))
        control = self.app.exchange(old)
        with self.app.db() as db:
            db.execute(
                "DELETE FROM auth_session_products WHERE session=?",
                (cloud.digest(control["access_token"]),),
            )
        self.assertEqual(self.app.session(control["access_token"])["product"], "aieyra-control")

    def test_products_and_accounts_isolated_but_browser_sees_own_products(self):
        _, os = self.login()
        _, control = self.login("desktop")
        os_ticket = self.app.submit_feedback(os, self.report(os))
        control_ticket = self.app.submit_feedback(control, self.report(control, "control-fixture"))
        self.assertEqual(self.app.list_feedback(os)["tickets"][0]["product"], "aieyra-os")
        self.assertEqual(len(self.app.list_feedback(os)["tickets"]), 1)
        with self.assertRaises(FeedbackError):
            self.app.list_feedback(os, control_ticket["id"])
        with self.assertRaises(FeedbackError):
            self.app.list_feedback(control, os_ticket["id"])
        _, browser = self.login("browser")
        self.assertEqual(
            {x["product"] for x in self.app.list_feedback(browser)["tickets"]},
            {"aieyra-control", "aieyra-os"},
        )
        self.app.identity = lambda cookie: {"subject": "other", "name": "Other"}
        _, other = self.login()
        self.assertEqual(self.app.list_feedback(other)["tickets"], [])
        with self.assertRaises(FeedbackError):
            self.app.list_feedback(other, os_ticket["id"])

    def test_channel_product_session_expiry_and_legacy_token(self):
        _, os = self.login()
        _, control = self.login("desktop")
        b = self.report(os)
        with self.assertRaises(FeedbackError):
            self.app.submit_feedback(control, b)
        with self.assertRaises(FeedbackError) as mismatch:
            self.app.submit_feedback(os, {**b, "product": "aieyra-control"})
        self.assertEqual(mismatch.exception.code, "feedback_product_mismatch")
        for fields in (
            {"installation": "a" * 64, "station": "b" * 64},
            {"installation": "a" * 64, "station": "b" * 64, "product": "aieyra-control"},
        ):
            with self.assertRaises(FeedbackError):
                self.app.feedback_channel(os, fields)
        old = self.report(control, "legacy-channel")
        raw = old["channel_token"].split(".")[0]
        claims = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        del claims["product"]
        raw = base64.urlsafe_b64encode(canonical(claims).encode()).decode().rstrip("=")
        old["channel_token"] = (
            raw + "." + hmac.new(self.app.feedback_key, raw.encode(), hashlib.sha256).hexdigest()
        )
        self.assertEqual(self.app.submit_feedback(control, old)["product"], "aieyra-control")
        with patch("feedback_store.time.time", return_value=time.time() + 601):
            with self.assertRaises(FeedbackError):
                self.app.submit_feedback(os, b)

    def test_parallel_retry_restart_and_changed_payload(self):
        _, os = self.login()
        b = self.report(os)
        barrier = threading.Barrier(2)

        def submit():
            barrier.wait(timeout=5)
            return self.app.submit_feedback(os, b)

        with ThreadPoolExecutor(max_workers=2) as pool:
            rows = list(pool.map(lambda _: submit(), range(2)))
        self.assertEqual(len({x["id"] for x in rows}), 1)
        self.assertEqual(sum(not x["replayed"] for x in rows), 1)
        self.app = cloud.Cloud(self.tmp.name)
        renewed = self.report(os)
        self.assertEqual(self.app.submit_feedback(os, renewed)["id"], rows[0]["id"])
        with self.assertRaises(FeedbackError) as conflict:
            self.app.submit_feedback(os, {**renewed, "report": {**REPORT, "summary": "Changed"}})
        self.assertEqual(
            (conflict.exception.code, conflict.exception.status), ("feedback_request_conflict", 409)
        )
        with self.app.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM feedback").fetchone()[0], 1)

    def test_cross_product_request_conflicts_and_quota_remain_account_wide(self):
        _, control = self.login("desktop")
        old = self.app.submit_feedback(control, self.report(control, "shared-id"))
        _, os = self.login()
        with self.assertRaises(FeedbackError) as conflict:
            self.app.submit_feedback(os, self.report(os, "shared-id"))
        self.assertEqual(conflict.exception.status, 409)
        with self.app.db() as db:
            for i in range(19):
                db.execute(
                    "INSERT INTO feedback SELECT ?,subject,?,digest,installation,station,product,version,report,status,revision,note,created,updated FROM feedback WHERE id=?",
                    ("ACF-" + f"{i:024x}", "quota-" + str(i), old["id"]),
                )
        with self.assertRaises(FeedbackError) as quota:
            self.app.submit_feedback(os, self.report(os, "new-os"))
        self.assertEqual(quota.exception.code, "feedback_daily_limit")

    def test_admin_preserves_product_and_revision_audit(self):
        _, os = self.login()
        ticket = self.app.submit_feedback(os, self.report(os))
        cmd = [
            sys.executable,
            str(Path(cloud.__file__).parent / "admin.py"),
            "--data",
            self.tmp.name,
        ]
        result = subprocess.run([*cmd, "feedback-list"], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)["tickets"][0]["product"], "aieyra-os")
        result = subprocess.run(
            [
                *cmd,
                "feedback-update",
                ticket["id"],
                "--revision",
                "1",
                "--status",
                "triaged",
                "--note",
                "Synthetic review",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(json.loads(result.stdout)["revision"], 2)
        with self.app.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM feedback_audit").fetchone()[0], 1)

    def test_expired_and_blocked_feedback_sessions_are_rejected(self):
        result, session = self.login()
        with self.app.db() as db:
            db.execute("UPDATE sessions SET expires=0 WHERE hash=?", (session["hash"],))
        with self.assertRaises(cloud.Error):
            self.app.session(result["access_token"])
        result, session = self.login()
        started, pending = self.begin()
        self.app.authorize(started["flow_id"], "valid")
        with self.app.db() as db:
            db.execute("INSERT INTO blocked VALUES(?,?)", (session["subject"], "fixture"))
        with self.assertRaises(cloud.Error):
            self.app.session(result["access_token"])
        with self.assertRaises(cloud.Error):
            self.app.poll(pending)


class FeedbackProductHttpTests(ProductAuth, unittest.TestCase):
    def setUp(self):
        test_cloud.CloudTests.setUp(self)
        self.server = cloud.BoundedServer(("127.0.0.1", 0), cloud.Handler)
        self.server.app = self.app
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    request = test_distribution.DistributionTests.request

    def test_capabilities_native_login_submit_query_logout_and_scope_boundary(self):
        status, _, raw = self.request("/v1/feedback/capabilities")
        self.assertEqual(status, 200)
        self.assertIn("aieyra-os", json.loads(raw)["products"])
        verifier, state = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        headers = {"Content-Type": "application/json"}
        status, _, raw = self.request(
            "/v1/auth/start",
            headers,
            {
                "challenge": cloud.challenge(verifier),
                "state": state,
                "redirect_uri": cloud.CLOUD + "/auth/callback",
                "scope": "feedback",
                "product": "aieyra-os",
            },
            "POST",
        )
        self.assertEqual(status, 200)
        started = json.loads(raw)
        self.app.authorize(started["flow_id"], "valid")
        status, _, raw = self.request(
            "/v1/auth/poll",
            headers,
            {"flow_id": started["flow_id"], "verifier": verifier, "state": state},
            "POST",
        )
        self.assertEqual(status, 200)
        token = json.loads(raw)["access_token"]
        headers["Authorization"] = "Bearer " + token
        self.assertEqual(
            json.loads(self.request("/v1/session", headers)[2])["product"], "aieyra-os"
        )
        status, _, raw = self.request(
            "/v1/feedback/channel",
            headers,
            {"installation": "a" * 64, "station": "b" * 64, "product": "aieyra-os"},
            "POST",
        )
        self.assertEqual(status, 200)
        body = {
            "request_id": "os-feedback-http",
            "channel_token": json.loads(raw)["channel_token"],
            "product": "aieyra-os",
            "version": "0.2.20",
            "report": REPORT,
        }
        status, _, raw = self.request("/v1/feedback", headers, body, "POST")
        self.assertEqual(status, 200)
        ticket = json.loads(raw)
        self.assertEqual(ticket["product"], "aieyra-os")
        self.assertEqual(
            json.loads(self.request("/v1/feedback/" + ticket["id"], headers)[2])["tickets"][0][
                "id"
            ],
            ticket["id"],
        )
        self.assertEqual(self.request("/v1/community", headers, {}, "POST")[0], 410)
        for path in ("/v1/matrix/topics", "/v1/matrix/growth"):
            self.assertEqual(self.request(path, headers, {}, "POST")[0], 403, path)
        for path in (
            "/v1/releases/stable",
            "/v1/releases/events",
            "/artifacts/test.zip",
            "/v1/matrix/status",
            "/v1/matrix/events",
        ):
            self.assertEqual(self.request(path, headers)[0], 403, path)
        control, _ = self.login("desktop")
        self.assertEqual(self.request("/v1/auth/logout", headers, {}, "POST")[0], 200)
        self.assertEqual(self.request("/v1/feedback", headers)[0], 401)
        self.assertEqual(self.app.session(control["access_token"])["scope"], "desktop")

    def test_invalid_product_shapes_and_callback_are_client_errors(self):
        body = {
            "challenge": cloud.challenge(secrets.token_urlsafe(32)),
            "state": secrets.token_urlsafe(32),
            "redirect_uri": cloud.CLOUD + "/auth/callback",
            "scope": "feedback",
            "product": "aieyra-os",
        }
        for field, value in (
            ("product", []),
            ("scope", {}),
            ("redirect_uri", []),
            ("redirect_uri", cloud.SITE + "/auth/callback"),
            ("public_key", "x"),
        ):
            status, _, _ = self.request(
                "/v1/auth/start",
                {"Content-Type": "application/json"},
                {**body, field: value},
                "POST",
            )
            self.assertEqual(status, 400, (field, value))
