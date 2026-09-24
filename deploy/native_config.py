"""Native configuration transaction; no manual service starts or test runs.

daemon-reload can coldplug an active timer and catch up from LastTriggerUSec.
Preflight both old/new calendars, including the unchanged timer in the pair,
before saving and immediately before every reload (including rollback).
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from envfile import ConfigError, EnvDocument, atomic_write, private_backup, snapshot

SCHEDULE_KEYS = {
    "TZ", "TIMEZONE", "CRAWLER_MINUTE", "MORNING_PUSH_TIME", "NOON_PUSH_TIME",
    "EVENING_PUSH_TIME", "DAILY_SUMMARY_TIME", "WEEKLY_WEEKDAY", "WEEKLY_HOUR", "WEEKLY_MINUTE",
}
TIMER_NAMES = ("trendradar-lite.timer", "trendradar-weekly.timer")
COMMAND_TIMEOUT = 10
SAFETY_HORIZON = 120  # seconds; a guard band, not a lock on the systemd manager
_PROPERTIES = "ActiveState,UnitFileState,FragmentPath,DropInPaths,SubState,LastTriggerUSec,NeedDaemonReload"
_UTC_TIMESTAMP = re.compile(r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:\.\d{1,6})?) UTC")


def _timestamp(value: str) -> float:
    match = _UTC_TIMESTAMP.fullmatch(value)
    if not match:
        raise ApplyError("无法可靠解析 systemd UTC 时间")
    try:
        return datetime.fromisoformat(match[1]).replace(tzinfo=timezone.utc).timestamp()
    except (ValueError, OverflowError):
        raise ApplyError("无法可靠解析 systemd UTC 时间") from None


def _safety_error(reason: str) -> ApplyError:
    return ApplyError("拒绝应用时间配置：" + reason + "；daemon-reload 也可能触发补跑。"
                      "请等待原定时器下一次正常触发完成后，远离原/新计划时间重试；"
                      "若状态仍无法确认，请先检查 timer。未手动启动服务或执行测试，未停止 timer 或删除时间戳")


def _same_state(expected: tuple, actual: tuple) -> bool:
    # UnitFileState may become "disabled" as soon as a new file exists, even
    # before reload; this is not activation or enablement of the fresh timer.
    return actual == expected or (expected[0] == actual[0] == "inactive"
                                  and expected[1] in ("", "not-found")
                                  and actual[1] in ("disabled", "static", ""))


def config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


class ApplyError(ConfigError):
    pass


class UnitTransaction:
    """Preserve timer enablement and running state, refusing effective overrides."""
    def __init__(self, changes: dict[Path, bytes], runner=None):
        self.runner = runner or subprocess.run
        proposed = {Path(p): data for p, data in changes.items()}
        self.before = {path: snapshot(path) for path in proposed}
        self.changes = {path: data for path, data in proposed.items()
                        if self.before[path] is None or self.before[path][-1] != data}
        self.timers = {path: data for path, data in proposed.items() if path.name.endswith(".timer")}
        self.states = {}
        self.written = []
        self.reloaded = False
        self.checked_at = None
        self.safe_until = None
        if self.changes:
            self.preflight(initial=True)
            self.check()

    def properties(self, path):
        proc = self.call("show", path.name, "--property=" + _PROPERTIES)
        return dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)

    def calendar_next(self, calendars: list[str], base: str) -> list[float]:
        # Keep the host timezone for legacy zone-less calendars, ignoring the
        # menu process's TZ. systemd-analyze emits an explicit UTC row if needed.
        environment = dict(os.environ, LC_ALL="C", SYSTEMD_COLORS="0", COLUMNS="4096")
        environment.pop("TZ", None)
        try:
            result = self.runner(["systemd-analyze", "calendar", "--no-pager", "--iterations=1",
                                  "--base-time=" + base, *calendars], capture_output=True,
                                 text=True, env=environment, timeout=COMMAND_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired):
            raise ApplyError("只读 calendar 校验失败或超时") from None
        if result.returncode:
            raise ApplyError("只读 calendar 校验失败")
        rows = []
        for block in re.split(r"\n\s*\n", result.stdout.strip()):
            pairs = [(key.strip(), value.strip()) for line in block.splitlines()
                     if ":" in line for key, value in [line.split(":", 1)]]
            keys = [key for key, _ in pairs]
            fields = dict(pairs)
            if keys.count("Next elapse") != 1 or keys.count("(in UTC)") > 1:
                raise ApplyError("calendar 输出格式不受支持")
            rows.append(_timestamp(fields.get("(in UTC)", fields["Next elapse"])))
        if len(rows) != len(calendars):
            raise ApplyError("calendar 输出数量不匹配")
        return rows

    def preflight(self, *, initial=False):
        """Read-only safety check, never stop timers or alter persistent stamps.

        v255 timer.c: timer_coldplug -> timer_enter_waiting calculates *each*
        calendar from last_trigger, not from now. analyze-calendar.c uses that
        same calendar_spec_next_usec routine. Equal next occurrences at both
        bases exclude occurrences in the intervening interval. Timestamp
        parsing is intentionally narrow; unknown state/output fails closed.
        """
        from native_schedule import _DAILY, _WEEKLY, _timer_lines, ScheduleError

        started = time.time()
        deadline = started + SAFETY_HORIZON
        next_times = []
        for path, proposed in self.timers.items():
            properties = self.properties(path)
            active = properties.get("ActiveState")
            enabled = properties.get("UnitFileState")
            fragment = properties.get("FragmentPath", "")
            if properties.get("DropInPaths"):
                raise ApplyError("timer 存在有效 drop-in；请手工迁移")
            if fragment and Path(fragment).resolve() != path.resolve():
                raise ApplyError("systemd 实际加载的 timer 来自其他路径")
            if active not in ("active", "inactive") or enabled not in ("enabled", "enabled-runtime", "disabled", "static", "", "not-found"):
                raise ApplyError("timer 状态不是受支持的稳定启用/禁用状态")
            if initial:
                self.states[path.name] = (active, enabled)
            elif not _same_state(self.states[path.name], (active, enabled)):
                raise _safety_error("timer 启用或运行状态已变化")
            if active != "active":
                continue  # Disabled/inactive timers are never activated here.
            try:
                if properties.get("SubState") != "waiting":
                    raise ApplyError("活动 timer 不是稳定 waiting 状态")
                if not fragment:
                    raise ApplyError("活动 timer 的实际配置路径未知")
                if initial and properties.get("NeedDaemonReload") != "no":
                    raise ApplyError("活动 timer 的磁盘配置与管理器状态尚未确认一致")
                last_trigger = properties.get("LastTriggerUSec", "")
                last = _timestamp(last_trigger)
                if not 0 < last <= started:
                    raise ApplyError("上次触发时间未知或位于未来")
                before = self.before[path]
                if before is None:
                    raise ApplyError("活动 timer 缺少原始配置")
                calendars = list(dict.fromkeys(_timer_lines(before[-1], path.name)[2]
                                               + _timer_lines(proposed, path.name)[2]))
                # The manager can have a different TZ environment from this
                # process. A host-local legacy expression (including "hourly")
                # therefore has no provable timezone here. Do not guess it.
                for calendar in calendars:
                    match = _DAILY.fullmatch(calendar) or _WEEKLY.fullmatch(calendar)
                    if not match or not match.groups()[-1]:
                        raise ApplyError("活动 timer 含无显式时区或不受支持的计划；"
                                         "请在 timer 已为 inactive 时明确规范化，或手工核对并迁移为显式时区")
                from_last = self.calendar_next(calendars, last_trigger)
                from_horizon = self.calendar_next(calendars, f"@{deadline:.6f}")
                if from_last != from_horizon or any(value <= deadline for value in from_last):
                    raise ApplyError("原/新计划存在已过期或临近的触发点（安全窗口 120 秒）")
                next_times.extend(from_last)
            except (ConfigError, ScheduleError, UnicodeError) as error:
                raise _safety_error(str(error)) from None
        # Calendar tools take time too. A suspend/clock jump or slow preflight
        # must not consume the guard band before the caller reaches reload.
        now = time.time()
        if now < started or any(value <= now + SAFETY_HORIZON for value in next_times):
            raise _safety_error("校验期间时钟变化或触发点已进入安全窗口")
        self.checked_at = now
        self.safe_until = min(next_times, default=float("inf")) - SAFETY_HORIZON

    def reload(self):
        # Recheck freshness after filesystem checks too. No userspace guard can
        # lock out suspend, clock changes or another manager client after this.
        now = time.time()
        if self.checked_at is None or self.safe_until is None or not self.checked_at <= now < self.safe_until:
            raise _safety_error("重载前安全校验已过期")
        self.reloaded = True  # Even a failed/timed-out call may have taken effect.
        self.call("daemon-reload")

    def call(self, *args, check=True):
        try:
            result = self.runner(["systemctl", "--user", *args], capture_output=True, text=True,
                                 env=dict(os.environ, LC_ALL="C", TZ="UTC", SYSTEMD_COLORS="0"),
                                 timeout=COMMAND_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired):
            raise ApplyError("systemd 用户管理器操作失败或超时；未手动启动服务或执行测试") from None
        if check and result.returncode:
            raise ApplyError("systemd 操作失败（未手动启动服务或执行测试）")
        return result

    def check(self):
        for path, before in self.before.items():
            if snapshot(path) != before:
                raise ApplyError("timer/unit 文件已被其他进程修改；拒绝覆盖")

    def commit(self):
        self.check()
        try:
            for path, data in self.changes.items():
                before = self.before[path]
                if before:
                    private_backup(path, before[-1])
                if snapshot(path) != before:
                    raise ApplyError("timer/unit 在保存前发生变化")
                # replace() may succeed before a directory fsync raises. Track
                # attempts, then use snapshots/expected bytes during rollback.
                self.written.append(path)
                atomic_write(path, data, 0o644)
            if self.changes:
                self.preflight()
                self.check_written()
                self.reload()
                self.verify_states()
        except BaseException:
            self.rollback()
            raise

    def check_written(self):
        for path, before in self.before.items():
            now = snapshot(path)
            if path in self.changes:
                if not now or now[-1] != self.changes[path]:
                    raise ApplyError("timer/unit 在重载前发生变化")
            elif now != before:
                raise ApplyError("未修改的 timer/unit 在重载前发生变化")

    def verify_states(self):
        for name, state in self.states.items():
            proc = self.call("show", name, "--property=ActiveState,UnitFileState")
            actual = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
            if not _same_state(state, (actual.get("ActiveState"), actual.get("UnitFileState"))):
                raise ApplyError("timer 启用或运行状态出现变化")

    def rollback(self):
        failures = []
        restored = {}
        for path in reversed(self.written):
            try:
                now = snapshot(path)
                if now == self.before[path]:
                    restored[path] = now
                    continue  # Failure before replace: nothing to restore.
                if not now or now[-1] != self.changes[path]:
                    raise ApplyError("unit 再次发生变化")
                if self.before[path] is None:
                    path.unlink()
                else:
                    atomic_write(path, self.before[path][-1], 0o644)
                restored[path] = snapshot(path)
            except (OSError, ConfigError):
                try:
                    now = snapshot(path)
                    before = self.before[path]
                    bytes_restored = ((now is None and before is None)
                                      or (now is not None and before is not None and now[-1] == before[-1]))
                except (OSError, ConfigError):
                    bytes_restored = False
                failures.append("unit 内容已恢复，但持久化确认失败" if bytes_restored else "unit 文件")
        if self.reloaded:
            if failures:
                failures.append("systemd 状态（文件回滚未完全确认，未再次重载）")
            else:
                try:
                    # A failed or timed-out reload may have taken effect. Both
                    # possible runtime calendars and the restored files must be
                    # safe; otherwise leave disk restored and report uncertainty.
                    self.preflight()
                    for path, before in self.before.items():
                        if snapshot(path) != restored.get(path, before):
                            raise ApplyError("回滚重载前 unit 再次发生变化")
                    self.reload()
                    self.verify_states()
                except (ConfigError, OSError) as error:
                    reason = str(error) if isinstance(error, ConfigError) else "无法校验 unit 文件"
                    failures.append("systemd 状态（unit 文件已恢复，但运行时恢复未确认；"
                                    "未绕过安全校验；" + reason + "）")
        if failures:
            raise ApplyError("应用失败且回滚不完整：" + "、".join(failures) + "；请检查私有备份和 timer 状态")


class NativeApplication:
    def __init__(self, output: Path, app_dir: Path | None = None, unit_dir: Path | None = None,
                 install: bool = False, runner=None):
        from native_schedule import NativeSchedule
        self.document = EnvDocument(output)
        self.app_dir = app_dir or Path(__file__).resolve().parents[1]
        self.unit_dir = unit_dir or config_home() / "systemd/user"
        self.install = install
        self.runner = runner
        self.schedule = NativeSchedule(self.app_dir, self.unit_dir, self.document.values)

    def save(self, values: dict[str, str], normalize: bool = False) -> str:
        values = dict(values)
        if normalize:
            for key, suggestion in self.schedule.suggested.items():
                if key not in values or (not values[key] and not self.document.values.get(key)):
                    values[key] = suggestion
        changed_schedule = any(values.get(key, "") != self.document.values.get(key, "") for key in SCHEDULE_KEYS)
        changes = {}
        if changed_schedule or normalize:
            if values.get("TIMEZONE") and values.get("TIMEZONE") != values.get("TZ"):
                raise ConfigError("TIMEZONE 与 TZ 冲突，请在时间菜单确认统一时区")
            from native_schedule import ScheduleError
            try:
                changes = self.schedule.prepare(values, normalize=normalize)
            except ScheduleError as error:
                raise ConfigError(str(error)) from None
        transaction = UnitTransaction(changes, self.runner) if changes and not self.install else None
        self.document.check_unchanged()
        written = self.document.render(values)
        saved = False
        try:
            saved = self.document.save(values)
            if transaction:
                transaction.commit()
        except BaseException as error:
            restored = False
            try:
                current = snapshot(self.document.path)
                if saved or (written != self.document.content and current and current[-1] == written):
                    self.document.restore(written)
                    self.document = EnvDocument(self.document.path)
                    restored = True
            except (ConfigError, OSError):
                # restore() can also replace successfully before fsync fails.
                # Distinguish visible bytes from confirmed durability/runtime.
                try:
                    current = snapshot(self.document.path)
                    original = self.document.original
                    disk_restored = ((current is None and original is None)
                                     or (current is not None and original is not None
                                         and current[-1] == original[-1]))
                except (ConfigError, OSError):
                    disk_restored = False
                state = ("env 内容已恢复，但持久化确认失败" if disk_restored
                         else "env 回滚失败，保留当前文件及私有备份")
                detail = str(error) if isinstance(error, ConfigError) else "unit/运行时状态需检查"
                raise ApplyError(f"应用失败；{state}；{detail}") from None
            if isinstance(error, (KeyboardInterrupt, EOFError)):
                raise
            state = "env 已恢复" if restored else "env 未回滚（未确认本操作写入，保留当前文件）"
            detail = str(error) if isinstance(error, ConfigError) else "请检查 timer 状态与私有备份"
            raise ApplyError(f"应用失败；{state}；{detail}") from None
        if transaction and transaction.changes:
            return ("配置已保存；timer 已通过补跑风险校验并重载，原启用/运行状态已保留。"
                    "未手动启动服务、执行测试或重启 timer；原定时任务仍可正常运行。")
        if self.install and changes:
            return "配置已保存；timer 尚未安装，将由安装器继续处理。"
        return "配置已保存；普通配置于下一次 oneshot 生效，未重载或重启 timer。"
