"""SMTP response codes and partial-acceptance contracts, without real delivery."""
import io
import smtplib
import ssl
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from trendradar.notification import senders


RECIPIENTS = ["one@example.invalid", "two@example.invalid"]
PRIVATE_DETAIL = b"SYNTHETIC_PRIVATE_DETAIL one@example.invalid"


def smtp_server():
    server = Mock(spec=smtplib.SMTP_SSL)
    # smtplib returns a dictionary: empty means every envelope recipient accepted.
    server.send_message.return_value = {}
    return server


class SmtpResponseEdgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.report = Path(self.temp.name) / "report.html"
        self.report.write_text("<html>synthetic report</html>", encoding="utf-8")

    def send(self, servers, *, port=465, on_partial_delivery=None):
        output = io.StringIO()
        factory_name = "SMTP_SSL" if port == 465 else "SMTP"
        with patch.object(senders.smtplib, factory_name, side_effect=servers) as factory:
            with patch.object(senders.time, "sleep") as sleep, redirect_stdout(output):
                result = senders.send_to_email(
                    "sender@example.invalid", "synthetic-password", ",".join(RECIPIENTS),
                    "daily", str(self.report), "smtp.example.invalid", port,
                    on_partial_delivery=on_partial_delivery,
                )
        self.assert_private_log(output.getvalue())
        return result, output.getvalue(), factory, sleep

    def assert_private_log(self, output):
        for value in ("SYNTHETIC_PRIVATE_DETAIL", "sender@example.invalid", *RECIPIENTS, "smtp.example.invalid", "synthetic-password", str(self.report)):
            self.assertNotIn(value, output)

    def test_temporary_data_rejection_retries_with_fresh_connection(self):
        first, second = smtp_server(), smtp_server()
        first.send_message.side_effect = smtplib.SMTPDataError(451, PRIVATE_DETAIL)
        result, output, factory, sleep = self.send([first, second])
        self.assertTrue(result)
        self.assertEqual(factory.call_count, 2)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [3])
        for server in (first, second):
            server.quit.assert_called_once_with()
            self.assertEqual(server.send_message.call_args.kwargs["to_addrs"], RECIPIENTS)
            self.assertEqual(server.send_message.call_args.kwargs["from_addr"], "sender@example.invalid")
        context = factory.call_args.kwargs["context"]
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertIn("SMTPDataError", output)
        self.assertIn("邮件发送成功", output)

    def test_temporary_data_rejection_exhausts_existing_retry_budget(self):
        servers = [smtp_server() for _ in range(3)]
        for server in servers:
            server.send_message.side_effect = smtplib.SMTPDataError(451, PRIVATE_DETAIL)
        result, output, factory, sleep = self.send(servers)
        self.assertFalse(result)
        self.assertEqual(factory.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [3, 15])
        self.assertNotIn("邮件发送成功", output)
        for server in servers:
            server.quit.assert_called_once_with()

    def test_response_code_boundaries_decide_retry(self):
        for code, retry in ((399, False), (400, True), (499, True), (500, False), (550, False), (599, False)):
            with self.subTest(code=code):
                first, second = smtp_server(), smtp_server()
                first.send_message.side_effect = smtplib.SMTPDataError(code, PRIVATE_DETAIL)
                result, _, factory, sleep = self.send([first, second])
                self.assertEqual(result, retry)
                self.assertEqual(factory.call_count, 2 if retry else 1)
                self.assertEqual(sleep.call_count, 1 if retry else 0)

    def test_sender_refusal_distinguishes_temporary_and_permanent(self):
        for code, retry in ((450, True), (550, False)):
            with self.subTest(code=code):
                first, second = smtp_server(), smtp_server()
                first.send_message.side_effect = smtplib.SMTPSenderRefused(code, PRIVATE_DETAIL, "sender@example.invalid")
                result, _, factory, sleep = self.send([first, second])
                self.assertEqual(result, retry)
                self.assertEqual(factory.call_count, 2 if retry else 1)
                self.assertEqual(sleep.call_count, 1 if retry else 0)

    def test_connection_reply_distinguishes_temporary_and_permanent(self):
        for code, retry in ((421, True), (554, False)):
            with self.subTest(code=code):
                server = smtp_server()
                error = smtplib.SMTPConnectError(code, PRIVATE_DETAIL)
                result, _, factory, sleep = self.send([error, server])
                self.assertEqual(result, retry)
                self.assertEqual(factory.call_count, 2 if retry else 1)
                self.assertEqual(sleep.call_count, 1 if retry else 0)

    def test_starttls_reply_distinguishes_temporary_and_permanent(self):
        for code, retry in ((454, True), (501, False)):
            with self.subTest(code=code):
                first, second = smtp_server(), smtp_server()
                first.starttls.side_effect = smtplib.SMTPResponseException(code, PRIVATE_DETAIL)
                result, _, factory, sleep = self.send([first, second], port=587)
                self.assertEqual(result, retry)
                self.assertEqual(factory.call_count, 2 if retry else 1)
                self.assertEqual(sleep.call_count, 1 if retry else 0)
                context = first.starttls.call_args.kwargs["context"]
                self.assertTrue(context.check_hostname)
                self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    def test_authentication_errors_keep_existing_provider_retry_policy(self):
        # 535 is intentionally retried by the retained provider compatibility policy.
        for code in (454, 535):
            with self.subTest(code=code):
                servers = [smtp_server() for _ in range(3)]
                for server in servers:
                    server.login.side_effect = smtplib.SMTPAuthenticationError(code, PRIVATE_DETAIL)
                result, output, factory, sleep = self.send(servers)
                self.assertFalse(result)
                self.assertEqual(factory.call_count, 3)
                self.assertEqual([call.args[0] for call in sleep.call_args_list], [3, 15])
                self.assertIn("认证错误", output)
                for server in servers:
                    server.send_message.assert_not_called()
                    server.quit.assert_called_once_with()

    def test_all_temporary_recipient_refusals_can_retry(self):
        first, second = smtp_server(), smtp_server()
        first.send_message.side_effect = smtplib.SMTPRecipientsRefused({
            RECIPIENTS[0]: (450, PRIVATE_DETAIL), RECIPIENTS[1]: (451, PRIVATE_DETAIL),
        })
        result, _, factory, sleep = self.send([first, second])
        self.assertTrue(result)
        self.assertEqual(factory.call_count, 2)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [3])
        self.assertEqual(second.send_message.call_args.kwargs["to_addrs"], RECIPIENTS)

    def test_mixed_or_permanent_full_refusal_does_not_retry_whole_envelope(self):
        for codes in ((450, 550), (550, 550)):
            with self.subTest(codes=codes):
                server = smtp_server()
                server.send_message.side_effect = smtplib.SMTPRecipientsRefused({
                    address: (code, PRIVATE_DETAIL) for address, code in zip(RECIPIENTS, codes)
                })
                result, output, factory, sleep = self.send([server])
                self.assertFalse(result)
                self.assertEqual(factory.call_count, 1)
                sleep.assert_not_called()
                self.assertIn("收件人地址被拒绝", output)

    def test_421_before_data_retries_original_envelope_not_only_refusal_keys(self):
        first, second = smtp_server(), smtp_server()
        first.send_message.side_effect = smtplib.SMTPRecipientsRefused({
            RECIPIENTS[1]: (421, PRIVATE_DETAIL),
        })
        partial = Mock()
        result, _, factory, sleep = self.send([first, second], on_partial_delivery=partial)
        self.assertTrue(result)
        self.assertEqual(factory.call_count, 2)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [3])
        self.assertEqual(second.send_message.call_args.kwargs["to_addrs"], RECIPIENTS)
        partial.assert_not_called()

    def test_partial_refusal_is_explicit_and_never_retries_accepted_recipients(self):
        # A normal nonempty result means DATA was accepted for the other recipient.
        for code in (451, 550):
            with self.subTest(code=code):
                server = smtp_server()
                server.send_message.return_value = {RECIPIENTS[1]: (code, PRIVATE_DETAIL)}
                result, output, factory, sleep = self.send([server])
                self.assertFalse(result)
                self.assertEqual(factory.call_count, 1)
                self.assertEqual(server.send_message.call_count, 1)
                self.assertEqual(server.send_message.call_args.kwargs["to_addrs"], RECIPIENTS)
                server.quit.assert_called_once_with()
                sleep.assert_not_called()
                self.assertIn("部分投递", output)
                self.assertIn("1 个收件人被拒绝", output)
                self.assertIn("不自动重试", output)
                self.assertNotIn("邮件发送成功", output)

    def test_partial_acceptance_after_a_retry_stops_further_delivery(self):
        first, second = smtp_server(), smtp_server()
        first.send_message.side_effect = smtplib.SMTPDataError(451, PRIVATE_DETAIL)
        second.send_message.return_value = {RECIPIENTS[1]: (451, PRIVATE_DETAIL)}
        result, output, factory, sleep = self.send([first, second])
        self.assertFalse(result)
        self.assertEqual(factory.call_count, 2)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [3])
        self.assertEqual(second.send_message.call_count, 1)
        self.assertIn("部分投递", output)
        self.assertNotIn("邮件发送成功", output)

    def test_empty_refusal_dictionary_is_complete_smtp_acceptance(self):
        server = smtp_server()
        result, output, factory, sleep = self.send([server])
        self.assertTrue(result)
        self.assertEqual(factory.call_count, 1)
        self.assertIn("邮件发送成功", output)
        self.assertNotIn("部分投递", output)
        sleep.assert_not_called()

    def test_partial_callback_failure_cannot_restart_delivery(self):
        server = smtp_server()
        server.send_message.return_value = {RECIPIENTS[1]: (451, PRIVATE_DETAIL)}
        partial = Mock(side_effect=OSError(PRIVATE_DETAIL))
        result, output, factory, sleep = self.send([server], on_partial_delivery=partial)
        self.assertFalse(result)
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(server.send_message.call_count, 1)
        partial.assert_called_once_with()
        sleep.assert_not_called()
        self.assertIn("部分投递", output)
        self.assertNotIn("邮件发送成功", output)

    def test_partial_callback_is_not_called_without_partial_acceptance(self):
        for outcome in ({}, smtplib.SMTPDataError(554, PRIVATE_DETAIL)):
            with self.subTest(outcome_type=type(outcome).__name__):
                server = smtp_server()
                if isinstance(outcome, Exception):
                    server.send_message.side_effect = outcome
                else:
                    server.send_message.return_value = outcome
                partial = Mock()
                self.send([server], on_partial_delivery=partial)
                partial.assert_not_called()


if __name__ == "__main__":
    unittest.main()
