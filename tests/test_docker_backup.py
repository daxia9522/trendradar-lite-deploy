"""Optional Docker backup boundary, with synthetic credentials and mocked children."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from deploy.docker import entrypoint, runtime_config as runtime, scheduler
from deploy.envfile import atomic_write


ENABLED = {
    "TZ": "UTC", "STORAGE_BACKEND": "local", "R2_BACKUP_ENABLED": "true",
    "S3_BUCKET_NAME": "synthetic-bucket", "S3_ENDPOINT_URL": "https://s3.example.invalid",
    "S3_ACCESS_KEY_ID": "synthetic-access-id", "S3_SECRET_ACCESS_KEY": "synthetic-secret",
}


class DockerBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "env"
        self.base = {runtime.RUNTIME_ENV_KEY: str(self.path), "S3_SECRET_ACCESS_KEY": "stale-base-secret"}
        self.values = dict(ENABLED)
        self.put()
        self.clock = scheduler.Scheduler(lambda: runtime.load_runtime_config(self.base), state={})
        for name, value in (("_save_state", None), ("STOP", False)):
            mocked = patch.object(scheduler, name) if value is None else patch.object(scheduler, name, value)
            mocked.start()
            self.addCleanup(mocked.stop)

    def put(self, updates=None, remove=()):
        self.values.update(updates or {})
        for key in remove:
            self.values.pop(key, None)
        atomic_write(self.path, "".join(f"{key}={value}\n" for key, value in self.values.items()).encode())

    def at(self, hour=23, minute=40, second=0):
        return datetime(2026, 9, 20, hour, minute, second, tzinfo=timezone.utc)

    def successful(self):
        return patch.object(scheduler.subprocess, "run", return_value=subprocess.CompletedProcess([], 0))

    def test_only_three_explicit_backup_keys_are_supported(self):
        keys = {"R2_BACKUP_ENABLED", "R2_BACKUP_TIME", "R2_BACKUP_LOOKBACK_DAYS"}
        self.assertTrue(keys <= runtime.APP_KEYS)
        for key in ("R2_BACKUP_COMMAND", "R2_BACKUP_SECRET", "R2_UNKNOWN"):
            with self.assertRaises(runtime.RuntimeConfigError) as error:
                runtime.parse_runtime_env(f"{key}=synthetic\n".encode())
            self.assertEqual(error.exception.code, "unsupported")

    def test_default_disabled_needs_no_credentials_and_selects_no_backup(self):
        self.values = {"TZ": "UTC"}
        self.put()
        snapshot = runtime.load_runtime_config(self.base)
        self.assertFalse(snapshot.settings.backup_enabled)
        self.assertEqual(snapshot.settings.backup_time, "23:40")
        self.assertNotIn("S3_SECRET_ACCESS_KEY", snapshot.env)
        self.clock.poll(self.at(minute=39))
        with self.successful() as run:
            self.clock.poll(self.at())
            self.clock.poll(self.at(second=20))
        run.assert_not_called()

    def test_invalid_backup_is_value_free_and_config_check_fails_closed(self):
        cases = [{"R2_BACKUP_ENABLED": "SENSITIVE"}, {"R2_BACKUP_TIME": "25:00"},
                 {"R2_BACKUP_LOOKBACK_DAYS": "0"}, {"R2_BACKUP_LOOKBACK_DAYS": "3661"},
                 {"S3_ENDPOINT_URL": "https://user:SENSITIVE@example.invalid"}]
        cases += [{key: ""} for key in ("S3_BUCKET_NAME", "S3_ENDPOINT_URL", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY")]
        for changed in cases:
            with self.subTest(changed=changed):
                self.values = dict(ENABLED, **changed)
                self.put()
                with patch.object(entrypoint.os, "execve") as execute, redirect_stderr(io.StringIO()) as output:
                    self.assertEqual(entrypoint.main(["config-check"], self.base), 2)
                execute.assert_not_called()
                self.assertNotIn("SENSITIVE", output.getvalue())
                self.assertNotIn("synthetic", output.getvalue())
                self.assertIn("backup settings", output.getvalue())

    def test_enabled_requires_explicit_local_identity_before_runtime_injection(self):
        self.put(remove=("STORAGE_BACKEND",))
        for base in (self.base, dict(self.values)):
            with self.subTest(external=runtime.RUNTIME_ENV_KEY in base):
                with self.assertRaises(runtime.RuntimeConfigError) as error:
                    runtime.load_runtime_config(base)
                self.assertEqual(error.exception.code, "backup")

    def test_legacy_schedule_constructor_compatible_signature_omits_credentials_and_days(self):
        old = runtime.ScheduleSettings(ZoneInfo("UTC"), 0, frozenset(), 6, 12, 30, 20, 3)
        self.assertFalse(old.backup_enabled)
        self.assertEqual(old.backup_time, "23:40")
        self.assertNotEqual(old.timing_signature, replace(old, backup_enabled=True).timing_signature)
        self.assertNotEqual(old.timing_signature, replace(old, backup_time="23:41").timing_signature)
        first = runtime.load_runtime_config(self.base)
        self.put({"S3_SECRET_ACCESS_KEY": "rotated-secret", "R2_BACKUP_LOOKBACK_DAYS": "3"})
        second = runtime.load_runtime_config(self.base)
        self.assertEqual(first.settings.timing_signature, second.settings.timing_signature)
        self.assertNotIn("synthetic-secret", repr(first))
        with self.assertRaises(TypeError):
            first.env["S3_SECRET_ACCESS_KEY"] = "mutation"

    def test_daily_2340_runs_once_and_again_next_day(self):
        self.clock.poll(self.at(minute=39))
        with self.successful() as run:
            for seconds in (0, 20, 40):
                self.clock.poll(self.at(second=seconds))
            self.assertEqual(run.call_count, 1)
            self.clock.poll(self.at() + timedelta(days=1))
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args.args[0], [sys.executable, "deploy/r2_backup.py", "--configured"])
        self.assertEqual(self.clock.state["backup"], "2026-09-21T23:40Z")
        self.assertEqual(len(self.clock.state["_windows"]["backup"]), 2)

    def test_first_start_and_enabling_current_minute_do_not_backfill(self):
        with self.successful() as run:
            self.clock.poll(self.at())
            self.clock.poll(self.at(second=20))
            run.assert_not_called()
        self.put({"R2_BACKUP_ENABLED": "false"})
        clock = scheduler.Scheduler(lambda: runtime.load_runtime_config(self.base), state={})
        clock.poll(self.at(minute=39))
        self.put({"R2_BACKUP_ENABLED": "true"})
        with self.successful() as run:
            clock.poll(self.at())
            clock.poll(self.at(second=20))
            clock.poll(self.at(minute=41))
            run.assert_not_called()
            clock.poll(self.at() + timedelta(days=1))
        self.assertEqual(run.call_count, 1)

    def test_legacy_env_only_backup_also_avoids_startup_backfill(self):
        clock = scheduler.Scheduler(lambda: runtime.load_runtime_config(ENABLED), state={})
        with self.successful() as run:
            clock.poll(self.at())
            clock.poll(self.at(second=20))
            run.assert_not_called()
            clock.poll(self.at() + timedelta(days=1))
        self.assertEqual(run.call_count, 1)

    def test_missed_minutes_and_dst_spring_gap_are_not_backfilled(self):
        self.clock.poll(self.at(minute=39))
        with self.successful() as run:
            self.clock.poll(self.at(minute=41))
        run.assert_not_called()
        self.put({"TZ": "America/New_York", "R2_BACKUP_TIME": "02:30", "CRAWLER_MINUTE": "10"})
        clock = scheduler.Scheduler(lambda: runtime.load_runtime_config(self.base), state={})
        with self.successful() as run:
            clock.poll(datetime(2026, 3, 8, 6, 59, tzinfo=timezone.utc))
            clock.poll(datetime(2026, 3, 8, 7, 1, tzinfo=timezone.utc))
        run.assert_not_called()

    def test_dst_fold_deduplicates_backup_after_restart_and_equivalent_timezone(self):
        self.put({"TZ": "America/New_York", "R2_BACKUP_TIME": "01:40"})
        clock = scheduler.Scheduler(lambda: runtime.load_runtime_config(self.base), state={})
        at = lambda h, m: datetime(2026, 11, 1, h, m, tzinfo=timezone.utc)
        clock.poll(at(5, 39))
        with self.successful() as run:
            clock.poll(at(5, 40))
            self.put({"TZ": "America/Detroit"})
            state = json.loads(json.dumps(clock.state))
            restarted = scheduler.Scheduler(lambda: runtime.load_runtime_config(self.base), state=state)
            restarted.poll(at(6, 39))
            self.assertFalse(restarted.invalid)
            restarted.poll(at(6, 40))
            self.assertFalse(restarted.invalid)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(state["backup"], "2026-11-01T05:40Z")

    def test_old_state_without_backup_is_accepted_and_backup_history_is_validated(self):
        old = {"crawler": "2026-09-19T23:00", "weekly": "2026-09-13T12:30Z"}
        clock = scheduler.Scheduler(lambda: runtime.load_runtime_config(self.base), state=old.copy())
        clock.poll(self.at(minute=39))
        with self.successful():
            clock.poll(self.at())
        self.assertEqual(scheduler._validate_state(clock.state), clock.state)
        self.assertEqual(clock.state["crawler"], old["crawler"])
        self.assertEqual(clock.state["weekly"], old["weekly"])
        bad = json.loads(json.dumps(clock.state))
        bad["_windows"]["backup"][0]["local"] = "2026-09-20T23:41"
        with self.assertRaises(scheduler.SchedulerStateError):
            scheduler._validate_state(bad)

    def test_legacy_backup_marker_is_preserved_when_history_is_added(self):
        clock = scheduler.Scheduler(lambda: runtime.load_runtime_config(self.base),
                                    state={"backup": "2026-09-19T23:40"})
        clock.poll(self.at(minute=39))
        with self.successful():
            clock.poll(self.at())
        self.assertEqual(len(clock.state["_windows"]["backup"]), 2)
        scheduler._validate_state(clock.state)

    def all_due(self):
        self.put({"CRAWLER_MINUTE": "40", "WEEKLY_HOUR": "23", "WEEKLY_MINUTE": "40"})
        self.clock.poll(self.at(minute=39))

    def test_backup_last_and_rotated_credentials_are_taken_from_fresh_snapshot(self):
        self.all_due()
        before = dict(os.environ)

        def child(command, **kwargs):
            if "weekly_report" in command[1]:
                original = dict(kwargs["env"])
                self.put({"S3_ACCESS_KEY_ID": "new-id", "S3_SECRET_ACCESS_KEY": "new-secret",
                          "R2_BACKUP_LOOKBACK_DAYS": "3"})
                self.assertEqual(kwargs["env"], original)
            return subprocess.CompletedProcess(command, 0)

        with patch.object(scheduler.subprocess, "run", side_effect=child) as run:
            self.clock.poll(self.at())
        self.assertEqual(run.call_count, 3)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands[0][1:3], ["-m", "trendradar"])
        self.assertEqual(commands[1][1], entrypoint.WEEKLY_SCRIPT)
        self.assertEqual(commands[2][1:], [entrypoint.BACKUP_SCRIPT, "--configured"])
        self.assertEqual(run.call_args_list[1].kwargs["env"]["S3_SECRET_ACCESS_KEY"], "synthetic-secret")
        self.assertEqual(run.call_args.kwargs["env"]["S3_SECRET_ACCESS_KEY"], "new-secret")
        self.assertEqual(run.call_args.kwargs["env"]["R2_BACKUP_LOOKBACK_DAYS"], "3")
        self.assertEqual(dict(os.environ), before)

    def test_switch_or_time_change_cancels_pending_backup_and_other_selected_tasks(self):
        for change in ({"R2_BACKUP_ENABLED": "false"}, {"R2_BACKUP_TIME": "23:41"}):
            with self.subTest(change=change):
                self.values = dict(ENABLED)
                self.all_due()
                clock = scheduler.Scheduler(lambda: runtime.load_runtime_config(self.base), state={})
                clock.poll(self.at(minute=39))

                def child(command, **kwargs):
                    self.put(change)
                    return subprocess.CompletedProcess(command, 0)

                with patch.object(scheduler.subprocess, "run", side_effect=child) as run:
                    clock.poll(self.at())
                    clock.poll(self.at(second=20))
                self.assertEqual(run.call_count, 1)
                self.assertNotIn("weekly", clock.state)
                self.assertNotIn("backup", clock.state)

    def test_deleted_credential_pauses_and_restoration_does_not_backfill(self):
        self.clock.poll(self.at(minute=39))
        self.put(remove=("S3_SECRET_ACCESS_KEY",))
        with self.successful() as run, redirect_stdout(io.StringIO()) as output:
            self.clock.poll(self.at())
            self.clock.poll(self.at(second=10))
            self.assertTrue(self.clock.invalid)
            self.assertEqual(output.getvalue().count("paused:"), 1)
            self.put({"S3_SECRET_ACCESS_KEY": "repaired"})
            self.clock.poll(self.at(second=20))
            run.assert_not_called()
            self.clock.poll(self.at() + timedelta(days=1))
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.kwargs["env"]["S3_SECRET_ACCESS_KEY"], "repaired")

    def test_backup_revalidates_immediately_before_first_dispatch(self):
        self.clock.poll(self.at(minute=39))
        original = self.clock.loader
        reads = 0

        def loader():
            nonlocal reads
            reads += 1
            snapshot = original()
            if reads == 1:
                self.put(remove=("S3_SECRET_ACCESS_KEY",))
            return snapshot

        self.clock.loader = loader
        with self.successful() as run:
            self.clock.poll(self.at())
        run.assert_not_called()
        self.assertEqual(reads, 2)
        self.assertTrue(self.clock.invalid)

    def test_long_running_business_task_keeps_selected_backup_window_and_fresh_secret(self):
        self.put({"CRAWLER_MINUTE": "40"})
        instant = self.at(minute=39)
        clock = scheduler.Scheduler(lambda: runtime.load_runtime_config(self.base), state={}, clock=lambda: instant)
        clock.poll()
        instant = self.at()

        def child(command, **kwargs):
            nonlocal instant
            if command[1] == "-m":
                instant = self.at(minute=42)
                self.put({"S3_SECRET_ACCESS_KEY": "after-long-crawler"})
            return subprocess.CompletedProcess(command, 0)

        with patch.object(scheduler.subprocess, "run", side_effect=child) as run:
            clock.poll()
            clock.poll()
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args.args[0][1], entrypoint.BACKUP_SCRIPT)
        self.assertEqual(run.call_args.kwargs["env"]["S3_SECRET_ACCESS_KEY"], "after-long-crawler")
        self.assertEqual(clock.state["backup"], "2026-09-20T23:40Z")

    def test_clock_rollback_during_business_task_cancels_selected_backup(self):
        self.put({"CRAWLER_MINUTE": "40"})
        instant = self.at(minute=39)
        clock = scheduler.Scheduler(lambda: runtime.load_runtime_config(self.base), state={}, clock=lambda: instant)
        clock.poll()
        instant = self.at(second=20)

        def child(command, **kwargs):
            nonlocal instant
            instant = self.at(second=10)
            return subprocess.CompletedProcess(command, 0)

        with patch.object(scheduler.subprocess, "run", side_effect=child) as run:
            clock.poll()
            instant = self.at(second=40)
            clock.poll()
        self.assertEqual(run.call_count, 1)
        self.assertNotIn("backup", clock.state)

    def test_backup_failure_code_six_retries_with_same_budget_not_weekly_partial(self):
        self.clock.poll(self.at(minute=39))
        with patch.object(scheduler.subprocess, "run", return_value=subprocess.CompletedProcess([], 6)) as run:
            self.clock.poll(self.at())
            self.put({"S3_SECRET_ACCESS_KEY": "retry-secret"})
            for second in (10, 20, 30, 40, 50):
                self.clock.poll(self.at(second=second))
        self.assertEqual(run.call_count, 3)
        self.assertEqual(run.call_args.kwargs["env"]["S3_SECRET_ACCESS_KEY"], "retry-secret")
        self.assertNotIn("backup", self.clock.state)
        self.assertEqual(self.clock.attempts["backup"], ("2026-09-20T23:40Z", 3))

    def test_weekly_partial_still_deduplicates_and_does_not_prevent_backup(self):
        self.all_due()
        with patch.object(scheduler.subprocess, "run", side_effect=[subprocess.CompletedProcess([], code)
                                                                  for code in (0, 6, 0)]) as run:
            self.clock.poll(self.at())
            self.clock.poll(self.at(second=20))
        self.assertEqual(run.call_count, 3)
        self.assertEqual(self.clock.state["weekly"], self.clock.state["backup"])

    def test_cli_backup_only_allows_fixed_configured_mapping(self):
        for options in ([], ["--dry-run"]):
            with patch.object(entrypoint.os, "execve") as execute:
                self.assertEqual(entrypoint.main(["backup", *options], self.base), 0)
            self.assertEqual(execute.call_args.args[1], [sys.executable, "deploy/r2_backup.py", "--configured", *options])
            self.assertEqual(execute.call_args.args[2]["S3_SECRET_ACCESS_KEY"], "synthetic-secret")
        for args in (["backup", "--configured"], ["backup", "--dry"], ["backup", "--dry-run", "--dry-run"],
                     ["backup", "--date", "SENSITIVE"], ["backup", "--data-dir=SENSITIVE"],
                     ["backup", ";SENSITIVE"], ["python", "deploy/r2_backup.py", "--configured"],
                     ["backup", "--dry-run=SENSITIVE"], ["backup", "--help"]):
            with patch.object(entrypoint.os, "execve") as execute, redirect_stderr(io.StringIO()) as output:
                self.assertEqual(entrypoint.main(args, self.base), 2)
            execute.assert_not_called()
            self.assertNotIn("SENSITIVE", output.getvalue())

    def test_config_check_reports_nonsecret_backup_schedule_without_app_import(self):
        with patch.object(entrypoint.os, "execve") as execute, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(entrypoint.main(["config-check"], self.base), 0)
        execute.assert_not_called()
        self.assertIn("backup enabled; daily 23:40 (UTC)", output.getvalue())
        self.assertNotIn("synthetic", output.getvalue())


if __name__ == "__main__":
    unittest.main()
