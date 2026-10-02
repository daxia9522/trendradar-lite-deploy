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
from datetime import datetime
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from trendradar.ai import AIAnalysisResult
from trendradar.core.analyzer import count_word_frequency
from trendradar.core.scheduler import ResolvedSchedule
from trendradar.daily_flow import runner as runner_module
from trendradar.daily_flow.runner import DailyRunner
from trendradar.daily_flow.models import CrawlResult, KeywordRules, ModeInput, PreparedReportInput, RSSResult
from trendradar.notification import NotificationDispatcher
from trendradar.storage.base import RSSData, RSSItem


NOW = datetime(2026, 10, 2, 10, 0, 0)
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
    """No constructor, real storage, SMTP, model or configuration reads."""

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
        self.storage = Mock()
        self.storage.backend_name = "synthetic"
        self.storage.save_news_data.side_effect = self.event("hotlist.save", True)
        self.storage.save_txt_snapshot.side_effect = self.event("hotlist.snapshot", None)
        self.storage.save_rss_data.side_effect = self.event("rss.save", True)
        self.storage.get_rss_data.side_effect = self.event("rss.history", self.rss_today)
        self.storage.get_latest_rss_data.side_effect = self.event("rss.latest", self.rss_latest)
        self.storage.detect_new_rss_items.side_effect = self.event("rss.new", self.rss_new)
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
        self.dispatcher.send_report.side_effect = self.event(
            "email.send", SimpleNamespace(configured=True, sent=True, partially_delivered=False),
        )
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
        self.ctx.detect_new_titles.side_effect = self.event("input.new", self.new_titles)
        self.ctx.read_today_titles.side_effect = self.event("input.history", (self.history, self.names, self.title_info))
        self.ctx.load_frequency_words.side_effect = self.event("input.words", ([], [], ["BLOCK"]))
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


class DailyFlowCharacterizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        old_cwd = Path.cwd()
        os.chdir(self.temp.name)
        self.addCleanup(os.chdir, old_cwd)

    def test_real_run_keeps_order_and_mode_specific_candidates(self):
        for mode in ("incremental", "current", "daily"):
            with self.subTest(mode=mode):
                fixture = DailyFixture(mode)
                with fixture.boundaries():
                    fixture.analyzer.run()
                expected = [
                    "schedule.resolve", "hotlist.fetch", "hotlist.save", "hotlist.snapshot",
                    "rss.fetch", "rss.save", "input.words",
                    "rss.new" if mode == "incremental" else "rss.latest" if mode == "current" else "rss.history",
                    "rss.new", "input.new", "input.words",
                ]
                if mode != "incremental":
                    expected.extend(["input.history", "input.new", "input.words"])
                expected.extend(["input.count", "ai.analyze", "record.analyze", "html.generate", "email.send", "record.push", "cleanup"])
                self.assertEqual(fixture.events, expected)
                self.assertEqual(fixture.manual_force.call_count, 2)
                self.assertEqual(fixture.freshness_check.call_count, {"incremental": 2, "current": 3, "daily": 4}[mode])
                self.assertTrue(all(call.args[1:] == (3, "Asia/Shanghai") for call in fixture.freshness_check.call_args_list))
                fixture.days_old.assert_not_called()
                self.assertEqual(fixture.analyzer.report_mode, mode)
                self.assertEqual(fixture.analyzer.frequency_file, "synthetic-words.txt")
                self.assertEqual(fixture.ctx.load_frequency_words.call_count, 2 if mode == "incremental" else 3)
                self.assertTrue(all(call.args == ("synthetic-words.txt",) for call in fixture.ctx.load_frequency_words.call_args_list))
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
                self.assertEqual(standalone["platforms"][0]["items"][0]["title"], "BLOCK hot")
                self.assertEqual(standalone["platforms"][0]["items"][1]["title"], "HOT now")
                html = fixture.ctx.generate_html.call_args
                self.assertIs(html.args[0], ai_args["stats"])
                self.assertIs(html.kwargs["rss_items"], ai_args["rss_stats"])
                self.assertIs(html.kwargs["ai_analysis"], fixture.ai_result)
                self.assertIs(html.kwargs["standalone_data"], standalone)
                self.assertEqual(html.kwargs["failed_ids"], ["failed"])
                self.assertEqual(html.kwargs["frequency_file"], "synthetic-words.txt")
                self.assertEqual(fixture.gate.record_execution.call_args_list[0].args, ("morning", "analyze", DATE))
                self.assertEqual(fixture.gate.record_execution.call_args_list[1].args, ("morning", "push", DATE))

    def test_report_keyword_failure_stops_before_history_and_never_delivers(self):
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
                    fixture.ctx.read_today_titles.assert_not_called()
                    fixture.ctx.count_frequency.assert_not_called()
                    fixture.ai.analyze.assert_not_called()
                    fixture.ctx.generate_html.assert_not_called()
                    fixture.dispatcher.send_report.assert_not_called()
                    fixture.gate.record_execution.assert_not_called()
                    fixture.ctx.cleanup.assert_called_once_with()

    def test_collect_false_and_disabled_crawler_still_cleanup(self):
        for crawler_enabled in (False, True):
            fixture = DailyFixture()
            fixture.config["ENABLE_CRAWLER"] = crawler_enabled
            fixture.schedule.collect = False
            with fixture.boundaries():
                fixture.analyzer.run()
            self.assertEqual(fixture.events, ["schedule.resolve", "cleanup"] if crawler_enabled else ["cleanup"])

    def test_force_run_does_not_override_collect_false(self):
        fixture = DailyFixture()
        fixture.schedule.collect = False
        with fixture.boundaries(force=True):
            fixture.analyzer.run()
        self.assertEqual(fixture.events, ["schedule.resolve", "cleanup"])
        fixture.manual_force.assert_not_called()

    def test_cleanup_runs_after_hotlist_html_or_delivery_error(self):
        for boundary in ("crawl", "html", "email"):
            with self.subTest(boundary=boundary):
                fixture = DailyFixture()
                error = RuntimeError(f"synthetic {boundary} failure")
                if boundary == "crawl":
                    fixture.analyzer.data_fetcher.crawl_websites.side_effect = error
                elif boundary == "html":
                    fixture.ctx.generate_html.side_effect = error
                else:
                    fixture.dispatcher.send_report.side_effect = error
                with fixture.boundaries(), patch("traceback.print_exc"):
                    with self.assertRaisesRegex(RuntimeError, f"synthetic {boundary} failure"):
                        fixture.analyzer.run()
                self.assertEqual(fixture.events[-1], "cleanup")
                fixture.ctx.cleanup.assert_called_once_with()
                if boundary == "crawl":
                    fixture.fetcher.fetch_all.assert_not_called()
                if boundary != "email":
                    fixture.dispatcher.send_report.assert_not_called()

    def test_ai_error_is_contained_and_does_not_record_analysis(self):
        fixture = DailyFixture()
        fixture.ai.analyze.side_effect = ValueError("synthetic AI failure")
        with fixture.boundaries(), patch("traceback.print_exc"):
            fixture.analyzer.run()
        self.assertFalse(fixture.ctx.generate_html.call_args.kwargs["ai_analysis"].success)
        self.assertEqual(fixture.events[-3:], ["email.send", "record.push", "cleanup"])
        fixture.gate.record_execution.assert_called_once_with("morning", "push", DATE)

    def test_html_disabled_does_not_suppress_ai_or_dispatch_attempt(self):
        fixture = DailyFixture()
        fixture.config["STORAGE"]["FORMATS"]["HTML"] = False
        with fixture.boundaries():
            fixture.analyzer.run()
        fixture.ai.analyze.assert_called_once()
        fixture.ctx.generate_html.assert_not_called()
        self.assertIn("email.send", fixture.events)
        mail_call = fixture.dispatcher.send_report.call_args
        self.assertIsNone(mail_call.kwargs["html_file_path"])

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
        self.assertEqual(fixture.events[-5:], ["record.analyze", "html.generate", "email.send", "record.push", "cleanup"])
        self.assertEqual(stat_titles(fixture.ctx.generate_html.call_args.args[0]), ["HOT now"])
        self.assertEqual(stat_titles(fixture.ctx.generate_html.call_args.kwargs["rss_items"]), ["RSS now"])
        self.assertEqual(fixture.manual_force.call_count, 2)
        fixture.freshness_check.assert_not_called()
        fixture.days_old.assert_not_called()


class DailyInputCharacterizationTests(unittest.TestCase):
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

    def test_mode_history_fallback_and_current_failure_are_not_unified(self):
        for mode in ("incremental", "current", "daily"):
            fixture = DailyFixture(mode)
            fixture.analyzer.report_mode = mode
            fixture.ctx.read_today_titles.side_effect = lambda *_args, **_kwargs: ({}, {}, {})
            with redirect_stdout(io.StringIO()):
                if mode == "current":
                    with self.assertRaisesRegex(RuntimeError, "数据一致性检查失败"):
                        fixture.analyzer._select_mode_data(fixture.current, fixture.names, fixture.new_titles, TIME)
                else:
                    chosen = fixture.analyzer._select_mode_data(fixture.current, fixture.names, fixture.new_titles, TIME)
                    self.assertEqual(chosen, (fixture.current, fixture.names,
                                             fixture.analyzer._prepare_current_title_info(fixture.current, TIME), fixture.new_titles))
            if mode == "incremental":
                fixture.ctx.read_today_titles.assert_not_called()
            else:
                fixture.ctx.read_today_titles.assert_called_once_with(["p"], quiet=False)

    def test_history_returns_only_consumed_fields_but_keeps_keyword_validation(self):
        fixture = DailyFixture()
        with redirect_stdout(io.StringIO()):
            result = fixture.analyzer._load_analysis_data(quiet=True)
        self.assertIsInstance(result, ModeInput)
        self.assertEqual(result._fields, ("results", "id_to_name", "title_info", "new_titles"))
        self.assertIs(result.results, fixture.history)
        self.assertIs(result.id_to_name, fixture.names)
        self.assertIs(result.title_info, fixture.title_info)
        self.assertIs(result.new_titles, fixture.new_titles)
        fixture.ctx.read_today_titles.assert_called_once_with(["p"], quiet=True)
        fixture.ctx.detect_new_titles.assert_called_once_with(["p"], quiet=True)
        fixture.ctx.load_frequency_words.assert_called_once_with(None)

    def test_history_keyword_validation_preserves_failure_and_shape_checks(self):
        for mode in ("incremental", "current", "daily"):
            for failure in (FileNotFoundError("synthetic missing words"), ValueError("synthetic invalid words"), ([], [])):
                with self.subTest(mode=mode, failure=repr(failure)):
                    fixture = DailyFixture(mode)
                    fixture.analyzer.report_mode = mode
                    if isinstance(failure, Exception):
                        fixture.ctx.load_frequency_words.side_effect = failure
                    else:
                        fixture.ctx.load_frequency_words.side_effect = lambda *_args: failure
                    with redirect_stdout(io.StringIO()):
                        if mode == "current":
                            with self.assertRaisesRegex(RuntimeError, "数据一致性检查失败"):
                                fixture.analyzer._select_mode_data(fixture.current, fixture.names, fixture.new_titles, TIME)
                        else:
                            selected = fixture.analyzer._select_mode_data(fixture.current, fixture.names, fixture.new_titles, TIME)
                            self.assertIs(selected.results, fixture.current)
                            self.assertIs(selected.new_titles, fixture.new_titles)
                    self.assertEqual(fixture.ctx.load_frequency_words.call_count, int(mode != "incremental"))

    def test_history_detection_error_keeps_mode_specific_failure_policy(self):
        for mode in ("incremental", "current", "daily"):
            fixture = DailyFixture(mode)
            fixture.analyzer.report_mode = mode
            fixture.ctx.detect_new_titles.side_effect = OSError("synthetic detection failure")
            with redirect_stdout(io.StringIO()):
                if mode == "current":
                    with self.assertRaisesRegex(RuntimeError, "数据一致性检查失败"):
                        fixture.analyzer._select_mode_data(fixture.current, fixture.names, fixture.new_titles, TIME)
                else:
                    result = fixture.analyzer._select_mode_data(fixture.current, fixture.names, fixture.new_titles, TIME)
                    self.assertIs(result.results, fixture.current)
                    self.assertIs(result.new_titles, fixture.new_titles)
            self.assertEqual(fixture.ctx.read_today_titles.call_count, int(mode != "incremental"))
            self.assertEqual(fixture.ctx.detect_new_titles.call_count, int(mode != "incremental"))
            fixture.ctx.load_frequency_words.assert_not_called()

    def test_load_failure_stays_contained_and_keeps_quiet_binding(self):
        fixture = DailyFixture()
        fixture.ctx.read_today_titles.side_effect = OSError("synthetic read failure")
        with redirect_stdout(io.StringIO()):
            self.assertIsNone(fixture.analyzer._load_analysis_data(quiet=True))
        fixture.ctx.read_today_titles.assert_called_once_with(["p"], quiet=True)
        fixture.ctx.detect_new_titles.assert_not_called()
        fixture.ctx.load_frequency_words.assert_not_called()

    def test_load_keeps_context_access_inside_its_error_boundary(self):
        analyzer = DailyRunner.__new__(DailyRunner)
        analyzer.ctx = SimpleNamespace()
        with redirect_stdout(io.StringIO()):
            self.assertIsNone(analyzer._load_analysis_data())


class DailyRSSCharacterizationTests(unittest.TestCase):
    def test_disabled_rss_requires_no_feed_or_storage_initialization(self):
        analyzer = DailyRunner.__new__(DailyRunner)
        analyzer.ctx = SimpleNamespace(rss_enabled=False)
        self.assertEqual(analyzer._crawl_rss_data(), RSSResult())

    def test_disabled_display_keeps_raw_rss_without_keyword_statistics(self):
        fixture = DailyFixture()
        fixture.analyzer.report_mode = "current"
        fixture.config["DISPLAY"]["REGIONS"]["RSS"] = False
        with fixture.boundaries(), patch("trendradar.core.analyzer.count_rss_frequency") as count:
            result = fixture.analyzer._process_rss_data_by_mode(fixture.rss_latest)
        self.assertIsNone(result.stats)
        self.assertIsNone(result.new_stats)
        self.assertEqual([item["title"] for item in result.raw_items], ["RSS now", "BLOCK rss"])
        fixture.storage.detect_new_rss_items.assert_called_once_with(fixture.rss_latest)
        count.assert_not_called()
        fixture.ctx.load_frequency_words.assert_called_once_with(None)

    def test_missing_keywords_keeps_global_fallback_empty_as_before(self):
        fixture = DailyFixture()
        fixture.analyzer.report_mode = "current"
        fixture.ctx.load_frequency_words.side_effect = FileNotFoundError("synthetic")
        with fixture.boundaries():
            result = fixture.analyzer._process_rss_data_by_mode(fixture.rss_latest)
        self.assertEqual(set(stat_titles(result.stats)), {"RSS now", "BLOCK rss"})
        self.assertEqual(stat_titles(result.new_stats), ["RSS now"])
        self.assertEqual(len(result.raw_items), 2)
        flags = {item["title"]: item["is_new"] for stat in result.stats for item in stat["titles"]}
        self.assertEqual(flags, {"RSS now": True, "BLOCK rss": False})

    def test_incremental_no_new_keeps_none_tuple_and_two_detection_reads(self):
        fixture = DailyFixture()
        fixture.analyzer.report_mode = "incremental"
        fixture.storage.detect_new_rss_items.side_effect = lambda _data: {}
        with fixture.boundaries():
            result = fixture.analyzer._process_rss_data_by_mode(fixture.rss_latest)
        self.assertEqual(result, RSSResult())
        self.assertEqual(fixture.storage.detect_new_rss_items.call_count, 2)
        fixture.storage.get_latest_rss_data.assert_not_called()
        fixture.storage.get_rss_data.assert_not_called()

    def test_no_history_keeps_consumed_views_empty_for_both_display_settings(self):
        for display in (True, False):
            fixture = DailyFixture()
            fixture.analyzer.report_mode = "daily"
            fixture.config["DISPLAY"]["REGIONS"]["RSS"] = display
            fixture.storage.get_rss_data.side_effect = lambda _date: None
            with fixture.boundaries(), patch("trendradar.core.analyzer.count_rss_frequency") as count:
                result = fixture.analyzer._process_rss_data_by_mode(fixture.rss_latest)
            self.assertEqual(result, RSSResult())
            fixture.storage.detect_new_rss_items.assert_called_once_with(fixture.rss_latest)
            count.assert_not_called()

    def test_no_keyword_match_keeps_raw_for_standalone(self):
        fixture = DailyFixture()
        fixture.analyzer.report_mode = "current"
        fixture.ctx.load_frequency_words.side_effect = lambda _file: ([{"required": [], "normal": ["not-present"], "group_key": "none"}], [], [])
        with fixture.boundaries():
            result = fixture.analyzer._process_rss_data_by_mode(fixture.rss_latest)
        self.assertIsNone(result.stats)
        self.assertIsNone(result.new_stats)
        self.assertEqual([item["title"] for item in result.raw_items], ["RSS now", "BLOCK rss"])
        fixture.storage.detect_new_rss_items.assert_called_once_with(fixture.rss_latest)

    def test_rss_save_and_processing_errors_are_contained_without_retries(self):
        for failure in (False, OSError("synthetic save failure"), ImportError("synthetic missing parser")):
            fixture = DailyFixture()
            if isinstance(failure, Exception):
                fixture.storage.save_rss_data.side_effect = failure
            else:
                fixture.storage.save_rss_data.side_effect = lambda _data: failure
            with fixture.boundaries():
                self.assertEqual(fixture.analyzer._crawl_rss_data(), RSSResult())
            fixture.fetcher.fetch_all.assert_called_once_with()
            fixture.storage.get_rss_data.assert_not_called()
            fixture.storage.get_latest_rss_data.assert_not_called()
        fixture = DailyFixture()
        fixture.storage.detect_new_rss_items.side_effect = RuntimeError("synthetic preparation failure")
        with fixture.boundaries():
            self.assertEqual(fixture.analyzer._crawl_rss_data(), RSSResult())

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
                self.assertEqual(fixture.analyzer._crawl_rss_data(), RSSResult())
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


class DailyBoundaryStructureTests(unittest.TestCase):
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
        rss_result = RSSResult([], [], [{"title": "raw RSS"}])
        with fixture.boundaries(), patch.object(fixture.analyzer, "_crawl_data", return_value=crawl), \
                patch.object(fixture.analyzer, "_crawl_rss_data", return_value=rss_result), \
                patch.object(fixture.analyzer, "execute_report") as execute:
            fixture.analyzer.run()
        self.assertIs(execute.call_args.args[0], crawl)
        self.assertIs(execute.call_args.args[1], rss_result)

        hotlist = ModeInput(fixture.history, fixture.names, fixture.title_info, fixture.new_titles)
        fixture.ctx.load_frequency_words.reset_mock()
        with patch.object(fixture.analyzer, "_select_mode_data", return_value=hotlist):
            prepared = fixture.analyzer.prepare_report(crawl, rss_result)
        self.assertIs(prepared.hotlist, hotlist)
        self.assertIs(prepared.rss, rss_result)
        self.assertIsInstance(prepared.keywords, KeywordRules)
        fixture.ctx.load_frequency_words.assert_called_once_with("synthetic-words.txt")

        with fixture.boundaries(), patch.object(fixture.analyzer, "_process_rss_data_by_mode", return_value=rss_result) as prepare:
            result = fixture.analyzer._crawl_rss_data()
        self.assertIs(result, rss_result)
        prepare.assert_called_once_with(fixture.rss_latest)
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
