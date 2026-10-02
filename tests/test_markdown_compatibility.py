"""Unified Markdown semantics, escaping and the shared parser's input boundary."""
import ast
import html
from html.parser import HTMLParser
import inspect
import unittest

from trendradar.report.helpers import html_escape
from trendradar.report.markdown import render_inline, render_markdown


# These explicit expectations replace the intentionally different legacy daily
# and weekly snapshots. The change in block semantics is intentional, not a
# reason to drop paragraph, numbering, heading, inline or security assertions.
UNIFIED_CASES = (
    ("paragraph_breaks", "第一行\n第二行", "<p>第一行<br>第二行</p>"),
    ("blank_line_breaks", "第一行\n\n第二行\n第三行", "<p>第一行</p><p>第二行<br>第三行</p>"),
    ("ordered_start", "5. 第五项", '<ol start="5"><li>第五项</li></ol>'),
    ("chinese_order_marker", "5、第五项", '<ol start="5"><li>第五项</li></ol>'),
    ("headings", "# 一级\n## 二级\n### 三级", "<h1>一级</h1><h2>二级</h2><h3>三级</h3>"),
    ("emphasis", "*斜体* 与 **加粗** 和 `代码`", "<p><em>斜体</em> 与 <strong>加粗</strong> 和 <code>代码</code></p>"),
    ("blockquote", "> 引用", "<blockquote>引用</blockquote>"),
    ("quote_soft_break", "> 第一行\n> 第二行", "<blockquote>第一行<br>第二行</blockquote>"),
    ("default_bullets", "- 甲\n- 乙", "<ul><li>甲</li><li>乙</li></ul>"),
    ("separate_bullet_runs", "- 甲\n\n说明\n\n* 乙", "<ul><li>甲</li></ul><p>说明</p><ul><li>乙</li></ul>"),
    ("raw_html_escaped", "<script>alert(1)</script> **安全**", "<p>&lt;script&gt;alert(1)&lt;/script&gt; <strong>安全</strong></p>"),
)


class Tags(HTMLParser):
    def __init__(self, content):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.text = []
        self.feed(content)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_data(self, data):
        self.text.append(data)


class MarkdownContractTests(unittest.TestCase):
    def test_plain_markdown_has_one_exact_contract(self):
        for name, text, expected in UNIFIED_CASES:
            with self.subTest(name=name):
                rendered = render_markdown(text)
                self.assertEqual(rendered, expected)
                self.assertNotIn("ai-markdown", rendered)
                self.assertNotIn("ai-subtitle", rendered)

    def test_ordered_lists_restart_from_explicit_number_after_paragraph(self):
        text = "1. 第一项\n\n说明\n\n5. 第五项\n6、第六项"
        expected = (
            '<ol><li>第一项</li></ol><p>说明</p>'
            '<ol start="5"><li>第五项</li><li>第六项</li></ol>'
        )
        rendered = render_markdown(text)
        self.assertEqual(rendered, expected)
        self.assertEqual(
            [attrs for tag, attrs in Tags(rendered).tags if tag == "ol"],
            [{}, {"start": "5"}],
        )

    def test_legacy_subtitle_policy_is_not_part_of_the_general_parser(self):
        text = "【政策面】：\n甲\n【市场面】：\n乙"
        plain = "<p>【政策面】：<br>甲<br>【市场面】：<br>乙</p>"
        self.assertEqual(render_markdown(text), plain)
        for title in ("【标题】", "【标题】：", "【标题】:", "  【标题】：  "):
            with self.subTest(title=title):
                self.assertEqual(render_markdown(title), f"<p>{title.strip()}</p>")

    def test_code_is_literal_and_surrounding_inline_emphasis_is_rendered(self):
        text = "`**粗体**` 与 **`代码`** 和 *斜体*"
        expected = "<code>**粗体**</code> 与 <strong><code>代码</code></strong> 和 <em>斜体</em>"
        self.assertEqual(render_inline(text), expected)

    def test_malicious_html_is_text_and_is_escaped_exactly_once(self):
        text = '<img src=x onerror="evil()"> &amp; <script>evil()</script>'
        rendered = render_inline(text)
        self.assertEqual(rendered, html.escape(text))
        self.assertEqual(Tags(rendered).tags, [])
        self.assertEqual("".join(Tags(rendered).text), text)
        self.assertNotIn("&amp;lt;img", rendered)
        parsed = Tags(render_markdown(text + " **重点** `代码` *强调*"))
        self.assertTrue(
            {tag for tag, _ in parsed.tags} <= {"p", "strong", "code", "em"}
        )
        self.assertIn(text, "".join(parsed.text))

    def test_weekly_metadata_cleanup_is_not_a_markdown_rule(self):
        text = "统计周期：正文提到的周期\n数据样本：正文数据"
        expected = "<p>统计周期：正文提到的周期<br>数据样本：正文数据</p>"
        self.assertEqual(render_markdown(text), expected)

    def test_helper_nonstring_and_missing_value_contracts(self):
        for value in (None, False, 0, [], {}, 123):
            with self.subTest(value=value):
                self.assertEqual(html_escape(value), html.escape(str(value)))
                for strict in (render_inline, render_markdown):
                    with self.assertRaises(AttributeError):
                        strict(value)
        for value in ("", " \n ", "\t\r\n\t"):
            self.assertEqual(render_markdown(value), "")

    def test_block_core_has_one_line_loop_and_no_daily_policy_import(self):
        from trendradar.report import markdown

        tree = ast.parse(inspect.getsource(markdown))
        definitions = {
            node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)
        }
        self.assertEqual(set(definitions), {"render_inline", "render_markdown"})
        self.assertEqual(list(inspect.signature(render_markdown).parameters), ["text"])
        self.assertEqual(list(inspect.signature(render_inline).parameters), ["text"])
        loops = [node for node in ast.walk(definitions["render_markdown"])
                 if isinstance(node, (ast.For, ast.While))]
        self.assertEqual(len(loops), 1)
        self.assertIsInstance(loops[0], ast.For)
        self.assertEqual(ast.unparse(loops[0].iter), "text.splitlines()")
        self.assertFalse(any(
            isinstance(node, ast.ImportFrom) and node.module == "ai_content"
            for node in ast.walk(tree)
        ))


if __name__ == "__main__":
    unittest.main()
