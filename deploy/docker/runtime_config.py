"""Docker runtime configuration: one private-file snapshot, never shell evaluation.

Public menu API: detect_format(bytes), parse_runtime_env(bytes),
read_runtime_env(path), validate_runtime_values(mapping). Formats are *syntax*,
not deployment targets: unmarked files use EnvDocument's ``linux`` literal
syntax; only the historical docker marker enables Compose's $$ decoding.

In external-file mode the application's environment is rebuilt from scratch.
Only BASE_ENV_KEYS survive from the image/process; application keys use the
explicit APP_KEYS / APP_PREFIXES policy below. Unknown file keys fail closed.
Deleting a key therefore falls back to application/YAML defaults, never to an
old process secret. The runtime path, executable settings and Docker/local
storage identity cannot be reconfigured by the file. Legacy env-only mode is
retained when (and only when) TRENDRADAR_RUNTIME_ENV is absent.
"""
from __future__ import annotations

import errno
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Use exactly the literal parser behind read_env / EnvDocument, on bytes from
# our single securely opened descriptor (their path I/O would reopen the file).
from deploy.envfile import ConfigError, _records

RUNTIME_ENV_KEY = "TRENDRADAR_RUNTIME_ENV"
DOCKER_MARKER = b"# TrendRadar env format: docker\n"
MAX_ENV_BYTES = 1024 * 1024
BASE_ENV_KEYS = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR",
    "PYTHONPATH", "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE",
    "PYTHONIOENCODING", "PYTHONUTF8", "PYTHONHASHSEED",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "LITELLM_LOCAL_MODEL_COST_MAP",
})
APP_PREFIXES = ("AI_", "EMAIL_", "S3_", "STORAGE_", "SCHEDULE_", "SCHEDULER_", "PLATFORMS_")
APP_KEYS = frozenset({
    "TZ", "TIMEZONE", "DEBUG", "CONFIG_PATH", "FREQUENCY_WORDS_PATH",
    "SORT_BY_POSITION_FIRST", "MAX_NEWS_PER_KEYWORD", "LOCAL_RETENTION_DAYS",
    "REMOTE_RETENTION_DAYS", "PULL_ENABLED", "PULL_DAYS", "CRAWLER_MINUTE",
    "MORNING_PUSH_TIME", "NOON_PUSH_TIME", "EVENING_PUSH_TIME", "DAILY_SUMMARY_TIME",
    "WEEKLY_WEEKDAY", "WEEKLY_HOUR", "WEEKLY_MINUTE", "DOCKER_CONTAINER",
})
TIME_KEYS = ("MORNING_PUSH_TIME", "NOON_PUSH_TIME", "EVENING_PUSH_TIME", "DAILY_SUMMARY_TIME")
DEFAULT_TIMES = ("07:00", "12:00", "18:00", "22:00")
ERRORS = {
    "path": "runtime configuration path is invalid",
    "missing": "runtime configuration file is missing",
    "unreadable": "runtime configuration file cannot be read",
    "symlink": "runtime configuration path must not contain symbolic links",
    "not_regular": "runtime configuration must be a regular file",
    "permissions": "runtime configuration file or a traversed directory is writable by others",
    "too_large": "runtime configuration file is too large",
    "empty": "runtime configuration has no assignments",
    "format": "runtime configuration is not valid UTF-8 literal assignments",
    "unsupported": "runtime configuration contains an unsupported environment key",
    "locked": "runtime configuration cannot change Docker/local storage identity",
    "schedule": "runtime configuration contains invalid schedule settings",
    "value": "runtime configuration contains an invalid application setting",
}


class RuntimeConfigError(ConfigError):
    """Fixed, value-free diagnostics; .code is suitable for log deduplication."""

    def __init__(self, code: str):
        self.code = code if code in ERRORS else "format"
        super().__init__(ERRORS[self.code])


def detect_format(content: bytes) -> str:
    """Return the shared EnvDocument syntax name, from the same snapshot bytes."""
    return "docker" if content.startswith(DOCKER_MARKER) else "linux"


def parse_runtime_env(content: bytes) -> dict[str, str]:
    # This is the common final-byte contract for both menu save and runtime
    # reads. Count bytes (including comments), not decoded text or assignments.
    if len(content) > MAX_ENV_BYTES:
        raise RuntimeConfigError("too_large")
    try:
        records = _records(content.decode("utf-8"), detect_format(content))
    except (UnicodeError, ConfigError):
        raise RuntimeConfigError("format") from None
    values = {key: value for _, key, value in records if key is not None}
    if not values:
        raise RuntimeConfigError("empty")
    validate_runtime_values(values)
    return values


def _read_private_file(path: Path, *, max_bytes: int | None = None, require_private: bool = True) -> bytes:
    """Walk directories with O_NOFOLLOW, then open/fstat/read one inode.

    A directory mount sees atomic replacements on the *next* call; a concurrent
    replacement leaves this call on its original complete descriptor snapshot.
    O_NONBLOCK also prevents a malicious FIFO from hanging before fstat.
    The scheduler reuses this for non-secret state with its own byte limit;
    require_private=False permits historical 0644 state, not config files.
    With require_private=True every traversed directory must also be sealed:
    group/world-writable directories are rejected unless they carry the sticky
    bit (system paths such as /tmp), because a writable runtime/ directory lets
    another local user swap in a fresh 0600 file below the 0700 file check.
    """
    limit = MAX_ENV_BYTES if max_bytes is None else max_bytes
    descriptors: list[int] = []

    def sealed(descriptor: int) -> bool:
        # os.stat(".", dir_fd=) resolves the pinned inode like fstat, without
        # an extra os.fstat call: the inode cannot be swapped under an open
        # O_DIRECTORY descriptor, so the mode read here reflects the traversed
        # directory itself.
        info = os.stat(".", dir_fd=descriptor, follow_symlinks=False)
        return not (stat.S_IMODE(info.st_mode) & 0o022) or bool(info.st_mode & stat.S_ISVTX)

    try:
        absolute = Path(os.path.abspath(path))
        directory = os.open(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY)
        descriptors.append(directory)
        for component in absolute.parts[1:-1]:
            info = os.stat(component, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                raise RuntimeConfigError("symlink")
            directory = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            descriptors.append(directory)
            if require_private and not sealed(directory):
                raise RuntimeConfigError("permissions")
        descriptor = os.open(absolute.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        descriptors.append(descriptor)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeConfigError("not_regular")
        if require_private and stat.S_IMODE(info.st_mode) & ~0o600:
            raise RuntimeConfigError("permissions")
        if info.st_size > limit:
            raise RuntimeConfigError("too_large")
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            content = stream.read(limit + 1)
        if len(content) > limit:
            raise RuntimeConfigError("too_large")
        return content
    except RuntimeConfigError:
        raise
    except FileNotFoundError:
        raise RuntimeConfigError("missing") from None
    except OSError as error:
        code = "symlink" if error.errno == errno.ELOOP else "unreadable"
        raise RuntimeConfigError(code) from None
    except (ValueError, TypeError):
        raise RuntimeConfigError("path") from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def read_runtime_env(path: str | Path) -> dict[str, str]:
    """Read and validate one required private runtime file; never return fallback."""
    if not str(path).strip():
        raise RuntimeConfigError("path")
    return parse_runtime_env(_read_private_file(Path(path)))


def is_application_key(key: str) -> bool:
    return key in APP_KEYS or key.startswith(APP_PREFIXES)


def _integer(values: Mapping[str, str], key: str, default: int, low: int, high: int) -> int:
    try:
        value = int(values.get(key) or str(default))
        if not low <= value <= high:
            raise ValueError
        return value
    except (ValueError, TypeError):
        raise RuntimeConfigError("schedule") from None


@dataclass(frozen=True)
class ScheduleSettings:
    timezone: ZoneInfo
    crawler_minute: int
    push_times: frozenset[str]
    weekly_weekday: int
    weekly_hour: int
    weekly_minute: int
    poll_seconds: int
    max_attempts: int

    @property
    def timing_signature(self) -> tuple:
        # Poll/retry tuning and secrets are not schedule changes.
        return (self.timezone.key, self.crawler_minute, self.push_times,
                self.weekly_weekday, self.weekly_hour, self.weekly_minute)


def schedule_settings(values: Mapping[str, str]) -> ScheduleSettings:
    tz = values.get("TIMEZONE") or values.get("TZ") or "Asia/Shanghai"
    if values.get("TIMEZONE") and values.get("TZ") and values["TIMEZONE"] != values["TZ"]:
        raise RuntimeConfigError("schedule")
    try:
        timezone = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise RuntimeConfigError("schedule") from None
    times = [values.get(key) or default for key, default in zip(TIME_KEYS, DEFAULT_TIMES)]
    if any(not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value) for value in times):
        raise RuntimeConfigError("schedule")
    return ScheduleSettings(
        timezone, _integer(values, "CRAWLER_MINUTE", 0, 0, 59), frozenset(times),
        _integer(values, "WEEKLY_WEEKDAY", 6, 0, 6),
        _integer(values, "WEEKLY_HOUR", 12, 0, 23),
        _integer(values, "WEEKLY_MINUTE", 30, 0, 59),
        max(10, _integer(values, "SCHEDULER_POLL_SECONDS", 20, 1, 86400)),
        _integer(values, "SCHEDULER_MAX_ATTEMPTS", 3, 1, 100),
    )


def validate_runtime_values(values: Mapping[str, str]) -> None:
    """Validate without side effects; does not require AI or mail credentials."""
    for key, value in values.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or not is_application_key(key):
            raise RuntimeConfigError("unsupported")
        if not isinstance(value, str) or any(char in value for char in ("\0", "\r", "\n")):
            raise RuntimeConfigError("value")
    if values.get("DOCKER_CONTAINER", "true") != "true" or values.get("STORAGE_BACKEND", "local") != "local":
        raise RuntimeConfigError("locked")
    schedule_settings(values)
    for key in ("AI_ANALYSIS_ENABLED", "DEBUG", "SORT_BY_POSITION_FIRST", "SCHEDULE_ENABLED",
                "STORAGE_TXT_ENABLED", "STORAGE_HTML_ENABLED", "PULL_ENABLED"):
        if values.get(key) and values[key].lower() not in ("true", "false", "1", "0"):
            raise RuntimeConfigError("value")
    for key, low, high in (("AI_TIMEOUT", 1, 2147483647), ("EMAIL_SMTP_PORT", 1, 65535)):
        if values.get(key):
            try:
                if not low <= int(values[key]) <= high:
                    raise ValueError
            except ValueError:
                raise RuntimeConfigError("value") from None


@dataclass(frozen=True)
class RuntimeSnapshot:
    env: Mapping[str, str] = field(repr=False)
    settings: ScheduleSettings
    external: bool


def load_runtime_config(base_env: Mapping[str, str] | None = None) -> RuntimeSnapshot:
    """Build a complete child environment; never modify os.environ.

    Explicitly empty TRENDRADAR_RUNTIME_ENV is an error, not env-only mode.
    Executable and control variables can only originate from the caller/image.
    """
    base = dict(os.environ if base_env is None else base_env)
    external = RUNTIME_ENV_KEY in base
    if external:
        values = read_runtime_env(base[RUNTIME_ENV_KEY])
        environment = {key: value for key, value in base.items() if key in BASE_ENV_KEYS}
        environment.update(values)
        # A TIMEZONE-only file must not conflict with an image default TZ.
        environment.setdefault("TZ", values.get("TIMEZONE") or "Asia/Shanghai")
        environment[RUNTIME_ENV_KEY] = base[RUNTIME_ENV_KEY]
    else:
        environment = base
    environment.update(DOCKER_CONTAINER="true", STORAGE_BACKEND="local")
    settings = schedule_settings(environment)
    return RuntimeSnapshot(MappingProxyType(environment), settings, external)
