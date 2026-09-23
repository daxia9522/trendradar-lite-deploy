"""Partial SMTP acceptance must not become an automatic whole-report resend."""
import importlib.util
import io
import smtplib
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from trendradar.core.scheduler import ResolvedSchedule
from trendradar.daily import NewsAnalyzer
from trendradar.notification import senders
from trendradar.notification.dispatcher import NotificationDispatcher
from weekly_report import weekly_ai_report_email as weekly


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("partial_delivery_scheduler", ROOT / "deploy/docker/scheduler.py")
scheduler = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scheduler)
REFUSED = {"two@example.invalid": (550, b"synthetic rejection")}


def smtp_server(result):
    server = Mock(spec=smtplib.SMTP_SSL)
    if isinstance(result, Exception):
        server.send_message.side_effect = result
    else:
        server.send_message.return_value = result
    return server


class PartialDeliveryFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.report = self.root / "report.html"
        self.report.write_text("<html>synthetic report</html>", encoding="utf-8")
        self.config = {
            "ENABLE_NOTIFICATION": True,
            "EMAIL_FROM": "sender@example.invalid",
            "EMAIL_PASSWORD": "synthetic-password",
            "EMAIL_TO": "one@example.invalid,two@example.invalid",
            "EMAIL_SMTP_SERVER": "smtp.example.invalid",
            "EMAIL_SMTP_PORT": "465",
        }
        self.clock = lambda: datetime(2026, 9, 21, 7, 0)

    def daily_analyzer(self):
        # Avoid constructor setup, storage, crawling, AI and all real environment loads.
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.report_mode = "current"
        analyzer.ctx = Mock()
        analyzer.ctx.config = self.config
        analyzer.ctx.format_date.return_value = "2026-09-21"
        analyzer.ctx.create_notification_dispatcher.side_effect = lambda: NotificationDispatcher(self.config, self.clock)
        recorded = set()
        gate = analyzer.ctx.create_scheduler.return_value
        gate.already_executed.side_effect = lambda *key: key in recorded
        gate.record_execution.side_effect = lambda *key: recorded.add(key)
        schedule = ResolvedSchedule("morning", "早间速览", "test", True, False, True, "current", False, True)
        return analyzer, gate, schedule

    def invoke_daily_twice(self, smtp_result):
        analyzer, gate, schedule = self.daily_analyzer()
        server = smtp_server(smtp_result)
        with patch.object(senders.smtplib, "SMTP_SSL", return_value=server) as factory:
            with patch.object(senders.time, "sleep") as sleep:
                with patch("trendradar.daily._is_manual_force_run", return_value=False), redirect_stdout(io.StringIO()):
                    results = [analyzer._send_notification_if_needed(
                        [{"count": 1, "titles": [{"title": "synthetic news"}]}],
                        "current", html_file_path=str(self.report), schedule=schedule,
                    ) for _ in range(2)]
        sleep.assert_not_called()
        return results, gate, factory

    def test_partial_daily_delivery_records_window_without_reporting_success(self):
        results, gate, factory = self.invoke_daily_twice(REFUSED)
        self.assertEqual(results, [False, False])
        self.assertEqual(factory.call_count, 1)
        gate.record_execution.assert_called_once_with("morning", "push", "2026-09-21")

    def test_complete_daily_failure_does_not_consume_window(self):
        results, gate, factory = self.invoke_daily_twice(smtplib.SMTPDataError(554, b"synthetic rejection"))
        self.assertEqual(results, [False, False])
        self.assertEqual(factory.call_count, 2)
        gate.record_execution.assert_not_called()

    def test_complete_daily_acceptance_still_records_success(self):
        results, gate, factory = self.invoke_daily_twice({})
        self.assertEqual(results, [True, False])
        self.assertEqual(factory.call_count, 1)
        gate.record_execution.assert_called_once_with("morning", "push", "2026-09-21")

    def test_dispatcher_partial_flag_is_reset_for_each_dispatch(self):
        dispatcher = NotificationDispatcher(self.config, self.clock)
        server = smtp_server({})
        server.send_message.side_effect = [REFUSED, {}, smtplib.SMTPDataError(554, b"rejected")]
        flags, results = [], []
        with patch.object(senders.smtplib, "SMTP_SSL", return_value=server), redirect_stdout(io.StringIO()):
            for _ in range(3):
                results.append(dispatcher.dispatch_all("daily", str(self.report)))
                flags.append(dispatcher.email_partially_delivered)
        self.assertEqual(results, [{"email": False}, {"email": True}, {"email": False}])
        self.assertEqual(flags, [True, False, False])
        self.assertEqual(server.send_message.call_count, 3)

    def weekly_exit(self, smtp_result):
        server = smtp_server(smtp_result)
        client = Mock()
        client.validate_config.return_value = (True, "")
        client.chat.return_value = "synthetic AI output, not a real model call"
        client.last_model = "synthetic/model"
        # Exercise the real main-to-email exit mapping with every data/AI/env boundary mocked.
        replacements = {
            "OUTPUT_DIR": self.root / "weekly",
            "load_runtime_env": Mock(return_value=self.config),
            "load_ai_config": Mock(return_value={"MODEL": "synthetic/model"}),
            "AIClient": Mock(return_value=client),
            "collect_news": Mock(return_value=([{"title": "synthetic news"}], {}, {})),
            "build_prompt": Mock(return_value=[]),
            "build_evidence_index": Mock(return_value={}),
            "parse_structured_report": Mock(return_value=("synthetic report", [])),
            "keywords_from_themes": Mock(return_value=["甲", "乙", "丙", "丁", "戊"]),
            "extract_headline_keywords": Mock(side_effect=AssertionError("unexpected keyword AI path")),
            "render_html": Mock(return_value="<html>synthetic weekly</html>"),
            "build_rule_entity_headlines": Mock(return_value=[]),
        }
        argv = ["weekly", "--start", "2026-09-15", "--end", "2026-09-21"]
        with patch.multiple(weekly, **replacements), patch.object(sys, "argv", argv):
            with patch.object(senders.smtplib, "SMTP_SSL", return_value=server) as factory:
                with patch.object(senders.time, "sleep") as sleep, redirect_stdout(io.StringIO()):
                    result = weekly.main()
        replacements["load_runtime_env"].assert_called_once_with()
        client.chat.assert_called_once()
        replacements["extract_headline_keywords"].assert_not_called()
        self.assertEqual(factory.call_count, 1)
        sleep.assert_not_called()
        return result

    def test_weekly_partial_delivery_has_distinct_nonzero_exit(self):
        self.assertEqual(self.weekly_exit(REFUSED), weekly.PARTIAL_EMAIL_EXIT_CODE)
        self.assertEqual(weekly.PARTIAL_EMAIL_EXIT_CODE, scheduler.PARTIAL_EMAIL_EXIT_CODE)
        self.assertNotIn(weekly.PARTIAL_EMAIL_EXIT_CODE, (0, 1, 2, 3, 4, 5))

    def test_weekly_success_and_complete_failure_keep_existing_exit_codes(self):
        self.assertEqual(self.weekly_exit({}), 0)
        self.assertEqual(self.weekly_exit(smtplib.SMTPDataError(554, b"rejected")), 4)

    def test_docker_partial_weekly_is_terminal_but_not_success(self):
        state = {}
        output = io.StringIO()
        with patch.object(scheduler.subprocess, "run", return_value=subprocess.CompletedProcess([], weekly.PARTIAL_EMAIL_EXIT_CODE)):
            with patch.object(scheduler, "_save_state") as save, redirect_stdout(output):
                result = scheduler._run("weekly", ["synthetic-command"], "marker", state)
        self.assertFalse(result)
        self.assertEqual(state, {"weekly": "marker"})
        save.assert_called_once_with(state)
        self.assertIn("partially delivered", output.getvalue())
        self.assertIn("no automatic whole-task retry", output.getvalue())
        self.assertNotIn("remains eligible for retry", output.getvalue())

    def test_docker_other_failures_remain_retryable(self):
        for name, code in (("weekly", 1), ("weekly", 2), ("weekly", 3), ("weekly", 4), ("weekly", 5), ("crawler", 6)):
            with self.subTest(name=name, code=code):
                state = {}
                with patch.object(scheduler.subprocess, "run", return_value=subprocess.CompletedProcess([], code)):
                    with patch.object(scheduler, "_save_state") as save, redirect_stdout(io.StringIO()):
                        result = scheduler._run(name, ["synthetic-command"], "marker", state)
                self.assertFalse(result)
                self.assertEqual(state, {})
                save.assert_not_called()

    def test_docker_next_poll_does_not_repeat_partially_delivered_weekly(self):
        now = datetime(2026, 9, 20, 12, 30, tzinfo=scheduler.TIMEZONE)
        ticks = []

        def next_poll(_seconds):
            ticks.append(None)
            if len(ticks) == 2:
                scheduler.STOP = True

        state = {}
        replacements = {
            "STOP": False, "CRAWLER_MINUTE": 0, "PUSH_TIMES": set(),
            "WEEKLY_WEEKDAY": 6, "WEEKLY_HOUR": 12, "WEEKLY_MINUTE": 30,
            "_load_state": Mock(return_value=state), "_save_state": Mock(),
            "datetime": Mock(now=Mock(return_value=now)),
        }
        with patch.multiple(scheduler, **replacements):
            with patch.object(scheduler.signal, "signal"):
                with patch.object(scheduler.time, "sleep", side_effect=next_poll):
                    with patch.object(scheduler.subprocess, "run", return_value=subprocess.CompletedProcess([], weekly.PARTIAL_EMAIL_EXIT_CODE)) as run:
                        with redirect_stdout(io.StringIO()):
                            result = scheduler.main()
        self.assertEqual(result, 0)
        self.assertEqual(len(ticks), 2)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(state, {"weekly": now.strftime("%Y-%m-%dT%H:%M")})
        replacements["_save_state"].assert_called_once_with(state)


if __name__ == "__main__":
    unittest.main()
