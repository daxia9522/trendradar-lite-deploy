import tempfile
import unittest
from pathlib import Path

from trendradar.report.generator import generate_html_report


class ReportGenerationTests(unittest.TestCase):
    def test_report_writes_snapshot_latest_and_email_entry(self):
        stats = [{
            "word": "AI",
            "count": 1,
            "titles": [{
                "title": "OpenAI 发布新模型",
                "source_name": "微博",
                "time_display": "12:00",
                "count": 1,
                "ranks": [1],
                "rank_threshold": 3,
                "matched_keyword": "AI",
            }],
        }]

        def render(report_data, mode):
            self.assertEqual(mode, "daily")
            self.assertEqual(report_data["stats"][0]["titles"][0]["title"], "OpenAI 发布新模型")
            return "<html><body>OpenAI 发布新模型</body></html>"

        with tempfile.TemporaryDirectory() as temp_dir:
            snapshot = Path(generate_html_report(
                stats,
                mode="daily",
                output_dir=temp_dir,
                date_folder="2026-08-16",
                time_filename="12-30",
                render_email_html_func=render,
            ))
            latest = Path(temp_dir) / "html" / "latest" / "daily.html"
            email_entry = Path(temp_dir) / "email.html"

            self.assertTrue(snapshot.is_file())
            self.assertTrue(latest.is_file())
            self.assertTrue(email_entry.is_file())
            self.assertEqual(snapshot.read_text(encoding="utf-8"), latest.read_text(encoding="utf-8"))
            self.assertIn("OpenAI 发布新模型", email_entry.read_text(encoding="utf-8"))
