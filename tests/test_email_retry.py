"""Explicit retry attempts reuse frozen MIME and only confirmed temporary failures."""
import io
import json
import smtplib
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from trendradar.notification import EmailDeliveryResult, NotificationDispatcher, PreparedEmail
from trendradar.notification import senders


class EmailRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.report = Path(self.temp.name) / "report.html"
        self.report.write_text("<html>frozen report</html>", encoding="utf-8")
        self.config = {
            "EMAIL_FROM": "sender@example.invalid", "EMAIL_PASSWORD": "test-password",
            "EMAIL_TO": "one@example.invalid,two@example.invalid,three@example.invalid",
            "EMAIL_SMTP_SERVER": "smtp.example.invalid", "EMAIL_SMTP_PORT": 465,
        }
        self.clock = Mock(return_value=datetime(2026, 10, 5, 7, 0))
        self.dispatcher = NotificationDispatcher(self.config, self.clock)
        self.prepared = self.dispatcher.prepare_report("daily", str(self.report))

    def server(self, rcpt_codes=(250, 250, 250)):
        server = Mock(spec=smtplib.SMTP_SSL)
        server.mail.return_value = (250, b"ok")
        server.rcpt.side_effect = [(code, b"private response") for code in rcpt_codes]
        server.data.return_value = (250, b"ok")
        return server

    def test_partial_subset_retry_after_json_roundtrip_uses_identical_mime(self):
        first, second = self.server((250, 451, 550)), self.server((250,))
        with patch.object(senders.smtplib, "SMTP_SSL", side_effect=[first, second]) as factory, redirect_stdout(io.StringIO()):
            receipt = self.dispatcher.send_prepared(self.prepared)
            factory.assert_called_once()  # caller must record receipt before another attempt
            self.assertEqual(receipt.accepted, ("one@example.invalid",))
            self.assertEqual(receipt.temporary_failed, ("two@example.invalid",))
            self.assertEqual(receipt.permanent_failed, ("three@example.invalid",))
            stored = json.loads(json.dumps(self.prepared.to_dict()))
            receipt = EmailDeliveryResult.from_dict(json.loads(json.dumps(receipt.to_dict())))
            # Retry does not read a live HTML file, render, regenerate ID/date or
            # reread current sender/recipient configuration.
            self.report.unlink()
            self.config.update(EMAIL_FROM="changed@example.invalid", EMAIL_TO="other@example.invalid",
                               EMAIL_PASSWORD="rotated-password")
            with (
                patch.object(senders, "prepare_email", side_effect=AssertionError("must not render")),
                patch.object(senders, "make_msgid", side_effect=AssertionError("must not regenerate id")),
            ):
                result = self.dispatcher.send_prepared(PreparedEmail.from_dict(stored), recipients=receipt.temporary_failed)
        self.assertTrue(result.sent)
        self.assertEqual(result.requested, ("two@example.invalid",))
        second.rcpt.assert_called_once_with("two@example.invalid")
        second.mail.assert_called_once_with("sender@example.invalid")
        second.login.assert_called_once_with("sender@example.invalid", "rotated-password")
        self.assertEqual(first.data.call_args.args[0], second.data.call_args.args[0])
        self.assertEqual(second.data.call_args.args[0], self.prepared.mime_bytes)
        self.clock.assert_called_once()
        self.assertEqual(factory.call_count, 2)

    def test_all_rejected_mixed_codes_retry_only_transient_subset(self):
        first, second = self.server((450, 550, 451)), self.server((250, 250))
        with patch.object(senders.smtplib, "SMTP_SSL", side_effect=[first, second]), redirect_stdout(io.StringIO()):
            receipt = self.dispatcher.send_prepared(self.prepared)
            self.assertFalse(receipt.sent)
            self.assertFalse(receipt.partially_delivered)
            first.data.assert_not_called()
            result = self.dispatcher.send_prepared(self.prepared, recipients=receipt.temporary_failed)
        self.assertEqual([call.args[0] for call in second.rcpt.call_args_list],
                         ["one@example.invalid", "three@example.invalid"])
        self.assertTrue(result.sent)

    def test_connection_failure_returns_once_without_sleep_or_hidden_retry(self):
        server = self.server()
        server.login.side_effect = smtplib.SMTPServerDisconnected("private")
        with patch.object(senders.smtplib, "SMTP_SSL", return_value=server) as factory, redirect_stdout(io.StringIO()):
            result = self.dispatcher.send_prepared(self.prepared)
        self.assertEqual(result.temporary_failed, self.prepared.recipients)
        factory.assert_called_once()
        server.quit.assert_called_once()
        server.close.assert_called_once()
        server.data.assert_not_called()

    def test_data_loss_is_unknown_and_convenience_never_retries(self):
        server = self.server()
        server.data.side_effect = TimeoutError("private")
        with patch.object(senders.smtplib, "SMTP_SSL", return_value=server) as factory, redirect_stdout(io.StringIO()):
            result = self.dispatcher.send_report("daily", str(self.report))
        self.assertEqual(result.unknown, self.prepared.recipients)
        self.assertEqual(result.temporary_failed, ())
        factory.assert_called_once()
        server.data.assert_called_once()

    def test_subset_validation_precedes_network_and_empty_subset_never_sends(self):
        with patch.object(senders.smtplib, "SMTP_SSL") as factory:
            for recipients in (("unauthorized@example.invalid",),
                               ("one@example.invalid", "one@example.invalid"), "one@example.invalid"):
                with self.subTest(recipients=recipients), self.assertRaises(ValueError):
                    self.dispatcher.send_prepared(self.prepared, recipients=recipients)
            result = self.dispatcher.send_prepared(self.prepared, recipients=())
        factory.assert_not_called()
        self.assertEqual(result.requested, ())
        self.assertFalse(result.sent)

    def test_credentials_are_resolved_at_attempt_time_but_not_persisted(self):
        payload = json.dumps(self.prepared.to_dict())
        self.assertNotIn("test-password", payload)
        self.assertNotIn("smtp.example.invalid", payload)
        self.config.pop("EMAIL_PASSWORD")
        with patch.object(senders.smtplib, "SMTP_SSL") as factory:
            result = self.dispatcher.send_prepared(self.prepared)
        factory.assert_not_called()
        self.assertFalse(result.configured)
        self.assertEqual(result.permanent_failed, self.prepared.recipients)


if __name__ == "__main__":
    unittest.main()
