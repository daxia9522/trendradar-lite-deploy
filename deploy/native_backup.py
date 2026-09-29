"""Optional native backup units and deliberately separate activation.

A saved false -> true transition authorizes starting only the backup timer.
Other saves and reinstalls preserve a manually paused timer. Unit file changes
join the caller's ONE UnitTransaction, including the unchanged daily/weekly
calendars: daemon-reload must never bypass their existing catch-up safety gate.
"""
from __future__ import annotations

import hashlib
import os
import re
import shlex
import subprocess
from pathlib import Path

from backup_settings import BackupConfigError, load_backup_settings
from envfile import ConfigError, atomic_write, snapshot
from native_config import ApplyError, COMMAND_TIMEOUT, TIMER_NAMES, UnitTransaction

NAME = "trendradar-r2-backup"
TIMER = NAME + ".timer"
SERVICE = NAME + ".service"


def settings(values, *, credentials=True):
    try:
        return load_backup_settings(values, require_credentials=credentials)
    except BackupConfigError as error:
        raise ConfigError(str(error)) from None


def owner(app: Path, env: Path, units: Path) -> str:
    identity = hashlib.sha256(f"{app}\0{env}\0{units}".encode()).hexdigest()
    return f"# trendradar-r2-backup owner:{identity}\n"


def render_units(app, env, units, configured):
    from native_install import unit_path
    result = {}
    for suffix in ("service", "timer"):
        text = (app / f"deploy/systemd/{NAME}.{suffix}.in").read_text()
        text = text.replace("@APP_DIR@", unit_path(app)).replace("@ENV_FILE@", unit_path(env))
        text = text.replace("@PYTHON@", unit_path(app / ".venv/bin/python", executable=True))
        text = text.replace("@CALENDAR@", f"*-*-* {configured.time}:00 {configured.timezone}")
        result[units / f"{NAME}.{suffix}"] = (owner(app, env, units) + text).encode()
    return result


def _writable(path):
    """Check replace/mkdir prerequisites before env changes, without probe files."""
    candidate = path
    while not candidate.exists():
        if candidate.is_symlink():
            raise ApplyError("备份 unit 目录含符号链接；拒绝应用")
        candidate = candidate.parent
    if not candidate.is_dir() or not os.access(candidate, os.W_OK | os.X_OK):
        raise ApplyError("备份 unit 目录不可写；未保存配置")
    if not candidate.stat().st_mode & 0o222:
        raise ApplyError("备份 unit 目录不可写；未保存配置")
    for parent in (candidate, *candidate.parents):
        if parent.is_symlink():
            raise ApplyError("备份 unit 目录含符号链接；拒绝应用")


def guarded_timer_paths(units):
    return tuple(Path(units) / name for name in (*TIMER_NAMES, TIMER))


def guarded_changes(changes, units):
    """An eventual daemon-reload also coldplugs the existing report timers."""
    changes = dict(changes)
    for name in (*TIMER_NAMES, TIMER):
        path = units / name
        if path not in changes:
            before = snapshot(path)
            if before is not None:
                changes[path] = before[-1]
    return changes


class BackupPlan:
    def __init__(self, app, env, units, before, values, *, runner=None,
                 install=False, first_install=False, enable=False, explicit=False):
        self.app, self.env, self.units = Path(app), Path(env), Path(units)
        self.configured = settings(values)
        was_enabled = str(before.get("R2_BACKUP_ENABLED", "")).strip().lower() in ("true", "1")
        self.io = UnitTransaction({}, runner)
        self.paths = [self.units / SERVICE, self.units / TIMER]
        self.original = {path: snapshot(path) for path in self.paths}
        self.changes = {}
        self.states = {}
        self.touched = False
        self.action = None
        self.message = ""
        present = any(self.original.values())
        # Existing true is not a new grant of authority, including after an
        # --no-enable installation. Explicit install-backup --enable is a grant.
        activate = self.configured.enabled and (
            explicit or (first_install and enable and not present) or (not install and not was_enabled))
        create = self.configured.enabled and (activate or first_install)
        if install and not first_install and not explicit:
            create = False
        if not present and not create:
            if self.configured.enabled:
                command = shlex.join([str(self.app / ".venv/bin/python"),
                                      str(self.app / "deploy/native_install.py"), "install-backup", "--enable",
                                      "--app", str(self.app), "--output", str(self.env),
                                      "--unit-dir", str(self.units)])
                self.message = "备份配置已开启但 timer 尚未安装；未自动启用。明确安装/启用命令：" + command
            return
        self.expected = render_units(self.app, self.env, self.units, self.configured)
        self._validate_files()
        self._inspect(initial=True)
        if self.configured.enabled:
            self.changes = dict(self.expected)
            self.action = "enable" if activate else None
            self.message = ("备份 timer 已启用，仅等待将来的计划时间；未启动备份服务。" if activate else
                            "备份 timer 原启用/暂停状态已保留；未自动恢复人工暂停。")
        else:
            self.action = "disable"
            self.message = "备份 timer 已停止并禁用；后续运行入口也会检查关闭开关。已在途的上传不由此操作撤销。"
        if self.changes or self.action:
            _writable(self.units)
        self._check_wants()

    def _validate_files(self):
        """Marker AND exact shipped shape; edited commands/extra triggers fail closed."""
        for path, before in self.original.items():
            if before is None:
                continue
            data = before[-1]
            expected = self.expected[path]
            if path.name == TIMER:
                pattern = rb"(?m)^OnCalendar=\*-\*-\* (?:[01]\d|2[0-3]):[0-5]\d:00 [A-Za-z0-9_+./-]+$"
                data, count = re.subn(pattern, b"OnCalendar=<managed>", data)
                expected, _ = re.subn(pattern, b"OnCalendar=<managed>", expected)
                if count != 1:
                    raise ApplyError("备份 timer 不是受支持的 managed 计划；拒绝覆盖")
            if data != expected:
                raise ApplyError("备份 unit 不属于本 checkout/config 或已被手工修改；拒绝覆盖/启停")

    def _unit_paths(self):
        """Union the live lookup tool with defaults; pending overrides count too."""
        try:
            result = self.io.runner(
                ["systemd-analyze", "--user", "unit-paths"], capture_output=True, text=True,
                env=dict(os.environ, LC_ALL="C", SYSTEMD_COLORS="0"), timeout=COMMAND_TIMEOUT,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise ApplyError("无法只读核实 systemd 用户 unit 搜索路径") from None
        rows = result.stdout.splitlines()
        if result.returncode or not rows or any(not Path(row).is_absolute() for row in rows):
            raise ApplyError("systemd 用户 unit 搜索路径不完整或不可解析")
        directories = {Path(row) for row in rows}
        directories.update({self.units, Path("/etc/systemd/user"), Path("/run/systemd/user"),
                            Path("/usr/lib/systemd/user"), Path("/usr/local/lib/systemd/user")})
        config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
        runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
        directories.update({config / "systemd/user", config / "systemd/user.control", data / "systemd/user"})
        for suffix in ("user", "user.control", "transient", "generator.early", "generator", "generator.late"):
            directories.add(runtime / "systemd" / suffix)
        for variable, default in (("XDG_CONFIG_DIRS", "/etc/xdg"),
                                  ("XDG_DATA_DIRS", "/usr/local/share:/usr/share")):
            for value in (os.environ.get(variable) or default).split(":"):
                if value:
                    directories.add(Path(value) / "systemd/user")
        return directories

    def _dropins(self, name, directories):
        # Effective DropInPaths alone cannot reveal pending files before reload.
        suffix = name.rsplit(".", 1)[1]
        names = (name + ".d", suffix + ".d", f"trendradar-.{suffix}.d", f"trendradar-r2-.{suffix}.d")
        for directory in directories:
            candidate = directory / name
            if candidate.absolute() != (self.units / name).absolute() and (candidate.exists() or candidate.is_symlink()):
                raise ApplyError("备份 unit 搜索路径中存在其他同名配置；拒绝覆盖/启停")
            for entry in names:
                path = directory / entry
                if path.is_symlink() or (path.exists() and (not path.is_dir() or any(path.glob("*.conf")))):
                    raise ApplyError("备份 unit 存在磁盘 drop-in；拒绝覆盖/启停")

    def _check_wants(self):
        for directory in (self.units / "timers.target.wants", self.units / "timers.target.requires"):
            if directory.is_symlink():
                raise ApplyError("备份 enable 目录为符号链接；拒绝应用")
            if directory.exists():
                _writable(directory)
            link = directory / TIMER
            if link.is_symlink():
                if link.resolve() != (self.units / TIMER).resolve():
                    raise ApplyError("备份 timer 启用链接指向其他安装；拒绝覆盖")
            elif link.exists():
                raise ApplyError("备份 timer 启用路径被其他文件占用；拒绝覆盖")

    def _inspect(self, *, initial=False):
        directories = self._unit_paths()
        for path in self.paths:
            self._dropins(path.name, directories)
            props = self.io.properties(path)
            if props.get("DropInPaths"):
                raise ApplyError("备份 unit 存在有效 drop-in；拒绝覆盖/启停")
            fragment = props.get("FragmentPath", "")
            if fragment and Path(fragment).absolute() != path.absolute():
                raise ApplyError("备份 unit 实际加载自其他路径；拒绝覆盖/启停")
            active, enabled = props.get("ActiveState"), props.get("UnitFileState")
            supported = ("enabled", "enabled-runtime", "disabled", "static", "", "not-found")
            if active not in ("active", "inactive") or enabled not in supported:
                raise ApplyError("备份 unit 状态不稳定或被 mask；拒绝应用")
            if path.name == TIMER and active == "active" and not fragment:
                raise ApplyError("活动备份 timer 实际路径未知；拒绝应用")
            if initial:
                self.states[path.name] = (active, enabled)
            else:
                from native_config import _same_state
                if not _same_state(self.states[path.name], (active, enabled)):
                    raise ApplyError("备份 unit 启用/运行状态被其他进程更改；拒绝应用")

    def check(self):
        if not self.states:
            return
        for path, before in self.original.items():
            if snapshot(path) != before:
                raise ApplyError("备份 unit 在保存前被其他进程更改")
        self._inspect()
        self._check_wants()

    def check_applied(self):
        """Do not activate files changed after the guarded manager reload."""
        for path, before in self.original.items():
            expected = self.changes.get(path, before[-1] if before is not None else None)
            current = snapshot(path)
            if ((expected is None and current is not None)
                    or (expected is not None and (current is None or current[-1] != expected))):
                raise ApplyError("备份 unit 在重载后或启停前被更改；拒绝启停")

    def activate(self):
        if not self.states:
            return
        # Maintenance with action=None still reloads files: inspect the service
        # as well as timer after reload, rather than only checking timer states.
        self._inspect()
        self._check_wants()
        self.check_applied()
        if not self.action:
            return
        active, enabled = self.states[TIMER]
        # --no-reload is essential: implicit enable/disable daemon-reloads
        # would otherwise evade UnitTransaction's other-timer safety checks.
        self.touched = True  # A failed/timed-out command may already have acted.
        if self.action == "enable":
            if enabled not in ("enabled", "enabled-runtime"):
                self.io.call("enable", "--no-reload", TIMER)
            if active != "active":
                self.check_applied()
                self.io.call("start", TIMER)
            self._verify("active", ("enabled", "enabled-runtime"))
        else:
            if active == "active":
                self.io.call("stop", TIMER)
            if enabled in ("enabled", "enabled-runtime"):
                self.io.call("disable", "--no-reload", TIMER)
            self._verify("inactive", ("disabled", "static", "", "not-found"))

    def _verify(self, active, enabled):
        props = self.io.properties(self.units / TIMER)
        if props.get("ActiveState") != active or props.get("UnitFileState") not in enabled:
            raise ApplyError("备份 timer 操作后状态未确认；请检查 systemd 用户管理器")

    def rollback(self):
        if not self.touched:
            return
        active, enabled = self.states[TIMER]
        # Never start a service, including while recovering from failure.
        # A fresh start of our exact Persistent=false timer resets last_trigger
        # (systemd timer_start), so resuming an originally active timer waits
        # for a future calendar event, rather than replaying an elapsed event.
        props = self.io.properties(self.units / TIMER)
        if active == "inactive" and props.get("ActiveState") != "inactive":
            self.io.call("stop", TIMER)
        if enabled in ("enabled", "enabled-runtime"):
            args = ("enable", "--no-reload") + (("--runtime",) if enabled == "enabled-runtime" else ())
            self.io.call(*args, TIMER)
        else:
            self.io.call("disable", "--no-reload", TIMER)
        if active == "active" and props.get("ActiveState") != "active":
            self.io.call("start", TIMER)
        expected = (enabled,) if enabled in ("enabled", "enabled-runtime") else ("disabled", "static", "", "not-found")
        self._verify(active, expected)
        self.touched = False


def commit_plan(changes, plan, runner=None):
    """Installer counterpart of NativeApplication's env+unit transaction."""
    changes = dict(changes)
    changes.update(plan.changes)
    transaction = UnitTransaction(guarded_changes(changes, plan.units), runner,
                                  guard_paths=guarded_timer_paths(plan.units))
    plan.check()
    committed = False
    try:
        transaction.commit()
        committed = True
        plan.activate()
    except BaseException as error:
        failures = []
        try:
            plan.rollback()
        except (OSError, ConfigError):
            failures.append("备份 timer 状态回滚未确认")
        if committed:
            try:
                transaction.rollback()
            except (OSError, ConfigError):
                failures.append("unit 文件/运行时回滚未确认（未绕过安全校验）")
        if failures:
            raise ApplyError("备份安装失败；" + "；".join(failures)) from None
        raise error


def status(app, env, units, values, runner=None):
    """Only configuration/state summaries; no stdout from backup or systemctl."""
    configured = settings(values, credentials=False)
    present = any((units / name).exists() or (units / name).is_symlink() for name in (SERVICE, TIMER))
    label = "开启" if configured.enabled else "关闭"
    summary = f"R2/S3 备份：配置{label}；计划 {configured.time} {configured.timezone}；最近 {configured.lookback_days} 天。"
    if not present:
        return summary + " timer 未安装；手工将 env 设为 true 不会自动安装/启用 timer。"
    # Inspect ownership without requiring credentials, writing or changing units.
    plan = BackupPlan.__new__(BackupPlan)
    plan.app, plan.env, plan.units = app, env, units
    plan.paths = [units / SERVICE, units / TIMER]
    plan.original = {path: snapshot(path) for path in plan.paths}
    plan.expected = render_units(app, env, units, configured)
    plan.io, plan.states = UnitTransaction({}, runner), {}
    plan._validate_files()
    plan._inspect(initial=True)
    active, enabled = plan.states[TIMER]
    return summary + f" timer: {active}/{enabled}；人工暂停不会因普通保存或重装自动恢复。"


def uninstall(app, env, units, runner=None):
    """Fail before mutation for foreign fragments, overrides, links or busy states."""
    paths = [units / SERVICE, units / TIMER]
    if not any(path.exists() or path.is_symlink() for path in paths):
        return
    plan = BackupPlan(app, env, units, {}, {}, runner=runner)
    plan.check()
    # Gate the global reload before any stop/delete; the backup will be inactive
    # for reload, while the unrelated pair retains its original safety policy.
    transaction = UnitTransaction({}, runner, guard_paths=(units / name for name in TIMER_NAMES))
    transaction.preflight(initial=True)
    transaction.check()
    removed = []
    try:
        plan.activate()
        for path, before in plan.original.items():
            if snapshot(path) != before:
                raise ApplyError("备份 unit 卸载前已被修改；未删除")
            if before is not None:
                removed.append(path)
                path.unlink()
        transaction.preflight()
        transaction.check()
        transaction.reload()
        transaction.verify_states()
    except BaseException as error:
        failures = []
        for path in reversed(removed):
            try:
                current = snapshot(path)
                before = plan.original[path]
                if current is None:
                    atomic_write(path, before[-1], 0o644)
                elif current[-1] != before[-1]:
                    raise ApplyError("备份 unit 卸载回滚期间被其他进程修改")
            except (ConfigError, OSError):
                failures.append("备份 unit 文件回滚未确认")
        # Never reactivate an incomplete or externally modified unit pair.
        if not failures:
            try:
                plan.check_applied()
                transaction.preflight()
                transaction.check()
                if transaction.reloaded:
                    transaction.reload()
                    transaction.verify_states()
                plan.rollback()
            except (ConfigError, OSError):
                failures.append("备份 timer 或用户管理器状态回滚未确认")
        if failures:
            raise ApplyError("备份卸载失败；" + "；".join(failures) + "；未覆盖外部改动或绕过重载安全校验") from None
        raise error
