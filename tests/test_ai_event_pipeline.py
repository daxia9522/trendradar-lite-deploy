import unittest

from trendradar.ai.selector import select_ai_news


class UnifiedEventPipelineTests(unittest.TestCase):
    def test_hotlist_and_rss_merge_into_one_event(self):
        hotlist = [{
            "word": "AI",
            "titles": [{
                "title": "OpenAI 发布新模型",
                "source_name": "微博",
                "ranks": [1],
            }],
        }]
        rss = [{
            "word": "AI",
            "titles": [{
                "title": "OpenAI 发布全新模型",
                "feed_name": "BBC",
                "time_display": "08-15 12:00",
            }],
        }]

        result = select_ai_news(hotlist, rss, total_limit=10)

        self.assertEqual(result.selected_count, 1)
        self.assertEqual(result.clusters[0].sources, {"微博", "BBC"})
        self.assertFalse(hasattr(result, "selected_hotlist"))
        self.assertFalse(hasattr(result, "selected_rss"))
