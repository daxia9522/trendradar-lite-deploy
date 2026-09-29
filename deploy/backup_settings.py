"""Validated optional R2 backup settings, with no application or SDK imports.

Only explicit local-storage deployments may enable automatic whole-database
uploads. Diagnostics are selected from fixed strings and never echo values.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_BACKUP_TIME = "23:40"
DEFAULT_LOOKBACK_DAYS = 2
DEFAULT_TIMEZONE = "Asia/Shanghai"
REQUIRED_S3_KEYS = (
    "S3_BUCKET_NAME", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY", "S3_ENDPOINT_URL",
)
_ERRORS = {
    "enabled": "R2_BACKUP_ENABLED must be true, false, 1 or 0",
    "time": "R2_BACKUP_TIME must be HH:MM in the range 00:00..23:59",
    "lookback": "R2_BACKUP_LOOKBACK_DAYS must be an integer in the range 1..3660",
    "timezone": "Backup timezone must be a valid IANA timezone",
    "timezone_conflict": "TIMEZONE and TZ must agree for R2 backup",
    "backend": "Enabled R2 backup requires explicit STORAGE_BACKEND=local",
    "credentials": "Enabled R2 backup requires all four S3 credential settings",
    "endpoint": "S3_ENDPOINT_URL must be an HTTP(S) URL without userinfo, query or fragment",
    "config": "Automatic R2 backup requires a readable, valid application YAML configuration",
    "data_dir": "Automatic R2 backup requires a nonempty string storage.local.data_dir",
    "docker_data_dir": "Docker automatic R2 backup requires storage.local.data_dir to resolve to /app/output",
    "arguments": "Configured R2 backup uses environment settings; date and data-directory overrides are not allowed",
}


class BackupConfigError(ValueError):
    """A fixed, value-free diagnostic; unknown codes cannot leak input."""

    def __init__(self, code: str = "config"):
        self.code = code if code in _ERRORS else "config"
        super().__init__(_ERRORS[self.code])


@dataclass(frozen=True)
class BackupSettings:
    enabled: bool
    time: str
    lookback_days: int
    timezone: str


def _text(values: Mapping[str, str], name: str) -> str:
    value = values.get(name, "")
    return value.strip() if isinstance(value, str) else ""


def validate_s3_endpoint(value: str) -> None:
    """Validate structure without resolving the hostname or exposing input."""
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme.lower() in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and "?" not in value
            and "#" not in value
            and "\\" not in value
            and not any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
        )
        # Accessing .port also validates its syntax and range.
        parsed.port
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise BackupConfigError("endpoint") from None


def load_backup_settings(
    values: Mapping[str, str], *, require_credentials: bool = True,
) -> BackupSettings:
    """Read process-like values; disabled backup never needs S3 credentials.

    Empty switch means disabled; other settings are validated even when disabled
    so menus cannot save a malformed schedule. ``require_credentials=False``
    skips only completeness, not endpoint validation or local-storage identity.
    """
    enabled_value = _text(values, "R2_BACKUP_ENABLED").lower()
    if enabled_value not in {"", "true", "false", "1", "0"}:
        raise BackupConfigError("enabled")
    enabled = enabled_value in {"true", "1"}
    backup_time = _text(values, "R2_BACKUP_TIME") or DEFAULT_BACKUP_TIME
    if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", backup_time):
        raise BackupConfigError("time")
    raw_days = _text(values, "R2_BACKUP_LOOKBACK_DAYS") or str(DEFAULT_LOOKBACK_DAYS)
    if not re.fullmatch(r"[0-9]{1,4}", raw_days) or not 1 <= int(raw_days) <= 3660:
        raise BackupConfigError("lookback")

    timezone_value = _text(values, "TIMEZONE")
    tz_value = _text(values, "TZ")
    if timezone_value and tz_value and timezone_value != tz_value:
        raise BackupConfigError("timezone_conflict")
    timezone_name = timezone_value or tz_value or DEFAULT_TIMEZONE
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise BackupConfigError("timezone") from None

    if enabled:
        if _text(values, "STORAGE_BACKEND") != "local":
            raise BackupConfigError("backend")
        if require_credentials and any(not _text(values, name) for name in REQUIRED_S3_KEYS):
            raise BackupConfigError("credentials")
        endpoint = _text(values, "S3_ENDPOINT_URL")
        if endpoint:
            validate_s3_endpoint(endpoint)

    return BackupSettings(enabled, backup_time, int(raw_days), timezone_name)
