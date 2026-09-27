"""Idle scheduling preserves mutation visibility, failure recovery and delivery safety."""

import copy
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from test_control import control


class IdlePerformanceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.app = control.Application(
            {"coordination_mode": "local", "resources": [], "os_runtime_registration": False},
            Path(directory.name),
        )

    def test_clock_only_status_does_not_rewrite_persisted_cache(self):
        initial = self.app.hub.status()
        changed = copy.deepcopy(initial)
        changed["projects"].append({"id": "new-project", "name": "New"})
        with (
            patch.object(
                self.app.hub,
                "status",
                side_effect=[{**initial, "server_time": 1}, {**initial, "server_time": 2}, changed],
            ),
            patch.object(self.app.store, "cache", wraps=self.app.store.cache) as cache,
        ):
            self.app.refresh_hub()
            self.app.refresh_hub()
            self.assertEqual(cache.call_count, 1)
            self.app.refresh_hub()
            self.assertEqual(cache.call_count, 2)

    def test_local_snapshot_refreshes_on_demand_without_remote_polling(self):
        with patch.object(self.app.hub, "status", wraps=self.app.hub.status) as status:
            self.app.snapshot()
            self.app.snapshot()
            self.assertEqual(status.call_count, 1)
            self.app.hub_refreshed_at = 0
            self.app.snapshot()
            self.assertEqual(status.call_count, 2)
        self.app.hub.is_local = False
        with patch.object(self.app.hub, "status", side_effect=AssertionError("no GET remote poll")):
            self.app.snapshot()

    def test_idle_backoff_retains_fast_retry_for_external_runtime_and_deliveries(self):
        self.app.tick()
        self.assertEqual(self.app.sync_interval(), 30)
        with patch.object(self.app.store, "deliveries", return_value=[{"state": "unknown"}]):
            self.assertEqual(self.app.sync_interval(), 3)
        self.app.observer.bindings = [{"actor_id": "fixture"}]
        self.assertEqual(self.app.sync_interval(), 3)
        self.app.observer.bindings = []
        self.app.connection["state"] = "offline"
        self.assertEqual(self.app.sync_interval(), 3)

    def test_write_event_wakes_idle_loop_without_waiting_for_fallback(self):
        observed = threading.Event()
        calls = []

        def tick():
            calls.append(1)
            observed.set()

        worker = threading.Thread(target=self.app.run, daemon=True)
        with (
            patch.object(self.app, "tick", side_effect=tick),
            patch.object(self.app, "sync_interval", return_value=30),
        ):
            worker.start()
            try:
                self.assertTrue(observed.wait(1))
                observed.clear()
                self.app.wake_workers()
                self.assertTrue(observed.wait(1))
                self.assertEqual(len(calls), 2)
            finally:
                self.app.stop.set()
                self.app.wake_workers()
                worker.join(1)
        self.assertFalse(worker.is_alive())

    def test_offline_refresh_keeps_prior_observation_and_fast_retry(self):
        self.app.refresh_hub()
        before = copy.deepcopy(self.app.hub_snapshot)
        with patch.object(self.app.hub, "status", side_effect=OSError("offline")):
            self.app.refresh_hub()
        self.assertEqual(self.app.hub_snapshot, before)
        self.assertEqual(self.app.connection["state"], "offline")
        self.assertEqual(self.app.sync_interval(), 3)
