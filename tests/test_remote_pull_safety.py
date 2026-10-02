"""Pull regressions using real local SQLite files and fake S3, with no network I/O."""

import io
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError

from trendradar.storage import remote


DATE = "2026-09-28"
KEYS = [f"{kind}/{date}.db" for date in (DATE, "2026-09-27") for kind in ("news", "rss")]


def sqlite_payload(path, page_size=1024, journal_mode="DELETE"):
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(f"PRAGMA page_size = {page_size}")
        connection.execute(f"PRAGMA journal_mode = {journal_mode}")
        connection.execute("CREATE TABLE sample (value TEXT NOT NULL)")
        connection.executemany("INSERT INTO sample VALUES (?)", [(f"row-{i}-" + "x" * 100,) for i in range(40)])
        connection.commit()
    return path.read_bytes()


class TrackingBody(io.BytesIO):
    def __init__(self, payload, error=None, before_chunk=None):
        super().__init__(payload)
        self.error = error
        self.before_chunk = before_chunk
        self.close_calls = 0

    def iter_chunks(self, chunk_size):
        # Multiple reads exercise partial writes, including a harmless empty chunk.
        yield b""
        while chunk := self.read(min(chunk_size, 512)):
            if self.before_chunk:
                self.before_chunk()
            yield chunk
            if self.error:
                raise self.error

    def close(self):
        self.close_calls += 1
        super().close()


class MemoryS3:
    def __init__(self, payload):
        self.objects = dict.fromkeys(KEYS, payload)
        self.read_errors = {}
        self.before_chunk = None
        self.bodies = []
        self.head_object = Mock(side_effect=self.head)
        self.get_object = Mock(side_effect=self.get)
        self.put_object = Mock()

    def head(self, *, Bucket, Key):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}, "ResponseMetadata": {"HTTPStatusCode": 404}}, "HeadObject")
        return {}

    def get(self, *, Bucket, Key):
        body = TrackingBody(self.objects[Key], self.read_errors.get(Key), self.before_chunk)
        self.bodies.append(body)
        return {"Body": body}


class RemotePullSafetyTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.enterContext(redirect_stdout(self.output))
        for target in ("socket.socket.connect", "socket.socket.connect_ex", "socket.getaddrinfo", "boto3.client"):
            guard = self.enterContext(patch(target, side_effect=AssertionError("Network forbidden")))
            self.addCleanup(guard.assert_not_called)
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="pull-safety-")))
        self.payload = sqlite_payload(self.root / "fixture.db")
        self.s3 = MemoryS3(self.payload)
        self.backend = object.__new__(remote.RemoteStorageBackend)
        self.backend.bucket_name = "offline-bucket"
        self.backend.timezone = "UTC"
        self.backend.s3_client = self.s3
        self.backend._get_configured_time = Mock(return_value=datetime(2026, 9, 28, 12))
        # Exercise correct escaping of the read-only SQLite URI as well.
        self.destination = self.root / "pulled #?% 数据"
        self.addCleanup(self.s3.put_object.assert_not_called)
        self.addCleanup(self.close_bodies)

    def close_bodies(self):
        for body in self.s3.bodies:
            if not body.closed:
                body.close()

    def assert_closed(self):
        self.assertTrue(self.s3.bodies)
        for body in self.s3.bodies:
            self.assertTrue(body.closed)
            self.assertEqual(body.close_calls, 1)

    def assert_files(self, keys):
        files = {path.relative_to(self.destination).as_posix()
                 for path in self.destination.rglob("*") if path.is_file()}
        self.assertEqual(files, set(keys))  # No .part, journal, WAL or SHM leftovers.
        for key in keys:
            self.assertEqual((self.destination / key).read_bytes(), self.s3.objects[key])

    def test_success_closes_bodies_and_preserves_queryable_sqlite(self):
        self.assertEqual(self.backend.pull_recent_days(2, str(self.destination)), 4)
        self.assert_closed()
        self.assert_files(KEYS)
        for key in KEYS:
            with closing(sqlite3.connect(self.destination / key)) as connection:
                self.assertEqual(connection.execute("PRAGMA quick_check").fetchall(), [("ok",)])
                self.assertEqual(connection.execute("SELECT count(*) FROM sample").fetchone(), (40,))
        self.assertEqual(self.s3.head_object.call_count, 4)
        self.assertEqual(self.s3.get_object.call_count, 4)

    def test_unique_same_directory_temporary_files_and_close_before_publish(self):
        sentinel = self.destination / f"news/{DATE}.db.part"
        sentinel.parent.mkdir(parents=True)
        sentinel.write_bytes(b"another download owns this file")
        seen = set()

        def before_chunk():
            key = self.s3.get_object.call_args.kwargs["Key"]
            target = self.destination / key
            self.assertFalse(target.exists())
            partials = set(target.parent.glob("*.part")) - {sentinel}
            self.assertEqual(len(partials), 1)
            partial = partials.pop()
            self.assertNotEqual(partial, target.with_suffix(".db.part"))
            seen.add(partial)

        original_replace = Path.replace

        def replace(path, target):
            self.assertTrue(self.s3.bodies[-1].closed)
            self.assertEqual(path.parent, target.parent)
            self.assertIn(path, seen)
            return original_replace(path, target)

        self.s3.before_chunk = before_chunk
        with patch.object(Path, "replace", autospec=True, side_effect=replace):
            self.assertEqual(self.backend.pull_recent_days(2, str(self.destination)), 4)
        self.assertEqual(len(seen), 4)
        self.assert_closed()
        self.assertEqual(sentinel.read_bytes(), b"another download owns this file")
        self.assertEqual(list(self.destination.rglob("*.part")), [sentinel])

    def test_read_interruption_cleans_partial_continues_and_can_retry(self):
        for failed_key in KEYS[:2]:
            with self.subTest(key=failed_key):
                self.destination = self.root / failed_key.split("/")[0]
                self.s3.read_errors = {failed_key: OSError("private stream failure")}
                self.assertEqual(self.backend.pull_recent_days(2, str(self.destination)), 3)
                self.assert_files(set(KEYS) - {failed_key})
                self.assert_closed()
                self.s3.read_errors.clear()
                self.assertEqual(self.backend.pull_recent_days(2, str(self.destination)), 1)
                self.assert_files(KEYS)
                self.assert_closed()
        self.assertNotIn("private stream failure", self.output.getvalue())

    def test_invalid_sqlite_is_not_published_and_can_retry(self):
        corrupted = bytearray(self.payload)
        corrupted[1024] = 0  # Invalid b-tree page type, but valid header and page length.
        invalid_page_size = bytearray(self.payload)
        invalid_page_size[16:18] = (768).to_bytes(2, "big")
        invalid_freelist = bytearray(self.payload)
        invalid_freelist[36:40] = (1).to_bytes(4, "big")  # Count 1, but no freelist head.
        invalid_path = self.root / "invalid-freelist.db"
        invalid_path.write_bytes(invalid_freelist)
        with closing(sqlite3.connect(f"{invalid_path.as_uri()}?mode=ro&immutable=1", uri=True)) as connection:
            # Prove this fixture returns error rows rather than raising an exception.
            self.assertNotEqual(connection.execute("PRAGMA quick_check").fetchall(), [("ok",)])
        payloads = {
            "empty": b"",
            "not-sqlite": b"not a SQLite database" * 200,
            "header-only": self.payload[:100],
            "truncated-byte": self.payload[:-1],
            "truncated-page": self.payload[:-1024],
            "trailing-byte": self.payload + b"x",
            "trailing-page": self.payload + bytes(1024),
            "invalid-page-size": bytes(invalid_page_size),
            "corrupted-page": bytes(corrupted),
            "quick-check-error-rows": bytes(invalid_freelist),
        }
        for name, payload in payloads.items():
            with self.subTest(payload=name):
                self.destination = self.root / name
                self.s3.objects[KEYS[0]] = payload
                self.assertEqual(self.backend.pull_recent_days(2, str(self.destination)), 3)
                self.assert_files(KEYS[1:])
                self.assert_closed()
                self.s3.objects[KEYS[0]] = self.payload
                self.assertEqual(self.backend.pull_recent_days(2, str(self.destination)), 1)
                self.assert_files(KEYS)
                self.assert_closed()

    def test_valid_sqlite_page_sizes_including_65536_encoding(self):
        for page_size in (512, 4096, 65536):
            with self.subTest(page_size=page_size):
                self.destination = self.root / str(page_size)
                payload = sqlite_payload(self.root / f"fixture-{page_size}.db", page_size)
                self.s3.objects = dict.fromkeys(KEYS, payload)
                self.assertEqual(self.backend.pull_recent_days(1, str(self.destination)), 2)
                self.assert_files(KEYS[:2])
                self.assert_closed()

    def test_wal_snapshot_validation_leaves_no_sidecars(self):
        # The fixture connection closes and checkpoints before its main file is read.
        payload = sqlite_payload(self.root / "wal.db", journal_mode="WAL")
        self.assertEqual(payload[18:20], b"\x02\x02")
        self.s3.objects = dict.fromkeys(KEYS, payload)
        self.assertEqual(self.backend.pull_recent_days(2, str(self.destination)), 4)
        self.assert_files(KEYS)
        self.assert_closed()

    def test_body_close_failure_does_not_publish_and_other_objects_continue(self):
        original_get = self.s3.get

        def get(**kwargs):
            response = original_get(**kwargs)
            if kwargs["Key"] == KEYS[0]:
                body = response["Body"]
                original_close = body.close

                def close():
                    original_close()
                    raise OSError("private body close failure")

                body.close = close
            return response

        self.s3.get_object.side_effect = get
        self.assertEqual(self.backend.pull_recent_days(2, str(self.destination)), 3)
        self.assert_files(KEYS[1:])
        self.assert_closed()
        self.assertNotIn("private body close failure", self.output.getvalue())

    def test_temporary_close_failure_cleans_partial_and_other_objects_continue(self):
        original_temporary_file = tempfile.NamedTemporaryFile
        handles = []

        class FailingCloseFile:
            def __init__(self, handle):
                self.handle = handle

            def __enter__(self):
                return self.handle.__enter__()

            def __exit__(self, *args):
                self.handle.__exit__(*args)
                raise OSError("private file close failure")

        def temporary_file(*args, **kwargs):
            handle = original_temporary_file(*args, **kwargs)
            handles.append(handle)
            return FailingCloseFile(handle) if len(handles) == 1 else handle

        with patch.object(remote.tempfile, "NamedTemporaryFile", side_effect=temporary_file):
            self.assertEqual(self.backend.pull_recent_days(2, str(self.destination)), 3)
        self.assertTrue(all(handle.closed for handle in handles))
        self.assert_files(KEYS[1:])
        self.assert_closed()
        self.assertNotIn("private file close failure", self.output.getvalue())

    def test_temporary_creation_or_write_failure_closes_body_and_continues(self):
        original_temporary_file = tempfile.NamedTemporaryFile
        for failure in ("create", "write"):
            with self.subTest(failure=failure):
                self.destination = self.root / failure
                handles = []
                attempts = []

                def temporary_file(*args, **kwargs):
                    attempts.append(True)
                    if len(attempts) == 1 and failure == "create":
                        raise OSError("private file creation failure")
                    handle = original_temporary_file(*args, **kwargs)
                    handles.append(handle)
                    if len(attempts) == 1 and failure == "write":
                        handle.write = Mock(side_effect=OSError("private write failure"))
                    return handle

                with patch.object(remote.tempfile, "NamedTemporaryFile", side_effect=temporary_file):
                    self.assertEqual(self.backend.pull_recent_days(2, str(self.destination)), 3)
                self.assert_files(KEYS[1:])
                self.assert_closed()
                self.assertTrue(all(handle.closed for handle in handles))
        self.assertNotIn("private", self.output.getvalue())

    def test_publish_failure_cleans_partial_and_continues(self):
        original_replace = Path.replace

        def replace(path, target):
            if target == self.destination / KEYS[0]:
                raise OSError("private replace failure")
            return original_replace(path, target)

        with patch.object(Path, "replace", autospec=True, side_effect=replace):
            self.assertEqual(self.backend.pull_recent_days(2, str(self.destination)), 3)
        self.assert_closed()
        self.assert_files(KEYS[1:])
        self.assertNotIn("private replace failure", self.output.getvalue())

    def test_sqlite_validation_connections_close_on_success_and_failure(self):
        original_connect = sqlite3.connect
        connections = []

        class TrackingConnection(sqlite3.Connection):
            closed = False

            def close(self):
                self.closed = True
                super().close()

        def connect(*args, **kwargs):
            connection = original_connect(*args, **kwargs, factory=TrackingConnection)
            connections.append(connection)
            self.addCleanup(connection.close)
            return connection

        corrupted = bytearray(self.payload)
        corrupted[1024] = 0
        self.s3.objects[KEYS[0]] = bytes(corrupted)
        with patch.object(remote.sqlite3, "connect", side_effect=connect) as mocked:
            self.assertEqual(self.backend.pull_recent_days(2, str(self.destination)), 3)
        self.assertEqual(len(connections), 4)
        self.assertTrue(all(connection.closed for connection in connections))
        for call in mocked.call_args_list:
            self.assertTrue(call.kwargs["uri"])
            self.assertIn("mode=ro", call.args[0])
            self.assertIn("immutable=1", call.args[0])
        self.assert_files(KEYS[1:])
        self.assert_closed()

    def test_unknown_head_is_failure_not_absence_and_other_objects_continue(self):
        for error in (TimeoutError("private timeout"), ClientError(
            {"Error": {"Code": "AccessDenied"}, "ResponseMetadata": {"HTTPStatusCode": 403}}, "HeadObject",
        )):
            with self.subTest(error=type(error).__name__):
                self.destination = self.root / type(error).__name__

                def head(*, Bucket, Key):
                    if Key == KEYS[0]:
                        raise error
                    return self.s3.head(Bucket=Bucket, Key=Key)

                self.s3.head_object.side_effect = head
                self.s3.get_object.reset_mock()
                self.assertEqual(self.backend.pull_recent_days(2, str(self.destination)), 3)
                self.assert_files(KEYS[1:])
                self.assert_closed()
                self.assertEqual([call.kwargs["Key"] for call in self.s3.get_object.call_args_list], KEYS[1:])
        self.assertIn("拉取失败", self.output.getvalue())
        self.assertNotIn("远程不存在", self.output.getvalue())
        self.assertNotIn("private timeout", self.output.getvalue())

    def test_confirmed_missing_and_get_failure_preserve_unowned_partial(self):
        sentinel = self.destination / f"news/{DATE}.db.part"
        sentinel.parent.mkdir(parents=True)
        sentinel.write_bytes(b"unowned partial")
        del self.s3.objects[KEYS[0]]
        self.assertEqual(self.backend.pull_recent_days(1, str(self.destination)), 1)
        self.assertIn("远程不存在", self.output.getvalue())
        self.s3.objects[KEYS[0]] = self.payload
        self.s3.get_object.side_effect = OSError("private get failure")
        self.assertEqual(self.backend.pull_recent_days(1, str(self.destination)), 0)
        self.assertFalse((self.destination / KEYS[0]).exists())
        self.assertEqual(sentinel.read_bytes(), b"unowned partial")
        self.assert_closed()
        self.assertNotIn("private get failure", self.output.getvalue())

    def test_existing_local_file_is_not_replaced_or_requested(self):
        existing = self.destination / KEYS[0]
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"keep existing local data")
        self.assertEqual(self.backend.pull_recent_days(2, str(self.destination)), 3)
        self.assertEqual(existing.read_bytes(), b"keep existing local data")
        for method in (self.s3.head_object, self.s3.get_object):
            self.assertEqual([call.kwargs["Key"] for call in method.call_args_list], KEYS[1:])
        self.assert_closed()
        self.assertFalse(list(self.destination.rglob("*.part")))

    def test_nonpositive_days_do_not_access_s3_or_create_directory(self):
        for days in (0, -1):
            self.assertEqual(self.backend.pull_recent_days(days, str(self.destination)), 0)
        self.s3.head_object.assert_not_called()
        self.s3.get_object.assert_not_called()
        self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main()
