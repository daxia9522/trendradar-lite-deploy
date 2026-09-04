# coding=utf-8
"""关键词 RSS 的新增标记与当前式评分回归测试。"""

import unittest

from trendradar.ai.selector import _item_score
from trendradar.core.analyzer import count_rss_frequency


def _rss_item(title, url):
    return {
        "title": title,
        "feed_id": "feed1",
        "feed_name": "测试源",
        "url": url,
        "published_at": "2026-08-28T00:00:00+08:00",
    }


class RssIsNewFlagTests(unittest.TestCase):
    def test_keyword_rss_stats_keep_current_crawl_new_flag(self):
        old_item = _rss_item("旧RSS标题", "https://example.com/old")
        new_item = _rss_item("新RSS标题", "https://example.com/new")
        stats, _ = count_rss_frequency(
            rss_items=[old_item, new_item],
            word_groups=[],
            filter_words=[],
            new_items=[new_item],
            quiet=True,
        )
        by_url = {
            item["url"]: item
            for stat in stats
            for item in stat.get("titles", [])
        }
        self.assertFalse(by_url[old_item["url"]]["is_new"])
        self.assertTrue(by_url[new_item["url"]]["is_new"])

    def test_is_new_keeps_ai_score_boost(self):
        old_item = {"title": "旧RSS标题", "ranks": [1], "count": 1, "is_new": False}
        new_item = dict(old_item)
        new_item["is_new"] = True
        self.assertEqual(_item_score(new_item), _item_score(old_item) + 25.0)


if __name__ == "__main__":
    unittest.main()
