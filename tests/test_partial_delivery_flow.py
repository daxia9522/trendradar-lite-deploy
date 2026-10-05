"""Partial SMTP acceptance must not become an automatic whole-report resend."""
import importlib.util
import io
import smtplib
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from trendradar.core.scheduler import ResolvedSchedule
from trendradar.core.frequency import matches_word_groups
from trendradar.daily_flow.capture import capture_sources
from trendradar.daily_flow.inputs import prepare_captured_hotlist
from trendradar.daily_flow.models import KeywordRules, PreparedReportInput, ReportArtifacts
from trendradar.daily_flow.runner import DailyRunner
from trendradar.notification import senders
from trendradar.notification.dispatcher import NotificationDispatcher
from trendradar.storage.publication import LocalPublicationStore, PublicationError
from weekly_report import weekly_ai_report_email as weekly


ROOT = Path(__file__).resolve().parents[1]
TEST_TIMEZONE = ZoneInfo("Asia/Shanghai")
SPEC = importlib.util.spec_from_file_location("partial_delivery_scheduler", ROOT / "deploy/docker/scheduler.py")
scheduler = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scheduler)
REFUSED = {"two@example.invalid": (550, b"synthetic rejection")}


class FakePopen:
    """Pollable child double for Docker scheduler tests."""

    def __init__(self, returncode=0, *, running=False):
        self.returncode = None if running else returncode
        self.signals = []

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def send_signal(self, signum):
        self.signals.append(signum)
        self.returncode = -signum

    def kill(self):
        self.returncode = -9


def smtp_server(result):
    server = Mock(spec=smtplib.SMTP_SSL)
    server.mail.return_value = (250, b"ok")
    server.rcpt.side_effect = lambda address: (
        result.get(address, (250, b"ok")) if isinstance(result, dict) else (250, b"ok")
    )
    server.data.return_value = (250, b"queued")
    if isinstance(result, Exception):
        server.data.side_effect = result
    return server


class PartialDeliveryFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.report = self.root / "report.html"
        self.report.write_text("<html>synthetic report</html>", encoding="utf-8")
        self.config = {
            "ENABLE_NOTIFICATION": True, "ENABLE_CRAWLER": True,
            "EMAIL_FROM": "sender@example.invalid",
            "EMAIL_PASSWORD": "synthetic-password",
            "EMAIL_TO": "one@example.invalid,two@example.invalid",
            "EMAIL_SMTP_SERVER": "smtp.example.invalid",
            "EMAIL_SMTP_PORT": "465",
        }
        self.clock = lambda: datetime(2026, 9, 21, 7, 0, tzinfo=TEST_TIMEZONE)

    def daily_analyzer(self):
        # Real temporary publication store and SMTP state machine, no live I/O.
        analyzer = DailyRunner.__new__(DailyRunner)
        analyzer.report_mode = "current"
        analyzer.ctx = Mock()
        analyzer.ctx.config = self.config
        analyzer.ctx.get_time.side_effect = self.clock
        analyzer.ctx.format_date.return_value = "2026-09-21"
        analyzer.ctx.matches_word_groups.side_effect = matches_word_groups
        analyzer.ctx.create_notification_dispatcher.side_effect = lambda: NotificationDispatcher(self.config, self.clock)
        analyzer.storage_manager = Mock()
        store = LocalPublicationStore(self.root / "publication")
        self.addCleanup(store.close)
        analyzer.storage_manager.get_publication_store.return_value = store
        recorded = set()
        gate = analyzer.ctx.create_scheduler.return_value
        gate.already_executed.side_effect = lambda *key: key in recorded
        gate.record_execution.side_effect = lambda *key: recorded.add(key)
        schedule = ResolvedSchedule("morning", "早间速览", "test", True, False, True, "current", False, True)
        return analyzer, gate, schedule

    def prepared_daily(self, analyzer):
        reader = SimpleNamespace(read_day=lambda kind, date: {
            "date": date, "present": True, "latest_time": "07-00", "items": [{
                "title": "synthetic news", "source_id": "p", "source_name": "Synthetic source",
                "url": "https://example.invalid/story", "ranks": [1],
                "first_time": "07-00", "last_time": "07-00",
            }] if date == "2026-09-21" else [],
        })
        coordinator = analyzer._publication_coordinator()
        capture = capture_sources(reader, coordinator.capture_baseline(), self.clock(),
                                  platform_ids=["p"], feed_ids=[])
        return PreparedReportInput("current", prepare_captured_hotlist(capture.to_dict(), "current"),
                                   KeywordRules([], [], []), capture=capture)

    def invoke_daily(self, analyzer, schedule):
        plan = analyzer._begin_publication(schedule)
        coordinator = analyzer._publication_coordinator()
        for report_id in plan.retry_existing_reports:
            coordinator.deliver(report_id, analyzer.ctx.create_notification_dispatcher())
        if plan.report_id is None:
            return False
        prepared = self.prepared_daily(analyzer)
        return analyzer._send_notification_if_needed(
            prepared, ReportArtifacts([{"count": 1, "titles": [{"title": "synthetic news"}]}], str(self.report)),
            prepared.capture.to_dict(), plan.report_id, schedule,
        )

    def invoke_daily_twice(self, smtp_result):
        analyzer, gate, schedule = self.daily_analyzer()
        server = smtp_server(smtp_result)
        with patch.object(senders.smtplib, "SMTP_SSL", return_value=server) as factory:
            with patch("time.sleep") as sleep:
                with patch.object(analyzer, "_manual_force_run", return_value=False), redirect_stdout(io.StringIO()):
                    results = [self.invoke_daily(analyzer, schedule) for _ in range(2)]
        sleep.assert_not_called()
        return results, gate, factory, analyzer._publication_coordinator()

    def test_partial_daily_delivery_records_window_without_reporting_success(self):
        results, gate, factory, coordinator = self.invoke_daily_twice(REFUSED)
        self.assertEqual(results, [False, False])
        self.assertEqual(factory.call_count, 1)
        gate.record_execution.assert_called_once_with("morning", "push", "2026-09-21")
        status = coordinator.status()["reports"][0]
        self.assertTrue(status["published"])
        self.assertEqual(status["state"], "ATTENTION")
        self.assertEqual(status["outcome_counts"]["accepted"], 1)
        self.assertEqual(status["outcome_counts"]["permanent_failed"], 1)
        self.assertEqual(coordinator.retryable_reports(), [])
        self.assertEqual(coordinator.capture_baseline()["coverage"]["news"]["through"], self.clock().isoformat())

    def test_permanent_daily_failure_does_not_publish_or_blindly_regenerate(self):
        results, gate, factory, coordinator = self.invoke_daily_twice(smtplib.SMTPDataError(554, b"synthetic rejection"))
        self.assertEqual(results, [False, False])
        self.assertEqual(factory.call_count, 1)
        gate.record_execution.assert_not_called()
        status = coordinator.status()["reports"][0]
        self.assertFalse(status["published"])
        self.assertTrue(status["window_owned"])
        self.assertEqual(status["outcome_counts"]["permanent_failed"], 2)
        self.assertEqual(coordinator.status()["baseline"]["sequence"], 0)
        self.assertEqual(coordinator.retryable_reports(), [])

    def test_complete_daily_acceptance_still_records_success(self):
        results, gate, factory, coordinator = self.invoke_daily_twice({})
        self.assertEqual(results, [True, False])
        self.assertEqual(factory.call_count, 1)
        gate.record_execution.assert_called_once_with("morning", "push", "2026-09-21")
        self.assertEqual(coordinator.status()["reports"][0]["state"], "DELIVERED")

    def test_unknown_daily_submission_is_durable_and_not_retried_even_by_force(self):
        results, gate, factory, coordinator = self.invoke_daily_twice(
            smtplib.SMTPServerDisconnected("synthetic DATA response loss"))
        self.assertEqual(results, [False, False])
        self.assertEqual(factory.call_count, 1)
        gate.record_execution.assert_not_called()
        status = coordinator.status()["reports"][0]
        self.assertEqual(status["outcome_counts"]["unknown"], 2)
        self.assertFalse(status["published"])
        self.assertEqual(coordinator.retryable_reports(), [])
        with self.assertRaises(PublicationError):
            coordinator.claim_generation(status["window"], force=True)

    def test_partial_temporary_retry_uses_only_rejected_envelope_and_identical_mime(self):
        analyzer, gate, schedule = self.daily_analyzer()
        servers = [smtp_server({"two@example.invalid": (450, b"synthetic temporary rejection")}), smtp_server({})]
        with patch.object(senders.smtplib, "SMTP_SSL", side_effect=servers) as factory:
            with patch.object(analyzer, "_manual_force_run", return_value=False), redirect_stdout(io.StringIO()):
                self.assertFalse(self.invoke_daily(analyzer, schedule))
                coordinator = analyzer._publication_coordinator()
                original = coordinator.status()["reports"][0]
                self.assertTrue(original["published"])
                self.assertEqual(original["state"], "DELIVERY_PENDING")
                baseline = coordinator.capture_baseline()
                # Retry is independent of collection and uses frozen headers/content,
                # not the now-edited HTML, configured recipients, clock or AI output.
                self.report.write_text("<html>edited after first attempt</html>", encoding="utf-8")
                self.config["EMAIL_TO"] = "different@example.invalid"
                schedule.collect = False
                analyzer.ctx.create_scheduler.return_value.resolve.return_value = schedule
                analyzer._crawl_data = Mock(side_effect=AssertionError("retry must not crawl"))
                analyzer.prepare_report = Mock(side_effect=AssertionError("retry must not recapture"))
                analyzer.analyze_report = Mock(side_effect=AssertionError("retry must not analyze/render"))
                analyzer.run()
        self.assertEqual(factory.call_count, 2)
        self.assertEqual([call.args[0] for call in servers[0].rcpt.call_args_list],
                         ["one@example.invalid", "two@example.invalid"])
        servers[1].rcpt.assert_called_once_with("two@example.invalid")
        self.assertEqual(servers[0].data.call_args.args[0], servers[1].data.call_args.args[0])
        self.assertEqual(servers[0].mail.call_args, servers[1].mail.call_args)
        analyzer._crawl_data.assert_not_called()
        analyzer.prepare_report.assert_not_called()
        analyzer.analyze_report.assert_not_called()
        analyzer.ctx.cleanup.assert_called_once_with()
        gate.record_execution.assert_called_once_with("morning", "push", "2026-09-21")
        final = coordinator.status()["reports"][0]
        self.assertEqual(final["id"], original["id"])
        self.assertEqual(final["state"], "DELIVERED")
        self.assertEqual(final["receipt_count"], 2)
        self.assertEqual(coordinator.capture_baseline(), baseline)

    def test_all_temporary_refusal_retries_saved_report_without_publishing_early(self):
        analyzer, gate, schedule = self.daily_analyzer()
        recipients = ("one@example.invalid", "two@example.invalid")
        servers = [smtp_server({address: (450, b"synthetic temporary refusal") for address in recipients}),
                   smtp_server({})]
        with patch.object(senders.smtplib, "SMTP_SSL", side_effect=servers) as factory:
            with patch.object(analyzer, "_manual_force_run", return_value=False), redirect_stdout(io.StringIO()):
                self.assertFalse(self.invoke_daily(analyzer, schedule))
                coordinator = analyzer._publication_coordinator()
                original = coordinator.status()["reports"][0]
                self.assertFalse(original["published"])
                self.assertEqual(original["outcome_counts"]["temporary_failed"], 2)
                self.assertEqual(coordinator.status()["baseline"]["sequence"], 0)
                self.assertEqual(coordinator.retryable_reports(), [original["id"]])
                self.invoke_daily(analyzer, schedule)
        self.assertEqual(factory.call_count, 2)
        servers[0].data.assert_not_called()
        servers[1].data.assert_called_once()
        self.assertEqual([call.args[0] for call in servers[1].rcpt.call_args_list], list(recipients))
        gate.record_execution.assert_not_called()  # retry publication is ledger-owned, not a new generation
        final = coordinator.status()["reports"][0]
        self.assertEqual(final["id"], original["id"])
        self.assertEqual(final["state"], "DELIVERED")
        self.assertTrue(final["published"])
        self.assertEqual(coordinator.status()["total_reports"], 1)
        self.assertEqual(coordinator.status()["baseline"]["report_id"], original["id"])

    def test_dispatcher_returns_independent_partial_results_for_each_send(self):
        dispatcher = NotificationDispatcher(self.config, self.clock)
        servers = [smtp_server(REFUSED), smtp_server({}), smtp_server(smtplib.SMTPDataError(554, b"rejected"))]
        results = []
        with patch.object(senders.smtplib, "SMTP_SSL", side_effect=servers), redirect_stdout(io.StringIO()):
            for _ in range(3):
                results.append(dispatcher.send_report("daily", str(self.report)))
        self.assertEqual([result.configured for result in results], [True, True, True])
        self.assertEqual([result.sent for result in results], [False, True, False])
        self.assertEqual([result.partially_delivered for result in results], [True, False, False])
        self.assertEqual(sum(server.data.call_count for server in servers), 3)

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
            "render_weekly_html": Mock(return_value="<html>synthetic weekly</html>"),
            "build_rule_entity_headlines": Mock(return_value=[]),
        }
        argv = ["weekly", "--start", "2026-09-15", "--end", "2026-09-21"]
        with patch.multiple(weekly, **replacements), patch.object(sys, "argv", argv):
            with patch.object(senders.smtplib, "SMTP_SSL", return_value=server) as factory:
                with patch("time.sleep") as sleep, redirect_stdout(io.StringIO()):
                    result = weekly.main()
        replacements["load_runtime_env"].assert_called_once_with()
        client.chat.assert_called_once()
        replacements["extract_headline_keywords"].assert_not_called()
        replacements["render_weekly_html"].assert_called_once()
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

    def test_weekly_unknown_uses_scheduler_terminal_code_without_whole_task_retry(self):
        self.assertEqual(
            self.weekly_exit(smtplib.SMTPServerDisconnected("synthetic DATA response loss")),
            scheduler.PARTIAL_EMAIL_EXIT_CODE,
        )

    def test_docker_partial_weekly_is_terminal_but_not_success(self):
        state = {}
        output = io.StringIO()
        now = datetime(2026, 9, 20, 12, 30, tzinfo=TEST_TIMEZONE)
        values = {"TZ": TEST_TIMEZONE.key, "CRAWLER_MINUTE": "5",
                  "WEEKLY_WEEKDAY": "6", "WEEKLY_HOUR": "12", "WEEKLY_MINUTE": "30"}
        clock = scheduler.Scheduler(lambda: scheduler.load_runtime_config(values), state=state,
                                    clock=lambda: now)
        with patch.object(scheduler.subprocess, "Popen",
                          return_value=FakePopen(weekly.PARTIAL_EMAIL_EXIT_CODE)):
            with patch.object(scheduler, "_save_state") as save, redirect_stdout(output):
                clock.poll()
        self.assertEqual(state, {"weekly": now.strftime("%Y-%m-%dT%H:%M")})
        save.assert_called_once_with(state)
        self.assertIn("partially delivered", output.getvalue())
        self.assertIn("no automatic whole-task retry", output.getvalue())
        self.assertNotIn("remains eligible for retry", output.getvalue())

    def test_docker_other_failures_remain_retryable(self):
        for name, code in (("weekly", 1), ("weekly", 2), ("weekly", 3), ("weekly", 4), ("weekly", 5), ("crawler", 6)):
            with self.subTest(name=name, code=code):
                state = {}
                if name == "weekly":
                    now = datetime(2026, 9, 20, 12, 30, tzinfo=TEST_TIMEZONE)
                    values = {"TZ": TEST_TIMEZONE.key, "CRAWLER_MINUTE": "5",
                              "WEEKLY_WEEKDAY": "6", "WEEKLY_HOUR": "12", "WEEKLY_MINUTE": "30"}
                else:
                    now = datetime(2026, 9, 20, 12, 5, tzinfo=TEST_TIMEZONE)
                    values = {"TZ": TEST_TIMEZONE.key, "CRAWLER_MINUTE": "5",
                              "WEEKLY_WEEKDAY": "6", "WEEKLY_HOUR": "12", "WEEKLY_MINUTE": "30"}
                clock = scheduler.Scheduler(lambda: scheduler.load_runtime_config(values), state=state,
                                            clock=lambda: now)
                with patch.object(scheduler.subprocess, "Popen", return_value=FakePopen(code)):
                    with patch.object(scheduler, "_save_state") as save, redirect_stdout(io.StringIO()):
                        clock.poll()
                self.assertEqual(state, {})
                save.assert_not_called()

    def test_docker_next_poll_does_not_repeat_partially_delivered_weekly(self):
        now = datetime(2026, 9, 20, 12, 30, tzinfo=TEST_TIMEZONE)
        ticks = []

        def next_poll(_seconds):
            ticks.append(None)
            if len(ticks) == 2:
                scheduler.STOP = True

        state = {}
        replacements = {
            "STOP": False,
            "_load_state": Mock(return_value=state), "_save_state": Mock(),
            "datetime": Mock(now=Mock(return_value=now)),
        }
        base = {"TZ": TEST_TIMEZONE.key, "CRAWLER_MINUTE": "5",
                "WEEKLY_WEEKDAY": "6", "WEEKLY_HOUR": "12", "WEEKLY_MINUTE": "30"}
        with patch.multiple(scheduler, **replacements):
            with patch.object(scheduler.signal, "signal"):
                with patch.object(scheduler.STOP_EVENT, "wait", side_effect=next_poll):
                    with patch.object(scheduler.subprocess, "Popen",
                                      return_value=FakePopen(weekly.PARTIAL_EMAIL_EXIT_CODE)) as run:
                        with redirect_stdout(io.StringIO()):
                            result = scheduler.main(base)
        self.assertEqual(result, 0)
        self.assertEqual(len(ticks), 2)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(state, {"weekly": now.strftime("%Y-%m-%dT%H:%M")})
        replacements["_save_state"].assert_called_once_with(state)


if __name__ == "__main__":
    unittest.main()
