"""Fetcher diagnostics retain source labels but never raw URLs or error bodies."""
import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import requests

from trendradar.crawler.fetcher import DataFetcher


PRIVATE_MARKER = "SYNTHETIC_PRIVATE_MARKER"


def response(payload):
    result = requests.Response()
    result.status_code = 200
    result._content = json.dumps(payload).encode("utf-8")
    return result


class FetcherPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.primary = "https://primary.invalid/api/s"
        self.fallback = "https://sample:" + PRIVATE_MARKER + "@fallback.invalid/private-path/api/s"

    def assert_private_values_absent(self, text):
        for value in (PRIVATE_MARKER, self.primary, self.fallback, "sample:", "private-path", "exception-body"):
            self.assertNotIn(value, text)

    def test_fallback_success_logs_only_source_label(self):
        for status, status_label in (("success", "最新数据"), ("cache", "缓存数据")):
            with self.subTest(status=status):
                fetcher = DataFetcher(api_url=self.primary, api_fallback_urls=[self.fallback])
                expected = response({"status": status, "items": []})
                output = io.StringIO()
                with patch("trendradar.crawler.fetcher.requests.get", side_effect=[requests.ConnectTimeout("synthetic timeout"), expected]) as get:
                    with patch("trendradar.crawler.fetcher.time.sleep") as sleep, redirect_stdout(output):
                        actual = fetcher.fetch_data(("synthetic", "合成来源"), max_retries=0)
                self.assertEqual(actual, (expected.text, "synthetic", "合成来源"))
                self.assertEqual(get.call_count, 2)
                # Sanitization is for logs only; configured request URLs are unchanged.
                self.assertEqual(get.call_args.args[0], self.fallback + "?id=synthetic&latest")
                self.assertEqual(get.call_args.kwargs["timeout"], (2, 5))
                sleep.assert_not_called()
                self.assertIn("fallback#1", output.getvalue())
                self.assertIn(status_label, output.getvalue())
                self.assert_private_values_absent(output.getvalue())

    def test_retry_switch_and_final_failure_omit_url_and_exception_body(self):
        fetcher = DataFetcher(api_url=self.primary, api_fallback_urls=[self.fallback])
        error = requests.ConnectTimeout("exception-body " + self.primary + " -> " + self.fallback)
        output = io.StringIO()
        with patch("trendradar.crawler.fetcher.requests.get", side_effect=error) as get:
            with patch("trendradar.crawler.fetcher.time.sleep") as sleep, redirect_stdout(output):
                actual = fetcher.fetch_data("synthetic")
        self.assertEqual(actual, (None, "synthetic", "synthetic"))
        self.assertEqual(get.call_count, 5)
        self.assertEqual(sleep.call_count, 3)
        self.assertTrue(all(call.kwargs["timeout"] == (2, 5) for call in get.call_args_list))
        self.assertIn("主源", output.getvalue())
        self.assertIn("fallback#1", output.getvalue())
        self.assertIn("ConnectTimeout", output.getvalue())
        self.assert_private_values_absent(output.getvalue())

    def test_subsequent_fallbacks_preserve_order_without_logging_urls(self):
        second_fallback = "https://another:" + PRIVATE_MARKER + "@second.invalid/private-path/api/s"
        fetcher = DataFetcher(api_url=self.primary, api_fallback_urls=[self.fallback, second_fallback])
        expected = response({"status": "success", "items": []})
        error = requests.exceptions.ProxyError
        output = io.StringIO()
        with patch("trendradar.crawler.fetcher.requests.get", side_effect=[error("exception-body " + self.fallback), error("exception-body " + second_fallback), expected]) as get:
            with patch("trendradar.crawler.fetcher.time.sleep") as sleep, redirect_stdout(output):
                actual = fetcher.fetch_data("synthetic", max_retries=0)
        self.assertEqual(actual[0], expected.text)
        self.assertEqual([call.args[0] for call in get.call_args_list], [url + "?id=synthetic&latest" for url in (self.primary, self.fallback, second_fallback)])
        self.assertIn("fallback#2", output.getvalue())
        self.assertIn("ProxyError", output.getvalue())
        self.assert_private_values_absent(output.getvalue())
        self.assertNotIn(second_fallback, output.getvalue())
        sleep.assert_not_called()

    def test_unexpected_status_body_is_not_logged(self):
        fetcher = DataFetcher(api_url=self.primary)
        output = io.StringIO()
        with patch("trendradar.crawler.fetcher.requests.get", return_value=response({"status": PRIVATE_MARKER})):
            with redirect_stdout(output):
                actual = fetcher.fetch_data("synthetic", max_retries=0)
        self.assertIsNone(actual[0])
        self.assertIn("ValueError", output.getvalue())
        self.assert_private_values_absent(output.getvalue())


if __name__ == "__main__":
    unittest.main()
