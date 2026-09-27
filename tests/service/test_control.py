import concurrent.futures
import importlib.util
import json
import hashlib
import subprocess
from types import SimpleNamespace
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("control_service", ROOT / "service/main.py")
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)


class Hub:
    def __init__(self):
        self.calls = []
        self.offline = False

    def status(self):
        if self.offline:
            raise OSError("offline")
        return {
            "projects": [{"id": "independent", "name": "Independent project"}],
            "agents": [{"id": "worker", "online": False}],
            "tasks": [],
            "messages": [],
        }

    def registry(self):
        if self.offline:
            raise OSError("offline")
        return {
            "version": 1,
            "projects": [],
            "governance": [],
            "hosts": [],
            "adapters": [],
            "seats": [],
            "operations": [],
            "capabilities": {"manage_workstations": True},
        }

    def host_operations(self, host_id):
        if self.offline:
            raise OSError("offline")
        return {"host_id": host_id, "operations": []}

    def management(self, action, payload):
        if self.offline:
            raise OSError("offline")
        self.management_call = (action, payload)
        return {"operation": {"state": "pending"}, "action": action}

    def message(self, delivery):
        self.calls.append(delivery["id"])
        if self.offline:
            raise OSError("offline")
        return {"id": "hub:" + delivery["id"]}


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.rollout = self.root / "runtime.jsonl"
        self.rollout.write_text("", encoding="utf-8")
        self.hub = Hub()
        self.queue = []
        self.config = {
            "runtime_bindings": [
                {"actor_id": "worker", "thread_id": "fixture-thread", "rollout": str(self.rollout)}
            ],
            "runtime_sessions_dir": str(self.root),
            "resources": [],
        }
        self.app = control.Application(
            self.config, self.root / "data", hub=self.hub, queue_runner=self.enqueue
        )

    def enqueue(self, binding, delivery):
        self.queue.append((binding["thread_id"], delivery["id"]))
        return "queue:" + delivery["id"]

    def submit(self, target="worker", body="complete fixture", request_id="request-1"):
        return self.app.submit({"request_id": request_id, "target": target, "body": body})

    def append(self, *events):
        with self.rollout.open("a", encoding="utf-8") as f:
            for event in events:
                event.setdefault("timestamp", control.now())
                f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def user_event(self, body):
        return {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": body}],
            },
        }

    def turn_event(self, kind, turn="turn-1"):
        return {"type": "event_msg", "payload": {"type": kind, "turn_id": turn}}

    def final_answer(self, text):
        return {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "final_answer",
                "content": [{"type": "output_text", "text": text}],
            },
        }

    def test_duplicate_message_does_not_dispatch_twice(self):
        self.submit()
        self.app.dispatch_one()
        self.submit()
        self.app.dispatch_one()
        self.assertEqual(self.queue, [("fixture-thread", "request-1")])
        self.assertEqual(self.hub.calls, ["request-1"])
        self.assertEqual(self.app.store.deliveries()[0]["state"], "queued")

    def test_same_id_changed_content_rejected(self):
        self.submit()
        with self.assertRaises(control.Problem):
            self.submit(body="different")

    def test_unknown_target_rejected(self):
        with self.assertRaises(control.Problem):
            self.submit(target="stranger")

    def test_pending_message_does_not_follow_rebound_actor_after_restart(self):
        self.submit()
        changed = dict(
            self.config,
            runtime_bindings=[
                dict(self.config["runtime_bindings"][0], thread_id="replacement-thread")
            ],
        )
        reopened = control.Application(
            changed, self.root / "data", hub=self.hub, queue_runner=self.enqueue
        )
        reopened.dispatch_one()
        delivery = reopened.store.deliveries()[0]
        self.assertFalse(self.queue)
        self.assertFalse(self.hub.calls)
        self.assertEqual(delivery["target_thread_id"], "fixture-thread")
        self.assertEqual(delivery["state"], "unknown")

    def test_same_delivery_id_cannot_change_bound_thread(self):
        self.submit()
        self.app.observer.bindings["worker"]["thread_id"] = "replacement-thread"
        with self.assertRaises(control.Problem) as rejected:
            self.submit()
        self.assertEqual(rejected.exception.code, "id_conflict")

    def test_binding_change_during_hub_ack_never_queues_another_thread(self):
        self.submit()

        def change_binding(delivery):
            self.app.observer.bindings["worker"]["thread_id"] = "replacement-thread"
            return {"id": "hub:" + delivery["id"]}

        self.hub.message = change_binding
        self.app.dispatch_one()
        self.assertFalse(self.queue)
        self.assertEqual(self.app.store.deliveries()[0]["state"], "unknown")

    def test_rebound_runtime_cannot_complete_old_delivery(self):
        delivery = self.submit()
        self.app.dispatch_one()
        changed = dict(
            self.config,
            runtime_bindings=[
                dict(self.config["runtime_bindings"][0], thread_id="replacement-thread")
            ],
        )
        reopened = control.Application(
            changed, self.root / "data", hub=self.hub, queue_runner=self.enqueue
        )
        self.append(
            self.turn_event("task_started"),
            self.user_event(control.delivery_message(delivery)),
            self.final_answer("WRONG_RUNTIME_RESULT"),
            self.turn_event("task_complete"),
        )
        reopened.tick()
        observed = reopened.store.deliveries()[0]
        self.assertEqual(observed["state"], "queued")
        self.assertIsNone(observed["reply"])

    def test_legacy_pending_without_fixed_thread_is_never_adopted(self):
        self.submit()
        with self.app.store.db() as db:
            db.execute("UPDATE deliveries SET target_thread_id=NULL")
        reopened = control.Application(
            self.config, self.root / "data", hub=self.hub, queue_runner=self.enqueue
        )
        reopened.dispatch_one()
        self.assertFalse(self.queue)
        self.assertFalse(self.hub.calls)
        self.assertEqual(reopened.store.deliveries()[0]["state"], "unknown")

    def test_offline_message_is_durable_and_recovers(self):
        self.hub.offline = True
        self.submit()
        self.app.dispatch_one()
        self.assertEqual(self.app.store.deliveries()[0]["state"], "pending")
        reopened = control.Application(
            self.config, self.root / "data", hub=Hub(), queue_runner=self.enqueue
        )
        reopened.dispatch_one()
        self.assertEqual(len(self.queue), 1)

    def test_invalid_hub_receipt_keeps_pending_and_never_dispatches_runtime(self):
        self.hub.message = lambda delivery: {}
        self.submit()
        self.app.dispatch_one()
        self.assertEqual(self.app.store.deliveries()[0]["state"], "pending")
        self.assertFalse(self.queue)

    def test_ambiguous_queue_crash_never_auto_replays(self):
        self.submit()
        self.app.store.transition("request-1", "queue_submitting")
        reopened = control.Application(
            self.config, self.root / "data", hub=Hub(), queue_runner=self.enqueue
        )
        reopened.dispatch_one()
        self.assertEqual(reopened.store.deliveries()[0]["state"], "unknown")
        self.assertFalse(self.queue)

    def test_queue_timeout_is_unknown_not_retry(self):
        self.app.queue_runner = lambda *args: (_ for _ in ()).throw(TimeoutError())
        self.submit()
        self.app.dispatch_one()
        self.app.dispatch_one()
        self.assertEqual(self.app.store.deliveries()[0]["state"], "unknown")
        self.assertEqual(len(self.hub.calls), 1)

    def test_concurrent_claim_has_one_winner(self):
        self.submit()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: self.app.store.claim_pending(), range(4)))
        self.assertEqual(sum(x is not None for x in results), 1)

    def test_broadcast_stored_without_runtime_call(self):
        self.submit(target="all")
        self.app.dispatch_one()
        self.assertEqual(self.app.store.deliveries()[0]["state"], "stored")
        self.assertFalse(self.queue)

    def test_received_and_completed_require_real_matching_input(self):
        delivery = self.submit()
        self.app.dispatch_one()
        self.append(
            self.turn_event("task_started"), self.user_event(control.delivery_message(delivery))
        )
        self.app.tick()
        self.assertEqual(self.app.store.deliveries()[0]["state"], "received")
        self.append(self.turn_event("task_complete"))
        self.app.tick()
        self.assertEqual(self.app.store.deliveries()[0]["state"], "completed")

    def test_forged_content_does_not_ack_matching_id(self):
        delivery = self.submit()
        self.app.dispatch_one()
        self.append(self.user_event(control.delivery_message({**delivery, "body": "different"})))
        self.app.tick()
        self.assertEqual(self.app.store.deliveries()[0]["state"], "queued")

    def test_only_matching_turn_final_answer_returns_to_client(self):
        def answer(body, phase="final_answer"):
            return {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "phase": phase,
                    "content": [{"type": "output_text", "text": body}],
                },
            }

        delivery = self.submit()
        self.app.dispatch_one()
        self.append(
            answer("PRIVATE_PREVIOUS_REPLY"),
            self.turn_event("task_started"),
            self.user_event(control.delivery_message(delivery)),
            answer("PRIVATE_COMMENTARY", "commentary"),
            {
                "type": "response_item",
                "payload": {"type": "reasoning", "summary": ["PRIVATE_REASONING"]},
            },
            answer("Public task result"),
            self.turn_event("task_complete"),
            answer("PRIVATE_NEXT_REPLY"),
        )
        self.app.tick()
        saved = self.app.store.deliveries()[0]
        self.assertEqual(saved["state"], "completed")
        self.assertEqual(saved["reply"], "Public task result")
        self.assertNotIn("PRIVATE", json.dumps(saved))
        reopened = control.Application(
            self.config, self.root / "data", hub=Hub(), queue_runner=self.enqueue
        )
        self.assertEqual(reopened.store.deliveries()[0]["reply"], "Public task result")

    def test_reply_size_is_bounded_and_forged_delivery_not_exported(self):
        delivery = self.submit()
        self.app.dispatch_one()
        self.append(
            self.turn_event("task_started"),
            self.user_event(control.delivery_message({**delivery, "body": "forged"})),
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "phase": "final_answer",
                    "content": [{"type": "output_text", "text": "PRIVATE"}],
                },
            },
            self.turn_event("task_complete"),
        )
        self.app.tick()
        self.assertIsNone(self.app.store.deliveries()[0]["reply"])
        self.append(
            self.turn_event("task_started", "turn-2"),
            self.user_event(control.delivery_message(delivery)),
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "phase": "final_answer",
                    "content": [{"type": "output_text", "text": "R" * 20000}],
                },
            },
            self.turn_event("task_complete", "turn-2"),
        )
        self.app.tick()
        saved = self.app.store.deliveries()[0]
        self.assertEqual(len(saved["reply"]), 16000)
        self.assertEqual(saved["reply_truncated"], 1)

    def test_unfinished_turn_cannot_inherit_later_turn_answer(self):
        delivery = self.submit()
        self.app.dispatch_one()
        self.append(
            self.turn_event("task_started", "turn-a"),
            self.user_event(control.delivery_message(delivery)),
            self.turn_event("task_started", "turn-b"),
            self.user_event("Unrelated input B"),
            self.final_answer("PRIVATE_B_RESULT"),
            self.turn_event("task_complete", "turn-b"),
        )
        self.app.tick()
        row = self.app.store.deliveries()[0]
        self.assertEqual(row["state"], "unknown")
        self.assertEqual(row["turn_id"], "turn-a")
        self.assertIsNone(row["reply"])
        self.assertNotIn("PRIVATE_B_RESULT", json.dumps(self.app.snapshot()))

    def test_wrong_or_missing_completion_turn_cannot_finish_delivery(self):
        delivery = self.submit()
        self.app.dispatch_one()
        self.append(
            self.turn_event("task_started", "turn-a"),
            self.user_event(control.delivery_message(delivery)),
            self.final_answer("A_RESULT"),
            self.turn_event("task_complete", "turn-b"),
            self.turn_event("task_complete", None),
        )
        self.app.tick()
        self.assertEqual(self.app.store.deliveries()[0]["state"], "received")
        self.assertIsNone(self.app.store.deliveries()[0]["reply"])
        self.append(self.turn_event("task_complete", "turn-a"))
        self.app.tick()
        self.assertEqual(self.app.store.deliveries()[0]["reply"], "A_RESULT")

    def test_message_without_started_turn_never_associates_to_future_answer(self):
        delivery = self.submit()
        self.app.dispatch_one()
        self.append(
            self.user_event(control.delivery_message(delivery)),
            self.turn_event("task_started", "turn-b"),
            self.final_answer("PRIVATE_B_RESULT"),
            self.turn_event("task_complete", "turn-b"),
        )
        self.app.tick()
        self.assertEqual(self.app.store.deliveries()[0]["state"], "received")
        self.assertIsNone(self.app.store.deliveries()[0]["reply"])

    def test_duplicate_start_of_same_turn_preserves_correct_association(self):
        delivery = self.submit()
        self.app.dispatch_one()
        self.append(
            self.turn_event("task_started"),
            self.user_event(control.delivery_message(delivery)),
            self.turn_event("task_started"),
            self.final_answer("A_RESULT"),
            self.turn_event("task_complete"),
        )
        self.app.tick()
        self.assertEqual(self.app.store.deliveries()[0]["state"], "completed")
        self.assertEqual(self.app.store.deliveries()[0]["reply"], "A_RESULT")

    def test_corrupt_log_gap_invalidates_open_turn_association(self):
        delivery = self.submit()
        self.app.dispatch_one()
        self.append(
            self.turn_event("task_started"), self.user_event(control.delivery_message(delivery))
        )
        with self.rollout.open("a", encoding="utf-8") as f:
            f.write("CORRUPTED LIFECYCLE RECORD\n")
        self.append(self.final_answer("UNCONFIRMED_RESULT"), self.turn_event("task_complete"))
        self.app.tick()
        self.assertEqual(self.app.store.deliveries()[0]["state"], "unknown")
        self.assertIsNone(self.app.store.deliveries()[0]["reply"])

    def test_quoted_marker_cannot_forge_receipt(self):
        delivery = self.submit()
        self.app.dispatch_one()
        self.append(self.user_event("quote: " + control.delivery_message(delivery)))
        self.app.tick()
        self.assertEqual(self.app.store.deliveries()[0]["state"], "queued")

    def test_partial_json_line_waits_until_complete(self):
        event = {
            "timestamp": control.now(),
            "type": "event_msg",
            "payload": {"type": "task_started"},
        }
        encoded = json.dumps(event)
        self.rollout.write_text(encoded[:20], encoding="utf-8")
        self.app.observer.observe()
        self.assertEqual(self.app.observer.snapshot("worker")["state"], "unknown")
        with self.rollout.open("a", encoding="utf-8") as f:
            f.write(encoded[20:] + "\n")
        self.app.observer.observe()
        self.assertEqual(self.app.observer.snapshot("worker")["state"], "running")

    def test_stale_running_log_is_not_live_execution(self):
        self.append(
            {
                "timestamp": "2000-01-01T00:00:00Z",
                "type": "event_msg",
                "payload": {"type": "task_started"},
            }
        )
        self.app.observer.observe()
        state = self.app.observer.snapshot("worker")
        self.assertEqual(state["state"], "unknown")
        self.assertEqual(state["last_known_state"], "running")

    def test_offline_snapshot_keeps_data_and_marks_offline(self):
        self.app.tick()
        self.hub.offline = True
        self.app.tick()
        data = self.app.snapshot()
        self.assertEqual(data["connection"]["state"], "offline")
        self.assertEqual(data["projects"][0]["id"], "independent")
        self.assertIsNone(data["agents"][0]["online"])
        self.assertTrue(data["agents"][0]["runtime"]["bound"])

    def test_local_project_and_assignment_keep_registration_and_source_state(self):
        self.app.config["local_projects"] = [
            {"id": "control", "name": "Control", "registration": "local"}
        ]
        self.app.config["governance"] = {"assignments": [{"task_id": "task", "status": "assigned"}]}
        self.app.hub_snapshot = {
            "projects": [{"id": "independent"}],
            "tasks": [{"id": "task", "status": "delivered", "owner": "worker"}],
        }
        data = self.app.snapshot()
        self.assertEqual(data["projects"][1]["registration"], "local")
        self.assertEqual(data["governance"]["assignments"][0]["status"], "delivered")
        self.assertEqual(self.app.config["governance"]["assignments"][0]["status"], "assigned")

    def test_resource_is_metadata_only_and_persistent(self):
        private = self.root / "private.txt"
        private.write_text("THIS_VALUE_MUST_NOT_APPEAR", encoding="utf-8")
        row = self.app.register_resource(
            {
                "id": "doc",
                "name": "reference",
                "kind": "document",
                "location": str(private),
                "project": "independent",
            }
        )
        self.assertEqual(row["status"], "available")
        self.assertNotIn("THIS_VALUE_MUST_NOT_APPEAR", json.dumps(self.app.snapshot()))
        reopened = control.Application(
            self.config, self.root / "data", hub=Hub(), queue_runner=self.enqueue
        )
        self.assertEqual(len(reopened.resource_rows), 1)

    def test_brief_is_explicit_and_detects_changed_source(self):
        source = self.root / "code.py"
        source.write_text("original", encoding="utf-8")
        brief = self.root / "brief.json"
        ref = {
            "path": "code.py",
            "line": 1,
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        }
        brief.write_text(
            json.dumps(
                {
                    "features": [
                        {"id": "feature", "summary": "Fixture", "source": ref, "test": ref}
                    ],
                    "verification_receipt": ref,
                    "PRIVATE": "DO_NOT_EXPORT",
                }
            ),
            encoding="utf-8",
        )
        self.app.config["briefs"] = {"brief": {"path": str(brief), "root": str(self.root)}}
        result = self.app.brief("brief")
        self.assertEqual(result["features"][0]["source"]["freshness"], "matches_record")
        self.assertNotIn("DO_NOT_EXPORT", json.dumps(result))
        source.write_text("changed", encoding="utf-8")
        self.assertEqual(
            self.app.brief("brief")["features"][0]["source"]["freshness"], "source_changed"
        )
        with self.assertRaises(control.Problem):
            self.app.brief("arbitrary-path")

    def test_brief_reference_cannot_read_outside_project(self):
        project = self.root / "project"
        project.mkdir()
        outside = self.root / "private"
        outside.write_text("PRIVATE", encoding="utf-8")
        brief = project / "brief.json"
        brief.write_text(
            json.dumps(
                {
                    "features": [
                        {
                            "source": {
                                "path": "../private",
                                "sha256": hashlib.sha256(outside.read_bytes()).hexdigest(),
                            }
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        self.app.config["briefs"] = {"brief": {"path": str(brief), "root": str(project)}}
        result = self.app.brief("brief")
        self.assertEqual(result["features"][0]["source"]["freshness"], "outside_project")

    def test_credential_url_rejected(self):
        for url in (
            "https://user:password@github.com/repo",
            "https://github.com/repo?token=secret",
            "javascript:alert(1)",
        ):
            with self.assertRaises(control.Problem):
                self.app.register_resource(
                    {"id": "gh", "name": "repo", "kind": "github", "location": url}
                )

    def test_ssh_catalog_never_copies_credentials_or_commands(self):
        source = self.root / "ssh-config"
        source.write_text(
            "Host production\n HostName 192.0.2.1\n Port 22\n IdentityFile C:/SECRET_KEY\n ProxyCommand SECRET_COMMAND\n User SECRET_USER\n",
            encoding="utf-8",
        )
        rows, errors = control.collect([{"kind": "ssh_endpoints", "path": str(source)}])
        self.assertFalse(errors)
        self.assertEqual(rows[0]["location"], "ssh://192.0.2.1:22")
        self.assertNotIn("SECRET", json.dumps(rows))

    def test_github_catalog_strips_auth_and_query(self):
        repo = self.root / "repo"
        (repo / ".git").mkdir(parents=True)
        (repo / ".git/config").write_text(
            '[remote "origin"]\n url = https://user:SECRET@github.com/example/repo.git?token=SECRET\n',
            encoding="utf-8",
        )
        rows, errors = control.collect([{"kind": "git_remote", "path": str(repo)}])
        self.assertFalse(errors)
        self.assertEqual(rows[0]["location"], "https://github.com/example/repo.git")
        self.assertNotIn("SECRET", json.dumps(rows))

    def test_catalog_merges_same_server_and_keeps_both_sources(self):
        inventory = self.root / "inventory.md"
        inventory.write_text(
            "|服务器|地址|当前服务|\n|---|---|---|\n|primary|192.0.2.1|coordination|\n",
            encoding="utf-8",
        )
        ssh = self.root / "ssh-config"
        ssh.write_text("Host production\n HostName 192.0.2.1\n Port 22\n", encoding="utf-8")
        rows, errors = control.collect(
            [
                {"kind": "server_inventory", "path": str(inventory)},
                {"kind": "ssh_endpoints", "path": str(ssh)},
            ]
        )
        self.assertFalse(errors)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["details"]["aliases"], "production")
        self.assertEqual(rows[0]["details"]["当前服务"], "coordination")
        self.assertIn(str(inventory), rows[0]["source_ref"])
        self.assertIn(str(ssh), rows[0]["source_ref"])

    def test_monitor_fixed_readonly_command_and_whitelist(self):
        from monitor import collect_monitors

        configuration = {
            "executable": "ssh",
            "ssh_config": "fixture-config",
            "servers": [{"alias": "fixture", "location": "ssh://192.0.2.1:22"}],
        }
        sample = dict(
            load_1m=1.2,
            cpu_count=2,
            memory_total_bytes=1000,
            memory_available_bytes=100,
            disk_total_bytes=2000,
            disk_available_bytes=200,
            uptime_seconds=3600,
            private="SECRET",
        )
        calls = []

        def runner(args, **kwargs):
            calls.append((args, kwargs))
            return SimpleNamespace(returncode=0, stdout=json.dumps(sample))

        result = collect_monitors(configuration, runner)["ssh://192.0.2.1:22"]
        self.assertEqual(result["state"], "sampled")
        self.assertEqual(result["load_1m"], 1.2)
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertEqual(calls[0][0][-2], "fixture")
        self.assertIn("BatchMode=yes", calls[0][0])
        self.assertLessEqual(calls[0][1]["timeout"], 12)
        self.assertNotIn("shell", calls[0][1])

    def test_monitor_invalid_missing_and_timeout_are_unavailable(self):
        from monitor import collect_monitors

        configuration = {
            "executable": "ssh",
            "ssh_config": "fixture-config",
            "servers": [{"alias": "fixture", "location": "ssh://192.0.2.1:22"}],
        }
        sample = dict(
            load_1m=1,
            cpu_count=2,
            memory_total_bytes=1000,
            memory_available_bytes=100,
            disk_total_bytes=2000,
            disk_available_bytes=200,
            uptime_seconds=3600,
        )
        invalid = [
            [],
            {},
            {**sample, "load_1m": float("nan")},
            {**sample, "load_1m": float("inf")},
            {**sample, "cpu_count": True},
            {**sample, "memory_available_bytes": 1001},
        ]
        for payload in invalid:
            with self.subTest(payload=payload):
                result = collect_monitors(
                    configuration,
                    lambda *a, **kw: SimpleNamespace(returncode=0, stdout=json.dumps(payload)),
                )
                self.assertEqual(result["ssh://192.0.2.1:22"]["state"], "unavailable")
                self.assertNotIn("load_1m", result["ssh://192.0.2.1:22"])

        def timeout(*a, **kw):
            raise subprocess.TimeoutExpired("SSH_SECRET", 12)

        result = collect_monitors(configuration, timeout)
        self.assertEqual(result["ssh://192.0.2.1:22"]["state"], "unavailable")
        self.assertNotIn("SECRET", json.dumps(result))

    def test_expired_server_sample_never_looks_live(self):
        self.app.resource_rows = [{"id": "server", "location": "ssh://192.0.2.1:22"}]
        self.app.metrics = {
            "ssh://192.0.2.1:22": {
                "state": "sampled",
                "observed_at": "2000-01-01T00:00:00+00:00",
                "load_1m": 0.2,
            }
        }
        metric = self.app.snapshot()["resources"][0]["metrics"]
        self.assertEqual(metric["state"], "unavailable")
        self.assertTrue(metric["stale"])
        self.assertNotIn("load_1m", metric)

    def test_instance_lock_prevents_second_recovery(self):
        lock = control.InstanceLock(self.root / "lock")
        try:
            with self.assertRaises(control.Problem):
                control.InstanceLock(self.root / "lock")
        finally:
            lock.close()
        reopened = control.InstanceLock(self.root / "lock")
        reopened.close()

    def test_http_origin_csrf_and_path_traversal(self):
        web = self.root / "web"
        web.mkdir()
        (web / "index.html").write_text("test", encoding="utf-8")
        server = control.Server(0, self.app, web)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(server.origin + "/api/session") as response:
            token = json.load(response)["csrf"]
        with opener.open(server.origin + "/api/registry") as response:
            self.assertEqual(json.load(response)["hosts"], [])
        with opener.open(server.origin + "/api/host-operations?host_id=host-local-01") as response:
            self.assertEqual(json.load(response)["host_id"], "host-local-01")
        data = json.dumps({"request_id": "via-http", "target": "all", "body": "fixture"}).encode()
        for headers in (
            {"Content-Type": "application/json"},
            {
                "Content-Type": "application/json",
                "Origin": "https://evil.example",
                "X-Control-CSRF": token,
            },
        ):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                opener.open(
                    urllib.request.Request(server.origin + "/api/chat", data=data, headers=headers)
                )
            self.assertEqual(caught.exception.code, 403)
        with opener.open(
            urllib.request.Request(
                server.origin + "/api/chat",
                data=data,
                headers={
                    "Content-Type": "application/json",
                    "Origin": server.origin,
                    "X-Control-CSRF": token,
                },
            )
        ) as response:
            self.assertEqual(json.load(response)["delivery"]["state"], "pending")
        management = json.dumps(
            {
                "action": "seat-control",
                "payload": {
                    "request_id": "http-management-1",
                    "id": "seat-aieyra-os",
                    "operation": "open",
                    "version": 1,
                },
            }
        ).encode()
        with opener.open(
            urllib.request.Request(
                server.origin + "/api/management",
                data=management,
                headers={
                    "Content-Type": "application/json",
                    "Origin": server.origin,
                    "X-Control-CSRF": token,
                },
            )
        ) as response:
            self.assertEqual(json.load(response)["result"]["operation"]["state"], "pending")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            opener.open(server.origin + "/%2e%2e/private.txt")
        self.assertEqual(caught.exception.code, 404)

    def test_registry_gateway_is_read_only_until_management_action(self):
        registry = self.app.registry()
        self.assertEqual(registry["hosts"], [])
        operations = self.app.host_operations("host-local-01")
        self.assertEqual(operations["host_id"], "host-local-01")
        result = self.app.management(
            "seat-control",
            {
                "request_id": "management-1",
                "id": "seat-aieyra-os",
                "operation": "open",
                "version": 1,
            },
        )
        self.assertEqual(result["operation"]["state"], "pending")
        self.assertEqual(self.hub.management_call[0], "seat-control")

    def test_management_action_and_host_id_are_whitelisted(self):
        with self.assertRaises(control.Problem):
            self.app.management("shell", {"request_id": "management-2"})
        with self.assertRaises(control.Problem):
            self.app.host_operations("../etc")

    def test_center_conflict_status_is_not_hidden_as_service_failure(self):
        self.hub.management = lambda *args: (_ for _ in ()).throw(
            RuntimeError("409:adapter_operation_unavailable")
        )
        with self.assertRaises(control.Problem) as caught:
            self.app.management(
                "seat-control",
                {
                    "request_id": "management-conflict",
                    "id": "seat-aieyra-os",
                    "operation": "open",
                    "version": 1,
                },
            )
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.code, "adapter_operation_unavailable")


if __name__ == "__main__":
    unittest.main()
