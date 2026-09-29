#!/usr/bin/env python3
# coding=utf-8
"""Optional local SQLite -> R2/S3 backup, shared by Linux and Docker.

``--configured`` reads only the process environment for backup/credential
settings. Application YAML is read safely, without importing the application,
solely to select its local data directory. The legacy explicit CLI remains
available without the automatic-backup switch. No env files are sourced.

Whole date-keyed objects are replaced, not merged. The advisory lock coordinates
only processes sharing a local output directory; deployments still need one
writer across hosts (in particular before switching to remote GitHub Actions).
"""
from __future__ import annotations

import argparse
import fcntl
import os
import re
import sqlite3
import stat
import sys
import tempfile
import time
from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional, Sequence
from urllib.parse import quote
from zoneinfo import ZoneInfo

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deploy.backup_settings import (
    BackupConfigError,
    DEFAULT_LOOKBACK_DAYS,
    DEFAULT_TIMEZONE,
    REQUIRED_S3_KEYS,
    load_backup_settings,
    validate_s3_endpoint,
)

DB_TYPES = ("news", "rss")
DEFAULT_UPLOAD_ATTEMPTS = 3
SNAPSHOT_TIMEOUT_SECONDS = 30
PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOCK_NAME = ".r2-backup.lock"


class BackupBusyError(RuntimeError):
    """Another process owns the backup lock for this local data directory."""


class BackupUploadError(RuntimeError):
    """Safe final upload error retaining only an exception class/status."""


def _parse_date(value: str) -> date:
    try:
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            raise ValueError
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("日期必须是 YYYY-MM-DD 格式") from None


def dates_to_sync(
    requested_date: Optional[date], timezone_name: str, lookback_days: int,
) -> list[str]:
    """Return newest-first date strings for the requested local timezone."""
    if not 1 <= lookback_days <= 3660:
        raise ValueError("lookback_days must be in the range 1..3660")
    base_date = requested_date or datetime.now(ZoneInfo(timezone_name)).date()
    return [(base_date - timedelta(days=offset)).isoformat() for offset in range(lookback_days)]


def local_database_targets(data_dir: Path, dates: Iterable[str]) -> list[tuple[str, Path, str]]:
    """Find only news/rss date files; callers cannot escape using a date path."""
    targets = []
    for date_str in dates:
        _parse_date(date_str)
        for db_type in DB_TYPES:
            local_path = data_dir / db_type / f"{date_str}.db"
            if local_path.is_file():
                targets.append((db_type, local_path, f"{db_type}/{date_str}.db"))
    return targets


def sqlite_quick_check(path: Path) -> None:
    """Raise when a snapshot is not readable and consistent; never echo rows."""
    uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        result = connection.execute("PRAGMA quick_check").fetchone()
        if not result or result[0] != "ok":
            raise RuntimeError("SQLite quick_check failed")


@contextmanager
def sqlite_snapshot(path: Path):
    """Yield a private, checked standalone snapshot including committed WAL.

    The source is read-only and never checkpointed. All retries use the same
    snapshot. The private temporary directory is removed on success or failure.
    """
    if path.stat().st_size == 0:
        raise ValueError("Refusing to back up an empty database")
    with tempfile.TemporaryDirectory(prefix="trendradar_r2_snapshot_") as directory:
        snapshot_path = Path(directory) / path.name
        uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"
        deadline = time.monotonic() + SNAPSHOT_TIMEOUT_SECONDS

        def check_deadline(status, remaining, total):
            if time.monotonic() > deadline:
                raise TimeoutError("SQLite snapshot timed out")

        # Create 0600 before sqlite opens it, rather than chmod after creation.
        descriptor = os.open(snapshot_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        with closing(sqlite3.connect(uri, uri=True)) as source:
            with closing(sqlite3.connect(snapshot_path)) as snapshot:
                source.backup(snapshot, pages=256, progress=check_deadline, sleep=0.05)
                mode = snapshot.execute("PRAGMA journal_mode=DELETE").fetchone()
                if not mode or mode[0].lower() != "delete":
                    raise RuntimeError("Cannot produce a standalone SQLite snapshot")
        sqlite_quick_check(snapshot_path)
        yield snapshot_path


@contextmanager
def backup_lock(data_dir: Path):
    """Nonblocking host-local advisory lock; never unlink the shared lock inode.

    Use the resolved data directory so relative paths/symlink aliases share the
    same lock. The directory must already exist. No locks are made for dry-run.
    """
    lock_path = data_dir.resolve() / LOCK_NAME
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RuntimeError("Invalid local backup lock")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BackupBusyError("Another local R2 backup is running") from None
        try:
            os.fchmod(descriptor, 0o600)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _required_environment(environ: Mapping[str, str]) -> dict[str, str]:
    if any(not environ.get(name, "").strip() for name in REQUIRED_S3_KEYS):
        raise BackupConfigError("credentials")
    validate_s3_endpoint(environ["S3_ENDPOINT_URL"])
    return {
        "bucket": environ["S3_BUCKET_NAME"],
        "access_key_id": environ["S3_ACCESS_KEY_ID"],
        "secret_access_key": environ["S3_SECRET_ACCESS_KEY"],
        "endpoint_url": environ["S3_ENDPOINT_URL"],
        "region": environ.get("S3_REGION") or "auto",
    }


def create_s3_client(environ: Optional[Mapping[str, str]] = None):
    """Construct with explicit credentials; never fall back to SDK discovery."""
    settings = _required_environment(os.environ if environ is None else environ)
    try:
        import boto3
        from botocore.config import Config as BotoConfig
    except ImportError:
        raise RuntimeError("R2 backup requires boto3") from None
    return boto3.client(
        "s3",
        endpoint_url=settings["endpoint_url"],
        aws_access_key_id=settings["access_key_id"],
        aws_secret_access_key=settings["secret_access_key"],
        region_name=settings["region"],
        config=BotoConfig(
            s3={"addressing_style": "virtual"}, signature_version="s3v4",
            # The explicit retry loop below is the single retry owner.
            retries={"mode": "standard", "total_max_attempts": 1},
        ),
    ), settings["bucket"]


def _exception_chain(exc: Exception):
    """Inspect SDK wrapper causes without rendering their potentially secret text."""
    seen = set()
    pending = [exc]
    while pending and len(seen) < 8:
        current = pending.pop()
        if not isinstance(current, Exception) or id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        pending.extend((current.__context__, current.__cause__))


def _http_status(exc: Exception) -> Optional[int]:
    for current in _exception_chain(exc):
        response = getattr(current, "response", None)
        if isinstance(response, dict):
            metadata = response.get("ResponseMetadata")
            if isinstance(metadata, dict):
                status_code = metadata.get("HTTPStatusCode")
                if isinstance(status_code, int) and not isinstance(status_code, bool) and 100 <= status_code <= 599:
                    return status_code
    return None


def _error_summary(exc: Exception) -> str:
    # Wrapped upload errors already contain only this generated summary.
    if isinstance(exc, BackupUploadError):
        return str(exc)
    name = type(exc).__name__
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", name):
        name = "Exception"
    status_code = _http_status(exc)
    return name if status_code is None else f"{name} HTTP {status_code}"


def _authentication_error(exc: Exception) -> bool:
    if _http_status(exc) in {401, 403}:
        return True
    for current in _exception_chain(exc):
        if type(current).__name__ in {"NoCredentialsError", "PartialCredentialsError", "CredentialRetrievalError"}:
            return True
        response = getattr(current, "response", None)
        if isinstance(response, dict) and isinstance(response.get("Error"), dict):
            if response["Error"].get("Code") in {
                "AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch",
                "ExpiredToken", "ExpiredTokenException", "InvalidToken", "TokenRefreshRequired",
            }:
                return True
    return False


def upload_target(
    client, bucket: str, local_path: Path, object_key: str,
    attempts: int, sleep_func: Callable[[float], None],
) -> None:
    """Upload one private snapshot and HEAD-check size, with bounded retries."""
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    file_size = local_path.stat().st_size
    failure = "Exception"
    for attempt in range(1, attempts + 1):
        try:
            client.upload_file(
                str(local_path), bucket, object_key,
                ExtraArgs={"ContentType": "application/x-sqlite3"},
            )
            metadata = client.head_object(Bucket=bucket, Key=object_key)
            if metadata.get("ContentLength") != file_size:
                raise RuntimeError("Remote object size validation failed")
            print(f"[R2备份] 已上传数据库 ({file_size} bytes)")
            return
        except Exception as exc:  # SDKs expose several exception classes.
            failure = _error_summary(exc)
            if _authentication_error(exc) or attempt == attempts:
                break
            wait_seconds = 2 ** (attempt - 1)
            print(f"[R2备份] 上传失败: {failure}；{wait_seconds} 秒后重试 ({attempt}/{attempts})")
            sleep_func(wait_seconds)
    # Raise outside except and suppress context: SDK messages/URLs/keys are not
    # retained in the safe error or rendered by the CLI traceback machinery.
    raise BackupUploadError(failure) from None


def sync_databases(
    data_dir: Path, dates: Sequence[str], dry_run: bool = False,
    environ: Optional[Mapping[str, str]] = None, client=None,
    bucket: Optional[str] = None, attempts: int = DEFAULT_UPLOAD_ATTEMPTS,
    sleep_func: Callable[[float], None] = time.sleep,
) -> int:
    """Back up selected databases; zero only if every found file succeeded."""
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    targets = local_database_targets(data_dir, dates)
    if not targets:
        print("[R2备份] 未找到待备份数据库", file=sys.stderr)
        return 1
    print(f"[R2备份] 待处理 {len(targets)} 个数据库")
    if dry_run:
        for _, local_path, object_key in targets:
            print(f"[R2备份] dry-run: {local_path} -> {object_key}")
        return 0

    # Acquire before SDK construction, snapshots or uploads, including for
    # manual callers. Failure or process exit releases flock automatically.
    with backup_lock(data_dir):
        if client is None or bucket is None:
            client, bucket = create_s3_client(environ)
        failures = 0
        for _, local_path, object_key in targets:
            try:
                with sqlite_snapshot(local_path) as snapshot_path:
                    upload_target(client, bucket, snapshot_path, object_key, attempts, sleep_func)
            except Exception as exc:
                failures += 1
                print(f"[R2备份] 数据库备份失败: {_error_summary(exc)}", file=sys.stderr)
        if failures:
            print(f"[R2备份] 完成但有 {failures} 个文件失败", file=sys.stderr)
            return 1
    print("[R2备份] 全部完成")
    return 0


def configured_data_dir(environ: Mapping[str, str], project_root: Optional[Path] = None) -> Path:
    """Honor application CONFIG_PATH and storage.local.data_dir read-only.

    Relative YAML/data paths resolve against the project working directory, as
    the application does under the shipped Linux/Docker jobs. Missing/invalid
    YAML fails closed rather than uploading an unrelated default output tree.
    Docker's persistent volume is fixed to /app/output, so other roots fail.
    """
    root = (PROJECT_ROOT if project_root is None else project_root).resolve()
    config_path = Path(environ.get("CONFIG_PATH") or "config/config.yaml")
    if not config_path.is_absolute():
        config_path = root / config_path
    try:
        import yaml
        with config_path.open(encoding="utf-8") as stream:
            document = yaml.safe_load(stream)
    except Exception:
        raise BackupConfigError("config") from None
    if not isinstance(document, dict):
        raise BackupConfigError("config")
    storage = document.get("storage", {})
    if not isinstance(storage, dict) or not isinstance(storage.get("local", {}), dict):
        raise BackupConfigError("data_dir")
    value = storage.get("local", {}).get("data_dir", "output")
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise BackupConfigError("data_dir")
    data_dir = Path(value)
    if not data_dir.is_absolute():
        data_dir = root / data_dir
    data_dir = data_dir.resolve()
    if environ.get("DOCKER_CONTAINER", "").lower() == "true" and data_dir != Path("/app/output").resolve():
        raise BackupConfigError("docker_data_dir")
    return data_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="独立同步 TrendRadar 的 news/rss SQLite 数据库到 R2")
    parser.add_argument("--configured", action="store_true", help="按进程环境运行可选自动备份；默认关闭")
    parser.add_argument("--data-dir", type=Path, help="手动模式数据目录，默认 output")
    parser.add_argument("--date", type=_parse_date, help="手动指定基准日期，默认指定时区的当天")
    parser.add_argument("--timezone", help=f"手动模式日期计算时区，默认 {DEFAULT_TIMEZONE}")
    parser.add_argument("--lookback-days", type=int, help="手动模式备份最近多少天，默认 2 天")
    parser.add_argument("--attempts", type=int, default=DEFAULT_UPLOAD_ATTEMPTS, help="每个对象最多尝试次数，默认 3 次")
    parser.add_argument("--dry-run", action="store_true", help="只列举，不创建锁/快照/S3客户端；configured模式仍验证启用设置")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.configured:
            settings = load_backup_settings(os.environ)
            if not settings.enabled:
                print("[R2备份] 未启用；跳过")
                return 0
            if any(value is not None for value in (args.date, args.data_dir, args.timezone, args.lookback_days)):
                raise BackupConfigError("arguments")
            data_dir = configured_data_dir(os.environ)
            timezone_name = settings.timezone
            lookback_days = settings.lookback_days
        else:
            data_dir = args.data_dir if args.data_dir is not None else Path("output")
            timezone_name = args.timezone or DEFAULT_TIMEZONE
            lookback_days = args.lookback_days if args.lookback_days is not None else DEFAULT_LOOKBACK_DAYS
        dates = dates_to_sync(args.date, timezone_name, lookback_days)
        return sync_databases(data_dir, dates, dry_run=args.dry_run, attempts=args.attempts)
    except BackupConfigError as exc:
        print(f"[R2备份] 配置错误: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"[R2备份] 失败: {_error_summary(exc)}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
