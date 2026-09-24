"""User-visible terminal menu contracts; no file writes, servers or systemd."""
import contextlib
import importlib.util
import io
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("presentation_configure", ROOT / "deploy/configure.py")
configure = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(configure)


class NativePresentationTests(unittest.TestCase):
    def test_pending_marker_and_config_path_are_visible(self):
        path = Path("/synthetic/not-real/env")
        app = SimpleNamespace(
            document=SimpleNamespace(path=path, values={"AI_MODEL": "openai/old"}, original=True),
            schedule=SimpleNamespace(warnings=[]),
        )
        transcript = io.StringIO()
        answers = ["2", "2", "openai/new", "0", "5", "q", "y"]
        with mock.patch.object(configure, "input", side_effect=answers, create=True), contextlib.redirect_stdout(transcript):
            self.assertFalse(configure.configure_terminal(path, application=app))
        text = transcript.getvalue()
        self.assertIn("配置文件：/synthetic/not-real/env", text)
        self.assertIn("openai/new [待保存]", text)
        self.assertIn("openai/old → openai/new", text)

    def test_secret_diff_names_the_operation_without_disclosing_values(self):
        for before, after, expected in (("oldsecret", "newsecret", "将替换"), ("", "newsecret", "将新增"), ("oldsecret", "", "将清空")):
            with self.subTest(operation=expected):
                transcript = io.StringIO()
                with contextlib.redirect_stdout(transcript):
                    configure.print_changes({"AI_API_KEY": before}, {"AI_API_KEY": after})
                self.assertIn(expected, transcript.getvalue())
                self.assertNotIn("oldsecret", transcript.getvalue())
                self.assertNotIn("newsecret", transcript.getvalue())

    def test_switch_weekday_and_advanced_defaults_are_readable(self):
        self.assertEqual(configure.display_value("AI_ANALYSIS_ENABLED", "true"), "已启用")
        self.assertEqual(configure.display_value("AI_ANALYSIS_ENABLED", "false"), "已停用")
        self.assertIn("周日", configure.display_value("WEEKLY_WEEKDAY", "6"))
        self.assertEqual(configure.display_value("AI_TIMEOUT", ""), "<使用程序配置>")

    def test_url_validation_and_masking_never_echo_secret_input(self):
        for key in ("AI_API_BASE", "PLATFORMS_API_URL", "PLATFORMS_API_FALLBACK_URLS"):
            for value in ("https://[bad?token=secretvalue", "ftp://example.com?token=secretvalue", "https://example.com:99999?token=secretvalue"):
                errors = configure.validate_field(key, value)
                self.assertTrue(errors)
                self.assertNotIn("secretvalue", " ".join(errors))
        self.assertEqual(configure.validate_field("PLATFORMS_API_FALLBACK_URLS", "https://a.example/v1, https://b.example/v1"), [])
        self.assertNotIn("secretvalue", configure.display_value("AI_API_BASE", "https://[bad?token=secretvalue"))
        self.assertNotIn("secretvalue", configure.display_value("AI_API_BASE", "https://example.com?token=secretvalue"))


if __name__ == "__main__":
    unittest.main()
