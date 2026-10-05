"""Structural hardening proofs. Entirely synthetic/offline, including real SQLite CAS."""
from __future__ import annotations

import copy
import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import timedelta
from unittest.mock import Mock
import test_publication_pipeline as fixtures

from test_publication_pipeline import (
    NOW, RECIPIENTS, MemoryStore, PublicationCase, SourceFixture,
    CrawlResult, RSSCollection, email, news, result, rss_item, smtp,
)
from trendradar.ai.selector import _item_score
from trendradar.daily_flow.capture import aliases, capture_sources
from trendradar.daily_flow.publication import MAX_LIVE_REPORTS, PublicationCoordinator
from trendradar.daily_flow.publication_index import SnapshotIndex, encoded
from trendradar.storage.publication import DOCUMENT_LIMIT, LocalPublicationStore, PublicationConflict, PublicationError


def growth_audit():
    """Same 124000 -> 126000 aliases and real 8 MiB store as independent audit."""
    with tempfile.TemporaryDirectory(prefix="publication-growth-fixed-") as directory:
        store = LocalPublicationStore(directory)
        coordinator = PublicationCoordinator(store, lambda: NOW)
        coordinator.capture_baseline()
        document, version = store.load()
        keys = [hashlib.sha256(str(index).encode()).hexdigest() for index in range(126000)]
        legacy = {"complete": True, "through": NOW.isoformat(), "seen": keys[:124000],
                  "boundary_sha256": "0" * 64}
        document["baseline"]["coverage"]["news"] = legacy
        document["schema"] = 1
        store.save(document, expected_version=version)
        before_size = len(encoded(document))
        report_id = coordinator.claim_generation("growth")
        coordinator.prepare(report_id, email(), {
            "captured_at": NOW.isoformat(), "coverage": {"news": {**legacy, "seen": keys}}})
        immutable_index_writes_after_smtp = []
        original_put = store.put_snapshot
        def put_after(snapshot_id, payload):
            if snapshot_id.startswith("idx-"):
                immutable_index_writes_after_smtp.append(snapshot_id)
            return original_put(snapshot_id, payload)
        def accept(*args, **kwargs):
            claimed, _ = store.load()
            staged_root = claimed["reports"][report_id]["attempt"]["publication"]["coverage"]["news"]["seen_root"]
            assert len(coordinator.known_identities(staged_root, keys)) == 126000
            store.put_snapshot = put_after
            return result(accepted=RECIPIENTS)
        dispatcher = Mock()
        dispatcher.send_prepared.side_effect = accept
        with redirect_stdout(io.StringIO()):
            coordinator.deliver(report_id, dispatcher)
        durable, _ = store.load()
        root = durable["baseline"]["coverage"]["news"]["seen_root"]
        preserved = set(key for key, value in coordinator.index.items(root) if value is True)
        assert preserved == set(keys)
        assert not immutable_index_writes_after_smtp
        assert durable["reports"][report_id]["receipt_count"] == 1
        assert durable["reports"][report_id]["state"] == "DELIVERED"
        assert durable["baseline"]["sequence"] == 1
        info = {"limit": DOCUMENT_LIMIT, "seed_manifest_bytes": before_size,
                "final_manifest_bytes": len(encoded(durable)), "exact_identities_preserved": len(preserved),
                "accepted_smtp_calls": dispatcher.send_prepared.call_count,
                "durable_receipts": durable["reports"][report_id]["receipt_count"],
                "state": durable["reports"][report_id]["state"], "baseline_sequence": 1,
                "index_writes_after_smtp": len(immutable_index_writes_after_smtp)}
        store.close()
        reopened = LocalPublicationStore(directory)
        restarted = PublicationCoordinator(reopened, lambda: NOW)
        assert len(restarted.known_identities(root, keys)) == 126000
        assert restarted.status(report_id)["reports"][0]["receipt_count"] == 1
        reopened.close()
        return info


class GrowthAndArchiveTests(PublicationCase):
    def test_real_size_limit_audit_and_reopen_preserve_every_identity(self):
        info = growth_audit()
        self.assertGreater(info["seed_manifest_bytes"], 8_300_000)
        self.assertLess(info["final_manifest_bytes"], 32_000)

    def test_reports_windows_and_receipts_archive_without_losing_inspection_or_guard(self):
        ids = []
        with redirect_stdout(io.StringIO()):
            for number in range(90):
                ids.append(self.publish(window=f"window-{number}"))
        document = self.store.document
        self.assertLessEqual(len(document["reports"]), 18)
        self.assertLessEqual(len(document["windows"]), 18)
        self.assertLess(len(encoded(document)), 80_000)
        self.assertTrue(self.coordinator.status(ids[0])["reports"][0]["archived"])
        self.assertIsNone(self.coordinator.claim_generation("window-0"))
        self.assertEqual(self.coordinator.window_report("window-0")["id"], ids[0])
        pages = [self.coordinator.status(offset=offset, limit=17) for offset in range(0, 90, 17)]
        self.assertEqual([r["id"] for p in pages for r in p["reports"]], list(reversed(ids)))
        receipts = self.coordinator.receipt_history(ids[0])
        self.assertEqual(len(receipts["records"]), 1)
        self.assertNotIn("@", json.dumps(receipts))
        self.assertNotIn("@", json.dumps(pages))

    def test_many_receipts_have_constant_live_cache_and_bounded_pages(self):
        report_id = self.prepare()
        with redirect_stdout(io.StringIO()):
            for _ in range(110):
                self.coordinator.deliver(report_id, smtp(result(temporary_failed=RECIPIENTS)))
            self.coordinator.deliver(report_id, smtp(result(accepted=RECIPIENTS)))
        report = self.store.document["reports"][report_id]
        self.assertEqual(len(report["receipts"]), 1)
        self.assertEqual(report["receipt_count"], 111)
        self.assertLess(len(encoded(self.store.document)), 10_000)
        cursor, seen = None, []
        while True:
            page = self.coordinator.receipt_history(report_id, cursor=cursor, limit=19)
            seen.extend(page["records"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(len(seen), 111)
        self.assertEqual(len({r["attempt_id"] for r in seen}), 111)

    def test_unresolved_backlog_applies_backpressure_without_discarding_results(self):
        with redirect_stdout(io.StringIO()):
            for number in range(MAX_LIVE_REPORTS):
                report_id = self.prepare(window=f"unknown-{number}")
                self.coordinator.deliver(report_id, smtp(result(unknown=RECIPIENTS)))
        before = copy.deepcopy(self.store.document)
        with self.assertRaisesRegex(PublicationError, "backlog"):
            self.coordinator.claim_generation("one-too-many")
        self.assertEqual(self.store.document, before)
        self.assertEqual(self.coordinator.status()["total_reports"], MAX_LIVE_REPORTS)
        self.assertEqual(self.coordinator.retryable_reports(), [])

    def test_archive_failure_or_ambiguous_write_never_discards_live_report(self):
        with redirect_stdout(io.StringIO()):
            ids = [self.publish(window=str(n)) for n in range(18)]
        before = copy.deepcopy(self.store.document)
        self.store.before_save = lambda document: (_ for _ in ()).throw(PublicationConflict("synthetic archive CAS"))
        with self.assertRaises(PublicationConflict):
            self.coordinator.claim_generation("archive-fails")
        self.assertEqual(self.store.document, before)
        self.store.before_save = None
        self.assertEqual(self.coordinator.status(ids[0])["reports"][0]["state"], "DELIVERED")

    def test_terminal_retention_yields_all_slots_to_unresolved_work(self):
        with redirect_stdout(io.StringIO()):
            terminal = [self.publish(window=f"done-{n}") for n in range(16)]
            for n in range(MAX_LIVE_REPORTS):
                report_id = self.prepare(window=f"pending-{n}")
                self.coordinator.deliver(report_id, smtp(result(unknown=RECIPIENTS)))
        self.assertEqual(len(self.store.document["reports"]), MAX_LIVE_REPORTS)
        self.assertTrue(all(self.coordinator.status(r)["reports"][0]["archived"] for r in terminal))

    def test_reservation_and_snapshot_preflight_stop_before_smtp(self):
        report_id = self.prepare()
        dispatcher = Mock()
        self.store.max_document_bytes = len(encoded(self.store.document)) + 100
        with self.assertRaisesRegex(PublicationError, "capacity"):
            self.coordinator.deliver(report_id, dispatcher)
        dispatcher.send_prepared.assert_not_called()
        self.assertIsNone(self.store.document["reports"][report_id]["attempt"])
        del self.store.max_document_bytes
        self.store.max_snapshot_bytes = 1000
        with self.assertRaisesRegex(PublicationError, "snapshot capacity"):
            self.coordinator.deliver(report_id, dispatcher)
        dispatcher.send_prepared.assert_not_called()

    def test_missing_identity_shard_fails_before_smtp(self):
        self.sources.add("news", "2026-10-05", news("first"))
        report_id = self.prepare()
        root = self.store.get_snapshot(report_id)["input"]["coverage"]["news"]["seen_root"]
        del self.store.snapshots[root]
        # Validate every staged captured root even when previous root is empty.
        dispatcher = Mock()
        with self.assertRaises(PublicationError):
            self.coordinator.deliver(report_id, dispatcher)
        dispatcher.send_prepared.assert_not_called()


class SourceIsolationTests(PublicationCase):
    def test_partial_news_consumes_captured_aliases_without_advancing_read_frontier(self):
        self.sources.add("news", "2026-10-05", news("captured-news"))
        captured = self.capture(news_available=False)
        self.publish(captured)
        baseline = self.coordinator.capture_baseline()["coverage"]["news"]
        self.assertIsNone(baseline["through"])
        self.assertFalse(baseline["complete"])
        self.assertEqual(self.capture().to_dict()["new_news"], {})

    def test_disabled_previously_adopted_stale_kind_never_scans_or_changes_frontier(self):
        self.sources.add("rss", "2026-10-05", rss_item("old-rss"))
        self.publish()
        previous = self.coordinator.capture_baseline()["coverage"]["rss"]
        self.clock += timedelta(days=40)
        self.sources.calls.clear()
        captured = capture_sources(self.sources, self.coordinator.capture_baseline(), self.clock,
            platform_ids=["p"], feed_ids=[], identity_lookup=self.coordinator.known_identities)
        self.assertFalse(any(kind == "rss" for kind, _ in self.sources.calls))
        self.assertNotIn("rss", captured.to_dict()["coverage"])
        self.publish(captured, window="rss-disabled")
        self.assertEqual(self.coordinator.capture_baseline()["coverage"]["rss"], previous)

    def test_disabled_rss_and_independent_first_enable_after_40_days(self):
        self.coordinator.capture_baseline(enabled_kinds=["news"])
        self.clock += timedelta(days=40)
        document, version = self.store.load()
        document["baseline"]["coverage"]["news"]["through"] = (self.clock - timedelta(hours=1)).isoformat()
        self.store.save(document, version)
        self.sources.add("news", self.clock.date().isoformat(), news("healthy"))
        disabled = capture_sources(self.sources, self.coordinator.capture_baseline(enabled_kinds=["news"]),
                                   self.clock, platform_ids=["p"], feed_ids=[])
        self.assertIn("healthy", disabled.to_dict()["new_news"]["p"])
        self.assertNotIn("rss", self.coordinator.capture_baseline()["coverage"])
        self.assertFalse(any(kind == "rss" for kind, day in self.sources.calls))
        baseline = self.coordinator.capture_baseline(enabled_kinds=["news", "rss"])
        self.assertEqual(baseline["coverage"]["rss"]["adoption"]["start_at"],
                         (self.clock - timedelta(hours=24)).isoformat())
        captured = self.capture().to_dict()
        self.assertIn("healthy", captured["new_news"]["p"])
        self.assertTrue(captured["coverage"]["rss"]["complete"])
        self.assertEqual(len([1 for kind, day in self.sources.calls if kind == "rss"]), 2)

    def test_established_stale_rss_is_attention_not_global_failure_or_truncation(self):
        self.sources.add("rss", "2026-10-05", rss_item("known"))
        self.publish()
        old = self.coordinator.capture_baseline()["coverage"]["rss"]
        self.clock += timedelta(days=40)
        document, version = self.store.load()
        document["baseline"]["coverage"]["news"]["through"] = (self.clock - timedelta(hours=1)).isoformat()
        self.store.save(document, version)
        self.sources.add("news", self.clock.date().isoformat(), news("healthy"))
        self.sources.add("rss", self.clock.date().isoformat(), rss_item("fresh-from-stale-source"))
        captured = self.capture(rss_available=False)
        self.assertIn("healthy", captured.to_dict()["new_news"]["p"])
        self.assertFalse(captured.to_dict()["coverage"]["rss"]["complete"])
        self.assertEqual(captured.to_dict()["coverage"]["rss"]["attention"][0]["reason"], "history_gap")
        self.publish(captured, window="40-days-later")
        baseline = self.coordinator.capture_baseline()
        self.assertEqual(baseline["coverage"]["rss"]["through"], old["through"])
        self.assertEqual(baseline["coverage"]["news"]["through"], self.clock.isoformat())
        self.assertEqual(self.capture(rss_available=False).to_dict()["new_rss"], [])

    def test_operator_readoption_preserves_identities_audits_gap_and_fences_old_snapshots(self):
        self.sources.add("rss", "2026-10-05", rss_item("published"))
        self.publish()
        self.sources.days["rss", "2026-10-05"]["items"].append(rss_item("old-unpublished"))
        old_report = self.prepare("old-prepared")
        self.clock += timedelta(days=40)
        status = self.coordinator.status()
        options = {"expected_version": status["version"], "evidence_reference": "gap-ticket-42"}
        before = copy.deepcopy(self.store.document)
        with self.assertRaises(PublicationError):
            self.coordinator.readopt_source("rss", **options)
        self.assertEqual(self.store.document, before)
        self.coordinator.readopt_source("rss", confirmed=True, collection_stopped=True, **options)
        adopted = self.coordinator.capture_baseline()["coverage"]["rss"]
        self.assertEqual(adopted["seen_root"], before["baseline"]["coverage"]["rss"]["seen_root"])
        audit = self.store.get_snapshot(adopted["adoption"]["audit_id"])
        self.assertEqual(audit["previous"]["through"], NOW.isoformat())
        self.coordinator.deliver(old_report, smtp(result(accepted=RECIPIENTS)))
        after = self.coordinator.capture_baseline()["coverage"]["rss"]
        self.assertIsNone(after["through"])
        self.assertEqual(after["adoption"], adopted["adoption"])
        self.assertTrue(self.coordinator.known_identities(after["seen_root"], aliases("rss", rss_item("old-unpublished"))))
        with self.assertRaises(PublicationConflict):
            self.coordinator.readopt_source("rss", confirmed=True, collection_stopped=True, **options)

    def test_partial_feed_published_identity_not_new_but_unread_frontier_stays(self):
        case = fixtures.RunnerTests()
        case.setUp()
        case.sources.add("news", "2026-10-05", news("news"))
        case.sources.add("rss", "2026-10-05", rss_item("rss-already-published"))
        runner, _, dispatcher = case.runner()
        runner._crawl_rss_data.return_value = RSSCollection(False, ("failed-feed",))
        runner.ctx.config["RSS"]["FEEDS"].append({"id": "failed-feed"})
        with redirect_stdout(io.StringIO()):
            runner.run()
            second = runner.prepare_report(CrawlResult({}, {}, []), RSSCollection(True))
        first_snapshot = next(value for value in case.store.snapshots.values() if "email" in value)
        first_item = next(item for group in first_snapshot["input"]["report_view"]["rss"]["stats"]
                          for item in group["titles"] if item["title"] == "rss-already-published")
        second_item = next(item for group in second.rss.stats for item in group["titles"]
                           if item["title"] == "rss-already-published")
        self.assertTrue(first_item["is_new"])
        self.assertFalse(second_item["is_new"])
        self.assertEqual(_item_score(first_item), _item_score(second_item) + 25)
        self.assertIsNone(case.coordinator.capture_baseline()["coverage"]["rss"]["through"])
        dispatcher.send_prepared.assert_called_once()


class ReceiptJournalTests(PublicationCase):
    def test_durable_acceptance_cannot_be_manually_reclassified_for_blind_retry(self):
        report_id = self.prepare()
        def fail_after_receipt(document):
            if document["reports"][report_id]["receipt_count"]:
                raise PublicationError("synthetic manifest outage")
        self.store.before_save = fail_after_receipt
        with self.assertRaises(PublicationError):
            self.coordinator.deliver(report_id, smtp(result(accepted=RECIPIENTS)))
        self.store.before_save = None
        status = self.coordinator.status(report_id)
        with self.assertRaisesRegex(PublicationConflict, "Durable SMTP receipt"):
            self.coordinator.resolve_unknown(report_id, expected_version=status["version"],
                resolutions={r: "not_accepted" for r in RECIPIENTS}, confirmed=True,
                submission_stopped=True, evidence_reference="contradictory-evidence",
                expected_attempt_id=status["reports"][0]["attempt"]["id"])
        self.coordinator.recover_interrupted()
        self.assertEqual(self.coordinator.retryable_reports(), [])

    def test_acceptance_journal_replays_after_manifest_failure_without_smtp(self):
        report_id = self.prepare()
        def fail_after_receipt(document):
            if document["reports"][report_id]["receipt_count"]:
                raise PublicationError("synthetic manifest outage")
        self.store.before_save = fail_after_receipt
        dispatcher = smtp(result(accepted=RECIPIENTS))
        with self.assertRaises(PublicationError):
            self.coordinator.deliver(report_id, dispatcher)
        attempt = self.store.document["reports"][report_id]["attempt"]
        self.assertIn("receipt-" + attempt["id"], self.store.snapshots)
        self.assertEqual(self.coordinator.capture_baseline()["sequence"], 0)
        self.store.before_save = None
        self.coordinator.recover_interrupted()
        self.coordinator.recover_interrupted()
        self.assertEqual(self.coordinator.capture_baseline()["report_id"], report_id)
        self.assertEqual(self.coordinator.status(report_id)["reports"][0]["receipt_count"], 1)
        dispatcher.send_prepared.assert_called_once()

    def test_single_submission_turn_fences_concurrent_smtp_but_allows_collection(self):
        first, second = self.prepare("first"), self.prepare("second")
        blocked = Mock()
        def accept(*args, **kwargs):
            with self.assertRaisesRegex(PublicationError, "Another SMTP attempt"):
                self.coordinator.deliver(second, blocked)
            self.capture()  # hourly read remains available while an attempt owns coverage
            return result(accepted=RECIPIENTS)
        dispatcher = Mock()
        dispatcher.send_prepared.side_effect = accept
        self.coordinator.deliver(first, dispatcher)
        blocked.send_prepared.assert_not_called()
        self.coordinator.deliver(second, smtp(result(accepted=RECIPIENTS)))
        self.assertEqual(self.coordinator.capture_baseline()["report_id"], second)

    def test_interrupted_attempt_blocks_other_sends_until_evidence_resolution(self):
        first, second = self.prepare("first"), self.prepare("second")
        with self.assertRaises(KeyboardInterrupt):
            self.coordinator.deliver(first, smtp(KeyboardInterrupt()))
        self.coordinator.recover_interrupted()
        dispatcher = Mock()
        with self.assertRaises(PublicationError):
            self.coordinator.deliver(second, dispatcher)
        dispatcher.send_prepared.assert_not_called()
        report_status = self.coordinator.status(first)
        self.coordinator.resolve_unknown(first, expected_version=report_status["version"],
            resolutions={r: "permanent_failed" for r in RECIPIENTS}, confirmed=True,
            submission_stopped=True, evidence_reference="verified-rejection-42",
            expected_attempt_id=report_status["reports"][0]["attempt"]["id"])
        self.coordinator.deliver(second, smtp(result(accepted=RECIPIENTS)))
        self.assertEqual(self.coordinator.capture_baseline()["report_id"], second)


if __name__ == "__main__":
    unittest.main()
