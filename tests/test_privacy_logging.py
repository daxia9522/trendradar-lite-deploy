import io
import smtplib
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from trendradar.notification.senders import send_to_email
from trendradar.storage import remote as remote_storage


class PrivacyLoggingTests(unittest.TestCase):
    def test_email_success_log_omits_addresses_and_server(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            report = Path(temp_dir) / "report.html"
            report.write_text("<html>report</html>", encoding="utf-8")
            smtp = Mock()
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


if __name__ == "__main__":
    unittest.main()
