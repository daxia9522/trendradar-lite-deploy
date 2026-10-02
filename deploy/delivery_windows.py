"""Dependency-free validation of the four deployment delivery windows.

A launch time opens admission until that hour's :59; two deployment slots
therefore cannot share an hour, even if their launch minutes differ.
Custom YAML ranges are validated separately by the application Scheduler.
"""
from __future__ import annotations

import re
from typing import Mapping


DELIVERY_DEFAULTS = {
    "MORNING_PUSH_TIME": "07:00",
    "NOON_PUSH_TIME": "12:00",
    "EVENING_PUSH_TIME": "18:00",
    "DAILY_SUMMARY_TIME": "22:00",
}


class DeliveryWindowError(ValueError):
    """Value-free error; callers may translate the stable code for their UI."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(
            "Delivery windows must use different hours; each ends at HH:59"
            if code == "overlap" else "Delivery times must use HH:MM"
        )


def delivery_times(values: Mapping[str, str]) -> tuple[str, ...]:
    """Resolve missing/empty stock defaults and reject malformed/overlapping slots."""
    times = tuple(values.get(key) or default for key, default in DELIVERY_DEFAULTS.items())
    if any(not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value) for value in times):
        raise DeliveryWindowError("format")
    if len({value[:2] for value in times}) != len(times):
        raise DeliveryWindowError("overlap")
    return times
