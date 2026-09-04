# coding=utf-8
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from trendradar.ai.analyzer import AIAnalyzer
from trendradar.ai.formatter import render_ai_analysis_html_rich


LEAD_SENTENCE = "**AI 资本投入趋于审慎，资金与产业关注点正在重新分布。**"

VALID_CURRENT_RESPONSE = """## 核心热点
**AI 资本投入趋于审慎，资金与产业关注点正在重新分布。**

1. **算力投入与回报预期重估**：多家厂商上调资本开支，但自由现金流转负。
2. **贸易与供应链压力**：关税措施升级传导至上游材料。

## 舆论风向
1. **平台温差**：AI 内容在娱乐消费与严肃信息场景中的接受度出现差异。
2. **认知断层**：机构与大众对同一份数据得出方向相反的结论。

## 异动与弱信号
1. **资金面异动**：资本调仓与产业投资出现节奏错位。
2. **政策弱信号**：监管表态较此前明显克制。

## 综合研判
1. **交叉验证**：不同来源对同一产业变化提供了互补信息。
2. **趋势判断**：中短期内产业投入节奏可能进一步分化。

## 行动建议
1. **投资者**：关注后续财报中的资本开支与现金流指标。
2. **品牌方**：留意监管动作与市场反馈的时间差。
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
            "核心热点",
            "舆论风向",
            "异动与弱信号",
            "综合研判",
            "行动建议",
        ]
        self.assertEqual([section.title for section in result.sections], expected)
        html = render_ai_analysis_html_rich(result)
        positions = [html.index(title) for title in expected]
        self.assertEqual(positions, sorted(positions))

    def test_required_sections_cannot_be_omitted(self):
        result = self.analyzer._parse_response("## 核心热点\n一句总领。")
        self.assertFalse(result.success)
        self.assertIn("缺少标题", result.error)

    def test_invalid_outputs_are_rejected(self):
        cases = (
            "## 新标题\n内容。",
            VALID_CURRENT_RESPONSE + "\n## 核心热点\n重复内容。",
        )
        for response in cases:
            with self.subTest(response=response):
                result = self.analyzer._parse_response(response)
                self.assertFalse(result.success)
                self.assertTrue(result.error)

    def test_internal_event_index_is_rejected_in_prose(self):
        """内部事件编号不得泄漏到用户可见输出。

        回归场景：输入侧曾用 "### 事件 N：标题" 组织事件簇，模型把 N
        当成引用锚点写回「异动信号」正文（prose 板块，不走 _validate_events）。
        """
        leaked = VALID_CURRENT_RESPONSE.replace(
            "1. **资金面异动**：资本调仓与产业投资出现节奏错位。",
            "1. **资金面异动**：事件1持续位居榜首，事件6、23共同指向预期角力。",
        )
        self.assertIn("事件1", leaked)
        result = self.analyzer._parse_response(leaked)
        self.assertFalse(result.success)
        self.assertIn("内部事件编号", result.error)

    def test_normal_event_wording_is_not_falsely_rejected(self):
        """不能误伤正常表达：仅「事件+数字」才是编号泄漏。"""
        ok = VALID_CURRENT_RESPONSE.replace(
            "1. **资金面异动**：资本调仓与产业投资出现节奏错位。",
            "1. **资金面异动**：三起独立事件共同指向同一主线，2 起已获跨源印证。",
        )
        result = self.analyzer._parse_response(ok)
        self.assertTrue(result.success, result.error)

    def test_event_subtitle_followed_by_numbered_list_is_not_rejected(self):
        """三级标题“支撑事件”后的编号列表不是内部事件编号。"""
        response = VALID_CURRENT_RESPONSE.replace(
            "**AI 资本投入趋于审慎，资金与产业关注点正在重新分布。**\n\n",
            "### 议程总览\n"
            "AI 资本投入趋于审慎，资金与产业关注点正在重新分布。\n\n"
            "### 支撑事件\n",
        )
        self.assertIn("### 支撑事件\n1.", response)
        result = self.analyzer._parse_response(response)
        self.assertTrue(result.success, result.error)

    def test_heading_variants_reorder_and_preserve_subtitles(self):
        response = """### 综合研判：
不同来源形成交叉验证。

# 核心热点
**全局呈现政策与产业双线联动。**
### 1. 政策面变动
- **第一条主线。** 具体分析。
政策面的细节。

## 舆论风向
存在传播温差。

## 异动与弱信号：
出现异常轨迹。

### 行动建议
继续观察后续变化。
"""
        result = self.analyzer._parse_response(response)
        self.assertTrue(result.success, result.error)
        self.assertEqual(
            [section.title for section in result.sections],
            ["核心热点", "舆论风向", "异动与弱信号", "综合研判", "行动建议"],
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
        self.assertIn("必须使用以下必选 Markdown 二级标题", user_prompt)
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
            "1. **资金面异动**：资本调仓与产业投资出现节奏错位。\n"
            "2. **政策弱信号**：监管表态较此前明显克制。",
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


class CoreSituationFormatTests(unittest.TestCase):
    """lead_points 校验器回归：核心热点生产契约已改 prose，这里固定旧契约覆盖校验逻辑。"""

    LEAD_POINTS_CONTRACT = """# AI_SECTION: 核心热点|required|lead_points
# AI_SECTION: 舆论风向|required|prose
# AI_SECTION: 异动与弱信号|required|prose
# AI_SECTION: 综合研判|required|prose
# AI_SECTION: 行动建议|required|prose
"""

    def setUp(self):
        self.analyzer = object.__new__(AIAnalyzer)
        self.analyzer.section_specs = self.analyzer._parse_section_specs(
            self.LEAD_POINTS_CONTRACT
        )

    def test_prose_core_section_accepts_plain_paragraph(self):
        """生产契约改为 prose 后，普通段落不得因缺少加粗总括句而重试。"""
        analyzer = object.__new__(AIAnalyzer)
        _, _, analyzer.section_specs = analyzer._load_prompt_template(
            "ai_analysis_prompt.txt"
        )
        prose_core = VALID_CURRENT_RESPONSE.replace(
            LEAD_SENTENCE + "\n\n"
            "1. **算力投入与回报预期重估**：多家厂商上调资本开支，但自由现金流转负。\n"
            "2. **贸易与供应链压力**：关税措施升级传导至上游材料。",
            "资金与产业关注点正在重新分布，算力投入趋于审慎，贸易摩擦继续向供应链上游传导。",
        )
        result = analyzer._parse_response(prose_core)
        self.assertTrue(result.success, result.error)

    def _with_lead(self, lead):
        return VALID_CURRENT_RESPONSE.replace(LEAD_SENTENCE, lead)

    def test_bold_lead_is_accepted(self):
        """提示词要求首句以加粗短句给出全局总括，校验器必须放行。

        回归场景：总领句式判定原先直接在原文上做，"**…。**" 因末尾是星号
        而非句号被判不合格。提示词要求加粗、校验器又禁止加粗，会导致每次
        生成都校验失败并白白重试一次。
        """
        for lead in (
            "**今日动态交织于三大主线：大国角力、供应链摩擦与 AI 商业化竞争。**",
            "__双下划线也是加粗语法。__",
            "**部分加粗**的总领也合法。",
            "**当前周期内，多线并行。**后半句不加粗也合法。",
        ):
            with self.subTest(lead=lead):
                result = self.analyzer._parse_response(self._with_lead(lead))
                self.assertTrue(result.success, result.error)

    def test_lead_must_be_standalone_and_bold(self):
        """lead_points 只锁两件事：首行独立加粗、其后至少 1 条分条。

        刻意不校验总括是否只有一句、是否以句号收尾——那属于文风偏好，
        为此触发重试会白白消耗一次 API 调用。
        """
        for lead in (
            "- **写成了列表项。**",
            "1. **写成了编号项。**",
            "### 写成了三级标题",
            "没有任何加粗的普通句子。",
        ):
            with self.subTest(lead=lead):
                result = self.analyzer._parse_response(self._with_lead(lead))
                self.assertFalse(result.success)

    def test_lead_tolerates_style_variance(self):
        """多句、无句号等文风差异不应判失败并触发重试。"""
        for lead in (
            "**第一句。第二句。**",
            "**未以句号结尾**",
        ):
            with self.subTest(lead=lead):
                result = self.analyzer._parse_response(self._with_lead(lead))
                self.assertTrue(result.success, result.error)

    def test_core_section_requires_split_mainlines(self):
        """核心态势不得把主线内容挤成单一大段。

        实测背景：旧契约下核心态势是 prose，模型把三大主线全塞进一段，
        可读性差。改为 events 后缺少三级标题应直接校验失败并触发重试。
        """
        crammed = """## 核心热点
**今日动态交织于三大主线。**
在地缘线，美俄互动出现罕见交集；在贸易线，美加贸易战持续升级；
在科技线，苹果发布首款量产 2nm 芯片。

## 舆论风向
存在传播温差。

## 异动与弱信号
出现异常轨迹。

## 综合研判
形成交叉验证。

## 行动建议
继续观察。
"""
        result = self.analyzer._parse_response(crammed)
        self.assertFalse(result.success)
        self.assertIn("分条拆解", result.error)

    def test_single_mainline_is_accepted(self):
        """单条主线也合法：核心态势不强制凑足 2 条。"""
        single = VALID_CURRENT_RESPONSE.replace(
            "1. **算力投入与回报预期重估**：多家厂商上调资本开支，但自由现金流转负。\n"
            "2. **贸易与供应链压力**：关税措施升级传导至上游材料。",
            "1. **算力投入与回报预期重估**：多家厂商上调资本开支，但自由现金流转负。",
        )
        result = self.analyzer._parse_response(single)
        self.assertTrue(result.success, result.error)

    def test_lead_and_numbered_points_render_correctly(self):
        """总括句独立成段，编号分条渲染为有序列表并保留加粗小标题。"""
        result = self.analyzer._parse_response(VALID_CURRENT_RESPONSE)
        self.assertTrue(result.success, result.error)
        html = render_ai_analysis_html_rich(result)
        self.assertIn(
            "<p><strong>AI 资本投入趋于审慎，资金与产业关注点正在重新分布。</strong></p>",
            html,
        )
        self.assertIn("<strong>算力投入与回报预期重估</strong>", html)
        self.assertIn("<ol", html)

    def test_heading_style_mainlines_also_accepted(self):
        """三级标题写法与编号写法都应放行，不为排版差异触发重试。"""
        heading_style = VALID_CURRENT_RESPONSE.replace(
            "1. **算力投入与回报预期重估**：多家厂商上调资本开支，但自由现金流转负。\n"
            "2. **贸易与供应链压力**：关税措施升级传导至上游材料。",
            "### 算力投入与回报预期重估\n多家厂商上调资本开支。\n\n"
            "### 贸易与供应链压力\n关税措施升级传导至上游材料。",
        )
        result = self.analyzer._parse_response(heading_style)
        self.assertTrue(result.success, result.error)

    def test_any_recognizable_ordinal_counts_as_a_point(self):
        """任何可辨识的标号都应计作一条分条。

        提示词只推荐「1. **小标题**：」，但模型会自发换成中文序号、括号
        序号、圆圈数字或纯加粗小标题。这些纯属排版差异，渲染层会归一化；
        若在此判失败，每次都会白白多花一次 API 调用重试。
        """
        numbered = (
            "1. **算力投入与回报预期重估**：多家厂商上调资本开支，但自由现金流转负。\n"
            "2. **贸易与供应链压力**：关税措施升级传导至上游材料。"
        )
        for label, style in (
            ("中文顶格序号", "一、**主线甲**：内容。\n二、**主线乙**：内容。"),
            ("全角括号序号", "（1）**主线甲**：内容。\n（2）**主线乙**：内容。"),
            ("半角括号序号", "(1) **主线甲**：内容。\n(2) **主线乙**：内容。"),
            ("单右括号序号", "1）**主线甲**：内容。\n2）**主线乙**：内容。"),
            ("圆圈数字", "① **主线甲**：内容。\n② **主线乙**：内容。"),
            ("拉丁字母序号", "A. **主线甲**：内容。\nB. **主线乙**：内容。"),
            ("无序号加粗小标题", "**主线甲**：内容。\n**主线乙**：内容。"),
            ("圆点符号", "• **主线甲**：内容。\n• **主线乙**：内容。"),
        ):
            with self.subTest(style=label):
                result = self.analyzer._parse_response(
                    VALID_CURRENT_RESPONSE.replace(numbered, style)
                )
                self.assertTrue(result.success, result.error)

    def test_lead_written_as_any_ordinal_is_rejected(self):
        """总括句写成任何形式的分条都应拦下，不限阿拉伯数字。"""
        for lead in (
            "一、**写成了中文序号。**",
            "（1）**写成了括号序号。**",
            "① **写成了圆圈数字。**",
            "1）**写成了单右括号。**",
            "• **写成了圆点列表。**",
        ):
            with self.subTest(lead=lead):
                result = self.analyzer._parse_response(self._with_lead(lead))
                self.assertFalse(result.success)

    def test_prompt_file_and_code_contract_stay_in_sync(self):
        """锁住提示词文件与代码契约一致，防止只改一边。"""
        prompt_path = (
            Path(__file__).resolve().parent.parent / "config" / "ai_analysis_prompt.txt"
        )
        text = prompt_path.read_text(encoding="utf-8")
        specs = AIAnalyzer._parse_section_specs(text)
        by_title = {spec.title: spec for spec in specs}
        self.assertEqual(by_title["核心热点"].format_type, "prose")
        # 提示词正文必须仍要求 MECE 归总，避免改成 prose 后丢失分析方法
        self.assertIn("MECE", text)
        for spec in specs:
            self.assertIn(spec.title, text)


if __name__ == "__main__":
    unittest.main()
