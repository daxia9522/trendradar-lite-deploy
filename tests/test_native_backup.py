"""Native optional-backup lifecycle, isolated files and a stateful fake manager.

No test enables a real service or uploads data. Only systemd-analyze verify is
allowed to execute for the offline unit parser check.
"""
import contextlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))
import configure
import native_backup as backup
import native_config
import native_install
from envfile import ConfigError, read_env, write_env
from native_config import ApplyError, NativeApplication


class Manager:
    def __init__(self, units):
        self.units = units
        self.calls = []
        self.states = {}
        self.properties = {}
        self.fail_after = set()
        self.reload_hook = None
        self.unsafe = False
        self.unit_paths = [str(units)]
        self.unit_paths_error = False

    def state(self, name):
        if name in self.states:
            return self.states[name]
        return ("inactive", "disabled" if (self.units / name).exists() else "")

    def __call__(self, command, **kwargs):
        self.calls.append(list(command))
        if command == ["systemd-analyze", "--user", "unit-paths"]:
            return subprocess.CompletedProcess(command, int(self.unit_paths_error), "\n".join(self.unit_paths), "")
        if command[:2] == ["systemd-analyze", "calendar"]:
            date = "Fri 2100-01-01 00:00:00 UTC"
            if self.unsafe and not command[4].startswith("--base-time=@"):
                date = "Thu 2026-01-01 00:40:00 UTC"
            return subprocess.CompletedProcess(command, 0,
                "\n\n".join("Next elapse: " + date for _ in command[5:]), "")
        if command[:2] != ["systemctl", "--user"]:
            raise AssertionError("unexpected external command")
        action = command[2]
        if action == "show":
            name = command[3]
            active, enabled = self.state(name)
            props = {
                "ActiveState": active, "UnitFileState": enabled,
                "FragmentPath": str(self.units / name) if (self.units / name).exists() else "",
                "DropInPaths": "", "SubState": "waiting" if active == "active" else "dead",
                "LastTriggerUSec": "Thu 2026-01-01 00:00:00 UTC", "NeedDaemonReload": "no",
            }
            props.update(self.properties.get(name, {}))
            return subprocess.CompletedProcess(command, 0,
                "\n".join(f"{key}={value}" for key, value in props.items()), "")
        if action == "daemon-reload":
            if self.reload_hook is not None:
                hook, self.reload_hook = self.reload_hook, None
                hook()
        elif action in ("enable", "disable", "start", "stop"):
            name = command[-1]
            if name != backup.TIMER:
                raise AssertionError("backup changed unrelated timer/service: " + name)
            active, enabled = self.state(name)
            if action == "enable":
                if "--no-reload" not in command:
                    raise AssertionError("implicit global reload")
                enabled = "enabled-runtime" if "--runtime" in command else "enabled"
            elif action == "disable":
                if "--no-reload" not in command:
                    raise AssertionError("implicit global reload")
                enabled = "disabled"
            elif action == "start":
                active = "active"
            else:
                active = "inactive"
            self.states[name] = (active, enabled)
        else:
            raise AssertionError("unexpected systemctl action: " + action)
        if action in self.fail_after:
            self.fail_after.remove(action)
            return subprocess.CompletedProcess(command, 1, "", "private-manager-diagnostic")
        return subprocess.CompletedProcess(command, 0, "", "")

    def mutations(self):
        return [call for call in self.calls if call[:2] == ["systemctl", "--user"] and call[2] != "show"]


class NativeBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="native-r2-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.units = self.root / "units"
        self.units.mkdir()
        self.env = self.root / "config" / "env"
        self.values = read_env(ROOT / ".env.example")
        self.values.update(TZ="UTC", EMAIL_FROM="sender@example.invalid", EMAIL_TO="reader@example.invalid",
                           EMAIL_PASSWORD="private-mail", R2_BACKUP_ENABLED="false", STORAGE_BACKEND="local")
        write_env(self.env, self.values)
        configure.write_systemd_timer(self.units / "trendradar-lite.timer", self.values)
        configure.write_systemd_weekly_timer(self.units / "trendradar-weekly.timer", self.values)
        self.manager = Manager(self.units)
        self.report_files = {path: path.read_bytes() for path in self.units.iterdir()}
        self.output = io.StringIO()
        self.capture = contextlib.redirect_stdout(self.output)
        self.capture.__enter__()
        self.addCleanup(self.capture.__exit__, None, None, None)
        for target in ("socket.socket.connect", "socket.socket.connect_ex", "socket.getaddrinfo"):
            patcher = patch(target, side_effect=AssertionError("network forbidden"))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.environment = patch.dict(os.environ, {
            "HOME": str(self.root / "home"), "XDG_CONFIG_HOME": str(self.root / "xdg"),
            "XDG_DATA_HOME": str(self.root / "data"), "XDG_RUNTIME_DIR": str(self.root / "run"),
            "XDG_CONFIG_DIRS": str(self.root / "system-config"), "XDG_DATA_DIRS": str(self.root / "system-data"),
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def tearDown(self):
        for value in ("private-mail", "private-access", "private-secret", "private-manager-diagnostic"):
            self.assertNotIn(value, self.output.getvalue())

    def enabled(self, **updates):
        values = dict(self.values, R2_BACKUP_ENABLED="true", R2_BACKUP_TIME="23:40",
                      S3_BUCKET_NAME="synthetic-bucket", S3_ENDPOINT_URL="https://objects.example.invalid",
                      S3_ACCESS_KEY_ID="private-access", S3_SECRET_ACCESS_KEY="private-secret")
        values.update(updates)
        return values

    def application(self, *, install=False):
        return NativeApplication(self.env, app_dir=ROOT, unit_dir=self.units, install=install, runner=self.manager)

    def existing(self, *, active="inactive", enabled="disabled"):
        values = self.enabled()
        write_env(self.env, values)
        for path, content in backup.render_units(ROOT, self.env, self.units, backup.settings(values)).items():
            path.write_bytes(content)
        self.manager.states[backup.TIMER] = (active, enabled)
        self.manager.states[backup.SERVICE] = ("inactive", "static")
        return values

    def report_unchanged(self):
        for path, content in self.report_files.items():
            self.assertEqual(path.read_bytes(), content)
        for call in self.manager.mutations():
            if call[2] != "daemon-reload":
                self.assertEqual(call[-1], backup.TIMER)

    def no_backup_files(self):
        self.assertFalse((self.units / backup.TIMER).exists())
        self.assertFalse((self.units / backup.SERVICE).exists())

    def test_default_disabled_save_never_registers_or_activates_backup(self):
        self.application().save(dict(self.values, EMAIL_TO="next@example.invalid"))
        self.no_backup_files()
        self.assertEqual(self.manager.mutations(), [])
        self.report_unchanged()

    def test_missing_credentials_rejected_before_env_or_units_change(self):
        original = self.env.read_bytes()
        with self.assertRaises(ConfigError):
            self.application().save(dict(self.values, R2_BACKUP_ENABLED="true"))
        self.assertEqual(self.env.read_bytes(), original)
        self.no_backup_files()
        self.assertEqual(self.manager.calls, [])

    def test_explicit_off_to_on_registers_future_only_timer_not_service(self):
        self.application().save(self.enabled())
        self.assertEqual(read_env(self.env)["R2_BACKUP_ENABLED"], "true")
        self.assertEqual(self.manager.state(backup.TIMER), ("active", "enabled"))
        timer = (self.units / backup.TIMER).read_text()
        self.assertIn("Persistent=false", timer)
        self.assertIn("OnCalendar=*-*-* 23:40:00 UTC", timer)
        self.assertIn("--configured", (self.units / backup.SERVICE).read_text())
        self.assertEqual([call[2] for call in self.manager.mutations()], ["daemon-reload", "enable", "start"])
        self.report_unchanged()

    def test_explicit_disable_stops_only_backup_timer_and_keeps_units(self):
        values = self.existing(active="active", enabled="enabled")
        self.application().save(dict(values, R2_BACKUP_ENABLED="false"))
        self.assertEqual(self.manager.state(backup.TIMER), ("inactive", "disabled"))
        self.assertEqual(read_env(self.env)["R2_BACKUP_ENABLED"], "false")
        self.assertTrue((self.units / backup.SERVICE).exists())
        self.assertEqual([call[2] for call in self.manager.mutations()], ["stop", "disable"])
        self.report_unchanged()

    def test_credential_change_preserves_manually_paused_timer(self):
        values = self.existing()
        self.application().save(dict(values, S3_SECRET_ACCESS_KEY="new-private-secret"))
        self.assertEqual(self.manager.mutations(), [])
        self.assertEqual(self.manager.state(backup.TIMER), ("inactive", "disabled"))
        self.report_unchanged()

    def test_backup_time_change_preserves_active_state_with_calendar_guards(self):
        values = self.existing(active="active", enabled="enabled")
        self.application().save(dict(values, R2_BACKUP_TIME="23:45"))
        self.assertIn("23:45:00 UTC", (self.units / backup.TIMER).read_text())
        self.assertEqual(self.manager.state(backup.TIMER), ("active", "enabled"))
        self.assertTrue(any(call[:2] == ["systemd-analyze", "calendar"] for call in self.manager.calls))
        self.assertEqual([call[2] for call in self.manager.mutations()], ["daemon-reload"])
        self.report_unchanged()

    def test_install_mode_saves_only_draft_without_manager_calls(self):
        self.application(install=True).save(self.enabled())
        self.no_backup_files()
        self.assertEqual(self.manager.calls, [])
        self.assertEqual(read_env(self.env)["R2_BACKUP_ENABLED"], "true")

    def fresh_install(self, *, enabled, allow_enable):
        for path in self.report_files:
            path.unlink()
        values = self.enabled() if enabled else self.values
        write_env(self.env, values)
        return native_install.install_units(ROOT, self.env, self.units, self.manager, enable_backup=allow_enable)

    def test_fresh_install_default_disabled_creates_no_backup_units(self):
        self.assertTrue(self.fresh_install(enabled=False, allow_enable=True))
        self.no_backup_files()
        self.assertFalse(any(call[2] in ("enable", "start") for call in self.manager.mutations()))

    def test_fresh_install_opt_in_activates_backup(self):
        self.assertTrue(self.fresh_install(enabled=True, allow_enable=True))
        self.assertEqual(self.manager.state(backup.TIMER), ("active", "enabled"))
        self.assertTrue((self.units / backup.SERVICE).exists())

    def test_no_enable_installs_opt_in_units_but_does_not_activate(self):
        self.assertTrue(self.fresh_install(enabled=True, allow_enable=False))
        self.assertEqual(self.manager.state(backup.TIMER), ("inactive", "disabled"))
        self.assertTrue((self.units / backup.SERVICE).exists())
        self.assertFalse(any(call[2] in ("enable", "start") for call in self.manager.mutations()))

    def test_reinstall_does_not_resume_a_manually_paused_backup(self):
        self.existing()
        native_install.install_units(ROOT, self.env, self.units, self.manager, enable_backup=True)
        self.assertEqual(self.manager.state(backup.TIMER), ("inactive", "disabled"))
        self.assertFalse(any(call[2] in ("enable", "start") for call in self.manager.mutations()))
        self.report_unchanged()

    def test_maintenance_does_not_implicitly_install_missing_opt_in_timer(self):
        write_env(self.env, self.enabled())
        native_install.install_units(ROOT, self.env, self.units, self.manager, enable_backup=True)
        self.no_backup_files()
        self.assertIn("install-backup", self.output.getvalue())
        self.assertFalse(any(call[2] in ("enable", "start") for call in self.manager.mutations()))

    def test_explicit_install_backed_by_valid_configuration_is_supported(self):
        values = self.enabled()
        write_env(self.env, values)
        plan = backup.BackupPlan(ROOT, self.env, self.units, values, values,
                                 runner=self.manager, install=True, explicit=True)
        backup.commit_plan({}, plan, self.manager)
        self.assertEqual(self.manager.state(backup.TIMER), ("active", "enabled"))
        self.report_unchanged()

    def test_enable_or_start_failure_restores_env_units_and_activation(self):
        for failing_action in ("enable", "start"):
            with self.subTest(action=failing_action):
                self.manager.states.clear()
                self.manager.calls.clear()
                original = self.env.read_bytes()
                self.manager.fail_after.add(failing_action)
                with self.assertRaises(ApplyError):
                    self.application().save(self.enabled())
                self.assertEqual(self.env.read_bytes(), original)
                self.no_backup_files()
                self.assertEqual(self.manager.state(backup.TIMER)[0], "inactive")
                self.assertNotIn(self.manager.state(backup.TIMER)[1], ("enabled", "enabled-runtime"))
                self.report_unchanged()

    def test_disable_failure_restores_previous_env_and_timer_state(self):
        values = self.existing(active="active", enabled="enabled")
        original = self.env.read_bytes()
        self.manager.fail_after.add("stop")
        with self.assertRaises(ApplyError):
            self.application().save(dict(values, R2_BACKUP_ENABLED="false"))
        self.assertEqual(self.env.read_bytes(), original)
        self.assertEqual(self.manager.state(backup.TIMER), ("active", "enabled"))
        self.report_unchanged()

    def test_reload_failure_rolls_back_before_any_activation(self):
        original = self.env.read_bytes()
        self.manager.fail_after.add("daemon-reload")
        with self.assertRaises(ApplyError):
            self.application().save(self.enabled())
        self.assertEqual(self.env.read_bytes(), original)
        self.no_backup_files()
        self.assertFalse(any(call[2] in ("enable", "start") for call in self.manager.mutations()))
        self.report_unchanged()

    def test_unit_write_failure_restores_saved_env(self):
        original = self.env.read_bytes()
        real_write = native_config.atomic_write
        def write(path, data, *args, **kwargs):
            if path == self.units / backup.SERVICE:
                raise PermissionError("synthetic write failure")
            return real_write(path, data, *args, **kwargs)
        with patch.object(native_config, "atomic_write", side_effect=write):
            with self.assertRaises(ApplyError):
                self.application().save(self.enabled())
        self.assertEqual(self.env.read_bytes(), original)
        self.no_backup_files()
        self.report_unchanged()

    def test_foreign_or_modified_units_are_rejected_before_env_change(self):
        values = self.existing()
        path = self.units / backup.SERVICE
        path.write_text(path.read_text() + "ExecStart=/bin/false\n")
        original = self.env.read_bytes()
        with self.assertRaises(ApplyError):
            self.application().save(dict(values, R2_BACKUP_TIME="23:42"))
        self.assertEqual(self.env.read_bytes(), original)
        self.assertEqual(self.manager.mutations(), [])
        self.assertIn("/bin/false", path.read_text())

    def test_symlink_unit_is_rejected(self):
        outside = self.root / "unrelated"
        outside.write_text("unrelated")
        (self.units / backup.SERVICE).symlink_to(outside)
        original = self.env.read_bytes()
        with self.assertRaises(ConfigError):
            self.application().save(self.enabled())
        self.assertEqual(self.env.read_bytes(), original)
        self.assertEqual(outside.read_text(), "unrelated")
        self.assertEqual(self.manager.mutations(), [])

    def test_disk_dropin_is_rejected_before_env_save(self):
        dropin = self.units / (backup.TIMER + ".d")
        dropin.mkdir()
        (dropin / "custom.conf").write_text("[Timer]\nOnBootSec=1s\n")
        original = self.env.read_bytes()
        with self.assertRaises(ApplyError):
            self.application().save(self.enabled())
        self.assertEqual(self.env.read_bytes(), original)
        self.assertEqual(self.manager.mutations(), [])

    def test_foreign_effective_fragment_or_dropin_is_rejected(self):
        for extra in ({"FragmentPath": "/unrelated/backup.timer"}, {"DropInPaths": "/unrelated/override.conf"}):
            with self.subTest(property=next(iter(extra))):
                self.manager.properties[backup.TIMER] = extra
                with self.assertRaises(ApplyError):
                    self.application().save(self.enabled())
                self.assertEqual(self.manager.mutations(), [])
        self.no_backup_files()

    def test_busy_service_is_rejected_without_stopping_upload(self):
        values = self.existing(active="active", enabled="enabled")
        original = self.env.read_bytes()
        self.manager.properties[backup.SERVICE] = {"ActiveState": "activating"}
        with self.assertRaises(ApplyError):
            self.application().save(dict(values, R2_BACKUP_ENABLED="false"))
        self.assertEqual(self.env.read_bytes(), original)
        self.assertEqual(self.manager.mutations(), [])

    def test_unwritable_unit_directory_is_rejected_before_env_change(self):
        original = self.env.read_bytes()
        self.units.chmod(0o555)
        try:
            with self.assertRaises(ApplyError):
                self.application().save(self.enabled())
        finally:
            self.units.chmod(0o700)
        self.assertEqual(self.env.read_bytes(), original)
        self.assertEqual(self.manager.mutations(), [])

    def test_modified_unit_between_plan_and_commit_is_preserved(self):
        values = self.existing()
        plan = backup.BackupPlan(ROOT, self.env, self.units, values, dict(values, R2_BACKUP_TIME="23:42"), runner=self.manager)
        path = self.units / backup.TIMER
        path.write_text(path.read_text() + "# external change\n")
        with self.assertRaises(ApplyError):
            backup.commit_plan({}, plan, self.manager)
        self.assertIn("# external change", path.read_text())
        self.assertEqual(self.manager.mutations(), [])

    def test_unit_change_after_reload_cannot_be_activated(self):
        original = self.env.read_bytes()
        path = self.units / backup.SERVICE
        def change():
            path.write_text(path.read_text() + "ExecStart=/bin/false\n")
        self.manager.reload_hook = change
        with self.assertRaises(ApplyError):
            self.application().save(self.enabled())
        self.assertEqual(self.env.read_bytes(), original)
        self.assertIn("/bin/false", path.read_text())
        self.assertFalse(any(call[2] in ("enable", "start") for call in self.manager.mutations()))

    def test_unrelated_active_timer_risk_blocks_backup_install(self):
        original = self.env.read_bytes()
        self.manager.states["trendradar-lite.timer"] = ("active", "enabled")
        self.manager.unsafe = True
        with self.assertRaisesRegex(ApplyError, "补跑"):
            self.application().save(self.enabled())
        self.assertEqual(self.env.read_bytes(), original)
        self.no_backup_files()
        self.assertEqual(self.manager.mutations(), [])
        self.report_unchanged()

    def test_uninstall_only_owned_backup_units_and_preserves_env_reports(self):
        self.existing(active="active", enabled="enabled")
        original = self.env.read_bytes()
        backup.uninstall(ROOT, self.env, self.units, self.manager)
        self.no_backup_files()
        self.assertEqual(self.env.read_bytes(), original)
        self.assertEqual(self.manager.state(backup.TIMER), ("inactive", "disabled"))
        self.report_unchanged()

    def test_uninstall_foreign_unit_fails_before_any_change(self):
        self.existing(active="active", enabled="enabled")
        path = self.units / backup.SERVICE
        path.write_text("# somebody else's service\n")
        with self.assertRaises(ApplyError):
            backup.uninstall(ROOT, self.env, self.units, self.manager)
        self.assertEqual(path.read_text(), "# somebody else's service\n")
        self.assertEqual(self.manager.mutations(), [])

    def test_uninstall_checks_unrelated_active_timer_before_stop_or_delete(self):
        self.existing(active="active", enabled="enabled")
        self.manager.states["trendradar-lite.timer"] = ("active", "enabled")
        self.manager.unsafe = True
        with self.assertRaisesRegex(ApplyError, "补跑"):
            backup.uninstall(ROOT, self.env, self.units, self.manager)
        self.assertTrue((self.units / backup.SERVICE).exists())
        self.assertEqual(self.manager.mutations(), [])

    def test_uninstall_partial_unlink_failure_restores_files_and_state(self):
        self.existing(active="active", enabled="enabled")
        original = {path: path.read_bytes() for path in (self.units / backup.SERVICE, self.units / backup.TIMER)}
        real_unlink = Path.unlink
        failed = False
        def unlink(path, *args, **kwargs):
            nonlocal failed
            if path == self.units / backup.TIMER and not failed:
                failed = True
                raise PermissionError("synthetic unlink failure")
            return real_unlink(path, *args, **kwargs)
        with patch.object(Path, "unlink", unlink):
            with self.assertRaises((ApplyError, OSError)):
                backup.uninstall(ROOT, self.env, self.units, self.manager)
        for path, content in original.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertEqual(self.manager.state(backup.TIMER), ("active", "enabled"))
        self.report_unchanged()

    def test_uninstall_reload_failure_restores_files_and_state(self):
        self.existing(active="active", enabled="enabled")
        original = {path: path.read_bytes() for path in (self.units / backup.SERVICE, self.units / backup.TIMER)}
        self.manager.fail_after.add("daemon-reload")
        with self.assertRaises((ApplyError, OSError)):
            backup.uninstall(ROOT, self.env, self.units, self.manager)
        for path, content in original.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertEqual(self.manager.state(backup.TIMER), ("active", "enabled"))
        self.report_unchanged()

    def test_status_needs_no_credentials_and_does_not_mutate(self):
        values = self.values | {"R2_BACKUP_ENABLED": "true"}
        result = backup.status(ROOT, self.env, self.units, values, self.manager)
        self.assertIn("timer 未安装", result)
        self.assertEqual(self.manager.mutations(), [])
        self.no_backup_files()

    def test_missing_active_report_file_is_guarded_before_backup_save(self):
        for name in native_config.TIMER_NAMES:
            with self.subTest(name=name):
                path = self.units / name
                path.unlink()
                self.manager.states[name] = ("active", "enabled")
                self.manager.properties[name] = {"FragmentPath": str(path)}
                original = self.env.read_bytes()
                with self.assertRaises(ApplyError):
                    self.application().save(self.enabled())
                self.assertEqual(self.env.read_bytes(), original)
                self.assertFalse(path.exists())
                self.no_backup_files()
                self.assertEqual(self.manager.mutations(), [])
                self.assertTrue(any(call[2:4] == ["show", name] for call in self.manager.calls if call[0] == "systemctl"))
                path.write_bytes(self.report_files[path])
                self.manager.states.pop(name)
                self.manager.properties.pop(name)

    def test_missing_report_loaded_from_another_path_is_rejected(self):
        path = self.units / native_config.TIMER_NAMES[0]
        path.unlink()
        self.manager.properties[path.name] = {"FragmentPath": str(self.root / "other" / path.name)}
        original = self.env.read_bytes()
        with self.assertRaises(ApplyError):
            self.application().save(self.enabled())
        self.assertEqual(self.env.read_bytes(), original)
        self.assertEqual(self.manager.mutations(), [])

    def test_missing_inactive_report_guards_do_not_create_placeholder_units(self):
        for path in self.report_files:
            path.unlink()
        self.application().save(self.enabled())
        for path in self.report_files:
            self.assertFalse(path.exists())
        self.assertEqual(self.manager.state(backup.TIMER), ("active", "enabled"))

    def test_installer_rejects_missing_but_manager_active_report_unit(self):
        for path in self.report_files:
            path.unlink()
        name = native_config.TIMER_NAMES[0]
        self.manager.states[name] = ("active", "enabled")
        self.manager.properties[name] = {"FragmentPath": str(self.units / name)}
        write_env(self.env, self.enabled())
        with self.assertRaises(ApplyError):
            native_install.install_units(ROOT, self.env, self.units, self.manager, enable_backup=True)
        self.no_backup_files()
        self.assertEqual(self.manager.mutations(), [])
        for path in self.report_files:
            self.assertFalse(path.exists())

    def test_uninstall_rejects_missing_active_report_before_backup_stop(self):
        self.existing(active="active", enabled="enabled")
        path = self.units / native_config.TIMER_NAMES[0]
        path.unlink()
        self.manager.states[path.name] = ("active", "enabled")
        self.manager.properties[path.name] = {"FragmentPath": str(path)}
        with self.assertRaises(ApplyError):
            backup.uninstall(ROOT, self.env, self.units, self.manager)
        self.assertTrue((self.units / backup.SERVICE).exists())
        self.assertEqual(self.manager.mutations(), [])

    def test_pending_user_control_service_override_is_rejected_before_save(self):
        values = self.existing(active="active", enabled="enabled")
        pending = Path(os.environ["XDG_CONFIG_HOME"]) / "systemd/user.control" / (backup.SERVICE + ".d")
        pending.mkdir(parents=True)
        (pending / "50-command.conf").write_text("[Service]\nExecStart=/bin/false\n")
        original = self.env.read_bytes()
        with self.assertRaises(ApplyError):
            self.application().save(dict(values, R2_BACKUP_TIME="23:45"))
        self.assertEqual(self.env.read_bytes(), original)
        self.assertEqual(self.manager.mutations(), [])

    def test_runtime_transient_and_generator_pending_overrides_are_checked(self):
        values = self.existing()
        for suffix in ("user.control", "transient", "generator.early", "generator", "generator.late"):
            with self.subTest(path=suffix):
                pending = Path(os.environ["XDG_RUNTIME_DIR"]) / "systemd" / suffix / (backup.SERVICE + ".d")
                pending.mkdir(parents=True)
                override = pending / "50-command.conf"
                override.write_text("[Service]\nExecStart=/bin/false\n")
                with self.assertRaises(ApplyError):
                    self.application().save(dict(values, R2_BACKUP_TIME="23:45"))
                self.assertEqual(self.manager.mutations(), [])
                override.unlink()

    def test_default_xdg_config_dirs_are_checked_when_unset(self):
        values = self.existing()
        pending = Path("/etc/xdg/systemd/user") / (backup.SERVICE + ".d")
        real_exists, real_is_dir, real_glob = Path.exists, Path.is_dir, Path.glob
        with patch.dict(os.environ, {"XDG_CONFIG_DIRS": ""}):
            with patch.object(Path, "exists", lambda path: True if path == pending else real_exists(path)):
                with patch.object(Path, "is_dir", lambda path: True if path == pending else real_is_dir(path)):
                    with patch.object(Path, "glob", lambda path, pattern: iter([pending / "50.conf"]) if path == pending else real_glob(path, pattern)):
                        with self.assertRaises(ApplyError):
                            self.application().save(dict(values, R2_BACKUP_TIME="23:45"))
        self.assertEqual(self.manager.mutations(), [])

    def test_lookup_tool_paths_include_nonstandard_pending_override(self):
        values = self.existing()
        directory = self.root / "nonstandard-manager-units"
        self.manager.unit_paths.append(str(directory))
        pending = directory / (backup.SERVICE + ".d")
        pending.mkdir(parents=True)
        (pending / "50.conf").write_text("[Service]\nExecStart=/bin/false\n")
        with self.assertRaises(ApplyError):
            self.application().save(dict(values, R2_BACKUP_TIME="23:45"))
        self.assertEqual(self.manager.mutations(), [])

    def test_lookup_tool_failure_fails_before_env_write(self):
        self.manager.unit_paths_error = True
        original = self.env.read_bytes()
        with self.assertRaises(ApplyError):
            self.application().save(self.enabled())
        self.assertEqual(self.env.read_bytes(), original)
        self.no_backup_files()
        self.assertEqual(self.manager.mutations(), [])

    def test_maintenance_rechecks_effective_service_override_after_reload(self):
        values = self.existing()
        original = self.env.read_bytes()
        def override():
            self.manager.properties[backup.SERVICE] = {"DropInPaths": "/unexpected/generated.conf"}
        self.manager.reload_hook = override
        with self.assertRaises(ApplyError):
            self.application().save(dict(values, R2_BACKUP_TIME="23:45"))
        self.assertEqual(self.env.read_bytes(), original)
        self.assertFalse(any(call[2] in ("enable", "start") for call in self.manager.mutations()))

    def test_pending_same_named_fragment_in_higher_priority_path_is_rejected(self):
        values = self.existing()
        directory = Path(os.environ["XDG_CONFIG_HOME"]) / "systemd/user.control"
        directory.mkdir(parents=True)
        (directory / backup.SERVICE).write_text("[Service]\nExecStart=/bin/false\n")
        with self.assertRaises(ApplyError):
            self.application().save(dict(values, R2_BACKUP_TIME="23:45"))
        self.assertEqual(self.manager.mutations(), [])

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd parser not installed")
    def test_generated_units_parse_with_real_systemd_without_starting(self):
        app = self.root / "checkout with spaces"
        shutil.copytree(ROOT / "deploy/systemd", app / "deploy/systemd")
        python = app / ".venv/bin/python"
        python.parent.mkdir(parents=True)
        python.symlink_to(sys.executable)
        for path, data in backup.render_units(app, self.env, self.units, backup.settings(self.enabled())).items():
            path.write_bytes(data)
        result = subprocess.run(["systemd-analyze", "verify", str(self.units / backup.SERVICE), str(self.units / backup.TIMER)],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
