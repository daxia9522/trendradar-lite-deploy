"""Permanent, offline characterization of the daily application boundaries.

The fixtures use synthetic storage, crawler, AI and mail doubles.  The real
selection/statistics and orchestration run against a fixed clock and config.
"""

import copy
import ast
import inspect
import io
import os
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from trendradar.ai import AIAnalysisResult
from trendradar.core.analyzer import count_word_frequency
from trendradar.core.frequency import matches_word_groups
from trendradar.core.scheduler import ResolvedSchedule
from trendradar.daily_flow import runner as runner_module
from trendradar.daily_flow.runner import DailyRunner
from trendradar.daily_flow.capture import FrozenCapture, capture_sources
from trendradar.daily_flow.models import CrawlResult, KeywordRules, ModeInput, PreparedReportInput, RSSCollection, RSSResult
from trendradar.daily_flow.publication import PublicationCoordinator
from trendradar.notification import NotificationDispatcher
from trendradar.notification.models import EmailDeliveryResult, PreparedEmail
from trendradar.storage.base import RSSData, RSSItem
from trendradar.storage.publication import LocalPublicationStore


NOW = datetime(2026, 10, 2, 10, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
DATE = "2026-10-02"
TIME = "10-00"


def schedule_for(mode="current", **overrides):
    values = dict(
        period_key="morning", period_name="早间速览", day_plan="synthetic",
        collect=True, analyze=True, push=True, report_mode=mode,
        once_analyze=True, once_push=True, frequency_file="synthetic-words.txt",
    )
    values.update(overrides)
    return ResolvedSchedule(**values)


def rss_data(*titles):
    return RSSData(
        DATE, TIME,
        {"feed": [RSSItem(
            title=title, feed_id="feed", url=f"https://rss.example.invalid/{index}/{title}",
            published_at=f"2026-10-02T0{index + 6}:00:00+08:00",
            summary=f"summary:{title}", author="synthetic author",
        ) for index, title in enumerate(titles)]},
        {"feed": "RSS source"},
    )


class DailyFixture:
    """Real capture/coordinator/private temporary ledger; fake external I/O only."""

    def __init__(self, mode="current"):
        self.events = []
        self.schedule = schedule_for(mode)
        self.config = {
            "ENABLE_CRAWLER": True, "ENABLE_NOTIFICATION": True,
            "EMAIL_FROM": "from@example.invalid", "EMAIL_PASSWORD": "synthetic",
            "EMAIL_TO": "to@example.invalid", "TIMEZONE": "Asia/Shanghai",
            "AI": {"MODEL": "synthetic/model"},
            "AI_ANALYSIS": {"ENABLED": True, "INCLUDE_STANDALONE": False},
            "STORAGE": {"FORMATS": {"HTML": True}},
            "DISPLAY": {
                "REGIONS": {"RSS": True, "STANDALONE": False},
                "STANDALONE": {"PLATFORMS": ["p"], "RSS_FEEDS": ["feed"], "MAX_ITEMS": 0},
            },
            "RSS": {
                "ENABLED": True,
                "FEEDS": [{"id": "feed", "url": "https://rss.example.invalid/feed"}],
                "FRESHNESS_FILTER": {"ENABLED": True, "MAX_AGE_DAYS": 3},
            },
        }
        self.current = {"p": {
            "HOT now": {"ranks": [2], "url": "https://hot.example.invalid/now"},
            "BLOCK hot": {"ranks": [1], "url": "https://hot.example.invalid/blocked"},
        }}
        self.history = {"p": {
            "HOT earlier": {"ranks": [4], "url": "https://hot.example.invalid/old"},
            **copy.deepcopy(self.current["p"]),
        }}
        self.names = {"p": "Hot source"}
        self.new_titles = {"p": {"HOT now": self.current["p"]["HOT now"]}}
        self.title_info = {"p": {
            title: {"first_time": "09-00", "last_time": "09-00" if title == "HOT earlier" else TIME,
                    "count": 2, "ranks": [5], "url": value["url"]}
            for title, value in self.history["p"].items()
        }}
        self.rss_today = rss_data("RSS earlier", "RSS now", "BLOCK rss")
        self.rss_latest = RSSData(DATE, TIME, {"feed": self.rss_today.items["feed"][1:]}, self.rss_today.id_to_name)
        self.rss_new = {"feed": self.rss_today.items["feed"][1:2]}
        self.source_days = {
            ("news", DATE): {"date": DATE, "present": True, "latest_time": TIME, "items": [
                {**copy.deepcopy(value), "title": title, "source_id": "p", "source_name": "Hot source",
                 "first_time": TIME if title == "HOT now" else "09-00",
                 "last_time": self.title_info["p"][title]["last_time"], "count": 2}
                for title, value in self.history["p"].items()
            ]},
            ("rss", DATE): {"date": DATE, "present": True, "latest_time": TIME, "items": [
                {**item.to_dict(), "feed_name": "RSS source",
                 "first_time": TIME if item.title == "RSS now" else "09-00",
                 "last_time": "09-00" if item.title == "RSS earlier" else TIME}
                for item in self.rss_today.items["feed"]
            ]},
        }
        self.reader = Mock()
        self.reader.read_day.side_effect = self.read_day
        self.temp = tempfile.TemporaryDirectory()
        self.publication_store = LocalPublicationStore(self.temp.name)
        unittest.addModuleCleanup(self.temp.cleanup)
        unittest.addModuleCleanup(self.publication_store.close)
        self.coordinator = PublicationCoordinator(self.publication_store, lambda: NOW)
        self.email = PreparedEmail(
            "from@example.invalid", ("to@example.invalid",), "Synthetic subject",
            b"From: from@example.invalid\r\n\r\nSynthetic report",
            "<daily-fixture@example.invalid>", "Fri, 02 Oct 2026 10:00:00 +0800",
        )
        # Establish an actual previously published report, never a hand-written
        # manifest. Earlier/blocked stories are known; the two 'now' items are not.
        prior_days = copy.deepcopy(self.source_days)
        for day in prior_days.values():
            day["items"] = [item for item in day["items"] if not item["title"].endswith(" now")]
        prior_reader = SimpleNamespace(read_day=lambda kind, date: copy.deepcopy(prior_days.get(
            (kind, date), {"date": date, "present": False, "items": [], "latest_time": ""},
        )))
        with redirect_stdout(io.StringIO()):
            baseline = self.coordinator.capture_baseline()
            prior = capture_sources(prior_reader, baseline, NOW - timedelta(hours=1),
                                    platform_ids=["p"], feed_ids=["feed"])
            prior_id = self.coordinator.claim_generation(DATE + ":earlier")
            self.coordinator.prepare(prior_id, self.email, prior.to_dict())
            self.coordinator.deliver(prior_id, SimpleNamespace(send_prepared=lambda prepared, *, recipients:
                EmailDeliveryResult(True, requested=recipients, accepted=recipients)))
        self.initial_baseline = self.coordinator.capture_baseline()
        self.storage = Mock()
        self.storage.backend_name = "synthetic"
        self.storage.save_news_data.side_effect = self.event("hotlist.save", True)
        self.storage.save_txt_snapshot.side_effect = self.event("hotlist.snapshot", None)
        self.storage.save_rss_data.side_effect = self.event("rss.save", True)
        self.storage.get_publication_store.return_value = self.publication_store
        for name in ("get_rss_data", "get_latest_rss_data", "detect_new_rss_items"):
            getattr(self.storage, name).side_effect = AssertionError("mutable RSS reread: " + name)
        self.fetcher = Mock()
        self.fetcher.feeds = ["synthetic"]
        self.fetcher.fetch_all.side_effect = self.event("rss.fetch", self.rss_latest)
        self.gate = Mock()
        self.gate.resolve.side_effect = self.event("schedule.resolve", self.schedule)
        self.gate.already_executed.return_value = False
        self.gate.record_execution.side_effect = lambda _period, action, _date: self.events.append(f"record.{action}")
        self.ai_result = AIAnalysisResult(success=True)
        self.ai = Mock()
        self.ai.analyze.side_effect = self.event("ai.analyze", self.ai_result)
        self.dispatcher = Mock(spec_set=NotificationDispatcher)
        self.dispatcher.prepare_report.side_effect = self.event("email.prepare", self.email)
        self.dispatcher.send_prepared.side_effect = self.event(
            "email.send", EmailDeliveryResult(True, requested=self.email.recipients, accepted=self.email.recipients))
        self.dispatcher.send_report.side_effect = AssertionError("daily must persist before send")
        self.ctx = Mock()
        self.ctx.config = self.config
        self.ctx.platform_ids = ["p"]
        self.ctx.platforms = [{"id": "p", "name": "Hot source", "expected_domain": " hot.example.invalid "}]
        self.ctx.timezone = "Asia/Shanghai"
        self.ctx.rss_config = self.config["RSS"]
        self.ctx.rss_feeds = self.config["RSS"]["FEEDS"]
        self.ctx.rss_enabled = True
        self.ctx.display_mode = "keyword"
        self.ctx.rank_threshold = 50
        self.ctx.weight_config = {"RANK_WEIGHT": .4, "FREQUENCY_WEIGHT": .3, "HOTNESS_WEIGHT": .3}
        self.ctx.get_time.return_value = NOW
        self.ctx.format_date.return_value = DATE
        self.ctx.format_time.return_value = TIME
        self.ctx.create_scheduler.return_value = self.gate
        self.ctx.create_publication_source_reader.return_value = self.reader
        self.ctx.detect_new_titles.side_effect = AssertionError("legacy crawler-relative novelty")
        self.ctx.read_today_titles.side_effect = AssertionError("mutable hotlist reread")
        self.ctx.load_frequency_words.side_effect = self.event("input.words", ([], [], ["BLOCK"]))
        self.ctx.matches_word_groups.side_effect = matches_word_groups
        self.ctx.count_frequency.side_effect = self.count_frequency
        self.ctx.generate_html.side_effect = self.event("html.generate", "synthetic-report.html")
        self.ctx.create_notification_dispatcher.return_value = self.dispatcher
        self.ctx.cleanup.side_effect = self.event("cleanup", None)
        self.manual_force = Mock(return_value=False)
        self.freshness_check = Mock(return_value=True)
        self.days_old = Mock(return_value=0)
        self.analyzer = DailyRunner.__new__(DailyRunner)
        self.analyzer.ctx = self.ctx
        self.analyzer.report_mode = "daily"  # schedule must take effect before RSS
        self.analyzer.frequency_file = None
        self.analyzer.rank_threshold = 50
        self.analyzer.request_interval = 7
        self.analyzer.proxy_url = None
        self.analyzer.storage_manager = self.storage
        self.analyzer.is_github_actions = False
        self.analyzer.is_docker_container = True
        self.analyzer.data_fetcher = Mock()
        self.analyzer.data_fetcher.crawl_websites.side_effect = self.event(
            "hotlist.fetch", (self.current, self.names, ["failed"]),
        )

    def read_day(self, kind, date):
        self.events.append(f"capture.{kind}:{date}")
        return copy.deepcopy(self.source_days.get(
            (kind, date), {"date": date, "present": False, "items": [], "latest_time": ""}))

    def prepare(self):
        self.analyzer.report_mode = self.schedule.report_mode
        return self.analyzer.prepare_report(CrawlResult(self.current, self.names, ["failed"]), RSSCollection(True))

    def assert_no_legacy_reads(self):
        self.ctx.read_today_titles.assert_not_called()
        self.ctx.detect_new_titles.assert_not_called()
        self.storage.get_rss_data.assert_not_called()
        self.storage.get_latest_rss_data.assert_not_called()
        self.storage.detect_new_rss_items.assert_not_called()

    def event(self, label, result):
        def invoke(*_args, **_kwargs):
            self.events.append(label)
            return result
        return invoke

    def count_frequency(self, data, groups, filters, names, info, new, **options):
        self.events.append("input.count")
        return count_word_frequency(
            data, groups, filters, names, title_info=info, new_titles=new,
            rank_threshold=50, is_first_crawl_func=lambda: False, **options,
        )

    def boundaries(self, *, force=False):
        stack = ExitStack()
        self.manual_force.return_value = force
        stack.enter_context(patch.object(self.analyzer, "_manual_force_run", self.manual_force))
        # Defaults are bound when the runner is defined: inject the callables
        # explicitly instead of patching module globals after import.
        convert_items = partial(
            self.analyzer._convert_rss_items_to_list,
            within_days=self.freshness_check, days_old=self.days_old,
        )
        stack.enter_context(patch.object(self.analyzer, "_convert_rss_items_to_list", convert_items))
        stack.enter_context(patch("trendradar.crawler.rss.RSSFetcher.from_config", return_value=self.fetcher))
        stack.enter_context(patch.object(runner_module, "AIAnalyzer", return_value=self.ai))
        stack.enter_context(redirect_stdout(io.StringIO()))
        return stack


def stat_titles(stats):
    return [title["title"] for stat in stats or [] for title in stat.get("titles", [])]


class TemporaryDailyCase(unittest.TestCase):
    """Keep collection's relative output directory out of the checkout."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        old_cwd = Path.cwd()
        os.chdir(self.temp.name)
        self.addCleanup(os.chdir, old_cwd)


class DailyFlowCharacterizationTests(TemporaryDailyCase):
    def test_real_run_keeps_order_and_mode_specific_candidates(self):
        for mode in ("incremental", "current", "daily"):
            with self.subTest(mode=mode):
                fixture = DailyFixture(mode)
                with fixture.boundaries():
                    fixture.analyzer.run()
                expected = [
                    "schedule.resolve", "hotlist.fetch", "hotlist.save", "hotlist.snapshot",
                    "rss.fetch", "rss.save", "capture.news:2026-10-01", "capture.news:2026-10-02",
                    "capture.rss:2026-10-01", "capture.rss:2026-10-02", "input.words",
                ]
                expected.extend(["input.count", "ai.analyze", "record.analyze", "html.generate",
                                 "email.prepare", "email.send", "record.push", "cleanup"])
                self.assertEqual(fixture.events, expected)
                self.assertEqual(fixture.freshness_check.call_count, {"incremental": 1, "current": 3, "daily": 4}[mode])
                self.assertTrue(all(call.args[1:] == (3, "Asia/Shanghai") for call in fixture.freshness_check.call_args_list))
                fixture.days_old.assert_not_called()
                self.assertEqual(fixture.analyzer.report_mode, mode)
                self.assertEqual(fixture.analyzer.frequency_file, "synthetic-words.txt")
                fixture.ctx.load_frequency_words.assert_called_once_with("synthetic-words.txt")
                fixture.assert_no_legacy_reads()
                self.assertEqual(len(fixture.reader.read_day.call_args_list), 4)
                self.assertEqual(len({call.args for call in fixture.reader.read_day.call_args_list}), 4)
                fixture.analyzer.data_fetcher.crawl_websites.assert_called_once_with(
                    [("p", "Hot source")], request_interval=7, domain_rules={"p": "hot.example.invalid"},
                )
                saved = fixture.storage.save_news_data.call_args.args[0]
                self.assertEqual((saved.date, saved.crawl_time, saved.failed_ids), (DATE, TIME, ["failed"]))
                ai_args = fixture.ai.analyze.call_args.kwargs
                hot_expected = {"HOT now", "HOT earlier"} if mode == "daily" else {"HOT now"}
                rss_expected = {"RSS now", "RSS earlier"} if mode == "daily" else {"RSS now"}
                self.assertEqual(set(stat_titles(ai_args["stats"])), hot_expected)
                self.assertEqual(set(stat_titles(ai_args["rss_stats"])), rss_expected)
                self.assertEqual(ai_args["report_mode"], mode)
                self.assertEqual(ai_args["platforms"], ["Hot source", "RSS source"])
                self.assertEqual(ai_args["keywords"], ["全部新闻", "全部 RSS"])
                # Raw standalone is not gated by either consumer's display/AI toggle.
                standalone = ai_args["standalone_data"]
                self.assertEqual([item["title"] for item in standalone["platforms"][0]["items"]],
                                 ["HOT now"] if mode == "incremental" else ["BLOCK hot", "HOT now"])
                html = fixture.ctx.generate_html.call_args
                self.assertIs(html.args[0], ai_args["stats"])
                self.assertIs(html.kwargs["rss_items"], ai_args["rss_stats"])
                self.assertIs(html.kwargs["ai_analysis"], fixture.ai_result)
                self.assertIs(html.kwargs["standalone_data"], standalone)
                self.assertEqual(html.kwargs["failed_ids"], ["failed"])
                self.assertEqual(html.kwargs["frequency_file"], "synthetic-words.txt")
                self.assertEqual(html.kwargs["captured_at"], NOW.isoformat())
                self.assertIsInstance(html.kwargs["keyword_rules"], KeywordRules)
                self.assertEqual({item["title"]: item["is_new"] for stat in ai_args["stats"] for item in stat["titles"]},
                                 {title: title == "HOT now" for title in hot_expected})
                self.assertEqual({item["title"]: item["is_new"] for stat in ai_args["rss_stats"] for item in stat["titles"]},
                                 {title: title == "RSS now" for title in rss_expected})
                self.assertEqual(fixture.gate.record_execution.call_args_list[0].args, ("morning", "analyze", DATE))
                self.assertEqual(fixture.gate.record_execution.call_args_list[1].args, ("morning", "push", DATE))

    def test_report_keyword_failure_stops_after_capture_and_never_delivers(self):
        for mode in ("incremental", "current", "daily"):
            for error_type in (FileNotFoundError, ValueError):
                with self.subTest(mode=mode, error=error_type.__name__):
                    fixture = DailyFixture(mode)
                    fixture.ctx.rss_enabled = False
                    fixture.ctx.load_frequency_words.side_effect = error_type("synthetic keyword failure")
                    with fixture.boundaries(), patch("traceback.print_exc"):
                        with self.assertRaisesRegex(error_type, "synthetic keyword failure"):
                            fixture.analyzer.run()
                    fixture.ctx.load_frequency_words.assert_called_once_with("synthetic-words.txt")
                    self.assertEqual([call.args[0] for call in fixture.reader.read_day.call_args_list], ["news", "news"])
                    fixture.assert_no_legacy_reads()
                    fixture.ctx.count_frequency.assert_not_called()
                    fixture.ai.analyze.assert_not_called()
                    fixture.ctx.generate_html.assert_not_called()
                    fixture.dispatcher.prepare_report.assert_not_called()
                    fixture.dispatcher.send_prepared.assert_not_called()
                    fixture.gate.record_execution.assert_not_called()
                    fixture.ctx.cleanup.assert_called_once_with()
                    self.assertEqual(fixture.coordinator.capture_baseline(), fixture.initial_baseline)
                    self.assertIsNone(fixture.coordinator.window_report(DATE + ":morning"))

    def test_collect_false_and_disabled_crawler_still_cleanup(self):
        for crawler_enabled in (False, True):
            fixture = DailyFixture()
            fixture.config["ENABLE_CRAWLER"] = crawler_enabled
            fixture.schedule.collect = False
            with fixture.boundaries():
                fixture.analyzer.run()
            self.assertEqual(fixture.events, ["schedule.resolve", "cleanup"])
            fixture.analyzer.data_fetcher.crawl_websites.assert_not_called()
            fixture.reader.read_day.assert_not_called()
            fixture.dispatcher.send_prepared.assert_not_called()

    def test_force_run_does_not_override_collect_false(self):
        fixture = DailyFixture()
        fixture.schedule.collect = False
        with fixture.boundaries(force=True):
            fixture.analyzer.run()
        self.assertEqual(fixture.events, ["schedule.resolve", "cleanup"])
        fixture.analyzer.data_fetcher.crawl_websites.assert_not_called()
        fixture.fetcher.fetch_all.assert_not_called()
        fixture.dispatcher.prepare_report.assert_not_called()
        self.assertEqual(fixture.coordinator.capture_baseline(), fixture.initial_baseline)

    def test_cleanup_runs_after_hotlist_html_or_email_preparation_error(self):
        for boundary in ("crawl", "html", "prepare"):
            with self.subTest(boundary=boundary):
                fixture = DailyFixture()
                error = RuntimeError(f"synthetic {boundary} failure")
                if boundary == "crawl":
                    fixture.analyzer.data_fetcher.crawl_websites.side_effect = error
                elif boundary == "html":
                    fixture.ctx.generate_html.side_effect = error
                else:
                    fixture.dispatcher.prepare_report.side_effect = error
                with fixture.boundaries(), patch("traceback.print_exc"):
                    with self.assertRaisesRegex(RuntimeError, f"synthetic {boundary} failure"):
                        fixture.analyzer.run()
                self.assertEqual(fixture.events[-1], "cleanup")
                fixture.ctx.cleanup.assert_called_once_with()
                if boundary == "crawl":
                    fixture.fetcher.fetch_all.assert_not_called()
                fixture.dispatcher.send_prepared.assert_not_called()
                self.assertEqual(fixture.coordinator.capture_baseline(), fixture.initial_baseline)
                self.assertIsNone(fixture.coordinator.window_report(DATE + ":morning"))

    def test_unhandled_transport_error_is_durable_unknown_and_cleanup_still_runs(self):
        fixture = DailyFixture()
        fixture.dispatcher.send_prepared.side_effect = RuntimeError("synthetic transport failure")
        with fixture.boundaries():
            fixture.analyzer.run()
            fixture.analyzer.run()
        fixture.dispatcher.send_prepared.assert_called_once()
        self.assertEqual(fixture.ctx.cleanup.call_count, 2)
        self.assertEqual(fixture.events[-1], "cleanup")
        status = fixture.coordinator.status()["reports"][0]
        self.assertEqual(status["state"], "ATTENTION")
        self.assertEqual(status["outcome_counts"]["unknown"], 1)
        self.assertFalse(status["published"])
        self.assertEqual(fixture.coordinator.retryable_reports(), [])
        self.assertEqual(fixture.coordinator.capture_baseline(), fixture.initial_baseline)

    def test_occupied_publication_window_keeps_collection_but_not_new_generation(self):
        fixture = DailyFixture()
        with fixture.boundaries():
            fixture.analyzer.run()
            fixture.analyzer.run()
        self.assertEqual(fixture.analyzer.data_fetcher.crawl_websites.call_count, 2)
        self.assertEqual(fixture.storage.save_news_data.call_count, 2)
        self.assertEqual(fixture.fetcher.fetch_all.call_count, 2)
        self.assertEqual(fixture.storage.save_rss_data.call_count, 2)
        self.assertEqual(fixture.reader.read_day.call_count, 4)
        fixture.ctx.load_frequency_words.assert_called_once_with("synthetic-words.txt")
        fixture.ai.analyze.assert_called_once()
        fixture.ctx.generate_html.assert_called_once()
        fixture.dispatcher.send_prepared.assert_called_once()
        self.assertEqual(fixture.ctx.cleanup.call_count, 2)

    def test_non_push_collection_keeps_items_new_until_next_publication(self):
        fixture = DailyFixture("incremental")
        fixture.schedule.push = False
        with fixture.boundaries():
            fixture.analyzer.run()
            self.assertEqual(fixture.coordinator.capture_baseline(), fixture.initial_baseline)
            fixture.dispatcher.send_prepared.assert_not_called()
            fixture.schedule.push = True
            fixture.analyzer.run()
        self.assertEqual(fixture.ctx.generate_html.call_count, 2)
        for html in fixture.ctx.generate_html.call_args_list:
            self.assertEqual(stat_titles(html.args[0]), ["HOT now"])
            self.assertEqual(stat_titles(html.kwargs["rss_items"]), ["RSS now"])
            self.assertTrue(html.args[0][0]["titles"][0]["is_new"])
            self.assertTrue(html.kwargs["rss_items"][0]["titles"][0]["is_new"])
        fixture.dispatcher.send_prepared.assert_called_once()
        self.assertGreater(fixture.coordinator.capture_baseline()["sequence"], fixture.initial_baseline["sequence"])

    def test_ai_error_is_contained_and_does_not_record_analysis(self):
        fixture = DailyFixture()
        fixture.ai.analyze.side_effect = ValueError("synthetic AI failure")
        with fixture.boundaries(), patch("traceback.print_exc"):
            fixture.analyzer.run()
        self.assertFalse(fixture.ctx.generate_html.call_args.kwargs["ai_analysis"].success)
        self.assertEqual(fixture.events[-3:], ["email.send", "record.push", "cleanup"])
        fixture.gate.record_execution.assert_called_once_with("morning", "push", DATE)

    def test_html_disabled_keeps_ai_but_never_pretends_to_send_or_publish(self):
        fixture = DailyFixture()
        fixture.config["STORAGE"]["FORMATS"]["HTML"] = False
        with fixture.boundaries():
            fixture.analyzer.run()
        fixture.ai.analyze.assert_called_once()
        fixture.ctx.generate_html.assert_not_called()
        fixture.dispatcher.prepare_report.assert_not_called()
        fixture.dispatcher.send_prepared.assert_not_called()
        fixture.gate.record_execution.assert_called_once_with("morning", "analyze", DATE)
        self.assertEqual(fixture.coordinator.capture_baseline(), fixture.initial_baseline)
        status = fixture.coordinator.status()["reports"][0]
        self.assertEqual((status["state"], status["reason"]), ("NO_EMAIL", "no_html"))
        self.assertFalse(status["window_owned"])

    def test_constructor_wiring_uses_existing_context_and_fetcher_factories(self):
        fixture = DailyFixture()
        fixture.config.update(PLATFORMS=fixture.ctx.platforms, REQUEST_INTERVAL=7, REPORT_MODE="daily", USE_PROXY=True, DEFAULT_PROXY="http://proxy.example.invalid")
        fixture.ctx.get_storage_manager.return_value = fixture.storage
        with patch.object(runner_module, "AppContext", return_value=fixture.ctx) as context_factory:
            with patch.object(runner_module, "DataFetcher") as fetcher_factory, patch.dict(os.environ, {"GITHUB_ACTIONS": "false", "DOCKER_CONTAINER": "true"}, clear=True):
                with redirect_stdout(io.StringIO()):
                    analyzer = DailyRunner(config=fixture.config)
        context_factory.assert_called_once_with(fixture.config)
        fetcher_factory.assert_called_once_with("http://proxy.example.invalid", api_url=None, api_fallback_urls=None)
        fixture.ctx.get_storage_manager.assert_called_once_with()
        self.assertIs(analyzer.storage_manager, fixture.storage)
        self.assertTrue(analyzer.is_docker_container)

    def test_browser_open_stays_after_delivery_and_before_cleanup(self):
        fixture = DailyFixture()
        fixture.analyzer.is_docker_container = False
        with fixture.boundaries(), patch.dict(os.environ, {"DISPLAY": "synthetic-display"}):
            with patch.object(runner_module.webbrowser, "open", side_effect=fixture.event("browser.open", True)) as browser:
                fixture.analyzer.run()
        self.assertEqual(fixture.events[-4:], ["email.send", "record.push", "browser.open", "cleanup"])
        browser.assert_called_once_with("file://" + str(Path("synthetic-report.html").resolve()))

    def test_disabled_freshness_does_not_suppress_rss_or_action_checks(self):
        fixture = DailyFixture()
        fixture.config["RSS"]["FRESHNESS_FILTER"]["ENABLED"] = False
        with fixture.boundaries():
            fixture.analyzer.run()
        self.assertEqual(fixture.events[-6:], ["record.analyze", "html.generate", "email.prepare", "email.send", "record.push", "cleanup"])
        self.assertEqual(stat_titles(fixture.ctx.generate_html.call_args.args[0]), ["HOT now"])
        self.assertEqual(stat_titles(fixture.ctx.generate_html.call_args.kwargs["rss_items"]), ["RSS now"])
        fixture.gate.already_executed.assert_called_once_with("morning", "analyze", DATE)
        fixture.freshness_check.assert_not_called()
        fixture.days_old.assert_not_called()


class DailyInputCharacterizationTests(TemporaryDailyCase):
    def test_current_metadata_keeps_missing_defaults_and_rank_history_unmodified(self):
        fixture = DailyFixture()
        data = {"p": {"one": {"ranks": [4, 2], "url": "url", "mobileUrl": "mobile"}, "two": {}}}
        original = copy.deepcopy(data)
        metadata = fixture.analyzer._prepare_current_title_info(data, TIME)
        self.assertEqual(metadata, {"p": {
            "one": {"first_time": TIME, "last_time": TIME, "count": 1, "ranks": [4, 2], "url": "url", "mobileUrl": "mobile"},
            "two": {"first_time": TIME, "last_time": TIME, "count": 1, "ranks": [], "url": "", "mobileUrl": ""},
        }})
        self.assertEqual(data, original)

    def test_standalone_orders_current_rank_merges_history_and_limits_each_source(self):
        fixture = DailyFixture()
        fixture.config["DISPLAY"]["STANDALONE"]["MAX_ITEMS"] = 1
        original = copy.deepcopy((fixture.history, fixture.title_info))
        result = fixture.analyzer._prepare_standalone_data(
            fixture.history, fixture.names, fixture.title_info,
            [{"feed_id": "feed", "feed_name": "RSS source", "title": "first", "url": "url"},
             {"feed_id": "feed", "title": "second"}],
        )
        self.assertEqual(result, {
            "platforms": [{"id": "p", "name": "Hot source", "items": [{
                "title": "BLOCK hot", "url": "https://hot.example.invalid/blocked", "mobileUrl": "",
                "rank": 1, "ranks": [5, 1], "first_time": "09-00", "last_time": TIME,
                "count": 2, "rank_timeline": [],
            }]}],
            "rss_feeds": [{"id": "feed", "name": "RSS source", "items": [{
                "title": "first", "url": "url", "published_at": "", "author": "",
            }]}],
        })
        self.assertEqual((fixture.history, fixture.title_info), original)

    def test_standalone_empty_unconfigured_or_missing_sources_returns_none(self):
        fixture = DailyFixture()
        self.assertIsNone(fixture.analyzer._prepare_standalone_data({}, {}, {}, []))
        fixture.config["DISPLAY"]["STANDALONE"] = {}
        self.assertIsNone(fixture.analyzer._prepare_standalone_data(fixture.current, fixture.names))

    def test_missing_captured_history_never_falls_back_to_unfrozen_crawl_data(self):
        for mode in ("incremental", "current", "daily"):
            with self.subTest(mode=mode):
                fixture = DailyFixture(mode)
                fixture.source_days.clear()
                with fixture.boundaries():
                    prepared = fixture.prepare()
                    artifacts = fixture.analyzer.analyze_report(prepared, fixture.schedule)
                self.assertEqual(prepared.hotlist.results, {})
                self.assertEqual(prepared.hotlist.new_titles, {})
                self.assertEqual(stat_titles(artifacts.stats), [])
                self.assertTrue(fixture.current)  # tempting but untrusted fallback is nonempty
                self.assertFalse(prepared.capture.to_dict()["coverage"]["news"]["complete"])
                fixture.ai.analyze.assert_not_called()
                fixture.assert_no_legacy_reads()

    def test_preparation_reads_each_frozen_source_and_keyword_rules_once(self):
        fixture = DailyFixture()
        with fixture.boundaries():
            prepared = fixture.prepare()
        result = prepared.hotlist
        self.assertIsInstance(result, ModeInput)
        self.assertEqual(result._fields, ("results", "id_to_name", "title_info", "new_titles"))
        self.assertEqual(set(result.results["p"]), set(fixture.history["p"]))
        self.assertEqual(result.id_to_name, fixture.names)
        self.assertEqual(set(result.new_titles["p"]), {"HOT now"})
        self.assertIsInstance(prepared.capture, FrozenCapture)
        copy_of_capture = prepared.capture.to_dict()
        copy_of_capture["new_news"].clear()
        self.assertEqual(set(prepared.capture.to_dict()["new_news"]["p"]), {"HOT now"})
        self.assertEqual(len({call.args for call in fixture.reader.read_day.call_args_list}), 4)
        self.assertEqual(fixture.reader.read_day.call_count, 4)
        fixture.ctx.create_publication_source_reader.assert_called_once_with()
        fixture.ctx.load_frequency_words.assert_called_once_with(None)
        fixture.assert_no_legacy_reads()

    def test_captured_keyword_validation_preserves_failure_and_shape_checks(self):
        for mode in ("incremental", "current", "daily"):
            for failure in (FileNotFoundError("synthetic missing words"), ValueError("synthetic invalid words"), ([], [])):
                with self.subTest(mode=mode, failure=repr(failure)):
                    fixture = DailyFixture(mode)
                    if isinstance(failure, Exception):
                        fixture.ctx.load_frequency_words.side_effect = failure
                    else:
                        fixture.ctx.load_frequency_words.side_effect = lambda *_args: failure
                    expected_error = type(failure) if isinstance(failure, Exception) else TypeError
                    with fixture.boundaries(), self.assertRaises(expected_error):
                        fixture.prepare()
                    fixture.ctx.load_frequency_words.assert_called_once_with(None)
                    self.assertEqual(fixture.reader.read_day.call_count, 4)
                    fixture.ctx.count_frequency.assert_not_called()
                    fixture.dispatcher.send_prepared.assert_not_called()
                    fixture.assert_no_legacy_reads()

    def test_unreadable_news_preserves_its_coverage_while_rss_can_publish(self):
        for mode in ("incremental", "current", "daily"):
            with self.subTest(mode=mode):
                fixture = DailyFixture(mode)

                def read(kind, date):
                    if kind == "news":
                        raise OSError("synthetic source failure")
                    return fixture.read_day(kind, date)

                fixture.reader.read_day.side_effect = read
                with fixture.boundaries():
                    fixture.analyzer.run()
                self.assertEqual(stat_titles(fixture.ctx.generate_html.call_args.args[0]), [])
                self.assertIn("RSS now", stat_titles(fixture.ctx.generate_html.call_args.kwargs["rss_items"]))
                fixture.dispatcher.send_prepared.assert_called_once()
                baseline = fixture.coordinator.capture_baseline()
                self.assertEqual(baseline["coverage"]["news"]["through"], fixture.initial_baseline["coverage"]["news"]["through"])
                self.assertFalse(baseline["coverage"]["news"]["complete"])
                self.assertGreater(baseline["coverage"]["rss"]["through"], fixture.initial_baseline["coverage"]["rss"]["through"])
                fixture.assert_no_legacy_reads()

    def test_ai_and_html_reuse_frozen_inputs_despite_late_source_and_rules_mutation(self):
        fixture = DailyFixture()
        rules = ([], [], ["BLOCK"])
        fixture.ctx.load_frequency_words.side_effect = fixture.event("input.words", rules)

        def mutate_during_ai(**_kwargs):
            fixture.source_days[("news", DATE)]["items"].clear()
            fixture.source_days[("rss", DATE)]["items"].clear()
            rules[2].append("HOT")
            return fixture.ai_result

        fixture.ai.analyze.side_effect = mutate_during_ai
        with fixture.boundaries():
            fixture.analyzer.run()
        html = fixture.ctx.generate_html.call_args
        self.assertEqual(stat_titles(html.args[0]), ["HOT now"])
        self.assertEqual(stat_titles(html.kwargs["rss_items"]), ["RSS now"])
        self.assertEqual(html.kwargs["keyword_rules"].global_filters, ["BLOCK"])
        status = fixture.coordinator.status()["reports"][0]
        snapshot = fixture.publication_store.get_snapshot(status["snapshot_id"])
        self.assertEqual(set(snapshot["input"]["new_news"]["p"]), {"HOT now"})
        self.assertEqual(snapshot["input"]["report_view"]["keywords"]["global_filters"], ["BLOCK"])
        self.assertEqual(fixture.reader.read_day.call_count, 4)
        fixture.ctx.load_frequency_words.assert_called_once_with("synthetic-words.txt")
        fixture.assert_no_legacy_reads()

    def test_capture_initialization_error_fails_closed_and_still_cleans_up(self):
        fixture = DailyFixture()
        fixture.ctx.create_publication_source_reader.side_effect = OSError("synthetic initialization failure")
        with fixture.boundaries(), self.assertRaisesRegex(OSError, "synthetic initialization failure"):
            fixture.analyzer.run()
        fixture.reader.read_day.assert_not_called()
        fixture.ctx.load_frequency_words.assert_not_called()
        fixture.ai.analyze.assert_not_called()
        fixture.ctx.generate_html.assert_not_called()
        fixture.dispatcher.send_prepared.assert_not_called()
        fixture.ctx.cleanup.assert_called_once_with()
        self.assertEqual(fixture.coordinator.capture_baseline(), fixture.initial_baseline)


class DailyRSSCharacterizationTests(TemporaryDailyCase):
    def test_disabled_rss_requires_no_feed_or_storage_initialization(self):
        analyzer = DailyRunner.__new__(DailyRunner)
        analyzer.ctx = SimpleNamespace(rss_enabled=False)
        self.assertEqual(analyzer._crawl_rss_data(), RSSCollection())

    def test_disabled_display_keeps_raw_rss_without_keyword_statistics(self):
        fixture = DailyFixture()
        fixture.config["DISPLAY"]["REGIONS"]["RSS"] = False
        with fixture.boundaries(), patch("trendradar.core.analyzer.count_rss_frequency") as count:
            result = fixture.prepare().rss
        self.assertIsNone(result.stats)
        self.assertIsNone(result.new_stats)
        self.assertEqual([item["title"] for item in result.raw_items], ["RSS now", "BLOCK rss"])
        fixture.assert_no_legacy_reads()
        count.assert_not_called()
        fixture.ctx.load_frequency_words.assert_called_once_with(None)

    def test_missing_keywords_aborts_instead_of_silently_dropping_global_filters(self):
        fixture = DailyFixture()
        fixture.ctx.load_frequency_words.side_effect = FileNotFoundError("synthetic")
        with fixture.boundaries(), self.assertRaisesRegex(FileNotFoundError, "synthetic"):
            fixture.prepare()
        fixture.ctx.load_frequency_words.assert_called_once_with(None)
        fixture.ctx.count_frequency.assert_not_called()
        fixture.dispatcher.prepare_report.assert_not_called()
        fixture.assert_no_legacy_reads()

    def test_incremental_no_new_after_publication_reads_capture_only_once(self):
        fixture = DailyFixture("incremental")
        with fixture.boundaries():
            fixture.analyzer.run()
            fixture.reader.read_day.reset_mock()
            fixture.ctx.load_frequency_words.reset_mock()
            result = fixture.prepare().rss
        self.assertEqual(result, RSSResult([], [], []))
        self.assertEqual(fixture.reader.read_day.call_count, 4)
        self.assertEqual(len({call.args for call in fixture.reader.read_day.call_args_list}), 4)
        fixture.ctx.load_frequency_words.assert_called_once_with("synthetic-words.txt")
        fixture.assert_no_legacy_reads()

    def test_no_history_keeps_consumed_views_empty_for_both_display_settings(self):
        for display in (True, False):
            fixture = DailyFixture("daily")
            fixture.config["DISPLAY"]["REGIONS"]["RSS"] = display
            del fixture.source_days[("rss", DATE)]
            with fixture.boundaries(), patch("trendradar.core.analyzer.count_rss_frequency") as count:
                prepared = fixture.prepare()
            self.assertEqual(prepared.rss, RSSResult([], [], []) if display else RSSResult(None, None, []))
            self.assertFalse(prepared.capture.to_dict()["coverage"]["rss"]["complete"])
            fixture.assert_no_legacy_reads()
            count.assert_not_called()

    def test_no_keyword_match_keeps_raw_for_standalone(self):
        fixture = DailyFixture()
        fixture.ctx.load_frequency_words.side_effect = lambda _file: ([{"required": [], "normal": ["not-present"], "group_key": "none"}], [], [])
        with fixture.boundaries():
            result = fixture.prepare().rss
        self.assertEqual(result.stats, [])
        self.assertEqual(result.new_stats, [])
        self.assertEqual([item["title"] for item in result.raw_items], ["RSS now", "BLOCK rss"])
        fixture.assert_no_legacy_reads()

    def test_rss_save_errors_are_contained_without_retry_or_premature_processing(self):
        for failure in (False, OSError("synthetic save failure"), ImportError("synthetic missing parser")):
            fixture = DailyFixture()
            if isinstance(failure, Exception):
                fixture.storage.save_rss_data.side_effect = failure
            else:
                fixture.storage.save_rss_data.side_effect = lambda _data: failure
            with fixture.boundaries():
                self.assertEqual(fixture.analyzer._crawl_rss_data(), RSSCollection())
            fixture.fetcher.fetch_all.assert_called_once_with()
            fixture.assert_no_legacy_reads()
        fixture = DailyFixture()
        with fixture.boundaries(), patch.object(fixture.analyzer, "_process_rss_data_by_mode") as prepare:
            self.assertEqual(fixture.analyzer._crawl_rss_data(), RSSCollection(True))
        prepare.assert_not_called()
        fixture.reader.read_day.assert_not_called()
        fixture.ctx.load_frequency_words.assert_not_called()
        fixture.assert_no_legacy_reads()

    def test_rss_failed_collection_does_not_advance_coverage_when_hotlist_publishes(self):
        fixture = DailyFixture()
        # This case isolates RSS failure; the shared fixture otherwise also
        # reports a failed hotlist source, which must hold the news frontier.
        fixture.analyzer.data_fetcher.crawl_websites.side_effect = fixture.event(
            "hotlist.fetch", (fixture.current, fixture.names, []),
        )
        fixture.storage.save_rss_data.side_effect = OSError("synthetic RSS persistence failure")
        with fixture.boundaries():
            fixture.analyzer.run()
        fixture.dispatcher.send_prepared.assert_called_once()
        baseline = fixture.coordinator.capture_baseline()
        self.assertEqual(baseline["coverage"]["rss"]["through"], fixture.initial_baseline["coverage"]["rss"]["through"])
        self.assertFalse(baseline["coverage"]["rss"]["complete"])
        self.assertGreater(baseline["coverage"]["news"]["through"], fixture.initial_baseline["coverage"]["news"]["through"])
        fixture.fetcher.fetch_all.assert_called_once_with()

    def test_failed_news_holds_its_frontier_without_blocking_healthy_rss(self):
        fixture = DailyFixture()  # news failed_ids=["failed"], healthy RSS
        with fixture.boundaries():
            fixture.analyzer.run()
        fixture.dispatcher.send_prepared.assert_called_once()
        baseline = fixture.coordinator.capture_baseline()
        self.assertEqual(baseline["coverage"]["news"]["through"],
                         fixture.initial_baseline["coverage"]["news"]["through"])
        self.assertFalse(baseline["coverage"]["news"]["complete"])
        self.assertGreater(baseline["coverage"]["rss"]["through"],
                           fixture.initial_baseline["coverage"]["rss"]["through"])
        with fixture.boundaries():
            captured = fixture.prepare().capture.to_dict()
        self.assertEqual(captured["new_news"], {})
        self.assertEqual(captured["new_rss"], [])

    def test_rss_disabled_empty_or_all_disabled_never_fetches(self):
        for case in ("disabled", "empty", "all-disabled"):
            fixture = DailyFixture()
            if case == "disabled":
                fixture.ctx.rss_enabled = False
            elif case == "empty":
                fixture.ctx.rss_feeds = []
            else:
                fixture.fetcher.feeds = []
            with fixture.boundaries():
                self.assertEqual(fixture.analyzer._crawl_rss_data(), RSSCollection())
            fixture.fetcher.fetch_all.assert_not_called()
            fixture.storage.save_rss_data.assert_not_called()

    def test_rss_fetcher_normalization_preserves_feed_overrides_without_mutation(self):
        fixture = DailyFixture()
        fixture.analyzer.proxy_url = "http://fallback.example.invalid"
        fixture.ctx.rss_config = {
            "REQUEST_INTERVAL": 31, "TIMEOUT": 8, "USE_PROXY": True, "PROXY_URL": "",
            "FRESHNESS_FILTER": {"ENABLED": False, "MAX_AGE_DAYS": 9},
        }
        feeds = [{"id": "one"}, {"id": "two", "max_items": 0, "max_age_days": 0}]
        before = copy.deepcopy(feeds)
        with patch("trendradar.crawler.rss.RSSFetcher.from_config") as factory:
            fixture.analyzer._create_rss_fetcher(feeds)
        factory.assert_called_once_with({
            "feeds": [{"id": "one", "max_items": 50}, {"id": "two", "max_items": 0, "max_age_days": 0}],
            "request_interval": 31, "timeout": 8, "use_proxy": True,
            "proxy_url": "http://fallback.example.invalid", "timezone": "Asia/Shanghai",
            "freshness_filter": {"enabled": False, "max_age_days": 9},
        })
        self.assertEqual(feeds, before)

    def test_freshness_injection_keeps_per_feed_overrides_undated_and_source_items(self):
        fixture = DailyFixture()
        fixture.ctx.rss_feeds = [
            {"id": "archive", "max_age_days": 0}, {"id": "invalid", "max_age_days": "bad"},
            {"id": "custom", "max_age_days": "7"},
        ]
        items = {feed: [RSSItem(title=feed, feed_id=feed, published_at="old")]
                 for feed in ("archive", "invalid", "custom", "default")}
        items["default"].append(RSSItem(title="undated", feed_id="default"))
        before = copy.deepcopy(items)
        fresh = Mock(return_value=False)
        with redirect_stdout(io.StringIO()):
            result = fixture.analyzer._convert_rss_items_to_list(items, {}, within_days=fresh)
        self.assertEqual([item["title"] for item in result], ["archive", "undated"])
        self.assertEqual([call.args for call in fresh.call_args_list], [
            ("old", 3, "Asia/Shanghai"), ("old", 7, "Asia/Shanghai"), ("old", 3, "Asia/Shanghai"),
        ])
        self.assertEqual(items, before)

    def test_freshness_and_debug_age_use_explicit_callbacks(self):
        fixture = DailyFixture()
        fixture.config["DEBUG"] = True
        items = {"feed": [RSSItem(title="x" * 55, feed_id="feed", published_at="old") for _ in range(12)]}
        output = io.StringIO()
        fresh = Mock(return_value=False)
        age = Mock(return_value=4.25)
        with redirect_stdout(output):
            result = fixture.analyzer._convert_rss_items_to_list(
                items, {"feed": "source"}, within_days=fresh, days_old=age,
            )
        self.assertEqual(result, [])
        self.assertEqual(fresh.call_count, 12)
        self.assertEqual(age.call_count, 12)
        self.assertTrue(all(call.args == ("old", 3, "Asia/Shanghai") for call in fresh.call_args_list))
        self.assertTrue(all(call.args == ("old", "Asia/Shanghai") for call in age.call_args_list))
        self.assertEqual(output.getvalue().count("[4.2天前] [source] " + "x" * 50 + "..."), 10)
        self.assertIn("还有 2 篇被过滤", output.getvalue())


class DailyBoundaryStructureTests(TemporaryDailyCase):
    def test_module_entry_uses_cli_and_prepared_daily_runner_boundary(self):
        from trendradar import __main__ as module_entry, cli

        self.assertIs(module_entry.main, cli.main)
        self.assertEqual(module_entry.__all__, ["main"])
        self.assertIs(cli.DailyRunner, DailyRunner)
        self.assertNotIn("NewsAnalyzer", vars(module_entry))
        self.assertEqual(list(inspect.signature(DailyRunner.analyze_report).parameters), ["self", "prepared", "schedule"])
        self.assertEqual(DailyRunner.__bases__, (object,))
        self.assertFalse(hasattr(DailyRunner, "_run_analysis_pipeline"))
        self.assertFalse(hasattr(DailyRunner, "_execute_mode_strategy"))

    def test_prepared_boundary_keeps_news_dicts_and_distinct_rss_views(self):
        from trendradar.daily_flow.models import CrawlResult, KeywordRules, ModeInput, PreparedReportInput, RSSResult

        hot = {"p": {"title": {"ranks": [1]}}}
        stats = [{"word": "topic", "titles": [{"title": "filtered RSS"}]}]
        raw = [{"title": "raw RSS"}]
        new_stats = [{"word": "topic", "titles": [{"title": "new RSS", "is_new": True}]}]
        crawl = CrawlResult(hot, {"p": "source"}, [])
        rss = RSSResult(stats, new_stats, raw)
        prepared = PreparedReportInput("current", ModeInput(crawl.results, crawl.id_to_name, {}, {}), KeywordRules([], [], []), rss)
        self.assertIs(prepared.hotlist.results, hot)
        self.assertIs(prepared.rss.stats, stats)
        self.assertIs(prepared.rss.raw_items, raw)
        self.assertIs(prepared.rss.new_stats, new_stats)
        self.assertEqual(RSSResult._fields, ("stats", "new_stats", "raw_items"))
        self.assertEqual(RSSResult(), (None, None, None))

    def test_new_boundary_results_are_forwarded_without_rewrapping(self):
        fixture = DailyFixture()
        crawl = CrawlResult(fixture.current, fixture.names, ["failed"])
        rss_result = RSSCollection(True)
        with fixture.boundaries(), patch.object(fixture.analyzer, "_crawl_data", return_value=crawl), \
                patch.object(fixture.analyzer, "_crawl_rss_data", return_value=rss_result), \
                patch.object(fixture.analyzer, "execute_report") as execute:
            fixture.analyzer.run()
        self.assertIs(execute.call_args.args[0], crawl)
        self.assertIs(execute.call_args.args[1], rss_result)

        fixture.ctx.load_frequency_words.reset_mock()
        with fixture.boundaries(), patch.object(fixture.analyzer, "_process_rss_data_by_mode",
                                                wraps=fixture.analyzer._process_rss_data_by_mode) as process:
            prepared = fixture.analyzer.prepare_report(crawl, rss_result)
        self.assertIsInstance(prepared.capture, FrozenCapture)
        self.assertIsInstance(prepared.rss, RSSResult)
        self.assertIsInstance(prepared.keywords, KeywordRules)
        process.assert_called_once()
        self.assertEqual(process.call_args.args[0], prepared.capture.to_dict())
        self.assertIs(process.call_args.args[1], prepared.keywords)
        self.assertEqual(prepared.failed_ids, crawl.failed_ids)
        fixture.ctx.load_frequency_words.assert_called_once_with("synthetic-words.txt")
        fixture.assert_no_legacy_reads()

        with fixture.boundaries(), patch.object(fixture.analyzer, "_process_rss_data_by_mode") as prepare:
            result = fixture.analyzer._crawl_rss_data()
        self.assertEqual(result, rss_result)
        prepare.assert_not_called()
        fixture.storage.save_rss_data.assert_called_once_with(fixture.rss_latest)

    def test_leaf_modules_do_not_import_runner_or_legacy_entry(self):
        from trendradar.daily_flow import collection, inputs, models, rss

        for module in (collection, inputs, models, rss):
            tree = ast.parse(inspect.getsource(module))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    self.assertNotIn(node.module, {"daily", "runner", "trendradar.daily", "trendradar.daily_flow.runner"})
                elif isinstance(node, ast.Import):
                    self.assertFalse({alias.name for alias in node.names} & {"trendradar.daily", "trendradar.daily_flow.runner"})


if __name__ == "__main__":
    unittest.main()
