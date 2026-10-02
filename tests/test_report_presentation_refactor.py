"""Synthetic DOM/content/resource contracts for the unified report components.

The former whole-document hashes froze two intentionally different layouts.
These expectations instead pin content, safety, clocks and the shared DOM;
resource digests below check installed bytes, not historical presentation.
"""
import ast
import copy
from datetime import datetime, timedelta, timezone
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zipfile

from trendradar.context import AppContext
from trendradar.report.ai import render_ai_analysis_html_rich
from trendradar.report.components import render_ai_card, render_header
from trendradar.report.daily import render_daily_html, render_report_body
from trendradar.report.markdown import render_markdown
from trendradar.report.models import ReportMeta
from trendradar.report.shell import render_document
from trendradar.report.styles import COMMON_STYLESHEETS, load_stylesheets
from trendradar.report.weekly import render_weekly_html


ROOT = Path(__file__).resolve().parents[1]
FIXED_TIME = datetime(2026, 10, 2, 10, 40, 15, tzinfo=timezone(timedelta(hours=8)))


def make_daily_fixture():
    """Exercise every daily region, escaped text, rank and timestamp paths."""
    lead = {
        "title": '热点 <script>evil()</script> & "新闻"',
        "source_name": '来源 <img src=x> &amp;',
        "time_display": '[08:00 ~ 09:05] & <时间>',
        "count": 2,
        "ranks": [1, 3],
        "rank_threshold": 10,
        "url": 'https://example.invalid/news?a=1&b="two"',
        "is_new": True,
        "matched_keyword": '关键<词>&"标签"',
    }
    second = dict(lead, title="普通热点", url="javascript:evil()", ranks=[5], count=1, is_new=False)
    third = dict(lead, title="低位热点", url="data:text/html,bad", ranks=[20], count=1, is_new=False)
    rss = [{"word": "订阅<关键词>", "count": 2, "titles": [
        {"title": "RSS <script>evil()</script>", "url": "https://example.invalid/rss?a=1&b=2",
         "time_display": "10-02 08:00 <本地>", "source_name": "Feed & <作者>", "is_new": True},
        {"title": "RSS 无链接", "url": "javascript:evil()", "source_name": "备用源"},
    ]}]
    return {
        "report_data": {
            "failed_ids": ['失败平台<script> & "异常"'],
            "stats": [
                {"word": "热点组<&>", "count": 11, "titles": [lead]},
                {"word": "温热组", "count": 6, "titles": [second]},
                {"word": "普通组", "count": 1, "titles": [third]},
            ],
            "new_titles": [{"source_name": "新增源<&>", "titles": [
                {"title": '新增 <img src=x> & "标题"', "ranks": [7, 9], "url": "https://example.invalid/new"},
                {"title": "未排名新增", "ranks": [], "url": "javascript:evil()"},
            ]}],
            "total_new_count": 2,
        },
        "mode": "current",
        "generated_at": FIXED_TIME.strftime("%Y-%m-%d %H:%M:%S"),
        "rss_items": rss,
        "rss_new_items": [{"word": "新订阅组", "count": 1, "titles": [rss[0]["titles"][0]]}],
        "standalone_data": {
            "platforms": [{"name": "独立热榜<&>", "items": [
                {"title": "独立热点 & <b>标题</b>", "url": "https://example.invalid/standalone",
                 "ranks": [3, 7], "first_time": "08-00", "last_time": "09-05", "count": 3},
                {"title": "单次热点", "mobileUrl": "https://example.invalid/mobile", "rank": 5,
                 "first_time": "08-00", "last_time": "08-00"},
            ]}],
            "rss_feeds": [{"name": "独立 Feed<&>", "items": [
                {"title": "独立订阅", "url": "https://example.invalid/feed", "author": "作者<script>",
                 "published_at": "2026-10-02T02:03:04+08:00"},
                {"title": "坏日期也保留", "url": "javascript:evil()", "published_at": "badTdate<script>"},
                {"title": "数值日期也保留", "published_at": 123},
            ]}],
        },
        "ai_analysis": SimpleNamespace(success=True, sections=[
            SimpleNamespace(title="事件<&>", format_type="events", content="【政策面】：\n**甲**\n【市场面】：\n乙"),
            SimpleNamespace(title="列表<&>", format_type="lead_points", content="- **第一条**\n- 第二条<script>"),
            SimpleNamespace(title="正文<&>", format_type="prose", content="第一行\n第二行\n\n5. 第五项\n6、第六项\n\n> 引用\n*斜体* 与 `代码`"),
        ]),
        "analysis_model": 'provider/fallback<model>&"实际"',
    }


def daily_cases():
    base = make_daily_fixture()
    cases = {"daily_default": base}
    cases["daily_hidden"] = dict(copy.deepcopy(base), show_new_section=False)
    cases["daily_platform"] = dict(copy.deepcopy(base), display_mode="platform", mode="incremental")
    cases["daily_reordered"] = dict(copy.deepcopy(base), region_order=["ai_analysis", "standalone", "new_items", "rss", "hotlist"])
    cases["daily_failure"] = dict(copy.deepcopy(base), ai_analysis=SimpleNamespace(success=False), mode="unknown")
    cases["daily_empty"] = {
        "report_data": {"failed_ids": [], "stats": [], "new_titles": [], "total_new_count": 0},
        "mode": "daily", "generated_at": FIXED_TIME.strftime("%Y-%m-%d %H:%M:%S"), "analysis_model": None,
    }
    return cases


def weekly_cases():
    base = {
        "title": '测试周报 <script> & "标题"',
        "date_range": "2026-09-26 ~ 2026-10-02 <周期>",
        "model_name": ' provider/fallback<&"model"> ',
        "statistics": {"Top5关键词": '芯片&<script> / &amp; / 火箭"事件" / /  '},
        "report_markdown": (
            "# 新闻周报\n\n统计周期：重复周期\n数据样本：重复样本\n\n---\n\n"
            "# 一级正文\n## 重点\n第一行\n第二行\n\n### 子标题\n"
            "5. 第五项\n6. 第六项\n\n5、中文编号原样\n\n"
            "- **第一要点**\n- 第二要点 <script>evil()</script>\n\n"
            "> *引用* 与 `代码`\n\n**加粗** 与 *斜体* &amp; <img src=x>\n"
        ),
        "generated_at": FIXED_TIME.strftime("%Y-%m-%d %H:%M:%S"),
    }
    return {
        "weekly_default": base,
        "weekly_missing_keywords": dict(copy.deepcopy(base), statistics={}, model_name=None),
        "weekly_empty_pills": dict(copy.deepcopy(base), statistics={"Top5关键词": " / "}, report_markdown="## 空关键词\n正文"),
    }


class ReportDOM(HTMLParser):
    """Small stdlib-only DOM probe retaining parentage, attributes and visible text."""
    def __init__(self, document):
        super().__init__(convert_charrefs=True)
        self.nodes = []
        self.stack = []
        self.feed(document)

    def handle_starttag(self, tag, attrs):
        node = {"tag": tag, "attrs": dict(attrs), "parent": self.stack[-1] if self.stack else None, "text": []}
        self.nodes.append(node)
        if tag not in {"meta", "br", "img", "hr", "link", "input"}:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index]["tag"] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        for node in self.stack:
            node["text"].append(data)

    def with_class(self, name):
        return [node for node in self.nodes if name in node["attrs"].get("class", "").split()]

    @staticmethod
    def text(node):
        return "".join(node["text"])

    def children(self, node):
        return [child for child in self.nodes if child["parent"] is node]

    def signature(self, node):
        """Canonical subtree, including attributes, structure and visible text."""
        return (node["tag"], tuple(sorted(node["attrs"].items())), self.text(node).strip(),
                tuple(self.signature(child) for child in self.children(node)))


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ReportPresentationTests(unittest.TestCase):
    def test_documents_use_one_shell_with_explicit_components(self):
        for name, kwargs in daily_cases().items():
            with self.subTest(name=name):
                rendered = render_daily_html(**kwargs)
                dom = ReportDOM(rendered)
                self.assertEqual(len(dom.with_class("header")), 1)
                self.assertEqual(len(dom.with_class("report-layout")), 1)
                self.assertEqual(len(dom.with_class("meta-item")), 3)
                self.assertEqual(len(dom.with_class("ai-card")), int(name != "daily_empty"))
                self.assertEqual(len(dom.with_class("ai-card__heading")), int(name not in {"daily_failure", "daily_empty"}))
                self.assertFalse(dom.with_class("tab-strip"))
                self.assertFalse(dom.with_class("report"))
                self.assertEqual([dom.text(node) for node in dom.with_class("header-title")], ["热点新闻分析"])
                self.assertEqual(dom.text(dom.with_class("meta-item")[2]), "生成时间：2026-10-02 10:40:15")
                self.assertEqual(rendered.count('<meta name="color-scheme" content="light dark">'), 1)
                self.assertEqual(rendered.count('<meta name="supported-color-schemes" content="light dark">'), 1)
                if name == "daily_empty":
                    self.assertFalse(dom.children(dom.with_class("report-layout")[0]))
                    self.assertEqual(dom.text(dom.with_class("meta-item")[1]), "分析模型：未启用")
                else:
                    self.assertEqual([dom.text(node).strip() for node in dom.with_class("error-item")],
                                     ['失败平台<script> & "异常"'])
                    self.assertEqual(len(dom.with_class("news-item")), 8)
                    self.assertEqual(len(dom.with_class("new-item")), 0 if name == "daily_hidden" else 2)
                    self.assertEqual(len(dom.with_class("rss-item")), 2 if name == "daily_hidden" else 3)
                    self.assertEqual(dom.text(dom.with_class("meta-item")[1]), '分析模型：provider/fallback<model>&"实际"')
        for name, kwargs in weekly_cases().items():
            with self.subTest(name=name):
                dom = ReportDOM(render_weekly_html(**kwargs))
                self.assertEqual(len(dom.with_class("header")), 1)
                layout = dom.with_class("report-layout")[0]
                self.assertEqual([child["attrs"].get("class") for child in dom.children(layout)], ["ai-card"])
                self.assertEqual(len(dom.with_class("ai-card__surface")), 1)
                self.assertEqual(len(dom.with_class("ai-card__body")), 1)
                self.assertFalse(dom.with_class("ai-card__heading"))
                self.assertFalse(dom.with_class("news-region"))
                self.assertFalse(dom.with_class("report"))
                self.assertEqual(dom.text(dom.with_class("header-title")[0]), '测试周报 <script> & "标题"')
                self.assertEqual(dom.text(dom.with_class("meta-item")[2]), "生成时间：2026-10-02 10:40:15")
                if name == "weekly_missing_keywords":
                    self.assertEqual(dom.text(dom.with_class("meta-item")[1]), "周报模型：-")
                    self.assertEqual([dom.text(node) for node in dom.with_class("tab-pill")], ["-"])
                elif name == "weekly_empty_pills":
                    self.assertFalse(dom.with_class("tab-strip"))
                    self.assertIn("空关键词", dom.text(dom.with_class("ai-card__body")[0]))

    def test_daily_dom_region_order_links_and_numbering(self):
        rendered = render_daily_html(**make_daily_fixture())
        dom = ReportDOM(rendered)
        report = dom.with_class("report-layout")[0]
        children = [node for node in dom.nodes if node["parent"] is report]
        self.assertEqual([node["attrs"]["class"] for node in children], [
            "news-region error-section", "news-region hotlist-section", "news-region rss-section",
            "news-region new-section", "news-region rss-section",
            "news-region standalone-section", "ai-card",
        ])
        self.assertEqual(dom.text(dom.with_class("meta-item")[0]), "报告类型：当前榜单")
        self.assertEqual(dom.text(dom.with_class("meta-item")[1]), '分析模型：provider/fallback<model>&"实际"')
        self.assertEqual(dom.text(dom.with_class("meta-item")[2]), "生成时间：2026-10-02 10:40:15")
        self.assertEqual([dom.text(node) for node in dom.nodes if node["tag"] == "h3"], [])
        self.assertIn("<p>【政策面】：<br><strong>甲</strong><br>【市场面】：<br>乙</p>", rendered)
        self.assertIn("<ul><li><strong>第一条</strong></li><li>第二条&lt;script&gt;</li></ul>", rendered)
        lists = [node for node in dom.nodes if node["tag"] == "ol"]
        self.assertEqual([node["attrs"] for node in lists], [{"start": "5"}])
        self.assertIn("<p>第一行<br>第二行</p>", rendered)
        self.assertIn('<span class="rank-num top">1-3</span>', rendered)
        self.assertIn('<span class="time-info">08:00~09:05</span>', rendered)
        self.assertIn('<span class="time-info">badTdate&lt;script&gt;</span>', rendered)
        links = [node["attrs"]["href"] for node in dom.nodes if node["tag"] == "a"]
        self.assertTrue(links)
        self.assertTrue(all(url.startswith("https://example.invalid/") for url in links))
        self.assertIn('https://example.invalid/news?a=1&b="two"', links)
        self.assertFalse({"script", "img", "iframe"} & {node["tag"] for node in dom.nodes})
        self.assertNotIn("&amp;lt;script", rendered)

    def test_hidden_new_items_and_custom_region_order_are_semantic(self):
        hidden = ReportDOM(render_daily_html(**daily_cases()["daily_hidden"]))
        self.assertFalse(hidden.with_class("new-section"))
        self.assertEqual(len(hidden.with_class("rss-section")), 1)
        reordered = ReportDOM(render_daily_html(**daily_cases()["daily_reordered"]))
        report = reordered.with_class("report-layout")[0]
        children = [node["attrs"]["class"] for node in reordered.nodes if node["parent"] is report]
        self.assertEqual(children, ["news-region error-section", "ai-card", "news-region standalone-section",
                                   "news-region new-section", "news-region rss-section",
                                   "news-region rss-section", "news-region hotlist-section"])
        self.assertEqual(render_report_body(
            {"failed_ids": [], "stats": [], "new_titles": [], "total_new_count": 0}, region_order=[]), "")

    def test_omitted_regions_stay_hidden_without_suppressing_platform_errors(self):
        kwargs = dict(make_daily_fixture(), region_order=["rss", "unknown", "ai_analysis"])
        dom = ReportDOM(render_daily_html(**kwargs))
        layout = dom.with_class("report-layout")[0]
        self.assertEqual([node["attrs"]["class"] for node in dom.children(layout)],
                         ["news-region error-section", "news-region rss-section", "ai-card"])
        self.assertFalse(dom.with_class("standalone-section"))
        self.assertFalse(dom.with_class("hotlist-section"))
        self.assertFalse(dom.with_class("new-section"))

    def test_news_text_counts_links_and_sort_order_are_preserved(self):
        for display_mode in ("keyword", "platform"):
            with self.subTest(display_mode=display_mode):
                dom = ReportDOM(render_daily_html(**dict(make_daily_fixture(), display_mode=display_mode)))
                self.assertEqual([dom.text(node).strip() for node in dom.with_class("news-title")], [
                    '热点 <script>evil()</script> & "新闻"', "普通热点", "低位热点",
                    "独立热点 & <b>标题</b>", "单次热点", "独立订阅", "坏日期也保留", "数值日期也保留",
                ])
                self.assertEqual([dom.text(node).strip() for node in dom.with_class("new-item-title")],
                                 ['新增 <img src=x> & "标题"', "未排名新增"])
                self.assertEqual([dom.text(node) for node in dom.with_class("word-count")], ["11条热点", "6条热点", "1条热点"])
                self.assertEqual([dom.text(node) for node in dom.with_class("word-index")], [" ▼1/3", " ▼2/3", " ▼3/3"])
                self.assertEqual([dom.text(node) for node in dom.with_class("feed-count")], ["2条", "1条"])
                self.assertEqual([dom.text(node) for node in dom.with_class("standalone-count")], ["2条", "3条"])
                self.assertEqual([dom.text(node) for node in dom.with_class("rank-num")], ["1-3", "5", "20", "3-7", "5"])
                if display_mode == "platform":
                    self.assertEqual([dom.text(node) for node in dom.with_class("keyword-tag")], ['[关键<词>&"标签"]'] * 3)
                else:
                    self.assertFalse(dom.with_class("keyword-tag"))
                    self.assertEqual([dom.text(node) for node in dom.with_class("source-name")][:3], ['来源 <img src=x> &amp;'] * 3)

    def test_same_meta_produces_identical_headers_through_both_consumers(self):
        empty = daily_cases()["daily_empty"]["report_data"]
        for pills in ((), ("日报也能显示<关键词>", "安全&转义")):
            with self.subTest(pills=pills):
                meta = ReportMeta("共同标题<&>", (("字段", "任意值<&>"), ("生成时间", "固定时间")), pills)
                with patch("trendradar.report.daily.ReportMeta", return_value=meta), \
                     patch("trendradar.report.weekly.ReportMeta", return_value=meta):
                    daily = ReportDOM(render_daily_html(empty, generated_at="unused"))
                    weekly = ReportDOM(render_weekly_html("标题", "周期", "模型", {}, "正文", generated_at="unused"))
                self.assertEqual(daily.signature(daily.with_class("header")[0]),
                                 weekly.signature(weekly.with_class("header")[0]))
                self.assertEqual(len(daily.with_class("tab-strip")), int(bool(pills)))
                self.assertEqual([daily.text(node) for node in daily.with_class("tab-pill")], list(pills))

    def test_same_safe_ai_body_and_heading_have_identical_dom_in_both_style_bundles(self):
        body = render_markdown("## 共同正文\n两行\n内容<script>\n\n5. 第五项\n6、后续\n\n> *引用* 与 `代码`")
        meta = ReportMeta("共同标题", (("字段", "值"),))
        for title in (None, "", '共享 AI 标题<script>&"'):
            with self.subTest(title=title):
                card = render_ai_card(body, title=title)
                documents = [ReportDOM(render_document(
                    title=meta.title, header_html=render_header(meta), content_html=card, stylesheets=styles,
                )) for styles in (COMMON_STYLESHEETS, (*COMMON_STYLESHEETS, "news"))]
                weekly, daily = documents
                self.assertEqual(daily.signature(daily.with_class("ai-card")[0]),
                                 weekly.signature(weekly.with_class("ai-card")[0]))
                for dom in documents:
                    surface = dom.with_class("ai-card__surface")[0]
                    self.assertEqual(surface["parent"]["attrs"]["class"], "ai-card")
                    self.assertIs(dom.with_class("ai-card__body")[0]["parent"], surface)
                    self.assertEqual(len(dom.with_class("ai-card__heading")), int(bool(title)))
                    self.assertEqual(len(dom.with_class("ai-intel-icon")), int(bool(title)))
                    self.assertFalse([node for node in dom.nodes if node["tag"] == "script"])

    def test_daily_and_weekly_call_the_same_card_renderer_and_position_does_not_change_it(self):
        with patch("trendradar.report.ai.render_ai_card", wraps=render_ai_card) as daily_card:
            rendered = render_daily_html(**make_daily_fixture())
        daily_card.assert_called_once()
        self.assertEqual(daily_card.call_args.kwargs, {"title": "AI 新闻简报"})
        self.assertIn('<h2 class="ai-block-title">事件&lt;&amp;&gt;</h2>', daily_card.call_args.args[0])
        with patch("trendradar.report.weekly.render_ai_card", wraps=render_ai_card) as weekly_card:
            weekly_rendered = render_weekly_html(**weekly_cases()["weekly_default"])
        weekly_card.assert_called_once()
        self.assertEqual(weekly_card.call_args.kwargs, {})
        self.assertIn('<ol start="5">', weekly_card.call_args.args[0])
        self.assertNotIn("AI 新闻简报", weekly_rendered)
        default = ReportDOM(rendered)
        reordered = ReportDOM(render_daily_html(**daily_cases()["daily_reordered"]))
        self.assertEqual(default.signature(default.with_class("ai-card")[0]),
                         reordered.signature(reordered.with_class("ai-card")[0]))

    def test_weekly_dom_preserves_header_card_and_markdown_policy(self):
        rendered = render_weekly_html(**weekly_cases()["weekly_default"])
        dom = ReportDOM(rendered)
        report = dom.with_class("ai-card__body")[0]
        self.assertEqual(report["parent"]["attrs"]["class"], "ai-card__surface")
        self.assertEqual([dom.text(node) for node in dom.with_class("meta-item")], [
            "收集时间：2026-09-26 ~ 2026-10-02 <周期>",
            '周报模型：Provider/fallback<&"model">',
            "生成时间：2026-10-02 10:40:15",
        ])
        self.assertEqual([dom.text(node) for node in dom.with_class("tab-pill")], ['芯片&<script>', '&amp;', '火箭"事件"'])
        self.assertIn("<p>第一行<br>第二行</p>", rendered)
        self.assertIn("<h1>一级正文</h1>", rendered)
        self.assertIn("<h2>重点</h2>", rendered)
        self.assertIn("<h3>子标题</h3>", rendered)
        self.assertIn('<blockquote><em>引用</em> 与 <code>代码</code></blockquote>', rendered)
        self.assertEqual([node["attrs"] for node in dom.nodes if node["tag"] == "ol"], [{"start": "5"}, {"start": "5"}])
        self.assertNotIn("重复周期", rendered)
        self.assertNotIn("重复样本", rendered)
        self.assertNotIn("&amp;lt;script", rendered)
        self.assertFalse({"script", "img", "iframe"} & {node["tag"] for node in dom.nodes})

    def test_app_context_resolves_configured_clock_once_before_direct_rendering(self):
        context = AppContext({"TIMEZONE": "America/New_York"})
        configured_time = FIXED_TIME.astimezone(timezone(timedelta(hours=-4)))
        with patch("trendradar.context.get_configured_time", return_value=configured_time) as clock, \
             patch("trendradar.context.render_daily_html", wraps=render_daily_html) as renderer:
            rendered = context.render_email_html(daily_cases()["daily_empty"]["report_data"])
        clock.assert_called_once_with("America/New_York")
        renderer.assert_called_once()
        self.assertEqual(renderer.call_args.kwargs["generated_at"], "2026-10-01 22:40:15")
        self.assertEqual(renderer.call_args.kwargs["parse_timestamp"], datetime.fromisoformat)
        dom = ReportDOM(rendered)
        self.assertEqual(dom.text(dom.with_class("meta-item")[2]), "生成时间：2026-10-01 22:40:15")

    def test_pure_renderers_use_supplied_display_time_without_resolving_a_clock(self):
        generated_at = "指定显示时间 <clock>&"
        with patch("trendradar.context.get_configured_time", side_effect=AssertionError("unexpected clock lookup")):
            documents = (
                render_daily_html(**dict(daily_cases()["daily_empty"], generated_at=generated_at)),
                render_weekly_html(**dict(weekly_cases()["weekly_default"], generated_at=generated_at)),
            )
        for rendered in documents:
            dom = ReportDOM(rendered)
            self.assertEqual(dom.text(dom.with_class("meta-item")[2]), f"生成时间：{generated_at}")
            self.assertNotIn("<clock>", rendered)

    def test_app_context_keeps_actual_model_display_flags_and_section_inputs(self):
        kwargs = make_daily_fixture()
        kwargs["ai_analysis"].model = ' provider/fallback<model>&"实际" '
        order = ["ai_analysis", "standalone", "new_items", "rss", "hotlist"]
        context = AppContext({
            "AI": {"MODEL": "configured-primary-must-not-display"},
            "DISPLAY_MODE": "platform",
            "DISPLAY": {"REGION_ORDER": order, "REGIONS": {"NEW_ITEMS": False}},
        })
        inputs = {name: kwargs[name] for name in (
            "report_data", "mode", "rss_items", "rss_new_items", "ai_analysis", "standalone_data",
        )}
        with patch.object(context, "get_time", return_value=FIXED_TIME) as clock, \
             patch("trendradar.context.render_daily_html", wraps=render_daily_html) as renderer:
            rendered = context.render_email_html(**inputs)
        clock.assert_called_once_with()
        renderer.assert_called_once()
        for name, value in inputs.items():
            self.assertIs(renderer.call_args.kwargs[name], value, name)
        self.assertEqual(renderer.call_args.kwargs["region_order"], order)
        self.assertEqual(renderer.call_args.kwargs["display_mode"], "platform")
        self.assertFalse(renderer.call_args.kwargs["show_new_section"])
        dom = ReportDOM(rendered)
        self.assertEqual(dom.text(dom.with_class("meta-item")[1]), '分析模型：Provider/fallback<model>&"实际"')
        self.assertNotIn("configured-primary-must-not-display", rendered)
        self.assertEqual([child["attrs"]["class"] for child in dom.children(dom.with_class("report-layout")[0])], [
            "news-region error-section", "ai-card", "news-region standalone-section",
            "news-region rss-section", "news-region hotlist-section",
        ])
        self.assertEqual(len(dom.with_class("keyword-tag")), 3)
        self.assertFalse(dom.with_class("new-section"))
        self.assertEqual(len(dom.with_class("rss-section")), 1)
        self.assertIn('<span class="time-info">10-02 02:03</span>', rendered)
        self.assertIn("badTdate&lt;script&gt;", rendered)

    def test_app_context_keeps_failure_and_disabled_model_labels(self):
        context = AppContext({})
        report = daily_cases()["daily_empty"]["report_data"]
        for analysis, label in (
            (None, "未启用"),
            (SimpleNamespace(success=True, model="", sections=[]), "未启用"),
            (SimpleNamespace(success=False, model="private fallback", error="private error"), "分析失败"),
        ):
            with self.subTest(label=label), patch.object(context, "get_time", return_value=FIXED_TIME):
                rendered = context.render_email_html(report, ai_analysis=analysis)
            dom = ReportDOM(rendered)
            self.assertEqual(dom.text(dom.with_class("meta-item")[1]), f"分析模型：{label}")
            self.assertNotIn("private fallback", rendered)
            self.assertNotIn("private error", rendered)

    def test_app_context_generate_saves_real_renderer_output_to_all_report_paths(self):
        context = AppContext({})
        kwargs = make_daily_fixture()
        kwargs["ai_analysis"].model = "provider/fallback"
        cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            try:
                os.chdir(tmp)
                with patch.object(context, "get_time", return_value=FIXED_TIME) as clock, \
                     patch.object(context, "format_date", return_value="2026-10-02"), \
                     patch.object(context, "format_time", return_value="10-40"):
                    path = context.generate_html(
                        kwargs["report_data"]["stats"], failed_ids=kwargs["report_data"]["failed_ids"],
                        mode="current", rss_items=kwargs["rss_items"], rss_new_items=kwargs["rss_new_items"],
                        ai_analysis=kwargs["ai_analysis"], standalone_data=kwargs["standalone_data"],
                    )
                clock.assert_called_once_with()
                self.assertEqual(path, "output/html/2026-10-02/10-40.html")
                rendered = Path(path).read_text(encoding="utf-8")
                self.assertEqual(Path("output/html/latest/current.html").read_text(encoding="utf-8"), rendered)
                self.assertEqual(Path("output/email.html").read_text(encoding="utf-8"), rendered)
                self.assertIn("生成时间", rendered)
                self.assertIn("2026-10-02 10:40:15", rendered)
                self.assertIn("Provider/fallback", rendered)
                self.assertIn("热点 &lt;script&gt;evil()&lt;/script&gt;", rendered)
                self.assertIn("RSS &lt;script&gt;evil()&lt;/script&gt;", rendered)
            finally:
                os.chdir(cwd)

    def test_weekly_missing_and_empty_top5_keep_default_keyword(self):
        for statistics in ({}, {"Top5关键词": ""}, {"Top5关键词": None}):
            with self.subTest(statistics=statistics):
                dom = ReportDOM(render_weekly_html(**dict(weekly_cases()["weekly_default"], statistics=statistics)))
                self.assertEqual([dom.text(node) for node in dom.with_class("tab-pill")], ["-"])

    def test_injected_timestamp_parser_errors_are_not_swallowed(self):
        parser = Mock(side_effect=RuntimeError("timestamp programming error"))
        with self.assertRaisesRegex(RuntimeError, "timestamp programming error"):
            render_report_body(
                daily_cases()["daily_empty"]["report_data"], region_order=["standalone"],
                standalone_data={"rss_feeds": [{"items": [{"published_at": "2026-10-02T00:00:00"}]}]},
                parse_timestamp=parser,
            )
        parser.assert_called_once_with("2026-10-02T00:00:00")

    def test_ai_failure_stays_generic_and_sections_escape_titles(self):
        failed = SimpleNamespace(
            success=False, error="private secret", raw_response="private raw response",
            sections=[SimpleNamespace(title="private title", content="private body", format_type="prose")],
        )
        with patch("trendradar.report.ai.render_markdown") as core:
            rendered = render_ai_analysis_html_rich(failed)
        core.assert_not_called()
        self.assertIn("⚠️ AI 分析失败", rendered)
        for private in (failed.error, failed.raw_response, failed.sections[0].title, failed.sections[0].content):
            self.assertNotIn(private, rendered)
        self.assertEqual(render_ai_analysis_html_rich(None), "")
        self.assertIn('<h2 class="ai-block-title">事件&lt;&amp;&gt;</h2>',
                      render_ai_analysis_html_rich(make_daily_fixture()["ai_analysis"]))

    def test_header_shell_escape_only_plain_text_and_reject_unknown_resources(self):
        meta = ReportMeta('标题 <script> &amp;', (("标签<&>", '值"<img>'),), ('词<script>',))
        header = render_header(meta)
        rendered = render_document(title=meta.title, header_html=header,
                                   content_html="        <p>内部 renderer 正文</p>")
        self.assertIn('<title>标题 &lt;script&gt; &amp;amp;</title>', rendered)
        self.assertIn('<strong>标签&lt;&amp;&gt;</strong>：值&quot;&lt;img&gt;', header)
        self.assertEqual(ReportDOM(header).text(ReportDOM(header).with_class("tab-pill")[0]), '词<script>')
        self.assertNotIn("&amp;lt;", rendered)
        for name in ('../pyproject.toml', 'daily', 'weekly', 'daily.css', 'daily"><script>', "other"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    load_stylesheets((name,))
                with self.assertRaises(ValueError):
                    render_document(title="标题", header_html="", content_html="", stylesheets=(name,))

    def test_component_css_is_embedded_once_in_declared_order(self):
        resources = {name: load_stylesheets((name,)) for name in (*COMMON_STYLESHEETS, "news")}
        for variant in ("daily", "weekly"):
            with self.subTest(variant=variant):
                expected = (*COMMON_STYLESHEETS, "news") if variant == "daily" else COMMON_STYLESHEETS
                css = load_stylesheets(expected)
                self.assertEqual(css, "\n".join(resources[name] for name in expected))
                self.assertNotIn("{{", css)
                self.assertNotIn("</style", css)
                rendered = (render_daily_html(**make_daily_fixture()) if variant == "daily"
                            else render_weekly_html(**weekly_cases()["weekly_default"]))
                self.assertEqual(rendered.count("<style>"), 1)
                self.assertIn("<style>\n" + css + "  </style>", rendered)
                self.assertNotIn('<link rel="stylesheet"', rendered)
                for name in expected:
                    self.assertEqual(css.count(resources[name]), 1, name)
        self.assertEqual(load_stylesheets(("base", "header", "base", "ai", "ai")), load_stylesheets(COMMON_STYLESHEETS))

    def test_rendering_modules_do_not_import_old_entrypoints_or_ai_client(self):
        for name in ("models", "styles", "components", "shell", "markdown", "ai", "daily", "weekly"):
            source = ROOT / "trendradar" / "report" / f"{name}.py"
            for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom):
                    self.assertNotIn(node.module, {"trendradar.ai.analyzer", "trendradar.ai.client", "trendradar.ai.formatter",
                                                   "trendradar.report.html", "weekly_report.presentation", "weekly_report.runtime"}, name)


IMPORT_AUDIT = textwrap.dedent('''
    import os
    import sys

    config_open_attempts = []
    socket_attempts = []
    runtime_import_attempts = []
    forbidden_modules = ("trendradar.ai", "litellm", "weekly_report.runtime")

    def is_forbidden_module(name):
        return any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden_modules)

    def reject_runtime_import(name):
        if is_forbidden_module(name):
            runtime_import_attempts.append(name)
            raise AssertionError(f"runtime import during presentation import: {name}")

    def no_external_io(event, args):
        if event.startswith("socket."):
            socket_attempts.append(event)
            raise AssertionError(f"network access during presentation import: {event}")
        if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            path = os.fsdecode(args[0])
            if path.endswith(("config.yaml", "config.yml", ".env")):
                config_open_attempts.append(path)
                raise AssertionError(f"configuration load during presentation import: {path!r}")
        if event == "import":
            reject_runtime_import(args[0])

    class PresentationImportGuard:
        def find_spec(self, fullname, path=None, target=None):
            # importlib.import_module does not always emit an import audit event.
            reject_runtime_import(fullname)
            return None

    def assert_no_external_io():
        # Caught configuration/runtime errors must not make the probe pass.
        assert not config_open_attempts, (
            f"configuration open attempts: count={len(config_open_attempts)}, paths={config_open_attempts!r}")
        # Every socket event is denied above, including urllib3's caught IPv6
        # capability probe. A rejected socket creation performs no socket I/O.
        assert not runtime_import_attempts, f"runtime import attempts: {runtime_import_attempts!r}"
        loaded = sorted(name for name in sys.modules if is_forbidden_module(name))
        assert not loaded, f"runtime modules loaded during presentation import: {loaded!r}"

    sys.addaudithook(no_external_io)
    sys.meta_path.insert(0, PresentationImportGuard())
''')


IMPORT_PROBE = IMPORT_AUDIT + textwrap.dedent('''
    import hashlib
    import importlib
    import json

    for name in sys.argv[1:]:
        importlib.import_module(name)
    import trendradar.report.styles as style_module
    assert style_module.__file__.startswith(os.environ["PYTHONPATH"] + "/"), style_module.__file__
    from importlib.resources import files
    from trendradar.report.styles import COMMON_STYLESHEETS, load_stylesheets
    resources = sorted(path.name for path in files("trendradar.report").joinpath("styles").iterdir())
    assert resources == ["ai.css", "base.css", "header.css", "news.css"], resources
    digests = {name: hashlib.sha256(load_stylesheets((name,)).encode()).hexdigest()
               for name in (*COMMON_STYLESHEETS, "news")}
    digests.update(
        daily=hashlib.sha256(load_stylesheets((*COMMON_STYLESHEETS, "news")).encode()).hexdigest(),
        weekly=hashlib.sha256(load_stylesheets(COMMON_STYLESHEETS).encode()).hexdigest(),
    )
    # The root package exports AppContext, which imports core.loader definitions.
    # The contract is no configuration I/O, not absence of that existing module.
    assert_no_external_io()
    print(json.dumps(digests))
''')


class ReportResourceTests(unittest.TestCase):
    def test_package_data_declares_css_resources(self):
        try:
            import tomllib
        except ImportError:
            self.skipTest("stdlib TOML inspection requires Python 3.11+")
        config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        package_data = config["tool"]["setuptools"]["package-data"]["trendradar.report"]
        self.assertIn("styles/*.css", package_data)
        self.assertEqual(sorted(path.name for path in (ROOT / "trendradar" / "report" / "styles").iterdir()),
                         ["ai.css", "base.css", "header.css", "news.css"])

    def run_probe(self, pythonpath, cwd, modules=(), program=IMPORT_PROBE):
        return subprocess.run([sys.executable, "-B", "-c", program, *modules], cwd=cwd,
            env={"PYTHONPATH": str(pythonpath), "PYTHONDONTWRITEBYTECODE": "1", "HOME": str(cwd),
                 "PYTHON_DOTENV_DISABLED": "1", "LITELLM_LOCAL_MODEL_COST_MAP": "True",
                 "AWS_EC2_METADATA_DISABLED": "true"},
            capture_output=True, text=True, check=False, timeout=40)

    def probe(self, pythonpath, cwd, modules):
        result = self.run_probe(pythonpath, cwd, modules)
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = {name: digest(load_stylesheets((name,))) for name in (*COMMON_STYLESHEETS, "news")}
        expected.update(
            daily=digest(load_stylesheets((*COMMON_STYLESHEETS, "news"))),
            weekly=digest(load_stylesheets(COMMON_STYLESHEETS)),
        )
        self.assertEqual(json.loads(result.stdout), expected)

    def assert_audit_rejects(self, cwd, operation, immediate_message, final_message):
        program = IMPORT_AUDIT + textwrap.dedent(f'''
            import importlib
            from pathlib import Path
            import socket

            try:
                {operation}
            except AssertionError as exc:
                assert str(exc) == {immediate_message!r}, str(exc)
            else:
                raise AssertionError("audit did not reject the forbidden operation")
            assert_no_external_io()
        ''')
        result = self.run_probe(ROOT, cwd, program=program)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(final_message, result.stderr)

    def test_import_audit_rejects_config_reads_even_when_caught(self):
        # These nonexistent names are resolved only inside a disposable directory.
        with tempfile.TemporaryDirectory() as tmp:
            for filename in ("config.yaml", "config.yml", ".env"):
                for operation in (
                    f"open({filename!r}).close()",
                    f"open({filename.encode()!r}).close()",
                    f"Path({filename!r}).read_text()",
                    f"os.open({filename!r}, os.O_RDONLY)",
                ):
                    with self.subTest(filename=filename, operation=operation):
                        self.assert_audit_rejects(tmp, operation,
                            f"configuration load during presentation import: {filename!r}",
                            f"configuration open attempts: count=1, paths={[filename]!r}")

    def test_import_audit_rejects_socket_operations(self):
        with tempfile.TemporaryDirectory() as tmp:
            for operation, event in (
                ("socket.socket()", "socket.__new__"),
                ("socket.getaddrinfo('example.invalid', 443)", "socket.getaddrinfo"),
            ):
                with self.subTest(operation=operation):
                    program = IMPORT_AUDIT + textwrap.dedent(f'''
                        import socket
                        {operation}
                    ''')
                    result = self.run_probe(ROOT, tmp, program=program)
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn(f"network access during presentation import: {event}", result.stderr)

    def test_import_audit_rejects_runtime_modules_even_when_caught(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("trendradar.ai", "litellm", "weekly_report.runtime"):
                for operation in (f"__import__({name!r})", f"importlib.import_module({name!r})"):
                    with self.subTest(name=name, operation=operation):
                        self.assert_audit_rejects(tmp, operation,
                            f"runtime import during presentation import: {name}",
                            f"runtime import attempts: {[name]!r}")

    def test_resources_and_import_orders_from_nonrepository_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            for modules in (
                ["trendradar.report.models", "trendradar.report.styles", "trendradar.report.components", "trendradar.report.shell"],
                ["trendradar.report.markdown", "trendradar.report.ai", "trendradar.report.weekly", "trendradar.report.daily"],
                ["trendradar.report.weekly", "trendradar.report.daily", "trendradar.report.shell", "trendradar.report.markdown"],
            ):
                with self.subTest(modules=modules):
                    self.probe(ROOT, tmp, modules)

    def test_resources_are_readable_from_zip_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "report-package.zip"
            with zipfile.ZipFile(package, "w") as archive:
                files = list((ROOT / "trendradar").rglob("*.py"))
                files += list((ROOT / "trendradar" / "report" / "styles").glob("*.css"))
                for path in files:
                    archive.write(path, path.relative_to(ROOT).as_posix())
            self.probe(package, tmp, ["trendradar.report.styles", "trendradar.report.weekly"])


if __name__ == "__main__":
    unittest.main()
