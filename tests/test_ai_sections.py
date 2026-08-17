# coding=utf-8
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from trendradar.ai.analyzer import AIAnalyzer
from trendradar.ai.formatter import render_ai_analysis_html_rich


VALID_CURRENT_RESPONSE = """## 核心态势
AI 资本投入趋于审慎，资金与产业关注点正在重新分布。

## 舆情分化
AI 内容在娱乐消费与严肃信息场景中的接受度出现温差。

## 异动信号
资本调仓、产业投资和政策讨论出现值得持续观察的联动。

## 综合研判
不同来源对同一产业变化提供了互补信息和交叉验证。

## 行动建议
投资者与品牌方应关注后续财报、监管动作和市场反馈。
"""


class AISectionContractTests(unittest.TestCase):
    def setUp(self):
        self.analyzer = object.__new__(AIAnalyzer)
        _, _, self.analyzer.section_specs = self.analyzer._load_prompt_template(
            "ai_analysis_prompt.txt"
        )

    def test_current_structure_parses_and_renders_in_order(self):
        result = self.analyzer._parse_response(VALID_CURRENT_RESPONSE)
        self.assertTrue(result.success, result.error)
        expected = [
            "核心态势",
            "舆情分化",
            "异动信号",
            "综合研判",
            "行动建议",
        ]
        self.assertEqual([section.title for section in result.sections], expected)
        html = render_ai_analysis_html_rich(result)
        positions = [html.index(title) for title in expected]
        self.assertEqual(positions, sorted(positions))

    def test_required_sections_cannot_be_omitted(self):
        result = self.analyzer._parse_response("## 核心态势\n一句总领。")
        self.assertFalse(result.success)
        self.assertIn("缺少标题", result.error)

    def test_invalid_outputs_are_rejected(self):
        cases = (
            "## 新标题\n内容。",
            VALID_CURRENT_RESPONSE + "\n## 核心态势\n重复内容。",
        )
        for response in cases:
            with self.subTest(response=response):
                result = self.analyzer._parse_response(response)
                self.assertFalse(result.success)
                self.assertTrue(result.error)

    def test_heading_variants_reorder_and_preserve_subtitles(self):
        response = """### 综合研判：
不同来源形成交叉验证。

# 核心态势
- **第一条主线。** 具体分析。
### 1. 政策面变动
政策面的细节。

## 舆情分化
存在传播温差。

## 异动信号：
出现异常轨迹。

### 行动建议
继续观察后续变化。
"""
        result = self.analyzer._parse_response(response)
        self.assertTrue(result.success, result.error)
        self.assertEqual(
            [section.title for section in result.sections],
            ["核心态势", "舆情分化", "异动信号", "综合研判", "行动建议"],
        )
        self.assertIn("### 1. 政策面变动", result.sections[0].content)
        html = render_ai_analysis_html_rich(result)
        self.assertIn("<li><strong>第一条主线。</strong> 具体分析。</li>", html)
        self.assertIn('<div class="ai-subtitle">1. 政策面变动</div>', html)

    def test_titles_can_change_without_python_field_mapping(self):
        contract = """# AI_SECTION: 今日核心态势|required|events
# AI_SECTION: 全天变化|optional|bullets
"""
        analyzer = object.__new__(AIAnalyzer)
        analyzer.section_specs = analyzer._parse_section_specs(contract)
        result = analyzer._parse_response(
            "## 今日核心态势\n一句总领。\n\n### 事件\n正文。\n\n## 全天变化\n- 变化一。"
        )
        self.assertTrue(result.success, result.error)
        self.assertEqual(
            [section.title for section in result.sections],
            ["今日核心态势", "全天变化"],
        )

    def test_contract_rejects_typos_and_duplicate_titles(self):
        bad_contracts = (
            "没有模块契约",
            "# AI_SECTION: 主模块|required",
            "# AI_SECTION: 主模块|requried|prose",
            "# AI_SECTION: 主模块|required|unknown",
            "# AI_SECTION: 主模块|required|prose\n# AI_SECTION: 主模块|optional|prose",
        )
        for contract in bad_contracts:
            with self.subTest(contract=contract):
                with self.assertRaises(ValueError):
                    AIAnalyzer._parse_section_specs(contract)

    def test_prompt_headings_are_generated_from_contract(self):
        prompt = """# AI_SECTION: 契约标题|required|prose
[system]
系统提示
[user]
正文要求
"""
        with patch.object(Path, "exists", return_value=True), patch.object(
            Path, "read_text", return_value=prompt
        ):
            _, user_prompt, specs = self.analyzer._load_prompt_template("unused.txt")
        self.assertEqual([spec.title for spec in specs], ["契约标题"])
        self.assertIn("必须使用以下 Markdown 二级标题", user_prompt)
        self.assertIn("## 契约标题", user_prompt)
        self.assertTrue(user_prompt.endswith("不要输出 JSON、代码块、分析过程或其他内容。"))

    def test_generation_retries_once_after_parse_failure(self):
        analyzer = object.__new__(AIAnalyzer)
        analyzer.section_specs = self.analyzer.section_specs
        analyzer.client = SimpleNamespace(last_finish_reason="", last_model="test-model")
        analyzer._call_ai = Mock(side_effect=["格式错误", VALID_CURRENT_RESPONSE])

        result = analyzer._generate_and_parse("测试提示词")

        self.assertTrue(result.success, result.error)
        self.assertEqual(result.model, "test-model")
        self.assertEqual(analyzer._call_ai.call_count, 2)

    def test_generation_stops_after_second_parse_failure(self):
        analyzer = object.__new__(AIAnalyzer)
        analyzer.section_specs = self.analyzer.section_specs
        analyzer.client = SimpleNamespace(last_finish_reason="", last_model="test-model")
        analyzer._call_ai = Mock(side_effect=["第一次错误", "第二次错误"])

        result = analyzer._generate_and_parse("测试提示词")

        self.assertFalse(result.success)
        self.assertIn("首次校验失败", result.error)
        self.assertIn("重试仍失败", result.error)
        self.assertEqual(analyzer._call_ai.call_count, 2)

    def test_generation_retries_once_after_truncation(self):
        analyzer = object.__new__(AIAnalyzer)
        analyzer.section_specs = self.analyzer.section_specs
        analyzer.client = SimpleNamespace(last_finish_reason="length", last_model="test-model")
        analyzer._call_ai = Mock(side_effect=["截断内容", VALID_CURRENT_RESPONSE])
        analyzer._last_ai_call_was_truncated = Mock(side_effect=[True, False])

        result = analyzer._generate_and_parse("测试提示词")

        self.assertTrue(result.success, result.error)
        self.assertEqual(analyzer._call_ai.call_count, 2)

    def test_generic_prose_contract_does_not_require_lead_module(self):
        analyzer = object.__new__(AIAnalyzer)
        analyzer.section_specs = analyzer._parse_section_specs(
            "# AI_SECTION: 摘要|required|prose"
        )
        result = analyzer._parse_response("## 摘要\n这是普通段落。")
        self.assertTrue(result.success, result.error)

    def test_ordered_points_preserve_explicit_start_across_paragraphs(self):
        response = VALID_CURRENT_RESPONSE.replace(
            "AI 资本投入趋于审慎，资金与产业关注点正在重新分布。",
            "1. **第一条主线。**\n\n第一条说明。\n\n5. **第五条主线。**\n\n第五条说明。",
        )
        result = self.analyzer._parse_response(response)
        self.assertTrue(result.success, result.error)
        html = render_ai_analysis_html_rich(result)
        self.assertIn("<ol><li><strong>第一条主线。</strong></li></ol>", html)
        self.assertIn(
            '<ol start="5"><li><strong>第五条主线。</strong></li></ol>',
            html,
        )


if __name__ == "__main__":
    unittest.main()
