"""Delivery admission windows remain distinct from exact launch times."""
import copy
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
import unittest
from unittest.mock import patch

import yaml

from deploy import configure, native_schedule
from deploy.docker import runtime_config
from trendradar.core.scheduler import Scheduler
from tests.test_scheduler import MemoryExecutionStorage


class DeliveryWindowTests(unittest.TestCase):
    def setUp(self):
        self.timeline = yaml.safe_load(Path("config/timeline.yaml").read_text())
        self.storage = MemoryExecutionStorage()

    def make_scheduler(self, clock):
        return Scheduler({"enabled": True, "preset": "custom"}, self.timeline,
                         self.storage, lambda: clock[0])

    def test_all_overrides_start_at_requested_minute_and_end_at_hour_boundary(self):
        slots = (("MORNING_PUSH_TIME", "morning_brief", 7), ("NOON_PUSH_TIME", "noon_brief", 12),
                 ("EVENING_PUSH_TIME", "afternoon_brief", 18), ("DAILY_SUMMARY_TIME", "nightly_daily", 22))
        original = copy.deepcopy(self.timeline)
        for key, period, hour in slots:
            for minute in (0, 10, 30, 59):
                with self.subTest(key=key, minute=minute), patch.dict(os.environ, {key: f"{hour:02}:{minute:02}"}, clear=True):
                    start = datetime(2026, 10, 1, hour, minute)
                    clock = [start]
                    scheduler = self.make_scheduler(clock)
                    for instant in (start, start.replace(second=59), start.replace(minute=59, second=59)):
                        clock[0] = instant
                        result = scheduler.resolve()
                        self.assertEqual(result.period_key, period)
                        self.assertTrue(result.analyze and result.push)
                        self.assertTrue(result.once_analyze and result.once_push)
                    for instant in (start - timedelta(seconds=1), start.replace(minute=59) + timedelta(minutes=1)):
                        clock[0] = instant
                        result = scheduler.resolve()
                        self.assertNotEqual(result.period_key, period)
                        self.assertFalse(result.push)
        self.assertEqual(self.timeline, original)

    def test_custom_window_and_independent_once_records_survive(self):
        self.timeline["custom"]["periods"]["morning_brief"].update(start="06:40", end="08:20")
        clock = [datetime(2026, 10, 1, 8, 10)]
        with patch.dict(os.environ, {"NOON_PUSH_TIME": "12:30"}, clear=True):
            scheduler = self.make_scheduler(clock)
        result = scheduler.resolve()
        self.assertEqual(result.period_key, "morning_brief")
        scheduler.record_execution(result.period_key, "analyze", "2026-10-01")
        self.assertTrue(scheduler.already_executed(result.period_key, "analyze", "2026-10-01"))
        self.assertFalse(scheduler.already_executed(result.period_key, "push", "2026-10-01"))
        scheduler.record_execution(result.period_key, "push", "2026-10-01")
        self.assertTrue(scheduler.already_executed(result.period_key, "push", "2026-10-01"))
        self.assertFalse(scheduler.already_executed(result.period_key, "push", "2026-10-02"))

    def test_no_override_or_empty_overrides_preserve_yaml_exactly(self):
        for env in ({}, {key: "" for key in runtime_config.TIME_KEYS}):
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True):
                scheduler = self.make_scheduler([datetime(2026, 10, 1, 7, 5)])
                self.assertEqual(scheduler.timeline, self.timeline["custom"])
                self.assertEqual(scheduler.resolve().period_key, "morning_brief")

    def test_trigger_minute_opens_window_without_early_delivery(self):
        clock = [datetime(2026, 10, 1, 7, 5)]
        with patch.dict(os.environ, {"MORNING_PUSH_TIME": "07:30"}, clear=True):
            scheduler = self.make_scheduler(clock)
        self.assertFalse(scheduler.resolve().push)
        clock[0] = datetime(2026, 10, 1, 7, 30)
        result = scheduler.resolve()
        self.assertEqual(result.period_key, "morning_brief")
        self.assertTrue(result.push)
        scheduler.record_execution(result.period_key, "push", "2026-10-01")
        clock[0] = datetime(2026, 10, 1, 7, 59, 59)
        self.assertEqual(scheduler.resolve().period_key, result.period_key)
        self.assertTrue(scheduler.already_executed(result.period_key, "push", "2026-10-01"))
        self.assertFalse(scheduler.already_executed(result.period_key, "analyze", "2026-10-01"))
        clock[0] = datetime(2026, 10, 1, 8, 0)
        self.assertFalse(scheduler.resolve().push)

    def test_same_hour_rejected_at_any_clock_with_full_or_partial_overrides(self):
        original = copy.deepcopy(self.timeline)
        for env in (
            {**dict(zip(runtime_config.TIME_KEYS, runtime_config.DEFAULT_TIMES)), "NOON_PUSH_TIME": "07:30"},
            {"NOON_PUSH_TIME": "07:30"},
            {"MORNING_PUSH_TIME": "12:59"},
            {"NOON_PUSH_TIME": "07:00"},
        ):
            for hour in (0, 7, 12, 23):
                with self.subTest(env=env, hour=hour), patch.dict(os.environ, env, clear=True):
                    with self.assertRaisesRegex(ValueError, "存在重叠"):
                        self.make_scheduler([datetime(2026, 10, 1, hour, 15)])
        self.assertEqual(self.timeline, original)

    def test_custom_yaml_overlap_is_not_silently_truncated_or_last_wins(self):
        cases = (
            ("morning_brief", "06:40", "08:20", {"NOON_PUSH_TIME": "08:10"}),
            ("nightly_daily", "22:00", "07:15", {"MORNING_PUSH_TIME": "07:10"}),
            ("noon_brief", "07:20", "07:40", {}),
        )
        for key, start, end, env in cases:
            with self.subTest(key=key, env=env), patch.dict(os.environ, env, clear=True):
                timeline = copy.deepcopy(self.timeline)
                timeline["custom"]["periods"][key].update(start=start, end=end)
                original = copy.deepcopy(timeline)
                with self.assertRaisesRegex(ValueError, "存在重叠"):
                    Scheduler({"enabled": True, "preset": "custom"}, timeline, self.storage,
                              lambda: datetime(2026, 10, 1, 10))
                self.assertEqual(timeline, original)

    def test_partial_override_does_not_change_other_yaml_windows(self):
        original = copy.deepcopy(self.timeline)
        with patch.dict(os.environ, {"MORNING_PUSH_TIME": "08:30"}, clear=True):
            scheduler = self.make_scheduler([datetime(2026, 10, 1, 8, 30)])
        for key in ("noon_brief", "afternoon_brief", "nightly_daily"):
            self.assertEqual(scheduler.timeline["periods"][key], original["custom"]["periods"][key])
        period = scheduler.timeline["periods"]["morning_brief"]
        self.assertEqual((period["start"], period["end"]), ("08:30", "08:59"))
        self.assertEqual(period["once"], {"analyze": True, "push": True})
        self.assertEqual(self.timeline, original)


class DeliveryWindowValidationTests(unittest.TestCase):
    """All deployment inputs reject overlap before saving or starting children."""

    FORM = {"EMAIL_FROM": "sender@example.invalid", "EMAIL_TO": "reader@example.invalid",
            "EMAIL_PASSWORD": "synthetic", "TZ": "Asia/Shanghai"}

    def assert_rejected(self, values):
        original = dict(values)
        for deployment in ("linux", "docker"):
            errors = configure.validate({**self.FORM, **values}, deployment)
            self.assertTrue(any("同一小时" in error for error in errors), errors)
        # Native's low-level API requires a complete schedule; preparation merges
        # missing fields with inspected/default settings before calling _validate.
        native_values = {**native_schedule.DEFAULT_SCHEDULE,
                         **{key: value for key, value in values.items() if value}}
        with self.assertRaisesRegex(native_schedule.ScheduleError, "different hours"):
            native_schedule._validate(native_values)
        for validate in (runtime_config.validate_runtime_values, runtime_config.schedule_settings):
            with self.assertRaises(runtime_config.RuntimeConfigError) as raised:
                validate(values)
            self.assertEqual(raised.exception.code, "delivery_overlap")
        content = "".join(f"{key}={value}\n" for key, value in values.items()).encode()
        with self.assertRaises(runtime_config.RuntimeConfigError) as raised:
            runtime_config.parse_runtime_env(content)
        self.assertEqual(raised.exception.code, "delivery_overlap")
        self.assertEqual(values, original)

    def test_all_slot_pairs_reject_same_hour_regardless_of_minute_or_order(self):
        keys = runtime_config.TIME_KEYS
        for i, first in enumerate(keys):
            for second in keys[i + 1:]:
                for minutes in (("00", "30"), ("59", "00"), ("30", "30")):
                    with self.subTest(first=first, second=second, minutes=minutes):
                        values = dict(zip(keys, runtime_config.DEFAULT_TIMES))
                        values.update({first: f"09:{minutes[0]}", second: f"09:{minutes[1]}"})
                        self.assert_rejected(values)

    def test_partial_overrides_are_checked_against_missing_or_empty_defaults(self):
        for values in (
            {"NOON_PUSH_TIME": "07:30"},
            {"MORNING_PUSH_TIME": "", "NOON_PUSH_TIME": "07:30"},
            {"MORNING_PUSH_TIME": "12:59"},
            {"NOON_PUSH_TIME": "07:00"},
            {"EVENING_PUSH_TIME": "22:10", "DAILY_SUMMARY_TIME": ""},
        ):
            with self.subTest(values=values):
                self.assert_rejected(values)

    def test_defaults_and_distinct_hours_remain_valid_in_all_entrypoints(self):
        for values in (
            {},
            {key: "" for key in runtime_config.TIME_KEYS},
            {"MORNING_PUSH_TIME": "07:30"},
            dict(zip(runtime_config.TIME_KEYS, ("00:59", "01:00", "22:59", "23:00"))),
            dict(zip(runtime_config.TIME_KEYS, ("23:59", "22:30", "01:10", "00:00"))),
        ):
            with self.subTest(values=values):
                for deployment in ("linux", "docker"):
                    self.assertEqual(configure.validate({**self.FORM, **values}, deployment), [])
                native_schedule._validate({**native_schedule.DEFAULT_SCHEDULE,
                                           **{key: value for key, value in values.items() if value}})
                runtime_config.validate_runtime_values(values)
                expected = tuple(values.get(key) or default for key, default
                                 in zip(runtime_config.TIME_KEYS, runtime_config.DEFAULT_TIMES))
                self.assertEqual(runtime_config.schedule_settings(values).push_times, frozenset(expected))
                runtime_config.parse_runtime_env(b"TZ=Asia/Shanghai\n" + "".join(
                    f"{key}={value}\n" for key, value in values.items()).encode())
                with patch.dict(os.environ, values, clear=True):
                    Scheduler({"enabled": True, "preset": "custom"},
                              yaml.safe_load(Path("config/timeline.yaml").read_text()),
                              MemoryExecutionStorage(), lambda: datetime(2026, 10, 1, 10))

    def test_malformed_time_still_reports_format_not_overlap(self):
        for value in ("7:30", "24:00", "07:60", "not-a-time"):
            with self.subTest(value=value):
                values = {"MORNING_PUSH_TIME": value}
                errors = configure.validate({**self.FORM, **values})
                self.assertTrue(any("HH:MM" in error for error in errors))
                with self.assertRaisesRegex(native_schedule.ScheduleError, "HH:MM"):
                    native_schedule._validate({**native_schedule.DEFAULT_SCHEDULE, **values})
                with self.assertRaises(runtime_config.RuntimeConfigError) as raised:
                    runtime_config.validate_runtime_values(values)
                self.assertEqual(raised.exception.code, "schedule")

    def test_same_minute_in_different_hours_remains_valid(self):
        values = dict(zip(runtime_config.TIME_KEYS, ("07:30", "12:30", "18:30", "22:30")))
        self.assertEqual(configure.validate({**self.FORM, **values}), [])
        native_schedule._validate({**native_schedule.DEFAULT_SCHEDULE, **values})
        runtime_config.validate_runtime_values(values)

    def test_legacy_timer_generator_rejects_before_writing_and_defaults_empty_values(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "new" / "daily.timer"
            with self.assertRaisesRegex(ValueError, "different hours"):
                configure.write_systemd_timer(target, {"NOON_PUSH_TIME": "07:30"})
            self.assertFalse(target.parent.exists())
            target.parent.mkdir()
            target.write_text("preserve-existing-timer\n")
            with self.assertRaisesRegex(ValueError, "different hours"):
                configure.write_systemd_timer(target, {"NOON_PUSH_TIME": "07:00"})
            self.assertEqual(target.read_text(), "preserve-existing-timer\n")
            configure.write_systemd_timer(target, {"MORNING_PUSH_TIME": "", "NOON_PUSH_TIME": "12:30"})
            self.assertIn("OnCalendar=*-*-* 07:00:00", target.read_text())
            self.assertIn("OnCalendar=*-*-* 12:30:00", target.read_text())

    def test_native_custom_yaml_resolution_and_unrelated_save_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory) / "app"
            units = Path(directory) / "units"
            units.mkdir()
            (app / "config").mkdir(parents=True)
            for name in ("config.yaml", "timeline.yaml"):
                shutil.copyfile(Path("config") / name, app / "config" / name)
            timeline_path = app / "config/timeline.yaml"
            timeline_path.write_text(timeline_path.read_text().replace('"07:00"', '"08:12"').replace('"07:59"', '"08:45"'))
            original = timeline_path.read_bytes()
            initial = {"NOON_PUSH_TIME": "07:30"}
            schedule = native_schedule.NativeSchedule(app, units, initial)
            self.assertTrue(schedule.supported, schedule.warnings)
            self.assertEqual(schedule.suggested["MORNING_PUSH_TIME"], "08:12")
            self.assertEqual(configure.validate({**self.FORM, **initial}, check_delivery_windows=False), [])
            self.assertEqual(schedule.prepare({"EMAIL_TO": "changed@example.invalid"}), {})
            prepared = schedule.prepare({"CRAWLER_MINUTE": "10"}, normalize=True)
            daily = prepared[units / "trendradar-lite.timer"].decode()
            self.assertIn("OnCalendar=*-*-* 08:12:00", daily)
            self.assertIn("OnCalendar=*-*-* 07:30:00", daily)
            with self.assertRaisesRegex(native_schedule.ScheduleError, "different hours"):
                schedule.prepare({"NOON_PUSH_TIME": "08:30"}, normalize=True)
            with self.assertRaisesRegex(native_schedule.ScheduleError, "HH:MM"):
                schedule.prepare({"MORNING_PUSH_TIME": ""}, normalize=True)
            self.assertEqual(list(units.iterdir()), [])
            self.assertEqual(timeline_path.read_bytes(), original)

    def test_standalone_native_import_from_outside_repository(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            # A script fixture avoids inheriting the test runner's sys.path.
            script = Path(directory) / "check_import.py"
            script.write_text(
                "import importlib.util\n"
                "from pathlib import Path\n"
                "import sys\n"
                "spec = importlib.util.spec_from_file_location('native_schedule', sys.argv[1])\n"
                "module = importlib.util.module_from_spec(spec)\n"
                "spec.loader.exec_module(module)\n"
                "module._validate(module.DEFAULT_SCHEDULE)\n"
            )
            result = subprocess.run([sys.executable, "-I", str(script), str(root / "deploy/native_schedule.py")],
                                    cwd=directory, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
