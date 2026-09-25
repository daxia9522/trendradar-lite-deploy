import io
import json
import os
import signal
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from deploy.docker import scheduler
from deploy.docker.runtime_config import RUNTIME_ENV_KEY, load_runtime_config
from deploy.envfile import atomic_write


class RuntimeSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "env"
        self.base = {"AI_API_KEY": "stale-secret"}
        self.base.update([(RUNTIME_ENV_KEY, str(self.path))])
        self.values = {
            "TZ": "UTC", "AI_API_KEY": "first", "CRAWLER_MINUTE": "0",
            "WEEKLY_WEEKDAY": "6", "WEEKLY_HOUR": "12", "WEEKLY_MINUTE": "0",
        }
        self.put()
        self.clock = scheduler.Scheduler(lambda: load_runtime_config(self.base), state={})
        self.save_patch = patch.object(scheduler, "_save_state")
        self.save = self.save_patch.start()
        self.addCleanup(self.save_patch.stop)

    def put(self, updates=None, remove=()):
        self.values.update(updates or {})
        for key in remove:
            self.values.pop(key, None)
        atomic_write(self.path, "".join(f"{key}={value}\n" for key, value in self.values.items()).encode())

    def at(self, hour, minute, second=0):
        # Sunday, so weekly is due at 12:00 UTC as well as daily collection.
        return datetime(2026, 9, 20, hour, minute, second, tzinfo=timezone.utc)

    def test_startup_due_minute_is_not_backfilled_but_next_due_runs(self):
        with patch.object(scheduler.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            self.clock.poll(self.at(12, 0))
            self.clock.poll(self.at(12, 0, 20))
            self.clock.poll(self.at(12, 0, 40))
            run.assert_not_called()
            self.clock.poll(self.at(13, 0))
            self.clock.poll(self.at(13, 0, 20))
        self.assertEqual(run.call_count, 1)
        self.assertEqual(self.clock.state["crawler"], "2026-09-20T13:00Z")
        self.assertNotIn("weekly", self.clock.state)
        self.assertEqual(len(self.clock.state["_windows"]["crawler"]), 1)

    def test_new_timing_cannot_trigger_current_minute_and_repeated_save_does_not_rearm(self):
        self.clock.poll(self.at(11, 59))
        with patch.object(scheduler.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            self.put({"CRAWLER_MINUTE": "1", "WEEKLY_MINUTE": "1"})
            self.clock.poll(self.at(12, 1))
            self.clock.poll(self.at(12, 1, 20))
            run.assert_not_called()
            self.put()  # Same logical settings, new inode: do not suppress 13:01.
            self.clock.poll(self.at(13, 1))
            self.clock.poll(self.at(13, 1, 20))
        self.assertEqual(run.call_count, 1)

    def test_timezone_switch_dedup_uses_actual_minute_and_no_backfill(self):
        self.clock.poll(self.at(11, 59))
        with patch.object(scheduler.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            self.clock.poll(self.at(12, 0))
            self.assertEqual(run.call_count, 2)
            self.put({"TZ": "Asia/Shanghai", "WEEKLY_HOUR": "20"})
            self.clock.poll(self.at(12, 0, 20))
            self.clock.poll(self.at(12, 0, 40))
            self.assertEqual(run.call_count, 2)
            self.clock.poll(self.at(13, 0))
            self.assertEqual(run.call_count, 3)
            # Even a backwards wall clock cannot revisit 12:00 UTC.
            self.clock.poll(self.at(12, 0, 50))
        self.assertEqual(run.call_count, 3)
        self.assertEqual(self.clock.state["weekly"], "2026-09-20T12:00Z")

    def test_each_new_task_gets_latest_complete_snapshot_after_first_child(self):
        self.clock.poll(self.at(11, 59))

        def child(command, **kwargs):
            if command[1] == "-m":
                old_env = dict(kwargs["env"])
                self.put({"AI_MODEL": "openai/after", "EMAIL_TO": "new@example.invalid"},
                         remove=("AI_API_KEY",))
                self.assertEqual(kwargs["env"], old_env)  # Running child remains unchanged.
            return subprocess.CompletedProcess(command, 0)

        with patch.object(scheduler.subprocess, "run", side_effect=child) as run:
            self.clock.poll(self.at(12, 0))
            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args_list[0].kwargs["env"]["AI_API_KEY"], "first")
            latest = run.call_args_list[1].kwargs["env"]
            self.assertNotIn("AI_API_KEY", latest)
            self.assertEqual(latest["AI_MODEL"], "openai/after")
            self.assertEqual(latest["EMAIL_TO"], "new@example.invalid")

    def test_invalid_config_during_first_child_blocks_second_and_recovery_no_backfill(self):
        self.clock.poll(self.at(11, 59))

        def child(command, **kwargs):
            atomic_write(self.path, b"TZ=invalid-synthetic-zone\n")
            return subprocess.CompletedProcess(command, 0)

        with patch.object(scheduler.subprocess, "run", side_effect=child) as run:
            self.clock.poll(self.at(12, 0))
            self.assertEqual(run.call_count, 1)
            self.put()
            self.clock.poll(self.at(12, 0, 20))
            self.assertEqual(run.call_count, 1)
            run.side_effect = None
            run.return_value = subprocess.CompletedProcess([], 0)
            self.clock.poll(self.at(13, 0))
        self.assertEqual(run.call_count, 2)

    def test_first_dispatch_reloads_after_initial_poll_read(self):
        self.clock.poll(self.at(11, 59))
        original_loader = self.clock.loader
        reads = 0

        def loader():
            nonlocal reads
            reads += 1
            snapshot = original_loader()
            if reads == 1:
                self.path.unlink()
            return snapshot

        self.clock.loader = loader
        with patch.object(scheduler.subprocess, "run") as run:
            self.clock.poll(self.at(12, 0))
        self.assertEqual(reads, 2)
        run.assert_not_called()

    def test_stop_during_running_child_does_not_launch_second_child(self):
        self.clock.poll(self.at(11, 59))

        def child(command, **kwargs):
            scheduler._stop(15, None)
            return subprocess.CompletedProcess(command, 0)

        with patch.object(scheduler, "STOP", False):
            with patch.object(scheduler.subprocess, "run", side_effect=child) as run:
                self.clock.poll(self.at(12, 0))
        self.assertEqual(run.call_count, 1)

    def test_timing_or_timezone_change_in_first_child_rearms_without_backfill(self):
        for change in ({"WEEKLY_MINUTE": "1"}, {"TZ": "Asia/Shanghai", "WEEKLY_HOUR": "20"}):
            with self.subTest(change=change):
                self.put({"TZ": "UTC", "WEEKLY_HOUR": "12", "WEEKLY_MINUTE": "0"})
                clock = scheduler.Scheduler(lambda: load_runtime_config(self.base), state={})
                clock.poll(self.at(11, 59))

                def child(command, **kwargs):
                    self.put(change)
                    return subprocess.CompletedProcess(command, 0)

                with patch.object(scheduler.subprocess, "run", side_effect=child) as run:
                    clock.poll(self.at(12, 0))
                    clock.poll(self.at(12, 0, 20))
                self.assertEqual(run.call_count, 1)

    def test_long_running_child_keeps_selected_weekly_window_with_latest_environment(self):
        instant = self.at(11, 59)
        clock = scheduler.Scheduler(lambda: load_runtime_config(self.base), state={}, clock=lambda: instant)
        clock.poll()
        instant = self.at(12, 0)

        def child(command, **kwargs):
            nonlocal instant
            if command[1] == "-m":
                instant = self.at(12, 1)
                self.put({"AI_MODEL": "openai/after-long-crawler"}, remove=("AI_API_KEY",))
            return subprocess.CompletedProcess(command, 0)

        with patch.object(scheduler.subprocess, "run", side_effect=child) as run:
            clock.poll()
            clock.poll()
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[1].kwargs["env"]["AI_MODEL"], "openai/after-long-crawler")
        self.assertNotIn("AI_API_KEY", run.call_args_list[1].kwargs["env"])
        self.assertEqual(clock.state["weekly"], "2026-09-20T12:00Z")

    def test_long_running_child_does_not_add_originally_unselected_weekly(self):
        self.put({"WEEKLY_MINUTE": "1"})
        instant = self.at(11, 59)
        clock = scheduler.Scheduler(lambda: load_runtime_config(self.base), state={}, clock=lambda: instant)
        clock.poll()
        instant = self.at(12, 0)

        def child(command, **kwargs):
            nonlocal instant
            instant = self.at(12, 1)
            return subprocess.CompletedProcess(command, 0)

        with patch.object(scheduler.subprocess, "run", side_effect=child) as run:
            clock.poll()
        self.assertEqual(run.call_count, 1)
        self.assertNotIn("weekly", clock.state)

    def test_selected_weekly_partial_after_long_child_is_deduplicated_across_dst_restart(self):
        self.put({"TZ": "America/New_York", "CRAWLER_MINUTE": "30",
                  "WEEKLY_HOUR": "1", "WEEKLY_MINUTE": "30"})
        at = lambda h, m: datetime(2026, 11, 1, h, m, tzinfo=timezone.utc)
        instant = at(5, 29)
        clock = scheduler.Scheduler(lambda: load_runtime_config(self.base), state={}, clock=lambda: instant)
        clock.poll()
        instant = at(5, 30)

        def child(command, **kwargs):
            nonlocal instant
            if command[1] == "-m":
                instant = at(5, 32)
                self.put({"AI_MODEL": "openai/fresh-partial"})
                return subprocess.CompletedProcess(command, 0)
            return subprocess.CompletedProcess(command, 6)

        with patch.object(scheduler.subprocess, "run", side_effect=child) as run:
            clock.poll()
            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args_list[1].kwargs["env"]["AI_MODEL"], "openai/fresh-partial")
            clock.poll()
            restarted = scheduler.Scheduler(lambda: load_runtime_config(self.base), state=json.loads(json.dumps(clock.state)))
            restarted.poll(at(6, 29))
            restarted.poll(at(6, 30))
        self.assertEqual(run.call_count, 2)
        self.assertEqual(clock.state["weekly"], "2026-11-01T05:30Z")
        self.assertEqual(clock.state["_windows"]["weekly"][0]["local"], "2026-11-01T01:30")

    def test_pending_window_cancelled_by_backward_clock_is_not_revisited_at_catchup(self):
        instant = self.at(11, 59)
        clock = scheduler.Scheduler(lambda: load_runtime_config(self.base), state={}, clock=lambda: instant)
        clock.poll()
        instant = self.at(12, 0, 20)

        def child(command, **kwargs):
            nonlocal instant
            instant = self.at(12, 0, 10)
            return subprocess.CompletedProcess(command, 0)

        with patch.object(scheduler.subprocess, "run", side_effect=child) as run:
            clock.poll()
            instant = self.at(12, 0, 40)
            clock.poll()
        self.assertEqual(run.call_count, 1)
        self.assertNotIn("weekly", clock.state)

    def test_invalid_file_never_reuses_old_secrets_log_transition_only_and_recover_safely(self):
        self.clock.poll(self.at(11, 59))
        atomic_write(self.path, b'AI_API_KEY="SECRET_INVALID\n')
        with patch.object(scheduler.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            with redirect_stdout(io.StringIO()) as output:
                for second in (0, 20, 40):
                    self.clock.poll(self.at(12, 0, second))
            run.assert_not_called()
            self.assertEqual(output.getvalue().count("paused:"), 1)
            self.assertNotIn("SECRET_INVALID", output.getvalue())
            self.put({"AI_API_KEY": "fixed"})
            self.clock.poll(self.at(12, 0, 50))
            run.assert_not_called()
            self.clock.poll(self.at(13, 0))
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.kwargs["env"]["AI_API_KEY"], "fixed")

    def test_invalid_initial_file_can_be_polled_without_stale_fallback(self):
        self.path.unlink()
        with patch.object(scheduler.subprocess, "run") as run, redirect_stdout(io.StringIO()):
            self.clock.poll(self.at(11, 59))
            self.put()
            self.clock.poll(self.at(12, 0))
            self.clock.poll(self.at(12, 0, 20))
        run.assert_not_called()

    def test_retry_budget_stays_three_and_each_retry_gets_fresh_environment(self):
        self.put({"WEEKLY_MINUTE": "30"})
        self.clock.poll(self.at(11, 59))
        with patch.object(scheduler.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)) as run:
            self.clock.poll(self.at(12, 0))
            self.put({"AI_API_KEY": "second"})
            self.clock.poll(self.at(12, 0, 10))
            self.put(remove=("AI_API_KEY",))
            self.clock.poll(self.at(12, 0, 20))
            self.clock.poll(self.at(12, 0, 40))
            self.assertEqual(run.call_count, 3)
            self.assertEqual(run.call_args_list[0].kwargs["env"]["AI_API_KEY"], "first")
            self.assertEqual(run.call_args_list[1].kwargs["env"]["AI_API_KEY"], "second")
            self.assertNotIn("AI_API_KEY", run.call_args_list[2].kwargs["env"])
            self.clock.poll(self.at(13, 0))
        self.assertEqual(run.call_count, 4)
        self.assertEqual(self.clock.state, {})
        self.save.assert_not_called()

    def test_partial_weekly_is_terminal_and_other_failure_retries(self):
        self.clock.poll(self.at(11, 59))

        def child(command, **kwargs):
            code = 6 if command[1].endswith("weekly_ai_report_email.py") else 1
            return subprocess.CompletedProcess(command, code)

        with patch.object(scheduler.subprocess, "run", side_effect=child) as run:
            for second in (0, 10, 20, 30, 40):
                self.clock.poll(self.at(12, 0, second))
        self.assertEqual(run.call_count, 4)  # 3 crawler failures, 1 partial weekly.
        self.assertEqual(self.clock.state["weekly"], "2026-09-20T12:00Z")
        self.assertNotIn("crawler", self.clock.state)
        self.assertEqual(len(self.clock.state["_windows"]["weekly"]), 1)
        self.save.assert_called_once_with(self.clock.state)

    def test_process_spawn_failure_retries_without_logging_exception_secrets(self):
        self.put({"WEEKLY_MINUTE": "30"})
        self.clock.poll(self.at(11, 59))
        with patch.object(scheduler.subprocess, "run", side_effect=OSError("SECRET_EXEC_FAILURE")) as run:
            with redirect_stdout(io.StringIO()) as output:
                for second in (0, 10, 20, 30):
                    self.clock.poll(self.at(12, 0, second))
        self.assertEqual(run.call_count, 3)
        self.assertNotIn("SECRET_EXEC_FAILURE", output.getvalue())
        self.assertEqual(self.clock.state, {})

    def test_legacy_env_only_keeps_first_due_minute_and_existing_task_markers(self):
        legacy = scheduler.Scheduler(lambda: load_runtime_config({"TZ": "UTC", "CRAWLER_MINUTE": "0"}), state={})
        with patch.object(scheduler.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            legacy.poll(self.at(12, 0))
            legacy.poll(self.at(12, 0, 20))
        self.assertEqual(run.call_count, 1)
        self.assertEqual(legacy.state, {"crawler": "2026-09-20T12:00"})

    def test_poll_settings_reload_without_a_real_sleep_loop(self):
        self.assertEqual(self.clock.poll(self.at(11, 59)), 20)
        self.put({"SCHEDULER_POLL_SECONDS": "35", "SCHEDULER_MAX_ATTEMPTS": "2"})
        with patch.object(scheduler.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)) as run:
            self.assertEqual(self.clock.poll(self.at(12, 0)), 35)
            self.clock.poll(self.at(12, 0, 10))
            self.clock.poll(self.at(12, 0, 20))
        self.assertEqual(run.call_count, 4)  # two attempts for each due job

    def test_initial_bad_config_makes_scheduler_startup_fail(self):
        self.path.unlink()
        with patch.object(scheduler.time, "sleep") as sleep:
            self.assertEqual(scheduler.main(self.base), 2)
        sleep.assert_not_called()

    def test_dst_fold_does_not_repeat_weekly_local_window_even_after_restart(self):
        self.put({"TZ": "America/New_York", "CRAWLER_MINUTE": "17",
                  "WEEKLY_HOUR": "1", "WEEKLY_MINUTE": "30"})
        def at(hour, minute):
            return datetime(2026, 11, 1, hour, minute, tzinfo=timezone.utc)
        with patch.object(scheduler.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            self.clock.poll(at(5, 29))
            self.clock.poll(at(5, 30))  # First local 01:30 (EDT).
            self.clock.poll(at(6, 30))  # Second local 01:30 (EST).
            self.assertEqual(run.call_count, 1)
            restarted = scheduler.Scheduler(lambda: load_runtime_config(self.base), state=dict(self.clock.state))
            restarted.poll(at(6, 29))
            restarted.poll(at(6, 30))
        self.assertEqual(run.call_count, 1)
        self.assertEqual(self.clock.state["weekly"], "2026-11-01T05:30Z")

    def test_dst_fold_partial_weekly_is_not_retried(self):
        self.put({"TZ": "America/New_York", "CRAWLER_MINUTE": "17",
                  "WEEKLY_HOUR": "1", "WEEKLY_MINUTE": "30"})
        with patch.object(scheduler.subprocess, "run", return_value=subprocess.CompletedProcess([], 6)) as run:
            self.clock.poll(datetime(2026, 11, 1, 5, 29, tzinfo=timezone.utc))
            self.clock.poll(datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc))
            self.clock.poll(datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc))
        self.assertEqual(run.call_count, 1)

    def test_dst_multiple_crawler_windows_survive_restart_and_weekly_partial(self):
        self.put({"TZ": "America/New_York", "MORNING_PUSH_TIME": "01:30",
                  "WEEKLY_HOUR": "1", "WEEKLY_MINUTE": "30"})
        at = lambda h, m: datetime(2026, 11, 1, h, m, tzinfo=timezone.utc)

        def child(command, **kwargs):
            return subprocess.CompletedProcess(command, 6 if command[1].endswith(".py") else 0)

        with patch.object(scheduler.subprocess, "run", side_effect=child) as run:
            for h, m in ((4, 59), (5, 0), (5, 30)):
                self.clock.poll(at(h, m))
            state_path = Path(self.temp.name) / "state.json"
            state_path.write_text(json.dumps(self.clock.state))
            with patch.object(scheduler, "STATE_PATH", state_path):
                restarted = scheduler.Scheduler(lambda: load_runtime_config(self.base))
            restarted.poll(at(5, 59))
            restarted.poll(at(6, 0))
            restarted.poll(at(6, 30))
            self.assertEqual(run.call_count, 3)  # Two crawler windows, one partial weekly.
            restarted.poll(at(7, 0))
        self.assertEqual(run.call_count, 4)

    def test_equivalent_timezone_during_fold_preserves_all_prior_business_windows(self):
        self.put({"TZ": "America/New_York", "MORNING_PUSH_TIME": "01:30", "WEEKLY_HOUR": "12"})
        at = lambda h, m: datetime(2026, 11, 1, h, m, tzinfo=timezone.utc)
        with patch.object(scheduler.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            self.clock.poll(at(4, 59))
            self.clock.poll(at(5, 0))
            self.clock.poll(at(5, 30))
            self.put({"TZ": "America/Detroit"})
            self.assertEqual(load_runtime_config(self.base).settings.timezone.key, "America/Detroit")
            self.clock.poll(at(5, 59))
            self.clock.poll(at(6, 0))
            self.clock.poll(at(6, 30))
        self.assertEqual(run.call_count, 2)

    def test_failed_dst_window_is_still_retryable_not_a_completion(self):
        self.put({"TZ": "America/New_York", "MORNING_PUSH_TIME": "01:30",
                  "WEEKLY_HOUR": "12"})
        at = lambda h, m, s=0: datetime(2026, 11, 1, h, m, s, tzinfo=timezone.utc)
        with patch.object(scheduler.subprocess, "run", side_effect=[
                subprocess.CompletedProcess([], 1), subprocess.CompletedProcess([], 0),
                subprocess.CompletedProcess([], 0)]) as run:
            self.clock.poll(at(4, 59))
            self.clock.poll(at(5, 0))
            self.clock.poll(at(5, 0, 20))
            self.clock.poll(at(5, 30))
            self.clock.poll(at(6, 0))
            self.clock.poll(at(6, 30))
        self.assertEqual(run.call_count, 3)

    def test_uncompleted_dst_window_keeps_retry_budget_and_retries_in_second_fold(self):
        self.put({"TZ": "America/New_York", "MORNING_PUSH_TIME": "01:30", "WEEKLY_HOUR": "12"})
        at = lambda h, m, s=0: datetime(2026, 11, 1, h, m, s, tzinfo=timezone.utc)
        with patch.object(scheduler.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)) as run:
            self.clock.poll(at(4, 59))
            for second in (0, 10, 20, 30):
                self.clock.poll(at(5, 0, second))
            self.assertEqual(run.call_count, 3)
            self.assertEqual(self.clock.state, {})
            run.return_value = subprocess.CompletedProcess([], 0)
            self.clock.poll(at(5, 30))
            self.clock.poll(at(6, 0))
            self.clock.poll(at(6, 30))
        self.assertEqual(run.call_count, 5)
        self.assertEqual(len(self.clock.state["_windows"]["crawler"]), 2)

    def test_legacy_env_only_preserves_plain_state_shape_without_extended_history(self):
        legacy_env = {"TZ": "America/New_York", "CRAWLER_MINUTE": "0", "MORNING_PUSH_TIME": "01:30"}
        clock = scheduler.Scheduler(lambda: load_runtime_config(legacy_env), state={})
        with patch.object(scheduler.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            for hour, minute in ((5, 0), (5, 0), (5, 30), (5, 30)):
                clock.poll(datetime(2026, 11, 1, hour, minute, tzinfo=timezone.utc))
        self.assertEqual(run.call_count, 2)
        self.assertEqual(clock.state, {"crawler": "2026-11-01T01:30"})

    def test_switching_to_legacy_mode_does_not_write_stale_external_history(self):
        state = {"crawler": "2026-09-20T12:00Z", "_windows": {"crawler": [
            {"utc": "2026-09-20T12:00Z", "local": "2026-09-20T12:00", "timezone": "UTC"}]}}
        clock = scheduler.Scheduler(lambda: load_runtime_config({"TZ": "UTC"}), state=state)
        with patch.object(scheduler.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
            clock.poll(self.at(13, 0))
        self.assertEqual(clock.state, {"crawler": "2026-09-20T13:00"})
        scheduler._validate_state(clock.state)

    def test_window_history_is_bounded_and_recent_windows_still_deduplicate(self):
        self.put({"WEEKLY_HOUR": "23", "WEEKLY_MINUTE": "59"})
        start = self.at(12, 0)
        self.clock.poll(start - timedelta(minutes=1))
        with patch.object(scheduler, "MAX_COMPLETED_WINDOWS", 4):
            with patch.object(scheduler.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
                for hour in range(8):
                    self.clock.poll(start + timedelta(hours=hour))
                self.clock.poll(start + timedelta(hours=7, seconds=20))
        self.assertEqual(run.call_count, 8)
        self.assertEqual(len(self.clock.state["_windows"]["crawler"]), 4)

    def test_legacy_utc_and_local_state_markers_remain_compatible(self):
        self.put({"TZ": "America/New_York", "CRAWLER_MINUTE": "30", "WEEKLY_HOUR": "12"})
        for marker in ("2026-11-01T05:30Z", "2026-11-01T01:30"):
            with self.subTest(marker=marker):
                clock = scheduler.Scheduler(lambda: load_runtime_config(self.base), state={"crawler": marker})
                with patch.object(scheduler.subprocess, "run") as run:
                    clock.poll(datetime(2026, 11, 1, 6, 29, tzinfo=timezone.utc))
                    clock.poll(datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc))
                run.assert_not_called()

    def test_migration_retains_old_marker_when_a_second_window_completes(self):
        self.put({"TZ": "America/New_York", "MORNING_PUSH_TIME": "01:30", "WEEKLY_HOUR": "12"})
        at = lambda h, m: datetime(2026, 11, 1, h, m, tzinfo=timezone.utc)
        for marker in ("2026-11-01T05:00Z", "2026-11-01T01:00"):
            with self.subTest(marker=marker):
                clock = scheduler.Scheduler(lambda: load_runtime_config(self.base), state={"crawler": marker})
                with patch.object(scheduler.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
                    clock.poll(at(5, 29))
                    clock.poll(at(5, 30))
                    clock.poll(at(6, 0))
                    clock.poll(at(6, 30))
                self.assertEqual(run.call_count, 1)
                self.assertEqual(len(clock.state["_windows"]["crawler"]), 2)
                scheduler._validate_state(clock.state)

    def test_corrupt_state_is_fixed_diagnostic_and_startup_fails_closed(self):
        payloads = ["null", "[]", '{"crawler": null}', '{"crawler": []}',
                    '{"crawler": 17}', '{"crawler": "SECRET_INVALID"}',
                    '{"crawler": "2026-02-30T12:00Z"}', '{"crawler": "2026-9-20T12:00Z"}',
                    '{"_windows": null}', '{"_windows": {"crawler": [null]}}',
                    '{"_windows": {"crawler": [{"utc": "2026-09-20T12:00Z"}]}}',
                    '{"crawler": "2026-09-20T12:00Z", "crawler": null}', "{broken"]
        state_path = Path(self.temp.name) / "state.json"
        for payload in payloads:
            with self.subTest(payload=payload):
                state_path.write_text(payload)
                with patch.object(scheduler, "STATE_PATH", state_path), patch.object(scheduler, "STOP", False):
                    with patch.object(scheduler.subprocess, "run") as run, patch.object(
                            scheduler.time, "sleep", side_effect=AssertionError("must not run scheduler loop")):
                        with redirect_stdout(io.StringIO()) as output, patch("sys.stderr", new=io.StringIO()) as errors:
                            self.assertEqual(scheduler.main(self.base), 2)
                    run.assert_not_called()
                self.assertIn("state", output.getvalue() + errors.getvalue())
                self.assertNotIn("SECRET_INVALID", output.getvalue() + errors.getvalue())
                self.assertEqual(state_path.read_text(), payload)  # Never silently replace damaged evidence.


    def test_state_history_format_mismatch_and_bad_types_fail_closed(self):
        window = {"utc": "2026-09-20T12:00Z", "local": "2026-09-20T12:00", "timezone": "UTC"}
        for changes in ({"utc": None}, {"utc": "2026-09-20T12:00"}, {"local": []},
                        {"local": "2026-09-20T13:00"}, {"timezone": None},
                        {"timezone": "invalid-synthetic-zone"}, {"secret": "***"}):
            with self.subTest(changes=changes):
                state = {"crawler": window["utc"], "_windows": {"crawler": [dict(window, **changes)]}}
                with self.assertRaises(scheduler.SchedulerStateError):
                    scheduler.Scheduler(lambda: load_runtime_config(self.base), state=state)
        for state in (
            {"crawler": window["utc"], "_windows": {"crawler": [window, window]}},
            {"crawler": "2026-09-20T13:00Z", "_windows": {"crawler": [window]}},
            {"_windows": {"crawler": [window]}},
        ):
            with self.assertRaises(scheduler.SchedulerStateError):
                scheduler.Scheduler(lambda: load_runtime_config(self.base), state=state)

    def test_state_reader_missing_is_new_but_unreadable_or_oversized_is_not_empty(self):
        state_path = Path(self.temp.name) / "state.json"
        with patch.object(scheduler, "STATE_PATH", state_path):
            self.assertEqual(scheduler._load_state(), {})
            for content in (b"\xff", b"{" + b" " * scheduler.MAX_STATE_BYTES):
                state_path.write_bytes(content)
                with self.assertRaises(scheduler.SchedulerStateError):
                    scheduler._load_state()
                self.assertEqual(state_path.read_bytes(), content)
            with patch.object(scheduler.os, "open", side_effect=PermissionError("SECRET_IO")):
                with self.assertRaises(scheduler.SchedulerStateError) as error:
                    scheduler._load_state()
                self.assertNotIn("SECRET_IO", str(error.exception))

    def test_old_nonprivate_regular_state_is_still_accepted(self):
        state_path = Path(self.temp.name) / "old-state.json"
        old = {"crawler": "2026-09-20T12:00", "weekly": "2026-09-20T12:30Z"}
        state_path.write_text(json.dumps(old))
        state_path.chmod(0o644)
        with patch.object(scheduler, "STATE_PATH", state_path):
            self.assertEqual(scheduler._load_state(), old)

    def test_state_links_are_not_treated_as_missing_or_usable_state(self):
        state_path = Path(self.temp.name) / "linked-state.json"
        target = Path(self.temp.name) / "real-state.json"
        for existing in (False, True):
            with self.subTest(existing=existing):
                if existing:
                    target.write_text('{}')
                state_path.symlink_to(target)
                try:
                    with patch.object(scheduler, "STATE_PATH", state_path):
                        with self.assertRaises(scheduler.SchedulerStateError):
                            scheduler._load_state()
                finally:
                    state_path.unlink()
        directory_link = Path(self.temp.name) / "linked-parent"
        directory_link.symlink_to(self.temp.name, target_is_directory=True)
        with patch.object(scheduler, "STATE_PATH", directory_link / "real-state.json"):
            with self.assertRaises(scheduler.SchedulerStateError):
                scheduler._load_state()

    def test_fifo_state_is_rejected_promptly_without_a_writer(self):
        state_path = Path(self.temp.name) / "fifo-state"
        os.mkfifo(state_path, 0o600)

        def deadline(_signum, _frame):
            raise AssertionError("state reader blocked on FIFO")

        previous = signal.signal(signal.SIGALRM, deadline)
        signal.setitimer(signal.ITIMER_REAL, 0.5)
        try:
            with patch.object(scheduler, "STATE_PATH", state_path):
                with self.assertRaises(scheduler.SchedulerStateError):
                    scheduler._load_state()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)

    def test_real_atomic_state_roundtrip_persists_all_windows_and_partial(self):
        self.save_patch.stop()
        state_path = Path(self.temp.name) / "meta/state.json"
        self.clock.poll(self.at(11, 59))

        def child(command, **kwargs):
            return subprocess.CompletedProcess(command, 6 if command[1].endswith(".py") else 0)

        with patch.object(scheduler, "STATE_PATH", state_path):
            with patch.object(scheduler.subprocess, "run", side_effect=child) as run:
                self.clock.poll(self.at(12, 0))
                self.clock.poll(self.at(13, 0))
                self.assertEqual(scheduler._load_state(), self.clock.state)
                restarted = scheduler.Scheduler(lambda: load_runtime_config(self.base))
                restarted.poll(self.at(12, 59))
                restarted.poll(self.at(13, 0))
        self.assertEqual(run.call_count, 3)
        self.assertEqual(state_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(sorted(p.name for p in state_path.parent.iterdir()), ["state.json"])

    def test_state_save_failure_is_fail_closed_before_next_child(self):
        self.clock.poll(self.at(11, 59))
        self.save_patch.stop()
        with patch.object(scheduler.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            with patch.object(scheduler, "atomic_write", side_effect=OSError("SECRET_WRITE")):
                with self.assertRaises(scheduler.SchedulerStateError) as error:
                    self.clock.poll(self.at(12, 0))
        self.assertNotIn("SECRET_WRITE", str(error.exception))
        self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
