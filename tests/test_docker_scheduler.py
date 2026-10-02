"""Task outcome bookkeeping is driven by nonblocking process completion."""
import unittest
from unittest.mock import patch

from deploy.docker import scheduler
from deploy.docker.runtime_config import load_runtime_config
from tests.fake_process import FakeProcess


class DockerSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.clock = scheduler.Scheduler(lambda: load_runtime_config({"TZ": "UTC"}), state={})

    def test_failed_run_is_not_marked_successful(self):
        with patch.object(scheduler.subprocess, "Popen", return_value=FakeProcess([], 1)):
            with patch.object(scheduler, "_save_state") as save:
                self.assertTrue(self.clock._spawn("crawler", ["synthetic"], "marker", {}))
        self.assertEqual(self.clock.state, {})
        self.assertFalse(self.clock.running)
        save.assert_not_called()

    def test_successful_run_is_marked_only_after_exit(self):
        child = FakeProcess([], None)
        with patch.object(scheduler.subprocess, "Popen", return_value=child):
            with patch.object(scheduler, "_save_state") as save:
                self.clock._spawn("crawler", ["synthetic"], "marker", {})
                self.assertEqual(self.clock.state, {})
                save.assert_not_called()
                child.returncode = 0
                self.clock._reap_children()
                self.assertEqual(self.clock.state, {"crawler": "marker"})
                save.assert_called_once_with(self.clock.state)


if __name__ == "__main__":
    unittest.main()
