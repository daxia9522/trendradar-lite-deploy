"""Offline synthetic SQLite / mock S3 verification for the optional backup.

Every in-process test forbids network connections and DNS. Subprocess tests
only invoke --help or disabled/credential-free dry runs with a clean env.
"""
import builtins
import contextlib
import importlib.util
import io
import os
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import traceback
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

from deploy import r2_backup as backup
from deploy.backup_settings import BackupConfigError

ROOT = Path(__file__).resolve().parents[1]


class ServiceError(Exception):
    def __init__(self, message, status=503, code="ServiceUnavailable"):
        super().__init__(message)
        self.response = {"ResponseMetadata": {"HTTPStatusCode": status}, "Error": {"Code": code}}


class FakeS3:
    def __init__(self):
        self.uploads = []
        self.objects = {}
        self.failures = []
        self.head_failures = []
        self.remote_delta = 0
        self.on_upload = None

    def upload_file(self, filename, bucket, key, ExtraArgs=None):
        path = Path(filename)
        payload = path.read_bytes()
        self.uploads.append((path, bucket, key, payload, ExtraArgs))
        if self.on_upload:
            self.on_upload(path)
        if self.failures:
            raise self.failures.pop(0)
        self.objects[(bucket, key)] = payload

    def head_object(self, Bucket, Key):
        if self.head_failures:
            raise self.head_failures.pop(0)
        return {"ContentLength": len(self.objects[(Bucket, Key)]) + self.remote_delta}


class R2BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "output"
        self.output.mkdir()
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        for patcher in (
            mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")),
            mock.patch("socket.create_connection", side_effect=AssertionError("network forbidden")),
            mock.patch("socket.getaddrinfo", side_effect=AssertionError("DNS forbidden")),
            mock.patch.dict(os.environ, {}, clear=True),
            contextlib.redirect_stdout(self.stdout), contextlib.redirect_stderr(self.stderr),
        ):
            patcher.__enter__()
            self.addCleanup(patcher.__exit__, None, None, None)

    def credentials(self, **values):
        return {
            "S3_BUCKET_NAME": "PRIVATE-BUCKET", "S3_ACCESS_KEY_ID": "PRIVATE-ACCESS",
            "S3_SECRET_ACCESS_KEY": "PRIVATE-SECRET", "S3_ENDPOINT_URL": "https://private-endpoint.invalid",
            **values,
        }

    def enabled(self, **values):
        return self.credentials(R2_BACKUP_ENABLED="true", STORAGE_BACKEND="local", **values)

    def database(self, day="2026-09-28", kind="news", data_dir=None):
        path = (data_dir or self.output) / kind / f"{day}.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE records (value TEXT)")
            connection.execute("INSERT INTO records VALUES ('committed')")
        return path

    def config(self, text="storage:\n  local:\n    data_dir: output\n", path=None):
        path = path or self.root / "config/config.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def logs(self):
        return self.stdout.getvalue() + self.stderr.getvalue()

    def test_disabled_main_is_zero_without_sdk_yaml_config_or_database_access(self):
        real_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name.split(".")[0] in {"boto3", "botocore", "yaml", "trendradar"}:
                raise AssertionError("disabled import forbidden")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=guarded_import), \
                mock.patch.object(backup, "configured_data_dir", side_effect=AssertionError("config read")), \
                mock.patch.object(backup, "local_database_targets", side_effect=AssertionError("database access")), \
                mock.patch.object(backup, "backup_lock", side_effect=AssertionError("lock")):
            self.assertEqual(backup.main(["--configured"]), 0)
            self.assertEqual(backup.main(["--configured", "--dry-run"]), 0)
        self.assertIn("未启用", self.logs())
        self.assertFalse((self.output / backup.LOCK_NAME).exists())

    def test_enabled_missing_credentials_fails_before_config_and_sdk(self):
        with mock.patch.dict(os.environ, {"R2_BACKUP_ENABLED": "1", "STORAGE_BACKEND": "local"}), \
                mock.patch.object(backup, "configured_data_dir") as data, \
                mock.patch.object(backup, "create_s3_client") as client:
            self.assertEqual(backup.main(["--configured"]), 2)
            self.assertEqual(backup.main(["--configured", "--dry-run"]), 2)
        data.assert_not_called()
        client.assert_not_called()

    def test_configured_env_selects_timezone_days_and_custom_yaml_data_dir(self):
        self.config("storage:\n  local:\n    data_dir: alternate-data\n")
        alternate = self.root / "alternate-data"
        self.database(day="2026-01-01", data_dir=alternate)
        self.database(day="2025-12-31", data_dir=alternate)
        self.database(day="2025-12-30", data_dir=alternate)
        client = FakeS3()
        with mock.patch.dict(os.environ, self.enabled(TIMEZONE="UTC", R2_BACKUP_LOOKBACK_DAYS="3")), \
                mock.patch.object(backup, "PROJECT_ROOT", self.root), \
                mock.patch.object(backup, "datetime") as clock, \
                mock.patch.object(backup, "create_s3_client", return_value=(client, "PRIVATE-BUCKET")):
            clock.now.return_value = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
            self.assertEqual(backup.main(["--configured"]), 0)
            self.assertEqual(str(clock.now.call_args.args[0]), "UTC")
        self.assertEqual([item[2] for item in client.uploads], ["news/2026-01-01.db", "news/2025-12-31.db", "news/2025-12-30.db"])
        self.assertTrue((alternate / backup.LOCK_NAME).exists())
        self.assertFalse((self.output / backup.LOCK_NAME).exists())

    def test_configured_dry_run_validates_and_lists_without_sdk_lock_snapshot(self):
        self.config()
        original = self.database()
        before = original.read_bytes()
        with mock.patch.dict(os.environ, self.enabled()), \
                mock.patch.object(backup, "PROJECT_ROOT", self.root), \
                mock.patch.object(backup, "dates_to_sync", return_value=["2026-09-28"]), \
                mock.patch.object(backup, "create_s3_client", side_effect=AssertionError("client")), \
                mock.patch.object(backup, "sqlite_snapshot", side_effect=AssertionError("snapshot")), \
                mock.patch.object(backup, "backup_lock", side_effect=AssertionError("lock")):
            self.assertEqual(backup.main(["--configured", "--dry-run"]), 0)
        self.assertIn("news/2026-09-28.db", self.logs())
        self.assertEqual(before, original.read_bytes())
        self.assertFalse((self.output / backup.LOCK_NAME).exists())

    def test_configured_rejects_cli_overrides(self):
        with mock.patch.dict(os.environ, self.enabled()):
            for args in (["--data-dir", "elsewhere"], ["--date", "2026-09-28"], ["--lookback-days", "5"], ["--timezone", "UTC"]):
                with self.subTest(args=args):
                    self.assertEqual(backup.main(["--configured", *args]), 2)

    def test_manual_cli_still_uploads_without_enabled_or_backend(self):
        self.database()
        client = FakeS3()
        with mock.patch.dict(os.environ, self.credentials()), \
                mock.patch.object(backup, "create_s3_client", return_value=(client, "PRIVATE-BUCKET")):
            result = backup.main(["--data-dir", str(self.output), "--date", "2026-09-28", "--lookback-days", "1"])
        self.assertEqual(result, 0)
        self.assertEqual(len(client.uploads), 1)
        self.assertNotIn("PRIVATE-BUCKET", self.logs())
        self.assertNotIn("news/2026-09-28.db", self.logs())

    def test_legacy_dry_run_no_credentials_needed(self):
        self.database()
        with mock.patch.object(backup, "create_s3_client", side_effect=AssertionError("SDK")):
            self.assertEqual(backup.main(["--data-dir", str(self.output), "--date", "2026-09-28", "--dry-run"]), 0)
        self.assertFalse((self.output / backup.LOCK_NAME).exists())

    def test_no_targets_nonzero_no_lock_no_sdk(self):
        with mock.patch.object(backup, "create_s3_client") as client:
            self.assertEqual(backup.sync_databases(self.output, ["2026-09-28"]), 1)
        client.assert_not_called()
        self.assertFalse((self.output / backup.LOCK_NAME).exists())

    def test_targets_only_requested_dates_news_and_rss(self):
        self.database()
        self.database(kind="rss")
        self.database(kind="other")
        self.database(day="2026-09-27")
        found = backup.local_database_targets(self.output, ["2026-09-28"])
        self.assertEqual([target[2] for target in found], ["news/2026-09-28.db", "rss/2026-09-28.db"])
        with self.assertRaises(Exception):
            backup.local_database_targets(self.output, ["../../bad"])

    def test_dates_cross_year_leap_day_and_default_window(self):
        self.assertEqual(backup.dates_to_sync(date(2026, 1, 1), "UTC", 2), ["2026-01-01", "2025-12-31"])
        self.assertEqual(backup.dates_to_sync(date(2024, 3, 1), "UTC", 3), ["2024-03-01", "2024-02-29", "2024-02-28"])
        for days in (0, -1, 3661):
            with self.assertRaises(ValueError):
                backup.dates_to_sync(date(2026, 1, 1), "UTC", days)
        self.assertEqual(backup.DEFAULT_LOOKBACK_DAYS, 2)
        self.assertEqual(backup.DEFAULT_UPLOAD_ATTEMPTS, 3)

    def test_timezone_cross_midnight(self):
        instant = datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc)
        with mock.patch.object(backup, "datetime") as clock:
            clock.now.side_effect = lambda zone: instant.astimezone(zone)
            self.assertEqual(backup.dates_to_sync(None, "America/Los_Angeles", 2), ["2025-12-31", "2025-12-30"])
            self.assertEqual(backup.dates_to_sync(None, "Asia/Shanghai", 2), ["2026-01-01", "2025-12-31"])

    def test_wal_snapshot_includes_committed_excludes_uncommitted_readonly_private(self):
        path = self.output / "news/2026-09-28.db"
        path.parent.mkdir()
        writer = sqlite3.connect(path)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE records (value TEXT)")
        writer.execute("INSERT INTO records VALUES ('committed-wal')")
        writer.commit()
        wal_path = Path(str(path) + "-wal")
        before_db, before_wal = path.read_bytes(), wal_path.read_bytes()
        writer.execute("INSERT INTO records VALUES ('uncommitted')")
        real_connect = sqlite3.connect
        with mock.patch.object(backup.sqlite3, "connect", wraps=real_connect) as connect:
            with backup.sqlite_snapshot(path) as snapshot:
                self.assertEqual(stat.S_IMODE(snapshot.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(snapshot.parent.stat().st_mode), 0o700)
                self.assertFalse(Path(str(snapshot) + "-wal").exists())
                with contextlib.closing(real_connect(snapshot)) as saved:
                    self.assertEqual(saved.execute("SELECT value FROM records").fetchall(), [("committed-wal",)])
                    self.assertEqual(saved.execute("PRAGMA journal_mode").fetchone()[0], "delete")
                self.assertEqual(path.read_bytes(), before_db)
                self.assertEqual(wal_path.read_bytes(), before_wal)
                first_call = connect.call_args_list[0]
                self.assertIn("mode=ro", first_call.args[0])
                self.assertTrue(first_call.kwargs["uri"])
            self.assertFalse(snapshot.exists())
            self.assertFalse(snapshot.parent.exists())

    def test_snapshot_cleaned_on_consumer_exception(self):
        path = self.database()
        with self.assertRaises(RuntimeError):
            with backup.sqlite_snapshot(path) as snapshot:
                raise RuntimeError("consumer failure")
        self.assertFalse(snapshot.parent.exists())

    def test_snapshot_timeout_prevents_upload(self):
        self.database()
        client = FakeS3()
        with mock.patch.object(backup.time, "monotonic", side_effect=[0, 31]):
            self.assertEqual(backup.sync_databases(self.output, ["2026-09-28"], client=client, bucket="PRIVATE"), 1)
        self.assertEqual(client.uploads, [])
        self.assertIn("TimeoutError", self.logs())

    def test_snapshot_quotes_special_characters_in_local_uri(self):
        path = self.database(data_dir=self.root / "data ?# with spaces")
        with backup.sqlite_snapshot(path) as snapshot:
            with contextlib.closing(sqlite3.connect(snapshot)) as connection:
                self.assertEqual(connection.execute("SELECT value FROM records").fetchone()[0], "committed")

    def test_quick_check_does_not_echo_corrupt_row(self):
        path = self.database()
        connection = mock.Mock()
        connection.execute.return_value.fetchone.return_value = ("PRIVATE-CORRUPT-ROW",)
        with mock.patch.object(backup.sqlite3, "connect", return_value=connection):
            with self.assertRaises(RuntimeError) as raised:
                backup.sqlite_quick_check(path)
        self.assertEqual(str(raised.exception), "SQLite quick_check failed")
        connection.close.assert_called_once()

    def test_zero_and_corrupt_database_rejected_before_upload(self):
        zero = self.output / "news/2026-09-28.db"
        zero.parent.mkdir()
        zero.touch()
        corrupt = self.output / "rss/2026-09-28.db"
        corrupt.parent.mkdir()
        corrupt.write_bytes(b"not sqlite; PRIVATE-ROW")
        client = FakeS3()
        self.assertEqual(backup.sync_databases(self.output, ["2026-09-28"], client=client, bucket="PRIVATE-BUCKET"), 1)
        self.assertEqual(client.uploads, [])
        self.assertNotIn("PRIVATE-ROW", self.logs())
        self.assertIn("ValueError", self.logs())
        self.assertIn("DatabaseError", self.logs())

    def test_quick_check_failure_prevents_upload_and_cleans_snapshot(self):
        self.database()
        client = FakeS3()
        snapshots = []

        def fail(path):
            snapshots.append(path)
            raise RuntimeError("PRIVATE-ROW")

        with mock.patch.object(backup, "sqlite_quick_check", side_effect=fail):
            self.assertEqual(backup.sync_databases(self.output, ["2026-09-28"], client=client, bucket="PRIVATE-BUCKET"), 1)
        self.assertFalse(snapshots[0].parent.exists())
        self.assertEqual(client.uploads, [])
        self.assertNotIn("PRIVATE-ROW", self.logs())

    def test_retry_uses_same_private_snapshot_and_waits_1_2(self):
        path = self.database()
        client = FakeS3()
        client.failures = [ServiceError("PRIVATE URL"), ServiceError("PRIVATE URL")]
        sleeper = mock.Mock()

        def alter_live(snapshot):
            self.assertNotEqual(path, snapshot)
            self.assertEqual(stat.S_IMODE(snapshot.stat().st_mode), 0o600)
            with sqlite3.connect(path) as writer:
                writer.execute("INSERT INTO records VALUES ('new-live-row')")

        client.on_upload = alter_live
        self.assertEqual(backup.sync_databases(self.output, ["2026-09-28"], client=client, bucket="PRIVATE-BUCKET", sleep_func=sleeper), 0)
        self.assertEqual(len(client.uploads), 3)
        self.assertEqual(sleeper.call_args_list, [mock.call(1), mock.call(2)])
        self.assertEqual(len({item[0] for item in client.uploads}), 1)
        self.assertEqual(len({item[3] for item in client.uploads}), 1)
        self.assertFalse(client.uploads[0][0].exists())
        self.assertIn("ServiceError HTTP 503", self.logs())
        self.assertNotIn("PRIVATE", self.logs())

    def test_head_size_mismatch_retries_and_fails(self):
        self.database()
        client = FakeS3()
        client.remote_delta = 1
        sleeper = mock.Mock()
        self.assertEqual(backup.sync_databases(self.output, ["2026-09-28"], client=client, bucket="PRIVATE", sleep_func=sleeper), 1)
        self.assertEqual(len(client.uploads), 3)
        self.assertEqual(sleeper.call_args_list, [mock.call(1), mock.call(2)])
        self.assertNotIn("PRIVATE", self.logs())

    def test_head_transient_failure_retries(self):
        self.database()
        client = FakeS3()
        client.head_failures = [ServiceError("PRIVATE-HEAD", status=503)]
        self.assertEqual(backup.sync_databases(self.output, ["2026-09-28"], client=client, bucket="PRIVATE", sleep_func=mock.Mock()), 0)
        self.assertEqual(len(client.uploads), 2)
        self.assertNotIn("PRIVATE", self.logs())

    def test_authentication_direct_wrapped_and_head_do_not_retry(self):
        path = self.database()
        for where in ("upload", "wrapped", "head"):
            with self.subTest(where=where):
                client = FakeS3()
                denied = ServiceError("PRIVATE-ACCESS PRIVATE-BUCKET private-endpoint.invalid", status=403, code="AccessDenied")
                if where == "head":
                    client.head_failures = [denied]
                elif where == "wrapped":
                    wrapped = RuntimeError("PRIVATE-WRAPPED")
                    wrapped.__cause__ = denied
                    client.failures = [wrapped]
                else:
                    client.failures = [denied]
                sleeper = mock.Mock()
                with self.assertRaises(backup.BackupUploadError) as raised:
                    backup.upload_target(client, "PRIVATE-BUCKET", path, "PRIVATE-KEY", 3, sleeper)
                self.assertEqual(len(client.uploads), 1)
                sleeper.assert_not_called()
                rendered = "".join(traceback.format_exception(raised.exception))
                self.assertNotIn("PRIVATE", rendered)
                self.assertNotIn("private-endpoint", rendered)
                self.assertIsNone(raised.exception.__cause__)
                self.assertIsNone(raised.exception.__context__)
                self.assertIn("HTTP 403", str(raised.exception))

    def test_failure_logs_class_only_never_raw_exception_keys_or_endpoint(self):
        self.database()
        self.database(kind="rss")
        client = FakeS3()
        client.failures = [RuntimeError("PRIVATE-SECRET https://private-endpoint.invalid/PRIVATE-BUCKET/PRIVATE-KEY")]
        self.assertEqual(backup.sync_databases(self.output, ["2026-09-28"], client=client, bucket="PRIVATE-BUCKET", attempts=1), 1)
        self.assertEqual(len(client.uploads), 2)
        self.assertIn("RuntimeError", self.logs())
        for secret in ("PRIVATE", "private-endpoint", "news/2026", "rss/2026"):
            self.assertNotIn(secret, self.logs())

    def test_sdk_creation_failure_is_sanitized_at_cli_boundary(self):
        self.database()
        with mock.patch.object(backup, "create_s3_client", side_effect=ServiceError("PRIVATE-SECRET", status=403)):
            self.assertEqual(backup.main(["--data-dir", str(self.output), "--date", "2026-09-28"]), 2)
        self.assertIn("ServiceError HTTP 403", self.logs())
        self.assertNotIn("PRIVATE", self.logs())
        self.assertNotIn("Traceback", self.logs())

    def test_lock_blocks_nested_upload_before_client_or_snapshot(self):
        self.database()
        with backup.backup_lock(self.output):
            with mock.patch.object(backup, "create_s3_client") as client, \
                    mock.patch.object(backup, "sqlite_snapshot") as snapshot, \
                    self.assertRaises(backup.BackupBusyError):
                backup.sync_databases(self.output, ["2026-09-28"])
            client.assert_not_called()
            snapshot.assert_not_called()
        self.assertEqual(stat.S_IMODE((self.output / backup.LOCK_NAME).stat().st_mode), 0o600)
        # Persistent inode is intentional: unlinking permits multiple lock owners.
        inode = (self.output / backup.LOCK_NAME).stat().st_ino
        with backup.backup_lock(self.output):
            self.assertEqual((self.output / backup.LOCK_NAME).stat().st_ino, inode)

    def test_lock_resolves_directory_alias_and_releases_after_failure(self):
        self.database()
        alias = self.root / "alias"
        alias.symlink_to(self.output, target_is_directory=True)
        with self.assertRaises(RuntimeError):
            with backup.backup_lock(self.output):
                with self.assertRaises(backup.BackupBusyError):
                    with backup.backup_lock(alias):
                        self.fail("second lock acquired")
                raise RuntimeError("stop")
        with backup.backup_lock(alias):
            pass

    def test_lock_blocks_a_second_process_nonblocking(self):
        self.database()
        with backup.backup_lock(self.output):
            result = subprocess.run(
                [sys.executable, str(ROOT / "deploy/r2_backup.py"),
                 "--data-dir", str(self.output), "--date", "2026-09-28"],
                cwd=self.root, env={"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"},
                capture_output=True, text=True, timeout=5,
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("BackupBusyError", result.stderr)
        self.assertNotIn("credentials", result.stderr)

    def test_lock_refuses_symlink_or_hardlink_lock_file(self):
        target = self.root / "must-not-change"
        target.write_text("private unrelated file")
        lock = self.output / backup.LOCK_NAME
        lock.symlink_to(target)
        with self.assertRaises(OSError):
            with backup.backup_lock(self.output):
                self.fail("symlink lock acquired")
        lock.unlink()
        os.link(target, lock)
        with self.assertRaises(RuntimeError):
            with backup.backup_lock(self.output):
                self.fail("hardlink lock acquired")
        self.assertEqual(target.read_text(), "private unrelated file")

    def test_dry_run_does_not_take_existing_lock(self):
        self.database()
        with backup.backup_lock(self.output):
            self.assertEqual(backup.sync_databases(self.output, ["2026-09-28"], dry_run=True), 0)

    def test_create_client_explicit_env_region_and_sdk_retry_limit(self):
        boto = mock.Mock()
        config_class = mock.Mock()
        config_module = mock.Mock(Config=config_class)
        with mock.patch.dict(sys.modules, {"boto3": boto, "botocore.config": config_module}), \
                mock.patch.dict(os.environ, self.credentials(S3_BUCKET_NAME="ambient-must-not-win")):
            client, bucket = backup.create_s3_client(self.credentials())
        self.assertIs(client, boto.client.return_value)
        self.assertEqual(bucket, "PRIVATE-BUCKET")
        self.assertEqual(boto.client.call_args.kwargs["region_name"], "auto")
        self.assertEqual(boto.client.call_args.kwargs["aws_secret_access_key"], "PRIVATE-SECRET")
        self.assertEqual(config_class.call_args.kwargs["retries"]["total_max_attempts"], 1)

    def test_empty_explicit_environment_does_not_fall_back_to_process_secrets(self):
        with mock.patch.dict(os.environ, self.credentials()):
            with self.assertRaises(BackupConfigError):
                backup.create_s3_client({})

    def test_credentials_checked_before_importing_boto3(self):
        real_import = builtins.__import__

        def guarded(name, *args, **kwargs):
            if name in {"boto3", "botocore.config"}:
                raise AssertionError("unexpected SDK import")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=guarded), self.assertRaises(BackupConfigError):
            backup.create_s3_client({})

    def test_data_dir_default_custom_absolute_and_config_path(self):
        for text, expected in (
            ("{}\n", self.output),
            ("storage:\n  local:\n    data_dir: other-output\n", self.root / "other-output"),
            (f"storage:\n  local:\n    data_dir: {self.root}/absolute-output\n", self.root / "absolute-output"),
        ):
            self.config(text)
            self.assertEqual(backup.configured_data_dir({}, self.root), expected)
        custom = self.config("storage:\n  local:\n    data_dir: alternate\n", self.root / "custom.yaml")
        for config_value in ("custom.yaml", str(custom)):
            self.assertEqual(backup.configured_data_dir({"CONFIG_PATH": config_value}, self.root), self.root / "alternate")

    def test_data_dir_configuration_missing_invalid_unsafe_yaml_fails_closed(self):
        with self.assertRaises(BackupConfigError):
            backup.configured_data_dir({}, self.root)
        for text in (
            "storage: [bad]\n", "[]\n", "storage:\n  local: [bad]\n",
            "storage:\n  local:\n    data_dir: null\n", "storage:\n  local:\n    data_dir: 42\n",
            "storage:\n  local:\n    data_dir: ''\n", "PRIVATE-INVALID: [\n",
            "!!python/object/apply:os.system ['PRIVATE-command']\n",
        ):
            self.config(text)
            with self.subTest(text=text), self.assertRaises(BackupConfigError) as raised:
                backup.configured_data_dir({}, self.root)
            self.assertNotIn("PRIVATE", str(raised.exception))

    def test_docker_requires_fixed_persistent_volume_directory(self):
        self.config()
        with self.assertRaises(BackupConfigError) as raised:
            backup.configured_data_dir({"DOCKER_CONTAINER": "true"}, self.root)
        self.assertEqual(raised.exception.code, "docker_data_dir")
        self.config("storage:\n  local:\n    data_dir: /app/output\n")
        self.assertEqual(backup.configured_data_dir({"DOCKER_CONTAINER": "true"}, self.root), Path("/app/output"))

    def test_compatibility_cli_help_and_offline_execution_from_other_cwd(self):
        self.database()
        env = {"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"}
        for script in (ROOT / "deploy/r2_backup.py",):
            with self.subTest(script=script):
                result = subprocess.run([sys.executable, str(script), "--help"], cwd=self.root, env=env, capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                for option in ("--configured", "--data-dir", "--date", "--lookback-days", "--timezone", "--attempts", "--dry-run"):
                    self.assertIn(option, result.stdout)
                result = subprocess.run([sys.executable, str(script), "--configured"], cwd=self.root, env=env, capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                result = subprocess.run([sys.executable, str(script), "--data-dir", str(self.output), "--date", "2026-09-28", "--dry-run"], cwd=self.root, env=env, capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.output / backup.LOCK_NAME).exists())

    def test_invalid_attempts_no_client_or_lock(self):
        self.database()
        with mock.patch.object(backup, "create_s3_client") as client, self.assertRaises(ValueError):
            backup.sync_databases(self.output, ["2026-09-28"], attempts=0)
        client.assert_not_called()
        self.assertFalse((self.output / backup.LOCK_NAME).exists())


if __name__ == "__main__":
    unittest.main()
