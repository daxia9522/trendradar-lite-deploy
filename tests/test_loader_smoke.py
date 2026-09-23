# coding=utf-8
"""Smoke test: load_config() must run end-to-end on the shipped config.

The 2026-09-23 shadow-removal incident deleted three loader functions that no
other test executed, crashing every production run with NameError while the
suite stayed green. This test closes that gap: it calls the real loader and
asserts the sections downstream code depends on are present.
"""
import unittest
from pathlib import Path

from trendradar.core.loader import load_config

CONFIG = Path(__file__).resolve().parent.parent / "config" / "config.yaml"


class LoaderSmokeTests(unittest.TestCase):
    def test_load_config_runs_and_returns_required_sections(self):
        config = load_config(str(CONFIG))
        # 全路径必须无 NameError 跑完，且关键段落齐备
        for key in ("AI", "AI_ANALYSIS", "PLATFORMS", "STORAGE", "SCHEDULE", "REPORT_MODE"):
            self.assertIn(key, config, f"missing config key: {key}")
        for key in ("EMAIL_FROM", "EMAIL_PASSWORD", "EMAIL_TO"):
            self.assertIn(key, config, f"missing email key: {key}")
        self.assertIn("BACKEND", config["STORAGE"])
        self.assertIn("REMOTE", config["STORAGE"])


if __name__ == "__main__":
    unittest.main()
