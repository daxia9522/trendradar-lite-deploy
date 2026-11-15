"""Daily source reads and novelty since the local publication cutoff."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from trendradar.storage.day_snapshot import DaySnapshotError, DaySnapshotSource
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
    """Project report rows from the shared, read-only daily source."""
    def __init__(self, backend=None, *, source=None):
        if source is None:
            if backend is None:
                raise ValueError("A source backend is required")
            source = DaySnapshotSource(backend)
        self.source = source
        self.backend = source.backend

    def read_day(self, kind, date):
        try:
            with self.source.open_day(kind, date, max_bytes=MAX_DB_BYTES) as conn:
                if conn is None:
                    return {"date": date, "present": False, "items": [], "latest_time": ""}
                return self._read_connection(conn, kind, date)
        except DaySnapshotError as error:
            raise SourceCaptureError(str(error)) from None

    @staticmethod
    def _read_connection(conn: sqlite3.Connection, kind, date):
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
    return {(kind, source, key, value) for key, value in values}


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
    """In-memory report input; each consumer receives its own copy."""
    serialized: str

    def to_dict(self):
        return json.loads(self.serialized)


def capture_sources(reader, beacon, now, *, platform_ids, feed_ids,
                    rss_available=True, news_available=True):
    result = {
        "captured_at": now.isoformat(), "coverage": {}, "sources": {},
        "new_news": {}, "new_rss": [], "news_names": {}, "rss_names": {},
    }
    for kind, allowed in (("news", set(platform_ids)), ("rss", set(feed_ids))):
        if not allowed:
            result["sources"][kind] = []
            continue
        cutoff = beacon.get(kind)
        start = datetime.fromisoformat(cutoff) if cutoff else now - timedelta(hours=24)
        if start.tzinfo is None:
            start = start.replace(tzinfo=now.tzinfo)
        first_day = (start - timedelta(days=1)).date() if cutoff else start.date()
        if (now.date() - first_day).days >= MAX_HISTORY_DAYS:
            first_day = now.date() - timedelta(days=MAX_HISTORY_DAYS - 1)
            print(f"[新增] {kind} 只读取最近 {MAX_HISTORY_DAYS} 天")
        complete = news_available if kind == "news" else rss_available
        sources = []
        for index in range(max(1, (now.date() - first_day).days + 1)):
            date = (first_day + timedelta(days=index)).isoformat()
            try:
                source = reader.read_day(kind, date)
                source["items"] = [item for item in source["items"]
                                   if item["source_id" if kind == "news" else "feed_id"] in allowed]
                for item in source["items"]:
                    _first_seen(date, item["first_time"], now.tzinfo)
                if date == now.date().isoformat() and not source.get("present"):
                    complete = False
                sources.append(source)
            except Exception as error:
                complete = False
                sources.append({"date": date, "items": [], "latest_time": ""})
                print(f"[新增] {kind} {date} 不可读，不更新该信标（{type(error).__name__}）")
        old_keys = {key for source in sources for item in source["items"]
                    if _first_seen(source["date"], item["first_time"], now.tzinfo) <= start
                    for key in aliases(kind, item)}
        new_keys = set()
        for source in sources:
            for item in source["items"]:
                source_id = item["source_id"] if kind == "news" else item["feed_id"]
                result["news_names" if kind == "news" else "rss_names"][source_id] = item[
                    "source_name" if kind == "news" else "feed_name"]
                observed = _first_seen(source["date"], item["first_time"], now.tzinfo)
                keys = aliases(kind, item)
                if not start < observed <= now or keys & old_keys:
                    continue
                if kind == "news":
                    for title, existing in list(result["new_news"].get(source_id, {}).items()):
                        if aliases(kind, existing) & keys:
                            del result["new_news"][source_id][title]
                    result["new_news"].setdefault(source_id, {})[item["title"]] = {
                        **item, "first_seen_at": observed.isoformat(),
                    }
                elif not keys & new_keys:
                    result["new_rss"].append(item)
                new_keys.update(keys)
        result["sources"][kind] = sources
        result["coverage"][kind] = {"complete": complete, "through": now.isoformat()}
    return FrozenCapture(json.dumps(result, ensure_ascii=False))
