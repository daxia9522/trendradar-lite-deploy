import importlib.util
import os
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("deploy_configure", ROOT / "deploy" / "configure.py")
configure = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(configure)


class ConfigureTests(unittest.TestCase):
    def test_env_round_trip_and_private_permissions(self):
        values = {
            "EMAIL_FROM": "sender@example.com",
            "EMAIL_PASSWORD": "secret value",
            "EMAIL_TO": "reader@example.com",
            "EMAIL_SMTP_SERVER": "smtp.example.com",
            "EMAIL_SMTP_PORT": "465",
            "TZ": "Asia/Shanghai",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "env"
            configure.write_env(path, values)
            loaded = configure.read_env(path)

            self.assertEqual(loaded["EMAIL_PASSWORD"], "secret value")
            self.assertEqual(loaded["STORAGE_BACKEND"], "local")
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_required_email_fields_are_validated(self):
        errors = configure.validate({})
        self.assertTrue(any("发件邮箱" in error for error in errors))
        self.assertTrue(any("SMTP" in error for error in errors))

    def test_ai_credentials_are_required_only_when_enabled(self):
        values = {
            "EMAIL_FROM": "sender@example.com",
            "EMAIL_PASSWORD": "secret",
            "EMAIL_TO": "reader@example.com",
            "EMAIL_SMTP_SERVER": "smtp.example.com",
            "EMAIL_SMTP_PORT": "465",
            "TZ": "Asia/Shanghai",
            "AI_ANALYSIS_ENABLED": "true",
        }
        self.assertTrue(any("AI_MODEL" in error for error in configure.validate(values)))
        values.update({"AI_MODEL": "openai/model", "AI_API_KEY": "key"})
        self.assertEqual(configure.validate(values), [])

    def test_numeric_ranges_are_validated(self):
        values = {
            "EMAIL_FROM": "sender@example.com",
            "EMAIL_PASSWORD": "secret",
            "EMAIL_TO": "reader@example.com",
            "EMAIL_SMTP_SERVER": "smtp.example.com",
            "EMAIL_SMTP_PORT": "70000",
            "TZ": "Asia/Shanghai",
            "CRAWLER_MINUTE": "60",
        }
        errors = configure.validate(values)
        self.assertTrue(any("EMAIL_SMTP_PORT" in error for error in errors))
        self.assertTrue(any("CRAWLER_MINUTE" in error for error in errors))

    def test_push_time_format_is_validated(self):
        values = {
            "EMAIL_FROM": "sender@example.com",
            "EMAIL_PASSWORD": "secret",
            "EMAIL_TO": "reader@example.com",
            "EMAIL_SMTP_SERVER": "smtp.example.com",
            "EMAIL_SMTP_PORT": "465",
            "TZ": "Asia/Shanghai",
            "MORNING_PUSH_TIME": "25:10",
        }
        self.assertTrue(any("MORNING_PUSH_TIME" in error for error in configure.validate(values)))

    def test_ssh_command_uses_detected_target(self):
        command = configure.ssh_tunnel_command("root", "152.44.45.211", 22, 8765)
        self.assertEqual(command, "ssh -L 8765:127.0.0.1:8765 root@152.44.45.211")
        custom_port = configure.ssh_tunnel_command("ubuntu", "203.0.113.10", 2222, 8765)
        self.assertEqual(custom_port, "ssh -p 2222 -L 8765:127.0.0.1:8765 ubuntu@203.0.113.10")

    def test_systemd_timer_contains_collection_and_delivery_times(self):
        values = {
            "CRAWLER_MINUTE": "5",
            "MORNING_PUSH_TIME": "07:10",
            "NOON_PUSH_TIME": "12:20",
            "EVENING_PUSH_TIME": "18:30",
            "DAILY_SUMMARY_TIME": "22:40",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "trendradar-lite.timer"
            configure.write_systemd_timer(path, values)
            content = path.read_text(encoding="utf-8")
        self.assertIn("OnCalendar=*-*-* *:05:00", content)
        self.assertIn("OnCalendar=*-*-* 18:30:00", content)
        self.assertIn("OnCalendar=*-*-* 22:40:00", content)


if __name__ == "__main__":
    unittest.main()
