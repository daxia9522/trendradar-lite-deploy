"""Report URLs degrade to escaped titles instead of aborting the HTML report."""
import unittest

from trendradar.report import html as report_html
from trendradar.report.helpers import safe_report_url


class ReportUrlEdgeTests(unittest.TestCase):
    def test_malformed_urls_are_rejected_without_raising(self):
        for url in (
            "https://[invalid",
            "https://[not-an-ip]/news",
            "https://example.invalid\uff1a443/news",
        ):
            with self.subTest(url=url):
                self.assertIsNone(safe_report_url(url))

    def test_valid_urls_and_existing_scheme_restrictions_are_preserved(self):
        for url in (
            "https://example.invalid/news?a=1&b=2",
            "http://example.invalid/news",
            "https://[2001:db8::1]/news",
        ):
            with self.subTest(url=url):
                self.assertEqual(safe_report_url(" " + url + " "), url)
        for value in (None, 123, b"https://example.invalid", "", "//example.invalid", "javascript:alert(1)"):
            with self.subTest(value=value):
                self.assertIsNone(safe_report_url(value))

    def test_malformed_link_preserves_title_and_rest_of_report(self):
        report = {
            "failed_ids": [],
            "new_titles": [],
            "total_new_count": 0,
            "stats": [{
                "word": "合成新闻",
                "count": 2,
                "titles": [
                    {"title": "坏链接 <b>仍保留</b>", "source_name": "合成来源", "url": "https://[invalid", "ranks": [1]},
                    {"title": "正常链接", "source_name": "合成来源", "url": "https://example.invalid/news", "ranks": [2]},
                ],
            }],
        }
        rendered = report_html._render_report_body(report, region_order=["hotlist"])
        self.assertIn("坏链接 &lt;b&gt;仍保留&lt;/b&gt;", rendered)
        self.assertNotIn("https://[invalid", rendered)
        self.assertNotIn("<b>仍保留</b>", rendered)
        self.assertEqual(rendered.count('<a href="'), 1)
        self.assertIn('<a href="https://example.invalid/news"', rendered)
        self.assertIn("正常链接</a>", rendered)

    def test_malformed_mobile_link_also_degrades_to_plain_title(self):
        report = {
            "failed_ids": [],
            "new_titles": [{
                "source_name": "合成来源",
                "titles": [{"title": "新增标题", "mobile_url": "https://[invalid", "url": "https://example.invalid/news", "ranks": [1]}],
            }],
            "total_new_count": 1,
            "stats": [],
        }
        rendered = report_html._render_report_body(report, region_order=["new_items"])
        self.assertIn("新增标题", rendered)
        self.assertNotIn('<a href="', rendered)
        self.assertNotIn("https://[invalid", rendered)


if __name__ == "__main__":
    unittest.main()
