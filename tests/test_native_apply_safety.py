"""Catch-up and atomic-replace regressions; systemctl is always a fake.

Only the explicitly marked parser integration tests execute systemd-analyze
calendar, a read-only calculation which never contacts the systemd manager.
"""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))
import envfile
import native_config
from envfile import read_env, write_env
from native_config import ApplyError, NativeApplication, UnitTransaction
from native_schedule import DEFAULT_SCHEDULE, NativeSchedule

NOW = datetime(2026, 9, 24, 10, 30, tzinfo=timezone.utc).timestamp()
LAST = "Thu 2026-09-24 10:05:00 UTC"
FUTURE = "Thu 2026-09-24 11:05:00 UTC"


class SafetyRunner:
    def __init__(self, units, *, real_calendar=False):
        self.units = units
        self.real_calendar = real_calendar
        self.calls = []
        self.options = []
        self.states = {"trendradar-lite.timer": ("active", "enabled"),
                       "trendradar-weekly.timer": ("inactive", "disabled")}
        self.last = LAST
        self.substate = "waiting"
        self.dirty = "no"
        self.dropins = ""
        self.calendar_error = None
        self.calendar_output = None
        self.show_hook = None
        self.reload_hook = None
        self.show_count = 0
        self.reload_count = 0
        self.fail_reload = 0
        self.calendar_hook = None
        self.override_next = {}

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        self.options.append(kwargs)
        if command[:2] == ["systemd-analyze", "calendar"]:
            if self.calendar_hook:
                self.calendar_hook()
            if self.calendar_error:
                raise self.calendar_error
            if self.calendar_output is not None:
                return subprocess.CompletedProcess(command, *self.calendar_output)
            if self.real_calendar:
                # This is the ONLY real child process this runner may execute.
                return subprocess.run(command, **kwargs)
            base = command[4].removeprefix("--base-time=")
            output = []
            for calendar in command[5:]:
                value = self.override_next.get(calendar, FUTURE)
                if isinstance(value, tuple):
                    value = value[1 if base.startswith("@") else 0]
                output.append("Normalized form: " + calendar + "\n    Next elapse: " + value)
            return subprocess.CompletedProcess(command, 0, "\n\n".join(output), "")
        assert command[:2] == ["systemctl", "--user"], command
        action = command[2]
        assert action in {"show", "daemon-reload"}, command
        if action == "daemon-reload":
            self.reload_count += 1
            if self.reload_hook:
                self.reload_hook()
            if self.fail_reload:
                self.fail_reload -= 1
                return subprocess.CompletedProcess(command, 1, "", "private diagnostics")
            return subprocess.CompletedProcess(command, 0, "", "")
        self.show_count += 1
        name = command[3]
        if self.show_hook:
            self.show_hook(name)
        active, enabled = self.states[name]
        output = (f"ActiveState={active}\nUnitFileState={enabled}\nFragmentPath={self.units / name}\n"
                  f"DropInPaths={self.dropins}\nLastTriggerUSec={self.last}\n"
                  f"SubState={self.substate}\nNeedDaemonReload={self.dirty}\n")
        return subprocess.CompletedProcess(command, 0, output, "")


class NativeApplySafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app_dir = self.root / "app"
        shutil.copytree(ROOT / "config", self.app_dir / "config")
        self.units = self.root / "units"
        self.units.mkdir()
        self.env = self.root / "env"
        self.values = {**DEFAULT_SCHEDULE, "TZ": "UTC", "AI_MODEL": "original/model"}
        write_env(self.env, self.values)
        for path, data in NativeSchedule(self.app_dir, self.units, self.values).prepare(self.values, normalize=True).items():
            path.write_bytes(data)
        self.before = {path: path.read_bytes() for path in (self.env, *self.units.glob("*.timer"))}
        self.runner = SafetyRunner(self.units)
        self.clock = mock.patch.object(native_config.time, "time", return_value=NOW).start()
        self.addCleanup(mock.patch.stopall)
        self.application = NativeApplication(self.env, self.app_dir, self.units, runner=self.runner)
        self.daily = self.units / "trendradar-lite.timer"
        self.weekly = self.units / "trendradar-weekly.timer"

    def save(self, **updates):
        return self.application.save({**self.values, "CRAWLER_MINUTE": "40", **updates})

    def assert_original(self):
        for path, data in self.before.items():
            self.assertEqual(path.read_bytes(), data, path)

    def assert_no_mutations(self):
        self.assert_original()
        self.assertEqual(self.runner.reload_count, 0)
        self.assertFalse(list(self.root.rglob(".*.backups")))

    def test_overdue_new_calendar_rejected_before_env_save_backup_or_reload(self):
        self.runner.override_next["*-*-* *:20:00 UTC"] = (
            "Thu 2026-09-24 10:20:00 UTC", "Thu 2026-09-24 11:20:00 UTC")
        with mock.patch.object(self.application.document, "save", wraps=self.application.document.save) as saving:
            with self.assertRaisesRegex(ApplyError, "daemon-reload.*补跑") as raised:
                self.save(CRAWLER_MINUTE="20")
            saving.assert_not_called()
        self.assertIn("下一次正常触发", str(raised.exception))
        self.assert_no_mutations()

    def test_overdue_old_calendar_rejected_even_when_new_is_future(self):
        self.runner.override_next["*-*-* *:05:00 UTC"] = (
            "Thu 2026-09-24 10:05:00 UTC", "Thu 2026-09-24 11:05:00 UTC")
        with self.assertRaises(ApplyError):
            self.save()
        self.assert_no_mutations()

    def test_future_active_change_allowed_without_start_restart_stop_enable(self):
        result = self.save()
        self.assertEqual(read_env(self.env)["CRAWLER_MINUTE"], "40")
        self.assertIn(b"*:40:00 UTC", self.daily.read_bytes())
        self.assertEqual(self.runner.reload_count, 1)
        self.assertIn("未手动启动服务", result)
        self.assertIn("原定时任务仍可正常运行", result)
        self.assertTrue(all(command[2] in {"show", "daemon-reload"}
                            for command in self.runner.calls if command[0] == "systemctl"))
        self.assertEqual(sum(command[0] == "systemd-analyze" for command in self.runner.calls), 4)
        for command, options in zip(self.runner.calls, self.runner.options):
            self.assertEqual(options["env"]["LC_ALL"], "C")
            self.assertEqual(options["timeout"], native_config.COMMAND_TIMEOUT)
            if command[0] == "systemctl":
                self.assertEqual(options["env"]["TZ"], "UTC")

    def test_active_but_disabled_is_still_checked(self):
        self.runner.states[self.daily.name] = ("active", "disabled")
        self.runner.last = "n/a"
        with self.assertRaises(ApplyError):
            self.save()
        self.assert_no_mutations()

    def test_unknown_invalid_zero_or_future_last_trigger_fail_closed(self):
        for last in ("", "n/a", "0", "private bad timestamp", "Thu 1970-01-01 00:00:00 UTC",
                     "Thu 2026-09-24 10:31:00 UTC", "Thu 2026-02-30 10:05:00 UTC",
                     "Thu 2026-09-24 10:05:00 CST"):
            with self.subTest(last=last):
                self.runner.last = last
                with self.assertRaises(ApplyError) as raised:
                    self.save()
                self.assertNotIn("private bad timestamp", str(raised.exception))
                self.assert_no_mutations()

    def test_calendar_failed_unknown_never_or_incomplete_output_fail_closed(self):
        for output in ((1, "", "private diagnostics"), (0, "Next elapse: never", ""),
                       (0, "Next elapse: not a timestamp", ""), (0, "", ""),
                       (0, "Next elapse: " + FUTURE, "")):
            with self.subTest(output=output):
                self.runner.calendar_output = output
                with self.assertRaises(ApplyError) as raised:
                    self.save()
                self.assertNotIn("private diagnostics", str(raised.exception))
                self.assert_no_mutations()

    def test_calendar_unavailable_or_timed_out_fails_closed(self):
        for error in (FileNotFoundError(), subprocess.TimeoutExpired("systemd-analyze", 10)):
            with self.subTest(error=error):
                self.runner.calendar_error = error
                with self.assertRaises(ApplyError):
                    self.save()
                self.assert_no_mutations()

    def test_inactive_enabled_and_disabled_remain_editable_without_calendar(self):
        for enabled in ("enabled", "disabled"):
            with self.subTest(enabled=enabled):
                self.runner.states[self.daily.name] = ("inactive", enabled)
                self.runner.last = "n/a"
                self.runner.calendar_error = AssertionError("Inactive timer needs no calculation")
                self.application = NativeApplication(self.env, self.app_dir, self.units, runner=self.runner)
                self.save(CRAWLER_MINUTE="40" if enabled == "enabled" else "42")
        self.assertEqual(self.runner.reload_count, 2)
        self.assertFalse(any(command[0] == "systemd-analyze" for command in self.runner.calls))

    def test_fresh_timer_state_can_change_from_not_found_to_disabled_without_activation(self):
        data = self.daily.read_bytes()
        self.daily.unlink()
        def state_from_disk(name):
            self.runner.states[name] = ("inactive", "disabled" if (self.units / name).exists() else "not-found")
        self.runner.show_hook = state_from_disk
        transaction = UnitTransaction({self.daily: data}, self.runner)
        transaction.commit()
        self.assertEqual(self.runner.reload_count, 1)
        self.assertEqual(self.runner.states[self.daily.name], ("inactive", "disabled"))

    def test_systemctl_show_failure_or_timeout_is_readonly(self):
        for error in (OSError(), subprocess.TimeoutExpired("systemctl", 10)):
            with self.subTest(error=error):
                def unavailable(command, **kwargs):
                    self.assertEqual(kwargs["timeout"], native_config.COMMAND_TIMEOUT)
                    raise error
                self.application.runner = unavailable
                with self.assertRaisesRegex(ApplyError, "systemd.*失败或超时"):
                    self.save()
                self.assert_no_mutations()

    def test_timed_out_reload_is_treated_as_possibly_committed(self):
        def timeout_once(command, **kwargs):
            result = self.runner(command, **kwargs)
            if command[2] == "daemon-reload" and self.runner.reload_count == 1:
                raise subprocess.TimeoutExpired("systemctl", 10)
            return result
        self.application.runner = timeout_once
        with self.assertRaisesRegex(ApplyError, "env 已恢复.*失败或超时"):
            self.save()
        self.assert_original()
        self.assertEqual(self.runner.reload_count, 2)

    def test_unchanged_active_partner_checked_because_reload_coldplugs_both(self):
        self.runner.states[self.daily.name] = ("inactive", "disabled")
        self.runner.states[self.weekly.name] = ("active", "enabled")
        self.runner.last = "n/a"
        with self.assertRaises(ApplyError):
            self.save()
        self.assert_no_mutations()

    def test_active_zone_less_legacy_calendar_is_indeterminate_but_inactive_normalization_works(self):
        for calendar in ("hourly", "*-*-* *:05:00"):
            with self.subTest(calendar=calendar):
                self.daily.write_text("[Timer]\nOnCalendar=" + calendar + "\nPersistent=true\n")
                self.runner.states[self.daily.name] = ("active", "enabled")
                self.application = NativeApplication(self.env, self.app_dir, self.units, runner=self.runner)
                old_env, old_timer = self.env.read_bytes(), self.daily.read_bytes()
                with self.assertRaisesRegex(ApplyError, "无显式时区"):
                    self.application.save(self.values, normalize=True)
                self.assertEqual(self.env.read_bytes(), old_env)
                self.assertEqual(self.daily.read_bytes(), old_timer)
                self.assertEqual(self.runner.reload_count, 0 if calendar == "hourly" else 1)
                self.runner.states[self.daily.name] = ("inactive", "disabled")
                self.application.save(self.values, normalize=True)
                self.assertIn(b" UTC", self.daily.read_bytes())
        self.assertEqual(self.runner.reload_count, 2)

    def test_running_elapsed_or_dirty_active_timer_rejected(self):
        for substate, dirty in (("running", "no"), ("elapsed", "no"), ("", "no"),
                                ("waiting", "yes"), ("waiting", "")):
            with self.subTest(substate=substate, dirty=dirty):
                self.runner.substate, self.runner.dirty = substate, dirty
                with self.assertRaises(ApplyError):
                    self.save()
                self.assert_no_mutations()

    def test_safety_horizon_rejects_nominal_occurrence_at_or_before_deadline(self):
        for when in ("10:30:00", "10:31:59", "10:32:00"):
            with self.subTest(when=when):
                self.runner.override_next["*-*-* *:40:00 UTC"] = f"Thu 2026-09-24 {when} UTC"
                with self.assertRaises(ApplyError):
                    self.save()
                self.assert_no_mutations()

    def test_slow_preflight_and_backwards_clock_do_not_consume_guard_band(self):
        for changed_clock in (NOW + 35 * 60, NOW - 1):
            with self.subTest(changed_clock=changed_clock):
                self.clock.return_value = NOW
                self.runner.calendar_hook = lambda: setattr(self.clock, "return_value", changed_clock)
                with self.assertRaises(ApplyError):
                    self.save()
                self.assert_no_mutations()

    def test_repeat_preflight_before_reload_rolls_back_if_state_becomes_unknown(self):
        def state_changes(name):
            if self.runner.show_count >= 3:
                self.runner.last = "n/a"
        self.runner.show_hook = state_changes
        with self.assertRaisesRegex(ApplyError, "env 已恢复"):
            self.save()
        self.assert_original()
        self.assertEqual(self.runner.reload_count, 0)

    def test_repeat_preflight_checks_active_state_and_dropins_again(self):
        for change in (lambda: self.runner.states.update({self.daily.name: ("inactive", "disabled")}),
                       lambda: setattr(self.runner, "dropins", "/external/override.conf")):
            with self.subTest(change=change):
                self.runner.states[self.daily.name] = ("active", "enabled")
                self.runner.dropins = ""
                self.runner.show_count = 0
                self.application = NativeApplication(self.env, self.app_dir, self.units, runner=self.runner)
                self.runner.show_hook = lambda name: change() if self.runner.show_count == 3 else None
                with self.assertRaisesRegex(ApplyError, "env 已恢复"):
                    self.save()
                self.assert_original()
                self.assertEqual(self.runner.reload_count, 0)

    def test_safety_window_cannot_expire_during_final_disk_check(self):
        original = UnitTransaction.check_written
        def delayed_check(transaction):
            original(transaction)
            self.clock.return_value = NOW + 35 * 60
        with mock.patch.object(UnitTransaction, "check_written", delayed_check):
            with self.assertRaisesRegex(ApplyError, "env 已恢复.*安全校验已过期"):
                self.save()
        self.assert_original()
        self.assertEqual(self.runner.reload_count, 0)

    def test_failed_reload_safe_rollback_reloads_old_files_and_restores_env(self):
        self.runner.fail_reload = 1
        with self.assertRaisesRegex(ApplyError, "env 已恢复") as raised:
            self.save()
        self.assertNotIn("回滚不完整", str(raised.exception))
        self.assertNotIn("private diagnostics", str(raised.exception))
        self.assert_original()
        self.assertEqual(self.runner.reload_count, 2)

    def test_risky_rollback_does_not_reload_and_reports_disk_vs_runtime(self):
        self.runner.fail_reload = 1
        self.runner.reload_hook = lambda: setattr(self.runner, "last", "n/a")
        with self.assertRaisesRegex(ApplyError, "env 已恢复.*回滚不完整.*unit 文件已恢复.*运行时恢复未确认"):
            self.save()
        self.assert_original()
        self.assertEqual(self.runner.reload_count, 1)

    def test_failed_second_reload_reports_incomplete_runtime_but_restored_disk(self):
        self.runner.fail_reload = 2
        with self.assertRaisesRegex(ApplyError, "env 已恢复.*回滚不完整.*运行时恢复未确认"):
            self.save()
        self.assert_original()
        self.assertEqual(self.runner.reload_count, 2)

    def test_concurrent_unit_edit_is_not_clobbered_or_reloaded_during_rollback(self):
        self.runner.fail_reload = 1
        self.runner.reload_hook = lambda: self.daily.write_bytes(b"external changes\n")
        with self.assertRaisesRegex(ApplyError, "env 已恢复.*回滚不完整.*未再次重载"):
            self.save()
        self.assertEqual(self.daily.read_bytes(), b"external changes\n")
        self.assertEqual(self.env.read_bytes(), self.before[self.env])
        self.assertEqual(self.runner.reload_count, 1)

    def test_first_or_second_unit_directory_fsync_failure_restores_all_files(self):
        for target in (self.daily, self.weekly):
            with self.subTest(target=target.name):
                self.application = NativeApplication(self.env, self.app_dir, self.units, runner=self.runner)
                original_fsync = os.fsync
                failed = []
                def fsync(fd):
                    if not failed and stat.S_ISDIR(os.fstat(fd).st_mode) and target.read_bytes() != self.before[target]:
                        failed.append(target)
                        raise OSError("directory fsync after successful replace")
                    return original_fsync(fd)
                with mock.patch.object(envfile.os, "fsync", side_effect=fsync):
                    with self.assertRaisesRegex(ApplyError, "env 已恢复"):
                        self.save(WEEKLY_MINUTE="41")
                self.assertEqual(failed, [target])
                self.assert_original()
                self.assertEqual(self.runner.reload_count, 0)

    def test_first_or_second_unit_failure_before_replace_skips_unchanged_file(self):
        for target in (self.daily, self.weekly):
            with self.subTest(target=target.name):
                self.application = NativeApplication(self.env, self.app_dir, self.units, runner=self.runner)
                original_write = native_config.atomic_write
                def failing_write(path, data, mode):
                    if path == target and data != self.before[path]:
                        raise OSError("before replace")
                    original_write(path, data, mode)
                with mock.patch.object(native_config, "atomic_write", side_effect=failing_write):
                    with self.assertRaisesRegex(ApplyError, "env 已恢复") as raised:
                        self.save(WEEKLY_MINUTE="41")
                self.assertNotIn("回滚不完整", str(raised.exception))
                self.assert_original()
                self.assertEqual(self.runner.reload_count, 0)

    def test_concurrent_change_after_failed_replace_is_not_overwritten(self):
        original_write = native_config.atomic_write
        def failing_write(path, data, mode):
            original_write(path, data, mode)
            if path == self.daily:
                path.write_bytes(b"concurrent writer\n")
                raise OSError("failed after replace and concurrent change")
        with mock.patch.object(native_config, "atomic_write", side_effect=failing_write):
            with self.assertRaisesRegex(ApplyError, "env 已恢复.*回滚不完整"):
                self.save()
        self.assertEqual(self.daily.read_bytes(), b"concurrent writer\n")
        self.assertEqual(self.env.read_bytes(), self.before[self.env])
        self.assertEqual(self.runner.reload_count, 0)

    def test_new_file_failed_after_replace_is_removed_on_rollback(self):
        path = self.units / "new.service"
        original_write = native_config.atomic_write
        def fail(path, data, mode):
            original_write(path, data, mode)
            raise OSError("after replace")
        transaction = UnitTransaction({path: b"new bytes"}, self.runner)
        with mock.patch.object(native_config, "atomic_write", side_effect=fail):
            with self.assertRaises(OSError):
                transaction.commit()
        self.assertFalse(path.exists())
        self.assertEqual(self.runner.reload_count, 0)

    def test_unit_restore_failed_after_replace_reports_bytes_and_skips_reload(self):
        self.runner.fail_reload = 1
        original_write = native_config.atomic_write
        def fail(path, data, mode):
            original_write(path, data, mode)
            if path == self.daily and data == self.before[self.daily]:
                raise OSError("restored unit but fsync failed")
        with mock.patch.object(native_config, "atomic_write", side_effect=fail):
            with self.assertRaisesRegex(ApplyError, "env 已恢复.*unit 内容已恢复，但持久化确认失败.*未再次重载"):
                self.save()
        self.assert_original()
        self.assertEqual(self.runner.reload_count, 1)

    def test_env_save_failed_after_replace_is_detected_and_restored(self):
        original_write = envfile.atomic_write
        failed = []
        def fail(path, data, mode=0o600):
            original_write(path, data, mode)
            if path == self.env and not failed:
                failed.append(True)
                raise OSError("env after replace")
        with mock.patch.object(envfile, "atomic_write", side_effect=fail):
            with self.assertRaisesRegex(ApplyError, "env 已恢复"):
                self.save()
        self.assert_original()
        self.assertEqual(self.runner.reload_count, 0)

    def test_env_restore_failed_after_replace_reports_bytes_restored_durability_uncertain(self):
        self.runner.fail_reload = 1
        original_write = envfile.atomic_write
        def fail(path, data, mode=0o600):
            original_write(path, data, mode)
            if path == self.env and data == self.before[self.env]:
                raise OSError("env restored but fsync failed")
        with mock.patch.object(envfile, "atomic_write", side_effect=fail):
            with self.assertRaisesRegex(ApplyError, "env 内容已恢复，但持久化确认失败"):
                self.save()
        self.assert_original()

    def test_concurrent_env_change_blocks_restore_without_hiding_unit_outcome(self):
        self.runner.fail_reload = 1
        self.runner.reload_hook = lambda: self.env.write_bytes(b"EXTERNAL=writer\n")
        with self.assertRaisesRegex(ApplyError, "env 回滚失败"):
            self.save()
        self.assertEqual(self.env.read_bytes(), b"EXTERNAL=writer\n")
        self.assertEqual(self.daily.read_bytes(), self.before[self.daily])

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze is not installed")
    def test_real_readonly_calendar_parser_refuses_1005_to_1020_at_1030(self):
        self.runner.real_calendar = True
        with self.assertRaisesRegex(ApplyError, "已过期或临近"):
            self.save(CRAWLER_MINUTE="20")
        self.assert_no_mutations()

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze is not installed")
    def test_real_readonly_calendar_parser_accepts_future_and_uses_explicit_timezone(self):
        self.runner.real_calendar = True
        self.save()
        self.assertEqual(self.runner.reload_count, 1)
        # OnCalendar zones use systemd's calculation, not Python scheduling.
        transaction = UnitTransaction({}, self.runner)
        with mock.patch.dict(os.environ, {"TZ": "Pacific/Honolulu", "LC_ALL": "invalid-locale"}):
            next_time = transaction.calendar_next(["*-*-* *:40:00 Asia/Kolkata"], LAST)
        self.assertEqual(next_time, [datetime(2026, 9, 24, 10, 10, tzinfo=timezone.utc).timestamp()])
        self.assertNotIn("TZ", self.runner.options[-1]["env"])

    def test_non_utc_analyzer_output_uses_utc_row(self):
        self.runner.calendar_output = (0, "Next elapse: Thu 2026-09-24 19:05:00 CST\n"
                                      "(in UTC): Thu 2026-09-24 11:05:00 UTC\nFrom now: 35min left", "")
        transaction = UnitTransaction({}, self.runner)
        result = transaction.calendar_next(["hourly"], LAST)
        self.assertEqual(result, [NOW + 35 * 60])


if __name__ == "__main__":
    unittest.main()
