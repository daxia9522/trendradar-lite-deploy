"""Structural and smoke checks for the split weekly report implementation."""
import ast
import importlib
import inspect
import typing
import subprocess
import sys
import unittest
from pathlib import Path

from trendradar.report.weekly import render_weekly_html
from weekly_report import keywords, prompting

ROOT = Path(__file__).resolve().parents[1]


class WeeklyRefactorTests(unittest.TestCase):
    def test_split_modules_have_resolvable_types_and_unique_definitions(self):
        for name in ("weekly_report.collection", "weekly_report.prompting", "weekly_report.keywords",
                     "trendradar.report.weekly", "weekly_report.runtime"):
            module = importlib.import_module(name)
            for function in vars(module).values():
                if inspect.isfunction(function) and function.__module__ == module.__name__:
                    typing.get_type_hints(function)
            definitions = [node.name for node in ast.parse(Path(module.__file__).read_text()).body
                           if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
            self.assertEqual(len(definitions), len(set(definitions)))

    def test_entrypoint_help_smoke(self):
        result = subprocess.run(
            [sys.executable, "weekly_report/weekly_ai_report_email.py", "--help"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--dry-run", result.stdout)

    def test_data_and_presentation_paths_are_local_and_deterministic(self):
        items = [
            {
                "title": "日本发生地震",
                "source_type": "news",
                "platforms": ["热榜"],
                "dates": ["2026-09-30"],
                "count": 1,
                "score": 10.0,
                "ranks": [1],
            }
        ]
        evidence = prompting.build_evidence_index(items)
        report, themes = keywords.parse_structured_report(
            "<THEMES_JSON>[]</THEMES_JSON><REPORT_MARKDOWN># 正文</REPORT_MARKDOWN>",
            evidence,
        )
        self.assertEqual(report, "# 正文")
        self.assertEqual(themes, [])
        rendered = render_weekly_html(
            "测试周报",
            "2026-09-30 ~ 2026-09-30",
            "synthetic/model",
            {"Top5关键词": "日本地震"},
            report,
            generated_at="2026-09-30 07:30:00",
        )
        self.assertIn("测试周报", rendered)
        self.assertIn("日本地震", rendered)
        self.assertIn("<h1>正文</h1>", rendered)


if __name__ == "__main__":
    unittest.main()
