"""Daily publication ledger, independent of schedule execution markers.

A live generation claim owns a window; empty/failed pre-snapshot generations
release it. SMTP attempts require a second durable claim. Interrupted attempts
are UNKNOWN, never a retry queue. Operator resolution is audited and CAS-fenced.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

from trendradar.notification.models import EmailDeliveryResult, PreparedEmail
from trendradar.storage.publication import DOCUMENT_LIMIT, SNAPSHOT_LIMIT, PublicationConflict, PublicationError
from .publication_index import SnapshotIndex, encoded


SCHEMA = 2
MAX_LIVE_REPORTS = 32
KEEP_TERMINAL_REPORTS = 16
REPORT_RESERVATION = 128 * 1024
ENVELOPE_LIMIT = 16 * 1024
OUTCOMES = ("accepted", "temporary_failed", "permanent_failed", "unknown")
RELEASABLE_STATES = {"NO_EMAIL", "GENERATION_FAILED"}


class PublicationCoordinator:
    def __init__(self, store, now):
        self.store = store
        self.now = now
        self.index = SnapshotIndex(store)

    def _load(self):
        document, version = self.store.load()
        if document is None:
            document = {
                "schema": SCHEMA, "stream": "daily", "next_sequence": 1,
                "bootstrap": {"strategy": "previous_24h", "start_at": (
                    self.now() - timedelta(hours=24)).isoformat()},
                "baseline": {"sequence": 0, "report_id": None, "coverage": {}},
                "windows": {}, "reports": {},
                "archives": {"reports": None, "sequences": None, "windows": None},
            }
            version = self.store.save(document, expected_version=None)
        self._validate_document(document)
        document = deepcopy(document)
        if document["schema"] == 1 or any("seen" in c for c in document["baseline"]["coverage"].values()):
            version = self._save(document, version)
        return document, version

    def known_identities(self, root, aliases):
        found = self.index.get_many(root, aliases)
        if any(value is not True for value in found.values()):
            raise PublicationError("Invalid publication identity value")
        return set(found)

    def _indexed_coverage(self, coverage):
        result = deepcopy(coverage)
        for kind, value in result.items():
            if kind not in {"news", "rss"}:
                raise PublicationError("Invalid publication source kind")
            if "seen" in value:
                identities = value.pop("seen")
                if any(not isinstance(key, str) or len(key) != 64
                       or any(c not in "0123456789abcdef" for c in key) for key in identities):
                    raise PublicationError("Invalid captured identity")
                value["seen_root"] = self.index.update(value.get("seen_root"), {key: True for key in identities})
        return result

    @staticmethod
    def _terminal(report):
        return (not report.get("attempt") and report["state"] != "GENERATING"
                and not set(report["outcomes"].values()) & {"pending", "temporary_failed", "unknown"})

    def _compact(self, document, *, reserve=0):
        terminal = sorted((r for r in document["reports"].values() if self._terminal(r)),
                          key=lambda r: r["sequence"])
        nonterminal_count = len(document["reports"]) - len(terminal)
        keep = max(0, min(KEEP_TERMINAL_REPORTS, MAX_LIVE_REPORTS - nonterminal_count - reserve))
        archive = terminal[:max(0, len(terminal) - keep)]
        if not archive:
            return
        roots = document["archives"]
        roots["reports"] = self.index.update(roots["reports"], {r["id"]: r for r in archive})
        roots["sequences"] = self.index.update(roots["sequences"], {str(r["sequence"]): r["id"] for r in archive})
        owned = {r["window"]: r["id"] for r in archive
                 if document["windows"].get(r["window"]) == r["id"]}
        roots["windows"] = self.index.update(roots["windows"], owned)
        for report in archive:
            self._release_window(document, report)
            del document["reports"][report["id"]]

    def _budget(self, document):
        if len(document["reports"]) > MAX_LIVE_REPORTS:
            raise PublicationError("Publication backlog is full; inspect unresolved reports before sending")
        reserve = 0
        for report in document["reports"].values():
            size = len(encoded(report))
            if size > REPORT_RESERVATION:
                raise PublicationError("Publication report exceeds reserved receipt capacity")
            reserve += REPORT_RESERVATION - size
        # Concurrent claims reserve the sender's future global coverage fields,
        # not merely its per-report outcome/receipt slot.
        baseline_size = len(encoded(document["baseline"]))
        reserve += max([0] + [max(0, len(encoded(r["attempt"]["publication"])) - baseline_size)
                              for r in document["reports"].values()
                              if r.get("attempt") and r["attempt"].get("publication")])
        limit = getattr(self.store, "max_document_bytes", DOCUMENT_LIMIT)
        if len(encoded(document)) + reserve > limit:
            raise PublicationError("Publication receipt capacity unavailable before SMTP")

    def _save(self, document, version):
        document["schema"] = SCHEMA
        document.setdefault("archives", {"reports": None, "sequences": None, "windows": None})
        document["baseline"]["coverage"] = self._indexed_coverage(document["baseline"]["coverage"])
        for report in document["reports"].values():
            if "receipt_count" not in report:
                records = report.get("receipts", [])
                report["receipts"] = []
                report["receipt_count"], report["receipt_head"] = 0, None
                for record in records:
                    self._append_receipt(report, record)
        if len(document["reports"]) > MAX_LIVE_REPORTS:
            self._compact(document)
        self._budget(document)
        return self.store.save(document, expected_version=version)

    def _find_report(self, document, report_id):
        if not isinstance(report_id, str):
            return None
        report = document["reports"].get(report_id)
        if report is None:
            report = self.index.get_many(document.get("archives", {}).get("reports"), [report_id]).get(report_id)
        return deepcopy(report)

    def _find_window(self, document, window):
        return document["windows"].get(window) or self.index.get_many(
            document.get("archives", {}).get("windows"), [window]).get(window)

    @staticmethod
    def _assert_turn(document, report_id=None):
        if any(r.get("attempt") and r["id"] != report_id for r in document["reports"].values()):
            raise PublicationError("Another SMTP attempt needs a durable receipt or explicit resolution")

    @staticmethod
    def _validate_document(document):
        if (not isinstance(document, dict) or document.get("schema") not in {1, SCHEMA} or document.get("stream") != "daily"
                or not isinstance(document.get("reports"), dict)
                or not isinstance(document.get("windows"), dict)
                or not isinstance(document.get("baseline"), dict)
                or not isinstance(document.get("bootstrap"), dict)):
            raise PublicationError("Invalid daily publication ledger")

    def _load_existing(self, expected_version):
        """Operator mutations never initialize an absent ledger or use stale views."""
        document, version = self.store.load()
        if document is None:
            raise PublicationError("Daily publication ledger does not exist")
        self._validate_document(document)
        if version != expected_version:
            raise PublicationConflict("Publication status changed; inspect again")
        document = deepcopy(document)
        document["baseline"]["coverage"] = self._indexed_coverage(document["baseline"]["coverage"])
        return document, version

    def capture_baseline(self, *, enabled_kinds=()):
        document, version = self._load()
        if not set(enabled_kinds) <= {"news", "rss"}:
            raise ValueError("Invalid enabled source kinds")
        changed = False
        # Hourly capture never blocks an active sender. New adoption is persisted
        # on the next capture without a submission owner; no baseline overwrite.
        if not any(r.get("attempt") for r in document["reports"].values()):
            for kind in enabled_kinds:
                if kind not in document["baseline"]["coverage"]:
                    document["baseline"]["coverage"][kind] = {
                        "through": None, "seen_root": None, "complete": False,
                        "adoption": {"strategy": "previous_24h", "start_at": (
                            self.now() - timedelta(hours=24)).isoformat(), "generation": 0},
                        "attention": [],
                    }
                    changed = True
        if changed:
            self._save(document, version)
        return deepcopy({"bootstrap": document["bootstrap"], **document["baseline"]})

    def window_report(self, window):
        document, _ = self._load()
        report_id = self._find_window(document, window)
        return self._find_report(document, report_id) if report_id else None

    def claim_generation(self, window, *, force=False):
        document, version = self._load()
        if not isinstance(window, str) or not window or len(window) > 160:
            raise ValueError("Invalid publication window")
        existing_id = self._find_window(document, window)
        if existing_id:
            existing = self._find_report(document, existing_id)
            recoverable = (existing["state"] in RELEASABLE_STATES
                           and not existing.get("published")
                           and not existing.get("snapshot_id") and not existing.get("attempt"))
            if not force and not recoverable:
                return None
            if (existing.get("attempt")
                    or "unknown" in existing.get("outcomes", {}).values()):
                raise PublicationError("Window has unresolved publication work")
        self._compact(document, reserve=1)
        report_id = uuid4().hex
        sequence = document["next_sequence"]
        document["next_sequence"] += 1
        document["windows"][window] = report_id
        document["reports"][report_id] = {
            "id": report_id, "sequence": sequence, "window": window,
            "state": "GENERATING", "published": False,
            "created_at": self.now().isoformat(), "snapshot_id": None,
            "outcomes": {}, "attempt": None, "receipts": [], "receipt_count": 0, "receipt_head": None,
        }
        self._save(document, version)
        return report_id

    def finish_without_email(self, report_id, reason):
        if reason not in {"empty", "no_html", "not_configured"}:
            raise ValueError("Invalid no-email reason")
        document, version = self._load()
        report = document["reports"][report_id]
        if report["state"] != "GENERATING":
            raise PublicationConflict("Generation state changed")
        report["state"] = "NO_EMAIL"
        report["reason"] = reason
        self._release_window(document, report)
        self._save(document, version)

    @staticmethod
    def _release_window(document, report):
        if document["windows"].get(report["window"]) == report["id"]:
            del document["windows"][report["window"]]

    def fail_generation(self, report_id):
        """Release a caught pre-snapshot failure, never a prepared/SMTP claim."""
        document, version = self._load()
        report = document["reports"][report_id]
        if (report["state"] != "GENERATING" or report.get("snapshot_id")
                or report.get("attempt") or report.get("published")):
            return False
        report["state"] = "GENERATION_FAILED"
        report["reason"] = "generation_failed"
        self._release_window(document, report)
        self._save(document, version)
        return True

    def prepare(self, report_id, prepared_email, frozen_input):
        document, version = self._load()
        report = document["reports"][report_id]
        if report["state"] != "GENERATING":
            raise PublicationConflict("Generation state changed")
        if len(encoded({"recipients": list(prepared_email.recipients)})) > ENVELOPE_LIMIT:
            raise PublicationError("Envelope exceeds reserved receipt capacity")
        frozen_input = deepcopy(frozen_input)
        frozen_input["coverage"] = self._indexed_coverage(frozen_input["coverage"])
        payload = {
            "schema": SCHEMA, "report_id": report_id,
            "sequence": report["sequence"], "email": prepared_email.to_dict(),
            "input": deepcopy(frozen_input),
        }
        self.store.put_snapshot(report_id, payload)
        report["snapshot_id"] = report_id
        report["state"] = "PREPARED"
        report["outcomes"] = {recipient: "pending" for recipient in prepared_email.recipients}
        self._save(document, version)

    def recover_interrupted(self):
        """Replay durable receipt journals; otherwise hold UNKNOWN, never resend."""
        document, _ = self._load()
        count = 0
        for report in list(document["reports"].values()):
            attempt = report.get("attempt")
            if not attempt:
                continue
            try:
                journal = self.store.get_snapshot("receipt-" + attempt["id"])
            except PublicationError:
                journal = None  # absent/unreadable evidence is uncertainty, not rejection
            if journal is not None:
                self._validate_journal(journal, report["id"], attempt)
                self._commit_receipt(report["id"], attempt["id"],
                                     EmailDeliveryResult.from_dict(journal["record"]["result"]),
                                     self.store.get_snapshot(report["snapshot_id"]), journal=journal)
                continue
            if attempt["state"] == "INFLIGHT":
                current, version = self._load()
                live = current["reports"][report["id"]]
                if not live.get("attempt") or live["attempt"]["id"] != attempt["id"]:
                    raise PublicationConflict("SMTP recovery ownership changed")
                live["attempt"]["state"] = "UNKNOWN"
                for recipient in attempt["requested"]:
                    live["outcomes"][recipient] = "unknown"
                live["state"] = "ATTENTION"
                self._save(current, version)
                count += 1
        if count:
            print(f"[发布] {count} 个无持久化回执的投递需人工核对；不自动重发")
        return count

    def retryable_reports(self, limit=10):
        document, _ = self._load()
        reports = sorted(document["reports"].values(), key=lambda r: r["sequence"])
        return [r["id"] for r in reports if not r.get("attempt") and r.get("snapshot_id")
                and any(value in {"pending", "temporary_failed"}
                        for value in r["outcomes"].values())][:limit]

    def attention_counts(self):
        document, _ = self._load()
        return {state: sum(r["state"] == state for r in document["reports"].values())
                for state in ("GENERATING", "ATTENTION")}

    def status(self, report_id=None, *, include_recipients=False, offset=0, limit=100):
        """Read-only inspection; never initialize, recover, render or send.

        Recipient addresses require explicit opt-in. Default output contains
        only IDs, state, aggregate outcome counts and the CAS version needed
        by operator actions; no MIME, credentials or SMTP response bodies.
        """
        if type(include_recipients) is not bool:
            raise ValueError("Recipient visibility requires an explicit boolean")
        if not isinstance(offset, int) or offset < 0 or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("Invalid publication status page")
        document, version = self.store.load()
        if document is None:
            if report_id is not None:
                raise PublicationError("Publication report does not exist")
            return {"initialized": False, "version": None, "reports": [], "total_reports": 0, "next_offset": None}
        self._validate_document(document)
        if report_id is not None:
            report = self._find_report(document, report_id)
            if report is None:
                raise PublicationError("Publication report does not exist")
            total = 1
            selected = [report][offset:offset + limit]
        else:
            total = document["next_sequence"] - 1
            sequence_numbers = list(range(total - offset, max(0, total - offset - limit), -1))
            live = {r["sequence"]: r for r in document["reports"].values()}
            archived_ids = self.index.get_many(document.get("archives", {}).get("sequences"),
                                               [str(n) for n in sequence_numbers if n not in live])
            selected = []
            for number in sequence_numbers:
                report = live.get(number) or self._find_report(document, archived_ids.get(str(number)))
                if report is None:
                    raise PublicationError("Publication archive sequence is missing")
                selected.append(report)
        summaries = []
        for report in selected:
            summary = {key: report[key] for key in (
                "id", "sequence", "window", "state", "published", "created_at", "snapshot_id"
            )}
            summary["reason"] = report.get("reason")
            summary["window_owned"] = self._find_window(document, report["window"]) == report["id"]
            summary["outcome_counts"] = {state: sum(value == state for value in report["outcomes"].values())
                                         for state in ("pending",) + OUTCOMES}
            summary["receipt_count"] = report.get("receipt_count", len(report["receipts"]))
            summary["receipt_head"] = report.get("receipt_head")
            summary["archived"] = report["id"] not in document["reports"]
            attempt = report.get("attempt")
            summary["attempt"] = None if not attempt else {
                "id": attempt["id"], "state": attempt["state"], "claimed_at": attempt["claimed_at"],
                "requested_count": len(attempt["requested"]),
            }
            if include_recipients:
                summary["outcomes"] = deepcopy(report["outcomes"])
                if attempt:
                    summary["attempt"]["requested"] = list(attempt["requested"])
            summaries.append(summary)
        return {
            "initialized": True, "version": version, "stream": "daily",
            "baseline": {"sequence": document["baseline"]["sequence"],
                         "report_id": document["baseline"]["report_id"],
                         "coverage": {kind: {**{k: deepcopy(v) for k, v in coverage.items() if k != "seen"},
                                             **({"legacy_identity_count": len(coverage["seen"])}
                                                if "seen" in coverage else {})}
                                      for kind, coverage in document["baseline"]["coverage"].items()}},
            "reports": summaries, "total_reports": total,
            "next_offset": offset + limit if offset + limit < total else None,
        }

    @staticmethod
    def _operator_confirmation(*, confirmed, stopped, evidence_reference, actor):
        if confirmed is not True or stopped is not True:
            raise PublicationError("Explicit confirmation and stopped-worker verification required")
        # Persist short, non-secret audit references, never raw provider replies.
        for value in (evidence_reference, actor):
            if (not isinstance(value, str) or not value.strip() or len(value) > 160
                    or any(ord(character) < 32 for character in value)):
                raise ValueError("A short operator and non-secret evidence reference are required")

    def release_generation(
        self, report_id, *, expected_version, confirmed=False, generation_stopped=False,
        evidence_reference, actor="operator",
    ):
        """Fence a crashed pre-snapshot generation after explicit human review.

        An old worker cannot prepare/send after this CAS changes its state.
        This API refuses PREPARED/SENDING/UNKNOWN; use resolve_unknown there.
        """
        self._operator_confirmation(confirmed=confirmed, stopped=generation_stopped,
                                    evidence_reference=evidence_reference, actor=actor)
        document, version = self._load_existing(expected_version)
        report = document["reports"].get(report_id)
        if (not report or report["state"] != "GENERATING" or report.get("snapshot_id")
                or report.get("attempt") or report.get("published")):
            raise PublicationError("Only an unprepared generation can be released")
        report["state"] = "GENERATION_FAILED"
        report["reason"] = "operator_confirmed_stopped"
        report["generation_resolution"] = {
            "recorded_at": self.now().isoformat(), "actor": actor,
            "evidence_reference": evidence_reference,
        }
        self._release_window(document, report)
        return self._save(document, version)

    def resolve_unknown(
        self, report_id, *, expected_version, resolutions, confirmed=False,
        submission_stopped=False, evidence_reference, actor="operator", expected_attempt_id=None,
    ):
        """Audit explicit SMTP evidence; NEVER performs SMTP or regenerates MIME.

        resolutions maps original recipients to 'accepted', 'not_accepted'
        (safe to retry) or 'permanent_failed'. Missing mailbox delivery is NOT
        evidence of SMTP non-acceptance. The operator must verify the submitting
        worker has stopped. Active/unresolved attempt IDs must match status().
        A partial resolution leaves every other ambiguous recipient UNKNOWN.
        """
        self._operator_confirmation(confirmed=confirmed, stopped=submission_stopped,
                                    evidence_reference=evidence_reference, actor=actor)
        if not isinstance(resolutions, dict) or not resolutions:
            raise ValueError("Explicit recipient resolutions are required")
        if any(value not in {"accepted", "not_accepted", "permanent_failed"} for value in resolutions.values()):
            raise ValueError("Invalid recipient resolution")
        document, version = self._load_existing(expected_version)
        report = document["reports"].get(report_id)
        if not report or not report.get("snapshot_id"):
            raise PublicationError("A prepared publication snapshot is required")
        self._assert_turn(document, report_id)
        attempt = report.get("attempt")
        if (attempt["id"] if attempt else None) != expected_attempt_id:
            raise PublicationConflict("SMTP attempt changed; inspect again")
        if attempt:
            try:
                durable_journal = self.store.get_snapshot("receipt-" + attempt["id"])
            except PublicationError:
                durable_journal = None
            if durable_journal is not None:
                self._validate_journal(durable_journal, report_id, attempt)
                raise PublicationConflict("Durable SMTP receipt exists; reconcile it before operator resolution")
        uncertain = {recipient for recipient, outcome in report["outcomes"].items() if outcome == "unknown"}
        if attempt:
            uncertain.update(attempt["requested"])
        if not set(resolutions) <= uncertain:
            raise PublicationError("Only unresolved recipients can be resolved")
        payload = self.store.get_snapshot(report["snapshot_id"])
        if payload.get("report_id") != report_id or payload.get("schema") not in {1, SCHEMA}:
            raise PublicationError("Snapshot identity mismatch")
        prepared = PreparedEmail.from_dict(payload["email"])
        if not set(resolutions) <= set(prepared.recipients):
            raise PublicationError("Resolution recipients are not in the original envelope")
        # Retire the old attempt token, fencing any delayed original worker.
        # All unverified members remain unknown, not pending/temporary failed.
        if attempt:
            for recipient in attempt["requested"]:
                report["outcomes"][recipient] = "unknown"
        receipt = EmailDeliveryResult(
            configured=True, requested=tuple(resolutions),
            accepted=tuple(r for r, value in resolutions.items() if value == "accepted"),
            temporary_failed=tuple(r for r, value in resolutions.items() if value == "not_accepted"),
            permanent_failed=tuple(r for r, value in resolutions.items() if value == "permanent_failed"),
        )
        plan = self._stage_publication(document, report, payload)
        self._apply_receipt(document, report, receipt, payload, {
            "attempt_id": "manual-" + uuid4().hex, "recorded_at": self.now().isoformat(),
            "result": receipt.to_dict(), "source": "operator_verified",
            "actor": actor, "evidence_reference": evidence_reference,
            "superseded_attempt": deepcopy(attempt),
        }, plan=plan)
        new_version = self._save(document, version)
        print("[发布] 人工核实结果已持久化；未执行 SMTP")
        return new_version

    def deliver(self, report_id, dispatcher):
        document, version = self._load()
        report = document["reports"].get(report_id)
        if not report or report.get("attempt") or not report.get("snapshot_id"):
            return None
        payload = self.store.get_snapshot(report["snapshot_id"])
        if payload.get("report_id") != report_id or payload.get("schema") not in {1, SCHEMA}:
            raise PublicationError("Snapshot identity mismatch")
        prepared = PreparedEmail.from_dict(payload["email"])
        requested = tuple(r for r in prepared.recipients
                          if report["outcomes"].get(r) in {"pending", "temporary_failed"})
        if not requested:
            return None
        self._assert_turn(document, report_id)
        if len(encoded({"recipients": list(prepared.recipients)})) > ENVELOPE_LIMIT:
            raise PublicationError("Envelope exceeds reserved receipt capacity")
        # All growing identity/index work happens BEFORE SMTP. The global turn
        # holds this baseline until receipt or explicit operator resolution.
        plan = self._stage_publication(document, report, payload)
        attempt_id = uuid4().hex
        report["attempt"] = {
            "id": attempt_id, "state": "INFLIGHT", "requested": list(requested),
            "claimed_at": self.now().isoformat(), "publication": plan,
        }
        report["state"] = "SENDING"
        # Preflight worst outcome strings + bounded receipt/audit overhead. All
        # concurrent writers reserve this report's entire slot, not just its size.
        probe = deepcopy(document)
        probe["baseline"] = deepcopy(plan)
        worst = probe["reports"][report_id]
        worst["outcomes"] = {r: "temporary_failed" for r in prepared.recipients}
        worst["receipts"] = [{"result": EmailDeliveryResult(
            True, requested, temporary_failed=requested).to_dict(), "audit_reserve": "x" * 4096}]
        self._budget(probe)
        if len(encoded(worst["receipts"][0])) > getattr(self.store, "max_snapshot_bytes", SNAPSHOT_LIMIT):
            raise PublicationError("Publication receipt snapshot capacity unavailable before SMTP")
        self._save(document, version)
        try:
            receipt = dispatcher.send_prepared(prepared, recipients=requested)
            if not isinstance(receipt, EmailDeliveryResult) or set(receipt.requested) != set(requested):
                raise ValueError("Invalid SMTP receipt")
        except Exception:
            receipt = EmailDeliveryResult(configured=True, requested=requested, unknown=requested)
        self._commit_receipt(report_id, attempt_id, receipt, payload)
        return receipt

    def _stage_publication(self, document, report, payload):
        baseline = deepcopy(document["baseline"])
        if report["published"]:
            return baseline
        if report["sequence"] > baseline["sequence"]:
            baseline["sequence"], baseline["report_id"] = report["sequence"], report["id"]
        captured_coverage = self._indexed_coverage(payload["input"]["coverage"])
        for kind, captured in captured_coverage.items():
            previous = baseline["coverage"].get(kind) or {}
            coverage = deepcopy(previous)
            coverage["seen_root"] = self.index.union(previous.get("seen_root"), captured.get("seen_root"))
            coverage.setdefault("through", None)
            coverage.setdefault("adoption", captured.get("adoption") or {
                "strategy": "previous_24h", "start_at": document["bootstrap"]["start_at"], "generation": 0})
            same_adoption = coverage["adoption"].get("generation", 0) == captured.get("adoption", {}).get("generation", 0)
            # Read completeness and identity consumption are deliberately separate.
            # Late old acceptance unions exact identities but never rewinds coverage.
            if same_adoption and captured["through"] >= (previous.get("observed_at") or previous.get("through") or ""):
                coverage["observed_at"] = captured["through"]
                coverage["complete"] = captured["complete"]
                coverage["attention"] = deepcopy(captured.get("attention", []))
                if captured["complete"]:
                    coverage["through"] = max(previous.get("through") or "", captured["through"])
                    coverage["boundary_sha256"] = captured.get("boundary_sha256")
            baseline["coverage"][kind] = coverage
        return baseline

    def _append_receipt(self, report, record):
        if "receipt_count" not in report:
            old_records = report.get("receipts", [])
            report["receipt_count"], report["receipt_head"], report["receipts"] = 0, None, []
            for old_record in old_records:
                self._append_receipt(report, old_record)
        journal = {"type": "receipt-history-v1", "report_id": report["id"],
                   "previous": report.get("receipt_head"), "record": deepcopy(record)}
        import hashlib
        reference = "history-" + hashlib.sha256(encoded(journal)).hexdigest()
        self.store.put_snapshot(reference, journal)
        report["receipt_head"] = reference
        report["receipt_count"] = report.get("receipt_count", 0) + 1
        report["receipts"] = [deepcopy(record)]  # one bounded inspection cache

    def receipt_history(self, report_id, *, cursor=None, limit=100, include_recipients=False):
        """Bounded newest-first receipt pages, including archived reports."""
        if type(include_recipients) is not bool or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid receipt history page")
        document, _ = self.store.load()
        report = self._find_report(document, report_id) if document else None
        if not report:
            raise PublicationError("Publication report does not exist")
        if "receipt_count" not in report and report.get("receipts"):
            raise PublicationError("Legacy receipt history requires ledger migration before paging")
        reference, rows = cursor or report.get("receipt_head"), []
        while reference and len(rows) < limit:
            journal = self.store.get_snapshot(reference)
            if journal.get("type") != "receipt-history-v1" or journal.get("report_id") != report_id:
                raise PublicationError("Receipt history identity mismatch")
            record = deepcopy(journal["record"])
            if not include_recipients:
                record["result"] = {k: len(v) if isinstance(v, list) else v for k, v in record["result"].items()}
                record.pop("superseded_attempt", None)
            rows.append(record)
            reference = journal["previous"]
        return {"records": rows, "next_cursor": reference}

    def _apply_receipt(self, document, report, receipt, payload, record, *, plan=None):
        for outcome in OUTCOMES:
            for recipient in getattr(receipt, outcome):
                report["outcomes"][recipient] = outcome
        self._append_receipt(report, record)
        if receipt.accepted and not report["published"]:
            if plan is None:
                raise PublicationError("Publication coverage was not staged before submission")
            document["baseline"] = deepcopy(plan)
            report["published"] = True
        report["attempt"] = None
        values = set(report["outcomes"].values())
        report["state"] = ("DELIVERED" if values == {"accepted"} else
                           "ATTENTION" if values & {"permanent_failed", "unknown"}
                           else "DELIVERY_PENDING")

    @staticmethod
    def _validate_journal(journal, report_id, attempt):
        if (journal.get("type") != "smtp-receipt-v1" or journal.get("report_id") != report_id
                or journal.get("record", {}).get("attempt_id") != attempt["id"]):
            raise PublicationError("SMTP receipt journal identity mismatch")
        receipt = EmailDeliveryResult.from_dict(journal["record"]["result"])
        if set(receipt.requested) != set(attempt["requested"]):
            raise PublicationError("SMTP receipt journal envelope mismatch")

    def _commit_receipt(self, report_id, attempt_id, receipt, payload, *, journal=None):
        # Journal is immutable durable evidence even if manifest CAS/storage later
        # fails. Recovery replays it, never repeats SMTP. No growing data is added
        # to the CAS manifest: only bounded refs, counts and reserved outcomes.
        record = {"attempt_id": attempt_id, "recorded_at": self.now().isoformat(), "result": receipt.to_dict()}
        journal = journal or {"type": "smtp-receipt-v1", "report_id": report_id, "record": record}
        for _ in range(3):
            document, version = self._load()
            report = document["reports"][report_id]
            attempt = report.get("attempt")
            if not attempt or attempt["id"] != attempt_id:
                raise PublicationConflict("SMTP attempt ownership changed")
            self._validate_journal(journal, report_id, attempt)
            self.store.put_snapshot("receipt-" + attempt_id, journal)
            plan = attempt.get("publication")
            # Legacy interrupted attempts have no staged plan; only durable
            # receipt recovery/operator evidence may upgrade those, never resend.
            if plan is None:
                plan = self._stage_publication(document, report, payload)
            self._apply_receipt(document, report, receipt, payload, journal["record"], plan=plan)
            try:
                self._save(document, version)
                print(f"[发布] 回执已持久化：接受 {len(receipt.accepted)}，暂时失败 "
                      f"{len(receipt.temporary_failed)}，永久失败 {len(receipt.permanent_failed)}，"
                      f"未知 {len(receipt.unknown)}")
                return
            except PublicationConflict:
                continue
        raise PublicationConflict("Receipt journal is durable; manifest reconciliation required")

    def readopt_source(self, kind, *, expected_version, confirmed=False, collection_stopped=False,
                       evidence_reference, actor="operator"):
        """Explicitly acknowledge a historical gap and re-adopt the last 24h.

        Preserves every published identity. The old frontier/adoption/attention is
        immutable audited evidence, not silently truncated history. Does not send,
        advance report sequence, or claim that the omitted interval was read.
        """
        if kind not in {"news", "rss"}:
            raise ValueError("Invalid source kind")
        self._operator_confirmation(confirmed=confirmed, stopped=collection_stopped,
                                    evidence_reference=evidence_reference, actor=actor)
        document, version = self._load_existing(expected_version)
        self._assert_turn(document)
        previous = document["baseline"]["coverage"].get(kind)
        if not previous:
            raise PublicationError("Source has not been adopted")
        audit_id = "adoption-" + uuid4().hex
        audit = {"type": "source-readoption-v1", "kind": kind, "previous": deepcopy(previous),
                 "recorded_at": self.now().isoformat(), "actor": actor, "evidence_reference": evidence_reference}
        self.store.put_snapshot(audit_id, audit)
        document["baseline"]["coverage"][kind] = {
            "seen_root": previous.get("seen_root"), "through": None, "complete": False, "attention": [],
            "adoption": {"strategy": "operator_previous_24h", "start_at": (
                self.now() - timedelta(hours=24)).isoformat(),
                "generation": previous.get("adoption", {}).get("generation", 0) + 1, "audit_id": audit_id},
        }
        return self._save(document, version)
