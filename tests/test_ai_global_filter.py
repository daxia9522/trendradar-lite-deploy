# coding=utf-8
"""AI 主池复用关键词筛选结果的回归测试。"""

import unittest
from unittest.mock import Mock

from trendradar.core.analyzer import count_word_frequency
from trendradar.daily_flow.models import KeywordRules, ModeInput, PreparedReportInput, RSSResult
from trendradar.daily_flow.runner import DailyRunner


class _FakeContext:
    display_mode = "keyword"

    def __init__(self, keyword_stats):
        self.keyword_stats = keyword_stats
        self.config = {
            "AI_ANALYSIS": {"ENABLED": True},
            "STORAGE": {"FORMATS": {"HTML": False}},
        }

    def count_frequency(self, *args, **kwargs):
        return self.keyword_stats, 2


class AiKeywordPoolTests(unittest.TestCase):
    def test_ai_main_pool_reuses_keyword_hotlist_and_rss_stats(self):
        hotlist_stats = [{
            "word": "AI",
            "count": 1,
            "titles": [{"title": "AI命中标题", "source_name": "测试源"}],
        }]
        rss_stats = [{
            "word": "AI",
            "count": 1,
            "titles": [{"title": "AI命中RSS", "source_name": "RSS源"}],
        }]
        analyzer = DailyRunner.__new__(DailyRunner)
        analyzer.ctx = _FakeContext(hotlist_stats)
        analyzer.ctx.count_frequency = Mock(wraps=analyzer.ctx.count_frequency)
        analyzer._get_mode_strategy = Mock(return_value={"report_type": "当前榜单"})
        analyzer._run_ai_analysis = Mock(return_value=None)

        prepared = PreparedReportInput(
            mode="current",
            hotlist=ModeInput(
                results={"source": {"AI命中标题": {}, "无关键词标题": {}}},
                id_to_name={"source": "测试源"}, title_info={}, new_titles={},
            ),
            keywords=KeywordRules(word_groups=[], filter_words=[], global_filters=["震惊"]),
            rss=RSSResult(stats=rss_stats, raw_items=[{"title": "未命中关键词的原始RSS"}]),
            standalone={"platforms": []}, quiet=True,
        )
        schedule = object()
        artifacts = analyzer.analyze_report(prepared, schedule)

        analyzer.ctx.count_frequency.assert_called_once_with(
            prepared.hotlist.results, [], [], prepared.hotlist.id_to_name, {}, {},
            mode="current", global_filters=["震惊"], quiet=True,
        )
        analyzer._run_ai_analysis.assert_called_once_with(
            hotlist_stats, rss_stats, "current", "当前榜单", prepared.hotlist.id_to_name,
            schedule=schedule, standalone_data=prepared.standalone,
        )
        self.assertIs(artifacts.stats, hotlist_stats)
        self.assertIsNone(artifacts.html_file)
        call = analyzer._run_ai_analysis.call_args
        self.assertIs(call.args[0], hotlist_stats)
        self.assertIs(call.args[1], rss_stats)
        self.assertNotIn("无关键词标题", str(call.args[:2]))
        self.assertNotIn("未命中关键词的原始RSS", str(call.args[:2]))

    def test_global_filter_still_applies_before_ai_pool(self):
        stats, _ = count_word_frequency(
            results={
                "source": {
                    "震惊标题不应进入AI": {"ranks": [1]},
                    "正常热点标题": {"ranks": [2]},
                }
            },
            word_groups=[],
            filter_words=[],
            id_to_name={"source": "测试源"},
            mode="daily",
            global_filters=["震惊"],
            is_first_crawl_func=lambda: True,
            quiet=True,
        )
        titles = [
            item["title"]
            for stat in stats
            for item in stat.get("titles", [])
        ]
        self.assertEqual(titles, ["正常热点标题"])


if __name__ == "__main__":
    unittest.main()
