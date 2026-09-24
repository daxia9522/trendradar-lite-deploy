"""Offline native menu, env transaction and mocked systemd regression tests."""
import contextlib
import importlib.util
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("menu_configure", ROOT / "deploy/configure.py")
configure = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(configure)
from envfile import ConfigError, ConcurrentEdit, EnvDocument, atomic_write, read_env, write_env
from native_config import ApplyError, NativeApplication, UnitTransaction
from native_install import install_launcher, launcher_content, remove_launcher


class FakeSystemctl:
    def __init__(self, units):
        self.units = units
        self.calls = []
        self.states = {"trendradar-lite.timer": ("active", "enabled"),
                       "trendradar-weekly.timer": ("inactive", "disabled")}
        self.fail_reload = 0
        self.dropins = ""
        self.fragment = None

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        if command[:2] == ["systemd-analyze", "calendar"]:
            # Calendar arithmetic/boundary failures are covered separately in
            # test_native_apply_safety; ordinary menu tests use a safe future.
            output = "\n\n".join("Next elapse: Fri 2100-01-01 00:00:00 UTC"
                                   for _ in command[5:])
            return subprocess.CompletedProcess(command, 0, output, "")
        assert command[:2] == ["systemctl", "--user"]
        action = command[2]
        assert action in ("show", "daemon-reload"), command
        if action == "daemon-reload":
            if self.fail_reload:
                self.fail_reload -= 1
                return subprocess.CompletedProcess(command, 1, "", "secret diagnostic")
            return subprocess.CompletedProcess(command, 0, "", "")
        name = command[3]
        active, enabled = self.states[name]
        output = (f"ActiveState={active}\nUnitFileState={enabled}\nFragmentPath={self.fragment or self.units / name}\nDropInPaths={self.dropins}\n"
                  "SubState=waiting\nLastTriggerUSec=Thu 2026-01-01 00:05:00 UTC\nNeedDaemonReload=no\n")
        return subprocess.CompletedProcess(command, 0, output, "")


class EnvFileTests(unittest.TestCase):
    def test_literal_roundtrip_comments_unknowns_backup_and_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "env"
            original = b"# my deployment\nTREND_RADAR_IMAGE=example:v1\nS3_ACCESS_KEY_ID='do not drop'\n\nEMAIL_PASSWORD='old'\n"
            path.write_bytes(original)
            secret = ' $HOME ${USER} $(touch sentinel) `touch sentinel` \\ space "quoted" \'single\' '
            write_env(path, {"EMAIL_PASSWORD": secret, "AI_API_KEY": secret})
            loaded = read_env(path)
            self.assertEqual(loaded["EMAIL_PASSWORD"], secret)
            self.assertEqual(loaded["AI_API_KEY"], secret)
            self.assertEqual(loaded["S3_ACCESS_KEY_ID"], "do not drop")
            self.assertTrue(path.read_bytes().startswith(original.split(b"EMAIL_PASSWORD")[0]))
            backup_dir = path.parent / ".env.backups"
            backups = list(backup_dir.iterdir())
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), original)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
            self.assertEqual(backup_dir.stat().st_mode & 0o777, 0o700)
            self.assertFalse((Path(tmp) / "sentinel").exists())

    def test_changed_duplicate_assignments_are_collapsed_without_losing_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "env"
            path.write_text("A=first\n# retain\nA=second\nX=literal # not shell comment\n")
            write_env(path, {"A": "new"})
            self.assertEqual(path.read_text().count("A="), 1)
            self.assertIn("# retain", path.read_text())
            self.assertEqual(read_env(path)["X"], "literal # not shell comment")

    def test_concurrent_edit_is_not_clobbered(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "env"
            path.write_text("A=old\n")
            document = EnvDocument(path)
            path.write_text("A=external\n")
            with self.assertRaises(ConcurrentEdit):
                document.save({"A": "mine"})
            self.assertEqual(path.read_text(), "A=external\n")
            self.assertFalse((path.parent / ".env.backups").exists())

    def test_identical_atomic_replacement_is_still_a_concurrent_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "env"
            path.write_text("A=old\n")
            document = EnvDocument(path)
            atomic_write(path, b"A=old\n")
            with self.assertRaises(ConcurrentEdit):
                document.save({"A": "mine"})

    def test_open_and_noop_save_have_no_side_effects(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing/env"
            document = EnvDocument(path)
            self.assertEqual(document.values, {})
            self.assertFalse(path.parent.exists())
            path.parent.mkdir()
            path.write_text("# untouched\nA='one'\n")
            before = path.stat()
            self.assertFalse(EnvDocument(path).save({"A": "one"}))
            self.assertEqual(path.stat().st_mtime_ns, before.st_mtime_ns)
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_invalid_syntax_and_newlines_do_not_echo_secrets_or_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "env"
            path.write_text("export SECRET=do-not-echo\n")
            with self.assertRaises(ConfigError) as raised:
                EnvDocument(path)
            self.assertNotIn("do-not-echo", str(raised.exception))
            path.unlink()
            with self.assertRaises(ConfigError):
                write_env(path, {"EMAIL_PASSWORD": "do-not-echo\nother"})
            self.assertFalse(path.exists())

    def test_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.write_text("A=old\n")
            path = Path(tmp) / "env"
            path.symlink_to(target)
            with self.assertRaises(ConfigError):
                write_env(path, {"A": "new"})
            self.assertEqual(target.read_text(), "A=old\n")

    def test_docker_single_quote_and_inline_comment_are_literal(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("A='single\\'quote $HOME'\nB=plain # compose comment\n")
            self.assertEqual(read_env(path, "docker"), {"A": "single'quote $HOME", "B": "plain"})


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.units = self.root / "units"
        self.units.mkdir()
        self.path = self.root / "env"
        self.values = read_env(ROOT / ".env.example")
        self.values.update(EMAIL_FROM="sender@example.com", EMAIL_TO="reader@example.com", EMAIL_PASSWORD="never-display-this", AI_MODEL="original/model")
        write_env(self.path, self.values)
        configure.write_systemd_timer(self.units / "trendradar-lite.timer", self.values)
        configure.write_systemd_weekly_timer(self.units / "trendradar-weekly.timer", self.values)
        self.runner = FakeSystemctl(self.units)
        self.app = NativeApplication(self.path, unit_dir=self.units, runner=self.runner)

    def menu(self, answers, secrets=()):
        output = io.StringIO()
        prompts = []
        answers = iter(answers)
        secrets = iter(secrets)
        def answer(prompt):
            prompts.append(prompt)
            value = next(answers)
            if isinstance(value, BaseException):
                raise value
            return value
        def secret(prompt):
            prompts.append(prompt)
            return next(secrets)
        with contextlib.redirect_stdout(output), mock.patch.object(configure, "input", answer, create=True), mock.patch.object(configure.getpass, "getpass", secret):
            result = configure.configure_terminal(self.path, application=self.app)
        return result, output.getvalue(), prompts

    def test_select_edit_view_save_ordinary_values_without_systemctl(self):
        result, output, _ = self.menu(["invalid", "2", "2", "changed/model", "0", "5", "s", "y"])
        self.assertTrue(result)
        self.assertEqual(read_env(self.path)["AI_MODEL"], "changed/model")
        self.assertIn("待保存变更", output)
        self.assertEqual(self.runner.calls, [])

    def test_secret_is_hidden_and_literal_spaces_survive(self):
        secret = ' new $password `literal` "q" '
        result, output, prompts = self.menu(["1", "2", "0", "5", "s", "y"], [secret])
        self.assertTrue(result)
        self.assertEqual(read_env(self.path)["EMAIL_PASSWORD"], secret)
        for value in (secret, self.values["EMAIL_PASSWORD"]):
            self.assertNotIn(value, output + "".join(prompts))

    def test_dirty_quit_needs_confirmation_and_can_be_declined(self):
        original = self.path.read_bytes()
        result, _, _ = self.menu(["2", "2", "changed/model", "0", "q", "n", "q", "y"])
        self.assertFalse(result)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.runner.calls, [])

    def test_revert_removes_diff_and_quit_does_not_ask_to_discard(self):
        result, output, prompts = self.menu(["2", "2", "changed/model", "2", "original/model", "0", "5", "q"])
        self.assertFalse(result)
        self.assertIn("没有待保存变更", output)
        self.assertFalse(any("放弃全部" in prompt for prompt in prompts))

    def test_eof_and_ctrl_c_after_edit_leave_disk_identical(self):
        for interruption in (EOFError(), KeyboardInterrupt()):
            original = self.path.read_bytes()
            with self.assertRaises(type(interruption)):
                self.menu(["2", "2", "changed/model", "0", interruption])
            self.assertEqual(self.path.read_bytes(), original)
            self.assertEqual(self.runner.calls, [])

    def test_error_reprompts_only_selected_field_and_clear_cancel(self):
        result, output, _ = self.menu(["4", "1", "0", "oops", "15", "2", ":cancel", "0", "2", "5", ":clear", "0", "s", "y"])
        self.assertTrue(result)
        self.assertEqual(read_env(self.path)["AI_TIMEOUT"], "15")
        self.assertIn("AI_TIMEOUT 必须", output)
        self.assertEqual(read_env(self.path)["AI_MODEL"], "original/model")

    def test_boolean_explicit_enable_disable_choices(self):
        result, output, _ = self.menu(["2", "1", "maybe", "1", "1", "2", "0", "q"])
        self.assertFalse(result)
        self.assertIn("必须是 true 或 false", output)

    def test_schedule_applies_both_timers_without_start_restart_or_enable(self):
        values = dict(self.values, TZ="UTC")
        result = self.app.save(values)
        self.assertIn("已保存", result)
        for name in self.runner.states:
            self.assertIn(" UTC", (self.units / name).read_text())
        actions = [call[2] for call in self.runner.calls if call[0] == "systemctl"]
        self.assertEqual(actions.count("daemon-reload"), 1)
        self.assertTrue(set(actions) <= {"show", "daemon-reload"})

    def test_daemon_reload_failure_rolls_back_env_and_units(self):
        old_env = self.path.read_bytes()
        old_units = {p: p.read_bytes() for p in self.units.glob("*.timer")}
        self.runner.fail_reload = 1
        with self.assertRaises(ApplyError) as raised:
            self.app.save(dict(self.values, TZ="UTC"))
        self.assertIn("env 已恢复", str(raised.exception))
        self.assertEqual(self.path.read_bytes(), old_env)
        for path, old in old_units.items():
            self.assertEqual(path.read_bytes(), old)
        self.assertNotIn("secret diagnostic", str(raised.exception))

    def test_failed_rollback_is_not_reported_as_success(self):
        self.runner.fail_reload = 2
        with self.assertRaises(ApplyError) as raised:
            self.app.save(dict(self.values, TZ="UTC"))
        self.assertIn("回滚不完整", str(raised.exception))

    def test_effective_dropin_or_foreign_fragment_refused_before_env_write(self):
        before = self.path.read_bytes()
        self.runner.dropins = "/external/timer.conf"
        with self.assertRaises(ApplyError):
            self.app.save(dict(self.values, TZ="UTC"))
        self.runner.dropins = ""
        self.runner.fragment = "/external/timer"
        with self.assertRaises(ApplyError):
            self.app.save(dict(self.values, TZ="UTC"))
        self.assertEqual(before, self.path.read_bytes())

    def test_legacy_mail_change_preserves_absent_schedule_and_custom_timer(self):
        legacy = {key: self.values[key] for key in ("EMAIL_FROM", "EMAIL_TO", "EMAIL_PASSWORD")}
        self.path.unlink()
        write_env(self.path, legacy)
        timer = self.units / "trendradar-lite.timer"
        timer.write_text("[Timer]\nOnCalendar=Mon *-*-* 01:00\n")
        self.app = NativeApplication(self.path, unit_dir=self.units, runner=self.runner)
        result, output, _ = self.menu(["1", "3", "other@example.com", "0", "s", "y"])
        self.assertTrue(result)
        self.assertIn("警告", output)
        self.assertNotIn("TZ", read_env(self.path))
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(timer.read_text(), "[Timer]\nOnCalendar=Mon *-*-* 01:00\n")

    def test_legacy_normalization_persists_runtime_overrides(self):
        legacy = {key: self.values[key] for key in ("EMAIL_FROM", "EMAIL_TO", "EMAIL_PASSWORD", "TZ")}
        self.path.unlink()
        write_env(self.path, legacy)
        (self.units / "trendradar-lite.timer").write_text("[Timer]\nOnCalendar=hourly\nPersistent=true\n")
        # Active host-local calendars cannot prove they share the manager's TZ.
        # Explicit normalization remains available when already inactive.
        self.runner.states["trendradar-lite.timer"] = ("inactive", "disabled")
        self.app = NativeApplication(self.path, unit_dir=self.units, runner=self.runner)
        self.app.save(legacy, normalize=True)
        loaded = read_env(self.path)
        self.assertEqual(loaded["CRAWLER_MINUTE"], "0")
        for key in configure.TIME_FIELDS:
            self.assertEqual(loaded[key], self.values[key])

    def test_new_menu_cancel_creates_nothing(self):
        path = self.root / "new-config/env"
        self.app = NativeApplication(path, unit_dir=self.root / "new-units", runner=self.runner)
        self.path = path
        result, _, _ = self.menu(["q", "y"])
        self.assertFalse(result)
        self.assertFalse(path.parent.exists())
        self.assertFalse(self.app.unit_dir.exists())
        self.assertEqual(self.runner.calls, [])


class ValidationAndLauncherTests(unittest.TestCase):
    def test_validation_edges(self):
        for key, bad in [("AI_ANALYSIS_ENABLED", "yes"), ("AI_TIMEOUT", "0"), ("AI_TIMEOUT", "1.5"),
                         ("CRAWLER_MINUTE", "60"), ("EMAIL_SMTP_PORT", "65536"), ("TZ", "NoSuch/Zone"),
                         ("MORNING_PUSH_TIME", "9:00"), ("WEEKLY_HOUR", "24"), ("WEEKLY_WEEKDAY", "7")]:
            with self.subTest(key=key):
                self.assertTrue(configure.validate_field(key, bad))
        self.assertFalse(configure.validate_field("TZ", "UTC"))
        self.assertTrue(any("不能重复" in err for err in configure.validate({"MORNING_PUSH_TIME": "07:00", "NOON_PUSH_TIME": "07:00"})))

    def test_launcher_collision_and_ownership(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "bin/trendradar"
            content = launcher_content(root / "app with space", root / "config/env", root / "units", "/usr/bin/python3")
            install_launcher(path, content)
            self.assertEqual(path.stat().st_mode & 0o777, 0o755)
            foreign = launcher_content(root / "other", root / "env", root / "units", "/usr/bin/python3")
            with self.assertRaises(ConfigError):
                install_launcher(path, foreign)
            remove_launcher(path, foreign)
            self.assertTrue(path.exists())
            remove_launcher(path, content)
            self.assertFalse(path.exists())
            path.write_text("#!/bin/sh\necho unrelated\n")
            with self.assertRaises(ConfigError):
                install_launcher(path, content)
            self.assertIn("unrelated", path.read_text())

    def test_loader_timezone_precedence(self):
        from trendradar.core.loader import _load_app_config
        with mock.patch.dict(os.environ, {"TZ": "UTC"}, clear=True):
            self.assertEqual(_load_app_config({"app": {"timezone": "Asia/Shanghai"}})["TIMEZONE"], "UTC")
        with mock.patch.dict(os.environ, {"TZ": "UTC", "TIMEZONE": "Asia/Tokyo"}, clear=True):
            self.assertEqual(_load_app_config({})["TIMEZONE"], "Asia/Tokyo")


if __name__ == "__main__":
    unittest.main()
