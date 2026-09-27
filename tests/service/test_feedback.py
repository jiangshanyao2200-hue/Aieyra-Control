import json
import time
import unittest
from unittest.mock import patch
from test_cloud import CloudTests, cloud
from test_station_continuity import StationContinuityTests
from test_distribution import DistributionTests
from feedback_contract import FeedbackError, prepare, redact
from feedback import Feedback
from cloud_link import CloudError
from agent_access import AgentError

REPORT = {
    "category": "bug",
    "severity": "normal",
    "title": "Update reconnect failed",
    "summary": "Reconnection remains stuck after a network interruption.",
    "steps": "Disconnect and reconnect",
    "expected": "Resume",
    "actual": "Stuck",
}


class ContractTests(unittest.TestCase):
    def test_invalid_classification_types_have_bounded_client_errors(self):
        for field in ("category", "severity"):
            for value in ([], {}, None, True, 1):
                with (
                    self.subTest(field=field, value=value),
                    self.assertRaises(FeedbackError) as caught,
                ):
                    prepare({**REPORT, field: value})
                self.assertEqual(caught.exception.code, "invalid_feedback_classification")

    def test_redacts_secrets_addresses_paths_and_opaque_values(self):
        original = (
            "Authorization: Bearer sensitive-value\n"
            + r"C:\Users\private\code\test.py"
            + "\n192.0.2.10 2001:db8::1\nfoo@example.invalid\nhttps://host.invalid/private?token=abc\n"
            + "A" * 48
        )
        result = redact(original)
        for secret in (
            "sensitive-value",
            "private\\code",
            "192.0.2.10",
            "2001:db8",
            "foo@",
            "host.invalid",
            "A" * 48,
        ):
            self.assertNotIn(secret, result)

    def test_strict_fields_and_utf8_size(self):
        for extra in ({"files": ["x"]}, {"project_memory": "x"}):
            with self.assertRaises(FeedbackError):
                prepare({**REPORT, **extra})
        with self.assertRaises(FeedbackError):
            prepare(
                {**REPORT, "diagnostics": "中" * 1200, "steps": "中" * 1000, "actual": "中" * 1000}
            )
        with self.assertRaises(FeedbackError):
            prepare({**REPORT, "title": ""})


class CloudFeedbackTests(CloudTests):
    def session(self, scope="desktop"):
        return self.app.session(self.app.exchange(self.flow(scope))["access_token"])

    def body(self, session, rid="report-one", report=None):
        channel = self.app.feedback_channel(
            session, {"installation": "a" * 64, "station": "b" * 64}
        )
        return {
            "channel_token": channel["channel_token"],
            "request_id": rid,
            "product": "aieyra-control",
            "version": "0.6.1",
            "report": report or REPORT,
        }

    def test_private_idempotent_and_no_public_post(self):
        s = self.session()
        b = self.body(s)
        r = self.app.submit_feedback(s, b)
        self.assertEqual(r["status"], "received")
        self.assertEqual(self.app.submit_feedback(s, b)["id"], r["id"])
        with self.assertRaises(FeedbackError):
            self.app.submit_feedback(s, {**b, "report": {**REPORT, "title": "other"}})
        self.assertEqual(self.app.feed()["posts"], [])
        other = {**s, "subject": "other"}
        self.assertEqual(self.app.list_feedback(other)["tickets"], [])
        with self.assertRaises(FeedbackError):
            self.app.list_feedback(other, r["id"])

    def test_channel_scope_tamper_expiry_and_session_binding(self):
        s = self.session()
        b = self.body(s)
        with self.assertRaises(FeedbackError):
            self.app.feedback_channel(
                self.session("browser"), {"installation": "a" * 64, "station": "b" * 64}
            )
        for token in ("invalid", b["channel_token"][:-1] + "x"):
            with self.assertRaises(FeedbackError):
                self.app.submit_feedback(s, {**b, "channel_token": token})
        with self.assertRaises(FeedbackError):
            self.app.submit_feedback(self.session(), b)
        with patch("feedback_store.time.time", return_value=time.time() + 601):
            with self.assertRaises(FeedbackError):
                self.app.submit_feedback(s, b)

    def test_defense_in_depth_redaction_and_account_quota(self):
        s = self.session()
        b = self.body(s, report={**REPORT, "diagnostics": "api_key=do-not-store"})
        r = self.app.submit_feedback(s, b)
        self.assertNotIn("do-not-store", json.dumps(self.app.list_feedback(s)))
        with self.app.db() as db:
            for i in range(19):
                db.execute(
                    "INSERT INTO feedback SELECT ?,subject,?,digest,installation,station,product,version,report,status,revision,note,created,updated FROM feedback WHERE id=?",
                    ("ACF-" + f"{i:024x}", str(i), r["id"]),
                )
        with self.assertRaises(FeedbackError):
            self.app.submit_feedback(s, {**b, "request_id": "new"})
        self.assertTrue(self.app.submit_feedback(s, b)["replayed"])

    def test_memory_limiter_and_concurrency_release(self):
        self.app.ingress_limit("test", 1, 60)
        with self.assertRaises(cloud.Error):
            self.app.ingress_limit("test", 1, 60)
        with self.app.capacity("stream", "user", 1, 2):
            with self.assertRaises(cloud.Error):
                with self.app.capacity("stream", "user", 1, 2):
                    pass
        self.assertEqual(self.app.connection_counts, {})


class FeedbackHttpTests(DistributionTests):
    def test_invalid_classification_returns_400_then_valid_report_succeeds(self):
        headers = {"Authorization": "Bearer " + self.token(), "Content-Type": "application/json"}
        code, _, raw = self.request(
            "/v1/feedback/channel", headers, {"installation": "a" * 64, "station": "b" * 64}, "POST"
        )
        self.assertEqual(code, 200)
        body = {
            "channel_token": json.loads(raw)["channel_token"],
            "request_id": "invalid-shape",
            "product": "aieyra-control",
            "version": "0.6.1",
            "report": {**REPORT, "category": []},
        }
        code, _, raw = self.request("/v1/feedback", headers, body, "POST")
        self.assertEqual(code, 400)
        self.assertIn("invalid_feedback_classification", raw.decode())
        body["report"] = REPORT
        self.assertEqual(self.request("/v1/feedback", headers, body, "POST")[0], 200)

    def test_anonymous_feedback_denied_and_browser_read_only(self):
        self.assertEqual(self.request("/v1/feedback")[0], 401)
        token = self.app.exchange(self.flow("browser"))["access_token"]
        headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
        self.assertEqual(
            self.request(
                "/v1/feedback/channel",
                headers,
                {"installation": "a" * 64, "station": "b" * 64},
                "POST",
            )[0],
            403,
        )
        self.assertEqual(self.request("/v1/feedback", headers)[0], 200)
        self.assertEqual(self.request("/feedback")[0], 200)

    def test_forwarded_ip_ignored_without_exact_trust(self):
        self.app.ingress_limit("ip:127.0.0.1", 600, 60)
        with self.app.rate_lock:
            self.app.rate_buckets["ip:127.0.0.1"] = (time.monotonic(), 600)
        self.assertEqual(
            self.request(
                "/healthz", {"X-Control-Client-IP": "192.0.2.8", "X-Forwarded-For": "192.0.2.8"}
            )[0],
            429,
        )
        self.app.trusted_proxies = {"127.0.0.1"}
        self.assertEqual(self.request("/healthz", {"X-Control-Client-IP": "192.0.2.8"})[0], 200)
        self.assertEqual(self.request("/healthz", {"X-Control-Client-IP": "garbage"})[0], 400)

    def test_channel_then_submit_receipt_and_owner_detail(self):
        headers = {"Authorization": "Bearer " + self.token(), "Content-Type": "application/json"}
        code, _, raw = self.request(
            "/v1/feedback/channel", headers, {"installation": "a" * 64, "station": "b" * 64}, "POST"
        )
        self.assertEqual(code, 200)
        b = {
            "channel_token": json.loads(raw)["channel_token"],
            "request_id": "one",
            "product": "aieyra-control",
            "version": "0.6.1",
            "report": REPORT,
        }
        code, _, raw = self.request("/v1/feedback", headers, b, "POST")
        self.assertEqual(code, 200)
        self.assertEqual(self.request("/v1/feedback/" + json.loads(raw)["id"], headers)[0], 200)
        self.assertEqual(
            self.request(
                "/v1/feedback/" + json.loads(raw)["id"], {"Authorization": "Bearer " + self.token()}
            )[0],
            200,
        )


class LocalFeedbackTests(StationContinuityTests):
    def value(self, rid="issue"):
        return {
            "session_id": "leader-one",
            "request_id": rid,
            "report": REPORT,
            "privacy_reviewed": True,
        }

    def enqueue(self, rid="issue"):
        return self.access.cloud_action(self.leader, "feedback", self.value(rid))

    def login(self, subject="account"):
        self.app.cloud.session = {
            "access_token": "token",
            "scope": "desktop",
            "expires_at": time.time() + 300,
            "user": {"subject": subject, "name": "fixture"},
        }

    def transport(self, path, body, token):
        if path.endswith("/channel"):
            return {"channel_token": "fixture-channel"}
        return {
            "id": "ACF-" + "a" * 24,
            "request_id": body["request_id"],
            "status": "received",
            "revision": 1,
            "note": "",
            "created": time.time(),
            "updated": time.time(),
        }

    def test_worker_cannot_submit_or_read_queue(self):
        self.join(self.worker, self.worker_seat, "worker-one")
        with self.assertRaises(AgentError):
            self.access.cloud_action(
                self.worker, "feedback", {**self.value(), "session_id": "worker-one"}
            )
        with self.assertRaises(AgentError):
            self.access.require_leader(self.worker)

    def test_offline_durable_redaction_and_identical_retry(self):
        v = self.value()
        v["report"] = {**REPORT, "diagnostics": "password=never-persist"}
        r = self.access.cloud_action(self.leader, "feedback", v)
        self.assertTrue(r["redacted"])
        with patch.object(
            self.app.cloud, "transport", side_effect=AssertionError("offline network")
        ):
            self.app.feedback.flush()
        self.assertEqual(self.app.feedback.list(self.leader)["feedback"][0]["state"], "queued")
        self.app.feedback = Feedback(self.app)
        self.login()
        with patch.object(self.app.cloud, "transport", side_effect=self.transport):
            self.app.feedback.flush()
        self.assertEqual(self.app.feedback.list(self.leader)["feedback"][0]["state"], "accepted")
        self.assertTrue(self.access.cloud_action(self.leader, "feedback", v)["replayed"])
        with self.app.store.db() as db:
            self.assertNotIn(
                "never-persist", db.execute("SELECT payload FROM feedback_outbox").fetchone()[0]
            )

    def test_account_switch_does_not_deliver_old_report(self):
        self.login("one")
        self.enqueue()
        self.login("two")
        with patch.object(
            self.app.cloud, "transport", side_effect=AssertionError("wrong-account network")
        ):
            self.app.feedback.flush()
        self.assertEqual(self.app.feedback.list(self.leader)["feedback"][0]["state"], "queued")

    def test_old_account_does_not_starve_current_account(self):
        self.login("old")
        for i in range(3):
            self.enqueue("old-" + str(i))
        self.login("current")
        current = self.enqueue("current")
        with patch.object(self.app.cloud, "transport", side_effect=self.transport):
            self.app.feedback.flush()
        rows = {r["id"]: r for r in self.app.feedback.list(self.leader)["feedback"]}
        self.assertEqual(rows[current["id"]]["state"], "accepted")
        self.assertEqual(sum(r["state"] == "queued" for r in rows.values()), 3)

    def test_maintenance_response_polling_and_wrong_receipt_backoff(self):
        self.login()
        r = self.enqueue()
        with patch.object(self.app.cloud, "transport", side_effect=self.transport):
            self.app.feedback.flush()
        with self.app.store.db() as db:
            db.execute("UPDATE feedback_outbox SET next_try=0")
        receipt = {
            **self.transport("", {"request_id": r["id"]}, ""),
            "status": "resolved",
            "revision": 2,
            "note": "Verified fix available",
        }
        with patch.object(
            self.app.cloud, "transport", return_value={"tickets": [receipt]}
        ) as transport:
            self.app.feedback.flush()
        transport.assert_called_once_with("/v1/feedback/" + receipt["id"], None, "token")
        row = self.app.feedback.list(self.leader)["feedback"][0]
        self.assertEqual(row["receipt"]["note"], "Verified fix available")
        self.assertEqual(row["receipt"]["revision"], 2)
        with self.app.store.db() as db:
            self.assertGreater(
                db.execute("SELECT next_try FROM feedback_outbox").fetchone()[0], time.time() + 290
            )
            db.execute("UPDATE feedback_outbox SET next_try=0")
        with patch.object(
            self.app.cloud,
            "transport",
            return_value={"tickets": [{**receipt, "request_id": "other"}]},
        ):
            self.app.feedback.flush()
        row = self.app.feedback.list(self.leader)["feedback"][0]
        self.assertEqual(row["receipt"], receipt)
        self.assertEqual(row["error"], "invalid_feedback_receipt")

    def test_revoked_receipts_do_not_starve_queued_reports(self):
        self.login()
        for i in range(3):
            self.enqueue("received-" + str(i))
        with patch.object(self.app.cloud, "transport", side_effect=self.transport):
            self.app.feedback.flush()
        with self.app.store.db() as db:
            db.execute("UPDATE feedback_outbox SET credential_id='revoked-fixture',next_try=0")
        current = self.enqueue("current")
        with patch.object(self.app.cloud, "transport", side_effect=self.transport):
            self.app.feedback.flush()
            self.app.feedback.flush()
        rows = {r["id"]: r for r in self.app.feedback.list(self.leader)["feedback"]}
        self.assertEqual(rows[current["id"]]["state"], "accepted")

    def test_revoke_leader_stops_delivery(self):
        self.enqueue()
        self.login()
        with (
            patch.object(self.access, "is_leader", return_value=False),
            patch.object(
                self.app.cloud, "transport", side_effect=AssertionError("revoked network")
            ),
        ):
            self.app.feedback.flush()
        self.assertEqual(self.app.feedback.list(self.leader)["feedback"][0]["state"], "paused")

    def test_unknown_result_retries_same_id_and_has_backoff(self):
        r = self.enqueue()
        self.login()
        with patch.object(
            self.app.cloud, "transport", side_effect=CloudError("cloud_unavailable", 503)
        ):
            self.app.feedback.flush()
        with self.app.store.db() as db:
            row = db.execute("SELECT * FROM feedback_outbox").fetchone()
            self.assertGreater(row["next_try"], time.time())
            self.assertEqual(row["attempts"], 1)
            db.execute("UPDATE feedback_outbox SET next_try=0")
        seen = []

        def transport(path, body, token):
            if not path.endswith("/channel"):
                seen.append(body["request_id"])
            return self.transport(path, body, token)

        with patch.object(self.app.cloud, "transport", side_effect=transport):
            self.app.feedback.flush()
        self.assertEqual(seen, [r["id"]])

    def test_privacy_review_and_cancel(self):
        with self.assertRaises(FeedbackError):
            self.access.cloud_action(
                self.leader, "feedback", {**self.value(), "privacy_reviewed": False}
            )
        r = self.enqueue()
        self.access.cloud_action(
            self.leader, "feedback-cancel", {"session_id": "leader-one", "id": r["id"]}
        )
        self.login()
        with patch.object(
            self.app.cloud, "transport", side_effect=AssertionError("cancelled network")
        ):
            self.app.feedback.flush()
        self.assertEqual(self.app.feedback.list(self.leader)["feedback"][0]["state"], "cancelled")


if __name__ == "__main__":
    unittest.main()
