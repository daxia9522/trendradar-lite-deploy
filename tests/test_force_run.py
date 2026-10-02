import io
import os
import runpy
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from trendradar import cli
from trendradar.core.execution_policy import FORCE_RUN_ENV, is_manual_force_run
from trendradar.daily_flow.runner import DailyRunner


class ForceRunTests(unittest.TestCase):
    def setUp(self):
        self.runner = DailyRunner.__new__(DailyRunner)

    def test_local_force_marker(self):
        with patch.dict(os.environ, {FORCE_RUN_ENV: "1"}, clear=True):
            self.assertTrue(is_manual_force_run(os.environ))
            self.assertTrue(self.runner._manual_force_run())

    def test_workflow_dispatch_marker(self):
        for marker in ("GITHUB_EVENT_NAME", "WORKFLOW_EVENT_NAME"):
            with self.subTest(marker=marker), patch.dict(os.environ, {marker: "workflow_dispatch"}, clear=True):
                self.assertTrue(is_manual_force_run(os.environ))
                self.assertTrue(self.runner._manual_force_run())

    def test_regular_schedule_is_not_forced(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(is_manual_force_run(os.environ))
            self.assertFalse(self.runner._manual_force_run())

    def test_runner_reads_environment_each_time_instead_of_caching_force(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(self.runner._manual_force_run())
            os.environ[FORCE_RUN_ENV] = "1"
            self.assertTrue(self.runner._manual_force_run())
            os.environ[FORCE_RUN_ENV] = "0"
            self.assertFalse(self.runner._manual_force_run())


class DailyCliTests(unittest.TestCase):
    def test_cli_constructs_daily_runner_with_one_config_load_and_explicit_force(self):
        for force in (False, True):
            with self.subTest(force=force):
                config = {"DEBUG": False}
                events = []
                runner = Mock(is_github_actions=False, ctx=SimpleNamespace(config=config))
                runner.run.side_effect = lambda: events.append(("run", is_manual_force_run(os.environ)))

                def create_runner(**kwargs):
                    self.assertIs(kwargs["config"], config)
                    events.append(("construct", is_manual_force_run(os.environ)))
                    return runner

                argv = ["trendradar"] + (["--force-run"] if force else [])
                with patch.object(cli, "load_config", return_value=config) as load:
                    with patch.object(cli, "DailyRunner", side_effect=create_runner) as factory:
                        with patch.object(cli, "check_all_versions", side_effect=AssertionError("unexpected remote version check")) as version_check:
                            with patch.object(sys, "argv", argv), patch.dict(os.environ, {}, clear=True):
                                with redirect_stdout(io.StringIO()):
                                    result = cli.main()
                                self.assertEqual(os.environ.get(FORCE_RUN_ENV), "1" if force else None)
                self.assertEqual(result, 0)
                load.assert_called_once_with()
                factory.assert_called_once_with(config=config)
                runner.run.assert_called_once_with()
                version_check.assert_not_called()
                self.assertEqual(events, [("construct", force), ("run", force)])

    def test_module_entry_exits_with_the_cli_status(self):
        entry_path = Path(cli.__file__).with_name("__main__.py")
        with patch.object(cli, "main", return_value=17) as main:
            with self.assertRaises(SystemExit) as stopped:
                runpy.run_path(str(entry_path), run_name="__main__")
        self.assertEqual(stopped.exception.code, 17)
        main.assert_called_once_with()
