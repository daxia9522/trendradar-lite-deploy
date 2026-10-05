"""Offline weekly AI log-boundary regressions; all private values are synthetic.

The real AIClient runs against mock completion. Imports skip the local config;
network/DNS and mail transports are denied, environment/config/history are
replaced, and generated reports stay in a temporary directory.
"""
import io
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
_original_exists = Path.exists
DENIED_TARGETS = (
    "socket.create_connection", "socket.socket.connect", "socket.socket.connect_ex",
    "socket.getaddrinfo", "socket.socket.sendto", "socket.socket.sendmsg",
    "smtplib.SMTP", "smtplib.SMTP_SSL",
)


def _offline_exists(path):
    # collection and runtime normally read config.yaml at import time.
    return False if path.absolute() == CONFIG_PATH else _original_exists(path)


with ExitStack() as imports:
    imports.enter_context(patch.dict(os.environ, {"LITELLM_LOCAL_MODEL_COST_MAP": "True"}, clear=True))
    imports.enter_context(patch.object(Path, "exists", _offline_exists))
    for target in DENIED_TARGETS:
        imports.enter_context(patch(target, side_effect=AssertionError("offline import attempted I/O")))
    from trendradar.ai.client import AIClient
    from weekly_report import keywords
    from weekly_report import weekly_ai_report_email as weekly


MODEL = "openai/synthetic-weekly-model"
PRIVATE_KEY = "SYNTHETIC_CREDENTIAL_MUST_NOT_APPEAR"
PRIVATE_URL = "https://weekly-relay.example.invalid/private-api"
PRIVATE_REQUEST = "SYNTHETIC_REQUEST_BODY_MUST_NOT_APPEAR"
PRIVATE_RESPONSE = "SYNTHETIC_RESPONSE_BODY_MUST_NOT_APPEAR"
PRIVATE_BODY = (
    f"POST {PRIVATE_URL}?api_key={PRIVATE_KEY}\n"
    f"Authorization: Bearer {PRIVATE_KEY}\n"
    f"request={PRIVATE_REQUEST} response={PRIVATE_RESPONSE}"
)
NEWS = [
    {"title": "日本地震救援继续", "source_type": "news"},
    {"title": "日本地震展开救援", "source_type": "news"},
]
REPORT = "# 合成周报\n\n日本地震救援继续。火箭发射、英伟达、量子计算、宇树科技。"
LABELS = ["日本地震", "火箭发射", "英伟达", "量子计算", "宇树科技"]


class SyntheticHTTPError(Exception):
    status_code = 401


class UnprintableError(Exception):
    def __str__(self):
        raise AssertionError("exception str must not be evaluated")

    def __repr__(self):
        raise AssertionError("exception repr must not be evaluated")


def response(content):
    return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}


class WeeklyAILoggingTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {"LITELLM_LOCAL_MODEL_COST_MAP": "True"}, clear=True))
        self.guards = [self._patch(target, side_effect=AssertionError("offline test attempted I/O"))
                       for target in DENIED_TARGETS]
        self.guards.append(self._patch(
            "trendradar.notification.dispatcher.send_prepared_email",
            side_effect=AssertionError("real mail entrypoint is forbidden"),
        ))
        self.output = io.StringIO()
        self.stack.enter_context(redirect_stdout(self.output))
        self.stack.enter_context(redirect_stderr(self.output))
        temporary = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.output_dir = Path(temporary) / "weekly-ai-reports"
        self.config = {
            "MODEL": MODEL, "API_KEY": PRIVATE_KEY, "API_BASE": PRIVATE_URL,
            "TIMEOUT": 1, "NUM_RETRIES": 0, "FALLBACK_MODELS": [],
        }
        self.env = {
            "EMAIL_FROM": "sender@example.invalid", "EMAIL_TO": "reader@example.invalid",
            "EMAIL_PASSWORD": "synthetic-mail-password",
        }
        self.completion = self._patch("trendradar.ai.client.completion", return_value=response(REPORT))
        self.sleep = self._patch("trendradar.ai.client.time.sleep")
        self._patch("trendradar.ai.client.random.uniform", return_value=0)
        self.dispatcher = Mock()
        self.dispatcher.send_report.return_value = SimpleNamespace(
            configured=True, sent=True, partially_delivered=False, unknown=(),
        )
        self.factory = self._patch(
            "weekly_report.weekly_ai_report_email.NotificationDispatcher",
            return_value=self.dispatcher,
        )
        self._patch("weekly_report.weekly_ai_report_email.OUTPUT_DIR", self.output_dir)
        self._patch("weekly_report.weekly_ai_report_email.load_runtime_env", return_value=self.env)
        self.load_config = self._patch(
            "weekly_report.weekly_ai_report_email.load_ai_config", return_value=self.config,
        )
        self.collect = self._patch(
            "weekly_report.weekly_ai_report_email.collect_news", return_value=(NEWS, {}, {}),
        )
        self._patch("weekly_report.weekly_ai_report_email.report_now", return_value=datetime(2026, 10, 3, 20))
        self.messages = [{"role": "user", "content": "synthetic weekly prompt"}]
        self._patch("weekly_report.weekly_ai_report_email.build_prompt", return_value=self.messages)
        # Most main() tests avoid a second AI branch; selected tests enable it below.
        self.themes = self._patch(
            "weekly_report.weekly_ai_report_email.keywords_from_themes", return_value=LABELS.copy(),
        )

    def tearDown(self):
        for guard in self.guards:
            guard.assert_not_called()

    def _patch(self, target, *args, **kwargs):
        return self.stack.enter_context(patch(target, *args, **kwargs))

    def _main(self, *flags):
        argv = ["weekly", "--start", "2026-09-27", "--end", "2026-10-03", *flags]
        with patch.object(sys, "argv", argv):
            return weekly.main()

    def _keywords(self):
        return keywords.extract_headline_keywords(
            AIClient(self.config), "2026-09-27", "2026-10-03", NEWS, REPORT,
        )

    def _assert_private_absent(self):
        logs = self.output.getvalue()
        for marker in (PRIVATE_KEY, PRIVATE_URL, PRIVATE_REQUEST, PRIVATE_RESPONSE,
                       "Authorization:", "Traceback (most recent call last)"):
            self.assertNotIn(marker, logs)
        for path in self.output_dir.glob("*"):
            for marker in (PRIVATE_KEY, PRIVATE_URL, PRIVATE_REQUEST, PRIVATE_RESPONSE):
                self.assertNotIn(marker, path.read_text(encoding="utf-8"))

    def _assert_no_delivery_or_files(self):
        self.factory.assert_not_called()
        self.dispatcher.send_report.assert_not_called()
        self.assertFalse(self.output_dir.exists())

    def test_report_http_error_is_classified_in_both_attempts_and_final_log(self):
        self.completion.side_effect = SyntheticHTTPError(PRIVATE_BODY)
        self.assertEqual(self._main(), 5)
        self.assertEqual(self.completion.call_count, 2)
        for attempt in (1, 2):
            self.assertIn(f"[周报正文] 调用失败 attempt={attempt}: SyntheticHTTPError\n", self.output.getvalue())
        self.assertIn("周报正文生成失败: SyntheticHTTPError\n", self.output.getvalue())
        self._assert_private_absent()
        self._assert_no_delivery_or_files()
        self.sleep.assert_not_called()

    def test_report_plain_error_is_classified_without_body(self):
        self.completion.side_effect = RuntimeError(PRIVATE_BODY)
        self.assertEqual(self._main("--dry-run"), 5)
        self.assertEqual(self.completion.call_count, 2)
        self.assertIn("周报正文生成失败: RuntimeError\n", self.output.getvalue())
        self._assert_private_absent()
        self._assert_no_delivery_or_files()

    def test_report_never_formats_exception_str_or_repr(self):
        self.completion.side_effect = UnprintableError(PRIVATE_BODY)
        self.assertEqual(self._main(), 5)
        self.assertEqual(self.completion.call_count, 2)
        self.assertIn("周报正文生成失败: UnprintableError\n", self.output.getvalue())
        self._assert_private_absent()
        self._assert_no_delivery_or_files()

    def test_report_second_attempt_success_still_delivers_once(self):
        self.completion.side_effect = [SyntheticHTTPError(PRIVATE_BODY), response(REPORT)]
        self.assertEqual(self._main(), 0)
        self.assertEqual(self.completion.call_count, 2)
        self.assertEqual(self.completion.call_args_list[0], self.completion.call_args_list[1])
        self.dispatcher.send_report.assert_called_once()
        self.assertEqual(len(list(self.output_dir.glob("*"))), 2)
        self.assertNotIn("周报正文生成失败", self.output.getvalue())
        self._assert_private_absent()

    def test_report_retry_success_dry_run_does_not_deliver(self):
        self.completion.side_effect = [RuntimeError(PRIVATE_BODY), response(REPORT)]
        self.assertEqual(self._main("--dry-run"), 0)
        self.assertEqual(self.completion.call_count, 2)
        self.factory.assert_not_called()
        self.assertEqual(len(list(self.output_dir.glob("*"))), 2)
        self._assert_private_absent()

    def test_report_first_success_uses_one_completion(self):
        self.assertEqual(self._main(), 0)
        self.completion.assert_called_once()
        self.dispatcher.send_report.assert_called_once()
        self._assert_private_absent()

    def test_report_empty_responses_keep_two_attempts_and_exit_five(self):
        self.completion.return_value = response("  \n ")
        self.assertEqual(self._main(), 5)
        self.assertEqual(self.completion.call_count, 2)
        self.assertIn("[周报正文] 空响应 attempt=1", self.output.getvalue())
        self.assertIn("[周报正文] 空响应 attempt=2", self.output.getvalue())
        self.assertIn("周报正文生成失败: empty_response", self.output.getvalue())
        self._assert_no_delivery_or_files()

    def test_report_exception_then_empty_records_last_failure(self):
        self.completion.side_effect = [RuntimeError(PRIVATE_BODY), response("")]
        self.assertEqual(self._main(), 5)
        self.assertEqual(self.completion.call_count, 2)
        self.assertIn("周报正文生成失败: empty_response", self.output.getvalue())
        self._assert_no_delivery_or_files()
        self._assert_private_absent()

    def test_report_empty_then_exception_records_last_failure(self):
        self.completion.side_effect = [response(""), SyntheticHTTPError(PRIVATE_BODY)]
        self.assertEqual(self._main(), 5)
        self.assertEqual(self.completion.call_count, 2)
        self.assertIn("周报正文生成失败: SyntheticHTTPError\n", self.output.getvalue())
        self._assert_no_delivery_or_files()
        self._assert_private_absent()

    def test_report_empty_then_success_still_delivers(self):
        self.completion.side_effect = [response(""), response(REPORT)]
        self.assertEqual(self._main(), 0)
        self.assertEqual(self.completion.call_count, 2)
        self.dispatcher.send_report.assert_called_once()

    def test_keyword_errors_keep_two_outer_attempts_and_rule_fallback(self):
        self.completion.side_effect = SyntheticHTTPError(PRIVATE_BODY)
        labels, source = self._keywords()
        self.assertEqual(source, "rule_only")
        self.assertTrue(labels)
        # The keyword client retains its existing one inner retry per outer attempt.
        self.assertEqual(self.completion.call_count, 4)
        self.assertEqual(self.sleep.call_count, 2)
        for attempt in (1, 2):
            self.assertIn(f"[关键词] lite 抽取失败 attempt={attempt}: SyntheticHTTPError\n", self.output.getvalue())
        self.assertIn("[关键词] 回退规则实体 Top5（SyntheticHTTPError）", self.output.getvalue())
        self._assert_private_absent()
        self._assert_no_delivery_or_files()

    def test_keyword_never_formats_exception_str_or_repr(self):
        self.completion.side_effect = UnprintableError(PRIVATE_BODY)
        labels, source = self._keywords()
        self.assertEqual(source, "rule_only")
        self.assertTrue(labels)
        self.assertEqual(self.completion.call_count, 4)
        self.assertIn("[关键词] 回退规则实体 Top5（UnprintableError）", self.output.getvalue())
        self.assertNotIn("AssertionError", self.output.getvalue())
        self._assert_private_absent()

    def test_keyword_second_outer_attempt_success_keeps_source(self):
        self.completion.side_effect = [
            SyntheticHTTPError(PRIVATE_BODY), SyntheticHTTPError(PRIVATE_BODY),
            response('["日本地震", "火箭发射", "英伟达", "量子计算", "宇树科技"]'),
        ]
        labels, source = self._keywords()
        self.assertEqual(source, "ai_lite_retry")
        self.assertEqual(labels, LABELS)
        self.assertEqual(self.completion.call_count, 3)
        self._assert_private_absent()

    def test_keyword_invalid_output_logs_counts_not_raw_response(self):
        self.completion.return_value = response(PRIVATE_BODY)
        labels, source = self._keywords()
        self.assertEqual(source, "rule_only")
        self.assertTrue(labels)
        self.assertEqual(self.completion.call_count, 2)
        self.assertEqual(self.output.getvalue().count("[关键词] lite 无效输出 attempt="), 2)
        self.assertIn("valid_labels=", self.output.getvalue())
        self.assertNotIn("raw=", self.output.getvalue())
        self._assert_private_absent()

    def test_keyword_failure_does_not_suppress_successful_report_delivery(self):
        self.themes.return_value = []
        self.completion.side_effect = [response(REPORT)] + [SyntheticHTTPError(PRIVATE_BODY)] * 4
        self.assertEqual(self._main(), 0)
        self.assertEqual(self.completion.call_count, 5)
        self.dispatcher.send_report.assert_called_once()
        self.assertEqual(len(list(self.output_dir.glob("*"))), 2)
        self._assert_private_absent()

    def test_delivery_result_exit_codes_unchanged_after_report_retry(self):
        cases = ((False, False, False, 3), (True, False, False, 4),
                 (True, False, True, 6), (True, True, False, 0))
        for configured, sent, partial, code in cases:
            with self.subTest(exit_code=code):
                self.completion.reset_mock()
                self.dispatcher.send_report.reset_mock()
                self.completion.side_effect = [RuntimeError(PRIVATE_BODY), response(REPORT)]
                self.dispatcher.send_report.return_value = SimpleNamespace(
                    configured=configured, sent=sent, partially_delivered=partial, unknown=(),
                )
                self.assertEqual(self._main(), code)
                self.assertEqual(self.completion.call_count, 2)
                self.dispatcher.send_report.assert_called_once()
                self._assert_private_absent()

    def test_unpaired_smtp_config_still_exits_three_without_dispatch(self):
        self.env["EMAIL_SMTP_SERVER"] = "smtp.example.invalid"
        self.assertEqual(self._main(), 3)
        self.completion.assert_called_once()
        self.factory.assert_not_called()

    def test_no_news_still_exits_one_without_ai_or_delivery(self):
        self.collect.return_value = ([], {}, {})
        self.assertEqual(self._main(), 1)
        self.completion.assert_not_called()
        self._assert_no_delivery_or_files()

    def test_missing_model_still_exits_two_without_ai_or_delivery(self):
        self.config["MODEL"] = ""
        self.assertEqual(self._main(), 2)
        self.completion.assert_not_called()
        self._assert_no_delivery_or_files()

    def test_history_failure_still_exits_seven_without_ai_or_delivery(self):
        self.collect.side_effect = RuntimeError(PRIVATE_BODY)
        self.assertEqual(self._main(), 7)
        self.completion.assert_not_called()
        self._assert_no_delivery_or_files()
        self._assert_private_absent()


if __name__ == "__main__":
    unittest.main()
