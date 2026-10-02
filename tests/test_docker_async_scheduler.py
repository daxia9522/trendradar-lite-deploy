"""Offline process-lifecycle tests: no crawler, AI, mail or storage services."""
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from deploy.docker import scheduler
from deploy.docker.runtime_config import load_runtime_config
from tests.fake_process import FakeProcess

ROOT = Path(__file__).resolve().parents[1]


class AsyncSchedulerTests(unittest.TestCase):
    def clock(self, **settings):
        return scheduler.Scheduler(lambda: load_runtime_config({"TZ": "UTC", **settings}), state={})

    def test_long_weekly_does_not_block_collection_or_reenter(self):
        clock = self.clock()
        weekly = FakeProcess([], None)
        crawler = FakeProcess([], None)
        with patch.object(scheduler.subprocess, "Popen", side_effect=[weekly, crawler]) as spawn, patch.object(scheduler, "_save_state"):
            clock.poll(datetime(2026, 10, 4, 12, 30, tzinfo=timezone.utc))
            clock.poll(datetime(2026, 10, 4, 12, 30, 20, tzinfo=timezone.utc))
            self.assertEqual(spawn.call_count, 1)
            clock.poll(datetime(2026, 10, 4, 13, 5, tzinfo=timezone.utc))
            self.assertEqual(spawn.call_count, 2)
            self.assertEqual(set(clock.running), {"weekly", "crawler"})
            self.assertEqual(clock.state, {})
            clock.poll(datetime(2026, 10, 4, 14, 5, tzinfo=timezone.utc))
            self.assertEqual(spawn.call_count, 2)
            crawler.returncode = 0
            clock._reap_children()
            self.assertEqual(clock.state["crawler"], "2026-10-04T13:05")
            self.assertIn("weekly", clock.running)
            weekly.returncode = 0
            clock._reap_children()
            self.assertFalse(clock.running)

    def test_shutdown_reaps_all_children_even_when_state_write_fails(self):
        clock = self.clock()
        first, second = FakeProcess([], 0), FakeProcess([], None)
        clock.running = {"crawler": scheduler.RunningTask("crawler", "marker", first, None),
                         "weekly": scheduler.RunningTask("weekly", "marker", second, None)}
        with patch.object(scheduler, "_save_state", side_effect=scheduler.SchedulerStateError()):
            with self.assertRaises(scheduler.SchedulerStateError):
                clock.shutdown()
        self.assertFalse(clock.running)
        self.assertEqual(second.signals, [signal.SIGTERM])

    def child(self, code):
        environment = {"PATH": os.defpath, "HOME": "/tmp", "PYTHONDONTWRITEBYTECODE": "1"}
        process = subprocess.Popen([sys.executable, "-c", code], cwd=ROOT, env=environment,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()
        self.addCleanup(cleanup)
        self.assertTrue(select.select([process.stdout], [], [], 10)[0], "child did not become ready")
        self.assertEqual(process.stdout.readline().strip(), "ready")
        return process

    def test_real_term_ignoring_child_is_killed_and_waited(self):
        process = self.child("import signal, threading; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); threading.Event().wait()")
        clock = self.clock()
        clock.running["weekly"] = scheduler.RunningTask("weekly", "marker", process, None)
        started = time.monotonic()
        with patch.object(scheduler, "CHILD_SHUTDOWN_TIMEOUT", 0.05), patch.object(scheduler, "_save_state") as save:
            clock.shutdown()
        self.assertEqual(process.returncode, -signal.SIGKILL)
        self.assertLess(time.monotonic() - started, 3)
        self.assertFalse(clock.running)
        save.assert_not_called()

    def test_sigterm_wakes_real_scheduler_instead_of_waiting_entire_poll_interval(self):
        process = self.child("from deploy.docker import scheduler as s\ns.Scheduler.poll = lambda self: (print('ready', flush=True) or 3600)\ns._load_state = lambda: {}\nraise SystemExit(s.main({'TZ': 'UTC'}))")
        process.send_signal(signal.SIGTERM)
        self.assertEqual(process.wait(timeout=3), 0, process.stderr.read())
