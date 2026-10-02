"""Environment boolean overrides must not silently turn typos into False."""
import os
import unittest
from unittest.mock import patch

from trendradar.core.loader import _get_env_bool, load_config


class EnvironmentBooleanTests(unittest.TestCase):
    def test_supported_values_and_whitespace(self):
        for value, expected in (("true", True), ("1", True), (" TRUE ", True),
                                ("false", False), ("0", False), (" False ", False)):
            with self.subTest(value=value), patch.dict(os.environ, {"SCHEDULE_ENABLED": value}, clear=True):
                self.assertIs(_get_env_bool("SCHEDULE_ENABLED"), expected)

    def test_missing_and_empty_values_keep_yaml_defaults(self):
        for values in ({}, {"SCHEDULE_ENABLED": ""}, {"SCHEDULE_ENABLED": "  "}):
            with self.subTest(values=values), patch.dict(os.environ, values, clear=True):
                self.assertIsNone(_get_env_bool("SCHEDULE_ENABLED"))
                self.assertTrue(load_config("config/config.yaml")["SCHEDULE"]["enabled"])

    def test_every_boolean_override_rejects_invalid_values_without_echoing_them(self):
        keys = ("DEBUG", "SORT_BY_POSITION_FIRST", "SCHEDULE_ENABLED", "AI_ANALYSIS_ENABLED",
                "STORAGE_TXT_ENABLED", "STORAGE_HTML_ENABLED", "PULL_ENABLED")
        for key in keys:
            with self.subTest(key=key), patch.dict(os.environ, {key: "synthetic-private-value"}, clear=True):
                with self.assertRaisesRegex(ValueError, key) as error:
                    load_config("config/config.yaml")
                self.assertNotIn("synthetic-private-value", str(error.exception))

    def test_false_is_an_explicit_override_not_missing(self):
        with patch.dict(os.environ, {"SCHEDULE_ENABLED": "false"}, clear=True):
            self.assertIs(load_config("config/config.yaml")["SCHEDULE"]["enabled"], False)
