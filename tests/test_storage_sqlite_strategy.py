"""Real SQLite locking, snapshot, and storage-connection regressions."""

import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from datetime import datetime
from pathlib import Path

from trendradar.storage.history_reader import HistoryReader
from trendradar.storage.local import LocalStorageBackend
from trendradar.storage.sqlite_mixin import (
    SQLITE_BUSY_TIMEOUT_MS,
    connect_sqlite,
)


class SQLiteStorageStrategyTests(unittest.TestCase):
    def test_connection_initialization_failure_closes_handle(self):
        connection = Mock()
        connection.execute.side_effect = sqlite3.OperationalError("synthetic pragma failure")
        with patch("trendradar.storage.sqlite_mixin.sqlite3.connect", return_value=connection):
            with self.assertRaises(sqlite3.OperationalError):
                connect_sqlite("synthetic.db", wal=True)
        connection.close.assert_called_once_with()

    def test_schema_failure_rolls_back_rss_migration_and_partial_ddl(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = connect_sqlite(Path(directory) / "rss.db", wal=True)
            self.addCleanup(connection.close)
            connection.execute("CREATE TABLE rss_items (feed_id TEXT)")
            connection.execute("INSERT INTO rss_items VALUES ('preserved')")
            connection.commit()
            schema = Path(directory) / "bad.sql"
            schema.write_text("CREATE TABLE first (value TEXT);\nINVALID SQL;\n")
            backend = LocalStorageBackend(directory, enable_txt=False, enable_html=False, timezone="UTC")
            with patch.object(backend, "_get_schema_path", return_value=schema):
                with self.assertRaises(sqlite3.OperationalError):
                    backend._init_tables(connection, "rss")
            self.assertFalse(connection.in_transaction)
            self.assertEqual([row[1] for row in connection.execute("PRAGMA table_info(rss_items)")], ["feed_id"])
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='first'").fetchone())
            self.assertEqual(connection.execute("SELECT feed_id FROM rss_items").fetchone()[0], "preserved")

    def test_schema_statements_use_sqlite_parsing_for_quoted_semicolons_and_triggers(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = connect_sqlite(Path(directory) / "schema.db")
            self.addCleanup(connection.close)
            schema = Path(directory) / "schema.sql"
            schema.write_text("CREATE TABLE values_table (value TEXT); CREATE TABLE audit (value TEXT);\n"
                "CREATE TRIGGER inserted AFTER INSERT ON values_table BEGIN\n"
                " INSERT INTO audit VALUES ('one;two'); INSERT INTO audit VALUES (NEW.value); END;\n-- tail comment")
            backend = LocalStorageBackend(directory, enable_txt=False, enable_html=False, timezone="UTC")
            with patch.object(backend, "_get_schema_path", return_value=schema):
                backend._init_tables(connection)
            connection.execute("INSERT INTO values_table VALUES ('value')")
            self.assertEqual([row[0] for row in connection.execute("SELECT value FROM audit")], ["one;two", "value"])

    def test_local_connection_uses_wal_and_bounded_busy_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "local.db"
            connection = connect_sqlite(path, wal=True)
            try:
                self.assertEqual(
                    connection.execute("PRAGMA journal_mode").fetchone()[0].lower(),
                    "wal",
                )
                self.assertEqual(
                    connection.execute("PRAGMA busy_timeout").fetchone()[0],
                    SQLITE_BUSY_TIMEOUT_MS,
                )
            finally:
                connection.close()

    def test_two_writers_wait_for_real_lock_then_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "concurrent.db"
            writer = connect_sqlite(path, wal=True)
            writer.execute("CREATE TABLE values_table (value TEXT NOT NULL)")
            writer.commit()
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("INSERT INTO values_table VALUES ('first')")

            started = threading.Event()
            finished = threading.Event()
            result = {}

            def second_writer():
                connection = connect_sqlite(path, wal=True)
                try:
                    started.set()
                    begin = time.monotonic()
                    connection.execute("INSERT INTO values_table VALUES ('second')")
                    connection.commit()
                    result["waited"] = time.monotonic() - begin
                finally:
                    connection.close()
                    finished.set()

            thread = threading.Thread(target=second_writer)
            thread.start()
            self.assertTrue(started.wait(2))
            # Give the second connection a chance to reach SQLite's lock
            # wait, then release the first writer.
            time.sleep(0.15)
            self.assertFalse(finished.is_set())
            writer.commit()
            self.assertTrue(finished.wait(5))
            thread.join(5)
            writer.close()

            self.assertGreaterEqual(result["waited"], 0.10)
            reader = connect_sqlite(path, readonly=True)
            try:
                self.assertEqual(
                    [row[0] for row in reader.execute(
                        "SELECT value FROM values_table ORDER BY rowid"
                    )],
                    ["first", "second"],
                )
            finally:
                reader.close()

    def test_wal_reader_keeps_snapshot_until_transaction_ends(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.db"
            writer = connect_sqlite(path, wal=True)
            writer.execute("CREATE TABLE values_table (value TEXT NOT NULL)")
            writer.execute("INSERT INTO values_table VALUES ('before')")
            writer.commit()

            reader = connect_sqlite(path, readonly=True)
            try:
                reader.execute("BEGIN")
                self.assertEqual(
                    reader.execute("SELECT COUNT(*) FROM values_table").fetchone()[0],
                    1,
                )
                writer.execute("INSERT INTO values_table VALUES ('after')")
                writer.commit()
                self.assertEqual(
                    reader.execute("SELECT COUNT(*) FROM values_table").fetchone()[0],
                    1,
                )
                reader.commit()
                self.assertEqual(
                    reader.execute("SELECT COUNT(*) FROM values_table").fetchone()[0],
                    2,
                )
            finally:
                reader.close()
                writer.close()

    def test_remote_style_connection_is_rollback_journal_without_wal_sidecar(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "remote.db"
            connection = connect_sqlite(path, wal=False)
            try:
                self.assertEqual(
                    connection.execute("PRAGMA journal_mode").fetchone()[0].lower(),
                    "delete",
                )
                connection.execute("CREATE TABLE values_table (value TEXT)")
                connection.execute("INSERT INTO values_table VALUES ('committed')")
                connection.commit()
            finally:
                connection.close()

            self.assertFalse(Path(f"{path}-wal").exists())
            reread = sqlite3.connect(path)
            try:
                self.assertEqual(
                    reread.execute("SELECT value FROM values_table").fetchone()[0],
                    "committed",
                )
            finally:
                reread.close()

    def test_history_reader_connection_is_read_only_and_does_not_initialize_db(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "output" / "news" / "2026-10-01.db"
            database.parent.mkdir(parents=True)
            writer = connect_sqlite(database, wal=False)
            try:
                writer.execute("CREATE TABLE marker (value TEXT)")
                writer.execute("INSERT INTO marker VALUES ('safe')")
                writer.commit()
            finally:
                writer.close()

            readonly = connect_sqlite(database, readonly=True)
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    readonly.execute("INSERT INTO marker VALUES ('blocked')")
            finally:
                readonly.close()

            with self.assertRaises(FileNotFoundError):
                HistoryReader(root).read_all_titles_for_date(
                    datetime(2026, 10, 2)
                )
            self.assertFalse((root / "output" / "news" / "2026-10-02.db").exists())


class SQLiteNewsAssemblerTests(unittest.TestCase):
    def test_aggregate_and_latest_reads_share_news_item_mapping(self):
        from trendradar.storage.base import NewsData, NewsItem

        with tempfile.TemporaryDirectory() as directory:
            backend = LocalStorageBackend(directory, enable_txt=False, enable_html=False, timezone="UTC")
            try:
                self.assertTrue(backend.save_news_data(NewsData(
                    "2026-10-01", "09:00",
                    {"source": [NewsItem("title", "source", rank=2,
                                           url="https://example.invalid/a",
                                           crawl_time="09:00")]},
                    {"source": "Source"},
                )))
                self.assertTrue(backend.save_news_data(NewsData(
                    "2026-10-01", "10:00",
                    {"source": [NewsItem("title", "source", rank=1,
                                           url="https://example.invalid/a",
                                           crawl_time="10:00")]},
                    {"source": "Source"},
                )))
                aggregate = backend.get_today_all_data("2026-10-01")
                latest = backend.get_latest_crawl_data("2026-10-01")
                self.assertIsNotNone(aggregate)
                self.assertIsNotNone(latest)
                aggregate_item = aggregate.items["source"][0]
                latest_item = latest.items["source"][0]
                for field in ("title", "source_id", "source_name", "url", "mobile_url",
                              "first_time", "last_time", "count"):
                    self.assertEqual(getattr(aggregate_item, field), getattr(latest_item, field))
                self.assertEqual(latest_item.rank, 1)
            finally:
                backend.cleanup()


if __name__ == "__main__":
    unittest.main()
