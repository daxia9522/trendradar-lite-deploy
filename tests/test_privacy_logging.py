import io
import smtplib
import tempfile
import unittest
from datetime import datetime
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from trendradar.notification.senders import send_to_email
from trendradar.report.helpers import safe_report_url
from trendradar.storage import remote as remote_storage


class PrivacyLoggingTests(unittest.TestCase):
    def test_email_rejects_empty_or_invalid_recipients(self):
        self.assertFalse(send_to_email("sender@example.invalid", "password", "", "daily", "missing.html"))
        self.assertFalse(send_to_email("sender@example.invalid", "password", "bad-address", "daily", "missing.html"))

    def test_email_passes_explicit_envelope_recipients(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            report = Path(temp_dir) / "report.html"
            report.write_text("<html>report</html>", encoding="utf-8")
            smtp = Mock()
            smtp.send_message.return_value = {}

            with patch("trendradar.notification.senders.smtplib.SMTP_SSL", return_value=smtp):
                sent = send_to_email(
                    "sender@example.invalid",
                    "test-password",
                    "one@example.invalid, two@example.invalid",
                    "daily",
                    str(report),
                    custom_smtp_server="smtp.example.invalid",
                    custom_smtp_port=465,
                )

        self.assertTrue(sent)
        self.assertEqual(
            smtp.send_message.call_args.kwargs["to_addrs"],
            ["one@example.invalid", "two@example.invalid"],
        )

    def test_report_url_allows_http_https_only(self):
        self.assertEqual(safe_report_url(" https://example.com/a?x=1 "), "https://example.com/a?x=1")
        self.assertEqual(safe_report_url("http://example.com"), "http://example.com")
        self.assertIsNone(safe_report_url("javascript:alert(1)"))
        self.assertIsNone(safe_report_url("//example.com/path"))

    def test_email_success_log_omits_addresses_and_server(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            report = Path(temp_dir) / "report.html"
            report.write_text("<html>report</html>", encoding="utf-8")
            smtp = Mock()
            smtp.send_message.return_value = {}
            output = io.StringIO()

            with patch("trendradar.notification.senders.smtplib.SMTP_SSL", return_value=smtp):
                with redirect_stdout(output):
                    sent = send_to_email(
                        "sender@example.invalid",
                        "test-password",
                        "recipient@example.invalid",
                        "daily",
                        str(report),
                        custom_smtp_server="smtp.example.invalid",
                        custom_smtp_port=465,
                    )

        self.assertTrue(sent)
        self.assertIn("邮件发送成功 [daily]", output.getvalue())
        for private_value in (
            "sender@example.invalid",
            "recipient@example.invalid",
            "smtp.example.invalid",
            str(report),
        ):
            self.assertNotIn(private_value, output.getvalue())

    def test_email_error_log_omits_smtp_exception_details(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            report = Path(temp_dir) / "report.html"
            report.write_text("<html>report</html>", encoding="utf-8")
            smtp = Mock()
            smtp.login.side_effect = smtplib.SMTPAuthenticationError(
                535,
                b"authentication failed for sender@example.invalid",
            )
            output = io.StringIO()

            with patch("trendradar.notification.senders.smtplib.SMTP_SSL", return_value=smtp):
                # 认证错误不重试；替换等待以确保测试不会实际退避。
                with patch("trendradar.notification.senders.time.sleep"):
                    with redirect_stdout(output):
                        sent = send_to_email(
                            "sender@example.invalid",
                            "test-password",
                            "recipient@example.invalid",
                            "daily",
                            str(report),
                            custom_smtp_server="smtp.example.invalid",
                            custom_smtp_port=465,
                        )

        self.assertFalse(sent)
        self.assertIn("认证错误", output.getvalue())
        self.assertNotIn("sender@example.invalid", output.getvalue())

    def test_remote_storage_log_omits_bucket_and_endpoint(self):
        output = io.StringIO()
        boto3 = Mock()

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(remote_storage, "HAS_BOTO3", True):
                with patch.object(remote_storage, "boto3", boto3):
                    with patch.object(remote_storage, "BotoConfig", Mock(return_value=object())):
                        with redirect_stdout(output):
                            remote_storage.RemoteStorageBackend(
                                bucket_name="private-bucket-name",
                                access_key_id="test-access-id",
                                secret_access_key="test-secret-value",
                                endpoint_url="https://storage.example.invalid",
                                temp_dir=temp_dir,
                            )

        self.assertIn("[远程存储] 初始化完成", output.getvalue())
        for private_value in (
            "private-bucket-name",
            "test-access-id",
            "test-secret-value",
            "storage.example.invalid",
        ):
            self.assertNotIn(private_value, output.getvalue())


class UnprintableStorageError(Exception):
    def __str__(self):
        raise AssertionError("must not format remote exception bodies")

    __repr__ = __str__


class RemoteFailureLoggingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="private-storage-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.backend = object.__new__(remote_storage.RemoteStorageBackend)
        self.backend.bucket_name = "private-bucket-name"
        self.backend.endpoint_url = "https://private-storage.invalid"
        self.backend.timezone = "UTC"
        self.backend.temp_dir = self.root
        self.backend.enable_txt = True
        self.backend.enable_html = True
        self.backend._db_connections = {}
        self.backend._downloaded_files = []
        self.backend._batch_mode = False
        self.backend.s3_client = Mock()
        self.key = "private-object-key"
        self.local = self.root / "private-local-path"
        self.backend._get_remote_db_key = Mock(return_value=self.key)
        self.backend._get_local_db_path = Mock(return_value=self.local)
        self.backend._get_configured_time = Mock(return_value=datetime(2026, 9, 28))
        self.error = UnprintableStorageError("private-body private-bucket-name")

    def capture(self, operation):
        output = io.StringIO()
        with redirect_stdout(output):
            result = operation()
        text = output.getvalue()
        for marker in ("private-body", "private-bucket-name", "private-storage.invalid", self.key, str(self.root), "private-local-path", "private-code"):
            self.assertNotIn(marker, text)
        return result, text

    def client_error(self, code="AccessDenied", status=403):
        return remote_storage.ClientError({
            "Error": {"Code": code, "Message": "private-body private-bucket-name"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        }, "HeadObject")

    def test_head_client_error_omits_key_body_and_arbitrary_provider_code(self):
        self.backend.s3_client.head_object.side_effect = self.client_error("private-code")
        def check():
            with self.assertRaises(remote_storage.RemoteObjectCheckError) as raised:
                self.backend._check_object_exists(self.key)
            print(raised.exception)
        _, output = self.capture(check)
        self.assertIn("ClientError (HTTP 403)", output)
        self.backend.s3_client.head_object.assert_called_once_with(Bucket=self.backend.bucket_name, Key=self.key)

    def test_head_nonclient_error_is_never_formatted(self):
        self.backend.s3_client.head_object.side_effect = self.error
        def check():
            with self.assertRaises(remote_storage.RemoteObjectCheckError) as raised:
                self.backend._check_object_exists(self.key)
            print(raised.exception)
        _, output = self.capture(check)
        self.assertIn("UnprintableStorageError", output)

    def test_missing_object_keeps_absence_semantics_without_logging_key(self):
        for code in ("404", "NoSuchKey", "Not Found"):
            with self.subTest(code=code):
                self.backend.s3_client.head_object.side_effect = self.client_error(code, 404)
                result, output = self.capture(self.backend._download_sqlite)
                self.assertIsNone(result)
                self.assertIn("文件不存在", output)

    def test_download_exceptions_still_propagate_without_logging_details(self):
        self.backend._check_object_exists = Mock(return_value=True)
        for error in (self.error, self.client_error("private-code", 503)):
            with self.subTest(error_type=type(error).__name__):
                self.backend.s3_client.get_object.side_effect = error
                def download():
                    with self.assertRaises(type(error)) as raised:
                        self.backend._download_sqlite()
                    self.assertIs(raised.exception, error)
                _, output = self.capture(download)
                self.assertIn(type(error).__name__, output)

    def test_upload_preflight_and_error_logs_omit_paths(self):
        self.local.write_bytes(b"synthetic-db")
        self.backend.s3_client.put_object.side_effect = self.error
        result, output = self.capture(self.backend._upload_sqlite)
        self.assertFalse(result)
        self.assertIn("UnprintableStorageError", output)
        self.assertEqual(self.backend.s3_client.put_object.call_args.kwargs["Key"], self.key)

    def test_html_and_txt_write_errors_omit_body(self):
        data = Mock(date="2026-09-28")
        with patch("builtins.open", side_effect=self.error):
            for operation in (
                lambda: self.backend.save_html_report("synthetic", "private-local-path"),
                lambda: self.backend.save_txt_snapshot(data),
            ):
                result, output = self.capture(operation)
                self.assertIsNone(result)
                self.assertIn("UnprintableStorageError", output)

    def test_cleanup_connection_and_directory_errors_omit_paths(self):
        connection = Mock()
        connection.close.side_effect = self.error
        self.backend._db_connections = {str(self.local): connection}
        with patch.object(remote_storage.shutil, "rmtree", side_effect=self.error):
            _, output = self.capture(self.backend.cleanup)
        self.assertEqual(output.count("UnprintableStorageError"), 2)

    def test_cleanup_listing_and_deletion_errors_omit_details(self):
        self.backend.s3_client.get_paginator.side_effect = self.error
        result, output = self.capture(lambda: self.backend.cleanup_old_data(7))
        self.assertEqual(result, 0)
        self.assertIn("UnprintableStorageError", output)
        self.backend.s3_client.get_paginator.side_effect = None
        paginator = self.backend.s3_client.get_paginator.return_value
        paginator.paginate.side_effect = [
            [{"Contents": [{"Key": "news/2026-08-01.db"}]}], [],
        ]
        self.backend.s3_client.delete_objects.side_effect = self.error
        result, output = self.capture(lambda: self.backend.cleanup_old_data(7))
        self.assertEqual(result, 0)
        self.assertIn("批量删除失败: UnprintableStorageError", output)

    def test_pull_errors_omit_object_key_and_remove_partial_files(self):
        self.backend._check_object_exists = Mock(return_value=True)
        body = Mock()
        body.iter_chunks.side_effect = self.error
        self.backend.s3_client.get_object.return_value = {"Body": body}
        result, output = self.capture(lambda: self.backend.pull_recent_days(1, str(self.root)))
        self.assertEqual(result, 0)
        self.assertIn("UnprintableStorageError", output)
        self.assertFalse(list(self.root.rglob("*.part")))

    def test_list_errors_omit_exception_body(self):
        self.backend.s3_client.get_paginator.side_effect = self.error
        result, output = self.capture(self.backend.list_remote_dates)
        self.assertEqual(result, [])
        self.assertIn("UnprintableStorageError", output)

    def test_status_summary_only_accepts_numeric_http_status(self):
        for status in ("private-code", "403", 999, True, None):
            with self.subTest(status=status):
                error = self.client_error("private-code", status)
                self.assertEqual(remote_storage._error_summary(error), "ClientError")


if __name__ == "__main__":
    unittest.main()
