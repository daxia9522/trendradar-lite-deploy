"""Shared optional-backup UI contracts, using only temporary config and mocks."""
import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("backup_menu_shared", ROOT / "deploy/configure.py")
shared = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(shared)
from envfile import EnvDocument, read_env, write_env


class BackupMenuTests(unittest.TestCase):
    def values(self, **updates):
        values = {
            "EMAIL_FROM": "sender@example.invalid", "EMAIL_TO": "reader@example.invalid",
            "EMAIL_PASSWORD": "synthetic-mail-secret", "TZ": "Asia/Shanghai",
            "STORAGE_BACKEND": "local", "R2_BACKUP_ENABLED": "false",
        }
        values.update(updates)
        return values

    def enabled(self, **updates):
        values = self.values(
            R2_BACKUP_ENABLED="true", R2_BACKUP_TIME="23:40", R2_BACKUP_LOOKBACK_DAYS="2",
            S3_BUCKET_NAME="synthetic-bucket", S3_ENDPOINT_URL="https://objects.example.invalid",
            S3_ACCESS_KEY_ID="synthetic-access-private", S3_SECRET_ACCESS_KEY="synthetic-secret-private",
        )
        values.update(updates)
        return values

    def test_disabled_default_needs_no_storage_credentials(self):
        values = self.values()
        values.pop("R2_BACKUP_ENABLED")
        self.assertEqual(shared.validate(values), [])
        defaults = read_env(ROOT / ".env.example")
        self.assertEqual(defaults["R2_BACKUP_ENABLED"], "false")
        self.assertEqual(defaults["R2_BACKUP_TIME"], "23:40")
        self.assertEqual(defaults["R2_BACKUP_LOOKBACK_DAYS"], "2")

    def test_enabled_requires_all_four_credentials(self):
        for key in ("S3_BUCKET_NAME", "S3_ENDPOINT_URL", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"):
            with self.subTest(key=key):
                values = self.enabled()
                values.pop(key)
                self.assertTrue(shared.validate(values))
        self.assertEqual(shared.validate(self.enabled()), [])

    def test_remote_primary_storage_is_not_a_local_backup_source(self):
        self.assertTrue(shared.validate(self.enabled(STORAGE_BACKEND="remote")))
        self.assertTrue(shared.validate(self.enabled(STORAGE_BACKEND="auto")))

    def test_backup_field_ranges_are_validated_without_echoing_values(self):
        for key, value in (
            ("R2_BACKUP_TIME", "24:00"), ("R2_BACKUP_TIME", "9:00"),
            ("R2_BACKUP_LOOKBACK_DAYS", "0"), ("R2_BACKUP_LOOKBACK_DAYS", "3661"),
            ("R2_BACKUP_ENABLED", "synthetic-invalid-flag"),
        ):
            with self.subTest(key=key, value=value):
                errors = shared.validate_field(key, value)
                self.assertTrue(errors)
                if value == "synthetic-invalid-flag":
                    self.assertNotIn(value, " ".join(errors))

    def test_both_storage_keys_are_password_fields_and_never_rendered(self):
        values = self.enabled()
        page = shared.render(values)
        for key in ("S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"):
            self.assertIn(key, shared.SECRET_FIELDS)
            self.assertIn(f'name="{key}" type="password"', page)
            self.assertNotIn(values[key], page)
        self.assertIn('name="R2_BACKUP_TIME" type="time"', page)
        self.assertIn('name="R2_BACKUP_ENABLED"', page)
        self.assertNotIn("synthetic-mail-secret", page)

    def test_endpoint_embedded_credentials_are_hidden_and_rejected(self):
        values = self.enabled(S3_ENDPOINT_URL="https://user:private-endpoint-token@objects.example.invalid?signature=private-query-token")
        page = shared.render(values)
        self.assertNotIn("private-endpoint-token", page)
        self.assertNotIn("private-query-token", page)
        errors = shared.validate(values)
        self.assertTrue(errors)
        self.assertNotIn("private-endpoint-token", " ".join(errors))

    def test_secret_preview_and_prompt_preserve_literal_values_without_echo(self):
        values = self.enabled()
        replacement = ' private-new-secret $literal "quote" '
        with patch.object(shared.getpass, "getpass", return_value=replacement):
            with patch.object(shared, "input", side_effect=AssertionError("secret used visible input"), create=True):
                shared.edit_native_field("S3_SECRET_ACCESS_KEY", values)
        self.assertEqual(values["S3_SECRET_ACCESS_KEY"], replacement)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            shared.print_changes(self.enabled(), values)
        self.assertIn("S3_SECRET_ACCESS_KEY", output.getvalue())
        self.assertNotIn(replacement, output.getvalue())
        self.assertNotIn("synthetic-secret-private", output.getvalue())

    def test_explicit_secret_clear_invalidates_enabled_backup(self):
        values = self.enabled()
        with patch.object(shared.getpass, "getpass", return_value=":clear"):
            shared.edit_native_field("S3_ACCESS_KEY_ID", values)
        self.assertEqual(values["S3_ACCESS_KEY_ID"], "")
        self.assertTrue(shared.validate(values))

    def test_enable_in_draft_makes_auto_storage_explicitly_local(self):
        values = self.values(STORAGE_BACKEND="auto")
        with patch.object(shared, "input", return_value="1", create=True), contextlib.redirect_stdout(io.StringIO()):
            shared.edit_native_field("R2_BACKUP_ENABLED", values)
        self.assertEqual(values["R2_BACKUP_ENABLED"], "true")
        self.assertEqual(values["STORAGE_BACKEND"], "local")
        values = self.values(STORAGE_BACKEND="remote")
        with patch.object(shared, "input", return_value="1", create=True), contextlib.redirect_stdout(io.StringIO()):
            shared.edit_native_field("R2_BACKUP_ENABLED", values)
        self.assertEqual(values["STORAGE_BACKEND"], "remote")

    def test_new_menu_keeps_original_numbering_and_cancel_has_no_side_effects(self):
        self.assertIn("6", shared.MENU_SECTIONS)
        self.assertNotIn("5", shared.MENU_SECTIONS)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "env"
            write_env(path, self.values())
            original = path.read_bytes()
            application = SimpleNamespace(
                document=EnvDocument(path), app_dir=ROOT,
                schedule=SimpleNamespace(warnings=[], suggested={}), save=Mock(),
            )
            answers = iter(["6", "1", "1", "0", "5", "q", "y"])
            with patch.object(shared, "input", side_effect=lambda _: next(answers), create=True):
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    result = shared.configure_terminal(path, application=application)
            self.assertFalse(result)
            application.save.assert_not_called()
            self.assertEqual(path.read_bytes(), original)
            self.assertFalse((path.parent / ".env.backups").exists())
            self.assertIn("R2/S3", output.getvalue())
            self.assertNotIn("synthetic-mail-secret", output.getvalue())


if __name__ == "__main__":
    unittest.main()
