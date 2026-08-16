# coding=utf-8
"""历史日报 SQLite 读取（周报用）。

读日常爬虫写入的：
  output/news/YYYY-MM-DD.db
  output/rss/YYYY-MM-DD.db

不依赖 MCP，不做缓存服务。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple


class HistoryReader:
    """按日期读取本地历史热榜/RSS SQLite。"""

    def __init__(self, project_root: str | Path):
        self.project_root = Path(project_root)

    def _db_path(self, date: datetime, db_type: str = "news") -> Optional[Path]:
        path = self.project_root / "output" / db_type / f"{date.strftime('%Y-%m-%d')}.db"
        return path if path.exists() else None

    def read_all_titles_for_date(
        self,
        date: Optional[datetime] = None,
        platform_ids: Optional[List[str]] = None,
        db_type: str = "news",
    ) -> Tuple[Dict, Dict, Dict]:
        """
        读取指定日期数据。

        Returns:
            (all_titles, id_to_name, all_timestamps)

        Raises:
            FileNotFoundError: 对应日期 DB 不存在或为空
        """
        if date is None:
            date = datetime.now()
        result = self._read_from_sqlite(date, platform_ids, db_type)
        if not result:
            raise FileNotFoundError(
                f"未找到 {date.strftime('%Y-%m-%d')} 的 {db_type} 数据: "
                f"output/{db_type}/{date.strftime('%Y-%m-%d')}.db"
            )
        return result

    def _read_from_sqlite(
        self,
        date: datetime,
        platform_ids: Optional[List[str]],
        db_type: str,
    ) -> Optional[Tuple[Dict, Dict, Dict]]:
        db_path = self._db_path(date, db_type)
        if db_path is None:
            return None

        all_titles: Dict = {}
        id_to_name: Dict = {}
        all_timestamps: Dict = {}
        conn = None
        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            if db_type == "news":
                return self._read_news(cursor, platform_ids, all_titles, id_to_name, all_timestamps)
            if db_type == "rss":
                return self._read_rss(cursor, platform_ids, all_titles, id_to_name, all_timestamps)
            return None
        except Exception as e:
            print(f"Warning: 从 SQLite 读取失败 ({db_path.name}): {e}")
            return None
        finally:
            if conn is not None:
                conn.close()

    def _read_news(
        self,
        cursor,
        platform_ids: Optional[List[str]],
        all_titles: Dict,
        id_to_name: Dict,
        all_timestamps: Dict,
    ) -> Optional[Tuple[Dict, Dict, Dict]]:
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='news_items'"
        )
        if not cursor.fetchone():
            return None

        if platform_ids:
            placeholders = ",".join("?" * len(platform_ids))
            cursor.execute(
                f"""
                SELECT n.id, n.platform_id, p.name as platform_name, n.title,
                       n.rank, n.url, n.mobile_url,
                       n.first_crawl_time, n.last_crawl_time, n.crawl_count
                FROM news_items n
                LEFT JOIN platforms p ON n.platform_id = p.id
                WHERE n.platform_id IN ({placeholders})
                """,
                platform_ids,
            )
        else:
            cursor.execute(
                """
                SELECT n.id, n.platform_id, p.name as platform_name, n.title,
                       n.rank, n.url, n.mobile_url,
                       n.first_crawl_time, n.last_crawl_time, n.crawl_count
                FROM news_items n
                LEFT JOIN platforms p ON n.platform_id = p.id
                """
            )

        rows = cursor.fetchall()
        news_ids = [row["id"] for row in rows]
        rank_history_map: Dict[str, List[int]] = {}
        if news_ids:
            placeholders = ",".join("?" * len(news_ids))
            cursor.execute(
                f"""
                SELECT news_item_id, rank FROM rank_history
                WHERE news_item_id IN ({placeholders})
                ORDER BY news_item_id, crawl_time
                """,
                news_ids,
            )
            for rh_row in cursor.fetchall():
                news_id = rh_row["news_item_id"]
                rank_history_map.setdefault(news_id, []).append(rh_row["rank"])

        for row in rows:
            platform_id = row["platform_id"]
            platform_name = row["platform_name"] or platform_id
            title = row["title"]
            id_to_name.setdefault(platform_id, platform_name)
            all_titles.setdefault(platform_id, {})
            ranks = rank_history_map.get(row["id"], [row["rank"]])
            all_titles[platform_id][title] = {
                "ranks": ranks,
                "url": row["url"] or "",
                "mobileUrl": row["mobile_url"] or "",
                "first_time": row["first_crawl_time"] or "",
                "last_time": row["last_crawl_time"] or "",
                "count": row["crawl_count"] or 1,
            }

        cursor.execute(
            "SELECT crawl_time, created_at FROM crawl_records ORDER BY crawl_time"
        )
        for row in cursor.fetchall():
            try:
                ts = datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S").timestamp()
            except (ValueError, TypeError):
                ts = datetime.now().timestamp()
            all_timestamps[f"{row['crawl_time']}.db"] = ts

        if not all_titles:
            return None
        return all_titles, id_to_name, all_timestamps

    def _read_rss(
        self,
        cursor,
        feed_ids: Optional[List[str]],
        all_items: Dict,
        id_to_name: Dict,
        all_timestamps: Dict,
    ) -> Optional[Tuple[Dict, Dict, Dict]]:
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='rss_items'"
        )
        if not cursor.fetchone():
            return None

        if feed_ids:
            placeholders = ",".join("?" * len(feed_ids))
            cursor.execute(
                f"""
                SELECT i.id, i.feed_id, f.name as feed_name, i.title,
                       i.url, i.published_at, i.summary, i.author,
                       i.first_crawl_time, i.last_crawl_time, i.crawl_count
                FROM rss_items i
                LEFT JOIN rss_feeds f ON i.feed_id = f.id
                WHERE i.feed_id IN ({placeholders})
                ORDER BY i.published_at DESC
                """,
                feed_ids,
            )
        else:
            cursor.execute(
                """
                SELECT i.id, i.feed_id, f.name as feed_name, i.title,
                       i.url, i.published_at, i.summary, i.author,
                       i.first_crawl_time, i.last_crawl_time, i.crawl_count
                FROM rss_items i
                LEFT JOIN rss_feeds f ON i.feed_id = f.id
                ORDER BY i.published_at DESC
                """
            )

        for row in cursor.fetchall():
            feed_id = row["feed_id"]
            feed_name = row["feed_name"] or feed_id
            title = row["title"]
            id_to_name.setdefault(feed_id, feed_name)
            all_items.setdefault(feed_id, {})
            all_items[feed_id][title] = {
                "url": row["url"] or "",
                "published_at": row["published_at"] or "",
                "summary": row["summary"] or "",
                "author": row["author"] or "",
                "first_time": row["first_crawl_time"] or "",
                "last_time": row["last_crawl_time"] or "",
                "count": row["crawl_count"] or 1,
            }

        cursor.execute(
            "SELECT crawl_time, created_at FROM rss_crawl_records ORDER BY crawl_time"
        )
        for row in cursor.fetchall():
            try:
                ts = datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S").timestamp()
            except (ValueError, TypeError):
                ts = datetime.now().timestamp()
            all_timestamps[f"{row['crawl_time']}.db"] = ts

        if not all_items:
            return None
        return all_items, id_to_name, all_timestamps
