import os
import unittest
from unittest.mock import patch

from trendradar.core.loader import load_config


class ConfigContractTests(unittest.TestCase):
    def test_deployments_share_one_config_contract(self):
        with patch.dict(
            os.environ,
            {"STORAGE_BACKEND": "local", "AI_ANALYSIS_ENABLED": "true"},
            clear=False,
        ):
            config = load_config("config/config.yaml")

        self.assertEqual(config["STORAGE"]["BACKEND"], "local")
        self.assertTrue(config["AI_ANALYSIS"]["ENABLED"])
        self.assertIn("RSS", config)
        self.assertNotIn("HOTLIST_FRESHNESS_FILTER", config)

    def test_rss_freshness_remains_enabled(self):
        config = load_config("config/config.yaml")
        self.assertTrue(config["RSS"]["FRESHNESS_FILTER"]["ENABLED"])
        self.assertEqual(config["RSS"]["FRESHNESS_FILTER"]["MAX_AGE_DAYS"], 3)
