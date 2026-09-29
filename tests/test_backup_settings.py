"""Offline, stdlib-only optional backup configuration contract tests."""
import dataclasses
import unittest
from unittest import mock

from deploy.backup_settings import BackupConfigError, BackupSettings, load_backup_settings


class BackupSettingsTests(unittest.TestCase):
    def enabled(self, **overrides):
        return {
            "R2_BACKUP_ENABLED": "true", "STORAGE_BACKEND": "local",
            "S3_BUCKET_NAME": "synthetic-bucket", "S3_ACCESS_KEY_ID": "synthetic-access",
            "S3_SECRET_ACCESS_KEY": "synthetic-secret", "S3_ENDPOINT_URL": "https://s3.example.invalid",
            **overrides,
        }

    def test_defaults_disabled_no_credentials(self):
        self.assertEqual(load_backup_settings({}), BackupSettings(False, "23:40", 2, "Asia/Shanghai"))

    def test_settings_frozen(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            load_backup_settings({}).enabled = True

    def test_switch_all_supported_forms(self):
        for raw in ("true", "TRUE", "True", "1", " true "):
            with self.subTest(raw=raw):
                self.assertTrue(load_backup_settings(self.enabled(R2_BACKUP_ENABLED=raw)).enabled)
        for raw in ("false", "FALSE", "False", "0", "", " "):
            with self.subTest(raw=raw):
                self.assertFalse(load_backup_settings({"R2_BACKUP_ENABLED": raw}).enabled)

    def test_invalid_switch_fixed_diagnostic(self):
        for raw in ("yes", "no", "2", "secret-switch-123"):
            with self.subTest(raw=raw), self.assertRaises(BackupConfigError) as raised:
                load_backup_settings({"R2_BACKUP_ENABLED": raw})
            self.assertEqual(raised.exception.code, "enabled")
            self.assertEqual(str(raised.exception), "R2_BACKUP_ENABLED must be true, false, 1 or 0")

    def test_time_bounds_and_empty_default(self):
        for raw in ("00:00", "23:59", "07:03", "23:40"):
            self.assertEqual(load_backup_settings({"R2_BACKUP_TIME": raw}).time, raw)
        self.assertEqual(load_backup_settings({"R2_BACKUP_TIME": ""}).time, "23:40")
        for raw in ("24:00", "12:60", "7:03", "07:3", "23:40:00", "０７:３０", "secret-time"):
            with self.subTest(raw=raw), self.assertRaises(BackupConfigError) as raised:
                load_backup_settings({"R2_BACKUP_TIME": raw})
            self.assertEqual(raised.exception.code, "time")
            self.assertNotIn(raw, str(raised.exception))

    def test_lookback_bounds(self):
        for raw, expected in (("1", 1), ("3660", 3660), ("2", 2), ("", 2)):
            self.assertEqual(load_backup_settings({"R2_BACKUP_LOOKBACK_DAYS": raw}).lookback_days, expected)
        for raw in ("0", "3661", "999999999999999999", "-1", "1.0", "2 days", "+2", "２"):
            with self.subTest(raw=raw), self.assertRaises(BackupConfigError) as raised:
                load_backup_settings({"R2_BACKUP_LOOKBACK_DAYS": raw})
            self.assertEqual(raised.exception.code, "lookback")

    def test_timezone_precedence_defaults_and_agreement(self):
        for values, expected in (
            ({}, "Asia/Shanghai"), ({"TIMEZONE": "UTC"}, "UTC"),
            ({"TZ": "America/New_York"}, "America/New_York"),
            ({"TZ": "UTC", "TIMEZONE": "UTC"}, "UTC"),
            ({"TZ": "UTC", "TIMEZONE": ""}, "UTC"),
        ):
            with self.subTest(values=values):
                self.assertEqual(load_backup_settings(values).timezone, expected)

    def test_timezone_conflict_rejected_even_disabled(self):
        with self.assertRaises(BackupConfigError) as raised:
            load_backup_settings({"TIMEZONE": "UTC", "TZ": "Asia/Shanghai"})
        self.assertEqual(raised.exception.code, "timezone_conflict")

    def test_bad_timezone_never_echoed_or_chained(self):
        for raw in ("Bad/SECRET_ZONE", "/absolute/SECRET_ZONE", "../SECRET_ZONE", "secret\x00zone"):
            with self.subTest(raw=raw), self.assertRaises(BackupConfigError) as raised:
                load_backup_settings({"TZ": raw})
            self.assertNotIn("SECRET_ZONE", str(raised.exception))
            self.assertTrue(raised.exception.__suppress_context__)

    def test_enabled_requires_explicit_exact_local_backend(self):
        for raw in ("", "auto", "remote", "LOCAL"):
            with self.subTest(raw=raw), self.assertRaises(BackupConfigError) as raised:
                load_backup_settings(self.enabled(STORAGE_BACKEND=raw))
            self.assertEqual(raised.exception.code, "backend")

    def test_enabled_requires_each_credential_without_revealing_values(self):
        for name in ("S3_BUCKET_NAME", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY", "S3_ENDPOINT_URL"):
            for empty in ("", " "):
                with self.subTest(name=name, empty=empty), self.assertRaises(BackupConfigError) as raised:
                    load_backup_settings(self.enabled(**{name: empty}))
                self.assertEqual(raised.exception.code, "credentials")
                self.assertNotIn("synthetic", str(raised.exception))

    def test_credential_free_inspection_still_requires_local_and_valid_endpoint(self):
        settings = load_backup_settings({"R2_BACKUP_ENABLED": "1", "STORAGE_BACKEND": "local"}, require_credentials=False)
        self.assertTrue(settings.enabled)
        with self.assertRaises(BackupConfigError):
            load_backup_settings({"R2_BACKUP_ENABLED": "true"}, require_credentials=False)
        with self.assertRaises(BackupConfigError):
            load_backup_settings(self.enabled(S3_ENDPOINT_URL="https://secret@host.invalid"), require_credentials=False)

    def test_optional_region_not_required(self):
        self.assertTrue(load_backup_settings(self.enabled()).enabled)
        self.assertTrue(load_backup_settings(self.enabled(S3_REGION="auto")).enabled)

    def test_valid_endpoint_shapes_without_network(self):
        for endpoint in ("https://s3.example.invalid", "http://localhost:9000", "https://[::1]:9000/base", "https://s3.example.invalid/"):
            with self.subTest(endpoint=endpoint), mock.patch("socket.getaddrinfo", side_effect=AssertionError("network")):
                self.assertTrue(load_backup_settings(self.enabled(S3_ENDPOINT_URL=endpoint)).enabled)

    def test_endpoint_rejects_secrets_and_malformed_urls_fixed_errors(self):
        for endpoint in (
            "ftp://s3.example.invalid", "https://", "//s3.example.invalid", "s3.example.invalid",
            "https://SECRET@s3.example.invalid", "https://user:SECRET@s3.example.invalid",
            "https://s3.example.invalid/?token=SECRET", "https://s3.example.invalid/#SECRET",
            "https://s3.example.invalid?", "https://s3.example.invalid#", "https://s3.example.invalid:bad",
            "https://s3.example.invalid:65536", "https://[SECRET", "https://bad host.invalid",
            "https://bad\nSECRET.invalid", "https://host.invalid\\SECRET", "https://host.invalid/\x00SECRET",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(BackupConfigError) as raised:
                load_backup_settings(self.enabled(S3_ENDPOINT_URL=endpoint))
            self.assertEqual(raised.exception.code, "endpoint")
            self.assertNotIn("SECRET", str(raised.exception))
            self.assertTrue(raised.exception.__suppress_context__)

    def test_disabled_does_not_validate_unrelated_s3_settings(self):
        self.assertFalse(load_backup_settings({"S3_ENDPOINT_URL": "invalid-secret", "STORAGE_BACKEND": "remote"}).enabled)

    def test_error_constructor_cannot_echo_arbitrary_value(self):
        self.assertNotIn("private-token", str(BackupConfigError("private-token")))

    def test_mapping_not_mutated(self):
        values = self.enabled(TZ="UTC")
        before = dict(values)
        load_backup_settings(values)
        self.assertEqual(values, before)


if __name__ == "__main__":
    unittest.main()
