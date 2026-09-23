# coding=utf-8
"""邮件重试行为的契约测试。

验证移植自 VPS1 的瞬时故障重试：重试次数、退避序列、
永久错误的立即上抛，以及日志不泄漏地址。
"""
import io
import smtplib
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from trendradar.notification import senders
from trendradar.notification.senders import send_to_email


class EmailRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.report = Path(self.temp.name) / "report.html"
        self.report.write_text("<html>report</html>", encoding="utf-8")
        self.addCleanup(self.temp.cleanup)

    def _send(self, smtp, output=None):
        smtp.send_message.return_value = {}  # smtplib: 所有收件人均被接受
        kwargs = dict(
            from_email="sender@example.invalid",
            password="test-password",
            to_email="recipient@example.invalid",
            report_type="daily",
            html_file_path=str(self.report),
            custom_smtp_server="smtp.example.invalid",
            custom_smtp_port=465,
        )
        if output is None:
            return send_to_email(**kwargs)
        with redirect_stdout(output):
            return send_to_email(**kwargs)

    def test_transient_failure_is_retried_then_succeeds(self):
        smtp = Mock()
        smtp.login.side_effect = [
            smtplib.SMTPServerDisconnected("dropped"),
            smtplib.SMTPServerDisconnected("dropped"),
            None,
        ]
        sleeps = []

        with patch.object(senders.smtplib, "SMTP_SSL", return_value=smtp):
            with patch.object(senders.time, "sleep", side_effect=sleeps.append):
                sent = self._send(smtp, io.StringIO())

        self.assertTrue(sent)
        self.assertEqual(sleeps, list(senders.SEND_RETRY_DELAYS))
        self.assertEqual(smtp.login.call_count, 3)

    def test_retry_exhaustion_returns_false(self):
        smtp = Mock()
        smtp.login.side_effect = smtplib.SMTPServerDisconnected("dropped")
        sleeps = []
        output = io.StringIO()

        with patch.object(senders.smtplib, "SMTP_SSL", return_value=smtp):
            with patch.object(senders.time, "sleep", side_effect=sleeps.append):
                sent = self._send(smtp, output)

        self.assertFalse(sent)
        attempts = len(senders.SEND_RETRY_DELAYS) + 1
        self.assertEqual(smtp.login.call_count, attempts)
        # 最后一次失败不再等待
        self.assertEqual(sleeps, list(senders.SEND_RETRY_DELAYS))
        self.assertIn("服务器意外断开连接", output.getvalue())

    def test_permanent_error_is_not_retried(self):
        smtp = Mock()
        smtp.login.return_value = None
        smtp.send_message.side_effect = smtplib.SMTPRecipientsRefused(
            {"recipient@example.invalid": (550, b"no such user")}
        )
        sleeps = []

        with patch.object(senders.smtplib, "SMTP_SSL", return_value=smtp):
            with patch.object(senders.time, "sleep", side_effect=sleeps.append):
                sent = self._send(smtp, io.StringIO())

        self.assertFalse(sent)
        self.assertEqual(sleeps, [])
        self.assertEqual(smtp.send_message.call_count, 1)

    def test_retry_log_omits_addresses_and_server(self):
        smtp = Mock()
        smtp.login.side_effect = smtplib.SMTPServerDisconnected(
            "failure detail mentioning recipient@example.invalid"
        )
        output = io.StringIO()

        with patch.object(senders.smtplib, "SMTP_SSL", return_value=smtp):
            with patch.object(senders.time, "sleep"):
                self._send(smtp, output)

        text = output.getvalue()
        self.assertIn("次失败", text)
        self.assertIn("SMTPServerDisconnected", text)
        for private_value in ("recipient@example.invalid", "sender@example.invalid", "smtp.example.invalid"):
            self.assertNotIn(private_value, text)

    def test_connection_is_closed_between_attempts(self):
        smtp = Mock()
        smtp.login.side_effect = [
            smtplib.SMTPServerDisconnected("dropped"),
            None,
        ]

        with patch.object(senders.smtplib, "SMTP_SSL", return_value=smtp):
            with patch.object(senders.time, "sleep"):
                self._send(smtp, io.StringIO())

        # 每次尝试都新建连接并显式关闭，避免复用半开连接
        self.assertEqual(smtp.quit.call_count, 2)


if __name__ == "__main__":
    unittest.main()