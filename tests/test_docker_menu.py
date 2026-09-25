"""Grouped Docker setup regression tests: temporary files, no Docker/network/tasks."""
import contextlib
import importlib.util
import io
import os
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("docker_setup_tests", ROOT / "deploy/docker/docker_configure.py")
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)
from envfile import ConfigError, EnvDocument, read_env, write_env

VALID = {"EMAIL_FROM": "sender@example.com", "EMAIL_TO": "reader@example.com",
         "EMAIL_PASSWORD": "smtp $literal '$$' secret", "AI_API_KEY": "api-secret",
         "TZ": "UTC", "AI_ANALYSIS_ENABLED": "false", "WEEKLY_HOUR": "12", "WEEKLY_MINUTE": "30"}


class DockerMenuTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "runtime/env"

    def application(self, values=VALID):
        write_env(self.path, values)
        return setup.DockerApplication(self.path)

    def menu(self, app, answers, secrets=()):
        transcript = io.StringIO()
        with mock.patch("builtins.input", side_effect=answers), mock.patch.object(setup.shared.getpass, "getpass", side_effect=secrets), contextlib.redirect_stdout(transcript):
            result = setup.configure_terminal(app)
        return result, transcript.getvalue()

    def test_single_field_edit_preview_save_retains_other_fields_and_literal_dollars(self):
        app = self.application()
        result, text = self.menu(app, ["2", "2", "openai/new", "0", "5", "s", "y"])
        self.assertTrue(result)
        loaded = read_env(self.path)
        self.assertEqual(loaded, dict(VALID, STORAGE_BACKEND="local", AI_MODEL="openai/new"))
        self.assertIn("openai/new [待保存]", text)
        self.assertIn("待保存变更", text)
        self.assertNotIn(VALID["EMAIL_PASSWORD"], text)
        self.assertNotIn(VALID["AI_API_KEY"], text)
        self.assertNotIn("AI API Key 文件", text)
        self.assertFalse(self.path.read_bytes().startswith(setup.DOCKER_MARKER))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        backups = list((self.path.parent / ".env.backups").iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)

    def test_secret_input_clear_and_weekly_unified_time(self):
        app = self.application()
        result, text = self.menu(app, ["2", "3", "0", "3", "7", "18:45", "0", "s", "y"], [":clear"])
        self.assertTrue(result)
        loaded = read_env(self.path)
        self.assertEqual(loaded["AI_API_KEY"], "")
        self.assertEqual((loaded["WEEKLY_HOUR"], loaded["WEEKLY_MINUTE"]), ("18", "45"))
        self.assertNotIn("WEEKLY_TIME", loaded)
        self.assertIn("将清空", text)
        self.assertNotIn("api-secret", text)

    def test_invalid_field_does_not_overwrite_until_valid(self):
        app = self.application()
        result, text = self.menu(app, ["3", "7", "25:72", "09:01", "0", "s", "y"])
        self.assertTrue(result)
        self.assertIn("HH:MM", text)
        self.assertEqual(read_env(self.path)["WEEKLY_HOUR"], "9")

    def test_cancel_keeps_bytes_and_does_not_create_backup(self):
        app = self.application()
        before = self.path.read_bytes()
        result, text = self.menu(app, ["2", "2", "openai/cancel", "0", "q", "y"])
        self.assertFalse(result)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((self.path.parent / ".env.backups").exists())
        self.assertIn("已取消", text)

    def test_defaults_are_only_in_memory_on_cancel(self):
        defaults = self.root / ".env.example"
        defaults.write_bytes((ROOT / ".env.example").read_bytes())
        app = setup.DockerApplication(self.path, defaults=defaults)
        self.assertFalse(self.path.exists())
        result, _ = self.menu(app, ["q", "y"])
        self.assertFalse(result)
        self.assertFalse(self.path.exists())

    def test_existing_docker_marker_keeps_correct_literal_secret(self):
        write_env(self.path, VALID, "docker")
        app = setup.DockerApplication(self.path)
        app.save(dict(app.values, EMAIL_TO="other@example.com"))
        self.assertEqual(read_env(self.path)["EMAIL_PASSWORD"], VALID["EMAIL_PASSWORD"])
        self.assertTrue(self.path.read_bytes().startswith(setup.DOCKER_MARKER))

    def test_existing_runtime_is_authoritative_and_never_migrated_over(self):
        self.application()
        before = self.path.read_bytes()
        (self.root / ".env").write_text('EMAIL_PASSWORD="${UNSUPPORTED}"\n')
        app = setup.DockerApplication(self.path, legacy=self.root / ".env")
        self.assertIsNone(app.legacy_document)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((self.root / ".env.backups").exists())

    def test_legacy_migration_private_backup_and_no_deployment_parameters(self):
        legacy = self.root / ".env"
        write_env(legacy, dict(VALID, TREND_RADAR_IMAGE="my-image", SETUP_PORT="8765", COMPOSE_PROJECT_NAME="unchanged"), "docker")
        before = legacy.read_bytes()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            app = setup.DockerApplication(self.path, legacy=legacy)
            self.assertFalse(self.path.exists())
            self.assertFalse((self.root / ".env.backups").exists())
            saved, _ = self.menu(app, ["s", "y"])
            self.assertTrue(saved)
        values = read_env(self.path)
        self.assertEqual(values["EMAIL_PASSWORD"], VALID["EMAIL_PASSWORD"])
        self.assertNotIn("TREND_RADAR_IMAGE", values)
        self.assertNotIn("SETUP_PORT", values)
        self.assertNotIn("COMPOSE_PROJECT_NAME", values)
        self.assertEqual(legacy.read_bytes(), before)
        self.assertFalse(self.path.read_bytes().startswith(setup.DOCKER_MARKER))
        self.assertNotIn("smtp $literal", out.getvalue())
        backup_dir = self.root / ".env.backups"
        backup = next(backup_dir.iterdir())
        self.assertEqual(backup.read_bytes(), before)
        self.assertEqual(backup_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)

    def test_legacy_literals_single_quotes_and_compose_dollars(self):
        legacy = self.root / ".env"
        legacy.write_text("EMAIL_PASSWORD='literal $NAME ${OTHER} $$'\nAI_API_KEY=two$$dollars\n")
        values = setup.legacy_values(legacy)
        self.assertEqual(values["EMAIL_PASSWORD"], "literal $NAME ${OTHER} $$")
        self.assertEqual(values["AI_API_KEY"], "two$dollars")

    def test_unsupported_compose_interpolation_fails_without_output_or_writes(self):
        for value in ('"${VERY_SECRET:-secret-value}"', 'prefix$VERY_SECRET', '"line\\nsecret"'):
            with self.subTest(value=value):
                legacy = self.root / ".env"
                legacy.write_text(f"EMAIL_PASSWORD={value}\n")
                with self.assertRaises(ConfigError) as raised:
                    setup.DockerApplication(self.path, legacy=legacy)
                self.assertEqual(str(raised.exception), setup.MIGRATION_ERROR)
                self.assertNotIn("secret-value", str(raised.exception))
                self.assertFalse(self.path.exists())
                self.assertFalse((self.root / ".env.backups").exists())

    def test_symlink_runtime_and_backup_are_rejected(self):
        target = self.root / "external"
        target.write_text("do not change")
        self.path.parent.mkdir()
        self.path.symlink_to(target)
        with self.assertRaises(ConfigError):
            setup.DockerApplication(self.path)
        self.assertEqual(target.read_text(), "do not change")

    def web(self, app, requests):
        responses = []
        requests = iter(requests)
        class Server:
            server_port = 12345
            def __init__(self, _address, handler):
                self.handler = handler
            def handle_request(self):
                method, fields = next(requests)
                handler = self.handler.__new__(self.handler)
                body = urllib.parse.urlencode(fields).encode()
                handler.headers = {"Content-Length": str(len(body))}
                handler.rfile, handler.wfile = io.BytesIO(body), io.BytesIO()
                status = []
                handler.send_response = status.append
                handler.send_header = lambda *_: None
                handler.end_headers = lambda: None
                getattr(handler, f"do_{method}")()
                responses.append((status[0], handler.wfile.getvalue().decode()))
            def server_close(self):
                pass
        args = SimpleNamespace(host="127.0.0.1", port=0, public_port=0, ssh_user="user", ssh_host="host", ssh_port=22)
        with mock.patch.object(setup, "HTTPServer", Server), contextlib.redirect_stdout(io.StringIO()):
            saved = setup.serve(app, args)
        return saved, responses

    def test_web_uses_runtime_document_and_unified_weekly_preserves_secret(self):
        app = self.application()
        form = dict(VALID, EMAIL_PASSWORD="", AI_API_KEY="", EMAIL_TO="other@example.com", WEEKLY_TIME="09:45")
        saved, responses = self.web(app, [("GET", {}), ("POST", dict(form, _action="preview")), ("POST", form)])
        self.assertTrue(saved)
        self.assertEqual(read_env(self.path)["EMAIL_PASSWORD"], VALID["EMAIL_PASSWORD"])
        self.assertEqual(read_env(self.path)["WEEKLY_HOUR"], "9")
        self.assertEqual(read_env(self.path)["WEEKLY_MINUTE"], "45")
        for status, page in responses:
            self.assertEqual(status, 200)
            self.assertNotIn(VALID["EMAIL_PASSWORD"], page)
            self.assertNotIn('name="WEEKLY_HOUR"', page)
            self.assertNotIn('name="WEEKLY_MINUTE"', page)
            self.assertNotIn('name="AI_API_KEY_FILE"', page)

    def test_web_cancel_and_failed_validation_never_save(self):
        app = self.application()
        before = self.path.read_bytes()
        saved, responses = self.web(app, [("POST", dict(VALID, WEEKLY_TIME="25:00")), ("POST", {"_action": "cancel"})])
        self.assertFalse(saved)
        self.assertEqual(responses[0][0], 400)
        self.assertEqual(self.path.read_bytes(), before)

    def test_migrated_secret_and_unknown_application_values_never_leak_in_previews(self):
        secrets = {"S3_SECRET_ACCESS_KEY": "synthetic-s3-secret", "S3_ACCESS_KEY_ID": "synthetic-access-id",
                   "AI_FUTURE_TOKEN": "synthetic-future-token", "STORAGE_OPAQUE": "synthetic-opaque-value"}
        native_fields = setup.shared.SECRET_FIELDS.copy()
        write_env(self.root / ".env", dict(VALID, **secrets, S3_REGION="test-region"), "docker")
        app = setup.DockerApplication(self.path, legacy=self.root / ".env")
        saved, text = self.menu(app, ["5", "q", "y"])
        self.assertFalse(saved)
        form = dict(VALID, EMAIL_PASSWORD="", AI_API_KEY="", WEEKLY_TIME="12:30", _action="preview")
        saved, responses = self.web(app, [("POST", form), ("POST", {"_action": "cancel"})])
        self.assertFalse(saved)
        for output in (text, responses[0][1]):
            for secret in secrets.values():
                self.assertNotIn(secret, output)
            self.assertIn("test-region", output)
        self.assertFalse(self.path.exists())
        self.assertFalse((self.root / ".env.backups").exists())
        self.assertEqual(setup.shared.SECRET_FIELDS, native_fields)

    def test_web_validation_error_keeps_new_secret_server_side_through_get_preview_save(self):
        app = self.application()
        first = dict(VALID, EMAIL_PASSWORD="replacement-secret", AI_API_KEY="replacement-key", WEEKLY_TIME="25:00")
        retry = dict(VALID, EMAIL_PASSWORD="", AI_API_KEY="", WEEKLY_TIME="12:30")
        saved, responses = self.web(app, [("POST", first), ("GET", {}),
                                        ("POST", dict(retry, _action="preview")), ("POST", retry)])
        self.assertTrue(saved)
        self.assertEqual([status for status, _ in responses], [400, 200, 200, 200])
        self.assertEqual(read_env(self.path)["EMAIL_PASSWORD"], "replacement-secret")
        self.assertEqual(read_env(self.path)["AI_API_KEY"], "replacement-key")
        self.assertIn("待保存", responses[0][1])
        self.assertNotIn("已保存，留空保持不变", responses[0][1])
        for _, page in responses:
            self.assertNotIn("replacement-secret", page)
            self.assertNotIn("replacement-key", page)

    def test_web_pending_clear_survives_validation_error_and_blank_retry(self):
        app = self.application(dict(VALID, S3_SECRET_ACCESS_KEY="old-s3-secret"))
        first = dict(VALID, EMAIL_PASSWORD="", AI_API_KEY="", WEEKLY_TIME="25:00",
                     _clear_AI_API_KEY="yes", _clear_S3_SECRET_ACCESS_KEY="yes")
        retry = dict(VALID, EMAIL_PASSWORD="", AI_API_KEY="", WEEKLY_TIME="12:30")
        saved, responses = self.web(app, [("POST", first), ("GET", {}), ("POST", retry)])
        self.assertTrue(saved)
        self.assertEqual(responses[0][0], 400)
        self.assertEqual(read_env(self.path)["AI_API_KEY"], "")
        self.assertEqual(read_env(self.path)["S3_SECRET_ACCESS_KEY"], "")
        self.assertIn("待清空", responses[1][1])
        self.assertTrue(all("old-s3-secret" not in page for _, page in responses))

    def test_web_save_error_retains_new_secret_draft_for_retry(self):
        app = self.application()
        save = app.save
        first = dict(VALID, EMAIL_PASSWORD="replacement-secret", AI_API_KEY="", WEEKLY_TIME="12:30")
        retry = dict(first, EMAIL_PASSWORD="")
        with mock.patch.object(app, "save", side_effect=[ConfigError("合成保存失败"), None]) as mocked:
            saved, responses = self.web(app, [("POST", first), ("POST", retry)])
        self.assertTrue(saved)
        self.assertEqual([status for status, _ in responses], [409, 200])
        self.assertEqual(mocked.call_args.args[0]["EMAIL_PASSWORD"], "replacement-secret")
        save(mocked.call_args.args[0])
        self.assertEqual(read_env(self.path)["EMAIL_PASSWORD"], "replacement-secret")
        self.assertTrue(all("replacement-secret" not in page for _, page in responses))

    def test_web_required_secret_clear_is_a_draft_until_replaced_or_cancelled(self):
        app = self.application()
        before = self.path.read_bytes()
        clear = dict(VALID, EMAIL_PASSWORD="", AI_API_KEY="", WEEKLY_TIME="12:30", _clear_EMAIL_PASSWORD="yes")
        saved, responses = self.web(app, [("POST", clear), ("GET", {}), ("POST", {"_action": "cancel"})])
        self.assertFalse(saved)
        self.assertEqual(responses[0][0], 400)
        self.assertIn("待清空", responses[1][1])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((self.path.parent / ".env.backups").exists())
        self.assertTrue(all(VALID["EMAIL_PASSWORD"] not in page for _, page in responses))

    def test_unknown_application_field_is_sensitive_even_when_shared_ui_adds_it(self):
        values = dict(VALID, AI_FUTURE_SETTING="opaque-future-value")
        extra = ("AI_FUTURE_SETTING", "未来设置", False, "")
        with mock.patch.object(setup.shared, "FIELDS", [*setup.shared.FIELDS, extra]):
            page = setup.render(values)
            self.assertNotIn("opaque-future-value", page)
            self.assertIn('name="AI_FUTURE_SETTING" type="password" value=""', page)
        self.assertNotIn("opaque-future-value", "\n".join(setup.change_lines({}, values)))


if __name__ == "__main__":
    unittest.main()
