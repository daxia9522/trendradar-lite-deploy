"""Offline publication semantics: frozen sources, shared baseline, recipient retries.

All addresses/rows are synthetic. No crawler, SMTP, AI API or S3 call occurs.
"""
from __future__ import annotations

import copy
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from trendradar.ai.selector import _item_score
from trendradar.context import AppContext
from trendradar.core.analyzer import count_word_frequency
from trendradar.core.scheduler import ResolvedSchedule
from trendradar.daily_flow.capture import DailySourceReader, SourceCaptureError, capture_sources
from trendradar.daily_flow.inputs import prepare_captured_hotlist
from trendradar.daily_flow.models import CrawlResult, KeywordRules, RSSCollection
from trendradar.daily_flow.publication import PublicationCoordinator
from trendradar.daily_flow.runner import DailyRunner
from trendradar.notification.models import EmailDeliveryResult, PreparedEmail
from trendradar.storage.base import NewsData, NewsItem, RSSData, RSSItem
from trendradar.storage.local import LocalStorageBackend
from trendradar.storage.publication import LocalPublicationStore, PublicationConflict, PublicationError


TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 5, 7, 0, tzinfo=TZ)
RECIPIENTS = ("a@example.invalid", "b@example.invalid")


def news(title, first="07-00", last=None, url=None):
    return {"source_id": "p", "source_name": "Platform", "title": title,
            "url": url or f"https://example.invalid/{title}", "mobileUrl": "",
            "first_time": first, "last_time": last or first, "ranks": [1],
            "rank": 1, "count": 1, "rank_timeline": []}


def rss_item(title, first="07-00", last=None):
    return {"feed_id": "f", "feed_name": "Feed", "title": title,
            "url": f"https://rss.example.invalid/{title}", "guid": "guid:" + title,
            "first_time": first, "last_time": last or first,
            "published_at": "2026-10-05T00:00:00+08:00", "summary": "", "author": ""}


class MemoryStore:
    """Exact document CAS/snapshot contract; failures never touch network."""
    def __init__(self):
        self.document = None
        self.version = None
        self.snapshots = {}
        self.before_save = None
        self.events = []

    def load(self):
        return copy.deepcopy(self.document), self.version

    def save(self, document, expected_version):
        if self.before_save:
            self.before_save(document)
        if expected_version != self.version:
            raise PublicationConflict("synthetic stale writer")
        self.document = copy.deepcopy(document)
        self.version = (self.version or 0) + 1
        self.events.append("save")
        return self.version

    def put_snapshot(self, snapshot_id, payload):
        if snapshot_id in self.snapshots and self.snapshots[snapshot_id] != payload:
            raise PublicationConflict("immutable snapshot")
        self.snapshots[snapshot_id] = copy.deepcopy(payload)
        self.events.append("snapshot")

    def get_snapshot(self, snapshot_id):
        if snapshot_id not in self.snapshots:
            raise PublicationError("snapshot missing")
        return copy.deepcopy(self.snapshots[snapshot_id])


class SourceFixture:
    def __init__(self):
        self.days = {}
        self.calls = []
        self.errors = set()

    def add(self, kind, date, *items, latest="07-00"):
        self.days[kind, date] = {"date": date, "present": True,
                                 "items": list(items), "latest_time": latest}

    def read_day(self, kind, date):
        self.calls.append((kind, date))
        if (kind, date) in self.errors:
            raise OSError("PRIVATE untrusted source response")
        return copy.deepcopy(self.days.get((kind, date), {
            "date": date, "present": False, "items": [], "latest_time": ""}))


def email(body=b"From: sender@example.invalid\r\n\r\nfrozen body", recipients=RECIPIENTS):
    return PreparedEmail("sender@example.invalid", recipients, "Frozen subject", body,
                         "<stable@example.invalid>", "Mon, 05 Oct 2026 07:00:00 +0800")


def result(requested=RECIPIENTS, accepted=(), temporary_failed=(), permanent_failed=(), unknown=()):
    return EmailDeliveryResult(True, requested, accepted, temporary_failed, permanent_failed, unknown)


def smtp(*receipts):
    dispatcher = Mock()
    dispatcher.send_prepared.side_effect = receipts
    return dispatcher


class PublicationCase(unittest.TestCase):
    def setUp(self):
        self.clock = NOW
        self.store = MemoryStore()
        self.coordinator = PublicationCoordinator(self.store, lambda: self.clock)
        self.sources = SourceFixture()

    def capture(self, **kw):
        return capture_sources(self.sources, self.coordinator.capture_baseline(enabled_kinds=["news", "rss"]), self.clock,
                               platform_ids=["p"], feed_ids=["f"],
                               identity_lookup=self.coordinator.known_identities, **kw)

    def prepare(self, window="morning", capture=None, prepared_email=None):
        report_id = self.coordinator.claim_generation(window)
        self.coordinator.prepare(report_id, prepared_email or email(),
                                 (capture or self.capture()).to_dict())
        return report_id

    def publish(self, capture=None, window="morning"):
        report_id = self.prepare(window, capture)
        self.coordinator.deliver(report_id, smtp(result(accepted=RECIPIENTS)))
        return report_id


class FrozenNoveltyTests(PublicationCase):
    def test_bootstrap_previous_24h_no_midnight_reset(self):
        self.sources.add("news", "2026-10-04", news("old", "06-00"), news("overnight", "23-00"))
        self.sources.add("news", "2026-10-05", news("old"), news("morning"))
        captured = self.capture().to_dict()
        self.assertEqual(set(captured["new_news"]["p"]), {"overnight", "morning"})
        self.assertEqual(captured["bootstrap"]["strategy"], "previous_24h")
        self.assertEqual(captured["bootstrap"]["start_at"], (NOW - timedelta(hours=24)).isoformat())
        self.assertEqual(self.store.document["baseline"]["sequence"], 0)

    def test_hourly_capture_does_not_consume_novelty(self):
        self.sources.add("news", "2026-10-05", news("new"))
        self.sources.add("rss", "2026-10-05", rss_item("new"))
        hourly = self.capture().to_dict()
        self.clock += timedelta(hours=1)
        push = self.capture()
        self.assertEqual(hourly["new_news"], push.to_dict()["new_news"])
        self.assertEqual(hourly["new_rss"], push.to_dict()["new_rss"])
        self.publish(push)
        after = self.capture().to_dict()
        self.assertEqual(after["new_news"], {})
        self.assertEqual(after["new_rss"], [])

    def test_previous_day_22_to_07_includes_off_list_and_crossday_dedup(self):
        self.clock = NOW.replace(day=4, hour=22)
        self.sources.add("news", "2026-10-04", news("known", "22-00"))
        self.sources.add("rss", "2026-10-04", rss_item("known", "22-00"))
        self.publish(window="night")
        self.sources.add("news", "2026-10-04", news("known", "22-00"), news("off-list", "23-00"))
        self.sources.add("rss", "2026-10-04", rss_item("known", "22-00"), rss_item("off-list", "23-00"))
        self.sources.add("news", "2026-10-05", news("known"), news("current"))
        self.sources.add("rss", "2026-10-05", rss_item("known"), rss_item("current"))
        self.clock = NOW
        capture = self.capture().to_dict()
        hotlist = prepare_captured_hotlist(capture, "current")
        self.assertEqual(set(hotlist.results["p"]), {"known", "current"})
        self.assertEqual(set(hotlist.new_titles["p"]), {"off-list", "current"})
        self.assertEqual({item["title"] for item in capture["new_rss"]}, {"off-list", "current"})
        self.assertEqual(hotlist.id_to_name, {"p": "Platform"})

    def test_same_minute_late_items_not_skipped_and_capture_is_immutable(self):
        self.sources.add("news", "2026-10-05", news("first"))
        self.sources.add("rss", "2026-10-05", rss_item("first"))
        captured = self.capture()
        self.sources.days["news", "2026-10-05"]["items"].append(news("late"))
        self.sources.days["rss", "2026-10-05"]["items"].append(rss_item("late"))
        mutated = captured.to_dict()
        mutated["new_news"] = {}
        self.assertIn("first", captured.to_dict()["new_news"]["p"])
        self.publish(captured)
        after = self.capture().to_dict()
        self.assertEqual(set(after["new_news"]["p"]), {"late"})
        self.assertEqual([item["title"] for item in after["new_rss"]], ["late"])

    def test_normalized_url_and_title_aliases_survive_replaced_daily_ids(self):
        self.sources.add("news", "2026-10-05", news("same", url="https://example.invalid/story?utm_source=one"))
        self.publish()
        self.sources.add("news", "2026-10-05", news("renamed", url="https://example.invalid/story?utm_source=two"))
        self.assertEqual(self.capture().to_dict()["new_news"], {})
        self.sources.add("news", "2026-10-05", news("same", url="https://example.invalid/different"))
        self.assertEqual(self.capture().to_dict()["new_news"], {})

    def test_failed_rss_capture_keeps_separate_frontier(self):
        self.sources.add("rss", "2026-10-05", rss_item("pending"))
        captured = self.capture(rss_available=False)
        self.publish(captured)
        coverage = self.coordinator.capture_baseline()["coverage"]
        self.assertIn("news", coverage)
        self.assertIsNone(coverage["rss"]["through"])
        self.assertFalse(coverage["rss"]["complete"])
        self.clock += timedelta(hours=1)
        self.assertEqual(self.capture().to_dict()["new_rss"], [])

    def test_unreadable_news_not_converted_to_empty_coverage(self):
        self.sources.errors.add(("news", "2026-10-04"))
        output = io.StringIO()
        with redirect_stdout(output):
            captured = self.capture()
            self.publish(captured)
        self.assertIsNone(self.coordinator.capture_baseline()["coverage"]["news"]["through"])
        self.assertNotIn("PRIVATE", output.getvalue())
        self.assertFalse(captured.to_dict()["coverage"]["news"]["complete"])

    def test_stale_history_is_bounded_fail_closed(self):
        self.coordinator.capture_baseline(enabled_kinds=["news", "rss"])
        self.clock += timedelta(days=40)
        capture = self.capture().to_dict()
        self.assertFalse(capture["coverage"]["news"]["complete"])
        self.assertEqual(capture["coverage"]["news"]["attention"][0]["reason"], "history_gap")

    def test_disappeared_database_inside_published_interval_cannot_advance(self):
        self.sources.add("news", "2026-10-05", news("first"))
        self.publish()
        before = self.coordinator.capture_baseline()["coverage"]["news"]
        self.sources.days.clear()
        self.clock += timedelta(days=1)
        captured = self.capture()
        self.assertFalse(captured.to_dict()["coverage"]["news"]["complete"])
        self.publish(captured, window="next-day")
        after = self.coordinator.capture_baseline()["coverage"]["news"]
        self.assertEqual(after["through"], before["through"])
        self.assertEqual(after["seen_root"], before["seen_root"])
        self.assertFalse(after["complete"])


class LifecycleTests(PublicationCase):
    def test_explicit_force_can_create_new_generation_but_normal_run_cannot(self):
        first = self.coordinator.claim_generation("morning")
        self.assertIsNone(self.coordinator.claim_generation("morning"))
        forced = self.coordinator.claim_generation("morning", force=True)
        self.assertNotEqual(first, forced)
        self.assertEqual(self.store.document["reports"][first]["state"], "GENERATING")
        self.assertEqual(self.store.document["windows"]["morning"], forced)

    def test_partial_publishes_and_retry_uses_exact_mime_and_subset(self):
        report_id = self.prepare()
        dispatcher = smtp(result(accepted=RECIPIENTS[:1], temporary_failed=RECIPIENTS[1:]),
                          result(requested=RECIPIENTS[1:], accepted=RECIPIENTS[1:]))
        first = self.coordinator.deliver(report_id, dispatcher)
        self.assertFalse(first.sent)
        self.assertEqual(self.coordinator.capture_baseline()["report_id"], report_id)
        self.assertEqual(self.coordinator.retryable_reports(), [report_id])
        self.coordinator.deliver(report_id, dispatcher)
        calls = dispatcher.send_prepared.call_args_list
        self.assertEqual(calls[0].args[0].to_dict(), calls[1].args[0].to_dict())
        self.assertEqual(calls[1].kwargs["recipients"], RECIPIENTS[1:])
        self.assertEqual(self.store.document["reports"][report_id]["state"], "DELIVERED")
        self.assertIsNone(self.coordinator.claim_generation("morning"))
        self.assertEqual(sum("email" in snapshot for snapshot in self.store.snapshots.values()), 1)

    def test_all_refusal_no_publication_and_only_temporary_retry(self):
        report_id = self.prepare()
        self.coordinator.deliver(report_id, smtp(result(temporary_failed=RECIPIENTS[:1], permanent_failed=RECIPIENTS[1:])))
        self.assertEqual(self.coordinator.capture_baseline()["sequence"], 0)
        dispatcher = smtp(result(requested=RECIPIENTS[:1], accepted=RECIPIENTS[:1]))
        self.coordinator.deliver(report_id, dispatcher)
        self.assertEqual(dispatcher.send_prepared.call_args.kwargs["recipients"], RECIPIENTS[:1])
        self.assertTrue(self.store.document["reports"][report_id]["published"])
        self.assertEqual(self.store.document["reports"][report_id]["state"], "ATTENTION")

    def test_permanent_and_unknown_never_automatically_retried(self):
        report_id = self.prepare()
        self.coordinator.deliver(report_id, smtp(result(permanent_failed=RECIPIENTS[:1], unknown=RECIPIENTS[1:])))
        self.assertEqual(self.coordinator.retryable_reports(), [])
        self.assertIsNone(self.coordinator.deliver(report_id, Mock()))
        with self.assertRaises(PublicationError):
            self.coordinator.claim_generation("morning", force=True)

    def test_exception_after_entering_dispatcher_is_unknown(self):
        report_id = self.prepare()
        dispatcher = smtp(RuntimeError("PRIVATE SMTP response"))
        output = io.StringIO()
        with redirect_stdout(output):
            receipt = self.coordinator.deliver(report_id, dispatcher)
        self.assertEqual(receipt.unknown, RECIPIENTS)
        self.assertNotIn("PRIVATE", output.getvalue())
        self.assertNotIn("@", output.getvalue())
        self.assertEqual(self.coordinator.retryable_reports(), [])

    def test_crash_after_attempt_claim_becomes_unknown(self):
        report_id = self.prepare()
        with self.assertRaises(KeyboardInterrupt):
            self.coordinator.deliver(report_id, smtp(KeyboardInterrupt()))
        restarted = PublicationCoordinator(self.store, lambda: self.clock)
        self.assertEqual(restarted.recover_interrupted(), 1)
        self.assertEqual(restarted.retryable_reports(), [])
        self.assertEqual(set(self.store.document["reports"][report_id]["outcomes"].values()), {"unknown"})

    def test_manifest_failure_defers_publication_until_durable_journal_reconciliation(self):
        report_id = self.prepare()
        def fail_receipt(document):
            if document["reports"][report_id]["receipts"]:
                raise PublicationError("synthetic uncertain write")
        self.store.before_save = fail_receipt
        with self.assertRaises(PublicationError):
            self.coordinator.deliver(report_id, smtp(result(accepted=RECIPIENTS)))
        self.store.before_save = None
        self.assertEqual(self.coordinator.capture_baseline()["sequence"], 0)
        self.coordinator.recover_interrupted()
        self.assertEqual(self.coordinator.capture_baseline()["report_id"], report_id)
        self.assertEqual(self.coordinator.retryable_reports(), [])

    def test_cas_or_ambiguous_claim_failure_prevents_smtp(self):
        report_id = self.prepare()
        dispatcher = Mock()
        def fail_claim(document):
            if document["reports"][report_id]["attempt"]:
                raise PublicationConflict("stale synthetic claim")
        self.store.before_save = fail_claim
        with self.assertRaises(PublicationConflict):
            self.coordinator.deliver(report_id, dispatcher)
        dispatcher.send_prepared.assert_not_called()
        self.assertEqual(self.store.document["reports"][report_id]["state"], "PREPARED")

    def test_snapshot_missing_prevents_smtp_and_claim(self):
        report_id = self.prepare()
        self.store.snapshots.clear()
        dispatcher = Mock()
        with self.assertRaises(PublicationError):
            self.coordinator.deliver(report_id, dispatcher)
        dispatcher.send_prepared.assert_not_called()
        self.assertIsNone(self.store.document["reports"][report_id]["attempt"])

    def test_receipt_cas_conflict_retries_storage_not_smtp(self):
        report_id = self.prepare()
        conflicted = []
        def conflict_once(document):
            if document["reports"][report_id]["receipts"] and not conflicted:
                conflicted.append(True)
                self.store.version += 1
                raise PublicationConflict("concurrent unrelated update")
        self.store.before_save = conflict_once
        dispatcher = smtp(result(accepted=RECIPIENTS))
        self.coordinator.deliver(report_id, dispatcher)
        dispatcher.send_prepared.assert_called_once()
        self.assertTrue(self.store.document["reports"][report_id]["published"])

    def test_prepare_failure_cannot_leave_a_sendable_report(self):
        report_id = self.coordinator.claim_generation("morning")
        def fail_prepared(document):
            if document["reports"][report_id]["state"] == "PREPARED":
                raise PublicationConflict("lost preparation CAS")
        self.store.before_save = fail_prepared
        with self.assertRaises(PublicationConflict):
            self.coordinator.prepare(report_id, email(), self.capture().to_dict())
        self.assertIn(report_id, self.store.snapshots)
        self.assertEqual(self.coordinator.retryable_reports(), [])
        dispatcher = Mock()
        self.assertIsNone(self.coordinator.deliver(report_id, dispatcher))
        dispatcher.send_prepared.assert_not_called()

    def test_old_retry_never_rewinds_newer_baseline(self):
        self.sources.add("news", "2026-10-05", news("first"))
        old = self.prepare("morning")
        self.coordinator.deliver(old, smtp(result(accepted=RECIPIENTS[:1], temporary_failed=RECIPIENTS[1:])))
        self.clock += timedelta(hours=5)
        self.sources.days["news", "2026-10-05"]["items"].append(news("noon", "12-00"))
        newer = self.publish(window="noon")
        baseline = self.coordinator.capture_baseline()
        self.coordinator.deliver(old, smtp(result(requested=RECIPIENTS[1:], accepted=RECIPIENTS[1:])))
        after = self.coordinator.capture_baseline()
        self.assertEqual(after["report_id"], newer)
        self.assertEqual(after["coverage"]["news"]["through"], baseline["coverage"]["news"]["through"])
        self.assertEqual(after["coverage"]["news"]["seen_root"], baseline["coverage"]["news"]["seen_root"])

    def test_live_owner_can_commit_receipt_after_conservative_recovery(self):
        report_id = self.prepare()
        dispatcher = Mock()
        def deliver(*args, **kw):
            self.coordinator.recover_interrupted()
            return result(accepted=RECIPIENTS)
        dispatcher.send_prepared.side_effect = deliver
        self.coordinator.deliver(report_id, dispatcher)
        self.assertTrue(self.store.document["reports"][report_id]["published"])
        self.assertEqual(self.store.document["reports"][report_id]["state"], "DELIVERED")

    def test_local_reopen_retries_stored_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            first = LocalPublicationStore(directory)
            coordinator = PublicationCoordinator(first, lambda: NOW)
            report_id = coordinator.claim_generation("morning")
            coordinator.prepare(report_id, email(), self.capture().to_dict())
            coordinator.deliver(report_id, smtp(result(temporary_failed=RECIPIENTS)))
            first.close()
            second = LocalPublicationStore(directory)
            restarted = PublicationCoordinator(second, lambda: NOW)
            dispatcher = smtp(result(accepted=RECIPIENTS))
            restarted.deliver(report_id, dispatcher)
            self.assertEqual(dispatcher.send_prepared.call_args.args[0].mime_bytes, email().mime_bytes)
            self.assertEqual(restarted.capture_baseline()["report_id"], report_id)
            second.close()


class OperatorResolutionTests(PublicationCase):
    def unknown_report(self):
        report_id = self.prepare()
        self.coordinator.deliver(report_id, smtp(result(unknown=RECIPIENTS)))
        return report_id

    def resolve(self, report_id, resolutions, **overrides):
        arguments = {
            "expected_version": self.coordinator.status(report_id)["version"],
            "resolutions": resolutions, "confirmed": True, "submission_stopped": True,
            "evidence_reference": "smtp-admin-ticket-42", "actor": "test-operator",
        }
        arguments.update(overrides)
        return self.coordinator.resolve_unknown(report_id, **arguments)

    def test_status_of_missing_ledger_is_read_only(self):
        self.assertFalse(self.coordinator.status()["initialized"])
        self.assertIsNone(self.store.document)
        self.assertEqual(self.store.events, [])

    def test_status_redacts_envelope_and_never_recovers_inflight(self):
        report_id = self.prepare()
        with self.assertRaises(KeyboardInterrupt):
            self.coordinator.deliver(report_id, smtp(KeyboardInterrupt()))
        before = copy.deepcopy(self.store.document)
        version = self.store.version
        status = self.coordinator.status(report_id)
        self.assertEqual(status["reports"][0]["attempt"]["state"], "INFLIGHT")
        self.assertNotIn("@", json.dumps(status))
        self.assertNotIn("Frozen subject", json.dumps(status))
        self.assertNotIn("frozen body", json.dumps(status))
        self.assertEqual(self.store.document, before)
        self.assertEqual(self.store.version, version)
        explicit = self.coordinator.status(report_id, include_recipients=True)
        self.assertEqual(set(explicit["reports"][0]["outcomes"]), set(RECIPIENTS))

    def test_verified_acceptance_publishes_without_smtp_and_other_recipient_stays_unknown(self):
        report_id = self.unknown_report()
        snapshot = self.store.get_snapshot(report_id)
        output = io.StringIO()
        with redirect_stdout(output):
            self.resolve(report_id, {RECIPIENTS[0]: "accepted"})
        self.assertEqual(self.coordinator.capture_baseline()["report_id"], report_id)
        report = self.store.document["reports"][report_id]
        self.assertEqual(report["outcomes"][RECIPIENTS[1]], "unknown")
        self.assertEqual(report["receipts"][-1]["source"], "operator_verified")
        self.assertEqual(report["receipts"][-1]["evidence_reference"], "smtp-admin-ticket-42")
        self.assertEqual(self.coordinator.retryable_reports(), [])
        self.assertEqual(self.store.get_snapshot(report_id), snapshot)
        self.assertNotIn("@", output.getvalue())

    def test_verified_nonacceptance_enables_only_that_recipient_frozen_retry(self):
        report_id = self.unknown_report()
        self.resolve(report_id, {RECIPIENTS[0]: "not_accepted"})
        self.assertEqual(self.coordinator.capture_baseline()["sequence"], 0)
        dispatcher = smtp(result(requested=RECIPIENTS[:1], accepted=RECIPIENTS[:1]))
        self.coordinator.deliver(report_id, dispatcher)
        self.assertEqual(dispatcher.send_prepared.call_args.kwargs["recipients"], RECIPIENTS[:1])
        self.assertEqual(dispatcher.send_prepared.call_args.args[0].to_dict(), email().to_dict())
        self.assertEqual(self.store.document["reports"][report_id]["outcomes"][RECIPIENTS[1]], "unknown")

    def test_resolution_requires_confirmation_stopped_worker_evidence_and_fresh_version(self):
        report_id = self.unknown_report()
        for overrides in ({"confirmed": False}, {"submission_stopped": False},
                          {"evidence_reference": ""}, {"expected_version": -1}):
            before = copy.deepcopy(self.store.document)
            with self.subTest(overrides=overrides):
                with self.assertRaises((PublicationError, ValueError)):
                    self.resolve(report_id, {RECIPIENTS[0]: "not_accepted"}, **overrides)
                self.assertEqual(self.store.document, before)
        with self.assertRaises(PublicationError):
            self.resolve(report_id, {"outside@example.invalid": "accepted"})
        with self.assertRaises(ValueError):
            self.resolve(report_id, {RECIPIENTS[0]: "mailbox_empty"})

    def test_inflight_resolution_fences_late_original_receipt(self):
        report_id = self.prepare()
        with self.assertRaises(KeyboardInterrupt):
            self.coordinator.deliver(report_id, smtp(KeyboardInterrupt()))
        attempt_id = self.coordinator.status(report_id)["reports"][0]["attempt"]["id"]
        with self.assertRaises(PublicationConflict):
            self.resolve(report_id, {RECIPIENTS[0]: "accepted"})
        self.resolve(report_id, {RECIPIENTS[0]: "accepted"}, expected_attempt_id=attempt_id)
        self.assertIsNone(self.store.document["reports"][report_id]["attempt"])
        with self.assertRaises(PublicationConflict):
            self.coordinator._commit_receipt(report_id, attempt_id, result(accepted=RECIPIENTS),
                                             self.store.get_snapshot(report_id))
        self.assertEqual(self.store.document["reports"][report_id]["outcomes"][RECIPIENTS[1]], "unknown")

    def test_old_manual_acceptance_never_rewinds_newer_publication(self):
        self.sources.add("news", "2026-10-05", news("first"))
        old = self.unknown_report()
        self.clock += timedelta(hours=5)
        self.sources.days["news", "2026-10-05"]["items"].append(news("later", "12-00"))
        latest = self.publish(window="noon")
        before = self.coordinator.capture_baseline()
        self.resolve(old, {RECIPIENTS[0]: "accepted"})
        self.assertEqual(self.coordinator.capture_baseline(), before)
        self.assertEqual(before["report_id"], latest)
        with self.assertRaises(PublicationError):
            self.resolve(old, {RECIPIENTS[0]: "accepted"})

    def test_release_generation_is_explicit_cas_fenced_and_refuses_prepared_mail(self):
        report_id = self.coordinator.claim_generation("morning")
        status = self.coordinator.status(report_id)
        arguments = {"expected_version": status["version"], "evidence_reference": "worker-stop-42"}
        with self.assertRaises(PublicationError):
            self.coordinator.release_generation(report_id, **arguments)
        self.coordinator.release_generation(report_id, confirmed=True, generation_stopped=True, **arguments)
        with self.assertRaises(PublicationConflict):
            self.coordinator.prepare(report_id, email(), self.capture().to_dict())
        replacement = self.prepare("morning")
        with self.assertRaises(PublicationError):
            self.coordinator.release_generation(replacement, expected_version=self.store.version,
                                                confirmed=True, generation_stopped=True,
                                                evidence_reference="worker-stop-43")
        self.assertEqual(self.store.document["windows"]["morning"], replacement)


class RunnerTests(PublicationCase):
    def runner(self, mode="current", push=True):
        config = {
            "PLATFORMS": [{"id": "p"}], "REPORT_MODE": mode, "TIMEZONE": "Asia/Shanghai",
            "ENABLE_CRAWLER": True, "ENABLE_NOTIFICATION": True,
            "EMAIL_FROM": "sender@example.invalid", "EMAIL_PASSWORD": "test-only",
            "EMAIL_TO": ",".join(RECIPIENTS),
            "AI_ANALYSIS": {"ENABLED": True},
            "WEIGHT_CONFIG": {"RANK_WEIGHT": 0.4, "FREQUENCY_WEIGHT": 0.3, "HOTNESS_WEIGHT": 0.3},
            "RSS": {"ENABLED": True, "FEEDS": [{"id": "f"}], "FRESHNESS_FILTER": {"ENABLED": False}},
            "STORAGE": {"FORMATS": {"HTML": True}},
        }
        runner = DailyRunner.__new__(DailyRunner)
        runner.ctx = AppContext(config)
        runner.ctx.get_time = lambda: self.clock
        runner.ctx.load_frequency_words = Mock(return_value=([], [], ["BLOCK"]))
        runner.ctx.create_publication_source_reader = lambda: self.sources
        runner.ctx.generate_html = Mock(return_value="synthetic-frozen-report.html")
        runner.ctx.cleanup = Mock()
        runner.report_mode = mode
        runner.frequency_file = None
        runner.rank_threshold = 50
        runner.is_docker_container = True
        runner.is_github_actions = False
        runner._publication = self.coordinator
        runner._manual_force_run = lambda: False
        runner._run_ai_analysis = Mock(return_value=None)
        runner._should_open_browser = lambda: False
        schedule = ResolvedSchedule(
            period_key="morning", period_name="早间", day_plan="test", collect=True,
            analyze=True, push=push, report_mode=mode, once_analyze=True, once_push=True,
            frequency_file=None,
        )
        scheduler = Mock()
        scheduler.resolve.return_value = schedule
        runner.ctx.create_scheduler = lambda: scheduler
        runner._crawl_data = Mock(return_value=CrawlResult({}, {}, []))
        runner._crawl_rss_data = Mock(return_value=RSSCollection(True))
        dispatcher = Mock()
        dispatcher.prepare_report.return_value = email()
        dispatcher.send_prepared.return_value = result(accepted=RECIPIENTS)
        runner.ctx.create_notification_dispatcher = lambda: dispatcher
        return runner, schedule, dispatcher

    def test_main_pools_unchanged_and_unified_is_new_reaches_ai_plus25(self):
        self.sources.add("news", "2026-10-05", news("earlier", "06-00"), news("current"))
        self.sources.add("rss", "2026-10-05", rss_item("earlier", "06-00"), rss_item("current"))
        for mode, expected in (("current", {"current"}), ("daily", {"earlier", "current"})):
            with self.subTest(mode=mode):
                runner, schedule, _ = self.runner(mode)
                prepared = runner.prepare_report(CrawlResult({}, {}, []), RSSCollection(True))
                runner.analyze_report(prepared, schedule)
                stats, rss_stats = runner._run_ai_analysis.call_args.args[:2]
                for group in (stats, rss_stats):
                    items = [item for stat in group for item in stat["titles"]]
                    self.assertEqual({item["title"] for item in items}, expected)
                    for item in items:
                        self.assertTrue(item["is_new"])
                        self.assertEqual(_item_score(item), _item_score({**item, "is_new": False}) + 25)
                runner.ctx.load_frequency_words.assert_called_once()

    def test_new_only_hotlist_and_rss_can_send_with_empty_current_pool(self):
        for kind in ("news", "rss"):
            with self.subTest(kind=kind):
                self.setUp()
                row = news("overnight", "23-00") if kind == "news" else rss_item("overnight", "23-00")
                self.sources.add(kind, "2026-10-04", row)
                runner, _, dispatcher = self.runner()
                runner.run()
                dispatcher.send_prepared.assert_called_once()
                runner._run_ai_analysis.assert_not_called()
                self.assertGreater(self.coordinator.capture_baseline()["sequence"], 0)
                html = runner.ctx.generate_html.call_args
                self.assertFalse(any(stat["titles"] for stat in html.args[0]))

    def test_nonmatching_new_only_does_not_send(self):
        self.sources.add("news", "2026-10-04", news("BLOCK overnight", "23-00"))
        self.sources.add("rss", "2026-10-04", rss_item("BLOCK overnight", "23-00"))
        runner, _, dispatcher = self.runner()
        runner.run()
        dispatcher.send_prepared.assert_not_called()
        self.assertEqual(self.coordinator.capture_baseline()["sequence"], 0)

    def test_partial_retry_preserves_scheduled_collection_but_no_new_ai_render_or_prepare(self):
        self.sources.add("news", "2026-10-05", news("current"))
        runner, _, dispatcher = self.runner()
        dispatcher.send_prepared.side_effect = [
            result(accepted=RECIPIENTS[:1], temporary_failed=RECIPIENTS[1:]),
            result(requested=RECIPIENTS[1:], accepted=RECIPIENTS[1:]),
        ]
        runner.run()
        runner.run()
        self.assertEqual(runner._crawl_data.call_count, 2)
        self.assertEqual(runner._crawl_rss_data.call_count, 2)
        runner._run_ai_analysis.assert_called_once()
        runner.ctx.generate_html.assert_called_once()
        dispatcher.prepare_report.assert_called_once()
        self.assertEqual(dispatcher.send_prepared.call_count, 2)
        self.assertEqual(dispatcher.send_prepared.call_args.kwargs["recipients"], RECIPIENTS[1:])

    def test_retry_only_schedule_never_crawls_analyzes_or_prepares_again(self):
        self.sources.add("news", "2026-10-05", news("current"))
        runner, schedule, dispatcher = self.runner()
        dispatcher.send_prepared.side_effect = [
            result(accepted=RECIPIENTS[:1], temporary_failed=RECIPIENTS[1:]),
            result(requested=RECIPIENTS[1:], accepted=RECIPIENTS[1:]),
        ]
        runner.run()
        runner.ctx.create_scheduler().resolve.return_value = replace(schedule, collect=False)
        runner.run()
        runner._crawl_data.assert_called_once()
        runner._crawl_rss_data.assert_called_once()
        runner._run_ai_analysis.assert_called_once()
        runner.ctx.generate_html.assert_called_once()
        dispatcher.prepare_report.assert_called_once()
        self.assertEqual(dispatcher.send_prepared.call_count, 2)

    def test_0705_after_published_window_still_collects_and_late_data_reaches_noon(self):
        self.sources.add("news", "2026-10-05", news("first"))
        runner, schedule, dispatcher = self.runner()
        runner.run()
        self.clock += timedelta(minutes=5)
        self.sources.days["news", "2026-10-05"]["items"].append(news("collected-at-0705", "07-05"))
        runner.run()
        self.assertEqual(runner._crawl_data.call_count, 2)
        self.assertEqual(runner._crawl_rss_data.call_count, 2)
        runner._run_ai_analysis.assert_called_once()
        dispatcher.send_prepared.assert_called_once()
        self.assertEqual(sum("email" in snapshot for snapshot in self.store.snapshots.values()), 1)
        self.clock = NOW.replace(hour=12)
        runner.ctx.create_scheduler().resolve.return_value = replace(schedule, period_key="noon")
        runner.run()
        latest_id = self.coordinator.capture_baseline()["report_id"]
        self.assertEqual(set(self.store.get_snapshot(latest_id)["input"]["new_news"]["p"]), {"collected-at-0705"})
        self.assertEqual(runner._crawl_data.call_count, 3)

    def test_no_email_reasons_release_window_for_later_first_publication(self):
        for reason in ("empty", "no_html", "not_configured"):
            with self.subTest(reason=reason):
                self.setUp()
                runner, _, dispatcher = self.runner()
                if reason != "empty":
                    self.sources.add("news", "2026-10-05", news("later-valid"))
                if reason == "no_html":
                    runner.ctx.config["STORAGE"]["FORMATS"]["HTML"] = False
                if reason == "not_configured":
                    dispatcher.prepare_report.return_value = None
                runner.run()
                first = next(iter(self.store.document["reports"].values()))
                self.assertEqual(first["state"], "NO_EMAIL")
                self.assertEqual(first["reason"], reason)
                self.assertEqual(self.store.document["windows"], {})
                dispatcher.send_prepared.assert_not_called()
                self.sources.add("news", "2026-10-05", news("later-valid"))
                runner.ctx.config["STORAGE"]["FORMATS"]["HTML"] = True
                dispatcher.prepare_report.return_value = email()
                runner.run()
                dispatcher.send_prepared.assert_called_once()
                self.assertNotEqual(self.coordinator.capture_baseline()["report_id"], first["id"])

    def test_render_failure_releases_only_unprepared_generation_for_same_window_retry(self):
        self.sources.add("news", "2026-10-05", news("current"))
        runner, _, dispatcher = self.runner()
        runner.ctx.generate_html.side_effect = RuntimeError("synthetic renderer failure")
        with self.assertRaises(RuntimeError):
            runner.run()
        first = next(iter(self.store.document["reports"].values()))
        self.assertEqual(first["state"], "GENERATION_FAILED")
        self.assertEqual(self.store.document["windows"], {})
        dispatcher.send_prepared.assert_not_called()
        runner.ctx.generate_html.side_effect = None
        runner.run()
        dispatcher.send_prepared.assert_called_once()
        self.assertGreater(self.coordinator.capture_baseline()["sequence"], first["sequence"])

    def test_collection_failure_does_not_disable_independent_snapshot_retry(self):
        self.sources.add("news", "2026-10-05", news("current"))
        runner, _, dispatcher = self.runner()
        dispatcher.send_prepared.side_effect = [
            result(temporary_failed=RECIPIENTS), result(accepted=RECIPIENTS),
        ]
        runner.run()
        runner._crawl_data.side_effect = RuntimeError("synthetic crawler failure")
        with self.assertRaises(RuntimeError):
            runner.run()
        self.assertEqual(dispatcher.send_prepared.call_count, 2)
        runner.ctx.generate_html.assert_called_once()
        self.assertEqual(sum("email" in snapshot for snapshot in self.store.snapshots.values()), 1)

    def test_disabled_crawler_still_allows_snapshot_only_retry(self):
        self.sources.add("news", "2026-10-05", news("current"))
        runner, _, dispatcher = self.runner()
        dispatcher.send_prepared.side_effect = [
            result(temporary_failed=RECIPIENTS), result(accepted=RECIPIENTS),
        ]
        runner.run()
        runner.ctx.config["ENABLE_CRAWLER"] = False
        runner.run()
        runner._crawl_data.assert_called_once()
        runner.ctx.generate_html.assert_called_once()
        self.assertEqual(dispatcher.send_prepared.call_count, 2)

    def test_hourly_does_not_retry_pending_smtp(self):
        self.prepare()
        runner, _, dispatcher = self.runner(push=False)
        runner.run()
        dispatcher.send_prepared.assert_not_called()
        self.assertEqual(self.coordinator.capture_baseline()["sequence"], 0)
        runner._crawl_data.assert_called_once()

    def test_ai_and_smtp_delay_do_not_expand_capture_or_reread_keywords(self):
        self.sources.add("news", "2026-10-05", news("current"))
        runner, _, dispatcher = self.runner()
        def ai(*args, **kwargs):
            self.sources.days["news", "2026-10-05"]["items"].append(news("arrived-during-ai"))
            self.clock += timedelta(hours=1)
        runner._run_ai_analysis.side_effect = ai
        def deliver(*args, **kwargs):
            self.sources.days["news", "2026-10-05"]["items"].append(news("arrived-during-smtp"))
            self.clock += timedelta(hours=1)
            return result(accepted=RECIPIENTS)
        dispatcher.send_prepared.side_effect = deliver
        runner.run()
        snapshot = next(value for value in self.store.snapshots.values() if "email" in value)
        self.assertEqual(set(snapshot["input"]["new_news"]["p"]), {"current"})
        self.assertEqual(snapshot["input"]["coverage"]["news"]["through"], NOW.isoformat())
        after = self.capture().to_dict()
        self.assertEqual(set(after["new_news"]["p"]), {"arrived-during-ai", "arrived-during-smtp"})
        runner.ctx.load_frequency_words.assert_called_once()

    def test_legacy_push_markers_do_not_seed_or_block_first_adoption(self):
        self.sources.add("news", "2026-10-05", news("new"))
        runner, _, dispatcher = self.runner()
        scheduler = runner.ctx.create_scheduler()
        scheduler.already_executed.return_value = True
        runner.run()
        dispatcher.send_prepared.assert_called_once()
        scheduler.already_executed.assert_not_called()

    def test_real_html_uses_captured_keywords_timestamp_and_interval_names(self):
        from trendradar.report.generator import generate_html_report
        self.sources.add("news", "2026-10-04", news("overnight", "23-00"))
        self.sources.add("rss", "2026-10-04", rss_item("rss-overnight", "23-00"))
        runner, schedule, _ = self.runner()
        prepared = runner.prepare_report(CrawlResult({}, {}, []), RSSCollection(True))
        runner.ctx.load_frequency_words.side_effect = AssertionError("No config reread after capture")
        runner.ctx.generate_html = AppContext.generate_html.__get__(runner.ctx)
        with tempfile.TemporaryDirectory() as directory:
            with patch("trendradar.context.generate_html_report", side_effect=lambda **kw: generate_html_report(
                **{**kw, "output_dir": directory}
            )):
                artifacts = runner.analyze_report(prepared, schedule)
            html = Path(artifacts.html_file).read_text()
            self.assertIn("overnight", html)
            self.assertIn("rss-overnight", html)
            self.assertIn("Platform", html)
            self.assertIn("2026-10-05", html)
        runner.ctx.load_frequency_words.assert_called_once()

    def test_incremental_uses_publication_delta_not_first_crawl_reset(self):
        self.sources.add("news", "2026-10-05", news("known"))
        self.publish()
        self.sources.days["news", "2026-10-05"]["items"].append(news("new"))
        runner, schedule, _ = self.runner(mode="incremental")
        runner.ctx.is_first_crawl = Mock(side_effect=AssertionError("No live first-crawl read"))
        prepared = runner.prepare_report(CrawlResult({}, {}, []), RSSCollection(True))
        artifacts = runner.analyze_report(prepared, schedule)
        self.assertEqual({item["title"] for stat in artifacts.stats for item in stat["titles"]}, {"new"})
        runner.ctx.is_first_crawl.assert_not_called()


class StrictSourceReaderTests(unittest.TestCase):
    def test_local_reader_and_remote_fresh_object_capture_same_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = LocalStorageBackend(directory)
            backend.save_news_data(NewsData("2026-10-05", "07-00", {"p": [NewsItem(
                "synthetic", "p", "Platform", 2, "https://example.invalid/a", crawl_time="07-00")]}, {"p": "Platform"}))
            backend.save_rss_data(RSSData("2026-10-05", "07-00", {"f": [RSSItem(
                "synthetic", "f", "Feed", "https://rss.example.invalid/a", guid="a", crawl_time="07-00")]}, {"f": "Feed"}))
            reader = DailySourceReader(backend)
            local_rows = {kind: reader.read_day(kind, "2026-10-05") for kind in ("news", "rss")}
            # Closing checkpoints WAL before a fake object upload, just like a
            # standalone SQLite object (no production remote backend involved).
            backend.cleanup()
            client = Mock()
            def get_object(**kw):
                data = (Path(directory) / kw["Key"]).read_bytes()
                return {"Body": io.BytesIO(data), "ContentLength": len(data)}
            client.get_object.side_effect = get_object
            remote = DailySourceReader(SimpleNamespace(backend_name="remote", s3_client=client, bucket_name="test"))
            for kind in ("news", "rss"):
                self.assertEqual(remote.read_day(kind, "2026-10-05"), local_rows[kind])
            self.assertEqual(client.get_object.call_count, 2)

    def test_missing_is_distinct_from_corrupt_and_remote_denial(self):
        with tempfile.TemporaryDirectory() as directory:
            reader = DailySourceReader(SimpleNamespace(backend_name="local", data_dir=directory))
            self.assertFalse(reader.read_day("news", "2026-10-05")["present"])
            path = Path(directory) / "news" / "2026-10-05.db"
            path.parent.mkdir()
            path.write_bytes(b"corrupt source")
            with self.assertRaises(sqlite3.DatabaseError):
                reader.read_day("news", "2026-10-05")
        client = Mock()
        client.get_object.side_effect = RuntimeError("PRIVATE remote response")
        reader = DailySourceReader(SimpleNamespace(backend_name="remote", s3_client=client, bucket_name="test"))
        with self.assertRaisesRegex(SourceCaptureError, "Remote source read failed") as raised:
            reader.read_day("news", "2026-10-05")
        self.assertNotIn("PRIVATE", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
