"""Offline regressions for the native total uninstaller; never use a real manager."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))
from native_config import ApplyError
from native_uninstall import remove_reports

REPORT_UNITS = tuple(f"trendradar-{name}.{suffix}"
                     for name in ("lite", "weekly") for suffix in ("service", "timer"))


class NativeUninstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.app = self.root / "checkout with spaces"
        shutil.copytree(ROOT / "deploy", self.app / "deploy", ignore=shutil.ignore_patterns("__pycache__"))
        self.home = self.root / "home"
        self.config = self.home / ".config"
        self.units = self.config / "systemd/user"
        self.units.mkdir(parents=True)
        self.original = {name: (b"[Timer]\nOnCalendar=*-*-* 18:00:00 UTC\nPersistent=true\n"
                               if name.endswith(".timer") else f"# original {name}\n".encode())
                         for name in REPORT_UNITS}
        for name, content in self.original.items():
            (self.units / name).write_bytes(content)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "calls.jsonl"
        self.environment = {
            "HOME": str(self.home), "XDG_CONFIG_HOME": str(self.config),
            "PATH": str(self.bin) + os.pathsep + os.defpath,
            "PYTHON_BIN": sys.executable, "PYTHONDONTWRITEBYTECODE": "1",
            "FAKE_LOG": str(self.log), "FAKE_UNITS": str(self.units), "LANG": "C.UTF-8",
            "FAKE_MODE": "success",
        }
        (self.bin / "systemctl").write_text(f'''#!{sys.executable}
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
logpath = Path(os.environ["FAKE_LOG"])
with logpath.open("a") as log:
    log.write(json.dumps(args) + "\\n")
units = Path(os.environ["FAKE_UNITS"])
mode = os.environ["FAKE_MODE"]
disabled = logpath.with_suffix(".disabled")
reloaded = logpath.with_suffix(".reloaded")
if "disable" in args:
    if "--no-reload" not in args or "--now" not in args:
        raise SystemExit("unsafe disable invocation")
    if mode == "disable-fail":
        raise SystemExit(1)
    disabled.touch()
    if mode == "stop-fail-after-disable":
        raise SystemExit(1)
    if mode == "concurrent-edit":
        (units / "trendradar-lite.timer").write_text("external edit\\n")
    raise SystemExit(0)
if "show" in args:
    name = args[args.index("show") + 1]
    if mode == "initial-show-fail" or (mode == "show-fail" and disabled.exists()):
        print("simulated manager unavailable", file=sys.stderr)
        raise SystemExit(1)
    exists = (units / name).exists()
    props = dict(LoadState="loaded" if exists else "not-found", ActiveState="inactive",
                 SubState="dead", UnitFileState="disabled" if exists else "",
                 FragmentPath=str(units / name) if exists else "", DropInPaths="",
                 LastTriggerUSec="Thu 2026-01-01 00:00:00 UTC", NeedDaemonReload="no")
    active = (mode in ("active-success", "overdue-report") and not disabled.exists()
              and name == "trendradar-lite.timer")
    active |= mode == "missing-active-report" and name == "trendradar-lite.timer"
    active |= mode == "missing-active-backup" and name == "trendradar-r2-backup.timer"
    active |= mode in ("backup-risk-after-disable", "backup-risk-recovery") and name == "trendradar-r2-backup.timer"
    target = os.environ.get("FAKE_TARGET", "trendradar-lite.timer")
    active |= mode == "stay-active" and disabled.exists() and name == target
    active |= mode == "reload-reactivate" and reloaded.exists() and name == target
    if active:
        props.update(LoadState="loaded", ActiveState="active", SubState="waiting",
                     UnitFileState="enabled", FragmentPath=str(units / name))
    if mode == "stay-enabled" and disabled.exists() and name == target:
        props["UnitFileState"] = os.environ.get("FAKE_ENABLED", "enabled")
    if mode == "foreign-fragment" and name == target:
        props["FragmentPath"] = str(units.parent / name)
    if mode == "effective-dropin" and name == target:
        props["DropInPaths"] = str(units / (name + ".d/override.conf"))
    if disabled.exists() and mode == "incomplete-show":
        props.pop(os.environ["FAKE_MISSING_FIELD"])
    if disabled.exists() and mode == "unknown-state" and name == target:
        props["ActiveState"] = "deactivating"
    print("\\n".join(key + "=" + value for key, value in props.items()))
    raise SystemExit(1 if props["LoadState"] == "not-found" else 0)
if "daemon-reload" in args:
    first = not reloaded.exists()
    reloaded.touch()
    raise SystemExit(1 if mode in ("reload-fail", "backup-risk-recovery") and first else 0)
raise SystemExit("unexpected fake systemctl call")
''')
        (self.bin / "systemctl").chmod(0o755)
        (self.bin / "systemd-analyze").write_text(f'''#!{sys.executable}
import json, os, sys
from pathlib import Path
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps(["systemd-analyze", *sys.argv[1:]]) + "\\n")
assert sys.argv[1:4] == ["calendar", "--no-pager", "--iterations=1"]
date = "Fri 2100-01-01 00:00:00 UTC"
mode = os.environ["FAKE_MODE"]
logpath = Path(os.environ["FAKE_LOG"])
unsafe = mode == "overdue-report"
unsafe |= mode == "backup-risk-after-disable" and logpath.with_suffix(".disabled").exists()
unsafe |= mode == "backup-risk-recovery" and logpath.with_suffix(".reloaded").exists()
if unsafe and not sys.argv[4].startswith("--base-time=@"):
    date = "Thu 2026-01-01 18:00:00 UTC"
print("\\n\\n".join("Next elapse: " + date for calendar in sys.argv[5:]))
''')
        (self.bin / "systemd-analyze").chmod(0o755)
        # Fake data only. A failed --purge-data run must never reach these.
        self.envpath = self.config / "trendradar-lite/env"
        self.envpath.parent.mkdir()
        self.envpath.write_text("# fake data, not credentials\n")
        self.backups = self.envpath.parent / ".env.backups"
        self.backups.mkdir()
        (self.backups / "keep").write_text("fake backup\n")
        (self.app / "output").mkdir()
        (self.app / "output/keep").write_text("fake output\n")
        self.launcher = self.home / ".local/bin/trendradar"
        self.launcher.parent.mkdir(parents=True)
        from native_install import launcher_content
        self.launcher.write_bytes(launcher_content(self.app, self.envpath, self.units, sys.executable))

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def run_uninstall(self, *args):
        return subprocess.run(["bash", str(self.app / "deploy/linux/uninstall.sh"), *args],
                              cwd=self.root, env=self.environment, input="", text=True,
                              capture_output=True, timeout=15)

    def runner(self, command, **kwargs):
        kwargs["env"] = dict(self.environment, **{key: value for key, value in kwargs.get("env", {}).items()
                                                 if key in ("LC_ALL", "TZ", "SYSTEMD_COLORS")})
        return subprocess.run(command, **kwargs)

    def assert_preserved(self):
        for name, content in self.original.items():
            self.assertEqual((self.units / name).read_bytes(), content)
        self.assertTrue(self.launcher.exists())
        self.assertTrue(self.envpath.exists())
        self.assertTrue((self.backups / "keep").exists())
        self.assertTrue((self.app / "output/keep").exists())

    def assert_no_reload(self):
        self.assertFalse(any("daemon-reload" in call for call in self.calls()), self.calls())

    def assert_no_start(self):
        self.assertFalse(any("start" in call or "enable" in call or "restart" in call for call in self.calls()), self.calls())

    def test_disable_failure_preserves_all_report_units_without_reload(self):
        self.environment["FAKE_MODE"] = "disable-fail"
        result = self.run_uninstall("--purge-data")
        summary = {"returncode": result.returncode,
                   "remaining": [name for name in REPORT_UNITS if (self.units / name).exists()],
                   "calls": self.calls()}
        self.assertNotEqual(result.returncode, 0, summary)
        self.assert_preserved()
        self.assert_no_reload()
        self.assert_no_start()

    def test_stop_failure_after_disable_preserves_units_and_never_restarts(self):
        self.environment["FAKE_MODE"] = "stop-fail-after-disable"
        result = self.run_uninstall()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_preserved()
        self.assert_no_reload()
        self.assert_no_start()

    def test_successful_command_must_confirm_both_timers_stopped_and_disabled(self):
        for mode in ("stay-active", "stay-enabled"):
            for name in ("trendradar-lite.timer", "trendradar-weekly.timer"):
                with self.subTest(mode=mode, timer=name):
                    self.environment.update(FAKE_MODE=mode, FAKE_TARGET=name)
                    result = self.run_uninstall("--purge-data")
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assert_preserved()
                    self.assert_no_reload()

    def test_runtime_enablement_is_not_disabled(self):
        self.environment.update(FAKE_MODE="stay-enabled", FAKE_ENABLED="enabled-runtime")
        self.assertNotEqual(self.run_uninstall().returncode, 0)
        self.assert_preserved()
        self.assert_no_reload()

    def test_show_failures_are_not_mistaken_for_missing_units(self):
        for mode in ("initial-show-fail", "show-fail"):
            with self.subTest(mode=mode):
                self.environment["FAKE_MODE"] = mode
                self.assertNotEqual(self.run_uninstall().returncode, 0)
                self.assert_preserved()
                self.assert_no_reload()

    def test_incomplete_or_unknown_state_after_disable_fails_closed(self):
        for field in ("ActiveState", "UnitFileState", "FragmentPath", "DropInPaths"):
            with self.subTest(missing=field):
                self.environment.update(FAKE_MODE="incomplete-show", FAKE_MISSING_FIELD=field)
                self.assertNotEqual(self.run_uninstall().returncode, 0)
                self.assert_preserved()
                self.assert_no_reload()
        self.environment["FAKE_MODE"] = "unknown-state"
        self.assertNotEqual(self.run_uninstall().returncode, 0)
        self.assert_preserved()
        self.assert_no_reload()

    def test_success_uses_no_reload_disable_and_confirms_states_before_reload(self):
        result = self.run_uninstall()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(any((self.units / name).exists() for name in REPORT_UNITS))
        self.assertFalse(self.launcher.exists())
        self.assertTrue(self.envpath.exists())
        calls = self.calls()
        disable = next(i for i, call in enumerate(calls) if "disable" in call)
        reload = next(i for i, call in enumerate(calls) if "daemon-reload" in call)
        self.assertEqual(calls[disable], ["--user", "disable", "--no-reload", "--now",
                                          "trendradar-lite.timer", "trendradar-weekly.timer"])
        for name in ("trendradar-lite.timer", "trendradar-weekly.timer"):
            self.assertTrue(any("show" in call and name in call for call in calls[disable + 1:reload]))
        self.assert_no_start()
        # A complete not-found state (show rc=1) is a safe, repeatable no-op.
        again = self.run_uninstall()
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual(sum("disable" in call for call in self.calls()), 1)

    def test_partially_missing_install_only_disables_present_timer(self):
        (self.units / "trendradar-lite.timer").unlink()
        result = self.run_uninstall()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(any((self.units / name).exists() for name in REPORT_UNITS))
        self.assertEqual([call for call in self.calls() if "disable" in call], [
            ["--user", "disable", "--no-reload", "--now", "trendradar-weekly.timer"]])

    def test_both_timers_verified_after_disable_before_first_unlink(self):
        original_unlink = Path.unlink
        checked = []

        def check_order(path, *args, **kwargs):
            if path.name in REPORT_UNITS and not checked:
                calls = self.calls()
                disabled = next(i for i, call in enumerate(calls) if "disable" in call)
                for name in ("trendradar-lite.timer", "trendradar-weekly.timer"):
                    self.assertTrue(any("show" in call and name in call for call in calls[disabled + 1:]))
                self.assertTrue(all((self.units / name).exists() for name in REPORT_UNITS))
                checked.append(True)
            return original_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", check_order):
            remove_reports(self.units, self.runner)
        self.assertEqual(checked, [True])

    def test_active_report_is_calendar_guarded_then_stopped_without_start(self):
        self.environment["FAKE_MODE"] = "active-success"
        result = self.run_uninstall()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(any(call[0] == "systemd-analyze" for call in self.calls()))
        self.assert_no_start()

    def test_overdue_report_is_rejected_by_original_calendar_gate(self):
        self.environment["FAKE_MODE"] = "overdue-report"
        result = self.run_uninstall()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("补跑", result.stderr)
        self.assert_preserved()
        self.assert_no_reload()
        self.assertFalse(any("disable" in call for call in self.calls()))

    def test_missing_active_manager_timer_is_not_skipped(self):
        for mode in ("missing-active-report", "missing-active-backup"):
            with self.subTest(mode=mode):
                self.environment["FAKE_MODE"] = mode
                name = "trendradar-lite.timer"
                if mode == "missing-active-report":
                    (self.units / name).unlink()
                result = self.run_uninstall()
                self.assertNotEqual(result.returncode, 0)
                self.assert_no_reload()
                self.assertFalse(any("disable" in call for call in self.calls()))
                if mode == "missing-active-report":
                    self.assertFalse((self.units / name).exists())
                    (self.units / name).write_bytes(self.original[name])
                self.assert_preserved()

    def test_foreign_fragment_and_effective_dropin_fail_before_disable(self):
        for mode in ("foreign-fragment", "effective-dropin"):
            with self.subTest(mode=mode):
                self.environment["FAKE_MODE"] = mode
                self.assertNotEqual(self.run_uninstall().returncode, 0)
                self.assert_preserved()
                self.assert_no_reload()
                self.assertFalse(any("disable" in call for call in self.calls()))

    def test_concurrent_edit_after_disable_is_not_deleted_or_overwritten(self):
        self.environment["FAKE_MODE"] = "concurrent-edit"
        self.assertNotEqual(self.run_uninstall().returncode, 0)
        self.assertEqual((self.units / "trendradar-lite.timer").read_text(), "external edit\n")
        self.assertTrue(all((self.units / name).exists() for name in REPORT_UNITS))
        self.assert_no_reload()
        self.assert_no_start()

    def test_reload_failure_restores_unit_bytes_without_restarting_reports(self):
        self.environment["FAKE_MODE"] = "reload-fail"
        result = self.run_uninstall("--purge-data")
        self.assertNotEqual(result.returncode, 0)
        self.assert_preserved()
        self.assertEqual(sum("daemon-reload" in call for call in self.calls()), 2)
        self.assert_no_start()

    def test_reactivation_blocks_recovery_reload(self):
        self.environment["FAKE_MODE"] = "reload-reactivate"
        result = self.run_uninstall()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("恢复未确认", result.stderr)
        self.assert_preserved()
        self.assertEqual(sum("daemon-reload" in call for call in self.calls()), 1)
        self.assert_no_start()

    def test_other_active_timer_gate_is_rechecked_after_disable_and_for_recovery(self):
        backup = self.units / "trendradar-r2-backup.timer"
        backup.write_bytes(self.original["trendradar-lite.timer"])
        for mode, reloads in (("backup-risk-after-disable", 0), ("backup-risk-recovery", 1)):
            with self.subTest(mode=mode):
                for path in (self.log, self.log.with_suffix(".disabled"), self.log.with_suffix(".reloaded")):
                    path.unlink(missing_ok=True)
                self.environment["FAKE_MODE"] = mode
                with self.assertRaises(ApplyError):
                    remove_reports(self.units, self.runner)
                self.assert_preserved()
                self.assertEqual(backup.read_bytes(), self.original["trendradar-lite.timer"])
                self.assertEqual(sum("daemon-reload" in call for call in self.calls()), reloads)
                self.assertFalse(any("disable" in call and backup.name in call for call in self.calls()))
                self.assert_no_start()

    def test_foreign_r2_unit_rejection_does_not_touch_reports_or_purge(self):
        backup = self.units / "trendradar-r2-backup.service"
        backup.write_text("# foreign backup service\n")
        result = self.run_uninstall("--purge-data")
        self.assertNotEqual(result.returncode, 0)
        self.assert_preserved()
        self.assertEqual(backup.read_text(), "# foreign backup service\n")
        self.assert_no_reload()
        self.assertFalse(any("disable" in call for call in self.calls()))

    def test_disable_timeout_preserves_units_without_reload_or_restart(self):
        def timeout(command, **kwargs):
            if "disable" in command:
                raise subprocess.TimeoutExpired(command, 10)
            return self.runner(command, **kwargs)
        with self.assertRaises(ApplyError):
            remove_reports(self.units, timeout)
        self.assert_preserved()
        self.assert_no_reload()
        self.assert_no_start()

    def test_partial_unlink_failure_restores_units_without_reload_or_restart(self):
        original_unlink = Path.unlink

        def fail_second(path, *args, **kwargs):
            if path == self.units / REPORT_UNITS[1]:
                raise PermissionError("simulated unlink failure")
            return original_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", fail_second), self.assertRaises(ApplyError):
            remove_reports(self.units, self.runner)
        self.assert_preserved()
        self.assert_no_reload()
        self.assert_no_start()

    def test_unlink_failure_does_not_overwrite_external_replacement(self):
        original_unlink = Path.unlink

        def fail_second(path, *args, **kwargs):
            if path == self.units / REPORT_UNITS[1]:
                (self.units / REPORT_UNITS[0]).write_text("external replacement\n")
                raise PermissionError("simulated unlink failure")
            return original_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", fail_second), self.assertRaisesRegex(ApplyError, "恢复未确认"):
            remove_reports(self.units, self.runner)
        self.assertEqual((self.units / REPORT_UNITS[0]).read_text(), "external replacement\n")
        for name in REPORT_UNITS[1:]:
            self.assertEqual((self.units / name).read_bytes(), self.original[name])
        self.assert_no_reload()
        self.assert_no_start()

    def test_reload_timeout_restores_files_using_guarded_reload_only(self):
        timed_out = []

        def timeout_once(command, **kwargs):
            result = self.runner(command, **kwargs)
            if "daemon-reload" in command and not timed_out:
                timed_out.append(True)
                raise subprocess.TimeoutExpired(command, 10)
            return result

        with self.assertRaises(ApplyError):
            remove_reports(self.units, timeout_once)
        self.assert_preserved()
        self.assertEqual(sum("daemon-reload" in call for call in self.calls()), 2)
        self.assert_no_start()


if __name__ == "__main__":
    unittest.main()
