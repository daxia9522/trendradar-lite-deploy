"""Publication maintenance CLI contracts; no SMTP, AI or remote calls."""
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from trendradar import publication_cli as cli
from trendradar.daily_flow.publication import PublicationCoordinator
from trendradar.storage.publication import LocalPublicationStore, PublicationConflict, PublicationError


class PublicationCLITests(unittest.TestCase):
    def invoke(self, arguments, coordinator=None):
        output, errors = io.StringIO(), io.StringIO()
        manager = Mock()
        coordinator = coordinator or Mock()
        with patch.object(cli, "_open_coordinator", return_value=(coordinator, manager)) as opened:
            with redirect_stdout(output), redirect_stderr(errors):
                code = cli.main(arguments)
        return code, output.getvalue(), errors.getvalue(), coordinator, manager, opened

    def test_status_default_is_private_and_read_only(self):
        coordinator = Mock()
        coordinator.status.return_value = {"initialized": True, "version": "v1", "reports": []}
        code, output, errors, _, manager, _ = self.invoke(["status"], coordinator)
        self.assertEqual(code, 0, errors)
        self.assertEqual(json.loads(output)["version"], "v1")
        coordinator.status.assert_called_once_with(None, include_recipients=False, offset=0, limit=20)
        coordinator.resolve_unknown.assert_not_called()
        coordinator.release_generation.assert_not_called()
        coordinator.deliver.assert_not_called()
        coordinator.recover_interrupted.assert_not_called()
        manager.cleanup.assert_called_once_with()

    def test_status_explicit_address_visibility_and_pagination(self):
        coordinator = Mock()
        coordinator.status.return_value = {"reports": []}
        code, _, _, _, _, _ = self.invoke(
            ["status", "--report-id", "report1", "--show-recipients", "--offset", "2", "--limit", "5"], coordinator)
        self.assertEqual(code, 0)
        coordinator.status.assert_called_once_with("report1", include_recipients=True, offset=2, limit=5)

    def test_invalid_or_unconfirmed_input_never_opens_store_or_echoes_argv(self):
        arguments = [
            ["status", "--unknown=PRIVATE"],
            ["status", "--limit", "PRIVATE"],
            ["status", "--limit", "101"],
            ["status", "--offset", "-1"],
            ["receipts", "report", "--limit", "0"],
            ["receipts", "report", "--limit", "101"],
            ["readopt-source", "PRIVATE", "--version", "v1", "--evidence", "ticket", "--confirm", "--stopped"],
            ["readopt-source", "rss", "--version", "v1", "--evidence", "ticket", "--confirm"],
            ["release-generation", "report", "--version", "v1", "--evidence", "PRIVATE"],
            ["release-generation", "report", "--version", "v1", "--evidence", "PRIVATE", "--confirm"],
            ["resolve-unknown", "report", "--version", "v1", "--evidence", "PRIVATE",
             "--recipient", "user@example.invalid", "--outcome", "PRIVATE", "--confirm", "--stopped"],
        ]
        for args in arguments:
            with self.subTest(args=args):
                code, output, errors, _, manager, opened = self.invoke(args)
                self.assertEqual(code, 2)
                self.assertNotIn("PRIVATE", output + errors)
                opened.assert_not_called()
                manager.cleanup.assert_not_called()

    def test_generation_release_requires_exact_version_and_operator_evidence(self):
        coordinator = Mock()
        coordinator.release_generation.return_value = "v2"
        code, output, errors, _, manager, _ = self.invoke([
            "release-generation", "report1", "--version", "v1", "--evidence", "ticket-123",
            "--actor", "admin", "--confirm", "--stopped",
        ], coordinator)
        self.assertEqual(code, 0, errors)
        self.assertEqual(json.loads(output), {"updated": True, "version": "v2", "mail_sent": False})
        coordinator.release_generation.assert_called_once_with(
            "report1", expected_version="v1", confirmed=True, generation_stopped=True,
            evidence_reference="ticket-123", actor="admin")
        coordinator.deliver.assert_not_called()
        manager.cleanup.assert_called_once_with()

    def test_unknown_resolution_never_sends_or_substitutes_status_version(self):
        coordinator = Mock()
        coordinator.resolve_unknown.return_value = '"etag2"'
        code, output, errors, _, _, _ = self.invoke([
            "resolve-unknown", "report1", "--version", '"etag1"', "--evidence", "smtp-record-7",
            "--recipient", "user@example.invalid", "--outcome", "not_accepted",
            "--attempt-id", "attempt1", "--confirm", "--stopped",
        ], coordinator)
        self.assertEqual(code, 0, errors)
        self.assertFalse(json.loads(output)["mail_sent"])
        self.assertNotIn("user@example.invalid", output + errors)
        coordinator.resolve_unknown.assert_called_once_with(
            "report1", expected_version='"etag1"', confirmed=True, submission_stopped=True,
            resolutions={"user@example.invalid": "not_accepted"}, expected_attempt_id="attempt1",
            evidence_reference="smtp-record-7", actor="operator")
        coordinator.status.assert_not_called()
        coordinator.deliver.assert_not_called()

    def test_conflict_and_storage_failure_are_redacted_and_close_resources(self):
        for error, expected in ((PublicationConflict("PRIVATE"), 3), (PublicationError("PRIVATE"), 2)):
            coordinator = Mock()
            coordinator.status.side_effect = error
            code, output, errors, _, manager, _ = self.invoke(["status"], coordinator)
            self.assertEqual(code, expected)
            self.assertNotIn("PRIVATE", output + errors)
            manager.cleanup.assert_called_once_with()

    def test_receipts_are_private_read_only_and_cursor_paged(self):
        coordinator = Mock()
        coordinator.receipt_history.return_value = {"records": [], "next_cursor": "page2"}
        code, output, errors, _, manager, _ = self.invoke(["receipts", "report1"], coordinator)
        self.assertEqual(code, 0, errors)
        self.assertEqual(json.loads(output)["next_cursor"], "page2")
        coordinator.receipt_history.assert_called_once_with(
            "report1", cursor=None, limit=20, include_recipients=False)
        coordinator.recover_interrupted.assert_not_called()
        coordinator.deliver.assert_not_called()
        manager.cleanup.assert_called_once_with()
        coordinator.reset_mock()
        code, _, errors, _, _, _ = self.invoke(
            ["receipts", "report1", "--cursor", "page2", "--limit", "5", "--show-recipients"], coordinator)
        self.assertEqual(code, 0, errors)
        coordinator.receipt_history.assert_called_once_with(
            "report1", cursor="page2", limit=5, include_recipients=True)

    def test_readoption_is_version_fenced_and_does_not_send(self):
        coordinator = Mock()
        coordinator.readopt_source.return_value = "v2"
        code, output, errors, _, _, _ = self.invoke([
            "readopt-source", "rss", "--version", "v1", "--evidence", "gap-acknowledged",
            "--confirm", "--stopped",
        ], coordinator)
        self.assertEqual(code, 0, errors)
        self.assertFalse(json.loads(output)["mail_sent"])
        coordinator.readopt_source.assert_called_once_with(
            "rss", expected_version="v1", confirmed=True, collection_stopped=True,
            evidence_reference="gap-acknowledged", actor="operator")
        coordinator.status.assert_not_called()
        coordinator.deliver.assert_not_called()

    def test_real_local_readoption_preserves_identity_root_and_sequence(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LocalPublicationStore(directory)
            coordinator = PublicationCoordinator(store, lambda: datetime(2026, 8, 1, 7, tzinfo=timezone.utc))
            coordinator.capture_baseline(enabled_kinds=("news", "rss"))
            before = coordinator.status()
            store.close()
            output, errors = io.StringIO(), io.StringIO()
            args = ["--backend", "local", "--data-dir", directory, "readopt-source", "rss",
                    "--version", before["version"], "--evidence", "accepted-history-gap", "--confirm", "--stopped"]
            with redirect_stdout(output), redirect_stderr(errors):
                code = cli.main(args)
            self.assertEqual(code, 0, errors.getvalue())
            self.assertFalse(json.loads(output.getvalue())["mail_sent"])
            store = LocalPublicationStore(directory)
            try:
                after = PublicationCoordinator(store, lambda: datetime.now(timezone.utc)).status()
                self.assertEqual(before["baseline"]["sequence"], after["baseline"]["sequence"])
                self.assertEqual(before["baseline"]["coverage"]["news"], after["baseline"]["coverage"]["news"])
                old_rss, new_rss = before["baseline"]["coverage"]["rss"], after["baseline"]["coverage"]["rss"]
                self.assertEqual(old_rss["seen_root"], new_rss["seen_root"])
                self.assertEqual(new_rss["adoption"]["generation"], old_rss["adoption"]["generation"] + 1)
                self.assertIsNone(new_rss["through"])
                audit = store.get_snapshot(new_rss["adoption"]["audit_id"])
                self.assertEqual(audit["previous"], old_rss)
                after_version = after["version"]
            finally:
                store.close()
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(args), 3)
            store = LocalPublicationStore(directory)
            try:
                self.assertEqual(store.load()[1], after_version)
            finally:
                store.close()

    def test_real_local_status_does_not_initialize_publication_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            output, errors = io.StringIO(), io.StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                code = cli.main(["--backend", "local", "--data-dir", directory, "status"])
            self.assertEqual(code, 0, errors.getvalue())
            self.assertFalse(json.loads(output.getvalue())["initialized"])
            store = LocalPublicationStore(directory)
            try:
                self.assertEqual(store.load(), (None, None))
            finally:
                store.close()

    def test_real_local_release_is_audited_without_network_or_mail(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LocalPublicationStore(directory)
            coordinator = PublicationCoordinator(store, lambda: datetime(2026, 10, 5, 7, tzinfo=timezone.utc))
            report_id = coordinator.claim_generation("2026-10-05:morning")
            version = coordinator.status()["version"]
            store.close()
            output, errors = io.StringIO(), io.StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                code = cli.main(["--backend", "local", "--data-dir", directory,
                                 "release-generation", report_id, "--version", version,
                                 "--evidence", "verified-stopped", "--confirm", "--stopped"])
            self.assertEqual(code, 0, errors.getvalue())
            self.assertFalse(json.loads(output.getvalue())["mail_sent"])
            store = LocalPublicationStore(directory)
            try:
                coordinator = PublicationCoordinator(store, lambda: datetime.now(timezone.utc))
                row = coordinator.status(report_id)["reports"][0]
                self.assertEqual(row["state"], "GENERATION_FAILED")
                self.assertFalse(row["window_owned"])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
