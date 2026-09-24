"""Offline contract tests: no systemd calls or production-path mutations."""
import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("native_schedule", ROOT / "deploy" / "native_schedule.py")
native = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(native)


class NativeScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = self.root / "app"
        self.config = self.app / "config"
        self.config.mkdir(parents=True)
        self.units = self.root / "units"
        self.units.mkdir()
        for name in ("config.yaml", "timeline.yaml"):
            shutil.copyfile(ROOT / "config" / name, self.config / name)
        for name in native.TIMER_NAMES:
            shutil.copyfile(ROOT / "deploy" / "systemd" / name, self.units / name)
        self.daily, self.weekly = (self.units / name for name in native.TIMER_NAMES)
        self.runner = mock.Mock(side_effect=AssertionError("Inspection must not run commands"))

    def inspect(self, values=None):
        return native.NativeSchedule(self.app, self.units, values or {}, runner=self.runner)

    def canonical(self, overrides=None):
        values = {**native.DEFAULT_SCHEDULE, **(overrides or {})}
        schedule = self.inspect(values)
        for path, data in schedule.prepare(values, normalize=True).items():
            path.write_bytes(data)
        return values

    def edit_file(self, path, old, new):
        text = path.read_text()
        self.assertIn(old, text)
        path.write_text(text.replace(old, new))

    def assert_blocked_but_mail_allowed(self, schedule):
        self.assertFalse(schedule.supported)
        self.assertTrue(schedule.warnings)
        self.assertEqual(schedule.prepare({"EMAIL_TO": "reader@example.com", "AI_MODEL": "new-model"}), {})
        with self.assertRaises(native.ScheduleError):
            schedule.prepare({"CRAWLER_MINUTE": "19"}, normalize=True)
        self.runner.assert_not_called()

    def test_legacy_hourly_is_zero_not_new_install_default_five(self):
        schedule = self.inspect()
        self.assertTrue(schedule.supported)
        self.assertEqual(schedule.initial, {})
        self.assertEqual(schedule.suggested["CRAWLER_MINUTE"], "0")
        self.assertEqual(native.DEFAULT_SCHEDULE["CRAWLER_MINUTE"], "5")
        self.assertEqual(schedule.suggested["MORNING_PUSH_TIME"], "07:00")
        self.assertEqual(schedule.suggested["WEEKLY_WEEKDAY"], "6")
        self.assertTrue(any("窗口为 07:00-07:59" in item for item in schedule.warnings))
        self.assertTrue(any("缺少" in item for item in schedule.warnings))
        self.assertIn("尚未查询", "\n".join(schedule.describe()))
        self.runner.assert_not_called()

    def test_initial_is_copy_and_environment_precedes_existing_timer(self):
        values = {"CRAWLER_MINUTE": "17", "MORNING_PUSH_TIME": "08:30", "WEEKLY_HOUR": "9", "TZ": "UTC"}
        schedule = self.inspect(values)
        values["CRAWLER_MINUTE"] = "50"
        self.assertEqual(schedule.initial["CRAWLER_MINUTE"], "17")
        for key, value in (("CRAWLER_MINUTE", "17"), ("MORNING_PUSH_TIME", "08:30"), ("WEEKLY_HOUR", "9"), ("TZ", "UTC")):
            self.assertEqual(schedule.suggested[key], value)
        self.assertTrue(any("不一致" in item for item in schedule.warnings))
        self.assertFalse(any("MORNING_PUSH_TIME: YAML window" in item for item in schedule.warnings))

    def test_legacy_generated_minute_and_weekly_values_are_inferred(self):
        self.edit_file(self.daily, "OnCalendar=hourly", "OnCalendar=*-*-* *:17:00")
        self.edit_file(self.weekly, "Sun *-*-* 12:30:00 Asia/Shanghai", "Tue *-*-* 09:45:00 UTC")
        schedule = self.inspect()
        self.assertTrue(schedule.supported)
        self.assertEqual(schedule.suggested["CRAWLER_MINUTE"], "17")
        self.assertEqual(schedule.suggested["WEEKLY_WEEKDAY"], "1")
        self.assertEqual(schedule.suggested["WEEKLY_HOUR"], "9")
        self.assertEqual(schedule.suggested["WEEKLY_MINUTE"], "45")
        self.assertTrue(any("时区" in item for item in schedule.warnings))

    def test_customized_shipped_yaml_times_not_hardcoded_defaults(self):
        self.edit_file(self.config / "timeline.yaml", 'start: "07:00"', 'start: "08:12"')
        self.edit_file(self.config / "timeline.yaml", 'end: "07:59"', 'end: "08:45"')
        self.edit_file(self.config / "config.yaml", "timezone: Asia/Shanghai", "timezone: Europe/London")
        schedule = self.inspect()
        self.assertTrue(schedule.supported)
        self.assertEqual(schedule.suggested["MORNING_PUSH_TIME"], "08:12")
        self.assertEqual(schedule.suggested["TZ"], "Europe/London")
        self.assertTrue(any("08:12-08:45" in item for item in schedule.warnings))

    def test_inspect_describe_and_prepare_do_not_write_or_call_systemctl(self):
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        schedule = self.inspect()
        schedule.describe()
        changes = schedule.prepare(schedule.suggested, normalize=True)
        self.assertEqual(set(changes), {self.daily, self.weekly})
        self.assertTrue(all(isinstance(data, bytes) for data in changes.values()))
        after = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.runner.assert_not_called()

    def test_unchanged_or_mail_ai_only_legacy_saves_leave_timers_untouched(self):
        schedule = self.inspect({"EMAIL_TO": "old@example.com"})
        self.assertEqual(schedule.prepare(schedule.initial), {})
        self.assertEqual(schedule.prepare({"EMAIL_TO": "new@example.com", "AI_ANALYSIS_ENABLED": "true"}), {})
        self.assertEqual(schedule.prepare({}), {})

    def test_legacy_schedule_edits_require_explicit_normalization(self):
        schedule = self.inspect()
        with self.assertRaisesRegex(native.ScheduleError, "normalize=True"):
            schedule.prepare({"CRAWLER_MINUTE": "17"})
        changes = schedule.prepare({"CRAWLER_MINUTE": "17"}, normalize=True)
        self.assertIn(b"OnCalendar=*-*-* *:17:00 Asia/Shanghai", changes[self.daily])
        self.assertIn(b"OnCalendar=Sun *-*-* 12:30:00 Asia/Shanghai", changes[self.weekly])

    def test_both_timers_get_explicit_timezone(self):
        schedule = self.inspect()
        changes = schedule.prepare({"TZ": "America/New_York"}, normalize=True)
        for data in changes.values():
            for line in data.decode().splitlines():
                if line.startswith("OnCalendar="):
                    self.assertTrue(line.endswith(" America/New_York"), line)

    def test_normalized_matching_schedule_changes_do_not_need_normalize(self):
        values = self.canonical()
        schedule = self.inspect(values)
        self.assertTrue(schedule.supported)
        self.assertEqual(schedule.warnings, [])
        self.assertEqual(schedule.prepare(values), {})
        changes = schedule.prepare({"CRAWLER_MINUTE": "12"})
        self.assertEqual(set(changes), {self.daily, self.weekly})
        self.assertIn(b"*:12:00 Asia/Shanghai", changes[self.daily])
        self.assertEqual(changes[self.weekly], self.weekly.read_bytes())

    def test_normalization_preserves_unknown_directives_comments_and_crlf(self):
        values = self.canonical()
        original = self.daily.read_text().replace("[Timer]\n", "[Timer]\n# Keep this comment\nRandomizedDelaySec=9s\nX-Local-Note=retain\n")
        original = original.replace("[Unit]\n", "[Unit]\nAfter=network-online.target\n")
        original += "\n# final comment\n"
        self.daily.write_bytes(original.replace("\n", "\r\n").encode())
        before = self.daily.read_bytes()
        after = self.inspect(values).prepare({"CRAWLER_MINUTE": "18"})[self.daily]
        keep = lambda data: [line for line in data.splitlines(keepends=True) if not line.startswith(b"OnCalendar=")]
        self.assertEqual(keep(before), keep(after))
        self.assertNotIn(b"\n", after.replace(b"\r\n", b""))

    def test_only_timer_section_oncalendar_is_replaced(self):
        values = self.canonical()
        self.edit_file(self.daily, "[Unit]\n", "[Unit]\nOnCalendar=do-not-touch\n")
        data = self.inspect(values).prepare({"CRAWLER_MINUTE": "16"})[self.daily]
        self.assertIn(b"[Unit]\nOnCalendar=do-not-touch\n", data)
        self.assertIn(b"OnCalendar=*-*-* *:16:00", data)

    def test_supported_legacy_generated_daily_timer_can_be_normalized(self):
        values = self.canonical()
        self.daily.write_text(self.daily.read_text().replace(" Asia/Shanghai", ""))
        schedule = self.inspect(values)
        self.assertTrue(schedule.supported)
        self.assertTrue(any("服务器本地时区" in warning for warning in schedule.warnings))
        with self.assertRaises(native.ScheduleError):
            schedule.prepare({"TZ": "UTC"})
        changes = schedule.prepare({"TZ": "UTC"}, normalize=True)
        self.assertEqual(changes[self.daily].count(b"OnCalendar="), 5)
        self.assertEqual(changes[self.daily].count(b" UTC\n"), 5)

    def test_stale_supported_timer_and_env_require_normalization(self):
        values = self.canonical()
        values["WEEKLY_HOUR"] = "9"
        schedule = self.inspect(values)
        self.assertTrue(schedule.supported)
        with self.assertRaises(native.ScheduleError):
            schedule.prepare({"CRAWLER_MINUTE": "14"})
        self.assertIn(b"09:30:00", schedule.prepare(values, normalize=True)[self.weekly])

    def test_fresh_pair_requires_normalization_and_is_not_claimed_active(self):
        self.daily.unlink()
        self.weekly.unlink()
        schedule = self.inspect()
        self.assertTrue(schedule.supported)
        self.assertTrue(any("不代表已生效" in item for item in schedule.warnings))
        self.assertEqual(schedule.prepare({"AI_MODEL": "test"}), {})
        with self.assertRaises(native.ScheduleError):
            schedule.prepare({"CRAWLER_MINUTE": "15"})
        changes = schedule.prepare({}, normalize=True)
        self.assertEqual(set(changes), {self.daily, self.weekly})
        self.assertIn(b"*:05:00 Asia/Shanghai", changes[self.daily])
        self.assertFalse(self.daily.exists())
        self.assertFalse(self.weekly.exists())

    def test_missing_single_timer_always_refused(self):
        for missing in (self.daily, self.weekly):
            with self.subTest(missing=missing.name):
                data = missing.read_bytes()
                missing.unlink()
                self.assert_blocked_but_mail_allowed(self.inspect())
                missing.write_bytes(data)

    def test_missing_config_never_guesses_active_schedule(self):
        (self.config / "timeline.yaml").unlink()
        self.assert_blocked_but_mail_allowed(self.inspect())

    def test_unsupported_calendar_forms_are_not_replaced(self):
        original = self.daily.read_text()
        for expr in ("minutely", "Mon..Fri *-*-* 07:00:00", "*-*-* *:00/15:00", "*-*-* 03:00:30", "", "daily"):
            with self.subTest(expression=expr):
                self.daily.write_text(original.replace("OnCalendar=hourly", "OnCalendar=" + expr))
                self.assert_blocked_but_mail_allowed(self.inspect())

    def test_custom_timer_triggers_target_resets_and_repeated_sections_rejected(self):
        original = self.daily.read_text()
        for extra in ("OnBootSec=2min", "OnUnitActiveSec=1h", "OnClockChange=true", "Unit=other.service", "OnCalendar=", "[Timer]\nAccuracySec=1s", "OnCalendar=hourly"):
            with self.subTest(extra=extra):
                self.daily.write_text(original.replace("Persistent=true", extra + "\nPersistent=true"))
                self.assert_blocked_but_mail_allowed(self.inspect())

    def test_matching_explicit_service_target_is_preserved(self):
        values = self.canonical()
        self.edit_file(self.daily, "[Timer]\n", "[Timer]\nUnit=trendradar-lite.service\n")
        schedule = self.inspect(values)
        self.assertTrue(schedule.supported)
        self.assertIn(b"Unit=trendradar-lite.service", schedule.prepare({"CRAWLER_MINUTE": "15"})[self.daily])

    def test_fifth_daily_trigger_and_mixed_zones_are_ambiguous(self):
        values = self.canonical()
        original = self.daily.read_text()
        self.daily.write_text(original.replace("Persistent=true", "OnCalendar=*-*-* 03:00:00 Asia/Shanghai\nPersistent=true"))
        self.assert_blocked_but_mail_allowed(self.inspect(values))
        self.daily.write_text(original.replace("07:00:00 Asia/Shanghai", "07:00:00 UTC"))
        self.assert_blocked_but_mail_allowed(self.inspect(values))

    def test_dropins_specific_prefix_and_typewide_block_schedule_only(self):
        for name in ("trendradar-lite.timer.d", "trendradar-weekly.timer.d", "trendradar-.timer.d", "timer.d"):
            with self.subTest(dropin=name):
                path = self.units / name
                path.mkdir()
                (path / "custom.conf").write_text("[Timer]\nOnCalendar=daily\n")
                self.assert_blocked_but_mail_allowed(self.inspect())
                shutil.rmtree(path)

    def test_symlinked_timer_and_unit_directory_rejected(self):
        data = self.daily.read_bytes()
        self.daily.unlink()
        target = self.root / "target.timer"
        target.write_bytes(data)
        self.daily.symlink_to(target)
        self.assert_blocked_but_mail_allowed(self.inspect())
        self.daily.unlink()
        self.daily.write_bytes(data)
        alias = self.root / "alias"
        alias.symlink_to(self.units, target_is_directory=True)
        schedule = native.NativeSchedule(self.app, alias, {}, runner=self.runner)
        self.assert_blocked_but_mail_allowed(schedule)

    def test_broken_symlinks_never_treated_as_fresh_install(self):
        self.daily.unlink()
        self.weekly.unlink()
        self.daily.symlink_to(self.root / "does-not-exist")
        self.assert_blocked_but_mail_allowed(self.inspect())

    def test_config_yaml_custom_schedule_fields_are_refused(self):
        self.edit_file(self.config / "config.yaml", "schedule:\n", "schedule:\n  custom_times: override\n")
        self.assert_blocked_but_mail_allowed(self.inspect())

    def test_disabled_or_preset_schedule_is_refused_even_with_normalize(self):
        for values in ({"SCHEDULE_ENABLED": "false"}, {"SCHEDULE_ENABLED": "yes"}, {"SCHEDULE_PRESET": "always_on"}, {"CONFIG_PATH": "other/config.yaml"}):
            with self.subTest(values=values):
                self.assert_blocked_but_mail_allowed(self.inspect(values))

    def test_timeline_arbitrary_customizations_are_not_approximated(self):
        path = self.config / "timeline.yaml"
        original = path.read_text()
        replacements = [
            ("1: all_day", "1: weekday"),
            ("report_mode: daily", "report_mode: current"),
            ("start: \"07:00\"", "start: &morning \"07:00\""),
            ("custom:", "custom: &base"),
            ("  periods:", "  overlap:\n    policy: first_match\n  periods:"),
            ("    morning_brief:\n", "    morning_brief:\n      extra: value\n"),
            ("      start: \"07:00\"", "      start: \"07:00\"\n      start: \"08:00\""),
            ("end: \"07:59\"", "end: \"06:00\""),
        ]
        for old, new in replacements:
            with self.subTest(replacement=new):
                path.write_text(original.replace(old, new, 1))
                self.assert_blocked_but_mail_allowed(self.inspect())
        path.write_text(original)

    def test_yaml_comments_and_quoted_hash_are_supported(self):
        self.edit_file(self.config / "timeline.yaml", "name: 早间速览", 'name: "早间 # 速览" # comment')
        self.edit_file(self.config / "timeline.yaml", 'start: "07:00"', 'start: "07:00" # start')
        self.assertTrue(self.inspect().supported)

    def test_unrelated_complex_config_sections_do_not_require_pyyaml(self):
        with (self.config / "config.yaml").open("a") as output:
            output.write('\nunrelated:\n  flow: {a: [1, 2]}\n  text: |\n    something\n')
        self.assertTrue(self.inspect().supported)

    def test_timer_and_config_change_since_inspection_refused(self):
        values = self.canonical()
        for path in (self.daily, self.weekly, self.config / "timeline.yaml", self.config / "config.yaml"):
            with self.subTest(path=path):
                original = path.read_bytes()
                schedule = self.inspect(values)
                path.write_bytes(original + b"\n# concurrent change\n")
                with self.assertRaisesRegex(native.ScheduleError, "changed since inspection"):
                    schedule.prepare({"CRAWLER_MINUTE": "17"})
                path.write_bytes(original)

    def test_dropin_added_after_inspection_refused(self):
        values = self.canonical()
        schedule = self.inspect(values)
        path = self.units / "timer.d"
        path.mkdir()
        (path / "late.conf").write_text("[Timer]\nOnBootSec=1min\n")
        with self.assertRaisesRegex(native.ScheduleError, "drop-ins"):
            schedule.prepare({"CRAWLER_MINUTE": "17"})

    def test_time_and_timezone_validation_has_no_silent_defaults(self):
        values = self.canonical()
        schedule = self.inspect(values)
        for invalid in ({"CRAWLER_MINUTE": "60"}, {"WEEKLY_WEEKDAY": "7"}, {"WEEKLY_HOUR": "24"}, {"WEEKLY_MINUTE": "-1"}, {"MORNING_PUSH_TIME": "7:00"}, {"TZ": "Not/A_Zone"}, {"TZ": "UTC\nOnBootSec=1"}, {"TZ": ""}, {"CRAWLER_MINUTE": ""}, {"NOON_PUSH_TIME": "07:00"}):
            with self.subTest(invalid=invalid):
                with self.assertRaises(native.ScheduleError):
                    schedule.prepare(invalid)

    def test_timezone_override_is_respected_and_conflict_cannot_be_saved(self):
        schedule = self.inspect({"TIMEZONE": "UTC", "TZ": "Asia/Shanghai"})
        self.assertEqual(schedule.suggested["TZ"], "UTC")
        self.assertTrue(any("TIMEZONE 会覆盖 TZ" in item for item in schedule.warnings))
        with self.assertRaisesRegex(native.ScheduleError, "TIMEZONE overrides TZ"):
            schedule.prepare({"TZ": "Europe/London"}, normalize=True)
        changes = schedule.prepare({"TZ": "UTC"}, normalize=True)
        for data in changes.values():
            self.assertIn(b" UTC\n", data)

    def test_unquoted_yaml_times_are_refused_instead_of_misparsed(self):
        self.edit_file(self.config / "timeline.yaml", 'start: "12:00"', 'start: 12:00')
        self.assert_blocked_but_mail_allowed(self.inspect())

    def test_invalid_environment_can_be_repaired_without_replacing_custom_units(self):
        values = self.canonical()
        values["CRAWLER_MINUTE"] = "99"
        schedule = self.inspect(values)
        self.assertTrue(schedule.supported)
        self.assertEqual(schedule.prepare({"EMAIL_TO": "new@example.com"}), {})
        with self.assertRaises(native.ScheduleError):
            schedule.prepare({"CRAWLER_MINUTE": "9"})
        self.assertIn(b"*:09:00", schedule.prepare({"CRAWLER_MINUTE": "9"}, normalize=True)[self.daily])

    def test_scheduler_control_changes_require_separate_migration(self):
        values = self.canonical()
        schedule = self.inspect(values)
        with self.assertRaisesRegex(native.ScheduleError, "Changing SCHEDULE_PRESET"):
            schedule.prepare({"SCHEDULE_PRESET": "always_on"}, normalize=True)


if __name__ == "__main__":
    unittest.main()
