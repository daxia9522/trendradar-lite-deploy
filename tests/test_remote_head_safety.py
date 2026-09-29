"""Remote HEAD safety regressions: in-memory S3 only, with socket I/O forbidden."""

import io
import sqlite3
import tempfile
import traceback
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError, EndpointConnectionError, ReadTimeoutError

from trendradar.storage import remote
from trendradar.storage.base import NewsData, NewsItem, RSSData, RSSItem
from trendradar.storage.local import LocalStorageBackend


DATE = "2026-09-28"
PRIVATE = "private-provider-body private-bucket private-object private-endpoint"


def client_error(code, status):
    response = {"Error": {"Code": code, "Message": PRIVATE}}
    if status is not None:
        response["ResponseMetadata"] = {"HTTPStatusCode": status}
    return ClientError(response, "HeadObject")


def head_failures():
    return (
        client_error("AccessDenied", 403),
        client_error("ServiceUnavailable", 503),
        ReadTimeoutError(endpoint_url="https://private-endpoint.invalid"),
        EndpointConnectionError(endpoint_url="https://private-endpoint.invalid"),
        TimeoutError(PRIVATE),
    )


def sample_data(db_type, title="new", crawl_time="12:00"):
    url = f"https://example.invalid/{title}"
    if db_type == "news":
        return NewsData(
            DATE, crawl_time,
            {"source": [NewsItem(title, "source", rank=1, url=url, crawl_time=crawl_time)]},
            {"source": "Source"},
        )
    return RSSData(
        DATE, crawl_time,
        {"feed": [RSSItem(title, "feed", url=url, guid=title, crawl_time=crawl_time)]},
        {"feed": "Feed"},
    )


class MemoryS3:
    """Record all requests; store uploaded bytes without constructing an SDK client."""

    def __init__(self):
        self.objects = {}
        self.head_object = Mock(side_effect=self.head)
        self.get_object = Mock(side_effect=self.get)
        self.put_object = Mock(side_effect=self.put)

    def head(self, *, Bucket, Key):
        if Key not in self.objects:
            raise client_error("404", 404)
        return {"ContentLength": len(self.objects[Key])}

    def get(self, *, Bucket, Key):
        body = Mock()
        body.iter_chunks.return_value = iter([self.objects[Key]])
        return {"Body": body}

    def put(self, *, Bucket, Key, Body, **kwargs):
        self.objects[Key] = Body
        return {}


class RemoteHeadSafetyTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.enterContext(redirect_stdout(self.output))
        # Fail closed even if a future change accidentally constructs a real client.
        for target in ("socket.socket.connect", "socket.socket.connect_ex", "socket.getaddrinfo"):
            guard = self.enterContext(patch(target, side_effect=AssertionError("Network forbidden")))
            self.addCleanup(guard.assert_not_called)
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="head-safety-")))
        self.factory = self.enterContext(patch.object(remote.boto3, "client"))
        self.index = 0

    def backend(self):
        self.index += 1
        s3 = MemoryS3()
        self.factory.return_value = s3
        backend = remote.RemoteStorageBackend(
            bucket_name="private-bucket",
            access_key_id="offline-key",
            secret_access_key="offline-secret",
            endpoint_url="https://private-endpoint.invalid",
            temp_dir=str(self.root / f"backend-{self.index}"),
            timezone="UTC",
        )
        self.addCleanup(backend.cleanup)
        backend._get_configured_time = Mock(return_value=datetime(2026, 9, 28, 12))
        return backend, s3

    def tearDown(self):
        # Includes errors caught and string-formatted by the RSS mixin.
        for marker in PRIVATE.split():
            self.assertNotIn(marker, self.output.getvalue())

    def seed(self, s3, db_type):
        # Build a genuine historical database entirely locally.
        with tempfile.TemporaryDirectory(dir=self.root) as directory:
            local = LocalStorageBackend(directory, enable_txt=False, timezone="UTC")
            try:
                self.assertTrue(getattr(local, f"save_{db_type}_data")(sample_data(db_type, "old", "11:00")))
            finally:
                local.cleanup()
            s3.objects[f"{db_type}/{DATE}.db"] = (Path(directory) / db_type / f"{DATE}.db").read_bytes()

    def titles(self, s3, db_type):
        path = self.root / "uploaded.db"
        path.write_bytes(s3.objects[f"{db_type}/{DATE}.db"])
        conn = sqlite3.connect(path)
        try:
            return {row[0] for row in conn.execute(f"SELECT title FROM {db_type}_items")}
        finally:
            conn.close()

    def save_failure(self, backend, db_type):
        operation = getattr(backend, f"save_{db_type}_data")
        if db_type == "news":
            with self.assertRaises(remote.RemoteObjectCheckError):
                operation(sample_data(db_type))
        else:
            # The shared RSS mixin returns False on errors, rather than raising.
            self.assertFalse(operation(sample_data(db_type)))

    def assert_pristine(self, backend, s3, db_type):
        s3.get_object.assert_not_called()
        s3.put_object.assert_not_called()
        self.assertFalse(backend._get_local_db_path(DATE, db_type).exists())
        self.assertEqual(backend._db_connections, {})
        self.assertEqual(backend._downloaded_files, [])
        self.assertEqual(backend._batch_dirty, set())

    def test_head_success_is_true(self):
        backend, s3 = self.backend()
        s3.objects["private-object"] = b"existing"
        self.assertIs(backend._check_object_exists("private-object"), True)
        s3.get_object.assert_not_called()
        s3.put_object.assert_not_called()

    def test_only_confirmed_missing_returns_false(self):
        for code, status in (("404", 404), ("NoSuchKey", 404), ("Not Found", 404),
                             ("NoSuchKey", None), ("", 404)):
            with self.subTest(code=code, status=status):
                backend, s3 = self.backend()
                s3.head_object.side_effect = client_error(code, status)
                self.assertIs(backend._check_object_exists("private-object"), False)
                self.assertIsNone(backend._download_sqlite(DATE))
                self.assert_pristine(backend, s3, "news")

    def test_unknown_head_raises_sanitized_error(self):
        errors = (*head_failures(), client_error("NoSuchBucket", 404),
                  client_error("NoSuchKey", 403), client_error("404", 503),
                  client_error("private-provider-code", 404))
        for error in errors:
            with self.subTest(error_type=type(error).__name__, response=getattr(error, "response", None)):
                backend, s3 = self.backend()
                s3.head_object.side_effect = error
                with self.assertRaises(remote.RemoteObjectCheckError) as caught:
                    backend._check_object_exists("private-object")
                self.assertIn(type(error).__name__, str(caught.exception))
                self.assertTrue(caught.exception.__suppress_context__)
                self.assertIsNone(caught.exception.__cause__)
                rendered = "".join(traceback.format_exception(caught.exception))
                for marker in PRIVATE.split():
                    self.assertNotIn(marker, str(caught.exception))
                    self.assertNotIn(marker, rendered)
                self.assert_pristine(backend, s3, "news")

    def test_malformed_head_response_is_unknown_not_absence(self):
        for response in (None, {}, {"Error": None}, {"ResponseMetadata": None},
                         {"Error": {"Code": []}, "ResponseMetadata": {"HTTPStatusCode": 404}},
                         {"Error": {"Code": False}, "ResponseMetadata": {"HTTPStatusCode": 404}},
                         {"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": "404"}}):
            with self.subTest(response=response):
                backend, s3 = self.backend()
                error = client_error("404", 404)
                error.response = response
                s3.head_object.side_effect = error
                with self.assertRaises(remote.RemoteObjectCheckError):
                    backend._check_object_exists("private-object")
                self.assert_pristine(backend, s3, "news")

    def test_get_404_uses_the_same_not_found_classification(self):
        for code, status, missing in (("NoSuchKey", 404, True), ("404", 404, True),
                                      ("NoSuchBucket", 404, False), ("NoSuchKey", 503, False)):
            with self.subTest(code=code, status=status):
                backend, s3 = self.backend()
                s3.head_object.side_effect = None
                error = client_error(code, status)
                s3.get_object.side_effect = error
                if missing:
                    self.assertIsNone(backend._download_sqlite(DATE))
                else:
                    with self.assertRaises(ClientError):
                        backend._download_sqlite(DATE)
                self.assertFalse(backend._get_local_db_path(DATE).exists())
                s3.put_object.assert_not_called()

    def test_head_failure_never_opens_or_initializes_sqlite(self):
        for db_type in ("news", "rss"):
            for error in head_failures():
                with self.subTest(db_type=db_type, error_type=type(error).__name__):
                    backend, s3 = self.backend()
                    s3.head_object.side_effect = error
                    with patch.object(remote.sqlite3, "connect", wraps=sqlite3.connect) as connect, \
                         patch.object(backend, "_init_tables", wraps=backend._init_tables) as init:
                        try:
                            backend._get_connection(DATE, db_type)
                        except RuntimeError:
                            pass
                        connect.assert_not_called()
                        init.assert_not_called()
                    self.assert_pristine(backend, s3, db_type)

    def test_head_failure_cannot_save_or_queue_upload_even_after_repeated_attempts(self):
        for batch in (False, True):
            for db_type in ("news", "rss"):
                for error in head_failures():
                    with self.subTest(batch=batch, db_type=db_type, error_type=type(error).__name__):
                        backend, s3 = self.backend()
                        self.seed(s3, db_type)
                        before = dict(s3.objects)
                        s3.head_object.side_effect = error
                        if batch:
                            backend.begin_batch()
                        for attempt in range(1, 4):
                            # Assert side effects before return/exception contracts, so
                            # the red test explicitly proves the overwrite path.
                            try:
                                result = getattr(backend, f"save_{db_type}_data")(sample_data(db_type))
                            except RuntimeError:
                                result = False
                            self.assertTrue(s3.objects == before, "HEAD failure overwrote remote bytes")
                            self.assert_pristine(backend, s3, db_type)
                            self.assertFalse(result)
                            self.assertEqual(s3.head_object.call_count, attempt)
                        self.assertTrue(backend.end_batch())
                        self.assert_pristine(backend, s3, db_type)

    def test_confirmed_404_allows_first_creation_and_upload(self):
        for db_type in ("news", "rss"):
            with self.subTest(db_type=db_type):
                backend, s3 = self.backend()
                self.assertTrue(getattr(backend, f"save_{db_type}_data")(sample_data(db_type)))
                self.assertEqual(self.titles(s3, db_type), {"new"})
                s3.get_object.assert_not_called()
                self.assertEqual(s3.put_object.call_count, 1)
                self.assertEqual(s3.head_object.call_count, 2)  # preflight + verification

    def test_caller_retry_after_head_failure_downloads_and_merges_history(self):
        for db_type in ("news", "rss"):
            for error in head_failures():
                with self.subTest(db_type=db_type, error_type=type(error).__name__):
                    backend, s3 = self.backend()
                    self.seed(s3, db_type)
                    before = dict(s3.objects)
                    s3.head_object.side_effect = error
                    self.save_failure(backend, db_type)
                    self.assert_pristine(backend, s3, db_type)
                    self.assertEqual(s3.objects, before)
                    s3.head_object.side_effect = s3.head
                    self.assertTrue(getattr(backend, f"save_{db_type}_data")(sample_data(db_type)))
                    s3.get_object.assert_called_once()
                    s3.put_object.assert_called_once()
                    self.assertEqual(self.titles(s3, db_type), {"old", "new"})
                    self.assertEqual(s3.head_object.call_count, 3)

    def test_caller_retry_may_create_only_after_new_confirmed_404(self):
        for db_type in ("news", "rss"):
            backend, s3 = self.backend()
            s3.head_object.side_effect = client_error("ServiceUnavailable", 503)
            self.save_failure(backend, db_type)
            self.assert_pristine(backend, s3, db_type)
            s3.head_object.side_effect = s3.head
            self.assertTrue(getattr(backend, f"save_{db_type}_data")(sample_data(db_type)))
            self.assertEqual(self.titles(s3, db_type), {"new"})
            self.assertEqual(s3.head_object.call_count, 3)

    def test_verification_head_failure_retains_dirty_database_for_flush_retry(self):
        for db_type in ("news", "rss"):
            backend, s3 = self.backend()
            backend.begin_batch()
            self.assertTrue(getattr(backend, f"save_{db_type}_data")(sample_data(db_type)))
            s3.put_object.assert_not_called()
            s3.head_object.side_effect = client_error("ServiceUnavailable", 503)
            self.assertFalse(backend.end_batch())
            self.assertIn("上传结果无法确认", self.output.getvalue())
            self.assertNotIn("文件未在远程存储中找到", self.output.getvalue())
            self.assertFalse(backend._batch_mode)
            self.assertEqual(backend._batch_dirty, {(DATE, db_type)})
            self.assertEqual(self.titles(s3, db_type), {"new"})
            s3.head_object.side_effect = s3.head
            self.assertTrue(backend.flush())
            self.assertEqual(backend._batch_dirty, set())
            self.assertEqual(s3.put_object.call_count, 2)

    def test_failed_initialization_does_not_discard_other_dirty_objects(self):
        backend, s3 = self.backend()
        backend.begin_batch()
        self.assertTrue(backend.save_news_data(sample_data("news")))
        s3.head_object.side_effect = client_error("AccessDenied", 403)
        self.assertFalse(backend.save_rss_data(sample_data("rss")))
        self.assertEqual(backend._batch_dirty, {(DATE, "news")})
        self.assertFalse(backend._get_local_db_path(DATE, "rss").exists())
        s3.put_object.assert_not_called()
        s3.head_object.side_effect = s3.head
        self.assertTrue(backend.end_batch())
        self.assertEqual(backend._batch_dirty, set())
        self.assertEqual(set(s3.objects), {f"news/{DATE}.db"})
        self.assertEqual(self.titles(s3, "news"), {"new"})

    def test_pull_head_failure_is_logged_as_failure_and_other_objects_continue(self):
        backend, s3 = self.backend()
        self.seed(s3, "rss")

        def head(*, Bucket, Key):
            if Key.startswith("news/"):
                raise client_error("AccessDenied", 403)
            return s3.head(Bucket=Bucket, Key=Key)

        s3.head_object.side_effect = head
        output_dir = self.root / "pulled"
        self.assertEqual(backend.pull_recent_days(1, str(output_dir)), 1)
        self.assertIn("拉取失败", self.output.getvalue())
        self.assertNotIn("远程不存在", self.output.getvalue())
        self.assertFalse((output_dir / "news" / f"{DATE}.db").exists())
        self.assertEqual((output_dir / "rss" / f"{DATE}.db").read_bytes(), s3.objects[f"rss/{DATE}.db"])
        self.assertFalse(list(output_dir.rglob("*.part")))
        s3.put_object.assert_not_called()


if __name__ == "__main__":
    unittest.main()
