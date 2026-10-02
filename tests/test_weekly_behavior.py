"""Deterministic weekly collection, evidence and presentation contracts."""
import json
from datetime import datetime
import unittest
from unittest.mock import Mock, patch

from trendradar.report.weekly import render_weekly_html
from weekly_report import collection, keywords, prompting


class SyntheticHistory:
    def read_all_titles_for_date(self, *, date, db_type):
        title = "日本发生地震" if db_type == "news" else "火箭发射成功"
        return ({f"{db_type}{i}": {title: {"ranks": [i], "count": 2}} for i in (1, 2)},
                {f"{db_type}{i}": f"{db_type} source {i}" for i in (1, 2)}, {})


class WeeklyBehaviorTests(unittest.TestCase):
    def test_collection_merges_days_but_keeps_news_and_rss_pools(self):
        with patch.object(collection, "HistoryReader", return_value=SyntheticHistory()):
            items, platforms, stats = collection.collect_news(datetime(2026, 9, 29), datetime(2026, 9, 30), max_news=10)
        self.assertEqual((stats["raw_news"], stats["raw_rss"]), (4, 4))
        self.assertEqual((stats["exact_total"], stats["cluster_total"], stats["selected"]), (2, 2, 2))
        self.assertEqual((stats["selected_news"], stats["selected_rss"]), (1, 1))
        self.assertEqual(stats["rss_day_coverage"], 1)
        self.assertEqual(sorted(platforms.values()), [2, 2, 2, 2])
        for item in items:
            self.assertEqual(item["count"], 8)
            self.assertEqual(item["dates"], ["2026-09-29", "2026-09-30"])
            self.assertEqual(len(item["platforms"]), 2)
        evidence = prompting.build_evidence_index(items)
        self.assertEqual(set(evidence), {"N1", "R1"})
        self.assertEqual(evidence["N1"]["source_type"], "news")
        messages = prompting.build_prompt("2026-09-29", "2026-09-30", items, platforms, stats)
        text = messages[-1]["content"]
        for expected in ("N1.", "R1.", "日本发生地震", "火箭发射成功", '"raw_news": 4', '"selected_rss": 1'):
            self.assertIn(expected, text)

    def test_themes_require_real_evidence_and_preserve_markdown(self):
        evidence = {"N1": {}, "N2": {}}
        themes = [{"title": "震后救援", "keyword": "日本地震", "evidence_ids": ["n1", "N1", "N2", "N99"]},
                  {"title": "虚构主题", "keyword": "火箭发射", "evidence_ids": ["N99", "N100"]}]
        body = "# 日本地震\n\n救援继续。"
        raw = f"<THEMES_JSON>{json.dumps(themes, ensure_ascii=False)}</THEMES_JSON><REPORT_MARKDOWN>{body}</REPORT_MARKDOWN>"
        report, parsed = keywords.parse_structured_report(raw, evidence)
        self.assertEqual(report, body)
        self.assertEqual(parsed, [{"title": "震后救援", "keyword": "日本地震", "evidence_ids": ["N1", "N2"]}])
        self.assertEqual(keywords.keywords_from_themes(parsed), ["日本地震"])

    def test_keyword_model_failure_uses_local_rules(self):
        client = Mock(model="synthetic/model", api_key="synthetic", api_base="", timeout=1, fallback_models=[])
        failed = Mock()
        failed.chat.side_effect = RuntimeError("synthetic unavailable")
        with patch.object(keywords, "build_keyword_client", return_value=failed):
            result, source = keywords.extract_headline_keywords(client, "2026-09-29", "2026-09-30",
                [{"title": "日本发生地震救援继续", "source_type": "news"},
                 {"title": "日本地震启动救援行动", "source_type": "news"}], "日本地震救援继续")
        self.assertEqual(source, "rule_only")
        self.assertEqual(failed.chat.call_count, 2)
        self.assertTrue(result)
        self.assertTrue(all(label in "日本地震救援继续" for label in result))

    def test_html_preserves_escaping_lists_and_injected_clock(self):
        rendered = render_weekly_html("标题 <script>", "2026-09-29 ~ 2026-09-30", "synthetic/model",
            {"Top5关键词": "日本地震 / 火箭发射"}, "# 新闻周报\n\n统计周期：重复字段\n\n## 正文\n- **重点**\n- <script>unsafe</script>\n\n1. 第一项",
            generated_at="2026-10-01 07:30:00")
        self.assertIn("2026-10-01 07:30:00", rendered)
        self.assertIn("<strong>重点</strong>", rendered)
        self.assertIn("<ol>", rendered)
        self.assertIn("&lt;script&gt;unsafe&lt;/script&gt;", rendered)
        self.assertNotIn("<script>", rendered)
        self.assertNotIn("重复字段", rendered)
