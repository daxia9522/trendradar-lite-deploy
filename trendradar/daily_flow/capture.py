"""Strict, bounded source capture for publication-relative report inputs.

SQLite reads use one read transaction per daily database. Remote reads use a
fresh, bounded object body, not the storage mixin's error-to-empty/cache paths.
The captured rows AND their identity boundary are frozen together before AI.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from trendradar.utils.url import normalize_url


MAX_HISTORY_DAYS = 32
MAX_DB_BYTES = 64 * 1024 * 1024
MAX_ROWS = 250000


class SourceCaptureError(RuntimeError):
    pass


def _bounded_rows(cursor):
    rows = cursor.fetchmany(MAX_ROWS + 1)
    if len(rows) > MAX_ROWS:
        raise SourceCaptureError("Source row limit exceeded")
    return rows


class DailySourceReader:
    """Read crawler databases without initializing or changing any source."""
    def __init__(self, backend):
        self.backend = backend

    def read_day(self, kind, date):
        if kind not in {"news", "rss"}:
            raise ValueError("Invalid source kind")
        if self.backend.backend_name == "local":
            path = Path(self.backend.data_dir) / kind / f"{date}.db"
            try:
                path.stat()
            except FileNotFoundError:
                return {"date": date, "present": False, "items": [], "latest_time": ""}
            return self._read_sqlite(path, kind, date)
        if self.backend.backend_name != "remote":
            raise SourceCaptureError("Unsupported publication source backend")
        # No use of cached downloaded databases: a fresh process must see the
        # same remote state as a long-lived worker.
        try:
            response = self.backend.s3_client.get_object(
                Bucket=self.backend.bucket_name, Key=f"{kind}/{date}.db")
        except Exception as exc:
            error = getattr(exc, "response", {}).get("Error", {})
            if str(error.get("Code")) in {"NoSuchKey", "NotFound", "404"}:
                return {"date": date, "present": False, "items": [], "latest_time": ""}
            raise SourceCaptureError("Remote source read failed") from None
        with closing(response["Body"]) as body, tempfile.TemporaryDirectory(prefix="trendradar-capture-") as directory:
            if response.get("ContentLength", 0) > MAX_DB_BYTES:
                raise SourceCaptureError("Source object size limit exceeded")
            path = Path(directory) / "capture.db"
            size = 0
            with path.open("wb") as output:
                while True:
                    chunk = body.read(min(1024 * 1024, MAX_DB_BYTES + 1 - size))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_DB_BYTES:
                        raise SourceCaptureError("Source object size limit exceeded")
                    output.write(chunk)
            if size < 100:
                raise SourceCaptureError("Invalid source database")
            return self._read_sqlite(path, kind, date)

    @staticmethod
    def _read_sqlite(path, kind, date):
        if path.stat().st_size > MAX_DB_BYTES:
            raise SourceCaptureError("Source database size limit exceeded")
        with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            if kind == "news":
                rows = _bounded_rows(conn.execute("""
                    SELECT n.*, p.name AS source_name FROM news_items n
                    LEFT JOIN platforms p ON p.id=n.platform_id
                    ORDER BY n.platform_id, n.last_crawl_time, n.id
                """))
                histories = {}
                for row in _bounded_rows(conn.execute("""
                    SELECT rh.news_item_id, rh.rank, rh.crawl_time FROM rank_history rh
                    JOIN news_items n ON n.id=rh.news_item_id
                    WHERE NOT (rh.rank=0 AND rh.crawl_time>n.last_crawl_time)
                    ORDER BY rh.news_item_id, rh.crawl_time, rh.id
                """)):
                    histories.setdefault(row["news_item_id"], []).append(dict(row))
                items = []
                for row in rows:
                    history = histories.get(row["id"], [])
                    ranks = list(dict.fromkeys(r["rank"] for r in history if r["rank"] != 0))
                    items.append({
                        "title": row["title"], "source_id": row["platform_id"],
                        "source_name": row["source_name"] or row["platform_id"],
                        "url": row["url"] or "", "mobileUrl": row["mobile_url"] or "",
                        "ranks": ranks or [row["rank"]], "rank": row["rank"],
                        "first_time": row["first_crawl_time"], "last_time": row["last_crawl_time"],
                        "count": row["crawl_count"], "rank_timeline": [{
                            "time": r["crawl_time"].split()[-1][:5], "rank": r["rank"] or None,
                        } for r in history],
                    })
                records_table = "crawl_records"
            else:
                rows = _bounded_rows(conn.execute("""
                    SELECT i.*, f.name AS feed_name FROM rss_items i
                    LEFT JOIN rss_feeds f ON f.id=i.feed_id
                    ORDER BY i.published_at DESC, i.id
                """))
                items = [{
                    "title": r["title"], "feed_id": r["feed_id"],
                    "feed_name": r["feed_name"] or r["feed_id"], "url": r["url"] or "",
                    "guid": r["guid"] or "", "published_at": r["published_at"] or "",
                    "summary": r["summary"] or "", "author": r["author"] or "",
                    "first_time": r["first_crawl_time"], "last_time": r["last_crawl_time"],
                    "count": r["crawl_count"],
                } for r in rows]
                records_table = "rss_crawl_records"
            latest = conn.execute(f"SELECT MAX(crawl_time) FROM {records_table}").fetchone()[0]
            return {"date": date, "present": True, "items": items, "latest_time": latest or ""}


def aliases(kind, item):
    """News preserves title and normalized-URL identity; RSS preserves GUID/URL."""
    source = item["source_id"] if kind == "news" else item["feed_id"]
    values = []
    if kind == "news":
        values.append(("title", item["title"]))
    elif item.get("guid"):
        values.append(("guid", item["guid"]))
    if item.get("url"):
        values.append(("url", normalize_url(item["url"], source if kind == "news" else "")))
    if not values:
        values.append(("title", item["title"]))
    return {hashlib.sha256(json.dumps([kind, source, key, value], ensure_ascii=False).encode()).hexdigest()
            for key, value in values}


def _first_seen(day, value, tz):
    if not value:
        raise SourceCaptureError("Missing source observation time")
    if len(value) > 10:
        result = datetime.fromisoformat(value)
        return result.replace(tzinfo=tz) if result.tzinfo is None else result
    normalized = value.replace("时", ":").replace("分", "").replace("-", ":")
    result = datetime.fromisoformat(f"{day}T{normalized}")
    return result.replace(tzinfo=tz)


@dataclass(frozen=True)
class FrozenCapture:
    """JSON is the immutable owner; consumers receive isolated copies."""
    serialized: str

    def to_dict(self):
        return json.loads(self.serialized)


def capture_sources(reader, baseline, now, *, platform_ids, feed_ids, rss_available=True, news_available=True,
                    identity_lookup=None):
    """Capture kinds independently; incomplete reads consume identities, not frontiers.

    A first-enabled kind adopts its own previous 24h, never the stream creation
    date. An established gap beyond the scan bound remains explicit: only today's
    current/daily pool is read, with the old frontier held until repair/re-adoption.
    """
    result = {
        "captured_at": now.isoformat(), "baseline_sequence": baseline["sequence"],
        "bootstrap": baseline["bootstrap"], "coverage": {}, "sources": {},
        "new_news": {}, "new_rss": [], "news_names": {}, "rss_names": {},
    }
    for kind, allowed in (("news", set(platform_ids)), ("rss", set(feed_ids))):
        if not allowed:
            result["sources"][kind] = []
            continue  # disabled kinds neither scan nor acquire/advance adoption
        previous = baseline.get("coverage", {}).get(kind) or {}
        adoption = previous.get("adoption") or {
            "strategy": "previous_24h", "start_at": (now - timedelta(hours=24)).isoformat(),
            "generation": 0,
        }
        bootstrap = datetime.fromisoformat(adoption["start_at"])
        start = datetime.fromisoformat(previous.get("through") or adoption["start_at"])
        if start.tzinfo is None and now.tzinfo is not None:
            start = start.replace(tzinfo=now.tzinfo)
        if bootstrap.tzinfo is None and now.tzinfo is not None:
            bootstrap = bootstrap.replace(tzinfo=now.tzinfo)
        first_day = (start - timedelta(days=1)).date() if previous.get("through") else start.date()
        days = (now.date() - first_day).days + 1
        attention = []
        if days < 1 or days > MAX_HISTORY_DAYS:
            attention.append({"reason": "history_gap", "start_at": start.isoformat(),
                              "end_at": now.isoformat()})
            first_day, days = now.date(), 1
            print(f"[发布] {kind} 历史缺口超出有界读取范围；仅捕获当日池，保留原边界，需修复或显式重新接入")
        available = news_available if kind == "news" else rss_available
        complete = not attention and available
        if not available:
            attention.append({"reason": "partial_collection"})
        sources = []
        seen = set()  # only this capture's observations, never cumulative history
        for index in range(days):
            date = (first_day + timedelta(days=index)).isoformat()
            try:
                source = reader.read_day(kind, date) if allowed else {
                    "date": date, "present": False, "items": [], "latest_time": ""}
                source["items"] = [item for item in source["items"]
                                   if item["source_id" if kind == "news" else "feed_id"] in allowed]
                # An absent database inside an already-published interval can
                # mean retention/loss, not a proven empty crawl. Bootstrap is
                # the sole explicit adoption policy that tolerates absent days.
                if previous.get("through") and not source.get("present") and date >= start.date().isoformat():
                    complete = False
                    attention.append({"reason": "missing_day", "date": date})
                # Validate all observation boundaries before accepting any day.
                for item in source["items"]:
                    _first_seen(date, item["first_time"], now.tzinfo)
                sources.append(source)
            except Exception as exc:
                complete = False
                attention.append({"reason": "unreadable_day", "date": date})
                sources.append({"date": date, "error": type(exc).__name__, "items": [], "latest_time": ""})
                print(f"[发布] {kind} {date} 输入不可读，保留原覆盖边界（{type(exc).__name__}）")
        observed = {key for source in sources for item in source["items"] for key in aliases(kind, item)}
        known_before_capture = set(previous.get("seen", [])) & observed  # v1 in-memory fixtures only
        if previous.get("seen_root"):
            if identity_lookup is None:
                raise SourceCaptureError("Publication identity lookup is required")
            known_before_capture.update(identity_lookup(previous["seen_root"], observed))
        # Items before initial adoption's explicit interval seed deduplication,
        # rather than becoming bootstrap novelty when repeated after midnight.
        if not previous.get("through"):
            for source in sources:
                for item in source["items"]:
                    if _first_seen(source["date"], item["first_time"], now.tzinfo) < bootstrap.replace(second=0, microsecond=0):
                        known_before_capture.update(aliases(kind, item))
        novel_aliases = set()
        for source in sources:
            for item in source["items"]:
                keys = aliases(kind, item)
                seen.update(keys)
                source_id = item["source_id"] if kind == "news" else item["feed_id"]
                result["news_names" if kind == "news" else "rss_names"][source_id] = item[
                    "source_name" if kind == "news" else "feed_name"]
                if keys & known_before_capture:
                    continue
                if kind == "news":
                    # A changed title for the same URL remains the same story;
                    # the latest captured spelling replaces its older spelling.
                    for title, existing in list(result["new_news"].get(source_id, {}).items()):
                        if aliases(kind, existing) & keys:
                            del result["new_news"][source_id][title]
                    result["new_news"].setdefault(source_id, {})[item["title"]] = item
                elif not keys & novel_aliases:
                    result["new_rss"].append(item)
                novel_aliases.update(keys)
        # Propagate aliases for known stories (e.g. changed title at same URL).
        seen.update(known_before_capture)
        result["sources"][kind] = sources
        result["coverage"][kind] = {
            "complete": complete, "through": now.isoformat(), "seen": sorted(seen),
            "adoption": adoption, "attention": attention,
            "boundary_sha256": hashlib.sha256(json.dumps(sources, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
        }
    return FrozenCapture(json.dumps(result, sort_keys=True, ensure_ascii=False))
