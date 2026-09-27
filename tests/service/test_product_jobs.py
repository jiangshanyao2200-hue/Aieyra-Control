import importlib.util
from pathlib import Path
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("product_jobs_control", ROOT / "service/main.py")
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)


class JobProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = control.Application({"resources": []}, Path(self.temp.name), hub=object())
        self.app.product_commands.source_provider = lambda: [
            {"id": "local", "product_host_ref": "local-reference"}
        ]
        self.result = dict(
            items=[
                dict(job_id="job-1", state="running", last_fact="running", cancel_supported=False)
            ],
            errors=[],
        )
        self.app.product_commands.product_job_sync = lambda source: self.result

    def test_poll_and_restart_never_invent_completion_or_current_observation(self):
        self.app.product_jobs.poll()
        self.assertTrue(self.app.product_jobs.snapshot()["available"])
        self.app.product_commands.product_job_sync = lambda source: (_ for _ in ()).throw(OSError())
        self.app.product_jobs.poll()
        source = self.app.product_jobs.snapshot()["sources"][0]
        self.assertFalse(source["available"])
        self.assertTrue(source["stale"])
        self.assertEqual(source["items"][0]["state"], "running")
        self.assertTrue(source["items"][0]["stale"])
        restarted = control.ProductJobs(self.app.store, self.app.product_commands)
        self.assertFalse(restarted.snapshot()["available"])
        self.assertEqual(restarted.snapshot()["sources"][0]["items"][0]["state"], "running")

    def test_idle_sampling_does_not_create_fake_events(self):
        self.app.product_jobs.poll()
        with self.app.store.db() as db:
            before = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        self.app.product_jobs.poll()
        with self.app.store.db() as db:
            self.assertEqual(before, db.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    def test_slow_receive_keeps_cached_get_responsive(self):
        self.app.product_jobs.poll()
        entered, release = threading.Event(), threading.Event()

        def slow(source):
            entered.set()
            release.wait(3)
            return self.result

        self.app.product_commands.product_job_sync = slow
        worker = threading.Thread(target=self.app.product_jobs.poll)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            self.assertEqual(
                self.app.product_jobs.snapshot()["sources"][0]["items"][0]["job_id"], "job-1"
            )
            self.assertLess(time.monotonic() - started, 0.2)
        finally:
            release.set()
            worker.join()

    def test_unconfigured_source_never_calls_receiver(self):
        self.app.product_commands.source_provider = lambda: [{"id": "local"}]
        self.app.product_commands.product_job_sync = lambda source: self.fail(
            "unconfigured receiver called"
        )
        self.app.product_jobs.poll()
        self.assertEqual(self.app.product_jobs.snapshot()["reason"], "product_host_not_configured")


if __name__ == "__main__":
    unittest.main()
