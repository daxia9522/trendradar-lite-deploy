"""Immutable payload/receipt contracts and shared offline weekly/CLI consumers."""
import io
import json
import os
import smtplib
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import FrozenInstanceError
from datetime import datetime
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr
from pathlib import Path
from types import MappingProxyType
from unittest.mock import Mock, patch

from trendradar import cli
from trendradar.notification import EmailDeliveryResult, NotificationDispatcher, PreparedEmail
from trendradar.notification import dispatcher as delivery
from trendradar.notification import senders
from weekly_report import weekly_ai_report_email as weekly

NOW = datetime(2026, 9, 21, 7, 30)
ADDRESSES = ("one@example.invalid", "two@example.invalid")
MAIL_CONFIG = {
    "EMAIL_FROM": "sender@example.invalid", "EMAIL_PASSWORD": "synthetic-password",
    "EMAIL_TO": ",".join(ADDRESSES), "EMAIL_SMTP_SERVER": "smtp.example.invalid", "EMAIL_SMTP_PORT": "465",
}
WEEKLY_SUBJECT = "AI周报（2026-09-15 ~ 2026-09-21）"


def smtp_server(refusals=None, data_error=None):
    server = Mock(spec=smtplib.SMTP_SSL)
    server.mail.return_value = (250, b"ok")
    server.rcpt.side_effect = lambda address: (refusals or {}).get(address, (250, b"ok"))
    server.data.return_value = (250, b"queued")
    server.data.side_effect = data_error
    return server


def parse_payload(payload):
    return BytesParser(policy=policy.default).parsebytes(payload)


class SingleEmailDeliveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.html = Path(temporary.name) / "report.html"
        self.html.write_text("<html>frozen report</html>", encoding="utf-8")
        self.config = dict(MAIL_CONFIG)
        self.clock = Mock(return_value=NOW)
        self.dispatcher = NotificationDispatcher(self.config, self.clock)

    def test_result_is_immutable_and_requires_explicit_fields(self):
        result = EmailDeliveryResult(True, ADDRESSES, ADDRESSES[:1], ADDRESSES[1:])
        with self.assertRaises(TypeError):
            bool(result)
        with self.assertRaises(FrozenInstanceError):
            result.sent = True
        self.assertEqual((result.configured, result.sent, result.partially_delivered), (True, False, True))
        self.assertEqual(EmailDeliveryResult.from_dict(json.loads(json.dumps(result.to_dict()))), result)
        self.assertNotIn(ADDRESSES[0], repr(result))

    def test_receipt_rejects_overlapping_missing_and_foreign_outcomes(self):
        for kwargs in (
            {"requested": ADDRESSES, "accepted": ADDRESSES, "unknown": ADDRESSES[:1]},
            {"requested": ADDRESSES, "accepted": ADDRESSES[:1]},
            {"requested": ADDRESSES[:1], "accepted": ADDRESSES},
            {"requested": ADDRESSES, "accepted": ADDRESSES, "configured": False},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                EmailDeliveryResult(**{"configured": True, **kwargs})
        self.assertFalse(EmailDeliveryResult(True).sent)
        with self.assertRaises(ValueError):
            EmailDeliveryResult.from_dict({"version": 1, "configured": True})

    def test_prepare_freezes_labels_html_date_id_and_serializes_without_smtp(self):
        with patch.object(delivery, "send_prepared_email") as send, patch("socket.getfqdn", side_effect=AssertionError("preparation must not resolve DNS")):
            prepared = self.dispatcher.prepare_report("current", str(self.html), period_name=" 早间速览 ")
        send.assert_not_called()
        self.assertEqual(prepared.subject, "早间速览 · 09月21日 07:30")
        self.assertEqual(prepared.recipients, ADDRESSES)
        msg = parse_payload(prepared.mime_bytes)
        self.assertEqual(parseaddr(msg["From"]), ("早间速览", MAIL_CONFIG["EMAIL_FROM"]))
        self.assertEqual(str(msg["Subject"]), prepared.subject)
        self.assertEqual(str(msg["Date"]), prepared.date)
        self.assertEqual(str(msg["Message-ID"]), prepared.message_id)
        self.assertTrue(prepared.message_id.endswith("@example.invalid>"))
        self.assertIn("报告类型：current", msg.get_body(preferencelist=("plain",)).get_content())
        self.assertEqual(msg.get_body(preferencelist=("html",)).get_content(), "<html>frozen report</html>")
        self.html.write_text("changed later", encoding="utf-8")
        self.assertNotIn(b"changed later", prepared.mime_bytes)
        self.assertEqual(PreparedEmail.from_dict(json.loads(json.dumps(prepared.to_dict()))), prepared)
        self.assertNotIn("synthetic-password", json.dumps(prepared.to_dict()))
        self.assertNotIn("example.invalid", repr(prepared))
        with self.assertRaises(FrozenInstanceError):
            prepared.subject = "changed"
        self.clock.assert_called_once()

    def test_prepared_snapshot_is_deeply_immutable_and_rejects_corruption(self):
        prepared = self.dispatcher.prepare_report("daily", str(self.html))
        document = prepared.to_dict()
        restored = PreparedEmail.from_dict(document)
        document["recipients"].append("other@example.invalid")
        self.assertEqual(restored.recipients, ADDRESSES)
        for key, value in (("mime_base64", "not base64!"), ("version", 99), ("recipients", []),
                           ("message_id", "\r\ninjected"), ("date", "")):
            bad = prepared.to_dict()
            bad[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                PreparedEmail.from_dict(bad)

    def test_send_report_is_exactly_prepare_plus_single_send(self):
        prepared = self.dispatcher.prepare_report("daily", str(self.html))
        receipt = EmailDeliveryResult(True, ADDRESSES, ADDRESSES)
        with patch.object(self.dispatcher, "prepare_report", return_value=prepared) as prepare, patch.object(self.dispatcher, "send_prepared", return_value=receipt) as send:
            actual = self.dispatcher.send_report("current", "source.html", period_name="label")
        self.assertIs(actual, receipt)
        prepare.assert_called_once_with("current", "source.html", period_name="label", subject_override=None, sender_name_override=None)
        send.assert_called_once_with(prepared)

    def test_subject_and_sender_overrides_preserve_plaintext_report_type(self):
        subject = "  精确主题 <周报> & 分析  "
        prepared = self.dispatcher.prepare_report("plaintext label", str(self.html), subject_override=subject, sender_name_override="AI周报")
        self.assertEqual(prepared.subject, subject)
        msg = parse_payload(prepared.mime_bytes)
        self.assertEqual(parseaddr(msg["From"])[0], "AI周报")
        self.assertIn("报告类型：plaintext label", msg.get_body(preferencelist=("plain",)).get_content())

    def test_missing_credentials_and_missing_html_never_send(self):
        for key in ("EMAIL_FROM", "EMAIL_PASSWORD", "EMAIL_TO"):
            config = dict(MAIL_CONFIG)
            config.pop(key)
            dispatcher = NotificationDispatcher(config, self.clock)
            with patch.object(delivery, "send_prepared_email") as send, redirect_stdout(io.StringIO()):
                result = dispatcher.send_report("daily", str(self.html))
            self.assertFalse(result.configured)
            send.assert_not_called()
        with patch.object(delivery, "send_prepared_email") as send, redirect_stdout(io.StringIO()):
            result = self.dispatcher.send_report("daily")
        self.assertTrue(result.configured)
        self.assertFalse(result.sent)
        send.assert_not_called()
        self.clock.assert_not_called()

    def test_daily_missing_or_unpaired_smtp_overrides_keep_provider_defaults(self):
        for missing in (("EMAIL_SMTP_SERVER",), ("EMAIL_SMTP_PORT",), ("EMAIL_SMTP_SERVER", "EMAIL_SMTP_PORT")):
            with self.subTest(missing=missing):
                config = dict(MAIL_CONFIG, EMAIL_FROM="synthetic-sender@qq.com", EMAIL_SMTP_PORT="587")
                for key in missing:
                    config.pop(key)
                smtp = smtp_server()
                with patch.object(senders.smtplib, "SMTP_SSL", return_value=smtp) as factory, patch.object(senders.smtplib, "SMTP", side_effect=AssertionError("unexpected transport")), redirect_stdout(io.StringIO()):
                    result = NotificationDispatcher(config, self.clock).send_report("daily", str(self.html))
                self.assertTrue(result.sent)
                self.assertEqual(factory.call_args.args, ("smtp.qq.com", 465))
                smtp.data.assert_called_once()

    def test_invalid_headers_envelope_and_port_fail_without_sending(self):
        with patch.object(senders.smtplib, "SMTP_SSL") as factory, patch.object(senders.smtplib, "SMTP") as tls_factory, redirect_stdout(io.StringIO()):
            self.assertIsNone(self.dispatcher.prepare_report("daily", str(self.html), subject_override="x\nBcc: y"))
            self.config["EMAIL_TO"] = "bad-address"
            self.assertIsNone(self.dispatcher.prepare_report("daily", str(self.html)))
            self.config["EMAIL_TO"] = MAIL_CONFIG["EMAIL_TO"]
            self.config["EMAIL_SMTP_PORT"] = "bad-port"
            result = self.dispatcher.send_report("daily", str(self.html))
        self.assertFalse(result.configured)
        self.assertEqual(result.permanent_failed, ADDRESSES)
        factory.assert_not_called()
        tls_factory.assert_not_called()

    def test_dispatcher_has_no_callback_or_mirrored_delivery_state(self):
        self.assertFalse(hasattr(self.dispatcher, "dispatch_all"))
        self.assertFalse(hasattr(self.dispatcher, "email_partially_delivered"))
        with self.assertRaises(TypeError):
            senders.send_to_email("a", "b", "c", "d", "e", on_partial_delivery=lambda: None)


class WeeklySingleEmailTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output_dir = Path(temporary.name) / "weekly-ai-reports"
        self.config = dict(MAIL_CONFIG)

    def run_weekly(self, *flags, config=None, smtp_result=None, real_runtime_env=False, send_now=NOW, real_html=False):
        config = self.config if config is None else config
        self.client = Mock()
        self.client.validate_config.return_value = (True, "")
        self.client.chat.return_value = "synthetic report, not a model request"
        self.client.last_model = "synthetic/fallback-model"
        self.smtp = smtp_server(data_error=smtp_result) if isinstance(smtp_result, Exception) else smtp_server(refusals=smtp_result)
        self.report_clock = Mock(return_value=NOW)
        self.send_clock = Mock(return_value=send_now)
        replacements = {
            "OUTPUT_DIR": self.output_dir, "report_now": self.report_clock, "datetime": Mock(now=self.send_clock),
            "load_ai_config": Mock(return_value={"MODEL": "synthetic/model", "EMAIL_TO": "wrong@example.invalid"}),
            "AIClient": Mock(return_value=self.client), "collect_news": Mock(return_value=([{"title": "synthetic news"}], {}, {})),
            "build_prompt": Mock(return_value=[]), "build_evidence_index": Mock(return_value={}),
            "parse_structured_report": Mock(return_value=("synthetic report", [])),
            "keywords_from_themes": Mock(return_value=["甲", "乙", "丙", "丁", "戊"]),
            "extract_headline_keywords": Mock(side_effect=AssertionError("unexpected keyword model request")),
            "render_weekly_html": Mock(wraps=weekly.render_weekly_html) if real_html else Mock(return_value="<html>synthetic weekly</html>"),
            "build_rule_entity_headlines": Mock(return_value=[]),
        }
        if not real_runtime_env:
            replacements["load_runtime_env"] = Mock(return_value=config)
        argv = ["weekly", "--start", "2026-09-15", "--end", "2026-09-21", *flags]
        output = io.StringIO()
        with (
            patch.multiple(weekly, **replacements), patch.object(sys, "argv", argv),
            patch.object(senders.smtplib, "SMTP_SSL", return_value=self.smtp) as self.smtp_factory,
            patch.object(senders.smtplib, "SMTP", side_effect=AssertionError("unexpected SMTP connection")),
            patch.object(delivery, "send_prepared_email", wraps=senders.send_prepared_email) as self.send,
            redirect_stdout(output),
        ):
            status = weekly.main()
        self.logs = output.getvalue()
        self.client.chat.assert_called_once_with([])
        replacements["extract_headline_keywords"].assert_not_called()
        replacements["render_weekly_html"].assert_called_once()
        self.assertEqual(replacements["render_weekly_html"].call_args.args[2], "synthetic/fallback-model")
        self.assertEqual(replacements["render_weekly_html"].call_args.kwargs["generated_at"], "2026-09-21 07:30:00")
        self.assertEqual(self.report_clock.call_count, 2)
        self.html_path = self.output_dir / "weekly-ai-2026-09-15-to-2026-09-21-20260921-073000.html"
        self.html_content = self.html_path.read_text(encoding="utf-8")
        self.assertIn("synthetic report" if real_html else "synthetic weekly", self.html_content)
        self.assertEqual(json.loads(self.html_path.with_suffix(".keywords.json").read_text(encoding="utf-8"))["final"], ["甲", "乙", "丙", "丁", "戊"])
        return status

    def test_to_and_subject_are_exact_without_mutating_shared_configuration(self):
        original = dict(self.config)
        subject = "  精确主题 <Report> & 周报  "
        self.assertEqual(self.run_weekly("--to", "personal@example.invalid, spare@example.invalid", "--subject", subject, config=MappingProxyType(self.config)), 0)
        self.assertEqual(self.config, original)
        self.send.assert_called_once()
        prepared = self.send.call_args.args[0]
        self.assertEqual(prepared.recipients, ("personal@example.invalid", "spare@example.invalid"))
        self.assertEqual(prepared.subject, subject)
        self.assertEqual(self.send.call_args.kwargs["password"], MAIL_CONFIG["EMAIL_PASSWORD"])
        self.smtp.data.assert_called_once_with(prepared.mime_bytes)
        self.assertEqual([call.args[0] for call in self.smtp.rcpt.call_args_list], list(prepared.recipients))
        msg = parse_payload(prepared.mime_bytes)
        self.assertEqual(parseaddr(msg["From"]), ("AI周报", MAIL_CONFIG["EMAIL_FROM"]))
        self.assertIn(f"报告类型：{subject}\n", msg.get_body(preferencelist=("plain",)).get_content())
        self.assertEqual(msg.get_body(preferencelist=("html",)).get_content(), "<html>synthetic weekly</html>")
        self.assertEqual(self.run_weekly(), 0)
        self.assertEqual(self.send.call_args.args[0].recipients, ADDRESSES)
        self.assertEqual(self.send.call_args.args[0].subject, WEEKLY_SUBJECT)

    def test_send_clock_does_not_replace_report_filename_or_html_clock(self):
        local_now = datetime(2026, 9, 20, 23, 30)
        self.assertEqual(self.run_weekly(send_now=local_now, real_html=True), 0)
        self.assertIn("20260921-073000", self.html_path.name)
        self.assertIn("2026-09-21 07:30:00", self.html_content)
        msg = parse_payload(self.send.call_args.args[0].mime_bytes)
        self.assertIn("生成时间：2026-09-20 23:30:00", msg.get_body(preferencelist=("plain",)).get_content())
        self.assertEqual(msg.get_body(preferencelist=("html",)).get_content(), self.html_content)
        self.send_clock.assert_called_once()

    def test_to_supplies_missing_recipient_without_changing_config(self):
        config = dict(self.config)
        config.pop("EMAIL_TO")
        self.assertEqual(self.run_weekly("--to", "personal@example.invalid", config=MappingProxyType(config)), 0)
        self.assertNotIn("EMAIL_TO", config)
        self.assertEqual(self.send.call_args.args[0].recipients, ("personal@example.invalid",))

    def test_mail_settings_still_come_only_from_email_environment(self):
        with patch.dict(os.environ, dict(self.config, AI_MODEL="unused/model", UNRELATED="ignored", PYTHON_DOTENV_DISABLED="1"), clear=True):
            self.assertEqual(self.run_weekly(real_runtime_env=True), 0)
            self.assertEqual(weekly.load_runtime_env(), self.config)
        self.assertEqual(self.send.call_args.args[0].recipients, ADDRESSES)
        self.assertEqual(self.send.call_args.kwargs["custom_smtp_server"], MAIL_CONFIG["EMAIL_SMTP_SERVER"])

    def test_dry_run_generates_files_without_config_or_email(self):
        for config in ({}, {"EMAIL_SMTP_SERVER": "smtp.example.invalid"}, {"EMAIL_SMTP_PORT": "465"}, self.config):
            with self.subTest(config=config), patch.object(NotificationDispatcher, "send_report") as report_send:
                self.assertEqual(self.run_weekly("--dry-run", config=config), 0)
            report_send.assert_not_called()
            self.send.assert_not_called()
            self.smtp_factory.assert_not_called()
            self.assertIn("dry-run", self.logs)

    def test_missing_or_unpaired_config_returns_three_after_generation(self):
        for key in MAIL_CONFIG:
            config = dict(self.config)
            config.pop(key)
            with self.subTest(key=key):
                self.assertEqual(self.run_weekly(config=config), 3)
                self.assertIn(key, self.logs)
                self.send.assert_not_called()
                self.smtp_factory.assert_not_called()

    def test_missing_credentials_take_priority_over_unpaired_smtp(self):
        for credential in ("EMAIL_FROM", "EMAIL_PASSWORD", "EMAIL_TO"):
            for smtp_key in ("EMAIL_SMTP_SERVER", "EMAIL_SMTP_PORT"):
                config = dict(self.config)
                config.pop(credential)
                config.pop(smtp_key)
                self.assertEqual(self.run_weekly(config=config), 3)
                self.assertIn(credential, self.logs)
                self.assertNotIn(smtp_key, self.logs)
                self.smtp_factory.assert_not_called()

    def test_omitting_both_smtp_overrides_keeps_provider_defaults(self):
        config = dict(self.config, EMAIL_FROM="synthetic-sender@qq.com")
        config.pop("EMAIL_SMTP_SERVER")
        config.pop("EMAIL_SMTP_PORT")
        self.assertEqual(self.run_weekly(config=config), 0)
        self.assertEqual(self.smtp_factory.call_args.args, ("smtp.qq.com", 465))

    def test_success_failure_partial_and_unknown_do_not_hidden_retry(self):
        for outcome, expected in (({}, 0), (smtplib.SMTPDataError(554, b"private"), 4),
                                  ({ADDRESSES[1]: (451, b"private")}, 6),
                                  (smtplib.SMTPServerDisconnected("private"), 6)):
            with self.subTest(expected=expected):
                self.assertEqual(self.run_weekly(smtp_result=outcome), expected)
                self.send.assert_called_once()
                self.smtp_factory.assert_called_once()
                self.smtp.data.assert_called_once()


class CliSingleEmailTests(unittest.TestCase):
    def test_missing_config_never_creates_html_and_cleans_up(self):
        for key in ("EMAIL_FROM", "EMAIL_PASSWORD", "EMAIL_TO"):
            config = dict(MAIL_CONFIG)
            config.pop(key)
            ctx = Mock()
            with patch.object(cli, "AppContext", return_value=ctx), patch.object(cli, "_create_test_html_file") as create, patch.object(NotificationDispatcher, "send_report") as send, redirect_stdout(io.StringIO()):
                self.assertFalse(cli._run_test_notification(config))
            create.assert_not_called()
            send.assert_not_called()
            ctx.cleanup.assert_called_once()

    def test_named_result_handles_full_partial_unknown_and_failure_with_cleanup(self):
        cases = (
            (EmailDeliveryResult(False), False, "配置不完整"),
            (EmailDeliveryResult(True, ADDRESSES, ADDRESSES), True, "邮件测试成功"),
            (EmailDeliveryResult(True, ADDRESSES, permanent_failed=ADDRESSES), False, "邮件测试失败"),
            (EmailDeliveryResult(True, ADDRESSES, ADDRESSES[:1], ADDRESSES[1:]), False, "不自动重发"),
            (EmailDeliveryResult(True, ADDRESSES, unknown=ADDRESSES), False, "提交结果未知"),
        )
        for result, expected, text in cases:
            ctx, output = Mock(), io.StringIO()
            with patch.object(cli, "AppContext", return_value=ctx), patch.object(cli, "_create_test_html_file", return_value="test.html"), patch.object(NotificationDispatcher, "send_report", return_value=result) as send, redirect_stdout(output):
                self.assertEqual(cli._run_test_notification(dict(MAIL_CONFIG)), expected)
            send.assert_called_once_with(report_type="通知连通性测试", html_file_path="test.html")
            ctx.cleanup.assert_called_once()
            self.assertIn(text, output.getvalue())

    def test_html_failure_never_sends_and_cleans_up(self):
        ctx = Mock()
        with patch.object(cli, "AppContext", return_value=ctx), patch.object(cli, "_create_test_html_file", return_value=None), patch.object(delivery, "send_prepared_email") as send, redirect_stdout(io.StringIO()):
            self.assertFalse(cli._run_test_notification(dict(MAIL_CONFIG)))
        send.assert_not_called()
        ctx.cleanup.assert_called_once()

    def test_command_retains_success_and_failure_exit_codes(self):
        for sent in (True, False):
            with patch.object(cli, "load_config", return_value={}), patch.object(cli, "_run_test_notification", return_value=sent), patch.object(sys, "argv", ["trendradar", "--test-notification"]):
                if sent:
                    self.assertEqual(cli.main(), 0)
                else:
                    with self.assertRaises(SystemExit) as stopped:
                        cli.main()
                    self.assertEqual(stopped.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
