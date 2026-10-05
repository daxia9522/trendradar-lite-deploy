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

    def test_openai_fallback_key_is_secret_even_without_a_menu_field(self):
        key = "AI_FALLBACK_API_KEY"
        old, new = "synthetic-old-fallback-key", "synthetic-new-fallback-key"
        self.assertNotIn(new, configure.display_value(key, new))
        for before, after, operation in (("", new, "将新增"), (old, new, "将替换"), (old, "", "将清空")):
            with self.subTest(operation=operation):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    configure.print_changes({key: before}, {key: after})
                self.assertIn(operation, output.getvalue())
                self.assertNotIn(old, output.getvalue())
                self.assertNotIn(new, output.getvalue())

    def test_openai_fallback_base_uses_existing_url_redaction_rules(self):
        key = "AI_FALLBACK_API_BASE"
        for value in ("https://synthetic-user@relay.example.invalid/v1",
                      "https://relay.example.invalid/v1?token=synthetic-query"):
            with self.subTest(value=value):
                self.assertTrue(configure.url_field_never_renders(key, value))
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    configure.print_changes({key: ""}, {key: value})
                self.assertNotIn(value, output.getvalue())
                self.assertNotIn("synthetic-", output.getvalue())
        self.assertEqual(configure.validate_field(key, "https://relay.example.invalid/v1"), [])
        self.assertTrue(configure.validate_field(key, "ftp://relay.example.invalid/v1"))

    def test_only_new_list_fields_are_exposed(self):
        self.assertTrue(set(configure.FALLBACK_LIST_FIELDS).issubset(configure.MENU_SECTIONS["2"][1]))
        self.assertFalse(any(key.startswith("AI_OPENAI_") for key in configure.FIELD_MAP))
        for phrase in ("英文逗号", "@", "保留空位", "单值内不能包含 @", "不支持转义", "留空保持", ":clear"):
            self.assertIn(phrase, configure.FALLBACK_LIST_HELP)

    def test_entire_fallback_key_column_is_hidden_in_preview(self):
        key = "AI_FALLBACK_API_KEY"
        before, after = "@synthetic-old-column@", "synthetic-new-first@@synthetic-new-last"
        self.assertIn(key, configure.SECRET_FIELDS)
        self.assertEqual(configure.display_value(key, after), "<已设置，隐藏>")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            configure.print_changes({key: before}, {key: after})
        self.assertIn("将替换", output.getvalue())
        self.assertNotIn("synthetic-", output.getvalue())

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
