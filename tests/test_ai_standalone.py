# coding=utf-8
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

from trendradar.ai.analyzer import AIAnalysisResult, AIAnalyzer
from trendradar.core.loader import _load_ai_analysis_config


class StandaloneConfigTests(unittest.TestCase):
    def test_include_standalone_is_loaded(self):
        config = _load_ai_analysis_config(
            {"ai_analysis": {"include_standalone": True}}
        )
        self.assertTrue(config["INCLUDE_STANDALONE"])


class StandalonePromptTests(unittest.TestCase):
    def _make_analyzer(self):
        analyzer = object.__new__(AIAnalyzer)
        analyzer.include_rank_timeline = True
        analyzer.include_standalone = True
        analyzer.language = "Chinese"
        return analyzer

    @staticmethod
    def _standalone_data():
        return {
            "platforms": [
                {
                    "id": "weibo",
                    "name": "微博",
                    "items": [
                        {
                            "title": "独立热点标题",
                            "ranks": [0, 8, 3],
                            "first_time": "09-00",
                            "last_time": "10-00",
                            "count": 2,
                            "rank_timeline": [
                                {"time": "09-00", "rank": 8},
                                {"time": "10-00", "rank": 3},
                            ],
                        }
                    ],
                }
            ],
            "rss_feeds": [
                {
                    "id": "example-feed",
                    "name": "示例信源",
                    "items": [
                        {
                            "title": "无排名标题",
                            "published_at": "2026-08-29T09:30:00+08:00",
                        }
                    ],
                }
            ],
        }

    def test_standalone_sources_share_one_untyped_format(self):
        analyzer = self._make_analyzer()
        content = analyzer._prepare_standalone_content(self._standalone_data())

        self.assertIn("### 微博", content)
        self.assertIn("### 示例信源", content)
        self.assertIn("排名:3-8", content)
        self.assertIn("轨迹:8(09:00)→3(10:00)", content)
        self.assertIn("- 无排名标题 | 时间:2026-08-29T09:30:00+08:00", content)
        self.assertNotIn("RSS", content)
        self.assertNotIn("热榜", content)
        self.assertNotIn("排名:0", content)

    def test_analyze_replaces_system_and_user_variables(self):
        analyzer = self._make_analyzer()
        analyzer.ai_config = {"MODEL": "test/model", "TIMEOUT": 30}
        analyzer.analysis_config = {}
        analyzer.client = SimpleNamespace(api_key="secret", timeout=30)
        analyzer.debug = False
        analyzer.get_time_func = lambda: datetime(2026, 8, 29, 12, 0, 0)
        analyzer.system_prompt = "模式={report_mode}；范围={platforms}"
        analyzer.user_prompt_template = (
            "数量={news_count}\n{news_content}\n<standalone_data>\n"
            "{standalone_content}\n</standalone_data>\n语言={language}"
        )
        analyzer._prepare_news_content = Mock(
            return_value=("### 主线\n- [来源] 标题", 1, 1)
        )
        analyzer._generate_and_parse = Mock(
            return_value=AIAnalysisResult(success=True)
        )

        analyzer.analyze(
            stats=[{"word": "", "titles": [{"title": "标题"}]}],
            report_mode="current",
            report_type="当前榜单",
            platforms=["来源甲", "来源乙"],
            keywords=[],
            standalone_data=self._standalone_data(),
        )

        user_prompt, system_prompt = analyzer._generate_and_parse.call_args.args
        self.assertEqual(system_prompt, "模式=current；范围=来源甲, 来源乙")
        self.assertIn("数量=1", user_prompt)
        self.assertIn("### 微博", user_prompt)
        self.assertIn("### 示例信源", user_prompt)
        self.assertIn("语言=Chinese", user_prompt)
        self.assertNotIn("{standalone_content}", user_prompt)

    def test_optional_standalone_section_may_be_omitted(self):
        analyzer = self._make_analyzer()
        analyzer.section_specs = analyzer._parse_section_specs(
            "# AI_SECTION: 主模块|required|prose\n"
            "# AI_SECTION: 独立源速览|optional|prose"
        )
        result = analyzer._parse_response("## 主模块\n正文。")
        self.assertTrue(result.success, result.error)
        self.assertEqual([section.title for section in result.sections], ["主模块"])


if __name__ == "__main__":
    unittest.main()
