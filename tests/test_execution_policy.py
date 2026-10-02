"""Characterize action gating separately from the success-recording boundary."""

import io
import itertools
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from trendradar.ai import AIAnalysisResult
from trendradar.core.execution_policy import ExecutionPolicy, is_manual_force_run
from trendradar.core.scheduler import ResolvedSchedule
from trendradar.daily_flow import runner as runner_module
from trendradar.daily_flow.runner import DailyRunner
from trendradar.notification import NotificationDispatcher


class NamedDeliveryResult:
    """Catch accidental truthiness checks when daily adopts send_report."""

    def __init__(self, configured=True, sent=True, partially_delivered=False):
        self.configured = configured
        self.sent = sent
        self.partially_delivered = partially_delivered

    def __bool__(self):
        raise AssertionError("delivery status must be read from named fields")


def make_analyzer():
    analyzer = DailyRunner.__new__(DailyRunner)
    analyzer.report_mode = "current"
    analyzer.ctx = Mock()
    analyzer.ctx.config = {
        "AI_ANALYSIS": {"ENABLED": True}, "AI": {},
        "ENABLE_NOTIFICATION": True, "EMAIL_FROM": "from@example.invalid",
        "EMAIL_PASSWORD": "synthetic", "EMAIL_TO": "to@example.invalid",
    }
    analyzer.ctx.format_date.return_value = "2026-10-02"
    analyzer.ctx.get_time = lambda: None  # fake AI factory never uses it
    analyzer.ctx.create_scheduler.return_value.already_executed.return_value = False
    dispatcher = Mock(spec_set=NotificationDispatcher)
    analyzer.ctx.create_notification_dispatcher.return_value = dispatcher
    dispatcher.send_report.return_value = NamedDeliveryResult()
    return analyzer


def make_schedule(**overrides):
    values = dict(period_key="morning", period_name="早间速览", day_plan="test",
                  collect=True, analyze=True, push=True, report_mode="current",
                  once_analyze=True, once_push=True)
    values.update(overrides)
    return ResolvedSchedule(**values)


class ExecutionBoundaryTests(unittest.TestCase):
    def invoke(self, action, analyzer, schedule, force=False, success=True):
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
                    result = analyzer._send_notification_if_needed(
                        [{"count": 1, "titles": [{"title": "synthetic"}]}],
                        "当前榜单", html_file_path="synthetic.html", schedule=schedule,
                    )
        return result, ai

    def test_action_matrix_preserves_force_once_and_history_read_timing(self):
        for action, scheduled, once, has_period, executed, force in itertools.product(
            ("analyze", "push"), (False, True), (False, True), (False, True), (False, True), (False, True),
        ):
            with self.subTest(action=action, scheduled=scheduled, once=once,
                              has_period=has_period, executed=executed, force=force):
                analyzer = make_analyzer()
                schedule = make_schedule(**{
                    action: scheduled, f"once_{action}": once,
                    "period_key": "morning" if has_period else None,
                })
                gate = analyzer.ctx.create_scheduler.return_value
                gate.already_executed.return_value = executed
                result, ai = self.invoke(action, analyzer, schedule, force)
                allowed = force or (scheduled and not (once and has_period and executed))
                if action == "analyze":
                    self.assertEqual(result is not None, allowed)
                    self.assertEqual(ai.analyze.call_count, int(allowed))
                else:
                    self.assertIs(result, allowed)
                    dispatcher = analyzer.ctx.create_notification_dispatcher.return_value
                    self.assertEqual(dispatcher.send_report.call_count, int(allowed))
                should_read = (scheduled or force) and once and has_period
                if should_read:
                    gate.already_executed.assert_called_once_with("morning", action, "2026-10-02")
                else:
                    gate.already_executed.assert_not_called()
                if allowed and once and has_period:
                    gate.record_execution.assert_called_once_with("morning", action, "2026-10-02")
                else:
                    gate.record_execution.assert_not_called()

    def test_unsuccessful_ai_never_consumes_once(self):
        analyzer = make_analyzer()
        result, ai = self.invoke("analyze", analyzer, make_schedule(), success=False)
        self.assertFalse(result.success)
        ai.analyze.assert_called_once()
        analyzer.ctx.create_scheduler.return_value.record_execution.assert_not_called()

    def test_daily_uses_named_send_report_without_result_truthiness(self):
        analyzer = make_analyzer()
        result, _ai = self.invoke("push", analyzer, make_schedule())
        self.assertIs(result, True)
        dispatcher = analyzer.ctx.create_notification_dispatcher.return_value
        dispatcher.send_report.assert_called_once_with(
            report_type="当前榜单", html_file_path="synthetic.html", period_name="早间速览",
        )

    def test_ai_feature_disable_still_dominates_manual_force(self):
        analyzer = make_analyzer()
        analyzer.ctx.config["AI_ANALYSIS"]["ENABLED"] = False
        result, ai = self.invoke("analyze", analyzer, make_schedule(), force=True)
        self.assertIsNone(result)
        ai.analyze.assert_not_called()
        analyzer.ctx.create_scheduler.assert_not_called()

    def test_delivery_result_records_only_full_or_partial_acceptance(self):
        for configured, sent, partial in ((False, False, False), (True, False, False), (True, True, False), (True, False, True)):
            with self.subTest(configured=configured, sent=sent, partial=partial):
                analyzer = make_analyzer()
                dispatcher = analyzer.ctx.create_notification_dispatcher.return_value
                dispatcher.send_report.return_value = NamedDeliveryResult(configured, sent, partial)
                result, _ai = self.invoke("push", analyzer, make_schedule())
                self.assertIs(result, sent)
                gate = analyzer.ctx.create_scheduler.return_value
                if sent or partial:
                    gate.record_execution.assert_called_once_with("morning", "push", "2026-10-02")
                else:
                    gate.record_execution.assert_not_called()

    def test_disabled_or_unconfigured_notification_does_not_send_despite_force(self):
        for key, value in (("ENABLE_NOTIFICATION", False), ("EMAIL_TO", "")):
            analyzer = make_analyzer()
            analyzer.ctx.config[key] = value
            result, _ai = self.invoke("push", analyzer, make_schedule(), force=True)
            self.assertFalse(result)
            analyzer.ctx.create_scheduler.assert_not_called()
            analyzer.ctx.create_notification_dispatcher.assert_not_called()

    def test_mode_content_rules_and_rss_only_delivery_remain_distinct(self):
        for mode in ("incremental", "current", "daily"):
            analyzer = make_analyzer()
            analyzer.report_mode = mode
            self.assertEqual(analyzer._has_valid_content([], {"p": {"new": {}}}), mode == "daily")
            self.assertFalse(analyzer._has_valid_content([{"count": 0}], {"p": {}}))
            self.assertTrue(analyzer._has_valid_content([{"count": 1}], {}))
            with patch.object(analyzer, "_manual_force_run", return_value=False), redirect_stdout(io.StringIO()):
                result = analyzer._send_notification_if_needed(
                    [], "report", rss_items=[{"count": 1, "titles": [{"title": "RSS"}]}],
                    schedule=make_schedule(),
                )
            self.assertTrue(result)


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
