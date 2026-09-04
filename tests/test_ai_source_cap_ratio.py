# coding=utf-8
"""AI 配置与 source_cap_ratio 归一化回归测试。"""

import os
import tempfile
import unittest
from unittest.mock import patch

from trendradar.ai.selector import SOURCE_CAP_RATIO, _normalize_source_cap_ratio
from trendradar.core.loader import _load_ai_analysis_config, _load_ai_config


class SourceCapRatioConfigTests(unittest.TestCase):
    def test_missing_key_uses_default(self):
        config = _load_ai_analysis_config({"ai_analysis": {}})
        self.assertEqual(config["SOURCE_CAP_RATIO"], 0.30)

    def test_empty_yaml_value_uses_default(self):
        config = _load_ai_analysis_config({"ai_analysis": {"source_cap_ratio": None}})
        self.assertEqual(config["SOURCE_CAP_RATIO"], 0.30)

    def test_explicit_zero_is_preserved_by_loader(self):
        config = _load_ai_analysis_config({"ai_analysis": {"source_cap_ratio": 0}})
        self.assertEqual(config["SOURCE_CAP_RATIO"], 0)

    def test_numeric_string_is_preserved_by_loader(self):
        config = _load_ai_analysis_config({"ai_analysis": {"source_cap_ratio": "0.2"}})
        self.assertEqual(config["SOURCE_CAP_RATIO"], "0.2")

    def test_api_key_file_is_used_when_direct_key_is_unset(self):
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False) as handle:
            handle.write("file-secret\n")
            secret_path = handle.name
        try:
            with patch.dict(os.environ, {"AI_API_KEY_FILE": secret_path}, clear=True):
                self.assertEqual(_load_ai_config({"ai": {}})["API_KEY"], "file-secret")
        finally:
            os.unlink(secret_path)


class SourceCapRatioNormalizationTests(unittest.TestCase):
    def test_none_uses_default(self):
        self.assertEqual(_normalize_source_cap_ratio(None), SOURCE_CAP_RATIO)

    def test_empty_string_uses_default(self):
        self.assertEqual(_normalize_source_cap_ratio(""), SOURCE_CAP_RATIO)

    def test_non_numeric_uses_default(self):
        self.assertEqual(_normalize_source_cap_ratio("abc"), SOURCE_CAP_RATIO)

    def test_zero_is_raised_to_minimum(self):
        self.assertEqual(_normalize_source_cap_ratio(0), 0.01)

    def test_above_one_is_clamped(self):
        self.assertEqual(_normalize_source_cap_ratio(2.5), 1.0)

    def test_valid_ratio_is_kept(self):
        self.assertEqual(_normalize_source_cap_ratio(0.2), 0.2)


if __name__ == "__main__":
    unittest.main()
