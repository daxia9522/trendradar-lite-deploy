"""Inspect and explicitly resolve daily publication state; never sends mail.

Run from the repository directory, or select the local output with --data-dir.
Remote mode uses the existing S3_* environment configuration. This maintenance
entry point intentionally does not initialize the crawler, AI or scheduler.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from datetime import datetime
import json
import os
import sys
from zoneinfo import ZoneInfo


class CommandError(ValueError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # Standard argparse errors can echo private recipient/evidence argv.
        raise CommandError("Invalid publication command or arguments")


def parse_arguments(argv=None):
    parser = _Parser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--backend", choices=("auto", "local", "remote"),
                        default=os.environ.get("STORAGE_BACKEND", "auto"))
    parser.add_argument("--data-dir", default="output", help="Local output directory")
    parser.add_argument("--timezone", default=os.environ.get("TIMEZONE") or os.environ.get("TZ") or "Asia/Shanghai")
    subparsers = parser.add_subparsers(dest="command", required=True)
    status = subparsers.add_parser("status", allow_abbrev=False, help="Read status without changing the publication baseline")
    status.add_argument("--report-id")
    status.add_argument("--show-recipients", action="store_true", help="Explicitly include original envelope addresses")
    status.add_argument("--offset", type=int, default=0)
    status.add_argument("--limit", type=int, default=20)
    receipts = subparsers.add_parser("receipts", allow_abbrev=False, help="Read bounded receipt history, including archived reports")
    receipts.add_argument("report_id")
    receipts.add_argument("--cursor", help="Exact next_cursor from the previous page")
    receipts.add_argument("--limit", type=int, default=20)
    receipts.add_argument("--show-recipients", action="store_true")
    for name in ("release-generation", "resolve-unknown", "readopt-source"):
        action = subparsers.add_parser(name, allow_abbrev=False)
        if name == "readopt-source":
            action.add_argument("kind", choices=("news", "rss"))
        else:
            action.add_argument("report_id")
        action.add_argument("--version", required=True, help="Exact version returned by the latest status inspection")
        action.add_argument("--confirm", action="store_true", help="Confirm the supplied evidence has been checked")
        action.add_argument("--stopped", action="store_true", help="Attest affected collection/generation/submission workers have stopped")
        action.add_argument("--evidence", required=True, help="Short non-secret evidence reference, not raw SMTP output")
        action.add_argument("--actor", default="operator")
        if name == "resolve-unknown":
            action.add_argument("--recipient", required=True)
            action.add_argument("--outcome", choices=("accepted", "not_accepted", "permanent_failed"), required=True)
            action.add_argument("--attempt-id", help="Current attempt ID from status, if one is present")
    arguments = parser.parse_args(argv)
    if arguments.backend not in {"auto", "local", "remote"}:
        raise CommandError("Invalid publication backend")
    if arguments.command in {"status", "receipts"}:
        if getattr(arguments, "offset", 0) < 0 or not 1 <= arguments.limit <= 100:
            raise CommandError("Invalid status page")
    elif not arguments.confirm or not arguments.stopped:
        raise CommandError("Explicit confirmation and stopped-worker verification are required")
    return arguments


def _open_coordinator(arguments):
    from trendradar.daily_flow.publication import PublicationCoordinator
    from trendradar.storage.manager import StorageManager

    timezone = ZoneInfo(arguments.timezone)
    manager = StorageManager(backend_type=arguments.backend, data_dir=arguments.data_dir,
                             enable_txt=False, enable_html=False, timezone=arguments.timezone)
    try:
        coordinator = PublicationCoordinator(manager.get_publication_store(),
                                              lambda: datetime.now(timezone))
        return coordinator, manager
    except Exception:
        manager.cleanup()
        raise


def main(argv=None):
    manager = None
    try:
        arguments = parse_arguments(argv)
        # Keep stdout as one JSON document; dependency diagnostics go to stderr.
        with redirect_stdout(sys.stderr):
            coordinator, manager = _open_coordinator(arguments)
            if arguments.command == "status":
                result = coordinator.status(arguments.report_id, include_recipients=arguments.show_recipients,
                                            offset=arguments.offset, limit=arguments.limit)
            elif arguments.command == "receipts":
                result = coordinator.receipt_history(arguments.report_id, cursor=arguments.cursor,
                                                     limit=arguments.limit,
                                                     include_recipients=arguments.show_recipients)
            else:
                confirmation = dict(expected_version=arguments.version, confirmed=True,
                                    evidence_reference=arguments.evidence, actor=arguments.actor)
                if arguments.command == "release-generation":
                    version = coordinator.release_generation(arguments.report_id, generation_stopped=True,
                                                             **confirmation)
                elif arguments.command == "readopt-source":
                    version = coordinator.readopt_source(arguments.kind, collection_stopped=True, **confirmation)
                else:
                    version = coordinator.resolve_unknown(
                        arguments.report_id, resolutions={arguments.recipient: arguments.outcome},
                        submission_stopped=True, expected_attempt_id=arguments.attempt_id, **confirmation)
                result = {"updated": True, "version": version, "mail_sent": False}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except CommandError as error:
        print(str(error), file=sys.stderr)
        return 2
    except Exception as error:
        from trendradar.storage.publication import PublicationConflict
        if isinstance(error, PublicationConflict):
            print("Publication state changed; inspect the latest status before retrying.", file=sys.stderr)
            return 3
        # No exception body, recipient address, evidence text, path or credential.
        print(f"Publication operation was not completed ({type(error).__name__}); check state and access.", file=sys.stderr)
        return 2
    finally:
        if manager is not None:
            try:
                with redirect_stdout(sys.stderr):
                    manager.cleanup()
            except Exception as error:
                print(f"Publication resource cleanup failed ({type(error).__name__}).", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
