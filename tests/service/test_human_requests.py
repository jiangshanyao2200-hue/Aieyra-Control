import concurrent.futures
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("human_control", ROOT / "service/main.py")
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)


class Hub:
    def __init__(self):
        self.calls = 0
        self.offline = self.drop_before = self.drop_after = False
        self.lock = threading.Lock()
        self.item = dict(
            id="human-1",
            version=1,
            category="direction",
            state="waiting_user",
            question="Choose format",
            recommendation="Plain text",
            impact="Only the report format",
            resume_summary="Write the original report",
            options=[{"id": "plain", "label": "Plain text"}, {"id": "json", "label": "JSON"}],
            allow_text=False,
            created=time.time(),
            expires=time.time() + 3600,
            observed_at=time.time(),
            project="os",
            fresh=True,
            binding_current=True,
            can_decide=True,
            decision=None,
            native_request_id="native-human",
            request_token_sha256="not-for-renderer",
            private="PRIVATE_AUTH_MUST_NOT_LEAK",
        )

    def human_snapshot(self, project, after=""):
        if self.offline:
            raise OSError("private transport")
        return {"human_requests": [copy.deepcopy(self.item)], "humans_has_more": False}

    def human_get(self, ident):
        if self.offline:
            raise OSError("private transport")
        return copy.deepcopy(self.item)

    def human_decide(self, value):
        with self.lock:
            self.calls += 1
            if self.drop_before:
                raise OSError("before commit")
            if value["version"] != self.item["version"]:
                raise ValueError("version_conflict")
            self.item.update(
                version=2,
                state="decision_recorded",
                decision=value["decision"],
                decision_request_id=value["request_id"],
                can_decide=False,
            )
            if self.drop_after:
                raise OSError("after commit")
            return copy.deepcopy(self.item)


class HumanServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.hub = Hub()
        self.config = {"resources": [], "human_projects": ["os"]}
        self.app = control.Application(self.config, self.root, hub=self.hub)
        self.body = {
            "request_id": "decision-1",
            "expected_version": 1,
            "decision": {"option_id": "plain"},
        }

    def test_projection_does_not_copy_secrets_or_invent_blockers(self):
        self.app.humans.poll()
        result = self.app.humans.snapshot()
        self.assertTrue(result["available"])
        self.assertFalse(result["stale"])
        self.assertEqual(result["items"][0]["resume_summary"], "Write the original report")
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertNotIn("not-for-renderer", json.dumps(result))
        self.hub.item["state"] = "submitting"
        self.app.humans.poll()
        result = self.app.humans.snapshot()["items"][0]
        self.assertEqual(result["state"], "decision_recorded")
        self.assertEqual(result["callback_state"], "submitting")

    def test_missing_context_is_unavailable_and_offline_preserves_facts(self):
        self.app.humans.poll()
        self.hub.offline = True
        self.app.humans.poll()
        result = self.app.humans.snapshot()
        self.assertFalse(result["available"])
        self.assertTrue(result["stale"])
        self.assertEqual(len(result["items"]), 1)
        self.hub.offline = False
        self.hub.item.pop("recommendation")
        self.app.humans.poll()
        self.assertFalse(self.app.humans.snapshot()["available"])
        other = control.Application({"resources": []}, self.root / "other", hub=self.hub)
        self.assertEqual(other.humans.snapshot()["reason"], "human_contract_not_connected")

    def test_native_stale_or_bad_version_cannot_decide(self):
        self.hub.item["fresh"] = False
        with self.assertRaises(control.HumanError):
            self.app.humans.decide("human-1", self.body)
        self.hub.item["fresh"] = True
        self.body["expected_version"] = 2
        with self.assertRaises(control.HumanError):
            self.app.humans.decide("human-1", self.body)
        self.assertEqual(self.hub.calls, 0)

    def test_slow_cloud_poll_does_not_block_cached_desktop_feed(self):
        self.app.humans.poll()
        entered = threading.Event()
        release = threading.Event()
        original = self.hub.human_snapshot

        def slow(*args):
            entered.set()
            release.wait(3)
            return original(*args)

        self.hub.human_snapshot = slow
        worker = threading.Thread(target=self.app.humans.poll)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            start = time.monotonic()
            snapshot = self.app.humans.snapshot()
            self.assertLess(time.monotonic() - start, 0.2)
            self.assertEqual(snapshot["items"][0]["id"], "human-1")
        finally:
            release.set()
            worker.join(4)

    def test_failed_native_sync_is_visible_and_does_not_notify_from_old_facts(self):
        self.app.product_commands.source_provider = lambda: [
            {"id": "fixture", "human_host_ref": "local.json"}
        ]
        self.app.product_commands.human_sync = lambda _: {"errors": [{"error": "unbound"}]}
        self.app.humans.poll()
        snapshot = self.app.humans.snapshot()
        self.assertTrue(snapshot["available"])
        self.assertTrue(snapshot["stale"])
        self.assertEqual(snapshot["reason"], "native_human_sync_incomplete")
        self.assertEqual(snapshot["adapter_status"][0]["state"], "degraded")
        self.assertEqual(len(snapshot["items"]), 1)

    def test_duplicate_concurrent_and_restart_only_query(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(lambda _: self.app.humans.decide("human-1", self.body), range(2))
            )
        self.assertTrue(all(x["state"] == "decision_recorded" for x in results))
        self.assertEqual(self.hub.calls, 1)
        restarted = control.Application(self.config, self.root, hub=self.hub)
        self.assertEqual(
            restarted.humans.decide("human-1", self.body)["state"], "decision_recorded"
        )
        self.assertEqual(self.hub.calls, 1)
        changed = copy.deepcopy(self.body)
        changed["decision"]["option_id"] = "json"
        with self.assertRaises(control.HumanError):
            restarted.humans.decide("human-1", changed)

    def test_lost_response_uses_original_center_decision_id(self):
        self.hub.drop_after = True
        result = self.app.humans.decide("human-1", self.body)
        self.assertEqual(result["state"], "decision_recorded")
        self.assertEqual(result["callback_state"], "decision_recorded")
        self.assertEqual(self.hub.calls, 1)
        self.hub.item.update(state="resuming", version=3)
        self.assertEqual(
            self.app.humans.decision_status("decision-1")["callback_state"], "resuming"
        )

    def test_missing_commit_unknown_is_never_reposted(self):
        self.hub.drop_before = True
        self.assertEqual(self.app.humans.decide("human-1", self.body)["state"], "unknown")
        self.hub.drop_before = False
        restarted = control.Application(self.config, self.root, hub=self.hub)
        self.assertEqual(restarted.humans.decide("human-1", self.body)["state"], "unknown")
        self.assertEqual(self.hub.calls, 1)
        self.hub.item.update(
            decision={"option_id": "plain"},
            decision_request_id="another-user-request",
            state="decision_recorded",
            version=2,
        )
        self.assertEqual(restarted.humans.decision_status("decision-1")["state"], "unknown")

    def test_reads_do_not_emit_fake_change_events(self):
        self.app.humans.decide("human-1", self.body)
        with self.app.store.db() as db:
            before = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        for _ in range(3):
            self.app.humans.decision_status("decision-1")
        with self.app.store.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events").fetchone()[0], before)

    def test_http_csrf_fixed_fields_and_query(self):
        server = control.Server(0, self.app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join()))
        base = "http://127.0.0.1:" + str(server.server_port)

        def post(value, headers):
            request = urllib.request.Request(
                base + "/api/human-requests/human-1/decision",
                json.dumps(value).encode(),
                headers={"Content-Type": "application/json", **headers},
            )
            with urllib.request.urlopen(request) as r:
                return json.load(r)

        with self.assertRaises(urllib.error.HTTPError) as err:
            post(self.body, {})
        self.assertEqual(err.exception.code, 403)
        headers = {"Origin": base, "X-Control-CSRF": self.app.csrf}
        with self.assertRaises(urllib.error.HTTPError):
            post({**self.body, "callback_url": "http://example.invalid"}, headers)
        self.assertEqual(self.hub.calls, 0)
        self.assertEqual(post(self.body, headers)["state"], "decision_recorded")
        with urllib.request.urlopen(base + "/api/human-decisions/decision-1") as r:
            self.assertEqual(json.load(r)["human_id"], "human-1")


class MultiQuestionTests(unittest.TestCase):
    setUp = HumanServiceTests.setUp

    def multi(self):
        self.hub.item["input_schema"] = {
            "questions": [
                {
                    "id": "portfolio_order",
                    "label": "交付次序",
                    "options": [
                        {"id": "delivery_first", "label": "先交付"},
                        {"id": "visual_first", "label": "先视觉"},
                    ],
                    "allow_text": True,
                    "multiple": False,
                    "required": True,
                },
                {
                    "id": "starry_direction",
                    "label": "星空方向",
                    "options": [
                        {"id": "deep_space_expedition", "label": "远征"},
                        {"id": "scale_and_time", "label": "尺度"},
                    ],
                    "allow_text": True,
                    "multiple": False,
                    "required": True,
                },
            ]
        }
        self.body["decision"] = {
            "answers": [
                {"question_id": "portfolio_order", "selected_ids": ["delivery_first"]},
                {
                    "question_id": "starry_direction",
                    "selected_ids": ["deep_space_expedition"],
                    "text": "  保留原文\n及用户空格。  ",
                },
            ]
        }

    def rejected(self, decision, code):
        with self.assertRaises(control.HumanError) as error:
            self.app.humans.decide("human-1", {**self.body, "decision": decision})
        self.assertEqual(error.exception.code, code)
        self.assertEqual(self.hub.calls, 0)
        with self.app.store.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM human_decisions").fetchone()[0], 0)

    def test_required_answers_are_atomic_and_text_is_verbatim(self):
        self.multi()
        original = copy.deepcopy(self.body)
        self.rejected({"answers": self.body["decision"]["answers"][:1]}, "human_answers_incomplete")
        self.app.humans.poll()
        self.assertEqual(
            self.app.humans.snapshot()["items"][0]["input_schema"], self.hub.item["input_schema"]
        )
        result = self.app.humans.decide("human-1", self.body)
        self.assertEqual(result["state"], "decision_recorded")
        self.assertEqual(result["item"]["decision"], original["decision"])
        self.assertEqual(self.body, original)
        self.assertEqual(self.hub.calls, 1)

    def test_invalid_choices_never_create_submission_intent(self):
        self.multi()
        cases = [
            ([{"question_id": "unknown", "text": "answer"}], "invalid_human_answer"),
            (
                [{"question_id": "portfolio_order", "selected_ids": ["unknown"]}],
                "invalid_human_choice",
            ),
            (
                [
                    {
                        "question_id": "portfolio_order",
                        "selected_ids": ["delivery_first", "visual_first"],
                    }
                ],
                "invalid_human_choice",
            ),
            (
                [
                    {
                        "question_id": "portfolio_order",
                        "selected_ids": ["delivery_first", "delivery_first"],
                    }
                ],
                "invalid_human_choice",
            ),
            (
                [
                    {"question_id": "portfolio_order", "text": "first"},
                    {"question_id": "portfolio_order", "text": "second"},
                ],
                "invalid_human_answer",
            ),
            ([{"question_id": "portfolio_order", "selected_ids": []}], "human_answer_required"),
            (
                [{"question_id": "portfolio_order", "selected_ids": "delivery_first"}],
                "invalid_human_choice",
            ),
            (
                [{"question_id": "portfolio_order", "selected_ids": [{}]}],
                "invalid_human_identifier",
            ),
            (
                [
                    {
                        "question_id": "portfolio_order",
                        "text": "answer",
                        "callback_url": "https://invalid",
                    }
                ],
                "invalid_human_answer",
            ),
            ([None], "invalid_human_answer"),
            ([{"question_id": "portfolio_order", "text": " \n "}], "invalid_human_text"),
            ([{"question_id": "portfolio_order", "text": "x" * 2001}], "invalid_human_text"),
            ([{"question_id": "portfolio_order", "text": "\ud800"}], "invalid_human_text"),
            ([{"question_id": "portfolio_order", "text": "a\x00b"}], "invalid_human_text"),
        ]
        for answers, code in cases:
            with self.subTest(answers=repr(answers)[:120]):
                self.rejected({"answers": answers}, code)
        self.rejected({"option_id": "plain"}, "invalid_human_answer")
        self.rejected({"answers": [], "text": "extra"}, "invalid_human_answer")
        self.rejected({"answers": {}}, "invalid_human_answer")
        self.rejected({"answers": [{}] * 13}, "invalid_human_answer")

    def test_multiple_optional_text_only_and_unicode_boundary(self):
        self.multi()
        questions = self.hub.item["input_schema"]["questions"]
        questions[0]["multiple"] = True
        questions[1]["required"] = False
        questions.append(
            {
                "id": "notes",
                "label": "备注",
                "options": [],
                "allow_text": True,
                "multiple": False,
                "required": True,
            }
        )
        self.body["decision"] = {
            "answers": [
                {
                    "question_id": "portfolio_order",
                    "selected_ids": ["delivery_first", "visual_first"],
                },
                {"question_id": "notes", "text": "🌌" * 2000},
            ]
        }
        result = self.app.humans.decide("human-1", self.body)
        self.assertEqual(result["item"]["decision"], self.body["decision"])
        self.assertEqual(self.hub.calls, 1)

    def test_text_permission_and_legacy_choice_checked_before_post(self):
        self.rejected({"answers": []}, "invalid_human_answer")
        self.rejected({"option_id": "not-an-option"}, "invalid_human_choice")
        self.rejected({"text": "not allowed"}, "human_free_text_not_allowed")
        self.multi()
        self.hub.item["input_schema"]["questions"][1]["allow_text"] = False
        self.rejected(self.body["decision"], "human_free_text_not_allowed")

    def test_multi_decision_concurrency_restart_and_changed_body(self):
        self.multi()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(lambda _: self.app.humans.decide("human-1", self.body), range(2))
            )
        self.assertTrue(all(x["state"] == "decision_recorded" for x in results))
        restarted = control.Application(self.config, self.root, hub=self.hub)
        # A later schema/state change cannot turn an exact replay into a new POST.
        self.hub.item.pop("input_schema")
        self.assertEqual(
            restarted.humans.decide("human-1", self.body)["state"], "decision_recorded"
        )
        changed = copy.deepcopy(self.body)
        changed["decision"]["answers"][0]["selected_ids"] = ["visual_first"]
        with self.assertRaises(control.HumanError) as error:
            restarted.humans.decide("human-1", changed)
        self.assertEqual(error.exception.code, "human_decision_id_conflict")
        self.assertEqual(self.hub.calls, 1)

    def test_multi_response_loss_queries_exact_decision(self):
        self.multi()
        HumanServiceTests.test_lost_response_uses_original_center_decision_id(self)

    def test_multi_missing_commit_does_not_replay_after_restart(self):
        self.multi()
        self.hub.drop_before = True
        self.assertEqual(self.app.humans.decide("human-1", self.body)["state"], "unknown")
        self.hub.drop_before = False
        restarted = control.Application(self.config, self.root, hub=self.hub)
        self.assertEqual(restarted.humans.decide("human-1", self.body)["state"], "unknown")
        self.hub.item.update(
            decision=copy.deepcopy(self.body["decision"]),
            decision_request_id="other-id",
            state="decision_recorded",
            version=2,
        )
        self.assertEqual(restarted.humans.decision_status("decision-1")["state"], "unknown")
        self.assertEqual(self.hub.calls, 1)

    def test_multi_stale_cancelled_and_changed_version_are_not_submitted(self):
        self.multi()
        for update in (
            {"fresh": False},
            {"version": 2},
            {"state": "cancelled", "can_decide": False},
        ):
            with self.subTest(update=update):
                old = copy.deepcopy(self.hub.item)
                self.hub.item.update(update)
                self.rejected(self.body["decision"], "human_request_not_decidable")
                self.hub.item = old

    def test_center_body_byte_limit_checked_before_intent(self):
        self.multi()
        question = self.hub.item["input_schema"]["questions"][0]
        self.hub.item["input_schema"]["questions"] = [
            {**question, "id": "q" + str(i)} for i in range(12)
        ]
        self.rejected(
            {"answers": [{"question_id": "q" + str(i), "text": "星" * 2000} for i in range(12)]},
            "human_decision_body_limit",
        )

    def test_multi_http_and_original_receipt_endpoint(self):
        self.multi()
        HumanServiceTests.test_http_csrf_fixed_fields_and_query(self)


if __name__ == "__main__":
    unittest.main()
