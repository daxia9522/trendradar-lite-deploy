import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from deploy.docker import entrypoint, scheduler
from deploy.docker.runtime_config import MAX_ENV_BYTES, RUNTIME_ENV_KEY
from deploy.envfile import atomic_write


class DockerEntrypointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "env"
        self.base = {"PATH": "/safe/bin", "AI_API_KEY": "stale"}
        self.base.update([(RUNTIME_ENV_KEY, str(self.path))])
        self.put("initial")

    def put(self, key):
        atomic_write(self.path, f"AI_API_KEY={key}\nTZ=UTC\n".encode())

    def test_doctor_weekly_manual_all_load_latest_runtime_without_global_mutation(self):
        commands = [
            (["doctor"], ["-m", "trendradar", "--doctor"]),
            (["current"], ["-m", "trendradar"]),
            (["force-run"], ["-m", "trendradar", "--force-run"]),
            (["show-schedule"], ["-m", "trendradar", "--show-schedule"]),
            (["test-notification"], ["-m", "trendradar", "--test-notification"]),
            (["weekly", "--dry-run", "--subject", "literal ; $(not-a-shell)"],
             [entrypoint.WEEKLY_SCRIPT, "--dry-run", "--subject", "literal ; $(not-a-shell)"]),
            (["python", "-m", "trendradar", "--doctor"], ["-m", "trendradar", "--doctor"]),
            (["python3", entrypoint.WEEKLY_SCRIPT, "--start=2026-09-01", "--end=2026-09-07", "--dry-run"],
             [entrypoint.WEEKLY_SCRIPT, "--start=2026-09-01", "--end=2026-09-07", "--dry-run"]),
            (["python", "/app/" + entrypoint.WEEKLY_SCRIPT, "--model", "synthetic/test"],
             [entrypoint.WEEKLY_SCRIPT, "--model", "synthetic/test"]),
        ]
        before = dict(os.environ)
        for index, (args, expected) in enumerate(commands):
            self.put(f"new-{index}")
            with patch.object(entrypoint.os, "execve") as execute:
                self.assertEqual(entrypoint.main(args, self.base), 0)
            executable, command, env = execute.call_args.args
            self.assertEqual(executable, sys.executable)
            self.assertEqual(command, [sys.executable, *expected])
            self.assertEqual(env["AI_API_KEY"], f"new-{index}")
            self.assertEqual(env["DOCKER_CONTAINER"], "true")
            self.assertEqual(env["STORAGE_BACKEND"], "local")
        self.assertEqual(dict(os.environ), before)

    def test_config_check_is_local_and_never_imports_or_executes_application(self):
        with patch.object(entrypoint.os, "execve") as execute, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(entrypoint.main(["config-check"], self.base), 0)
        execute.assert_not_called()
        self.assertIn("external file", output.getvalue())
        self.assertNotIn("initial", output.getvalue())

    def test_default_and_schedule_delegate_only_after_runtime_validation(self):
        for args in ([], ["schedule"]):
            with patch("deploy.docker.scheduler.main", return_value=17) as schedule:
                self.assertEqual(entrypoint.main(args, self.base), 17)
                schedule.assert_called_once_with(base_env=self.base)
        self.path.unlink()
        with patch("deploy.docker.scheduler.main") as schedule, redirect_stderr(io.StringIO()):
            self.assertEqual(entrypoint.main([], self.base), 2)
        schedule.assert_not_called()

    def test_invalid_configuration_blocks_every_action_including_doctor(self):
        atomic_write(self.path, b'AI_API_KEY="SECRET_NOT_CLOSED\n')
        for args in (["doctor"], ["weekly", "--dry-run"], ["force-run"], ["current"], ["show-schedule"], ["config-check"]):
            with patch.object(entrypoint.os, "execve") as execute, redirect_stderr(io.StringIO()) as output:
                self.assertEqual(entrypoint.main(args, self.base), 2)
            execute.assert_not_called()
            self.assertNotIn("SECRET_NOT_CLOSED", output.getvalue())
            self.assertNotIn(str(self.path), output.getvalue())

    def test_never_runs_shell_arbitrary_programs_or_unrecognized_cli_arguments(self):
        for args in (["sh", "-c", "SECRET"], ["python", "-c", "SECRET"], ["python", "/tmp/SECRET.py"],
                     ["current", "--unknown=SECRET"], ["weekly", "--subject"], ["schedule", "SECRET"],
                     ["weekly", "--dry"], ["config-check", "SECRET"], ["doctor;SECRET"]):
            with patch.object(entrypoint.os, "execve") as execute, redirect_stderr(io.StringIO()) as output:
                self.assertEqual(entrypoint.main(list(args), self.base), 2)
            execute.assert_not_called()
            self.assertNotIn("SECRET", output.getvalue())

    def test_exec_failure_is_redacted_and_legacy_mode_is_supported(self):
        with patch.object(entrypoint.os, "execve", side_effect=OSError("SECRET")), redirect_stderr(io.StringIO()) as output:
            self.assertEqual(entrypoint.main(["doctor"], self.base), 1)
        self.assertNotIn("SECRET", output.getvalue())
        with patch.object(entrypoint.os, "execve") as execute:
            self.assertEqual(entrypoint.main(["doctor"], {"AI_API_KEY": "legacy"}), 0)
        self.assertEqual(execute.call_args.args[2]["AI_API_KEY"], "legacy")


    def test_oversized_runtime_is_rejected_for_manual_and_scheduled_commands(self):
        atomic_write(self.path, b"TZ=UTC\n#" + b"x" * MAX_ENV_BYTES)
        for args in (["current"], ["weekly"], ["doctor"], ["config-check"], ["schedule"]):
            with patch.object(entrypoint.os, "execve") as execute, patch.object(scheduler, "main") as schedule:
                with redirect_stderr(io.StringIO()) as output:
                    self.assertEqual(entrypoint.main(args, self.base), 2)
            execute.assert_not_called()
            schedule.assert_not_called()
            self.assertIn("too large", output.getvalue())

    def test_schedule_corrupt_state_returns_fixed_error_without_application_launch(self):
        state_path = Path(self.temp.name) / "state.json"
        state_path.write_text('{"crawler": null}')
        with patch.object(scheduler, "STATE_PATH", state_path), patch.object(scheduler, "STOP", False):
            with patch.object(scheduler.subprocess, "run") as run, patch.object(scheduler.time, "sleep") as sleep:
                with redirect_stderr(io.StringIO()) as output:
                    self.assertEqual(entrypoint.main(["schedule"], self.base), 2)
        run.assert_not_called()
        sleep.assert_not_called()
        self.assertIn("scheduler state", output.getvalue())


if __name__ == "__main__":
    unittest.main()
