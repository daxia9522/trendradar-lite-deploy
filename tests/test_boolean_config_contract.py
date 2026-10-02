"""Boolean contracts across literal files, deployment validation and YAML loading.

All files and credentials are synthetic; no services or network are required.
"""
import ast
from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import yaml

from deploy import configure
from deploy.backup_settings import load_backup_settings
from deploy.docker import docker_configure, runtime_config as runtime
from deploy.envfile import EnvDocument, atomic_write
from deploy.native_schedule import NativeSchedule
from trendradar.core.loader import load_config


ROOT = Path(__file__).resolve().parents[1]
BOOLEAN_PATHS = {
    "DEBUG": ("DEBUG",),
    "SORT_BY_POSITION_FIRST": ("SORT_BY_POSITION_FIRST",),
    "SCHEDULE_ENABLED": ("SCHEDULE", "enabled"),
    "AI_ANALYSIS_ENABLED": ("AI_ANALYSIS", "ENABLED"),
    "STORAGE_TXT_ENABLED": ("STORAGE", "FORMATS", "TXT"),
    "STORAGE_HTML_ENABLED": ("STORAGE", "FORMATS", "HTML"),
    "PULL_ENABLED": ("STORAGE", "PULL", "ENABLED"),
}
EXPLICIT = (("true", True), ("TRUE", True), (" TrUe ", True), ("\t1\t", True),
            ("false", False), ("FALSE", False), (" FaLsE ", False), (" 0 ", False))
EMPTY = (None, "", "  ", " \t ")
INVALID = ("yes", "no", "on", "off", "2", "-1", "true false", "tr ue", " truex ",
           "  synthetic-private-value  ")
# Spaces, dollars and shell-like text must survive boolean validation unchanged.
SECRET = "  synthetic-$literal-${NO_EXPANSION}-$(not-executed)  "
FORM = {"TZ": "UTC", "STORAGE_BACKEND": "local", "EMAIL_FROM": "sender@example.invalid",
        "EMAIL_TO": "reader@example.invalid", "EMAIL_PASSWORD": SECRET,
        "AI_MODEL": "openai/synthetic", "AI_API_KEY": SECRET}


class BooleanConfigContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "env"
        self.config = self.root / "config.yaml"

    def yaml_defaults(self, enabled):
        self.config.write_text(yaml.safe_dump({
            "advanced": {"debug": enabled},
            "report": {"sort_by_position_first": enabled},
            "schedule": {"enabled": enabled, "preset": "custom"},
            "ai_analysis": {"enabled": enabled},
            "storage": {"formats": {"txt": enabled, "html": enabled},
                        "pull": {"enabled": enabled}},
        }), encoding="utf-8")

    def assert_loaded(self, values, expected):
        output = io.StringIO()
        with patch.dict(os.environ, values, clear=True), redirect_stdout(output), redirect_stderr(output):
            loaded = load_config(str(self.config))
        self.assertNotIn(SECRET, output.getvalue())
        for key, path in BOOLEAN_PATHS.items():
            actual = loaded
            for component in path:
                actual = actual[component]
            self.assertIs(actual, expected, key)

    def test_matrix_covers_every_loader_boolean(self):
        tree = ast.parse((ROOT / "trendradar/core/loader.py").read_text())
        keys = {node.args[0].value for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_get_env_bool"}
        self.assertEqual(keys, set(BOOLEAN_PATHS))
        self.assertEqual(set(configure.APP_BOOLEAN_KEYS), keys)

    def test_external_file_parse_validate_snapshot_and_loader(self):
        for syntax in ("linux", "docker"):
            for default in (False, True):
                self.yaml_defaults(default)
                for literal, expected in (*EXPLICIT, *((value, default) for value in EMPTY)):
                    with self.subTest(syntax=syntax, default=default, literal=literal):
                        values = dict(FORM)
                        if literal is not None:
                            values.update({key: literal for key in BOOLEAN_PATHS})
                        # Render from an empty document so missing really means absent,
                        # rather than retaining an earlier assignment.
                        document = EnvDocument(self.root / "never-written", syntax)
                        content = document.render(values)
                        atomic_write(self.path, content)
                        parsed = runtime.parse_runtime_env(content)
                        self.assertEqual(parsed, values)
                        runtime.validate_runtime_values(parsed)
                        self.assertEqual(parsed, values)  # validation must not normalize secrets
                        base = {runtime.RUNTIME_ENV_KEY: str(self.path), "AI_API_KEY": "stale-secret",
                                **{key: str(not expected).lower() for key in BOOLEAN_PATHS}}
                        before = dict(os.environ)
                        snapshot = runtime.load_runtime_config(base)
                        self.assertEqual(dict(os.environ), before)
                        self.assertEqual(snapshot.env["AI_API_KEY"], SECRET)
                        for key in BOOLEAN_PATHS:
                            if literal is None:
                                self.assertNotIn(key, snapshot.env)
                            else:
                                self.assertEqual(snapshot.env[key], literal)
                        self.assert_loaded(dict(snapshot.env), expected)
                        self.assertNotIn(SECRET, repr(snapshot))

    def test_native_literal_parse_menu_validation_and_loader(self):
        for default in (False, True):
            self.yaml_defaults(default)
            for literal, expected in (*EXPLICIT, *((value, default) for value in EMPTY)):
                with self.subTest(default=default, literal=literal):
                    values = dict(FORM)
                    if literal is not None:
                        values.update({key: literal for key in BOOLEAN_PATHS})
                    content = EnvDocument(self.root / "never-written").render(values)
                    atomic_write(self.path, content)
                    parsed = EnvDocument(self.path).values
                    self.assertEqual(configure.validate(parsed), [])
                    self.assertEqual(parsed, values)
                    self.assert_loaded(parsed, expected)

    def test_runtime_invalid_nonempty_values_still_fail_without_echo(self):
        for syntax in ("linux", "docker"):
            for key in BOOLEAN_PATHS:
                for literal in INVALID:
                    with self.subTest(syntax=syntax, key=key, literal=literal):
                        values = {**FORM, key: literal}
                        content = EnvDocument(self.root / "never-written", syntax).render(values)
                        for validate in (lambda: runtime.validate_runtime_values(values),
                                         lambda: runtime.parse_runtime_env(content)):
                            output = io.StringIO()
                            with redirect_stdout(output), redirect_stderr(output):
                                with self.assertRaises(runtime.RuntimeConfigError) as raised:
                                    validate()
                            self.assertEqual(raised.exception.code, "value")
                            self.assertEqual(str(raised.exception), runtime.ERRORS["value"])
                            self.assertEqual(output.getvalue(), "")
                            self.assertNotIn("synthetic-private-value", str(raised.exception))
                            self.assertNotIn(SECRET, str(raised.exception))

    def test_control_characters_remain_rejected_before_trimming(self):
        for key in BOOLEAN_PATHS:
            for literal in ("\ntrue", "false\r", "\0", " \n "):
                with self.subTest(key=key, literal=literal):
                    with self.assertRaises(runtime.RuntimeConfigError) as raised:
                        runtime.validate_runtime_values({key: literal})
                    self.assertEqual(raised.exception.code, "value")
        for key in ("AI_ANALYSIS_ENABLED", "R2_BACKUP_ENABLED"):
            self.assertTrue(configure.validate_field(key, "\ntrue"))

    def test_menu_ai_switch_credentials_and_safe_diagnostics(self):
        no_ai_credentials = {key: value for key, value in FORM.items() if key not in ("AI_MODEL", "AI_API_KEY")}
        for deployment in ("linux", "docker"):
            for literal, enabled in (*EXPLICIT, *((value, False) for value in EMPTY if value is not None)):
                with self.subTest(deployment=deployment, literal=literal):
                    values = {**no_ai_credentials, "AI_ANALYSIS_ENABLED": literal}
                    errors = configure.validate(values, deployment)
                    self.assertEqual(bool(errors), enabled)
                    if enabled:
                        self.assertEqual(len(errors), 1)
                        self.assertIn("AI_MODEL", errors[0])
                    self.assertEqual(configure.validate({**FORM, "AI_ANALYSIS_ENABLED": literal}, deployment), [])
            for literal in INVALID:
                errors = configure.validate({**FORM, "AI_ANALYSIS_ENABLED": literal}, deployment)
                self.assertTrue(errors)
                self.assertNotIn(literal.strip(), "\n".join(errors))
                self.assertNotIn(SECRET, "\n".join(errors))

    def test_menu_backup_switch_keeps_disabled_default_and_safety_gates(self):
        credentials = {"STORAGE_BACKEND": "local", "S3_BUCKET_NAME": "synthetic",
                       "S3_ENDPOINT_URL": "https://s3.example.invalid", "S3_ACCESS_KEY_ID": SECRET,
                       "S3_SECRET_ACCESS_KEY": SECRET}
        for literal, enabled in (*EXPLICIT, *((value, False) for value in EMPTY if value is not None)):
            with self.subTest(literal=literal):
                values = {**FORM, **credentials, "R2_BACKUP_ENABLED": literal}
                self.assertEqual(configure.validate(values), [])
                self.assertEqual(configure.validate(values, "docker"), [])
                self.assertIs(load_backup_settings(values).enabled, enabled)
                self.assertIs(runtime.schedule_settings(runtime.parse_runtime_env(
                    EnvDocument(self.root / "never-written").render(values))).backup_enabled, enabled)
                if enabled:
                    self.assertTrue(configure.validate({**FORM, "R2_BACKUP_ENABLED": literal}))
        for literal in INVALID:
            self.assertTrue(configure.validate_field("R2_BACKUP_ENABLED", literal))

    def test_docker_menu_saves_existing_padded_switches_without_rewriting_secrets(self):
        self.yaml_defaults(False)
        for syntax in ("linux", "docker"):
            with self.subTest(syntax=syntax):
                values = {**FORM, **{key: " TRUE " for key in BOOLEAN_PATHS}}
                content = EnvDocument(self.root / "never-written", syntax).render(values)
                atomic_write(self.path, content)
                app = docker_configure.DockerApplication(self.path)
                app.save(app.values)
                self.assertEqual(self.path.read_bytes(), content)
                self.assert_loaded(dict(runtime.load_runtime_config({runtime.RUNTIME_ENV_KEY: str(self.path)}).env), True)

    def test_menu_display_uses_normalized_switches_without_echoing_invalid_text(self):
        for key in (*BOOLEAN_PATHS, "R2_BACKUP_ENABLED"):
            for literal, enabled in EXPLICIT:
                self.assertEqual(configure.display_value(key, literal), "已启用" if enabled else "已停用")
            self.assertEqual(configure.display_value(key, " \t "), configure.display_value(key, ""))
            for literal in INVALID:
                self.assertNotIn(literal.strip(), configure.display_value(key, literal))

    def test_menu_validates_hidden_app_switches_and_redacts_error_pages(self):
        for key in BOOLEAN_PATHS:
            values = {**FORM, key: "synthetic-private-value"}
            for deployment in ("linux", "docker"):
                errors = configure.validate(values, deployment)
                self.assertTrue(errors)
                self.assertTrue(any(key in error for error in errors))
                page = configure.render(values, errors, deployment=deployment)
                self.assertNotIn("synthetic-private-value", page)
                self.assertNotIn(SECRET, page)
            page = docker_configure.render(values, docker_configure.validate(values))
            self.assertNotIn("synthetic-private-value", page)
            self.assertNotIn(SECRET, page)

    def test_native_save_validates_merged_booleans_before_writes_or_commands(self):
        from native_config import NativeApplication
        runner = Mock(side_effect=AssertionError("No native service operations"))
        # No full email validation here: partial native saves remain supported.
        for literal, _expected in (*EXPLICIT, *((value, False) for value in EMPTY if value is not None)):
            with self.subTest(literal=literal):
                values = {key: literal for key in BOOLEAN_PATHS}
                atomic_write(self.path, EnvDocument(self.root / "never-written").render(values))
                app = NativeApplication(self.path, app_dir=ROOT, unit_dir=self.root / "units",
                                        install=True, runner=runner)
                app.save({"EMAIL_TO": "new@example.invalid"})
                parsed = EnvDocument(self.path).values
                for key in BOOLEAN_PATHS:
                    self.assertEqual(parsed[key], literal)
        for key in BOOLEAN_PATHS:
            for inherited in (False, True):
                values = {key: "synthetic-private-value"} if inherited else {key: "false"}
                atomic_write(self.path, EnvDocument(self.root / "never-written").render(values))
                app = NativeApplication(self.path, app_dir=ROOT, unit_dir=self.root / "units",
                                        install=True, runner=runner)
                before = self.path.read_bytes()
                update = {"EMAIL_TO": "new@example.invalid"} if inherited else {key: "synthetic-private-value"}
                with self.assertRaises(ValueError) as raised:
                    app.save(update)
                self.assertIn(key, str(raised.exception))
                self.assertNotIn("synthetic-private-value", str(raised.exception))
                self.assertEqual(self.path.read_bytes(), before)
        runner.assert_not_called()

    def test_interactive_switch_aliases_do_not_widen_env_literals(self):
        for entered, expected in ((" 0 ", "0"), ("1", "true"), ("2", "false"), (":clear", "")):
            values = {"AI_ANALYSIS_ENABLED": "true"}
            with patch.object(configure, "input", return_value=entered, create=True):
                configure.edit_native_field("AI_ANALYSIS_ENABLED", values)
            self.assertEqual(values["AI_ANALYSIS_ENABLED"], expected)
        self.assertTrue(configure.validate_field("AI_ANALYSIS_ENABLED", "2"))

    def test_legacy_terminal_prompt_hides_invalid_boolean_without_clearing_it(self):
        for key in ("AI_ANALYSIS_ENABLED", "R2_BACKUP_ENABLED"):
            prompts = []
            def entered(prompt):
                prompts.append(prompt)
                return ""
            with patch.object(configure, "input", side_effect=entered, create=True):
                result = configure._prompt_field(key, "开关", False, "", {key: "synthetic-private-boolean"})
            self.assertEqual(result, "synthetic-private-boolean")
            self.assertNotIn("synthetic-private-boolean", "\n".join(prompts))
            self.assertIn("无效布尔值", "\n".join(prompts))

    def test_native_schedule_matches_loader_and_retains_custom_schedule_guard(self):
        app = self.root / "native"
        config_dir = app / "config"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "config.yaml"
        source = (ROOT / "config/config.yaml").read_text()
        self.assertIn("schedule:\n  enabled: true", source)
        (config_dir / "timeline.yaml").write_bytes((ROOT / "config/timeline.yaml").read_bytes())
        runner = Mock(side_effect=AssertionError("No commands during inspection"))
        for default in (False, True):
            config_path.write_text(source.replace("schedule:\n  enabled: true",
                                                  f"schedule:\n  enabled: {str(default).lower()}"))
            for literal, expected in (*EXPLICIT, *((value, default) for value in EMPTY)):
                with self.subTest(default=default, literal=literal):
                    values = {} if literal is None else {"SCHEDULE_ENABLED": literal}
                    atomic_write(self.path, EnvDocument(self.root / "never-written").render(values))
                    parsed = EnvDocument(self.path).values
                    schedule = NativeSchedule(app, self.root / "units", parsed, runner=runner)
                    self.assertIs(schedule.supported, expected)
                    with patch.dict(os.environ, parsed, clear=True), redirect_stdout(io.StringIO()):
                        self.assertIs(load_config(str(config_path))["SCHEDULE"]["enabled"], expected)
            for literal in INVALID:
                schedule = NativeSchedule(app, self.root / "units", {"SCHEDULE_ENABLED": literal}, runner=runner)
                self.assertFalse(schedule.supported)
                self.assertNotIn(literal.strip(), "\n".join(schedule.warnings))
        runner.assert_not_called()
