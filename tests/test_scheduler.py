import os
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import yaml

from trendradar.core.scheduler import Scheduler


class MemoryExecutionStorage:
    def __init__(self):
        self.executed = set()

    def has_period_executed(self, date_str, period_key, action):
        return (date_str, period_key, action) in self.executed

    def record_period_execution(self, date_str, period_key, action):
        self.executed.add((date_str, period_key, action))
        return True


class SchedulerContractTests(unittest.TestCase):
    def resolve_at(self, hour):
        scheduler = Scheduler(
            schedule_config={"enabled": True, "preset": "custom"},
            timeline_data=yaml.safe_load(
                Path("config/timeline.yaml").read_text(encoding="utf-8")
            ),
            storage_backend=MemoryExecutionStorage(),
            get_time_func=lambda: datetime(2026, 8, 11, hour, 15),
            fallback_report_mode="current",
        )
        return scheduler.resolve()

    def test_delivery_windows(self):
        expected = {
            7: ("morning_brief", "current"),
            12: ("noon_brief", "current"),
            18: ("afternoon_brief", "current"),
            22: ("nightly_daily", "daily"),
        }
        for hour, (period, mode) in expected.items():
            with self.subTest(hour=hour):
                schedule = self.resolve_at(hour)
                self.assertEqual(schedule.period_key, period)
                self.assertEqual(schedule.report_mode, mode)
                self.assertTrue(schedule.analyze)
                self.assertTrue(schedule.push)
                self.assertFalse(hasattr(schedule, "ai_mode"))

    def test_environment_can_override_delivery_time(self):
        with patch.dict(os.environ, {"MORNING_PUSH_TIME": "08:30"}):
            scheduler = Scheduler(
                schedule_config={"enabled": True, "preset": "custom"},
                timeline_data=yaml.safe_load(Path("config/timeline.yaml").read_text(encoding="utf-8")),
                storage_backend=MemoryExecutionStorage(),
                get_time_func=lambda: datetime(2026, 8, 11, 8, 30),
                fallback_report_mode="current",
            )
            schedule = scheduler.resolve()
        self.assertEqual(schedule.period_key, "morning_brief")
        self.assertTrue(schedule.push)

    def test_default_window_collects_without_delivery(self):
        schedule = self.resolve_at(10)
        self.assertIsNone(schedule.period_key)
        self.assertTrue(schedule.collect)
        self.assertFalse(schedule.analyze)
        self.assertFalse(schedule.push)
