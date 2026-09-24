"""Independent contract checks against real parsers; never start containers/services."""

import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("external_format_configure", ROOT / "deploy/configure.py")
configure = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(configure)


class ExternalFormatTests(unittest.TestCase):
    def test_docker_literal_values_match_compose_env_file(self):
        if not shutil.which("docker"):
            self.skipTest("Docker CLI unavailable; no daemon is needed for this test")
        version = subprocess.run(["docker", "compose", "version"], capture_output=True)
        if version.returncode:
            self.skipTest("Compose plugin unavailable")
        secret = "space 'single' \"double\" \\ slash $HOME ${USER} $(no-command) `no-command` # literal"
        values = {
            "EMAIL_FROM": "sender@example.com",
            "EMAIL_TO": "reader@example.com",
            "EMAIL_PASSWORD": secret,
            "AI_API_KEY": secret,
            "TZ": "Asia/Shanghai",
            "TREND_RADAR_IMAGE": "example.invalid/test:fixed",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configure.write_env(root / ".env", values, deployment="docker")
            (root / "compose.yaml").write_text(
                "services:\n  probe:\n    image: example.invalid/not-pulled:test\n    env_file:\n      - .env\n",
                encoding="utf-8",
            )
            clean_env = {"PATH": os.defpath, "HOME": directory, "LANG": "C.UTF-8"}
            result = subprocess.run(
                ["docker", "compose", "-f", str(root / "compose.yaml"), "config", "--format", "json"],
                cwd=directory,
                env=clean_env,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            actual = json.loads(result.stdout)["services"]["probe"]["environment"]
            # Canonical Compose output escapes literal dollars for re-parsing.
            self.assertEqual(actual["EMAIL_PASSWORD"], secret.replace("$", "$$"))
            self.assertEqual(actual["AI_API_KEY"], secret.replace("$", "$$"))
            interpolation = subprocess.run(
                ["docker", "compose", "-f", str(root / "compose.yaml"), "config", "--environment"],
                cwd=directory, env=clean_env, capture_output=True, text=True, timeout=20,
            )
            self.assertEqual(interpolation.returncode, 0, interpolation.stderr)
            variables = dict(line.split("=", 1) for line in interpolation.stdout.splitlines() if "=" in line)
            self.assertEqual(variables["EMAIL_PASSWORD"], secret)
            self.assertEqual(actual["TREND_RADAR_IMAGE"], values["TREND_RADAR_IMAGE"])
            self.assertEqual(configure.read_env(root / ".env", deployment="docker")["EMAIL_PASSWORD"], secret)

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze unavailable")
    def test_generated_calendars_parse_without_running_systemd(self):
        values = {
            "TZ": "Asia/Shanghai",
            "CRAWLER_MINUTE": "5",
            "MORNING_PUSH_TIME": "07:10",
            "NOON_PUSH_TIME": "12:20",
            "EVENING_PUSH_TIME": "18:30",
            "DAILY_SUMMARY_TIME": "22:40",
            "WEEKLY_WEEKDAY": "0",
            "WEEKLY_HOUR": "9",
            "WEEKLY_MINUTE": "45",
        }
        with tempfile.TemporaryDirectory() as directory:
            for filename, writer in (
                ("daily.timer", configure.write_systemd_timer),
                ("weekly.timer", configure.write_systemd_weekly_timer),
            ):
                path = Path(directory) / filename
                writer(path, values)
                calendars = [
                    line.split("=", 1)[1]
                    for line in path.read_text().splitlines()
                    if line.startswith("OnCalendar=") and line.split("=", 1)[1]
                ]
                self.assertTrue(calendars)
                for calendar in calendars:
                    self.assertTrue(calendar.endswith(" Asia/Shanghai"), calendar)
                    parsed = subprocess.run(
                        ["systemd-analyze", "calendar", calendar],
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    self.assertEqual(parsed.returncode, 0, parsed.stderr)


if __name__ == "__main__":
    unittest.main()
