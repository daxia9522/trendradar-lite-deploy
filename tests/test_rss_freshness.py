import unittest
from types import SimpleNamespace
from unittest.mock import patch

from trendradar.daily import NewsAnalyzer
from trendradar.storage.base import RSSItem


class RSSFreshnessTests(unittest.TestCase):
    def test_old_rss_is_excluded_from_push_but_input_is_preserved(self):
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.ctx = SimpleNamespace(
            rss_config={
                "FRESHNESS_FILTER": {"ENABLED": True, "MAX_AGE_DAYS": 3}
            },
            rss_feeds=[],
            config={"TIMEZONE": "Asia/Shanghai", "DEBUG": False},
        )
        fresh = RSSItem(
            title="Fresh article",
            feed_id="feed",
            published_at="2026-08-15T12:00:00+08:00",
        )
        old = RSSItem(
            title="Old article",
            feed_id="feed",
            published_at="2026-08-01T12:00:00+08:00",
        )
        stored_items = {"feed": [fresh, old]}

        with patch(
            "trendradar.daily.is_within_days",
            side_effect=lambda published_at, *_: published_at == fresh.published_at,
        ):
            push_items = analyzer._convert_rss_items_to_list(
                stored_items, {"feed": "Example Feed"}
            )

        self.assertEqual([item["title"] for item in push_items], ["Fresh article"])
        self.assertEqual(len(stored_items["feed"]), 2)

    def test_feed_can_disable_freshness_filter(self):
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.ctx = SimpleNamespace(
            rss_config={
                "FRESHNESS_FILTER": {"ENABLED": True, "MAX_AGE_DAYS": 3}
            },
            rss_feeds=[{"id": "archive", "max_age_days": 0}],
            config={"TIMEZONE": "Asia/Shanghai", "DEBUG": False},
        )
        old = RSSItem(
            title="Archived article",
            feed_id="archive",
            published_at="2020-01-01T00:00:00+08:00",
        )

        with patch("trendradar.daily.is_within_days") as freshness_check:
            result = analyzer._convert_rss_items_to_list(
                {"archive": [old]}, {"archive": "Archive"}
            )

        self.assertEqual([item["title"] for item in result], ["Archived article"])
        freshness_check.assert_not_called()
