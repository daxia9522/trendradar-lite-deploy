import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call

from trendradar.daily_flow.runner import DailyRunner
from trendradar.storage.base import RSSItem


class RSSFreshnessTests(unittest.TestCase):
    def test_old_rss_is_excluded_from_push_but_input_is_preserved(self):
        analyzer = DailyRunner.__new__(DailyRunner)
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

        freshness_check = Mock(
            side_effect=lambda published_at, *_: published_at == fresh.published_at,
        )
        push_items = analyzer._convert_rss_items_to_list(
            stored_items, {"feed": "Example Feed"}, within_days=freshness_check,
        )

        self.assertEqual([item["title"] for item in push_items], ["Fresh article"])
        self.assertEqual(len(stored_items["feed"]), 2)
        self.assertEqual(freshness_check.call_args_list, [
            call(fresh.published_at, 3, "Asia/Shanghai"),
            call(old.published_at, 3, "Asia/Shanghai"),
        ])

    def test_feed_can_disable_freshness_filter(self):
        analyzer = DailyRunner.__new__(DailyRunner)
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

        freshness_check = Mock(side_effect=AssertionError("archive bypass must not check freshness"))
        result = analyzer._convert_rss_items_to_list(
            {"archive": [old]}, {"archive": "Archive"}, within_days=freshness_check,
        )

        self.assertEqual([item["title"] for item in result], ["Archived article"])
        freshness_check.assert_not_called()
