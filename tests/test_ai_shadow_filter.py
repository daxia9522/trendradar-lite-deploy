# coding=utf-8
"""个人兴趣 AI 影子筛选的配置、候选和缓存回归测试。"""

import sqlite3
import tempfile
import unittest
from datetime import datetime
from json import dumps, loads
from pathlib import Path
from unittest.mock import patch

from trendradar.ai.shadow_filter import (
    ALLOWED_TAGS,
    ShadowCandidate,
    ShadowInterestFilter,
    ShadowRunSummary,
    _extract_json_array,
    _item_key,
)
from trendradar.core.loader import _load_ai_filter_shadow_config
from trendradar.daily import NewsAnalyzer


class ShadowFilterConfigTests(unittest.TestCase):
    def test_dedicated_model_environment_overrides_yaml(self):
        with patch.dict(
            "os.environ",
            {
                "AI_FILTER_SHADOW_ENABLED": "true",
                "AI_FILTER_MODEL": "gemini/filter-lite",
                "AI_FILTER_FALLBACK_MODELS": "gemini/filter-flash",
                "AI_FILTER_TIMEOUT": "75",
            },
            clear=False,
        ):
            config = _load_ai_filter_shadow_config({
                "ai_filter_shadow": {
                    "enabled": False,
                    "model": "yaml/model",
                    "fallback_models": [],
                    "timeout": 90,
                }
            })
        self.assertTrue(config["ENABLED"])
        self.assertEqual(config["MODEL"], "gemini/filter-lite")
        self.assertEqual(config["FALLBACK_MODELS"], ["gemini/filter-flash"])
        self.assertEqual(config["TIMEOUT"], 75)

    def test_preview_defaults_are_loaded(self):
        config = _load_ai_filter_shadow_config({"ai_filter_shadow": {}})
        self.assertEqual(config["MIN_CONFIDENCE"], 0.75)
        self.assertEqual(config["PREVIEW_TOTAL_LIMIT"], 30)
        self.assertEqual(config["PREVIEW_TAG_LIMIT"], 5)
        self.assertEqual(config["PREVIEW_SOURCE_LIMIT"], 8)


class ShadowCandidateTests(unittest.TestCase):
    def _filter(self):
        shadow = object.__new__(ShadowInterestFilter)
        return shadow

    @staticmethod
    def _word_group(word):
        return {
            "required": [],
            "normal": [{"word": word, "is_regex": False, "pattern": None}],
        }

    def test_rss_url_key_is_stable_across_title_changes(self):
        left = _item_key("rss", "feed", "旧标题", "https://example.com/a")
        right = _item_key("rss", "feed", "新标题", "https://example.com/a")
        self.assertEqual(left, right)

    def test_hotlist_key_is_source_scoped(self):
        left = _item_key("hotlist", "source-a", "相同标题")
        right = _item_key("hotlist", "source-b", "相同标题")
        self.assertNotEqual(left, right)

    def test_prepare_candidates_uses_current_unmatched_titles_only(self):
        shadow = self._filter()
        summary = ShadowRunSummary(policy_hash="test")
        candidates = shadow._prepare_candidates(
            results={
                "source": {
                    "AI命中标题": {"url": "https://example.com/1"},
                    "当前未命中标题": {"url": "https://example.com/2"},
                    "已经下榜标题": {"url": "https://example.com/3"},
                    "上证指数收报3800点": {"url": "https://example.com/4"},
                    "提醒：日内请重点关注": {"url": "https://example.com/5"},
                }
            },
            id_to_name={"source": "测试源"},
            title_info={
                "source": {
                    "AI命中标题": {"last_time": "12-00"},
                    "当前未命中标题": {"last_time": "12-00"},
                    "已经下榜标题": {"last_time": "11-00"},
                    "上证指数收报3800点": {"last_time": "12-00"},
                    "提醒：日内请重点关注": {"last_time": "12-00"},
                }
            },
            raw_rss_items=[
                {
                    "title": "RSS未命中标题",
                    "feed_id": "feed",
                    "feed_name": "测试RSS",
                    "url": "https://example.com/rss",
                }
            ],
            word_groups=[self._word_group("AI")],
            filter_words=[],
            global_filters=[],
            summary=summary,
        )
        self.assertEqual(
            {candidate.title for candidate in candidates},
            {"当前未命中标题", "RSS未命中标题"},
        )
        self.assertEqual(summary.candidates_seen, 5)
        self.assertEqual(summary.keyword_skipped, 1)
        self.assertEqual(summary.dataflow_skipped, 1)
        self.assertEqual(summary.blocked_skipped, 1)


class ShadowOutputTests(unittest.TestCase):
    def test_extract_json_array_accepts_code_fence(self):
        data = _extract_json_array(
            '```json\n[{"id":"a","relevant":true,"tag":"AI",'
            '"confidence":0.9,"reason":"直接相关"}]\n```'
        )
        self.assertEqual(data[0]["id"], "a")

    def test_validate_results_rejects_unknown_and_invalid_rows(self):
        batch = [ShadowCandidate("a", "hotlist", "s", "S", "标题")]
        parsed = ShadowInterestFilter._validate_results(
            [
                {"id": "unknown", "relevant": True, "confidence": 1},
                {"id": "a", "relevant": "yes", "confidence": 1},
                {
                    "id": "a",
                    "relevant": True,
                    "tag": "AI",
                    "confidence": 1.2,
                    "event": "AI产业进展",
                    "reason": "直接相关",
                },
            ],
            batch,
        )
        self.assertEqual(set(parsed), {"a"})
        self.assertEqual(parsed["a"]["confidence"], 1.0)
        self.assertEqual(parsed["a"]["tag"], "AI 相关")
        self.assertEqual(parsed["a"]["event_key"], "AI产业进展")

    def test_validate_results_drops_relevance_for_unknown_tag(self):
        batch = [ShadowCandidate("a", "hotlist", "s", "S", "标题")]
        parsed = ShadowInterestFilter._validate_results(
            [{
                "id": "a",
                "relevant": True,
                "tag": "模型自造标签",
                "confidence": 0.9,
                "reason": "测试",
            }],
            batch,
        )
        self.assertFalse(parsed["a"]["relevant"])
        self.assertEqual(parsed["a"]["tag"], "")

    def test_entertainment_ip_requires_explicit_interest_name(self):
        batch = [
            ShadowCandidate("a", "hotlist", "s", "S", "某歌手科幻小说获奖"),
            ShadowCandidate("b", "hotlist", "s", "S", "流浪地球新片进展"),
        ]
        parsed = ShadowInterestFilter._validate_results(
            [
                {"id": "a", "relevant": True, "tag": "文娱 IP", "confidence": 0.9, "event": "科幻小说获奖"},
                {"id": "b", "relevant": True, "tag": "文娱 IP", "confidence": 0.9, "event": "流浪地球新片"},
            ],
            batch,
        )
        self.assertFalse(parsed["a"]["relevant"])
        self.assertTrue(parsed["b"]["relevant"])

    def test_prompt_requires_fixed_tags_and_rejects_public_importance(self):
        batch = [ShadowCandidate("a", "hotlist", "s", "S", "标题")]
        messages = ShadowInterestFilter._messages("关注芯片", batch)
        system = messages[0]["content"]
        self.assertIn("不得因为新闻重大", system)
        self.assertIn("财经日历", system)
        self.assertIn("字段固定为id、relevant、tag、confidence、event、reason", system)
        for tag in ALLOWED_TAGS:
            self.assertIn(tag, system)

    def test_cache_is_scoped_by_policy_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "shadow.db")
            conn.execute(
                "CREATE TABLE results (item_key TEXT, policy_hash TEXT)"
            )
            conn.execute("INSERT INTO results VALUES ('a', 'old')")
            candidates = [
                ShadowCandidate("a", "hotlist", "s", "S", "标题A"),
                ShadowCandidate("b", "hotlist", "s", "S", "标题B"),
            ]
            pending_old = ShadowInterestFilter._uncached(conn, candidates, "old")
            pending_new = ShadowInterestFilter._uncached(conn, candidates, "new")
            conn.close()
        self.assertEqual([item.item_key for item in pending_old], ["b"])
        self.assertEqual([item.item_key for item in pending_new], ["a", "b"])

    def test_run_writes_cache_and_second_run_submits_nothing(self):
        class FakeClient:
            model = "gemini/filter-lite"
            last_model = "gemini/filter-lite"

            def __init__(self):
                self.calls = 0

            def chat(self, messages):
                self.calls += 1
                items = loads(messages[-1]["content"].split("待分类标题JSON：\n", 1)[1])
                return dumps([
                    {
                        "id": item["id"],
                        "relevant": "芯片" in item["title"],
                        "tag": "芯片" if "芯片" in item["title"] else "",
                        "confidence": 0.9,
                        "event": "芯片产业进展" if "芯片" in item["title"] else "",
                        "reason": "测试",
                    }
                    for item in items
                ], ensure_ascii=False)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            interests = root / "interests.txt"
            interests.write_text("关注芯片，不按公共重要性兜底。", encoding="utf-8")
            config = {
                "AI": {},
                "AI_FILTER_SHADOW": {
                    "MODEL": "gemini/filter-lite",
                    "FALLBACK_MODELS": [],
                    "BATCH_SIZE": 10,
                    "MIN_CONFIDENCE": 0.65,
                },
                "STORAGE": {"LOCAL": {"DATA_DIR": str(root / "output")}},
            }
            shadow = ShadowInterestFilter(
                config, lambda: datetime(2026, 8, 31, 1, 0, 0)
            )
            shadow.interests_path = interests
            shadow.client = FakeClient()
            kwargs = {
                "results": {
                    "source": {
                        "芯片产业进展": {},
                        "普通社会新闻": {},
                    }
                },
                "id_to_name": {"source": "测试源"},
                "title_info": {
                    "source": {
                        "芯片产业进展": {"last_time": "01-00"},
                        "普通社会新闻": {"last_time": "01-00"},
                    }
                },
                "raw_rss_items": [],
                "word_groups": [{
                    "required": [],
                    "normal": [{
                        "word": "不会命中",
                        "is_regex": False,
                        "pattern": None,
                    }],
                }],
                "filter_words": [],
                "global_filters": [],
            }
            first = shadow.run(**kwargs)
            second = shadow.run(**kwargs)
            payload = loads(shadow.latest_path.read_text(encoding="utf-8"))

        self.assertEqual(first.submitted, 2)
        self.assertEqual(first.classified, 2)
        self.assertEqual(first.relevant, 1)
        self.assertEqual(second.submitted, 0)
        self.assertEqual(second.cached_skipped, 2)
        self.assertEqual(shadow.client.calls, 1)
        self.assertFalse(payload["affects_push"])

    def test_formal_preview_enforces_tag_source_and_event_limits(self):
        shadow = object.__new__(ShadowInterestFilter)
        shadow.min_confidence = 0.75
        shadow.preview_total_limit = 4
        shadow.preview_tag_limit = 2
        shadow.preview_source_limit = 2
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "shadow.db")
            conn.execute(
                """
                CREATE TABLE results (
                    item_key TEXT, policy_hash TEXT, source_type TEXT,
                    source_name TEXT, title TEXT, tag TEXT, confidence REAL,
                    event_key TEXT, reason TEXT, classified_at TEXT,
                    relevant INTEGER
                )
                """
            )
            rows = [
                ("a", "p", "hotlist", "A", "芯片公司发布新品", "芯片", 0.95, "芯片公司新品", "", "t", 1),
                ("b", "p", "hotlist", "A", "Chip maker launches new product", "芯片", 0.94, "芯片公司新品", "", "t", 1),
                ("c", "p", "hotlist", "A", "另一家存储企业扩产", "芯片", 0.93, "存储企业扩产", "", "t", 1),
                ("d", "p", "hotlist", "B", "A股基金发行增长", "资本市场", 0.92, "基金发行增长", "", "t", 1),
                ("e", "p", "rss", "C", "原油价格上涨", "大宗商品", 0.91, "原油价格上涨", "", "t", 1),
                ("f", "p", "rss", "D", "机器人公司融资", "机器人", 0.90, "机器人公司融资", "", "t", 1),
            ]
            conn.executemany("INSERT INTO results VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
            preview = shadow._build_formal_preview(
                conn, "p", [row[0] for row in rows]
            )
            conn.close()
        self.assertLessEqual(preview["selected"], 4)
        self.assertLessEqual(preview["by_tag"].get("芯片", 0), 2)
        self.assertLessEqual(preview["by_source"].get("hotlist:A", 0), 2)
        self.assertEqual(
            sum(item["event"] == "芯片公司新品" for item in preview["items"]),
            1,
        )


class ShadowIsolationTests(unittest.TestCase):
    def test_shadow_failure_does_not_escape_daily_pipeline(self):
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.ctx = type(
            "Ctx",
            (),
            {
                "config": {"AI_FILTER_SHADOW": {"ENABLED": True}, "DEBUG": False},
                "get_time": staticmethod(lambda: None),
            },
        )()
        with patch(
            "trendradar.ai.shadow_filter.ShadowInterestFilter.run",
            side_effect=RuntimeError("shadow failed"),
        ):
            analyzer._run_ai_filter_shadow(
                results={},
                id_to_name={},
                title_info={},
                raw_rss_items=[],
                word_groups=[],
                filter_words=[],
                global_filters=[],
            )


if __name__ == "__main__":
    unittest.main()
