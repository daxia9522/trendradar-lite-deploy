"""Temporary SQLite/fake-reader regressions; no live data, AI, mail or S3."""
import errno
import io
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from trendradar.storage import history_reader
from trendradar.storage.history_reader import HistoryReader
from weekly_report import collection
from weekly_report import weekly_ai_report_email as weekly


DAY = datetime(2026, 10, 1)
STORAGE = Path(history_reader.__file__).parent
SECRET = "synthetic-password-must-not-appear"
PRIVATE_ERROR = f"https://user:{SECRET}@example.invalid/db?token={SECRET}"


def read_errors():
    return (sqlite3.DatabaseError(PRIVATE_ERROR), sqlite3.OperationalError(PRIVATE_ERROR),
            PermissionError(errno.EACCES, PRIVATE_ERROR), RuntimeError(PRIVATE_ERROR))


class TemporaryHistoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.reader = HistoryReader(self.root)

    def db_path(self, db_type, date=DAY):
        path = self.root / "output" / db_type / f"{date:%Y-%m-%d}.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def seed(self, db_type, *, populated=True, date=DAY):
        path = self.db_path(db_type, date)
        connection = sqlite3.connect(path)
        try:
            schema = "schema.sql" if db_type == "news" else "rss_schema.sql"
            connection.executescript((STORAGE / schema).read_text(encoding="utf-8"))
            if populated:
                if db_type == "news":
                    connection.execute("INSERT INTO platforms(id, name) VALUES ('source', 'Source')")
                    connection.execute(
                        "INSERT INTO news_items(title, platform_id, rank, url, "
                        "first_crawl_time, last_crawl_time) VALUES "
                        "('日本发生地震', 'source', 2, 'https://example.invalid/news', '09:00', '10:00')"
                    )
                    connection.execute(
                        "INSERT INTO rank_history(news_item_id, rank, crawl_time) VALUES (1, 1, '10:00')"
                    )
                    records = "crawl_records"
                else:
                    connection.execute("INSERT INTO rss_feeds(id, name) VALUES ('source', 'Source')")
                    connection.execute(
                        "INSERT INTO rss_items(title, feed_id, url, published_at, summary, "
                        "author, first_crawl_time, last_crawl_time) VALUES "
                        "('火箭发射成功', 'source', 'https://example.invalid/rss', "
                        "'2026-10-01T09:00:00Z', 'summary', 'author', '09:00', '10:00')"
                    )
                    records = "rss_crawl_records"
                connection.execute(
                    f"INSERT INTO {records}(crawl_time, created_at) VALUES ('10:00', '2026-10-01 10:00:00')"
                )
            connection.commit()
        finally:
            connection.close()
        return path

    def read_day(self, db_type, **kwargs):
        return self.reader.read_all_titles_for_date(DAY, db_type=db_type, **kwargs)

    def ingest(self, reader, db_type, stats):
        collection._ingest_day(reader, DAY, db_type, {}, {}, stats)


class HistoryReadClassificationTests(TemporaryHistoryTests):
    def test_missing_file_is_not_created_and_remains_missing(self):
        for db_type in ("news", "rss"):
            with self.subTest(db_type=db_type):
                path = self.db_path(db_type)
                with self.assertRaises(FileNotFoundError):
                    self.read_day(db_type)
                self.assertFalse(path.exists())

    def test_defined_empty_databases_remain_missing_and_unmodified(self):
        # Existing semantics: zero-byte DB, no relevant items table, complete
        # schema with no rows. Do not initialize or repair these while reading.
        for db_type in ("news", "rss"):
            path = self.db_path(db_type)
            for kind in ("zero_bytes", "no_items_table", "empty_schema"):
                with self.subTest(db_type=db_type, kind=kind):
                    if kind == "zero_bytes":
                        path.touch()
                    elif kind == "no_items_table":
                        connection = sqlite3.connect(path)
                        connection.execute("CREATE TABLE unrelated(value TEXT)")
                        connection.close()
                    else:
                        self.seed(db_type, populated=False)
                    before = path.read_bytes()
                    stats = {}
                    with self.assertRaises(FileNotFoundError):
                        self.read_day(db_type)
                    self.ingest(self.reader, db_type, stats)
                    self.assertEqual(stats, {f"missing_{db_type}_days": 1})
                    self.assertEqual(path.read_bytes(), before)

    def test_valid_rows_and_filtered_empty_result_keep_contract(self):
        for db_type in ("news", "rss"):
            with self.subTest(db_type=db_type):
                path = self.seed(db_type)
                before = path.read_bytes()
                titles, names, timestamps = self.read_day(db_type, platform_ids=["source"])
                self.assertEqual(names, {"source": "Source"})
                self.assertEqual(len(titles["source"]), 1)
                self.assertEqual(set(timestamps), {"10:00.db"})
                meta = next(iter(titles["source"].values()))
                self.assertEqual(meta["first_time"], "09:00")
                self.assertEqual(meta["last_time"], "10:00")
                self.assertEqual(meta["count"], 1)
                if db_type == "news":
                    self.assertEqual(meta["ranks"], [1])
                else:
                    self.assertEqual(meta["summary"], "summary")
                with self.assertRaises(FileNotFoundError):
                    self.read_day(db_type, platform_ids=["absent"])
                self.assertEqual(path.read_bytes(), before)

    def test_real_corrupt_database_is_not_missing(self):
        for db_type in ("news", "rss"):
            with self.subTest(db_type=db_type):
                path = self.db_path(db_type)
                content = b"synthetic corrupt database, not SQLite"
                path.write_bytes(content)
                with self.assertRaises(sqlite3.DatabaseError) as caught:
                    self.read_day(db_type)
                self.assertEqual(caught.exception.sqlite_errorcode, sqlite3.SQLITE_NOTADB)
                self.assertEqual(path.read_bytes(), content)

    def test_incomplete_schema_is_a_read_error_not_an_empty_day(self):
        for db_type in ("news", "rss"):
            tables = (("platforms", "rank_history", "crawl_records") if db_type == "news"
                      else ("rss_feeds", "rss_crawl_records"))
            for index, table in enumerate(tables):
                with self.subTest(db_type=db_type, table=table):
                    date = DAY + timedelta(days=index)
                    path = self.seed(db_type, date=date)
                    connection = sqlite3.connect(path)
                    connection.execute(f"DROP TABLE {table}")
                    connection.close()
                    with self.assertRaises(sqlite3.OperationalError):
                        self.reader.read_all_titles_for_date(date, db_type=db_type)

    def test_truncated_real_database_retains_corrupt_error_code(self):
        for db_type in ("news", "rss"):
            with self.subTest(db_type=db_type):
                path = self.seed(db_type)
                # Keep the SQLite header, truncate the actual schema/data pages.
                path.write_bytes(path.read_bytes()[:100])
                with self.assertRaises(sqlite3.DatabaseError) as caught:
                    self.read_day(db_type)
                self.assertEqual(caught.exception.sqlite_errorcode, sqlite3.SQLITE_CORRUPT)

    def test_disappearing_file_after_stat_does_not_get_created_or_hidden(self):
        path = self.seed("news")
        path.unlink()  # Simulate removal between the existence check and open.
        with patch.object(self.reader, "_db_path", return_value=path):
            with self.assertRaises(sqlite3.OperationalError) as caught:
                self.read_day("news")
        self.assertEqual(caught.exception.sqlite_errorcode, sqlite3.SQLITE_CANTOPEN)
        self.assertFalse(path.exists())

    def test_real_exclusive_lock_remains_busy_and_recovers_after_release(self):
        for db_type in ("news", "rss"):
            with self.subTest(db_type=db_type):
                path = self.seed(db_type)
                writer = sqlite3.connect(path)
                try:
                    writer.execute("BEGIN EXCLUSIVE")
                    # Fixture timeout only; production remains 30 seconds.
                    with patch("trendradar.storage.sqlite_mixin.SQLITE_BUSY_TIMEOUT_MS", 25):
                        with self.assertRaises(sqlite3.OperationalError) as caught:
                            self.read_day(db_type)
                    self.assertEqual(caught.exception.sqlite_errorcode, sqlite3.SQLITE_BUSY)
                finally:
                    writer.rollback()
                    writer.close()
                self.assertTrue(self.read_day(db_type)[0])

    @unittest.skipIf(not hasattr(os, "geteuid") or os.geteuid() == 0, "requires non-root POSIX permissions")
    def test_real_unreadable_file_is_not_missing(self):
        for db_type in ("news", "rss"):
            with self.subTest(db_type=db_type):
                path = self.seed(db_type)
                mode = path.stat().st_mode
                path.chmod(0)
                try:
                    with self.assertRaises(sqlite3.OperationalError) as caught:
                        self.read_day(db_type)
                    self.assertEqual(caught.exception.sqlite_errorcode, sqlite3.SQLITE_CANTOPEN)
                finally:
                    path.chmod(mode)

    @unittest.skipIf(not hasattr(os, "geteuid") or os.geteuid() == 0, "requires non-root POSIX permissions")
    def test_real_inaccessible_parent_is_a_permission_error(self):
        for db_type in ("news", "rss"):
            with self.subTest(db_type=db_type):
                path = self.seed(db_type)
                mode = path.parent.stat().st_mode
                path.parent.chmod(0)
                try:
                    with self.assertRaises(PermissionError):
                        self.read_day(db_type)
                finally:
                    path.parent.chmod(mode)

    def test_stat_errors_are_not_suppressed_by_exists_semantics(self):
        for error in (PermissionError(errno.EACCES, PRIVATE_ERROR), OSError(errno.EIO, PRIVATE_ERROR)):
            with self.subTest(error=type(error).__name__):
                with patch.object(Path, "stat", side_effect=error):
                    with self.assertRaises(type(error)) as caught:
                        self.read_day("news")
                self.assertIs(caught.exception, error)

    def test_open_errors_propagate_unchanged_without_printing_secrets(self):
        self.db_path("news").touch()
        for error in read_errors():
            with self.subTest(error=type(error).__name__):
                output = io.StringIO()
                with patch.object(history_reader, "connect_sqlite", side_effect=error):
                    with redirect_stdout(output), redirect_stderr(output), self.assertRaises(type(error)) as caught:
                        self.read_day("news")
                self.assertIs(caught.exception, error)
                self.assertEqual(output.getvalue(), "")

    def test_query_failure_preserves_error_and_closes_connection(self):
        self.db_path("news").touch()
        error = sqlite3.OperationalError(PRIVATE_ERROR)
        connection = Mock()
        connection.cursor.return_value.execute.side_effect = error
        with patch.object(history_reader, "connect_sqlite", return_value=connection):
            with self.assertRaises(sqlite3.OperationalError) as caught:
                self.read_day("news")
        self.assertIs(caught.exception, error)
        connection.close.assert_called_once_with()


class WeeklyHistoryFailureTests(TemporaryHistoryTests):
    def test_only_file_not_found_is_counted_by_fake_reader(self):
        for db_type in ("news", "rss"):
            with self.subTest(db_type=db_type):
                reader = Mock(spec=HistoryReader)
                reader.read_all_titles_for_date.side_effect = FileNotFoundError("synthetic missing day")
                stats = {f"missing_{db_type}_days": 1}
                self.ingest(reader, db_type, stats)
                self.assertEqual(stats, {f"missing_{db_type}_days": 2})

    def test_fake_reader_errors_do_not_mutate_missing_counts_or_log_secrets(self):
        for db_type in ("news", "rss"):
            for error in read_errors():
                with self.subTest(db_type=db_type, error=type(error).__name__):
                    reader = Mock(spec=HistoryReader)
                    reader.read_all_titles_for_date.side_effect = error
                    stats = {"missing_news_days": 2, "missing_rss_days": 3}
                    output = io.StringIO()
                    with redirect_stdout(output), redirect_stderr(output), self.assertRaises(type(error)) as caught:
                        self.ingest(reader, db_type, stats)
                    self.assertIs(caught.exception, error)
                    self.assertEqual(stats, {"missing_news_days": 2, "missing_rss_days": 3})
                    self.assertIn(type(error).__name__, output.getvalue())
                    self.assertIn("2026-10-01", output.getvalue())
                    self.assertIn(db_type, output.getvalue())
                    self.assertNotIn(SECRET, output.getvalue())
                    self.assertNotIn("example.invalid", output.getvalue())

    def test_real_missing_and_empty_days_preserve_coverage(self):
        self.seed("news")
        self.seed("rss")
        self.seed("news", populated=False, date=DAY + timedelta(days=1))
        with patch.object(collection, "PROJECT_ROOT", self.root), redirect_stdout(io.StringIO()):
            items, _, stats = collection.collect_news(DAY, DAY + timedelta(days=1))
        self.assertTrue(items)
        self.assertEqual(stats["missing_news_days"], 1)
        self.assertEqual(stats["missing_rss_days"], 1)
        self.assertEqual(stats["available_rss_days"], 1)
        self.assertEqual(stats["rss_day_coverage"], 0.5)

    def test_real_corrupt_day_aborts_collection_before_partial_selection(self):
        self.seed("news", date=DAY - timedelta(days=1))
        self.seed("rss", date=DAY - timedelta(days=1))
        for db_type in ("news", "rss"):
            with self.subTest(db_type=db_type):
                path = self.db_path(db_type)
                path.write_bytes(b"synthetic corrupt database")
                with patch.object(collection, "PROJECT_ROOT", self.root):
                    with patch.object(collection, "select_with_quota") as select:
                        output = io.StringIO()
                        with redirect_stdout(output), self.assertRaises(sqlite3.DatabaseError):
                            collection.collect_news(DAY - timedelta(days=1), DAY)
                select.assert_not_called()
                self.assertIn(f"sqlite_code={sqlite3.SQLITE_NOTADB}", output.getvalue())
                path.unlink()  # Only this test's temporary corrupt fixture.

    def test_cli_aborts_before_ai_mail_or_artifacts_with_safe_nonzero_status(self):
        replacements = {
            "PROJECT_ROOT": self.root,
            "OUTPUT_DIR": self.root / "reports",
            "load_runtime_env": Mock(return_value={}),
            "load_ai_config": Mock(),
            "AIClient": Mock(),
            "build_prompt": Mock(),
        }
        argv = ["weekly", "--start", "2026-10-01", "--end", "2026-10-01"]
        for error in read_errors():
            for flags in ([], ["--dry-run"]):
                with self.subTest(error=type(error).__name__, flags=flags):
                    output = io.StringIO()
                    reader = Mock(spec=HistoryReader)
                    reader.read_all_titles_for_date.side_effect = error
                    with patch.multiple(weekly, **replacements), patch.object(sys, "argv", argv + flags):
                        with patch.object(collection, "HistoryReader", return_value=reader):
                            with patch.object(weekly.NotificationDispatcher, "send_report") as mail:
                                with redirect_stdout(output), redirect_stderr(output):
                                    status = weekly.main()
                    self.assertEqual(status, 7)
                    self.assertEqual(status, weekly.HISTORY_READ_EXIT_CODE)
                    for name in ("load_ai_config", "AIClient", "build_prompt"):
                        replacements[name].assert_not_called()
                    mail.assert_not_called()
                    self.assertFalse(replacements["OUTPUT_DIR"].exists())
                    self.assertIn(type(error).__name__, output.getvalue())
                    self.assertNotIn(SECRET, output.getvalue())
                    self.assertNotIn("Traceback", output.getvalue())

    def test_cli_real_corrupt_database_uses_failure_exit_not_empty_exit(self):
        self.db_path("news").write_bytes(b"synthetic corrupt database")
        argv = ["weekly", "--start", "2026-10-01", "--end", "2026-10-01"]
        with patch.object(collection, "PROJECT_ROOT", self.root), patch.object(sys, "argv", argv):
            with patch.object(weekly, "load_runtime_env", return_value={}):
                with patch.object(weekly, "AIClient") as ai, patch.object(weekly.NotificationDispatcher, "send_report") as mail:
                    with redirect_stdout(io.StringIO()):
                        status = weekly.main()
        self.assertEqual(status, 7)
        ai.assert_not_called()
        mail.assert_not_called()

    def test_cli_all_missing_keeps_existing_no_data_exit(self):
        argv = ["weekly", "--start", "2026-10-01", "--end", "2026-10-01"]
        with patch.object(collection, "PROJECT_ROOT", self.root), patch.object(sys, "argv", argv):
            with patch.object(weekly, "load_runtime_env", return_value={}):
                with patch.object(weekly, "AIClient") as ai, redirect_stdout(io.StringIO()):
                    status = weekly.main()
        self.assertEqual(status, 1)
        ai.assert_not_called()


if __name__ == "__main__":
    unittest.main()
