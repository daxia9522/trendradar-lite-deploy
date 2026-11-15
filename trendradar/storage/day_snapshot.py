"""Backend-aware, read-only daily SQLite snapshots.

This layer owns obtaining and opening a database, not its news/RSS projection.
Local readers see committed WAL data in one read transaction. Remote readers
always GET a fresh, standalone object into a private temporary directory; they
never borrow the writer's cached connections or initialize a missing database.
"""
from __future__ import annotations

import sqlite3
import tempfile
from contextlib import closing, contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

from trendradar.storage.sqlite_mixin import connect_sqlite


class DaySnapshotError(RuntimeError):
    """A safe source/transport diagnostic, without provider-controlled text."""


def _day_string(day: str | date) -> str:
    if isinstance(day, datetime):
        return day.date().isoformat()
    if isinstance(day, date):
        return day.isoformat()
    if isinstance(day, str):
        try:
            normalized = date.fromisoformat(day).isoformat()
        except ValueError:
            pass
        else:
            if normalized == day:
                return normalized
    raise ValueError("Source date must be YYYY-MM-DD")


def _source_key(kind: str, day: str | date) -> str:
    if kind not in {"news", "rss"}:
        raise ValueError("Invalid source kind")
    return f"{kind}/{_day_string(day)}.db"


def local_day_path(data_dir: str | Path, kind: str, day: str | date) -> Path | None:
    """Resolve an existing local day, distinguishing absence from OS errors."""
    path = Path(data_dir) / _source_key(kind, day)
    try:
        path.stat()
    except FileNotFoundError:
        return None
    return path


def object_is_missing(error: Exception) -> bool:
    """Only object absence is missing; bucket/denial/contradictory errors are not."""
    response = getattr(error, "response", None)
    if not isinstance(response, dict):
        return False
    details = response.get("Error", {})
    metadata = response.get("ResponseMetadata", {})
    if not isinstance(details, dict) or not isinstance(metadata, dict):
        return False
    status = metadata.get("HTTPStatusCode")
    if status is not None and (type(status) is not int or status != 404):
        return False
    code = details.get("Code")
    return code in ("404", "NoSuchKey", "Not Found", "NotFound") or (
        code in (None, "") and status == 404
    )


def validate_sqlite_snapshot(path: Path) -> None:
    """Validate a standalone object, never an active local WAL database."""
    with path.open("rb") as source:
        header = source.read(100)
    if len(header) != 100 or header[:16] != b"SQLite format 3\x00":
        raise ValueError("Invalid SQLite header")
    page_size = int.from_bytes(header[16:18], "big")
    if page_size == 1:
        page_size = 65536
    if not 512 <= page_size <= 65536 or page_size & (page_size - 1):
        raise ValueError("Invalid SQLite page size")
    size = path.stat().st_size
    if size % page_size:
        raise ValueError("Incomplete SQLite page")
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as conn:
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        if size != page_size * page_count:
            raise ValueError("SQLite file size does not match its page count")
        if conn.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("SQLite quick_check failed")


@contextmanager
def open_sqlite_snapshot(
    path: Path, *, max_bytes: int | None = None, immutable: bool = False,
) -> Iterator[sqlite3.Connection]:
    """Open one read transaction; never create, migrate or checkpoint a source."""
    if max_bytes is not None:
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("Source size limit must be positive")
        if path.stat().st_size > max_bytes:
            raise DaySnapshotError("Source database size limit exceeded")
    with closing(connect_sqlite(path, readonly=True, immutable=immutable)) as conn:
        conn.execute("BEGIN")
        yield conn


@contextmanager
def _remote_body(body):
    """Close a borrowed response body without leaking provider cleanup errors.

    A consumer/SQL error remains the primary error even when body.close fails;
    on an otherwise successful read, cleanup failure is a safe source error.
    """
    try:
        yield body
    except BaseException:
        try:
            body.close()
        except Exception:
            pass
        raise
    else:
        try:
            body.close()
        except Exception:
            raise DaySnapshotError("Remote source close failed") from None


def _download_body(body, path: Path, expected_size: int | None, max_bytes: int | None) -> None:
    """Bound streaming independently of ContentLength; sanitize transfer errors."""
    if expected_size is not None:
        if type(expected_size) is not int or expected_size < 0:
            raise DaySnapshotError("Invalid remote source size")
        if max_bytes is not None and expected_size > max_bytes:
            raise DaySnapshotError("Source object size limit exceeded")
    size = 0
    try:
        with path.open("wb") as output:
            while True:
                chunk_size = 1024 * 1024
                if max_bytes is not None:
                    chunk_size = min(chunk_size, max_bytes + 1 - size)
                chunk = body.read(chunk_size)
                if not chunk:
                    break
                size += len(chunk)
                if max_bytes is not None and size > max_bytes:
                    raise DaySnapshotError("Source object size limit exceeded")
                output.write(chunk)
    except DaySnapshotError:
        raise
    except Exception:
        raise DaySnapshotError("Remote source read failed") from None
    if expected_size is not None and size != expected_size:
        raise DaySnapshotError("Incomplete source object")


class DaySnapshotSource:
    """Obtain daily databases through an already-selected storage backend.

    The backend/client is borrowed. Each open owns only its SQLite connection,
    downloaded body and temporary directory. Byte policies are caller-specific:
    daily publication retains its bound; history need not change its old limits.
    """

    def __init__(self, backend):
        self.backend = backend

    @contextmanager
    def open_day(
        self, kind: str, day: str | date, *, max_bytes: int | None = None,
    ) -> Iterator[sqlite3.Connection | None]:
        key = _source_key(kind, day)
        if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 1):
            raise ValueError("Source size limit must be positive")
        if self.backend.backend_name == "local":
            path = local_day_path(self.backend.data_dir, kind, day)
            if path is None:
                yield None
                return
            with open_sqlite_snapshot(path, max_bytes=max_bytes) as conn:
                yield conn
            return
        if self.backend.backend_name != "remote":
            raise DaySnapshotError("Unsupported source backend")
        try:
            response = self.backend.s3_client.get_object(Bucket=self.backend.bucket_name, Key=key)
        except Exception as error:
            if object_is_missing(error):
                yield None
                return
            raise DaySnapshotError("Remote source read failed") from None
        if not isinstance(response, dict) or "Body" not in response:
            raise DaySnapshotError("Invalid remote source response")
        with _remote_body(response["Body"]) as body, tempfile.TemporaryDirectory(prefix="trendradar-source-") as directory:
            path = Path(directory) / "source.db"
            _download_body(body, path, response.get("ContentLength"), max_bytes)
            try:
                validate_sqlite_snapshot(path)
            except (OSError, ValueError, sqlite3.Error):
                raise DaySnapshotError("Invalid source database") from None
            with open_sqlite_snapshot(path, immutable=True) as conn:
                yield conn
