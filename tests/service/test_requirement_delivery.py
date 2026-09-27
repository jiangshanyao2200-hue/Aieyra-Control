import copy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from test_control import control, Hub
from requirement_delivery import RequirementDeliveryError


class DeliveryHub(Hub):
    def __init__(self):
        super().__init__()
        self.item = dict(
            id="requirement-1",
            version=2,
            state="active",
            links=[dict(task_id="task-1", target_actors=["worker"])],
        )
        self.task = dict(id="task-1", version=3, status="doing", owner="worker", lease_until=0)
        self.facts, self.writes, self.queries = {}, [], []

    def intake_read(self, route, query):
        if self.offline:
            raise OSError("offline")
        return copy.deepcopy(self.item if route == "requirement" else self.task)

    def intake_record(self, payload):
        self.writes.append(copy.deepcopy(payload))
        result = dict(payload["receipt"], reporter_actor="reporter", seq=len(self.facts) + 1)
        self.facts[payload["request_id"]] = result
        return result

    def intake_receipt(self, identifier):
        self.queries.append(identifier)
        if identifier not in self.facts:
            raise RuntimeError("404:intake_receipt_missing")
        return dict(recorded=True, request_id=identifier, response=self.facts[identifier])


class RequirementDeliveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.rollout = self.root / "events.jsonl"
        self.rollout.write_text("", encoding="utf-8")
        self.config = dict(
            resources=[],
            runtime_bindings=[
                dict(actor_id="worker", thread_id="thread-original", rollout=str(self.rollout))
            ],
        )
        self.hub = DeliveryHub()
        self.queue = []
        self.app = self.reopen()
        self.intent = dict(
            request_id="dispatch-1",
            requirement_id="requirement-1",
            requirement_version=2,
            task_id="task-1",
            task_version=3,
            target_actor="worker",
            target_thread_id="thread-original",
            body="核对指定产物，不修改用户资产。\nPRIVATE_LOCAL_BODY",
        )

    def reopen(self):
        def queue(binding, delivery):
            self.queue.append((binding["thread_id"], delivery["id"]))
            return "queue-1"

        return control.Application(
            self.config, self.root / "data", hub=self.hub, queue_runner=queue
        )

    def submit(self):
        return self.app.requirement_deliveries.submit(self.intent)

    def row(self):
        return self.app.store.delivery(self.intent["request_id"])

    def append(self, *events):
        with self.rollout.open("a", encoding="utf-8", newline="\n") as out:
            for event in events:
                event.setdefault("timestamp", control.now())
                out.write(json.dumps(event, ensure_ascii=False) + "\n")

    @staticmethod
    def turn(kind="task_started", turn_id="turn-first"):
        return dict(type="event_msg", payload=dict(type=kind, turn_id=turn_id))

    def marker(self):
        return dict(
            type="response_item",
            payload=dict(
                type="message",
                role="user",
                content=[dict(type="input_text", text=control.delivery_message(self.row()))],
            ),
        )

    def retry_fact(self):
        with self.app.store.db() as db:
            db.execute("UPDATE intake_observations SET next_attempt=0")
        self.app.requirement_deliveries.sync()

    def test_exact_intent_complete_utf8_hash_and_duplicate_after_revision(self):
        original = self.submit()
        self.assertEqual(
            original["body_sha256"],
            hashlib.sha256(control.delivery_message(original).encode("utf-8")).hexdigest(),
        )
        self.assertNotEqual(
            original["body_sha256"], hashlib.sha256(self.intent["body"].encode("utf-8")).hexdigest()
        )
        self.app.dispatch_one()
        self.hub.item.update(version=3, state="superseded")
        self.hub.task.update(version=4, status="delivered")
        replay = self.submit()
        self.assertEqual(replay["queue_id"], "queue-1")
        self.app.dispatch_one()
        self.assertEqual(self.queue, [("thread-original", "dispatch-1")])
        for key, value in [
            ("body", "different"),
            ("task_version", 4),
            ("target_thread_id", "another"),
        ]:
            with self.subTest(key=key), self.assertRaises(RequirementDeliveryError):
                self.app.requirement_deliveries.submit(dict(self.intent, **{key: value}))

    def test_invalid_fields_and_body_do_not_create_delivery(self):
        for change in [
            dict(body=""),
            dict(body=[]),
            dict(task_version=True),
            dict(target_actor="all"),
            dict(target_thread_id="new-thread"),
            dict(extra="no"),
            dict(request_id="bad/id"),
        ]:
            with self.subTest(change=change), self.assertRaises(RequirementDeliveryError):
                self.app.requirement_deliveries.submit(dict(self.intent, **change))
        self.assertEqual(self.app.store.deliveries(), [])

    def test_revision_retirement_target_or_foreign_lease_stops_before_queue(self):
        cases = [
            ("revision", lambda: self.hub.item.update(version=3)),
            ("cancelled", lambda: self.hub.item.update(state="cancelled")),
            ("delivered", lambda: self.hub.task.update(status="delivered")),
            ("task-version", lambda: self.hub.task.update(version=4)),
            ("target", lambda: self.hub.item.update(links=[])),
            ("foreign-lease", lambda: self.hub.task.update(owner="other", lease_until=9999999999)),
        ]
        original_item, original_task = copy.deepcopy(self.hub.item), copy.deepcopy(self.hub.task)
        for index, (label, change) in enumerate(cases):
            with self.subTest(label=label):
                self.hub.item, self.hub.task = (
                    copy.deepcopy(original_item),
                    copy.deepcopy(original_task),
                )
                self.intent["request_id"] = "dispatch-" + str(index)
                self.submit()
                change()
                self.app.dispatch_one()
                self.assertEqual(self.row()["state"], "unknown")
        self.assertFalse(self.queue)
        self.assertFalse(self.hub.calls)

    def test_rechecks_after_center_message_and_never_rebinds(self):
        self.submit()
        self.hub.message = lambda delivery: (
            self.hub.task.update(version=4) or {"id": "center-message"}
        )
        self.app.dispatch_one()
        self.assertFalse(self.queue)
        self.assertEqual(self.row()["error"], "requirement_task_version_changed")

    def test_unavailable_recheck_keeps_pending_without_queue_and_recover_checks_current(self):
        self.submit()
        self.hub.offline = True
        self.app.dispatch_one()
        self.assertEqual(self.row()["state"], "pending")
        self.hub.offline = False
        self.hub.item["state"] = "superseded"
        self.app = self.reopen()
        self.app.dispatch_one()
        self.assertEqual(self.row()["state"], "unknown")
        self.assertFalse(self.queue)

    def test_lost_native_queue_result_and_restart_never_replays(self):
        self.submit()

        def lost(*args):
            self.queue.append("original-maybe-queued")
            raise TimeoutError("PRIVATE_TRANSPORT")

        self.app.queue_runner = lost
        self.app.dispatch_one()
        self.app = self.reopen()
        for _ in range(2):
            self.app.tick()
        self.assertEqual(self.queue, ["original-maybe-queued"])
        self.assertEqual(self.row()["state"], "unknown")
        self.assertEqual({fact["kind"] for fact in self.hub.facts.values()}, {"unknown"})
        self.assertNotIn("PRIVATE_TRANSPORT", json.dumps(self.hub.facts))

    def test_exact_native_marker_and_start_emit_separate_facts_no_owner_claim(self):
        self.submit()
        self.app.tick()
        self.assertEqual([x["kind"] for x in self.hub.facts.values()], ["dispatched"])
        self.append(self.turn(), self.marker(), self.turn("task_complete"))
        self.app.tick()
        self.assertEqual(self.row()["state"], "completed")
        facts = list(self.hub.facts.values())
        self.assertEqual(
            {x["kind"] for x in facts}, {"dispatched", "native_received", "native_started"}
        )
        for fact in facts:
            self.assertEqual(fact["task_version"], 3)
            self.assertEqual(fact["delivery_id"], "dispatch-1")
            self.assertEqual(fact["thread_ref"], "codex:thread-original")
            path = Path(fact["evidence_ref"])
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), fact["evidence_sha256"])
            self.assertNotIn("PRIVATE_LOCAL_BODY", path.read_text(encoding="utf-8"))
        self.assertEqual(self.hub.task["status"], "doing")
        self.assertNotIn("accepted_by", self.hub.task)

    def test_marker_before_unrelated_start_does_not_emit_started(self):
        self.submit()
        self.app.tick()
        self.append(self.marker(), self.turn(), self.turn("task_complete"))
        self.app.tick()
        self.assertEqual(self.row()["state"], "received")
        self.assertIsNone(self.row()["turn_id"])
        self.assertEqual(
            {x["kind"] for x in self.hub.facts.values()}, {"dispatched", "native_received"}
        )

    def test_duplicate_marker_cannot_replace_first_turn_after_restart(self):
        self.submit()
        self.app.tick()
        self.append(self.turn(), self.marker())
        self.app.tick()
        event_id = self.row()["native_event_id"]
        self.app = self.reopen()
        self.append(
            self.turn(turn_id="turn-later"), self.marker(), self.turn("task_complete", "turn-later")
        )
        self.app.tick()
        self.assertEqual(self.row()["turn_id"], "turn-first")
        self.assertEqual(self.row()["native_event_id"], event_id)
        self.assertEqual(self.row()["state"], "unknown")

    def test_outbox_lost_center_response_queries_original_not_native(self):
        self.submit()
        self.app.dispatch_one()
        original = self.hub.intake_record

        def lost(payload):
            original(payload)
            raise TimeoutError()

        self.hub.intake_record = lost
        self.app.requirement_deliveries.sync()
        self.assertEqual(len(self.hub.writes), 1)
        self.app = self.reopen()
        self.retry_fact()
        self.assertEqual(len(self.hub.queries), 1)
        self.assertEqual(len(self.hub.writes), 1)
        self.assertEqual(
            self.app.requirement_deliveries.status("dispatch-1")["receipts"][0]["state"], "recorded"
        )
        self.assertEqual(len(self.queue), 1)

    def test_unknown_receipt_only_explicit_missing_allows_same_center_body(self):
        self.submit()
        self.app.dispatch_one()

        def unavailable(payload):
            self.hub.writes.append(copy.deepcopy(payload))
            raise TimeoutError()

        self.hub.intake_record = unavailable
        self.app.requirement_deliveries.sync()
        first = self.hub.writes[0]
        self.hub.intake_receipt = lambda identifier: {"recorded": False}
        self.retry_fact()
        self.assertEqual(len(self.hub.writes), 1)

        def missing(identifier):
            raise RuntimeError("404:intake_receipt_missing")

        self.hub.intake_receipt = missing
        self.hub.intake_record = lambda payload: (
            self.hub.writes.append(copy.deepcopy(payload)) or payload["receipt"]
        )
        self.retry_fact()
        self.assertEqual(self.hub.writes, [first, first])
        self.assertEqual(len(self.queue), 1)

    def test_forbidden_reporter_is_explicit_rejected_not_owner_fallback(self):
        self.submit()
        self.app.dispatch_one()

        def denied(payload):
            raise RuntimeError("403:workstation_management_denied")

        self.hub.intake_record = denied
        self.app.requirement_deliveries.sync()
        status = self.app.requirement_deliveries.status("dispatch-1")
        self.assertEqual(status["receipts"][0]["state"], "rejected")
        self.assertEqual(status["receipts"][0]["error"], "workstation_management_denied")
        self.assertEqual(self.row()["state"], "queued")

    def test_crash_after_evidence_before_outbox_keeps_original_bytes(self):
        self.submit()
        self.app.dispatch_one()
        self.app.requirement_deliveries.collect()
        with self.app.store.db() as db:
            old = dict(db.execute("SELECT * FROM intake_observations").fetchone())
            db.execute("DELETE FROM intake_observations")
        payload = json.loads(old["payload"])
        path = Path(payload["receipt"]["evidence_ref"])
        before = path.read_bytes()
        self.app.store.transition("dispatch-1", "received", turn_id="turn-first")
        self.app.requirement_deliveries.collect()
        with self.app.store.db() as db:
            recovered = db.execute("SELECT payload FROM intake_observations").fetchone()[0]
        self.assertEqual(json.loads(recovered), payload)
        self.assertEqual(path.read_bytes(), before)

    def test_hub_reports_metadata_using_own_identity_never_private_body(self):
        calls = []

        class Client:
            def call(self, identity, path, payload=None):
                calls.append((identity, path, payload))
                return {}

        hub = control.Hub.__new__(control.Hub)
        hub.config = dict(hub_read_identity="own-reporter", hub_user_identity="user")
        hub.client = Client()
        delivery = self.submit()
        hub.message(delivery)
        hub.intake_record({"request_id": "own-fact"})
        hub.intake_receipt("own-fact")
        self.assertEqual({x[0] for x in calls}, {"own-reporter"})
        self.assertNotIn("PRIVATE_LOCAL_BODY", json.dumps(calls))

    def test_real_local_http_requires_csrf_explicit_submit_and_fixed_status_query(self):
        server = control.Server(0, self.app)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 3)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def post(headers):
            return urlopen(
                Request(
                    server.origin + "/api/requirement-dispatch",
                    data=json.dumps(self.intent).encode(),
                    headers={"Content-Type": "application/json", **headers},
                ),
                timeout=3,
            )

        with self.assertRaises(HTTPError) as denied:
            post({})
        self.assertEqual(denied.exception.code, 403)
        with post({"Origin": server.origin, "X-Control-CSRF": self.app.csrf}) as response:
            self.assertEqual(json.load(response)["delivery"]["state"], "pending")
        self.assertFalse(self.queue)
        self.app.tick()
        with urlopen(
            server.origin + "/api/requirement-dispatch?request_id=dispatch-1", timeout=3
        ) as response:
            status = json.load(response)
        self.assertEqual(status["delivery"]["state"], "queued")
        self.assertFalse(status["replay_allowed"])
        self.assertEqual(len(self.queue), 1)
        with self.assertRaises(HTTPError) as duplicate:
            urlopen(
                server.origin + "/api/requirement-dispatch?request_id=a&request_id=b", timeout=3
            )
        self.assertEqual(duplicate.exception.code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
