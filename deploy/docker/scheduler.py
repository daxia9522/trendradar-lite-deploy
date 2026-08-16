#!/usr/bin/env python3
"""Small foreground scheduler for the Docker Compose deployment."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


STATE_PATH = Path("output/meta/docker-scheduler.json")
TIMEZONE = ZoneInfo(os.environ.get("TZ", "Asia/Shanghai"))
CRAWLER_MINUTE = int(os.environ.get("CRAWLER_MINUTE", "0"))
PUSH_TIMES = {
    os.environ.get("MORNING_PUSH_TIME", "07:00"),
    os.environ.get("NOON_PUSH_TIME", "12:00"),
    os.environ.get("EVENING_PUSH_TIME", "18:00"),
    os.environ.get("DAILY_SUMMARY_TIME", "22:00"),
}
WEEKLY_WEEKDAY = int(os.environ.get("WEEKLY_WEEKDAY", "6"))  # Monday=0
WEEKLY_HOUR = int(os.environ.get("WEEKLY_HOUR", "12"))
WEEKLY_MINUTE = int(os.environ.get("WEEKLY_MINUTE", "30"))
POLL_SECONDS = max(10, int(os.environ.get("SCHEDULER_POLL_SECONDS", "20")))
STOP = False


def _stop(_signum: int, _frame: object) -> None:
    global STOP
    STOP = True


def _load_state() -> dict[str, str]:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(state: dict[str, str]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def _run(name: str, command: list[str], marker: str, state: dict[str, str]) -> None:
    print(f"[scheduler] starting {name}: {marker}", flush=True)
    result = subprocess.run(command, check=False)
    state[name] = marker
    _save_state(state)
    print(f"[scheduler] {name} exited with {result.returncode}", flush=True)


def main() -> int:
    if not 0 <= CRAWLER_MINUTE <= 59:
        raise ValueError("CRAWLER_MINUTE must be between 0 and 59")
    if not 0 <= WEEKLY_WEEKDAY <= 6:
        raise ValueError("WEEKLY_WEEKDAY must be between 0 and 6")

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    state = _load_state()
    print(
        "[scheduler] ready: crawler hourly at minute "
        f"{CRAWLER_MINUTE:02d}; weekly at weekday={WEEKLY_WEEKDAY} "
        f"{WEEKLY_HOUR:02d}:{WEEKLY_MINUTE:02d} ({TIMEZONE.key}); "
        f"deliveries at {', '.join(sorted(PUSH_TIMES))}",
        flush=True,
    )

    while not STOP:
        now = datetime.now(TIMEZONE)
        minute_marker = now.strftime("%Y-%m-%dT%H:%M")

        crawler_due = now.minute == CRAWLER_MINUTE or now.strftime("%H:%M") in PUSH_TIMES
        if crawler_due and state.get("crawler") != minute_marker:
            _run("crawler", [sys.executable, "-m", "trendradar"], minute_marker, state)

        weekly_due = (
            now.weekday() == WEEKLY_WEEKDAY
            and now.hour == WEEKLY_HOUR
            and now.minute == WEEKLY_MINUTE
        )
        if weekly_due and state.get("weekly") != minute_marker:
            _run(
                "weekly",
                [sys.executable, "weekly_report/weekly_ai_report_email.py"],
                minute_marker,
                state,
            )

        time.sleep(POLL_SECONDS)

    print("[scheduler] stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
