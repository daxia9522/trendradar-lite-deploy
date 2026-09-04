# coding=utf-8
"""RSS GUID/URL 双唯一约束冲突回归测试。"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from trendradar.storage.base import RSSData, RSSItem
from trendradar.storage.local import LocalStorageBackend


def _item(title, feed_id="feed1", url="", guid=""):
    return RSSItem(
        title=title,
        feed_id=feed_id,
        feed_name=feed_id,
        url=url,
        guid=guid,
        published_at="2026-08-28T00:00:00+00:00",
        crawl_time="09:00",
    )


def _save(backend, items, crawl_time="09:00"):
    data = RSSData(
        date="2026-08-28",
        crawl_time=crawl_time,
        items={"feed1": items},
        id_to_name={"feed1": "feed1"},
    )
    return backend.save_rss_data(data)


def _rows(backend):
    db_path = Path(backend.data_dir) / "rss" / "2026-08-28.db"
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT title, feed_id, url, guid, crawl_count, first_crawl_time, last_crawl_time "
            "FROM rss_items ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def _status(backend, crawl_time, feed_id="feed1"):
    db_path = Path(backend.data_dir) / "rss" / "2026-08-28.db"
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            """
            SELECT cs.status, cs.error_message
            FROM rss_crawl_status cs
            JOIN rss_crawl_records cr ON cs.crawl_record_id = cr.id
            WHERE cr.crawl_time = ? AND cs.feed_id = ?
            """,
            (crawl_time, feed_id),
        ).fetchone()
    finally:
        conn.close()


class RssGuidUrlConflictTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.backend = LocalStorageBackend(data_dir=self.tmp.name, timezone="UTC")

    def tearDown(self):
        self.backend.cleanup()
        self.tmp.cleanup()

    def test_guid_change_updates_existing_url_row(self):
        self.assertTrue(_save(self.backend, [
            _item("标题A", url="https://example.com/a", guid="guid-old"),
        ]))
        self.assertTrue(_save(self.backend, [
            _item("标题A", url="https://example.com/a", guid="guid-new"),
        ]))

        rows = _rows(self.backend)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][3], "guid-new")
        self.assertEqual(rows[0][2], "https://example.com/a")
        self.assertEqual(rows[0][4], 2)

    def test_url_change_allows_new_url(self):
        self.assertTrue(_save(self.backend, [
            _item("标题A", url="https://example.com/a", guid="guid-a"),
        ]))
        self.assertTrue(_save(self.backend, [
            _item("标题A", url="https://example.com/b", guid="guid-a"),
        ]))

        rows = _rows(self.backend)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][3], "guid-a")
        self.assertEqual(rows[0][2], "https://example.com/b")
        self.assertEqual(rows[0][4], 2)

    def test_guid_url_conflict_merges_to_guid_row(self):
        self.assertTrue(_save(self.backend, [
            _item("A", url="https://example.com/a", guid="g1"),
        ]))
        self.assertTrue(_save(self.backend, [
            _item("B", url="https://example.com/b", guid="g2"),
        ]))
        self.assertTrue(_save(self.backend, [
            _item("A", url="https://example.com/b", guid="g1"),
        ]))

        rows = _rows(self.backend)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][3], "g1")
        self.assertEqual(rows[0][2], "https://example.com/b")
        self.assertEqual(rows[0][4], 3)

    def test_item_level_failure_is_recorded_as_failed_status(self):
        self.assertTrue(_save(self.backend, [
            _item("缺URL和GUID", url="", guid=""),
        ]))

        rows = _rows(self.backend)
        self.assertEqual(len(rows), 0)
        status = _status(self.backend, "09:00")
        self.assertIsNotNone(status)
        self.assertEqual(status[0], "failed")
        self.assertIn("1 条 RSS 条目保存失败", status[1])
        self.assertIn("缺少 URL 和 GUID", status[1])


if __name__ == "__main__":
    unittest.main()
