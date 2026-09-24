"""Read-only native schedule inspection and conservative timer preparation.

Only the shipped four-period custom timeline and hourly/daily + weekly timer
shapes are editable. Unsupported installations remain usable for non-schedule
configuration: ``prepare`` returns {} when no schedule key changed. Inspection
never invokes systemctl; callers own file transactions and daemon reloads.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_SCHEDULE = {
    "TZ": "Asia/Shanghai",
    "CRAWLER_MINUTE": "5",
    "MORNING_PUSH_TIME": "07:00",
    "NOON_PUSH_TIME": "12:00",
    "EVENING_PUSH_TIME": "18:00",
    "DAILY_SUMMARY_TIME": "22:00",
    "WEEKLY_WEEKDAY": "6",
    "WEEKLY_HOUR": "12",
    "WEEKLY_MINUTE": "30",
}
SCHEDULE_KEYS = tuple(DEFAULT_SCHEDULE)
_CONTROL_KEYS = ("TIMEZONE", "SCHEDULE_ENABLED", "SCHEDULE_PRESET", "CONFIG_PATH")
PERIOD_KEYS = {
    "morning_brief": "MORNING_PUSH_TIME",
    "noon_brief": "NOON_PUSH_TIME",
    "afternoon_brief": "EVENING_PUSH_TIME",
    "nightly_daily": "DAILY_SUMMARY_TIME",
}
TIMER_NAMES = ("trendradar-lite.timer", "trendradar-weekly.timer")
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_TIME = r"(?:[01]\d|2[0-3]):[0-5]\d"
_ZONE = r"[A-Za-z0-9_+./-]+"
_DAILY = re.compile(rf"\*-\*-\* (\*|[01]\d|2[0-3]):([0-5]\d):00(?: ({_ZONE}))?")
_WEEKLY = re.compile(rf"({'|'.join(WEEKDAYS)}) \*-\*-\* ({_TIME}):00(?: ({_ZONE}))?")


class ScheduleError(ValueError):
    """A schedule is unsupported or requires explicit normalization."""


def _plain(value: object) -> str:
    return "" if value is None else str(value).strip()


def _no_symlink(path: Path) -> None:
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise ScheduleError(f"Symlink is not supported: {candidate}")


def _read(path: Path) -> bytes | None:
    _no_symlink(path)
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _strip_comment(line: str) -> str:
    """Strip YAML comments without treating a quoted # as a comment."""
    quote = None
    escaped = False
    for pos, char in enumerate(line):
        if escaped:
            escaped = False
        elif char == "\\" and quote == '"':
            escaped = True
        elif char in "\"'":
            if quote == char:
                quote = None
            elif quote is None:
                quote = char
        elif char == "#" and quote is None and (pos == 0 or line[pos - 1].isspace()):
            return line[:pos].rstrip()
    if quote:
        raise ScheduleError("Unterminated YAML quote")
    return line.rstrip()


def _scalar(value: str):
    if value.startswith(('"', "'")):
        if len(value) < 2 or value[-1] != value[0]:
            raise ScheduleError("Unsupported YAML string")
        # Escapes require a full YAML parser; do not guess at their meaning.
        if "\\" in value or value[0] in value[1:-1]:
            raise ScheduleError("Escaped YAML strings are not supported")
        return value[1:-1]
    if value in ("true", "false"):
        return value == "true"
    if re.fullmatch(r"0|[1-9]\d*", value):
        return int(value)
    if re.fullmatch(r"\d+:\d+(?::\d+)?", value):
        raise ScheduleError("Quote YAML times explicitly; implicit sexagesimal values are not supported")
    if not re.fullmatch(r"[\w./:+ -]+", value) or value.lower() in {"null", "yes", "no", "on", "off"}:
        raise ScheduleError(f"Unsupported YAML scalar: {value!r}")
    if value.startswith(("- ", ": ")) or ": " in value:
        raise ScheduleError("Unsupported YAML mapping in scalar")
    return value


def _yaml_subset(text: str):
    """Parse block mappings and scalar lists only; reject aliases/flow/tags.

    This intentionally is not a general YAML parser. Duplicate keys, nonstandard
    indentation and complex syntax fail closed instead of silently disappearing.
    """
    rows = []
    for raw in text.splitlines():
        if "\t" in raw:
            raise ScheduleError("YAML tabs are not supported")
        line = _strip_comment(raw)
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        rows.append((indent, line.strip()))
    if not rows:
        raise ScheduleError("Empty YAML schedule")

    def block(index: int, indent: int):
        is_list = rows[index][1].startswith("- ")
        result = [] if is_list else {}
        while index < len(rows) and rows[index][0] == indent:
            item = rows[index][1]
            if is_list:
                if not item.startswith("- "):
                    raise ScheduleError("Mixed YAML collection")
                result.append(_scalar(item[2:].strip()))
                index += 1
                if index < len(rows) and rows[index][0] > indent:
                    raise ScheduleError("Nested YAML list item is not supported")
                continue
            match = re.fullmatch(r"([A-Za-z_][\w-]*|[1-7]):(?: +(.*))?", item)
            if not match:
                raise ScheduleError(f"Unsupported YAML mapping: {item!r}")
            key = match[1]
            if key in result:
                raise ScheduleError(f"Duplicate YAML key: {key}")
            value = match[2]
            index += 1
            if value is not None:
                result[key] = _scalar(value)
                if index < len(rows) and rows[index][0] > indent:
                    raise ScheduleError("Unexpected nested YAML value")
            else:
                if index == len(rows) or rows[index][0] != indent + 2:
                    raise ScheduleError("Expected a YAML block indented by two spaces")
                result[key], index = block(index, indent + 2)
        if index < len(rows) and rows[index][0] > indent:
            raise ScheduleError("Unsupported YAML indentation")
        return result, index

    result, end = block(0, 0)
    if end != len(rows):
        raise ScheduleError("Unsupported YAML document structure")
    return result


def _config_sections(text: str) -> dict:
    """Read only app/schedule blocks, leaving unrelated config syntax alone."""
    selected = []
    keep = False
    seen = set()
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw[0].isspace():
            if keep:
                selected.append(raw)
            continue
        line = _strip_comment(raw)
        match = re.fullmatch(r"([A-Za-z_][\w-]*):(?: .*)?", line)
        if not match:
            raise ScheduleError("Unsupported config.yaml top-level syntax")
        key = match[1]
        if key in seen:
            raise ScheduleError(f"Duplicate config.yaml section: {key}")
        seen.add(key)
        keep = key in {"app", "schedule"}
        if keep:
            selected.append(line)
    return _yaml_subset("\n".join(selected)) if selected else {}


def _timeline_periods(text: str) -> dict[str, tuple[str, str]]:
    data = _yaml_subset(text)
    if not isinstance(data, dict) or set(data) != {"custom"}:
        raise ScheduleError("Only the shipped custom timeline is supported")
    custom = data["custom"]
    if not isinstance(custom, dict) or set(custom) != {"default", "periods", "day_plans", "week_map"}:
        raise ScheduleError("Unsupported custom timeline structure")
    default = {"collect": True, "analyze": False, "push": False,
               "report_mode": "current", "once": {"analyze": False, "push": False}}
    if custom["default"] != default:
        raise ScheduleError("Custom timeline default actions are not the shipped form")
    if custom["day_plans"] != {"all_day": {"periods": list(PERIOD_KEYS)}}:
        raise ScheduleError("Custom day plans are not supported")
    if custom["week_map"] != {str(day): "all_day" for day in range(1, 8)}:
        raise ScheduleError("Custom week mapping is not supported")
    periods = custom["periods"]
    if not isinstance(periods, dict) or set(periods) != set(PERIOD_KEYS):
        raise ScheduleError("Expected exactly the four shipped delivery periods")
    result = {}
    for name, key in PERIOD_KEYS.items():
        period = periods[name]
        if not isinstance(period, dict):
            raise ScheduleError(f"Invalid timeline period: {name}")
        allowed = {"name", "start", "end", "collect", "analyze", "push", "report_mode", "once"}
        if set(period) - allowed or not {"start", "end"} <= set(period):
            raise ScheduleError(f"Unsupported fields in timeline period: {name}")
        expected = {"collect": True, "analyze": True, "push": True,
                    "report_mode": "daily" if name == "nightly_daily" else "current",
                    "once": {"analyze": True, "push": True}}
        if {k: v for k, v in period.items() if k not in {"name", "start", "end"}} != expected:
            raise ScheduleError(f"Unsupported delivery actions in timeline period: {name}")
        start, end = period["start"], period["end"]
        if not isinstance(start, str) or not isinstance(end, str) or not re.fullmatch(_TIME, start) or not re.fullmatch(_TIME, end):
            raise ScheduleError(f"Invalid timeline time in {name}; use quoted HH:MM")
        if end < start:
            raise ScheduleError(f"Overnight timeline window is not supported: {name}")
        result[key] = (start, end)
    ordered = sorted(result.values())
    if any(left[1] >= right[0] for left, right in zip(ordered, ordered[1:])):
        raise ScheduleError("Overlapping delivery windows are not supported")
    return result


def _check_dropins(unit_dir: Path) -> None:
    # systemd applies type-wide and dash-prefix drop-ins as well as unit-specific.
    names = ("timer.d", "trendradar-.timer.d", *(name + ".d" for name in TIMER_NAMES))
    for name in names:
        path = unit_dir / name
        _no_symlink(path)
        if path.exists() and (not path.is_dir() or any(path.glob("*.conf"))):
            raise ScheduleError(f"Timer drop-ins are not supported: {path}")


def _timer_lines(data: bytes, name: str) -> tuple[list[str], list[int], list[str]]:
    text = data.decode("utf-8")
    lines = text.splitlines(keepends=True)
    section = None
    timer_sections = 0
    indices, calendars = [], []
    for index, raw in enumerate(lines):
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.endswith("\\"):
            raise ScheduleError(f"Unit continuations are not supported: {name}")
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            timer_sections += section == "Timer"
            continue
        if "=" not in line or section is None:
            raise ScheduleError(f"Malformed unit directive in {name}")
        key, value = (part.strip() for part in line.split("=", 1))
        if section == "Timer":
            if key == "OnCalendar":
                if not value:
                    raise ScheduleError(f"OnCalendar resets are not supported: {name}")
                indices.append(index)
                calendars.append(value)
            elif key.startswith("On") or key == "DeferReactivation":
                raise ScheduleError(f"Custom timer trigger {key} is not supported: {name}")
            elif key == "Unit" and value != name.removesuffix(".timer") + ".service":
                raise ScheduleError(f"Custom timer target is not supported: {name}")
    if timer_sections != 1 or not calendars:
        raise ScheduleError(f"Expected one [Timer] section with OnCalendar: {name}")
    return lines, indices, calendars


def _replace_calendars(data: bytes, name: str, calendars: list[str]) -> bytes:
    lines, indices, _ = _timer_lines(data, name)
    newline = "\r\n" if lines[indices[0]].endswith("\r\n") else "\n"
    replacement = "".join(f"OnCalendar={value}{newline}" for value in dict.fromkeys(calendars))
    remove = set(indices)
    return "".join(replacement if i == indices[0] else line
                   for i, line in enumerate(lines) if i == indices[0] or i not in remove).encode("utf-8")


def _fresh_timer(name: str, calendars: list[str]) -> bytes:
    title = "TrendRadar Lite weekly report" if name == TIMER_NAMES[1] else "TrendRadar Lite collection and delivery"
    return (f"[Unit]\nDescription=Run {title}\n\n[Timer]\n"
            + "".join(f"OnCalendar={value}\n" for value in dict.fromkeys(calendars))
            + "Persistent=true\nAccuracySec=1min\n\n[Install]\nWantedBy=timers.target\n").encode("utf-8")


def _validate(values: dict) -> dict[str, str]:
    result = {key: _plain(values.get(key)) for key in SCHEDULE_KEYS}
    for key, low, high in (("CRAWLER_MINUTE", 0, 59), ("WEEKLY_WEEKDAY", 0, 6),
                           ("WEEKLY_HOUR", 0, 23), ("WEEKLY_MINUTE", 0, 59)):
        value = result[key]
        if not re.fullmatch(r"\d{1,2}", value) or not low <= int(value) <= high:
            raise ScheduleError(f"{key} must be between {low} and {high}")
        result[key] = str(int(value))
    for key in PERIOD_KEYS.values():
        if not re.fullmatch(_TIME, result[key]):
            raise ScheduleError(f"{key} must use HH:MM")
    if len({result[key] for key in PERIOD_KEYS.values()}) != len(PERIOD_KEYS):
        raise ScheduleError("Delivery times must not overlap")
    zone = result["TZ"]
    if not re.fullmatch(_ZONE, zone) or zone.startswith("/") or ".." in zone.split("/"):
        raise ScheduleError("TZ must be an IANA timezone name")
    try:
        ZoneInfo(zone)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ScheduleError(f"Unknown timezone: {zone}") from exc
    if _plain(values.get("TIMEZONE")) and _plain(values["TIMEZONE"]) != zone:
        raise ScheduleError("TIMEZONE overrides TZ; make them agree before changing the schedule")
    return result


class NativeSchedule:
    """Inspect eagerly without subprocesses; prepare bytes without writing.

    ``initial`` is the unchanged input environment. ``suggested`` holds resolved
    schedule values, not a claim about running timers. ``supported`` means the
    filesystem/configuration shape can be edited; warnings may still require
    ``normalize=True``. Both timer paths are returned for a schedule operation,
    even if one timer's bytes are unchanged; filter byte-identical entries before
    an apply/reload transaction. Mail/AI-only operations return an empty dict.
    """

    def __init__(self, app_dir: Path, unit_dir: Path, values: dict, runner=None):
        self.app_dir = Path(app_dir)
        self.unit_dir = Path(unit_dir)
        self.initial = dict(values)
        self.suggested = dict(DEFAULT_SCHEDULE)
        self.warnings: list[str] = []
        self.supported = True
        self.runner = runner if runner is not None else subprocess.run
        self._normalization_required = False
        self._original: dict[Path, bytes | None] = {}
        self._config_original: dict[Path, bytes | None] = {}
        self._calendars: dict[str, list[str]] = {}
        self._windows: dict[str, tuple[str, str]] = {}
        self._inspect()

    def _warn(self, message: str, *, unsupported=False):
        self.warnings.append(message)
        self._normalization_required = True
        if unsupported:
            self.supported = False

    def _inspect_config(self):
        config_path = self.app_dir / "config" / "config.yaml"
        configured_path = _plain(self.initial.get("CONFIG_PATH"))
        if configured_path and Path(configured_path) not in {Path("config/config.yaml"), config_path}:
            raise ScheduleError("Custom CONFIG_PATH is not supported")
        timeline_path = config_path.with_name("timeline.yaml")
        config_data = _read(config_path)
        timeline_data = _read(timeline_path)
        self._config_original = {config_path: config_data, timeline_path: timeline_data}
        if config_data is None or timeline_data is None:
            raise ScheduleError("Missing config.yaml or timeline.yaml; defaults are not an active schedule")
        config = _config_sections(config_data.decode("utf-8"))
        app = config.get("app", {})
        if not isinstance(app, dict):
            raise ScheduleError("Unsupported app configuration")
        self.suggested["TZ"] = _plain(app.get("timezone")) or DEFAULT_SCHEDULE["TZ"]
        schedule = config.get("schedule", {})
        if not isinstance(schedule, dict) or set(schedule) - {"enabled", "preset"}:
            raise ScheduleError("Unsupported custom config.yaml schedule fields")
        enabled = _plain(self.initial.get("SCHEDULE_ENABLED"))
        preset = _plain(self.initial.get("SCHEDULE_PRESET")) or schedule.get("preset", "always_on")
        if (enabled.lower() not in {"true", "1"} if enabled else schedule.get("enabled") is not True) or preset != "custom":
            raise ScheduleError("Only enabled schedule.preset=custom is supported")
        self._windows = _timeline_periods(timeline_data.decode("utf-8"))
        for key, (start, end) in self._windows.items():
            self.suggested[key] = start
            if not _plain(self.initial.get(key)) and start != end:
                self._warn(f"{key}：YAML 推送窗口为 {start}-{end}；规范化将其收敛到指定分钟（start=end）")

    def _inspect_timer(self, name: str, data: bytes):
        _, _, calendars = _timer_lines(data, name)
        self._calendars[name] = calendars
        if name == TIMER_NAMES[0]:
            minutes, times, zones = [], [], []
            for calendar in calendars:
                if calendar == "hourly":
                    minutes.append(0)
                    zones.append(None)
                    continue
                match = _DAILY.fullmatch(calendar)
                if not match:
                    raise ScheduleError(f"Unsupported daily OnCalendar: {calendar}")
                hour, minute, zone = match.groups()
                zones.append(zone)
                if hour == "*":
                    minutes.append(int(minute))
                else:
                    times.append(f"{hour}:{minute}")
            if len(minutes) != 1 or len(times) > 4 or len(set(times)) != len(times):
                raise ScheduleError("Ambiguous hourly/daily timer; expected one hourly trigger and at most four delivery triggers")
            if len(set(zones)) > 1:
                raise ScheduleError("Mixed timezone expressions in daily timer are not supported")
            self.suggested["CRAWLER_MINUTE"] = str(minutes[0])
        else:
            if len(calendars) != 1 or not (match := _WEEKLY.fullmatch(calendars[0])):
                raise ScheduleError("Unsupported weekly OnCalendar; expected one explicit weekday/time")
            weekday, time, _zone = match.groups()
            hour, minute = time.split(":")
            self.suggested.update(WEEKLY_WEEKDAY=str(WEEKDAYS.index(weekday)),
                                  WEEKLY_HOUR=str(int(hour)), WEEKLY_MINUTE=str(int(minute)))

    def _inspect(self):
        try:
            self._inspect_config()
        except (OSError, UnicodeError, ScheduleError) as exc:
            self._warn(str(exc), unsupported=True)
        try:
            _check_dropins(self.unit_dir)
            for name in TIMER_NAMES:
                path = self.unit_dir / name
                data = _read(path)
                self._original[path] = data
                if data is not None:
                    self._inspect_timer(name, data)
            missing = [path.name for path, data in self._original.items() if data is None]
            if len(missing) == 1:
                self._warn(f"只找到一个 timer，缺少 {missing[0]}；不自动修复不完整安装", unsupported=True)
            elif len(missing) == 2:
                self._warn("两个 timer 均不存在；建议值不代表已生效。明确确认规范化后可以创建定时器")
        except (OSError, UnicodeError, ScheduleError) as exc:
            self._warn(str(exc), unsupported=True)
        # Environment takes precedence over inferred file values.
        for key in SCHEDULE_KEYS:
            if _plain(self.initial.get(key)):
                self.suggested[key] = _plain(self.initial[key])
        if _plain(self.initial.get("TIMEZONE")):
            if _plain(self.initial.get("TZ")) and _plain(self.initial["TZ"]) != _plain(self.initial["TIMEZONE"]):
                self._warn("TIMEZONE 会覆盖 TZ；程序与定时器的时区配置不一致")
            self.suggested["TZ"] = _plain(self.initial["TIMEZONE"])
        missing_keys = [key for key in SCHEDULE_KEYS if not _plain(self.initial.get(key))]
        if missing_keys:
            self.warnings.append("env 缺少时间字段；以下字段只有推定值，尚未保存：" + ", ".join(missing_keys))
        try:
            resolved = _validate({**self.initial, **self.suggested})
        except ScheduleError as exc:
            # Invalid environment values can be repaired by an explicit schedule
            # operation; they do not make a recognized unit shape uneditable.
            self._warn(str(exc))
            return
        self._check_mismatches(resolved)

    def _check_mismatches(self, values: dict):
        zone = values["TZ"]
        daily = self._calendars.get(TIMER_NAMES[0], [])
        if daily:
            actual_minute = None
            actual_times = set()
            for item in daily:
                if item == "hourly":
                    actual_minute = 0
                    self._warn("日常 timer 使用服务器时区（hourly），未显式指定 TZ；规范化将固定时区")
                    continue
                match = _DAILY.fullmatch(item)
                if not match:
                    return  # Shape was already marked unsupported.
                hour, minute, actual_zone = match.groups()
                if actual_zone != zone:
                    self._warn(f"日常 timer 时区不一致：{actual_zone or '服务器本地时区'} / {zone}")
                if hour == "*":
                    actual_minute = int(minute)
                else:
                    actual_times.add(f"{hour}:{minute}")
            if actual_minute != int(values["CRAWLER_MINUTE"]):
                self._warn("CRAWLER_MINUTE 与已安装 timer 不一致")
            expected = {values[key] for key in PERIOD_KEYS.values()}
            # An hourly trigger covers an exact delivery at its own minute.
            uncovered = {time for time in expected if int(time[-2:]) != actual_minute} - actual_times
            if uncovered or actual_times - expected:
                self._warn("已安装的日常推送时刻与 env/timeline 不一致")
        weekly = self._calendars.get(TIMER_NAMES[1], [])
        if weekly and (match := _WEEKLY.fullmatch(weekly[0])):
            day, time, actual_zone = match.groups()
            expected_time = f"{int(values['WEEKLY_HOUR']):02d}:{int(values['WEEKLY_MINUTE']):02d}"
            if day != WEEKDAYS[int(values["WEEKLY_WEEKDAY"])] or time != expected_time or actual_zone != zone:
                self._warn("已安装的周报 timer 与 env 时间或时区不一致")

    def describe(self) -> list[str]:
        lines = ["调度配置：" + ("已识别" if self.supported else "不支持自动编辑时间"),
                 "此处仅检查文件，尚未查询定时器的启用/运行状态。"]
        for name in TIMER_NAMES:
            calendars = self._calendars.get(name)
            lines.append(f"{name}: " + ("; ".join(calendars) if calendars else "不存在或无法识别"))
        lines.append("推定值（不代表已生效）：" + ", ".join(f"{key}={value}" for key, value in self.suggested.items()))
        lines.extend("警告：" + warning for warning in self.warnings)
        return lines

    def prepare(self, values: dict, normalize: bool = False) -> dict[Path, bytes]:
        """Return both proposed timer files, or {} for unrelated/no-op saves.

        Values may be a partial update or a complete environment. Omitted keys
        retain their initial values. Explicit blank schedule values are rejected,
        not silently replaced by defaults. ``normalize`` is explicit consent to
        replace recognized legacy/mismatched calendars, not unknown custom units.
        """
        changed = any(key in values and _plain(values[key]) != _plain(self.initial.get(key))
                      for key in (*SCHEDULE_KEYS, *_CONTROL_KEYS))
        if not changed and not normalize:
            return {}
        if not self.supported:
            raise ScheduleError("Unsupported native schedule; mail/AI settings may still be saved without changing schedule keys: " + "; ".join(self.warnings))
        if self._normalization_required and not normalize:
            raise ScheduleError("Explicit normalize=True is required: " + "; ".join(self.warnings))
        combined = {**self.initial, **self.suggested, **values}
        resolved = _validate(combined)
        # Changing scheduler selection requires a separate migration; this module
        # cannot prove an arbitrary replacement config has equivalent semantics.
        for key in ("SCHEDULE_ENABLED", "SCHEDULE_PRESET", "CONFIG_PATH"):
            if key in values and _plain(values[key]) != _plain(self.initial.get(key)):
                raise ScheduleError(f"Changing {key} is not supported by native schedule editing")
        _check_dropins(self.unit_dir)
        for path, original in {**self._original, **self._config_original}.items():
            if _read(path) != original:
                raise ScheduleError(f"Schedule files changed since inspection; reopen the menu: {path}")
        zone = resolved["TZ"]
        calendars = {
            TIMER_NAMES[0]: [f"*-*-* *:{int(resolved['CRAWLER_MINUTE']):02d}:00 {zone}"]
                           + [f"*-*-* {resolved[key]}:00 {zone}" for key in PERIOD_KEYS.values()],
            TIMER_NAMES[1]: [f"{WEEKDAYS[int(resolved['WEEKLY_WEEKDAY'])]} *-*-* "
                            f"{int(resolved['WEEKLY_HOUR']):02d}:{int(resolved['WEEKLY_MINUTE']):02d}:00 {zone}"],
        }
        result = {}
        for name in TIMER_NAMES:
            path = self.unit_dir / name
            original = self._original[path]
            result[path] = (_fresh_timer(name, calendars[name]) if original is None
                            else _replace_calendars(original, name, calendars[name]))
        return result
