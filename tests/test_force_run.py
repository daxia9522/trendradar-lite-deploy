import os
import unittest
from unittest.mock import patch

from trendradar.daily import _is_manual_force_run


class ForceRunTests(unittest.TestCase):
    def test_local_force_marker(self):
        with patch.dict(os.environ, {"TRENDRADAR_FORCE_RUN": "1"}, clear=True):
            self.assertTrue(_is_manual_force_run())

    def test_workflow_dispatch_marker(self):
        with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "workflow_dispatch"}, clear=True):
            self.assertTrue(_is_manual_force_run())

    def test_regular_schedule_is_not_forced(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(_is_manual_force_run())
