#!/usr/bin/env python3
"""Foreground Docker scheduler with per-dispatch immutable runtime snapshots.

External-file mode deliberately arms *after* its first observed minute, and
again after a timing/timezone change or invalid->valid recovery. It never
backfills that minute. Env-only deployments retain immediate-start behavior.
Each poll selects its due windows once. Each new task reloads and validates
the file; a forward-moving clock retains selected windows, while a changed
schedule, invalid config or clock rollback cancels the remaining selection.
Running children keep their original environment. External mode uses bounded
history for all completed DST windows, including weekly partial deliveries;
env-only mode retains its original plain state. Invalid state fails closed.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from deploy.docker.runtime_config import RuntimeConfigError, RuntimeSnapshot, _read_private_file, load_runtime_config
from deploy.envfile import ConfigError, atomic_write

STATE_PATH = Path("output/meta/docker-scheduler.json")
# Compatibility names; effective settings come from each runtime snapshot.
TIMEZONE = ZoneInfo("Asia/Shanghai")
CRAWLER_MINUTE = 0
PUSH_TIMES = {"07:00", "12:00", "18:00", "22:00"}
WEEKLY_WEEKDAY = 6
WEEKLY_HOUR = 12
WEEKLY_MINUTE = 30
POLL_SECONDS = 20
MAX_ATTEMPTS_PER_WINDOW = 3
PARTIAL_EMAIL_EXIT_CODE = 6
STOP = False
# At most one completion/task/UTC minute: 4096 entries retain at least 68 hours,
# longer than IANA's largest backward offset transition (24 hours). No secrets
# are stored. Old top-level task markers remain for state/API compatibility.
MAX_COMPLETED_WINDOWS = 4096
MAX_STATE_BYTES = 4 * 1024 * 1024
TASKS = ("crawler", "weekly")


class SchedulerStateError(ValueError):
    def __init__(self):
        super().__init__("scheduler state is invalid or unavailable; restore it before restarting")


def _stop(_signum: int, _frame: object) -> None:
    global STOP
    STOP = True


def _parse_marker(value: object, *, utc: bool | None = None) -> datetime:
    if not isinstance(value, str):
        raise SchedulerStateError()
    is_utc = value.endswith("Z")
    if utc is not None and is_utc != utc:
        raise SchedulerStateError()
    pattern = "%Y-%m-%dT%H:%MZ" if is_utc else "%Y-%m-%dT%H:%M"
    try:
        parsed = datetime.strptime(value, pattern)
        if parsed.strftime(pattern) != value:
            raise ValueError
        return parsed.replace(tzinfo=timezone.utc) if is_utc else parsed
    except ValueError:
        raise SchedulerStateError() from None


def _validate_state(state: object) -> dict:
    if not isinstance(state, dict) or set(state) - {*TASKS, "_windows"}:
        raise SchedulerStateError()
    for name in TASKS:
        if name in state:
            _parse_marker(state[name])
    history = state.get("_windows", {})
    if not isinstance(history, dict) or set(history) - set(TASKS):
        raise SchedulerStateError()
    for name, windows in history.items():
        if not isinstance(windows, list) or not 1 <= len(windows) <= MAX_COMPLETED_WINDOWS:
            raise SchedulerStateError()
        previous = ""
        for window in windows:
            if not isinstance(window, dict) or set(window) != {"utc", "local", "timezone"}:
                raise SchedulerStateError()
            actual = _parse_marker(window["utc"], utc=True)
            _parse_marker(window["local"], utc=False)
            zone = window["timezone"]
            try:
                if not isinstance(zone, str) or not 1 <= len(zone) <= 255:
                    raise ValueError
                local = actual.astimezone(ZoneInfo(zone)).strftime("%Y-%m-%dT%H:%M")
            except (ValueError, ZoneInfoNotFoundError, OverflowError):
                raise SchedulerStateError() from None
            if local != window["local"] or window["utc"] <= previous:
                raise SchedulerStateError()
            previous = window["utc"]
        if state.get(name) not in (windows[-1]["utc"], windows[-1]["local"]):
            raise SchedulerStateError()
    return state


def _unique_object(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise SchedulerStateError()
        result[key] = value
    return result


def _load_state() -> dict:
    try:
        content = _read_private_file(STATE_PATH, max_bytes=MAX_STATE_BYTES, require_private=False)
        return _validate_state(json.loads(content, object_pairs_hook=_unique_object))
    except RuntimeConfigError as error:
        if error.code == "missing":
            # Only genuinely absent state is a new installation. Symlinks,
            # nonregular files and unreadable paths must not reset history.
            return {}
        raise SchedulerStateError() from None
    except (OSError, UnicodeError, ValueError, RecursionError):
        raise SchedulerStateError() from None


def _save_state(state: dict) -> None:
    try:
        atomic_write(STATE_PATH, (json.dumps(state, indent=2) + "\n").encode())
    except (OSError, ConfigError):
        raise SchedulerStateError() from None


def _run(name: str, command: list[str], marker: str, state: dict,
         env: Mapping[str, str] | None = None, *, window: dict[str, str] | None = None) -> bool:
    """Keep the original four-argument API; scheduler supplies env and window."""
    windows = list(state.get("_windows", {}).get(name, []))
    if window is not None and not windows and name in state:
        # Preserve the only known pre-history completion before replacing its
        # top-level marker. Legacy local markers lack a timezone; interpret
        # them in the current zone, using the first occurrence of a fold.
        zone = ZoneInfo(window["timezone"])
        previous = _parse_marker(state[name])
        if previous.tzinfo is None:
            previous = previous.replace(tzinfo=zone)
        previous = previous.astimezone(timezone.utc)
        previous_utc = previous.strftime("%Y-%m-%dT%H:%MZ")
        if previous_utc < window["utc"]:
            windows.append({"utc": previous_utc, "local": previous.astimezone(zone).strftime("%Y-%m-%dT%H:%M"),
                            "timezone": zone.key})
    print(f"[scheduler] starting {name}: {marker}", flush=True)
    try:
        kwargs = {"env": dict(env)} if env is not None else {}
        result = subprocess.run(command, check=False, **kwargs)
    except OSError:
        print(f"[scheduler] {name} could not start; it remains eligible for retry", flush=True)
        return False
    print(f"[scheduler] {name} exited with {result.returncode}", flush=True)
    partial = name == "weekly" and result.returncode == PARTIAL_EMAIL_EXIT_CODE
    if partial:
        print("[scheduler] weekly partially delivered; no automatic whole-task retry", flush=True)
    elif result.returncode != 0:
        print(f"[scheduler] {name} failed; it remains eligible for retry", flush=True)
        return False
    state[name] = marker
    if window is not None:
        windows.append(dict(window))
        state.setdefault("_windows", {})[name] = windows[-MAX_COMPLETED_WINDOWS:]
    else:
        # Opting out of external-file mode restores the original state shape;
        # do not persist an old external ledger with now-stale last markers.
        state.pop("_windows", None)
    _save_state(state)
    return not partial


class Scheduler:
    """Testable polls with no file watcher, catch-up queue or stale secret cache."""

    def __init__(self, loader: Callable[[], RuntimeSnapshot], state: dict | None = None,
                 *, clock: Callable[[], datetime] | None = None):
        self.loader = loader
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.state = _load_state() if state is None else _validate_state(state)
        self.attempts: dict[str, tuple[str, int]] = {}
        self.signature: tuple | None = None
        self.blocked_minute: str | None = None
        self.latest_minute: str | None = None
        self.latest_instant: datetime | None = None
        self.error_code: str | None = None
        self.invalid = False
        self.poll_seconds = POLL_SECONDS

    def _refresh(self, now: datetime | None) -> tuple[RuntimeSnapshot, datetime, str] | None:
        try:
            snapshot = self.loader()
        except RuntimeConfigError as error:
            if error.code != self.error_code:
                print(f"[scheduler] paused: {error}", flush=True)
            self.error_code = error.code
            self.invalid = True
            return None

        settings = snapshot.settings
        self.poll_seconds = settings.poll_seconds
        local_now = (self.clock() if now is None else now).astimezone(settings.timezone)
        actual_now = local_now.astimezone(timezone.utc)
        actual_marker = actual_now.strftime("%Y-%m-%dT%H:%MZ")
        changed = self.signature != settings.timing_signature
        if snapshot.external and (changed or self.invalid):
            # Suppress the whole current minute, not only this poll/dispatch.
            self.blocked_minute = actual_marker
        if changed or self.invalid:
            print("[scheduler] configuration accepted" +
                  ("; current minute is not backfilled" if snapshot.external else "; legacy env-only mode"), flush=True)
        self.signature = settings.timing_signature
        self.error_code = None
        self.invalid = False

        if self.latest_instant is not None and actual_now < self.latest_instant:
            # Cancel even a within-minute rollback. Keep the high-water clock
            # and arm beyond its minute so catch-up cannot revive the canceled
            # pending window at the first formerly observed timestamp.
            self.blocked_minute = self.latest_minute
            return None
        self.latest_instant = actual_now
        self.latest_minute = actual_marker
        if self.blocked_minute == actual_marker:
            return None
        return snapshot, local_now, actual_marker

    def _completed(self, name: str, local_now: datetime, actual_marker: str) -> bool:
        local_marker = local_now.strftime("%Y-%m-%dT%H:%M")
        completed = self.state.get(name, "")
        if completed == local_marker:
            return True
        if completed.endswith("Z"):
            if completed >= actual_marker:
                return True
            # Compatibility with pre-history state (UTC-only completion).
            if _parse_marker(completed).astimezone(local_now.tzinfo).strftime("%Y-%m-%dT%H:%M") == local_marker:
                return True
        for window in self.state.get("_windows", {}).get(name, []):
            if window["utc"] >= actual_marker:
                return True
            # Project old completions into the currently selected timezone.
            # Equivalent zones/aliases must not discard earlier fold windows.
            completed_local = (window["local"] if window["timezone"] == local_now.tzinfo.key else
                               _parse_marker(window["utc"]).astimezone(local_now.tzinfo).strftime("%Y-%m-%dT%H:%M"))
            if completed_local == local_marker:
                return True
        return False

    def poll(self, now: datetime | None = None) -> int:
        # Explicit now fixes time only for deterministic single-instant tests.
        # Select due business windows once, not a stale environment. Reload
        # before dispatch but never add new tasks just because a child ran long.
        selected = self._refresh(now)
        if selected is None:
            return self.poll_seconds
        initial, selected_local, selected_actual = selected
        selected_settings = initial.settings
        selected_marker = (selected_actual if initial.external else selected_local.strftime("%Y-%m-%dT%H:%M"))
        due = {
            "crawler": (selected_local.minute == selected_settings.crawler_minute
                        or selected_local.strftime("%H:%M") in selected_settings.push_times),
            "weekly": (selected_local.weekday() == selected_settings.weekly_weekday
                       and selected_local.hour == selected_settings.weekly_hour
                       and selected_local.minute == selected_settings.weekly_minute),
        }
        commands = {
            "crawler": [sys.executable, "-m", "trendradar"],
            "weekly": [sys.executable, "weekly_report/weekly_ai_report_email.py"],
        }
        for name in TASKS:
            if not due[name]:
                continue
            if STOP:
                break
            fresh = self._refresh(now)
            if fresh is None:
                break
            snapshot, _local_now, actual_marker = fresh
            settings = snapshot.settings
            if settings.timing_signature != selected_settings.timing_signature or snapshot.external != initial.external:
                self.blocked_minute = actual_marker
                break
            if self._completed(name, selected_local, selected_actual):
                continue
            previous, count = self.attempts.get(name, ("", 0))
            if previous != selected_marker:
                count = 0
            if count < settings.max_attempts:
                self.attempts[name] = (selected_marker, count + 1)
                window = ({"utc": selected_actual, "local": selected_local.strftime("%Y-%m-%dT%H:%M"),
                           "timezone": settings.timezone.key} if snapshot.external else None)
                _run(name, commands[name], selected_marker, self.state, env=snapshot.env, window=window)
        return self.poll_seconds


def main(base_env: Mapping[str, str] | None = None) -> int:
    base = dict(os.environ if base_env is None else base_env)
    loader = lambda: load_runtime_config(base)
    try:
        loader()  # Initial bad/missing external config is a hard startup failure.
    except RuntimeConfigError as error:
        print(f"[scheduler] cannot start: {error}", file=sys.stderr, flush=True)
        return 2
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        scheduler = Scheduler(loader)
        while not STOP:
            time.sleep(scheduler.poll())
    except SchedulerStateError as error:
        print(f"[scheduler] cannot continue: {error}", file=sys.stderr, flush=True)
        return 2
    print("[scheduler] stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
