"""Invalid feed timestamps degrade safely; programming errors must propagate."""
from datetime import datetime
import unittest
from unittest.mock import Mock

from trendradar.report.daily import render_report_body


class ReportTimestampTests(unittest.TestCase):
    def render(self, timestamp, *, parse_timestamp=datetime.fromisoformat):
        return render_report_body(
            {"failed_ids": [], "stats": [], "new_titles": [], "total_new_count": 0},
            region_order=["standalone"],
            standalone_data={"rss_feeds": [{"name": "Synthetic feed", "items": [
                {"title": "Preserved title", "published_at": timestamp},
            ]}]},
            parse_timestamp=parse_timestamp,
        )

    def test_valid_and_plain_timestamps(self):
        for timestamp, expected in (("2026-10-01T07:30:00Z", "10-01 07:30"),
                                    ("2026-10-01T07:30:00+08:00", "10-01 07:30"),
                                    ("2026-10-01", "2026-10-01"), (123, "123")):
            with self.subTest(timestamp=timestamp):
                self.assertIn(f'<span class="time-info">{expected}</span>', self.render(timestamp))

    def test_bad_timestamp_is_escaped_not_dropped(self):
        rendered = self.render('badTdate<script>')
        self.assertIn("badTdate&lt;script&gt;", rendered)
        self.assertIn("Preserved title", rendered)
        self.assertNotIn("<script>", rendered)

    def test_no_timestamp_omits_time_label(self):
        self.assertNotIn('class="time-info"', self.render(None))

    def test_unexpected_errors_and_interrupts_are_not_swallowed(self):
        for error_type in (RuntimeError, KeyboardInterrupt, SystemExit):
            with self.subTest(error_type=error_type):
                parser = Mock(side_effect=error_type("synthetic failure"))
                with self.assertRaises(error_type):
                    self.render("2026-10-01T07:30:00Z", parse_timestamp=parser)
                parser.assert_called_once_with("2026-10-01T07:30:00+00:00")
