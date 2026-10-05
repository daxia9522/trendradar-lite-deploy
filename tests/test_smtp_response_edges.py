"""SMTP phase/outcome tests: only mocked connections, no network or sleeps."""
import io
import smtplib
import ssl
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from trendradar.notification import senders

RECIPIENTS = ("one@example.invalid", "two@example.invalid", "three@example.invalid")
PRIVATE_DETAIL = b"SYNTHETIC_PRIVATE_DETAIL one@example.invalid"
SMTP_DATA = smtplib.SMTP.data


def smtp_server():
    server = Mock(spec=smtplib.SMTP_SSL)
    server.ehlo.return_value = (250, b"hello")
    server.mail.return_value = (250, b"ok")
    server.rcpt.return_value = (250, b"ok")
    server.data.return_value = (250, b"queued")
    return server


class SmtpResponseEdgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.report = Path(self.temp.name) / "report.html"
        self.report.write_text("<html>synthetic report</html>", encoding="utf-8")

    def send(self, server, *, port=465):
        output = io.StringIO()
        factory_name = "SMTP_SSL" if port == 465 else "SMTP"
        unused = "SMTP" if port == 465 else "SMTP_SSL"
        with (
            patch.object(senders.smtplib, factory_name, side_effect=[server]) as factory,
            patch.object(senders.smtplib, unused, side_effect=AssertionError("unexpected transport")),
            redirect_stdout(output),
        ):
            result = senders.send_to_email(
                "sender@example.invalid", "synthetic-password", ",".join(RECIPIENTS),
                "daily", str(self.report), "smtp.example.invalid", port,
            )
        self.assert_private_log(output.getvalue())
        factory.assert_called_once()
        return result, output.getvalue(), factory

    def assert_private_log(self, output):
        for value in ("SYNTHETIC_PRIVATE_DETAIL", "sender@example.invalid", *RECIPIENTS,
                      "smtp.example.invalid", "synthetic-password", str(self.report)):
            self.assertNotIn(value, output)

    def test_complete_acceptance_requires_final_data_ack(self):
        server = smtp_server()
        result, output, factory = self.send(server)
        self.assertTrue(result.sent)
        self.assertEqual(result.accepted, RECIPIENTS)
        self.assertEqual(result.requested, RECIPIENTS)
        server.mail.assert_called_once_with("sender@example.invalid")
        self.assertEqual([call.args[0] for call in server.rcpt.call_args_list], list(RECIPIENTS))
        server.data.assert_called_once()
        server.send_message.assert_not_called()
        server.quit.assert_called_once()
        server.close.assert_called_once()
        context = factory.call_args.kwargs["context"]
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertIn("邮件发送成功", output)

    def test_data_response_code_boundaries(self):
        for code, field in ((399, "unknown"), (400, "temporary_failed"),
                            (499, "temporary_failed"), (500, "permanent_failed"),
                            (599, "permanent_failed"), (600, "unknown")):
            with self.subTest(code=code):
                server = smtp_server()
                server.data.side_effect = smtplib.SMTPDataError(code, PRIVATE_DETAIL)
                result, _, _ = self.send(server)
                self.assertEqual(getattr(result, field), RECIPIENTS)
                self.assertFalse(result.sent)
                self.assertFalse(result.partially_delivered)

    def test_mail_sender_rejection_classifies_all_and_never_sends_data(self):
        for code, field in ((450, "temporary_failed"), (550, "permanent_failed")):
            with self.subTest(code=code):
                server = smtp_server()
                server.mail.return_value = (code, PRIVATE_DETAIL)
                result, _, _ = self.send(server)
                self.assertEqual(getattr(result, field), RECIPIENTS)
                server.rcpt.assert_not_called()
                server.data.assert_not_called()

    def test_connection_reply_distinguishes_temporary_and_permanent(self):
        for code, field in ((421, "temporary_failed"), (554, "permanent_failed")):
            with self.subTest(code=code):
                result, _, _ = self.send(smtplib.SMTPConnectError(code, PRIVATE_DETAIL))
                self.assertEqual(getattr(result, field), RECIPIENTS)

    def test_starttls_reply_distinguishes_temporary_and_permanent(self):
        for code, field in ((454, "temporary_failed"), (501, "permanent_failed")):
            with self.subTest(code=code):
                server = smtp_server()
                server.starttls.side_effect = smtplib.SMTPResponseException(code, PRIVATE_DETAIL)
                result, _, _ = self.send(server, port=587)
                self.assertEqual(getattr(result, field), RECIPIENTS)
                context = server.starttls.call_args.kwargs["context"]
                self.assertTrue(context.check_hostname)
                self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
                server.login.assert_not_called()
                server.data.assert_not_called()

    def test_authentication_errors_require_manual_correction(self):
        for code in (454, 535):
            with self.subTest(code=code):
                server = smtp_server()
                server.login.side_effect = smtplib.SMTPAuthenticationError(code, PRIVATE_DETAIL)
                result, output, _ = self.send(server)
                self.assertEqual(result.permanent_failed, RECIPIENTS)
                self.assertIn("认证错误", output)
                server.data.assert_not_called()
                server.quit.assert_called_once()

    def test_mixed_full_refusals_retain_independent_outcomes(self):
        server = smtp_server()
        server.rcpt.side_effect = [(450, PRIVATE_DETAIL), (550, PRIVATE_DETAIL), (451, PRIVATE_DETAIL)]
        result, _, _ = self.send(server)
        self.assertEqual(result.temporary_failed, (RECIPIENTS[0], RECIPIENTS[2]))
        self.assertEqual(result.permanent_failed, (RECIPIENTS[1],))
        self.assertEqual(result.accepted, ())
        self.assertEqual(result.unknown, ())
        server.data.assert_not_called()

    def test_partial_acceptance_with_both_rejection_classes(self):
        server = smtp_server()
        server.rcpt.side_effect = [(250, b"ok"), (451, PRIVATE_DETAIL), (550, PRIVATE_DETAIL)]
        result, output, _ = self.send(server)
        self.assertEqual(result.accepted, (RECIPIENTS[0],))
        self.assertEqual(result.temporary_failed, (RECIPIENTS[1],))
        self.assertEqual(result.permanent_failed, (RECIPIENTS[2],))
        self.assertTrue(result.partially_delivered)
        self.assertFalse(result.sent)
        self.assertIn("部分投递", output)
        server.data.assert_called_once()

    def test_rcpt_success_is_not_publication_when_data_is_rejected(self):
        server = smtp_server()
        server.rcpt.side_effect = [(250, b"ok"), (451, PRIVATE_DETAIL), (550, PRIVATE_DETAIL)]
        server.data.side_effect = smtplib.SMTPDataError(554, PRIVATE_DETAIL)
        result, _, _ = self.send(server)
        self.assertEqual(result.accepted, ())
        self.assertEqual(result.temporary_failed, (RECIPIENTS[1],))
        self.assertEqual(result.permanent_failed, (RECIPIENTS[0], RECIPIENTS[2]))

    def test_lost_data_response_is_unknown_but_prior_refusals_stay_known(self):
        for error in (smtplib.SMTPServerDisconnected("private"), TimeoutError("private"),
                      ConnectionResetError("private"), RuntimeError("private")):
            with self.subTest(error=type(error).__name__):
                server = smtp_server()
                server.rcpt.side_effect = [(250, b"ok"), (451, PRIVATE_DETAIL), (550, PRIVATE_DETAIL)]
                server.data.side_effect = error
                result, _, _ = self.send(server)
                self.assertEqual(result.unknown, (RECIPIENTS[0],))
                self.assertEqual(result.temporary_failed, (RECIPIENTS[1],))
                self.assertEqual(result.permanent_failed, (RECIPIENTS[2],))
                self.assertEqual(result.accepted, ())

    def test_disconnect_before_data_is_safe_temporary_failure(self):
        for phase in ("login", "mail", "rcpt"):
            with self.subTest(phase=phase):
                server = smtp_server()
                getattr(server, phase).side_effect = smtplib.SMTPServerDisconnected("private")
                result, _, _ = self.send(server)
                self.assertEqual(result.temporary_failed, RECIPIENTS)
                self.assertEqual(result.unknown, ())
                server.data.assert_not_called()

    def test_rcpt_disconnect_preserves_earlier_permanent_refusal(self):
        server = smtp_server()
        server.rcpt.side_effect = [(550, PRIVATE_DETAIL), (250, b"ok"), smtplib.SMTPServerDisconnected("private")]
        result, _, _ = self.send(server)
        self.assertEqual(result.permanent_failed, (RECIPIENTS[0],))
        self.assertEqual(result.temporary_failed, RECIPIENTS[1:])
        server.data.assert_not_called()

    def test_rcpt_421_stops_without_data_even_after_previous_rcpt_success(self):
        server = smtp_server()
        server.rcpt.side_effect = [(250, b"ok"), (421, PRIVATE_DETAIL)]
        result, _, _ = self.send(server)
        self.assertEqual(result.temporary_failed, RECIPIENTS)
        self.assertEqual(server.rcpt.call_count, 2)
        server.data.assert_not_called()

    def test_quit_failure_does_not_undo_data_acceptance(self):
        server = smtp_server()
        server.quit.side_effect = smtplib.SMTPServerDisconnected("private")
        result, _, _ = self.send(server)
        self.assertEqual(result.accepted, RECIPIENTS)
        self.assertTrue(result.sent)
        server.close.assert_called_once()

    def test_certificate_validation_failure_is_not_retryable(self):
        result, _, _ = self.send(ssl.SSLCertVerificationError("private"))
        self.assertEqual(result.permanent_failed, RECIPIENTS)

    def test_process_interruption_is_not_fabricated_as_a_durable_receipt(self):
        server = smtp_server()
        server.data.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.send(server)
        server.close.assert_called_once()

    def test_unprintable_exception_detail_is_never_formatted(self):
        class UnprintableError(Exception):
            def __str__(self):
                raise AssertionError("must not format provider detail")
            __repr__ = __str__
        server = smtp_server()
        server.data.side_effect = UnprintableError()
        result, _, _ = self.send(server)
        self.assertEqual(result.unknown, RECIPIENTS)

    def test_real_smtplib_data_ack_and_response_loss_are_distinct(self):
        # Exercise stdlib DATA framing/reply parsing, replacing only its wire
        # methods. No socket is constructed and no DNS/network request occurs.
        for final_reply, expected in (
            ((250, b"queued"), "accepted"),
            ((451, PRIVATE_DETAIL), "temporary_failed"),
            ((554, PRIVATE_DETAIL), "permanent_failed"),
            (smtplib.SMTPServerDisconnected("private"), "unknown"),
        ):
            with self.subTest(expected=expected):
                wire = Mock()
                wire.debuglevel = 0
                wire.getreply.side_effect = [(354, b"go ahead"), final_reply]
                server = smtp_server()
                server.data.side_effect = lambda payload: SMTP_DATA(wire, payload)
                result, _, _ = self.send(server)
                self.assertEqual(getattr(result, expected), RECIPIENTS)
                wire.putcmd.assert_called_once_with("data")
                payload = server.data.call_args.args[0]
                self.assertTrue(payload.endswith(b"\r\n"))
                wire.send.assert_called_once_with(payload + b".\r\n")

    def test_real_smtplib_initial_data_rejection_sends_no_body(self):
        wire = Mock()
        wire.debuglevel = 0
        wire.getreply.return_value = (451, PRIVATE_DETAIL)
        server = smtp_server()
        server.data.side_effect = lambda payload: SMTP_DATA(wire, payload)
        result, _, _ = self.send(server)
        self.assertEqual(result.temporary_failed, RECIPIENTS)
        wire.send.assert_not_called()

    def test_real_smtplib_body_write_failure_is_unknown(self):
        wire = Mock()
        wire.debuglevel = 0
        wire.getreply.return_value = (354, b"go ahead")
        wire.send.side_effect = ConnectionResetError("private")
        server = smtp_server()
        server.data.side_effect = lambda payload: SMTP_DATA(wire, payload)
        result, _, _ = self.send(server)
        self.assertEqual(result.unknown, RECIPIENTS)


if __name__ == "__main__":
    unittest.main()
