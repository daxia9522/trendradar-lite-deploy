# coding=utf-8
"""AI 主池复用关键词筛选结果的回归测试。"""

import unittest
from unittest.mock import Mock

from trendradar.core.analyzer import count_word_frequency
from trendradar.daily import NewsAnalyzer


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
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.ctx = _FakeContext(hotlist_stats)
        analyzer._get_mode_strategy = Mock(return_value={"report_type": "当前榜单"})
        analyzer._run_ai_analysis = Mock(return_value=None)

        analyzer._run_analysis_pipeline(
            data_source={"source": {"AI命中标题": {}, "无关键词标题": {}}},
            mode="current",
            title_info={},
            new_titles={},
            word_groups=[],
            filter_words=[],
            id_to_name={"source": "测试源"},
            global_filters=["震惊"],
            rss_items=rss_stats,
            raw_rss_items=[{"title": "未命中关键词的原始RSS"}],
            standalone_data={"platforms": []},
            schedule=object(),
        )

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
