"""Runtime configuration and date helpers for weekly reports."""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional, Tuple

from trendradar.context import AppContext
from trendradar.core.loader import load_config
from trendradar.utils.time import DEFAULT_TIMEZONE

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def build_report_context() -> AppContext:
    """Load the same application timezone configuration used by daily reports."""
    config_path = PROJECT_ROOT / "config" / "config.yaml"
    try:
        return AppContext(load_config(str(config_path)))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return AppContext({"TIMEZONE": DEFAULT_TIMEZONE})

REPORT_CONTEXT = build_report_context()


def report_now() -> datetime:
    """Return the current time in the report display timezone."""
    return REPORT_CONTEXT.get_time()


def load_runtime_env() -> Dict[str, str]:
    """Read only EMAIL_* values from the process environment."""
    return {key: value for key, value in os.environ.items() if key.startswith("EMAIL_")}


def resolve_date_range(start: Optional[str], end: Optional[str]) -> Tuple[datetime, datetime]:
    if bool(start) ^ bool(end):
        raise SystemExit("--start 和 --end 必须一起传")
    if start and end:
        start_date = datetime.strptime(start, "%Y-%m-%d")
        end_date = datetime.strptime(end, "%Y-%m-%d")
        if start_date > end_date:
            raise SystemExit("--start 不能晚于 --end")
        return start_date, end_date
    end_date = report_now().replace(tzinfo=None)
    return end_date - timedelta(days=6), end_date
