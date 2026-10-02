"""Focused contracts for the safe shared Markdown renderer."""
import html
from html.parser import HTMLParser
import unittest
from unittest.mock import patch

from trendradar.report.markdown import render_inline, render_markdown


class Fragment(HTMLParser):
    def __init__(self, content):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.stack = []
        self.errors = []
        self.text = []
        self.code_text = []
        self.tags_in_code = []
        self.feed(content)
        self.close()

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if "code" in self.stack:
            self.tags_in_code.append(tag)
        if tag != "br":
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(tag)
        else:
            self.stack.pop()

    def handle_data(self, data):
        self.text.append(data)
        if "code" in self.stack:
            self.code_text.append(data)


class InlineRendererTests(unittest.TestCase):
    def test_code_contents_are_literal_markdown_and_escaped_html(self):
        content = '**粗体** *斜体* <img src=x onerror="evil()"> &amp;'
        rendered = render_inline(f"`{content}`")
        self.assertEqual(rendered, f"<code>{html.escape(content)}</code>")
        parsed = Fragment(rendered)
        self.assertEqual(parsed.tags, [("code", {})])
        self.assertEqual("".join(parsed.code_text), content)
        self.assertEqual(parsed.tags_in_code, [])
        self.assertEqual(parsed.errors, [])

    def test_code_spans_do_not_prevent_surrounding_emphasis(self):
        self.assertEqual(
            render_inline("**前 `*代码*` 后** 与 *`**字面**`*"),
            "<strong>前 <code>*代码*</code> 后</strong> 与 <em><code>**字面**</code></em>",
        )

    def test_nested_and_combined_star_emphasis_is_balanced(self):
        cases = (
            ("***兼有***", "<strong><em>兼有</em></strong>"),
            ("**粗 *斜* 粗**", "<strong>粗 <em>斜</em> 粗</strong>"),
            ("*斜 **粗** 斜*", "<em>斜 <strong>粗</strong> 斜</em>"),
            ("**粗 *斜***", "<strong>粗 <em>斜</em></strong>"),
            ("*斜 **粗***", "<em>斜 <strong>粗</strong></em>"),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(render_inline(text), expected)
                parsed = Fragment(expected)
                self.assertEqual(parsed.errors, [])
                self.assertEqual(parsed.stack, [])

    def test_unmatched_or_whitespace_delimiters_remain_text(self):
        for text in ("*未闭合", "**未闭合", "尾部*", "甲 * 乙", "** 空白 **", "****", "`未闭合", "``未闭合`"):
            with self.subTest(text=text):
                self.assertEqual(render_inline(text), text)

    def test_matching_backtick_runs_allow_literal_backticks(self):
        self.assertEqual(render_inline("``a ` tick``"), "<code>a ` tick</code>")
        self.assertEqual(render_inline("```a `` tick```"), "<code>a `` tick</code>")
        self.assertEqual(render_inline("` 甲 `"), "<code> 甲 </code>")
        self.assertEqual(render_inline("`甲`、`乙`"), "<code>甲</code>、<code>乙</code>")

    def test_unsupported_links_images_and_autolinks_stay_text(self):
        cases = (
            "[点击](javascript:alert(1))",
            "![图片](data:text/html,<script>evil()</script>)",
            "<https://example.invalid/a?x=1&y=2>",
            '<a href="javascript:evil()">点击</a>',
        )
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(render_inline(text), html.escape(text))
                self.assertEqual(Fragment(render_inline(text)).tags, [])

    def test_raw_html_and_preescaped_entities_round_trip_as_text(self):
        text = '<script>evil()</script> &lt;script&gt; &amp; "引号" \'单引号\''
        rendered = render_inline(text)
        self.assertEqual(rendered, html.escape(text))
        self.assertEqual("".join(Fragment(rendered).text), text)
        self.assertNotIn("&amp;lt;script&gt;evil", rendered)

    def test_malformed_emphasis_cannot_produce_crossing_html_tags(self):
        for text in ("**甲 *乙** 丙*", "***甲**", "*甲 **乙* 丙**", "*甲****乙*", "**<script>*x***"):
            with self.subTest(text=text):
                parsed = Fragment(render_inline(text))
                self.assertEqual(parsed.errors, [])
                self.assertEqual(parsed.stack, [])
                self.assertTrue({tag for tag, _ in parsed.tags} <= {"em", "strong"})

    def test_code_does_not_consume_adjacent_plain_text_or_marker_tokens(self):
        text = "\x00CODE0\x00 `**literal**` **bold** \x00CODE1\x00"
        self.assertEqual(
            render_inline(text),
            "\x00CODE0\x00 <code>**literal**</code> <strong>bold</strong> \x00CODE1\x00",
        )


class BlockRendererTests(unittest.TestCase):
    def test_soft_breaks_blank_lines_and_outer_whitespace(self):
        self.assertEqual(
            render_markdown(" \r\n  第一行  \r\n\t第二行\t\r\n \r\n末段\n"),
            "<p>第一行<br>第二行</p><p>末段</p>",
        )

    def test_all_block_transitions_close_the_previous_block(self):
        text = "开头\n- 甲\n* 乙\n5. 丙\n6、丁\n> 引用\n> 续行\n## 标题\n正文\n下一行\n\n尾段"
        expected = (
            '<p>开头</p><ul><li>甲</li><li>乙</li></ul>'
            '<ol start="5"><li>丙</li><li>丁</li></ol>'
            '<blockquote>引用<br>续行</blockquote><h2>标题</h2>'
            '<p>正文<br>下一行</p><p>尾段</p>'
        )
        rendered = render_markdown(text)
        self.assertEqual(rendered, expected)
        parsed = Fragment(rendered)
        self.assertEqual(parsed.errors, [])
        self.assertEqual(parsed.stack, [])

    def test_every_block_uses_the_one_inline_renderer(self):
        cases = (
            ("plain", "p"), ("# title", "h1"), ("## title", "h2"),
            ("### title", "h3"), ("- item", "li"), ("5. item", "li"),
            ("> quote", "blockquote"),
        )
        for text, tag in cases:
            with self.subTest(text=text):
                with patch("trendradar.report.markdown.render_inline", return_value="INLINE") as inline:
                    rendered = render_markdown(text)
                    inline.assert_called_once()
                    self.assertIn(f"<{tag}>INLINE</{tag}>", rendered)

    def test_ordered_markers_preserve_start_with_or_without_spaces(self):
        for marker, start in (("5. ", "5"), ("5.", "5"), ("5、", "5"), ("5、 ", "5"), ("005. ", "5"), ("0、", "0")):
            with self.subTest(marker=marker):
                self.assertEqual(
                    render_markdown(marker + "条目"),
                    f'<ol start="{start}"><li>条目</li></ol>',
                )
        self.assertEqual(render_markdown("001. 条目"), "<ol><li>条目</li></ol>")

    def test_lists_restart_after_blank_line_or_different_block(self):
        self.assertEqual(
            render_markdown("5. 甲\n\n8、乙\n- 丙\n3. 丁\n说明\n7. 戊"),
            '<ol start="5"><li>甲</li></ol><ol start="8"><li>乙</li></ol>'
            '<ul><li>丙</li></ul><ol start="3"><li>丁</li></ol>'
            '<p>说明</p><ol start="7"><li>戊</li></ol>',
        )

    def test_later_numbers_in_the_same_list_use_normal_html_counting(self):
        self.assertEqual(
            render_markdown("5. 甲\n99. 乙\n1、丙"),
            '<ol start="5"><li>甲</li><li>乙</li><li>丙</li></ol>',
        )

    def test_long_numeric_start_is_safe_without_integer_conversion(self):
        number = "5" * 5000
        self.assertEqual(
            render_markdown(f"{number}. 条目"),
            f'<ol start="{number}"><li>条目</li></ol>',
        )

    def test_heading_levels_and_empty_markers_are_deliberately_limited(self):
        self.assertEqual(
            render_markdown("#无空格\n#### 四级不支持\n#\n-\n1.\n***"),
            "<p>#无空格<br>#### 四级不支持<br>#<br>-<br>1.<br>***</p>",
        )
        self.assertEqual(render_markdown("#\t一级\n## 二级\n### 三级"), "<h1>一级</h1><h2>二级</h2><h3>三级</h3>")

    def test_blockquotes_group_only_explicit_quote_lines(self):
        self.assertEqual(
            render_markdown("> 首行\n>\n> *末行*\n普通行\n\n> 后一段\n>> 不递归嵌套"),
            "<blockquote>首行<br><br><em>末行</em></blockquote><p>普通行</p>"
            "<blockquote>后一段<br>&gt; 不递归嵌套</blockquote>",
        )

    def test_general_parser_never_removes_report_metadata_or_horizontal_text(self):
        text = "# 本周周报\n\n统计周期：保留\n分析周期：保留\n数据样本：保留\nTop5关键词：保留\n---"
        self.assertEqual(
            render_markdown(text),
            "<h1>本周周报</h1><p>统计周期：保留<br>分析周期：保留<br>"
            "数据样本：保留<br>Top5关键词：保留<br>---</p>",
        )

    def test_untrusted_text_in_every_block_has_only_safe_tags_and_attributes(self):
        attack = '<svg onload="evil()"><script>evil()</script></svg> &amp;'
        for prefix in ("", "# ", "## ", "### ", "- ", "5. ", "5、", "> "):
            with self.subTest(prefix=prefix):
                parsed = Fragment(render_markdown(prefix + attack + " `**literal**` **bold** *em*"))
                self.assertIn(attack, "".join(parsed.text))
                self.assertEqual(parsed.code_text, ["**literal**"])
                self.assertEqual(parsed.tags_in_code, [])
                self.assertEqual(parsed.errors, [])
                self.assertEqual(parsed.stack, [])
                for tag, attrs in parsed.tags:
                    self.assertIn(tag, {"p", "h1", "h2", "h3", "ul", "ol", "li", "blockquote", "code", "strong", "em"})
                    self.assertEqual(attrs, {"start": "5"} if tag == "ol" else {})


if __name__ == "__main__":
    unittest.main()
