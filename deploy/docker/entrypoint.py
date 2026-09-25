#!/usr/bin/env python3
"""One Docker runtime boundary for scheduled, manual and diagnostic commands.

Examples: entrypoint.py [schedule|config-check|doctor|current|force-run|weekly|
show-schedule|test-notification]. The existing `python -m trendradar` and
`python weekly_report/weekly_ai_report_email.py` forms are recognized, not
executed as arbitrary shell/program commands. Extra options are checked against
the existing daily/weekly CLI, then passed literally as argv. execve makes the
selected application receive container signals directly without mutating this
process's os.environ or importing application code before runtime validation.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Mapping

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from deploy.docker.runtime_config import RuntimeConfigError, load_runtime_config

DAILY_OPTIONS = ("--force-run", "--show-schedule", "--doctor", "--test-notification")
WEEKLY_OPTIONS = ("--start", "--end", "--to", "--subject", "--model")
WEEKLY_SCRIPT = "weekly_report/weekly_ai_report_email.py"
USAGE = "Usage: entrypoint.py [schedule|config-check|doctor|current|force-run|weekly|show-schedule|test-notification] [options]"


class CommandError(ValueError):
    def __init__(self):
        super().__init__("unsupported Docker command or arguments")


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise CommandError()  # argparse's normal errors can echo secret argv.


def _validate_args(arguments: list[str], weekly: bool) -> None:
    parser = _Parser(add_help=False, allow_abbrev=False)
    parser.add_argument("--help", "-h", action="store_true")
    if weekly:
        for option in WEEKLY_OPTIONS:
            parser.add_argument(option)
        parser.add_argument("--dry-run", action="store_true")
    else:
        for option in DAILY_OPTIONS:
            parser.add_argument(option, action="store_true")
    parser.parse_args(arguments)


def command_for(arguments: list[str]) -> list[str] | str:
    """Return a fixed Python argv, or an internal schedule/config-check action."""
    args = list(arguments)
    if not args:
        return "schedule"
    if args[0] in ("python", "python3", sys.executable):
        args.pop(0)
        if args[:2] == ["-m", "trendradar"]:
            args = ["current", *args[2:]]
        elif args and args[0] in (WEEKLY_SCRIPT, "/app/" + WEEKLY_SCRIPT):
            args = ["weekly", *args[1:]]
        else:
            raise CommandError()
    if args[0] in ("schedule", "config-check"):
        if len(args) != 1:
            raise CommandError()
        return args[0]
    command, options = args[0], args[1:]
    aliases = {"doctor": "--doctor", "force-run": "--force-run",
               "show-schedule": "--show-schedule", "test-notification": "--test-notification"}
    if command == "weekly":
        _validate_args(options, weekly=True)
        return [sys.executable, WEEKLY_SCRIPT, *options]
    if command == "current":
        pass
    elif command in aliases:
        options.insert(0, aliases[command])
    elif command in (*DAILY_OPTIONS, "--help", "-h"):
        options = args
    else:
        raise CommandError()
    _validate_args(options, weekly=False)
    return [sys.executable, "-m", "trendradar", *options]


def main(argv: list[str] | None = None, base_env: Mapping[str, str] | None = None) -> int:
    try:
        command = command_for(list(sys.argv[1:] if argv is None else argv))
        base = dict(os.environ if base_env is None else base_env)
        snapshot = load_runtime_config(base)
        if command == "config-check":
            print("[docker] runtime configuration valid (" + ("external file" if snapshot.external else "legacy env-only") + ")")
            return 0
        if command == "schedule":
            from deploy.docker.scheduler import main as schedule_main
            return schedule_main(base_env=base)
        os.execve(sys.executable, command, dict(snapshot.env))
        return 0  # Reached only by a mocked execve in unit tests.
    except RuntimeConfigError as error:
        print(f"[docker] {error}", file=sys.stderr)
        return 2
    except CommandError as error:
        print(f"[docker] {error}\n{USAGE}", file=sys.stderr)
        return 2
    except OSError:
        print("[docker] application process could not start", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
