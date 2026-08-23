import io
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

from trendradar.storage.local import LocalStorageBackend
from trendradar.storage.remote import RemoteStorageBackend


class StorageBackendTests(unittest.TestCase):
    def test_local_id_queries_return_news_and_rss_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            backend = LocalStorageBackend(data_dir=temp_dir, enable_txt=False, enable_html=False)
            news = backend._get_connection("2026-08-23", "news")
            news.execute("INSERT INTO platforms (id, name) VALUES (?, ?)", ("source", "Source"))
            news.execute(
                "INSERT INTO news_items (title, platform_id, rank, url, first_crawl_time, last_crawl_time) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "News title",
                    "source",
                    1,
                    "https://example.com/news",
                    "2026-08-23 00:00:00",
                    "2026-08-23 00:00:00",
                ),
            )
            news.commit()

            rss = backend._get_connection("2026-08-23", "rss")
            rss.execute(
                "INSERT INTO rss_feeds (id, name) VALUES (?, ?)",
                ("feed", "Feed"),
            )
            rss.execute(
                "INSERT INTO rss_items (title, url, feed_id, first_crawl_time, last_crawl_time) VALUES (?, ?, ?, ?, ?)",
                (
                    "RSS title",
                    "https://example.com/rss",
                    "feed",
                    "2026-08-23 00:00:00",
                    "2026-08-23 00:00:00",
                ),
            )
            rss.commit()

            self.assertEqual(backend.get_all_news_ids("2026-08-23")[0]["title"], "News title")
            self.assertEqual(backend.get_all_rss_ids("2026-08-23")[0]["title"], "RSS title")

    def test_local_missing_date_returns_empty_without_database_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            backend = LocalStorageBackend(data_dir=temp_dir, enable_txt=False, enable_html=False)

            self.assertEqual(backend.get_all_news_ids("2026-08-22"), [])
            self.assertEqual(backend.get_all_rss_ids("2026-08-22"), [])
            self.assertFalse((Path(temp_dir) / "news" / "2026-08-22.db").exists())
            self.assertFalse((Path(temp_dir) / "rss" / "2026-08-22.db").exists())

    def test_remote_pull_downloads_news_and_rss_to_current_layout(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            backend = object.__new__(RemoteStorageBackend)
            backend.bucket_name = "bucket"
            backend.timezone = "Asia/Shanghai"
            backend.s3_client = Mock()
            backend._get_configured_time = Mock(return_value=datetime(2026, 8, 23, 12, 0, 0))
            backend._check_object_exists = Mock(return_value=True)

            def get_object(Bucket, Key):
                body = Mock()
                body.iter_chunks.return_value = iter([Key.encode("utf-8")])
                return {"Body": body}

            backend.s3_client.get_object.side_effect = get_object

            pulled = backend.pull_recent_days(1, temp_dir)

            self.assertEqual(pulled, 2)
            self.assertEqual(
                (Path(temp_dir) / "news" / "2026-08-23.db").read_bytes(),
                b"news/2026-08-23.db",
            )
            self.assertEqual(
                (Path(temp_dir) / "rss" / "2026-08-23.db").read_bytes(),
                b"rss/2026-08-23.db",
            )
            self.assertFalse(list(Path(temp_dir).rglob("*.part")))

    def test_remote_date_listing_merges_news_and_rss_dates(self):
        backend = object.__new__(RemoteStorageBackend)
        backend.bucket_name = "bucket"
        paginator = Mock()
        paginator.paginate.side_effect = [
            [{"Contents": [{"Key": "news/2026-08-23.db"}]}],
            [{"Contents": [{"Key": "rss/2026-08-22.db"}, {"Key": "rss/invalid.db"}]}],
        ]
        backend.s3_client = Mock()
        backend.s3_client.get_paginator.return_value = paginator

        self.assertEqual(backend.list_remote_dates(), ["2026-08-23", "2026-08-22"])
        self.assertEqual(paginator.paginate.call_count, 2)

    def test_remote_cleanup_deletes_expired_news_and_rss_files(self):
        backend = object.__new__(RemoteStorageBackend)
        backend.bucket_name = "bucket"
        backend._get_configured_time = Mock(return_value=datetime(2026, 8, 23, 12, 0, 0))
        paginator = Mock()
        paginator.paginate.side_effect = [
            [{"Contents": [{"Key": "news/2026-08-01.db"}, {"Key": "news/2026-08-22.db"}]}],
            [{"Contents": [{"Key": "rss/2026-08-01.db"}]}],
        ]
        backend.s3_client = Mock()
        backend.s3_client.get_paginator.return_value = paginator
        backend.s3_client.delete_objects.return_value = {
            "Deleted": [{"Key": "news/2026-08-01.db"}, {"Key": "rss/2026-08-01.db"}]
        }

        self.assertEqual(backend.cleanup_old_data(7), 2)
        backend.s3_client.delete_objects.assert_called_once_with(
            Bucket="bucket",
            Delete={
                "Objects": [
                    {"Key": "news/2026-08-01.db"},
                    {"Key": "rss/2026-08-01.db"},
                ]
            },
        )


if __name__ == "__main__":
    unittest.main()
