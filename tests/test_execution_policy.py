"""Characterize AI once-gating separately from durable publication ownership.

Publication uses a private temporary CAS store and real coordinator. Only AI,
HTML preparation and SMTP are doubles; no legacy boolean delivery path remains.
"""

import io
import itertools
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from trendradar.ai import AIAnalysisResult
from trendradar.core.execution_policy import ExecutionPolicy, is_manual_force_run
from trendradar.core.frequency import matches_word_groups
from trendradar.core.scheduler import ResolvedSchedule
from trendradar.daily_flow import runner as runner_module
from trendradar.daily_flow.capture import capture_sources
from trendradar.daily_flow.inputs import prepare_captured_hotlist
from trendradar.daily_flow.models import KeywordRules, PreparedReportInput, ReportArtifacts, RSSResult
from trendradar.daily_flow.runner import DailyRunner
from trendradar.notification import NotificationDispatcher
from trendradar.notification.models import EmailDeliveryResult, PreparedEmail
from trendradar.storage.publication import LocalPublicationStore, PublicationError


NOW = datetime(2026, 10, 2, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
RECIPIENTS = ("one@example.invalid", "two@example.invalid")


def make_analyzer():
    analyzer = DailyRunner.__new__(DailyRunner)
    analyzer.report_mode = "current"
    analyzer.ctx = Mock()
    analyzer.ctx.config = {
        "AI_ANALYSIS": {"ENABLED": True}, "AI": {}, "ENABLE_CRAWLER": True,
        "ENABLE_NOTIFICATION": True, "EMAIL_FROM": "from@example.invalid",
        "EMAIL_PASSWORD": "synthetic", "EMAIL_TO": ",".join(RECIPIENTS),
    }
    analyzer.ctx.format_date.return_value = NOW.date().isoformat()
    analyzer.ctx.get_time.return_value = NOW
    analyzer.ctx.matches_word_groups.side_effect = matches_word_groups
    analyzer.ctx.create_scheduler.return_value.already_executed.return_value = False
    temp = tempfile.TemporaryDirectory()
    store = LocalPublicationStore(temp.name)
    unittest.addModuleCleanup(temp.cleanup)
    unittest.addModuleCleanup(store.close)
    analyzer.storage_manager = Mock()
    analyzer.storage_manager.get_publication_store.return_value = store
    dispatcher = Mock(spec_set=NotificationDispatcher)
    analyzer.ctx.create_notification_dispatcher.return_value = dispatcher
    dispatcher.prepare_report.return_value = PreparedEmail(
        "from@example.invalid", RECIPIENTS, "Synthetic report",
        b"From: from@example.invalid\r\n\r\nSynthetic frozen report",
        "<execution-policy@example.invalid>", "Fri, 02 Oct 2026 10:00:00 +0800",
    )
    dispatcher.send_prepared.side_effect = lambda prepared, *, recipients: EmailDeliveryResult(
        True, requested=recipients, accepted=recipients)
    dispatcher.send_report.side_effect = AssertionError("daily must prepare and persist before SMTP")
    return analyzer


def make_schedule(**overrides):
    values = dict(period_key="morning", period_name="早间速览", day_plan="test",
                  collect=True, analyze=True, push=True, report_mode="current",
                  once_analyze=True, once_push=True)
    values.update(overrides)
    return ResolvedSchedule(**values)


def report_input(analyzer, *, title="synthetic news", rss=RSSResult()):
    """A genuine FrozenCapture from a minimal source-reader contract."""
    row = {"title": title, "source_id": "p", "source_name": "Source",
           "url": "https://example.invalid/news", "first_time": "10-00",
           "last_time": "10-00", "ranks": [1]}
    reader = SimpleNamespace(read_day=lambda kind, date: {
        "date": date, "present": True, "latest_time": "10-00",
        "items": [row.copy()] if date == NOW.date().isoformat() else [],
    })
    coordinator = analyzer._publication_coordinator()
    capture = capture_sources(reader, coordinator.capture_baseline(), NOW,
                              platform_ids=["p"], feed_ids=[])
    return PreparedReportInput(analyzer.report_mode, prepare_captured_hotlist(capture.to_dict(), analyzer.report_mode),
                               KeywordRules([], [], ["BLOCK"]), rss, capture=capture)


class ExecutionBoundaryTests(unittest.TestCase):
    def invoke(self, action, analyzer, schedule, force=False, success=True, *, prepared=None, stats=None):
        ai = Mock()
        ai.analyze.return_value = AIAnalysisResult(success=success, error="" if success else "synthetic failure")
        with patch.object(runner_module, "AIAnalyzer", return_value=ai):
            with patch.object(analyzer, "_manual_force_run", return_value=force), redirect_stdout(io.StringIO()):
                if action == "analyze":
                    result = analyzer._run_ai_analysis(
                        [{"word": "topic", "count": 1, "titles": []}], None,
                        "current", "当前榜单", {"p": "source"}, schedule=schedule,
                    )
                else:
                    plan = analyzer._begin_publication(schedule)
                    result = False
                    if plan.report_id is not None:
                        prepared = prepared or report_input(analyzer)
                        artifacts = ReportArtifacts(
                            [{"count": 1, "titles": [{"title": "synthetic"}]}] if stats is None else stats,
                            "synthetic.html",
                        )
                        result = analyzer._send_notification_if_needed(
                            prepared, artifacts, prepared.capture.to_dict(), plan.report_id, schedule)
        return result, ai

    def test_ai_matrix_preserves_force_once_and_history_read_timing(self):
        for scheduled, once, has_period, executed, force in itertools.product((False, True), repeat=5):
            with self.subTest(scheduled=scheduled, once=once, has_period=has_period, executed=executed, force=force):
                analyzer = make_analyzer()
                schedule = make_schedule(analyze=scheduled, once_analyze=once,
                                         period_key="morning" if has_period else None)
                gate = analyzer.ctx.create_scheduler.return_value
                gate.already_executed.return_value = executed
                result, ai = self.invoke("analyze", analyzer, schedule, force)
                allowed = force or (scheduled and not (once and has_period and executed))
                self.assertEqual(result is not None, allowed)
                self.assertEqual(ai.analyze.call_count, int(allowed))
                if (scheduled or force) and once and has_period:
                    gate.already_executed.assert_called_once_with("morning", "analyze", "2026-10-02")
                else:
                    gate.already_executed.assert_not_called()
                if allowed and once and has_period:
                    gate.record_execution.assert_called_once_with("morning", "analyze", "2026-10-02")
                else:
                    gate.record_execution.assert_not_called()

    def test_push_matrix_uses_ledger_ownership_not_legacy_execution_history(self):
        for scheduled, once, has_period, claimed, executed, force in itertools.product((False, True), repeat=6):
            with self.subTest(scheduled=scheduled, once=once, has_period=has_period,
                              claimed=claimed, executed=executed, force=force):
                analyzer = make_analyzer()
                schedule = make_schedule(push=scheduled, once_push=once,
                                         period_key="morning" if has_period else None)
                coordinator = analyzer._publication_coordinator()
                if claimed:
                    coordinator.claim_generation(analyzer._publication_window(schedule))
                gate = analyzer.ctx.create_scheduler.return_value
                gate.already_executed.return_value = executed
                result, _ai = self.invoke("push", analyzer, schedule, force)
                allowed = force or (scheduled and not claimed)
                self.assertIs(result, allowed)
                dispatcher = analyzer.ctx.create_notification_dispatcher.return_value
                self.assertEqual(dispatcher.send_prepared.call_count, int(allowed))
                gate.already_executed.assert_not_called()
                if allowed and once and has_period:
                    gate.record_execution.assert_called_once_with("morning", "push", "2026-10-02")
                else:
                    gate.record_execution.assert_not_called()

    def test_unsuccessful_ai_never_consumes_once(self):
        analyzer = make_analyzer()
        result, ai = self.invoke("analyze", analyzer, make_schedule(), success=False)
        self.assertFalse(result.success)
        ai.analyze.assert_called_once()
        analyzer.ctx.create_scheduler.return_value.record_execution.assert_not_called()

    def test_daily_persists_prepared_email_before_structured_receipt_without_truthiness(self):
        analyzer = make_analyzer()
        dispatcher = analyzer.ctx.create_notification_dispatcher.return_value
        coordinator = analyzer._publication_coordinator()

        def send(prepared, *, recipients):
            status = coordinator.status()["reports"][0]
            self.assertEqual(status["state"], "SENDING")
            self.assertEqual(status["attempt"]["requested_count"], 2)
            payload = coordinator.store.get_snapshot(status["snapshot_id"])
            self.assertEqual(PreparedEmail.from_dict(payload["email"]), prepared)
            return EmailDeliveryResult(True, requested=recipients, accepted=recipients)

        dispatcher.send_prepared.side_effect = send
        result, _ai = self.invoke("push", analyzer, make_schedule())
        self.assertIs(result, True)
        dispatcher.prepare_report.assert_called_once_with(
            report_type="当前榜单", html_file_path="synthetic.html", period_name="早间速览")
        dispatcher.send_report.assert_not_called()
        self.assertEqual(coordinator.status()["reports"][0]["state"], "DELIVERED")
        with self.assertRaises(TypeError):
            bool(EmailDeliveryResult(True, requested=RECIPIENTS, accepted=RECIPIENTS))

    def test_ai_feature_disable_still_dominates_manual_force(self):
        analyzer = make_analyzer()
        analyzer.ctx.config["AI_ANALYSIS"]["ENABLED"] = False
        result, ai = self.invoke("analyze", analyzer, make_schedule(), force=True)
        self.assertIsNone(result)
        ai.analyze.assert_not_called()
        analyzer.ctx.create_scheduler.assert_not_called()

    def test_delivery_result_records_only_full_or_partial_acceptance(self):
        receipts = [
            EmailDeliveryResult(False, requested=RECIPIENTS, permanent_failed=RECIPIENTS),
            EmailDeliveryResult(True, requested=RECIPIENTS, temporary_failed=RECIPIENTS),
            EmailDeliveryResult(True, requested=RECIPIENTS, accepted=RECIPIENTS),
            EmailDeliveryResult(True, requested=RECIPIENTS, accepted=RECIPIENTS[:1], permanent_failed=RECIPIENTS[1:]),
            EmailDeliveryResult(True, requested=RECIPIENTS, unknown=RECIPIENTS),
        ]
        for receipt in receipts:
            with self.subTest(sent=receipt.sent, partial=receipt.partially_delivered, configured=receipt.configured):
                analyzer = make_analyzer()
                dispatcher = analyzer.ctx.create_notification_dispatcher.return_value
                dispatcher.send_prepared.side_effect = None
                dispatcher.send_prepared.return_value = receipt
                result, _ai = self.invoke("push", analyzer, make_schedule())
                self.assertIs(result, receipt.sent)
                gate = analyzer.ctx.create_scheduler.return_value
                if receipt.accepted:
                    gate.record_execution.assert_called_once_with("morning", "push", "2026-10-02")
                else:
                    gate.record_execution.assert_not_called()
                status = analyzer._publication_coordinator().status()
                self.assertEqual(status["reports"][0]["published"], bool(receipt.accepted))
                self.assertEqual(status["baseline"]["sequence"] > 0, bool(receipt.accepted))

    def test_disabled_or_unconfigured_notification_does_not_send_despite_force(self):
        for key, value in (("ENABLE_NOTIFICATION", False), ("EMAIL_TO", "")):
            analyzer = make_analyzer()
            analyzer.ctx.config[key] = value
            result, _ai = self.invoke("push", analyzer, make_schedule(), force=True)
            self.assertFalse(result)
            analyzer.ctx.create_scheduler.assert_not_called()
            analyzer.ctx.create_notification_dispatcher.assert_not_called()
            self.assertEqual(analyzer._publication_coordinator().status()["total_reports"], 0)

    def test_force_never_overrides_collection_or_crawler_disable_for_generation(self):
        for collect, enabled in ((False, True), (True, False), (False, False)):
            analyzer = make_analyzer()
            analyzer.ctx.config["ENABLE_CRAWLER"] = enabled
            result, _ai = self.invoke("push", analyzer, make_schedule(collect=collect), force=True)
            self.assertFalse(result)
            analyzer.ctx.create_notification_dispatcher.assert_not_called()
            self.assertEqual(analyzer._publication_coordinator().status()["total_reports"], 0)

    def test_all_modes_share_interval_content_rules_and_allow_rss_only_delivery(self):
        for mode in ("incremental", "current", "daily"):
            analyzer = make_analyzer()
            analyzer.report_mode = mode
            self.assertTrue(analyzer._has_valid_content([], {"p": {"new": {}}}))
            self.assertFalse(analyzer._has_valid_content([{"count": 0}], {"p": {}}))
            self.assertTrue(analyzer._has_valid_content([{"count": 1}], {}))
            prepared = report_input(analyzer, title="BLOCK hot", rss=RSSResult(
                [{"count": 1, "titles": [{"title": "RSS"}]}], [], []))
            result, _ai = self.invoke("push", analyzer, make_schedule(), prepared=prepared, stats=[])
            self.assertTrue(result)

    def test_keyword_filtered_interval_only_content_sends_but_blocked_content_does_not(self):
        for title, allowed in (("new off-list story", True), ("BLOCK off-list story", False)):
            analyzer = make_analyzer()
            prepared = report_input(analyzer, title=title)
            result, _ai = self.invoke("push", analyzer, make_schedule(), prepared=prepared, stats=[])
            self.assertIs(result, allowed)
            dispatcher = analyzer.ctx.create_notification_dispatcher.return_value
            self.assertEqual(dispatcher.send_prepared.call_count, int(allowed))
            status = analyzer._publication_coordinator().status()["reports"][0]
            self.assertEqual(status["state"], "DELIVERED" if allowed else "NO_EMAIL")

    def test_force_does_not_bypass_unknown_attempt_safety(self):
        analyzer = make_analyzer()
        dispatcher = analyzer.ctx.create_notification_dispatcher.return_value
        dispatcher.send_prepared.side_effect = RuntimeError("synthetic interrupted submission")
        self.assertFalse(self.invoke("push", analyzer, make_schedule())[0])
        with self.assertRaises(PublicationError):
            self.invoke("push", analyzer, make_schedule(), force=True)
        dispatcher.send_prepared.assert_called_once()
        self.assertEqual(analyzer._publication_coordinator().retryable_reports(), [])


class PureExecutionPolicyTests(unittest.TestCase):
    def test_decision_and_history_requirement_cover_all_inputs_without_io(self):
        for scheduled, once, has_period, force, executed in itertools.product((False, True), repeat=5):
            policy = ExecutionPolicy(scheduled, once, "morning" if has_period else None, force)
            with self.subTest(policy=policy, executed=executed):
                self.assertIs(policy.check_history, (scheduled or force) and once and has_period)
                self.assertIs(policy.allows(executed), force or (scheduled and not (once and has_period and executed)))

    def test_manual_marker_lexical_contract_is_unchanged(self):
        cases = [
            ({}, False), ({"TRENDRADAR_FORCE_RUN": "1"}, True),
            ({"TRENDRADAR_FORCE_RUN": "true"}, False),
            ({"TRENDRADAR_FORCE_RUN": " 1 "}, False),
            ({"WORKFLOW_EVENT_NAME": "workflow_dispatch"}, True),
            ({"GITHUB_EVENT_NAME": "workflow_dispatch"}, True),
            ({"WORKFLOW_EVENT_NAME": "schedule", "GITHUB_EVENT_NAME": "push"}, False),
            ({"GITHUB_EVENT_NAME": "WORKFLOW_DISPATCH"}, False),
        ]
        for environ, expected in cases:
            with self.subTest(environ=environ):
                self.assertIs(is_manual_force_run(environ), expected)


if __name__ == "__main__":
    unittest.main()
