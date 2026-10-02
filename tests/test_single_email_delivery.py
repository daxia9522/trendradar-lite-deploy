"""One report email, explicit delivery results, and unchanged weekly CLI contracts.

All report data, AI calls and SMTP connections are synthetic. SMTP MIME creation
is exercised through the real sender without contacting an email service.
"""
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
from email.header import decode_header, make_header
from email.utils import parseaddr
from pathlib import Path
from types import MappingProxyType
from unittest.mock import Mock, patch

from trendradar import cli
from trendradar.notification import EmailDeliveryResult, NotificationDispatcher
from trendradar.notification import dispatcher as delivery
from trendradar.notification import senders
from weekly_report import weekly_ai_report_email as weekly


NOW = datetime(2026, 9, 21, 7, 30)
MAIL_CONFIG = {
    "EMAIL_FROM": "sender@example.invalid",
    "EMAIL_PASSWORD": "synthetic-password",
    "EMAIL_TO": "one@example.invalid,two@example.invalid",
    "EMAIL_SMTP_SERVER": "smtp.example.invalid",
    "EMAIL_SMTP_PORT": "465",
}
WEEKLY_SUBJECT = "AI周报（2026-09-15 ~ 2026-09-21）"


class SingleEmailDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.config = dict(MAIL_CONFIG)
        self.clock = Mock(return_value=NOW)
        self.dispatcher = NotificationDispatcher(self.config, self.clock)

    def test_result_is_immutable_and_requires_explicit_fields(self):
        result = EmailDeliveryResult(configured=True, sent=False, partially_delivered=True)
        with self.assertRaises(TypeError):
            bool(result)
        with self.assertRaises(FrozenInstanceError):
            result.sent = True
        self.assertEqual((result.configured, result.sent, result.partially_delivered), (True, False, True))

    def test_daily_labels_and_legacy_recipients_use_one_sender_call(self):
        with patch.object(delivery, "send_to_email", return_value=True) as send:
            result = self.dispatcher.send_report("current", "report.html", period_name=" 早间速览 ")
        self.assertEqual(result, EmailDeliveryResult(True, True, False))
        send.assert_called_once()
        kwargs = send.call_args.kwargs
        self.assertEqual(kwargs["from_email"], self.config["EMAIL_FROM"])
        self.assertEqual(kwargs["password"], self.config["EMAIL_PASSWORD"])
        self.assertEqual(kwargs["to_email"], self.config["EMAIL_TO"])
        self.assertEqual(kwargs["report_type"], "current")
        self.assertEqual(kwargs["html_file_path"], "report.html")
        self.assertEqual(kwargs["custom_smtp_server"], self.config["EMAIL_SMTP_SERVER"])
        self.assertEqual(kwargs["custom_smtp_port"], 465)
        self.assertEqual(kwargs["subject_override"], "早间速览 · 09月21日 07:30")
        self.assertEqual(kwargs["sender_name_override"], "早间速览")
        self.assertIs(kwargs["get_time_func"], self.clock)
        self.assertTrue(callable(kwargs["on_partial_delivery"]))

    def test_subject_and_sender_overrides_do_not_replace_plaintext_report_type(self):
        subject = "  精确主题 <周报> & 分析  "
        with patch.object(delivery, "send_to_email", return_value=True) as send:
            result = self.dispatcher.send_report(
                "plaintext label", "report.html", period_name="ignored label",
                subject_override=subject, sender_name_override="AI周报",
            )
        self.assertTrue(result.sent)
        self.assertEqual(send.call_args.kwargs["subject_override"], subject)
        self.assertEqual(send.call_args.kwargs["sender_name_override"], "AI周报")
        self.assertEqual(send.call_args.kwargs["report_type"], "plaintext label")

    def test_missing_credentials_never_call_sender(self):
        for key in ("EMAIL_FROM", "EMAIL_PASSWORD", "EMAIL_TO"):
            with self.subTest(missing=key):
                config = dict(MAIL_CONFIG)
                config.pop(key)
                dispatcher = NotificationDispatcher(config, self.clock)
                with patch.object(delivery, "send_to_email") as send, redirect_stdout(io.StringIO()):
                    result = dispatcher.send_report("daily", "report.html")
                self.assertEqual(result, EmailDeliveryResult(False, False, False))
                send.assert_not_called()
        self.clock.assert_not_called()

    def test_omitting_both_smtp_overrides_keeps_provider_defaults(self):
        self.config.pop("EMAIL_SMTP_SERVER")
        self.config.pop("EMAIL_SMTP_PORT")
        with patch.object(delivery, "send_to_email", return_value=True) as send:
            result = self.dispatcher.send_report("daily", "report.html")
        self.assertTrue(result.configured)
        self.assertTrue(result.sent)
        self.assertIsNone(send.call_args.kwargs["custom_smtp_server"])
        self.assertIsNone(send.call_args.kwargs["custom_smtp_port"])

    def test_daily_unpaired_smtp_reaches_one_sender_and_keeps_provider_defaults(self):
        with tempfile.TemporaryDirectory() as temporary:
            html_path = Path(temporary) / "report.html"
            html_path.write_text("<html>synthetic daily</html>", encoding="utf-8")
            for missing in ("EMAIL_SMTP_SERVER", "EMAIL_SMTP_PORT"):
                with self.subTest(missing=missing):
                    # Either lone override must be ignored by the provider fallback.
                    config = dict(MAIL_CONFIG, EMAIL_FROM="synthetic-sender@qq.com", EMAIL_SMTP_PORT="587")
                    config.pop(missing)
                    dispatcher = NotificationDispatcher(config, self.clock)
                    smtp = Mock(spec=smtplib.SMTP_SSL)
                    smtp.send_message.return_value = {}
                    with (
                        patch.object(delivery, "send_to_email", wraps=senders.send_to_email) as send,
                        patch.object(senders, "make_msgid", return_value="<synthetic-daily@example.invalid>"),
                        patch.object(senders.smtplib, "SMTP_SSL", return_value=smtp) as smtp_factory,
                        patch.object(senders.smtplib, "SMTP", side_effect=AssertionError("unexpected SMTP connection")),
                        patch.object(senders.time, "sleep") as sleep,
                        redirect_stdout(io.StringIO()),
                    ):
                        result = dispatcher.send_report("daily", str(html_path))
                    self.assertEqual(result, EmailDeliveryResult(True, True, False))
                    send.assert_called_once()
                    self.assertEqual(send.call_args.kwargs["custom_smtp_server"], config.get("EMAIL_SMTP_SERVER"))
                    self.assertEqual(send.call_args.kwargs["custom_smtp_port"], 587 if config.get("EMAIL_SMTP_PORT") else None)
                    smtp_factory.assert_called_once()
                    self.assertEqual(smtp_factory.call_args.args, ("smtp.qq.com", 465))
                    smtp.send_message.assert_called_once()
                    sleep.assert_not_called()

    def test_missing_html_is_delivery_failure_not_missing_configuration(self):
        with patch.object(delivery, "send_to_email") as send, redirect_stdout(io.StringIO()):
            result = self.dispatcher.send_report("daily")
        self.assertEqual(result, EmailDeliveryResult(True, False, False))
        send.assert_not_called()
        self.clock.assert_not_called()

    def test_delivery_results_are_independent_for_every_call(self):
        responses = iter(("partial", "success", "failed", "partial", "partial"))

        def send_once(**kwargs):
            response = next(responses)
            if response == "partial":
                kwargs["on_partial_delivery"]()
            return response == "success"

        with patch.object(delivery, "send_to_email", side_effect=send_once) as send:
            results = []
            for _ in range(4):
                results.append(self.dispatcher.send_report("daily", "report.html"))
            with redirect_stdout(io.StringIO()):
                missing_html = self.dispatcher.send_report("daily")
            partial = self.dispatcher.send_report("daily", "report.html")
            self.config.clear()
            with redirect_stdout(io.StringIO()):
                unconfigured = self.dispatcher.send_report("daily", "report.html")
        self.assertEqual(send.call_count, 5)
        self.assertEqual([r.sent for r in results], [False, True, False, False])
        self.assertEqual([r.partially_delivered for r in results], [True, False, False, True])
        self.assertEqual(missing_html, EmailDeliveryResult(True, False, False))
        self.assertEqual(unconfigured, EmailDeliveryResult(False, False, False))
        self.assertTrue(partial.partially_delivered)
        self.assertIsNot(results[0], results[3])

    def test_dispatcher_has_no_legacy_channel_api_or_mirrored_delivery_state(self):
        self.assertFalse(hasattr(self.dispatcher, "dispatch_all"))
        self.assertFalse(hasattr(self.dispatcher, "email_partially_delivered"))
        with patch.object(delivery, "send_to_email", return_value=True):
            self.assertTrue(self.dispatcher.send_report("daily", "report.html").sent)
        self.assertFalse(hasattr(self.dispatcher, "email_partially_delivered"))


class WeeklySingleEmailTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output_dir = Path(temporary.name) / "weekly-ai-reports"
        self.config = dict(MAIL_CONFIG)

    def run_weekly(self, *flags, config=None, smtp_result=None, real_runtime_env=False,
                   send_now=NOW, real_html=False):
        config = self.config if config is None else config
        self.client = Mock()
        self.client.validate_config.return_value = (True, "")
        self.client.chat.return_value = "synthetic report, not a model request"
        self.client.last_model = "synthetic/fallback-model"
        self.smtp = Mock(spec=smtplib.SMTP_SSL)
        if isinstance(smtp_result, Exception):
            self.smtp.send_message.side_effect = smtp_result
        else:
            self.smtp.send_message.return_value = {} if smtp_result is None else smtp_result
        self.report_clock = Mock(return_value=NOW)
        self.send_clock = Mock(return_value=send_now)
        replacements = {
            "OUTPUT_DIR": self.output_dir,
            "report_now": self.report_clock,
            "datetime": Mock(now=self.send_clock),
            "load_ai_config": Mock(return_value={"MODEL": "synthetic/model", "EMAIL_TO": "wrong@example.invalid"}),
            "AIClient": Mock(return_value=self.client),
            "collect_news": Mock(return_value=([{"title": "synthetic news"}], {}, {})),
            "build_prompt": Mock(return_value=[]),
            "build_evidence_index": Mock(return_value={}),
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
            patch.multiple(weekly, **replacements),
            patch.object(sys, "argv", argv),
            patch.object(senders, "make_msgid", return_value="<synthetic-weekly@example.invalid>"),
            patch.object(senders.smtplib, "SMTP_SSL", return_value=self.smtp) as self.smtp_factory,
            patch.object(senders.smtplib, "SMTP", side_effect=AssertionError("unexpected SMTP connection")),
            patch.object(delivery, "send_to_email", wraps=senders.send_to_email) as self.send,
            patch.object(senders.time, "sleep") as self.sleep,
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
        self.sleep.assert_not_called()
        self.html_path = self.output_dir / "weekly-ai-2026-09-15-to-2026-09-21-20260921-073000.html"
        self.html_content = self.html_path.read_text(encoding="utf-8")
        if real_html:
            self.assertIn("synthetic report", self.html_content)
        else:
            self.assertEqual(self.html_content, "<html>synthetic weekly</html>")
        keywords_path = self.html_path.with_suffix(".keywords.json")
        self.assertEqual(json.loads(keywords_path.read_text(encoding="utf-8"))["final"], ["甲", "乙", "丙", "丁", "戊"])
        return status

    def test_to_and_subject_are_exact_and_do_not_mutate_shared_configuration(self):
        original = dict(self.config)
        subject = "  精确主题 <Report> & 周报  "
        recipients = "personal@example.invalid, spare@example.invalid"
        # A read-only shared mapping makes an accidental in-place --to update fail.
        self.assertEqual(self.run_weekly("--to", recipients, "--subject", subject,
                                         config=MappingProxyType(self.config)), 0)
        self.assertEqual(self.config, original)
        self.send.assert_called_once()
        kwargs = self.send.call_args.kwargs
        self.assertEqual(kwargs["to_email"], recipients)
        self.assertEqual(kwargs["from_email"], self.config["EMAIL_FROM"])
        self.assertEqual(kwargs["password"], self.config["EMAIL_PASSWORD"])
        self.assertEqual(kwargs["subject_override"], subject)
        self.assertEqual(kwargs["sender_name_override"], "AI周报")
        self.assertEqual(kwargs["report_type"], subject)
        self.assertEqual(kwargs["html_file_path"], str(self.html_path))
        self.assertIs(kwargs["get_time_func"], self.send_clock)
        self.smtp_factory.assert_called_once()
        self.smtp.send_message.assert_called_once()
        self.assertEqual(self.smtp.send_message.call_args.kwargs["to_addrs"],
                         ["personal@example.invalid", "spare@example.invalid"])
        message = self.smtp.send_message.call_args.args[0]
        self.assertEqual(str(message["Subject"]), subject)
        display_name, address = parseaddr(message["From"])
        self.assertEqual(str(make_header(decode_header(display_name))), "AI周报")
        self.assertEqual(address, self.config["EMAIL_FROM"])
        plain, html = message.get_payload()
        self.assertEqual([plain.get_content_type(), html.get_content_type()], ["text/plain", "text/html"])
        text = plain.get_payload(decode=True).decode("utf-8")
        self.assertIn("AI周报 热点分析报告", text)
        self.assertIn(f"报告类型：{subject}\n", text)
        self.assertIn("生成时间：2026-09-21 07:30:00", text)
        self.assertEqual(html.get_payload(decode=True).decode("utf-8"), "<html>synthetic weekly</html>")

        self.assertEqual(self.run_weekly(), 0)
        self.assertEqual(self.send.call_args.kwargs["to_email"], original["EMAIL_TO"])
        self.assertEqual(self.send.call_args.kwargs["subject_override"], WEEKLY_SUBJECT)
        self.assertEqual(self.send.call_args.kwargs["report_type"], WEEKLY_SUBJECT)

    def test_local_send_clock_does_not_replace_report_filename_or_html_clock(self):
        local_now = datetime(2026, 9, 20, 23, 30)
        date_header = "Mon, 21 Sep 2026 00:01:02 +0000"
        with patch.object(senders, "formatdate", return_value=date_header) as format_date:
            self.assertEqual(self.run_weekly(send_now=local_now, real_html=True), 0)
        self.send.assert_called_once()
        self.assertIs(self.send.call_args.kwargs["get_time_func"], self.send_clock)
        self.send_clock.assert_called()
        self.assertIn("20260921-073000", self.html_path.name)
        self.assertIn("2026-09-21 07:30:00", self.html_content)
        self.assertNotIn("2026-09-20 23:30:00", self.html_content)
        message = self.smtp.send_message.call_args.args[0]
        plain, html = message.get_payload()
        text = plain.get_payload(decode=True).decode("utf-8")
        self.assertIn("生成时间：2026-09-20 23:30:00", text)
        self.assertNotIn("2026-09-21 07:30:00", text)
        self.assertEqual(html.get_payload(decode=True).decode("utf-8"), self.html_content)
        format_date.assert_called_once_with(localtime=True)
        self.assertEqual(message["Date"], date_header)

    def test_to_can_supply_a_missing_recipient_without_changing_runtime_configuration(self):
        config = dict(self.config)
        config.pop("EMAIL_TO")
        self.assertEqual(self.run_weekly("--to", "personal@example.invalid",
                                         config=MappingProxyType(config)), 0)
        self.assertNotIn("EMAIL_TO", config)
        self.assertEqual(self.send.call_args.kwargs["to_email"], "personal@example.invalid")

    def test_mail_settings_still_come_only_from_email_environment(self):
        environment = dict(self.config, AI_MODEL="unused/model", UNRELATED="ignored", PYTHON_DOTENV_DISABLED="1")
        with patch.dict(os.environ, environment, clear=True):
            self.assertEqual(self.run_weekly(real_runtime_env=True), 0)
            self.assertEqual(weekly.load_runtime_env(), self.config)
        self.assertEqual(self.send.call_args.kwargs["to_email"], self.config["EMAIL_TO"])
        self.assertEqual(self.send.call_args.kwargs["custom_smtp_server"], self.config["EMAIL_SMTP_SERVER"])
        self.assertEqual(self.send.call_args.kwargs["custom_smtp_port"], 465)

    def test_dry_run_generates_files_without_any_email_configuration_or_send(self):
        for config in ({}, {"EMAIL_SMTP_SERVER": "smtp.example.invalid"},
                       {"EMAIL_SMTP_PORT": "465"}, dict(self.config)):
            with self.subTest(config=config):
                with patch.object(NotificationDispatcher, "send_report") as report_send:
                    self.assertEqual(self.run_weekly("--dry-run", config=config), 0)
                report_send.assert_not_called()
                self.send.assert_not_called()
                self.smtp_factory.assert_not_called()
                self.assertIn("dry-run", self.logs)

    def test_missing_or_unpaired_mail_configuration_returns_three_after_generation(self):
        for key in MAIL_CONFIG:
            with self.subTest(missing=key):
                config = dict(self.config)
                config.pop(key)
                self.assertEqual(self.run_weekly(config=config), 3)
                self.assertIn(key, self.logs)
                self.send.assert_not_called()
                self.smtp_factory.assert_not_called()

    def test_missing_credentials_take_priority_over_unpaired_smtp(self):
        for credential in ("EMAIL_FROM", "EMAIL_PASSWORD", "EMAIL_TO"):
            for smtp_key in ("EMAIL_SMTP_SERVER", "EMAIL_SMTP_PORT"):
                with self.subTest(credential=credential, smtp_key=smtp_key):
                    config = dict(self.config)
                    config.pop(credential)
                    config.pop(smtp_key)
                    self.assertEqual(self.run_weekly(config=config), 3)
                    self.assertIn(credential, self.logs)
                    self.assertNotIn(smtp_key, self.logs)
                    self.send.assert_not_called()
                    self.smtp_factory.assert_not_called()

    def test_omitting_both_smtp_overrides_keeps_weekly_provider_defaults(self):
        config = dict(self.config, EMAIL_FROM="synthetic-sender@qq.com")
        config.pop("EMAIL_SMTP_SERVER")
        config.pop("EMAIL_SMTP_PORT")
        self.assertEqual(self.run_weekly(config=config), 0)
        self.send.assert_called_once()
        self.assertIsNone(self.send.call_args.kwargs["custom_smtp_server"])
        self.assertIsNone(self.send.call_args.kwargs["custom_smtp_port"])
        self.smtp_factory.assert_called_once()
        self.assertEqual(self.smtp_factory.call_args.args, ("smtp.qq.com", 465))

    def test_success_failure_and_partial_delivery_keep_exit_codes_without_whole_mail_retry(self):
        cases = (
            ({}, 0),
            (smtplib.SMTPDataError(554, b"synthetic rejection"), 4),
            ({"two@example.invalid": (451, b"synthetic temporary rejection")}, 6),
        )
        for smtp_result, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(self.run_weekly(smtp_result=smtp_result), expected)
                self.send.assert_called_once()
                self.smtp_factory.assert_called_once()
                self.smtp.send_message.assert_called_once()
                self.assertEqual(self.smtp.send_message.call_args.kwargs["to_addrs"],
                                 ["one@example.invalid", "two@example.invalid"])


class CliSingleEmailTests(unittest.TestCase):
    def test_missing_configuration_does_not_create_html_or_send_and_still_cleans_up(self):
        configs = [{}]
        for credential in ("EMAIL_FROM", "EMAIL_PASSWORD", "EMAIL_TO"):
            config = dict(MAIL_CONFIG)
            config.pop(credential)
            configs.append(config)
        for config in configs:
            with self.subTest(config=config):
                ctx = Mock()
                with (
                    patch.object(cli, "AppContext", return_value=ctx),
                    patch.object(cli, "_create_test_html_file") as create_html,
                    patch.object(NotificationDispatcher, "send_report") as send,
                    redirect_stdout(io.StringIO()),
                ):
                    self.assertFalse(cli._run_test_notification(config))
                create_html.assert_not_called()
                send.assert_not_called()
                ctx.cleanup.assert_called_once_with()

    def test_notification_test_uses_explicit_single_email_result_and_cleans_up(self):
        cases = (
            (EmailDeliveryResult(False, False, False), False, "配置不完整"),
            (EmailDeliveryResult(True, True, False), True, "邮件测试成功"),
            (EmailDeliveryResult(True, False, False), False, "邮件测试失败"),
            (EmailDeliveryResult(True, False, True), False, "不自动重发"),
        )
        for result, expected, text in cases:
            with self.subTest(result=result):
                ctx = Mock()
                output = io.StringIO()
                with patch.object(cli, "AppContext", return_value=ctx):
                    with patch.object(cli, "_create_test_html_file", return_value="test.html"):
                        with patch.object(NotificationDispatcher, "send_report", return_value=result) as send:
                            with redirect_stdout(output):
                                actual = cli._run_test_notification(dict(MAIL_CONFIG))
                self.assertEqual(actual, expected)
                send.assert_called_once_with(report_type="通知连通性测试", html_file_path="test.html")
                ctx.cleanup.assert_called_once_with()
                self.assertIn(text, output.getvalue())
                self.assertNotIn("个渠道", output.getvalue())

    def test_html_failure_does_not_send_and_still_cleans_up(self):
        ctx = Mock()
        with patch.object(cli, "AppContext", return_value=ctx):
            with patch.object(cli, "_create_test_html_file", return_value=None):
                with patch.object(delivery, "send_to_email") as send, redirect_stdout(io.StringIO()):
                    self.assertFalse(cli._run_test_notification(dict(MAIL_CONFIG)))
        send.assert_not_called()
        ctx.cleanup.assert_called_once_with()

    def test_test_notification_command_retains_success_and_failure_exit_codes(self):
        for sent in (True, False):
            with self.subTest(sent=sent):
                with patch.object(cli, "load_config", return_value={}):
                    with patch.object(cli, "_run_test_notification", return_value=sent):
                        with patch.object(sys, "argv", ["trendradar", "--test-notification"]):
                            if sent:
                                self.assertEqual(cli.main(), 0)
                            else:
                                with self.assertRaises(SystemExit) as stopped:
                                    cli.main()
                                self.assertEqual(stopped.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
