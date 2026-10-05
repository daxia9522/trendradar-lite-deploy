"""Publication storage tests: temporary SQLite and offline S3 fakes/Stubber only."""

import hashlib
import io
import json
import multiprocessing
import os
import sqlite3
import stat
import tempfile
import traceback
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from pathlib import Path
from threading import Barrier, Lock
from types import SimpleNamespace
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError

from trendradar.storage.manager import StorageManager
from trendradar.storage.publication import (
    LocalPublicationStore, PublicationConflict, PublicationError, RemotePublicationStore,
)


PRIVATE = "synthetic-secret recipient@example.invalid <html>private body</html>"


def s3_error(code, status):
    return ClientError({"Error": {"Code": code, "Message": PRIVATE},
                        "ResponseMetadata": {"HTTPStatusCode": status}}, "GetObject")


class RecordingBody(io.BytesIO):
    def __init__(self, value):
        super().__init__(value)
        self.sizes = []

    def read(self, size=-1):
        if size < 0:
            raise AssertionError("unbounded read")
        self.sizes.append(size)
        return super().read(size)


class FakeS3:
    """Minimal conditional service; never creates a real AWS client."""

    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.meta = SimpleNamespace(service_model=SimpleNamespace(operation_model=lambda name:
            SimpleNamespace(input_shape=SimpleNamespace(members={"IfMatch": None, "IfNoneMatch": None}))))
        self.calls = []
        self.bodies = []
        self.get_error = None
        self.put_error = None
        self.lose_put_response = False
        self.missing_put_etag = False
        self.lock = Lock()

    @staticmethod
    def etag(payload):
        return '"' + hashlib.sha256(payload).hexdigest() + '"'

    def get_object(self, **kwargs):
        self.calls.append(("get", kwargs))
        if self.get_error is not None:
            raise self.get_error
        key = kwargs["Key"]
        with self.lock:
            if key not in self.objects:
                raise s3_error("NoSuchKey", 404)
            payload = self.objects[key]
        body = RecordingBody(payload)
        self.bodies.append(body)
        return {"Body": body, "ETag": self.etag(payload), "ContentLength": len(payload)}

    def put_object(self, **kwargs):
        self.calls.append(("put", kwargs))
        if self.put_error is not None:
            raise self.put_error
        if ("IfMatch" in kwargs) == ("IfNoneMatch" in kwargs):
            raise AssertionError("exactly one write precondition is required")
        key = kwargs["Key"]
        with self.lock:
            old = self.objects.get(key)
            if (kwargs.get("IfNoneMatch") == "*" and old is not None
                    or "IfMatch" in kwargs and (old is None or self.etag(old) != kwargs["IfMatch"])):
                raise s3_error("PreconditionFailed", 412)
            self.objects[key] = kwargs["Body"]
        if self.lose_put_response:
            raise TimeoutError(PRIVATE)
        return {} if self.missing_put_etag else {"ETag": self.etag(kwargs["Body"])}


def fresh_remote_reader(objects, output):
    store = RemotePublicationStore(FakeS3(objects), "offline-bucket")
    output.put((store.load(), store.get_snapshot("snapshot-1")))


def local_process_cas(path, barrier, output):
    store = LocalPublicationStore(path)
    document, version = store.load()
    barrier.wait(timeout=10)
    try:
        store.save({"count": document["count"] + 1}, version)
        output.put("saved")
    except PublicationConflict:
        output.put("conflict")
    finally:
        store.close()


class LocalPublicationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = LocalPublicationStore(self.root)
        self.addCleanup(self.store.close)

    def execute(self, sql, parameters=()):
        connection = sqlite3.connect(self.store.path)
        try:
            connection.execute(sql, parameters)
            connection.commit()
        finally:
            connection.close()

    def test_absent_manifest_and_reopen_roundtrip(self):
        self.assertEqual(self.store.load(), (None, None))
        document = {"baseline": {"news": ["one"]}, "rss": [], "title": "中文"}
        version = self.store.save(document, None)
        self.store.put_snapshot("snapshot-1", {"prepared": PRIVATE})
        document["baseline"]["news"].append("mutated")
        self.store.close()
        reopened = LocalPublicationStore(self.root)
        self.addCleanup(reopened.close)
        value, actual_version = reopened.load()
        self.assertEqual(actual_version, version)
        self.assertEqual(value["baseline"]["news"], ["one"])
        self.assertEqual(reopened.get_snapshot("snapshot-1"), {"prepared": PRIVATE})
        value["rss"].append("mutated")
        self.assertEqual(reopened.load()[0]["rss"], [])

    def test_private_database_and_directory_permissions(self):
        self.store.save({"receipt": PRIVATE}, None)
        self.store.put_snapshot("private", {"html": PRIVATE})
        self.assertEqual(stat.S_IMODE(self.store.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.store.path.parent.stat().st_mode), 0o700)
        self.assertFalse(list(self.store.path.parent.glob(".publication-*")))
        # Existing permissive files are tightened on reopen, not overwritten.
        self.store.path.chmod(0o644)
        self.store.path.parent.chmod(0o755)
        reopened = LocalPublicationStore(self.root)
        self.addCleanup(reopened.close)
        self.assertEqual(stat.S_IMODE(reopened.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(reopened.path.parent.stat().st_mode), 0o700)

    def test_sqlite_journal_is_private_and_rollback_is_atomic(self):
        version = self.store.save({"count": 1}, None)
        with self.store._connect() as connection:
            self.assertEqual(connection.execute("PRAGMA synchronous").fetchone()[0], 2)
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "delete")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("UPDATE manifest SET version='uncommitted'")
            journal = Path(str(self.store.path) + "-journal")
            self.assertEqual(stat.S_IMODE(journal.stat().st_mode), 0o600)
            connection.rollback()
        connection.close()
        self.assertEqual(self.store.load(), ({"count": 1}, version))

    def test_cas_create_update_and_aba(self):
        first = self.store.save({"count": 1}, None)
        with self.assertRaises(PublicationConflict):
            self.store.save({"count": 2}, None)
        second = self.store.save({"count": 1}, first)
        self.assertNotEqual(first, second)
        with self.assertRaises(PublicationConflict):
            self.store.save({"count": 3}, first)
        self.assertEqual(self.store.load(), ({"count": 1}, second))

    def test_process_concurrent_cas_has_one_winner(self):
        self.store.save({"count": 0}, None)
        context = multiprocessing.get_context("spawn")
        barrier, output = context.Barrier(2), context.Queue()
        processes = [context.Process(target=local_process_cas, args=(str(self.root), barrier, output))
                     for _ in range(2)]
        try:
            for process in processes:
                process.start()
            outcomes = [output.get(timeout=20) for _ in processes]
            for process in processes:
                process.join(timeout=20)
                self.assertEqual(process.exitcode, 0)
            self.assertEqual(sorted(outcomes), ["conflict", "saved"])
            self.assertEqual(self.store.load()[0], {"count": 1})
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join()
            output.close()

    def test_snapshot_idempotence_immutable_and_mutation_detached(self):
        self.store.put_snapshot("report-1", {"b": 2, "a": [1]})
        self.store.put_snapshot("report-1", {"a": [1], "b": 2})
        with self.assertRaises(PublicationConflict):
            self.store.put_snapshot("report-1", {"a": [2], "b": 2})
        fetched = self.store.get_snapshot("report-1")
        fetched["a"].append(9)
        self.assertEqual(self.store.get_snapshot("report-1"), {"a": [1], "b": 2})
        with self.assertRaises(PublicationError):
            self.store.get_snapshot("missing")

    def test_corrupt_manifest_does_not_become_bootstrap_or_get_overwritten(self):
        version = self.store.save({"valid": True}, None)
        self.execute("UPDATE manifest SET payload=?", (b'{"valid":false}',))
        for action in (self.store.load, lambda: self.store.save({}, version), lambda: self.store.save({}, None)):
            with self.subTest(action=action), self.assertRaises(PublicationError):
                action()

    def test_corrupt_and_oversized_snapshot_never_returns_empty(self):
        self.store.put_snapshot("report-1", {"valid": True})
        for invalid in (b"", b"{", b'{"valid":false}', "not a blob"):
            self.execute("UPDATE snapshots SET payload=?", (invalid,))
            with self.subTest(invalid=invalid), self.assertRaises(PublicationError):
                self.store.get_snapshot("report-1")
        self.execute("UPDATE snapshots SET payload=zeroblob(?)", (33 * 1024 * 1024,))
        with self.assertRaises(PublicationError):
            self.store.get_snapshot("report-1")

    def test_existing_empty_corrupt_unrelated_database_is_not_initialized(self):
        self.store.close()
        for value in (b"", b"corrupt SQLite bytes", b"SQLite format 3\0" + b"\0" * 100):
            self.store.path.write_bytes(value)
            with self.subTest(value=value), self.assertRaises(PublicationError):
                LocalPublicationStore(self.root)
            self.assertEqual(self.store.path.read_bytes(), value)
        self.store.path.unlink()
        self.execute("CREATE TABLE unrelated(value TEXT)")
        before = self.store.path.read_bytes()
        with self.assertRaises(PublicationError):
            LocalPublicationStore(self.root)
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_symlink_hardlink_and_disappearing_database_are_rejected(self):
        original = self.root / "untouched.sqlite3"
        self.store.path.rename(original)
        self.store.path.symlink_to(original)
        with self.assertRaises(PublicationError):
            LocalPublicationStore(self.root)
        self.store.path.unlink()
        os.link(original, self.store.path)
        with self.assertRaises(PublicationError):
            LocalPublicationStore(self.root)
        self.store.path.unlink()
        with self.assertRaises(PublicationError):
            self.store.load()
        self.assertFalse(self.store.path.exists())

    def test_safe_errors_and_closed_store(self):
        with patch("trendradar.storage.publication.sqlite3.connect", side_effect=sqlite3.OperationalError(PRIVATE)):
            with self.assertRaises(PublicationError) as caught:
                self.store.load()
        self.assertNotIn(PRIVATE, str(caught.exception))
        self.store.close()
        self.store.close()
        with self.assertRaises(PublicationError):
            self.store.load()

    def test_strict_json_ids_and_bounds(self):
        for invalid in ([], None, {1: "key"}, {"v": float("nan")}, {"v": float("inf")},
                        {"v": (1, 2)}, {"v": b"bytes"}, {"v": "\ud800"}):
            with self.subTest(invalid=repr(invalid)), self.assertRaises(PublicationError):
                self.store.save(invalid, None)
        circular = {}
        circular["self"] = circular
        with self.assertRaises(PublicationError):
            self.store.save(circular, None)
        for invalid in ("../outside", "nested/id", "", "a" * 129, "x\n", None):
            with self.subTest(invalid=invalid), self.assertRaises(PublicationError):
                self.store.put_snapshot(invalid, {})
        small = LocalPublicationStore(self.root, max_document_bytes=10, max_snapshot_bytes=10)
        self.addCleanup(small.close)
        with self.assertRaises(PublicationError):
            small.save({"large": "long"}, None)
        with self.assertRaises(PublicationError):
            small.put_snapshot("one", {"large": "long"})


class RemotePublicationTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeS3()
        self.store = RemotePublicationStore(self.client, "offline-bucket")
        self.addCleanup(self.store.close)

    def test_roundtrip_create_only_cas_and_aba(self):
        self.assertEqual(self.store.load(), (None, None))
        document = {"published": {"count": 1}, "中文": True}
        first = self.store.save(document, None)
        self.assertEqual(self.client.calls[-1][1]["IfNoneMatch"], "*")
        self.assertEqual(self.store.load(), (document, first))
        with self.assertRaises(PublicationConflict):
            self.store.save({"count": 2}, None)
        second = self.store.save(document, first)
        self.assertEqual(self.client.calls[-1][1]["IfMatch"], first)
        self.assertNotEqual(first, second)
        with self.assertRaises(PublicationConflict):
            self.store.save({"count": 3}, first)
        self.assertEqual(self.store.load(), (document, second))
        self.assertTrue(all(body.closed for body in self.client.bodies))

    def test_snapshot_immutable_idempotent_and_fresh_process(self):
        version = self.store.save({"snapshot": "snapshot-1"}, None)
        self.store.put_snapshot("snapshot-1", {"frozen": PRIVATE})
        self.store.put_snapshot("snapshot-1", {"frozen": PRIVATE})
        with self.assertRaises(PublicationConflict):
            self.store.put_snapshot("snapshot-1", {"different": True})
        context = multiprocessing.get_context("spawn")
        output = context.Queue()
        process = context.Process(target=fresh_remote_reader, args=(self.client.objects, output))
        try:
            process.start()
            result = output.get(timeout=20)
            process.join(timeout=20)
            self.assertEqual(process.exitcode, 0)
            self.assertEqual(result, (({"snapshot": "snapshot-1"}, version), {"frozen": PRIVATE}))
        finally:
            if process.is_alive():
                process.terminate()
                process.join()
            output.close()

    def test_concurrent_remote_writers_cannot_overwrite_winner(self):
        version = self.store.save({"count": 0}, None)
        barrier = Barrier(2)

        def compete(value):
            store = RemotePublicationStore(self.client, "offline-bucket")
            barrier.wait(timeout=10)
            try:
                return store.save({"count": value}, version)
            except PublicationConflict:
                return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(compete, [1, 2]))
        self.assertEqual(sum(item is not None for item in results), 1)
        self.assertEqual(self.store.load()[1], next(item for item in results if item is not None))

    def test_only_object_absence_bootstraps(self):
        errors = [s3_error("AccessDenied", 403), s3_error("NoSuchBucket", 404),
                  s3_error("NoSuchKey", 500), s3_error("InternalError", 500),
                  TimeoutError(PRIVATE), PermissionError(PRIVATE)]
        for error in errors:
            self.client.get_error = error
            with self.subTest(error=type(error).__name__), self.assertRaises(PublicationError) as caught:
                self.store.load()
            self.assertNotIn(PRIVATE, str(caught.exception))
        self.client.get_error = s3_error("NoSuchKey", 404)
        self.assertEqual(self.store.load(), (None, None))
        with self.assertRaises(PublicationError):
            self.store.get_snapshot("not-present")

    def test_provider_errors_never_leak_or_trigger_unconditional_put(self):
        for error in (s3_error("NotImplemented", 501), s3_error("AccessDenied", 403),
                      s3_error("InvalidRequest", 400), TimeoutError(PRIVATE)):
            self.client.put_error = error
            before = len(self.client.calls)
            with self.assertRaises(PublicationError) as caught:
                self.store.save({"private": PRIVATE}, None)
            self.assertEqual(len(self.client.calls), before + 1)
            rendered = "".join(traceback.format_exception(caught.exception))
            self.assertNotIn(PRIVATE, rendered)
            self.assertEqual(self.client.calls[-1][1]["IfNoneMatch"], "*")
            self.assertEqual(self.client.objects, {})

    def test_ambiguous_write_is_error_even_if_committed_then_reload_reconciles(self):
        self.client.lose_put_response = True
        with self.assertRaises(PublicationError) as caught:
            self.store.save({"claim": "inflight"}, None)
        self.assertNotIsInstance(caught.exception, PublicationConflict)
        self.assertEqual(len([call for call in self.client.calls if call[0] == "put"]), 1)
        fresh = RemotePublicationStore(self.client, "offline-bucket")
        document, version = fresh.load()
        self.assertEqual(document, {"claim": "inflight"})
        self.assertTrue(version)
        self.client.lose_put_response = False
        with self.assertRaises(PublicationConflict):
            fresh.save({"claim": "different"}, None)

    def test_ambiguous_snapshot_can_only_be_reconciled_by_identical_contents(self):
        self.client.lose_put_response = True
        with self.assertRaises(PublicationError):
            self.store.put_snapshot("frozen", {"prepared": PRIVATE})
        self.client.lose_put_response = False
        self.store.put_snapshot("frozen", {"prepared": PRIVATE})
        with self.assertRaises(PublicationConflict):
            self.store.put_snapshot("frozen", {"prepared": "other"})

    def test_missing_success_version_never_returns_success(self):
        self.client.missing_put_etag = True
        with self.assertRaises(PublicationError):
            self.store.save({}, None)
        with self.assertRaises(PublicationError):
            self.store.put_snapshot("snapshot", {})

    def test_corrupt_manifest_and_snapshot_fail_closed(self):
        for invalid in (b"", b"{", b"null", b"[]", b'{"x":1,"x":2}', b'{"v":NaN}',
                        b'{"format":1}', b"\xff"):
            self.client.objects[self.store.manifest_key] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(PublicationError):
                self.store.load()
        self.client.objects.clear()
        self.store.save({"valid": True}, None)
        envelope = json.loads(self.client.objects[self.store.manifest_key])
        envelope["document"]["valid"] = False
        self.client.objects[self.store.manifest_key] = json.dumps(envelope).encode()
        with self.assertRaises(PublicationError):
            self.store.load()
        self.store.put_snapshot("snapshot", {"valid": True})
        key = self.store._snapshot_key("snapshot")
        envelope = json.loads(self.client.objects[key])
        envelope["snapshot_id"] = "different"
        self.client.objects[key] = json.dumps(envelope).encode()
        with self.assertRaises(PublicationError):
            self.store.get_snapshot("snapshot")

    def test_reads_bounded_and_bodies_closed_on_bad_length_truncation_and_read_error(self):
        for body, length in ((RecordingBody(b"{}"), 40 * 1024 * 1024),
                             (RecordingBody(b"{}"), 100),
                             (RecordingBody(b"123"), 2)):
            with self.subTest(length=length), patch.object(self.client, "get_object", return_value={
                    "Body": body, "ContentLength": length, "ETag": '"etag"'}):
                with self.assertRaises(PublicationError):
                    self.store.load()
                self.assertTrue(body.closed)
                self.assertTrue(all(size > 0 for size in body.sizes))
                if length > 32 * 1024 * 1024:
                    self.assertEqual(body.sizes, [])
        body = Mock()
        body.read.side_effect = TimeoutError(PRIVATE)
        with patch.object(self.client, "get_object", return_value={
                "Body": body, "ContentLength": 100, "ETag": '"etag"'}):
            with self.assertRaises(PublicationError):
                self.store.load()
        body.close.assert_called_once_with()

    def test_sdk_missing_conditional_members_fails_before_io(self):
        self.client.meta.service_model = SimpleNamespace(operation_model=lambda name:
            SimpleNamespace(input_shape=SimpleNamespace(members={"Body": None})))
        with self.assertRaisesRegex(PublicationError, "conditional PutObject"):
            RemotePublicationStore(self.client, "offline-bucket")
        self.assertEqual(self.client.calls, [])

    def test_closed_store_does_not_close_shared_client(self):
        self.client.close = Mock()
        self.store.close()
        self.store.close()
        self.client.close.assert_not_called()
        with self.assertRaises(PublicationError):
            self.store.load()

    def test_remote_small_limits_and_invalid_tokens(self):
        small = RemotePublicationStore(self.client, "offline-bucket", max_document_bytes=10,
                                       max_snapshot_bytes=10)
        with self.assertRaises(PublicationError):
            small.save({"large": "long"}, None)
        for token in ("*", "", 4, "bad\n", "bad token", '"one",*', '"one","two"', 'W/"one"'):
            with self.subTest(token=token), self.assertRaises(PublicationError):
                small.save({}, token)
        self.assertEqual(self.client.calls, [])


class PublicationManagerTests(unittest.TestCase):
    def test_lazy_local_reuses_actual_backend_directory_and_cleanup(self):
        with tempfile.TemporaryDirectory() as root:
            manager = StorageManager(backend_type="local", data_dir="must-not-be-used")
            backend = Mock(backend_name="local", data_dir=root)
            manager._backend = backend
            self.assertIsNone(manager._publication_store)
            store = manager.get_publication_store()
            self.assertIs(store, manager.get_publication_store())
            self.assertEqual(store.path.parent, Path(root) / "meta")
            version = store.save({"saved": True}, None)
            manager.cleanup()
            self.assertIsNone(manager._publication_store)
            backend.cleanup.assert_called_once_with()
            with self.assertRaises(PublicationError):
                store.load()
            self.assertEqual(manager.get_publication_store().load(), ({"saved": True}, version))
            manager.cleanup()

    def test_existing_remote_backend_client_and_bucket_are_reused(self):
        client = FakeS3()
        manager = StorageManager(backend_type="remote")
        manager._backend = Mock(backend_name="remote", s3_client=client, bucket_name="offline-bucket")
        store = manager.get_publication_store()
        self.assertIs(store.s3_client, client)
        self.assertEqual(store.bucket_name, "offline-bucket")
        manager.cleanup()

    def test_remote_failed_backend_never_constructs_local_or_leaks_exception(self):
        manager = StorageManager(backend_type="remote")
        output = io.StringIO()
        with patch("trendradar.storage.remote.RemoteStorageBackend", side_effect=RuntimeError(PRIVATE)), \
                patch("trendradar.storage.local.LocalStorageBackend") as local, redirect_stdout(output):
            with self.assertRaises(PublicationError) as caught:
                manager.get_publication_store()
        local.assert_not_called()
        self.assertIsNone(manager._publication_store)
        self.assertNotIn(PRIVATE, output.getvalue() + str(caught.exception))

    def test_previous_news_fallback_cannot_create_local_publication_state(self):
        manager = StorageManager(backend_type="remote")
        manager._backend = Mock(backend_name="local")
        with patch("trendradar.storage.publication.LocalPublicationStore") as local:
            with self.assertRaises(PublicationError):
                manager.get_publication_store()
        local.assert_not_called()

    def test_auto_remote_selection_also_rejects_prior_fallback(self):
        manager = StorageManager(backend_type="auto", remote_config={
            "bucket_name": "offline", "access_key_id": "fake", "secret_access_key": "fake",
            "endpoint_url": "https://example.invalid"})
        manager._backend = Mock(backend_name="local")
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            with self.assertRaises(PublicationError):
                manager.get_publication_store()


class PublicationSDKTests(unittest.TestCase):
    """Actual botocore input/response validation, with zero network connections."""

    def test_real_sdk_fails_closed_or_accepts_both_conditional_inputs(self):
        import boto3
        from botocore.stub import ANY, Stubber

        client = boto3.client("s3", region_name="us-east-1", aws_access_key_id="offline-key",
                              aws_secret_access_key="offline-secret", endpoint_url="https://example.invalid")
        self.addCleanup(client.close)
        members = client.meta.service_model.operation_model("PutObject").input_shape.members
        with Stubber(client) as stubber:
            if not {"IfMatch", "IfNoneMatch"}.issubset(members):
                with self.assertRaisesRegex(PublicationError, "conditional PutObject"):
                    RemotePublicationStore(client, "offline-bucket")
                return
            store = RemotePublicationStore(client, "offline-bucket")
            base = {"Bucket": "offline-bucket", "Key": store.manifest_key,
                    "Body": ANY, "ContentType": "application/json"}
            stubber.add_response("put_object", {"ETag": '"first"'}, {**base, "IfNoneMatch": "*"})
            stubber.add_response("put_object", {"ETag": '"second"'}, {**base, "IfMatch": '"first"'})
            stubber.add_client_error("put_object", service_error_code="PreconditionFailed",
                                     service_message=PRIVATE, http_status_code=412,
                                     expected_params={**base, "IfMatch": '"first"'})
            self.assertEqual(store.save({}, None), '"first"')
            self.assertEqual(store.save({"changed": True}, '"first"'), '"second"')
            with self.assertRaises(PublicationConflict):
                store.save({}, '"first"')
            snapshot = {**base, "Key": store._snapshot_key("offline"), "IfNoneMatch": "*"}
            stubber.add_response("put_object", {"ETag": '"snapshot"'}, snapshot)
            store.put_snapshot("offline", {})
            stubber.assert_no_pending_responses()


if __name__ == "__main__":
    unittest.main()
