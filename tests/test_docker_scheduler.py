import importlib.util
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "docker_scheduler", ROOT / "deploy" / "docker" / "scheduler.py"
)
scheduler = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(scheduler)


class DockerSchedulerTests(unittest.TestCase):
    def test_failed_run_is_not_marked_successful(self):
        state = {}
        with patch.object(scheduler.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)):
            with patch.object(scheduler, "_save_state") as save_state:
                self.assertFalse(scheduler._run("crawler", ["false"], "marker", state))
        self.assertEqual(state, {})
        save_state.assert_not_called()

    def test_successful_run_is_marked_successful(self):
        state = {}
        with patch.object(scheduler.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
            with patch.object(scheduler, "_save_state") as save_state:
                self.assertTrue(scheduler._run("crawler", ["true"], "marker", state))
        self.assertEqual(state, {"crawler": "marker"})
        save_state.assert_called_once_with(state)


if __name__ == "__main__":
    unittest.main()
