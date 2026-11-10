"""Mock-only D3-A pipeline; it never invokes a real model adapter.

The only cross-system handoff runs under one Database.atomic() transaction.
The ContactService remains the owner of intent validation and delivery rules.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from .autonomous_intent import Candidate, Decision, DecisionKind
from .autonomous_intent_state import AutonomousIntentStateRepository, _iso, _time
from .contact import ContactService
from .ownership import current_character
from .proactive_expression import (DraftResult, DraftService, MockDraftGenerator,
                                   MockEvidenceReviewer)


@dataclass(frozen=True)
class PipelineResult:
    status: str
    candidate_id: str
    contact_intent_id: str | None = None
    proactive_message_id: str | None = None
    reason_code: str | None = None


class MockGeneralBudgetGate:
    """Test double for the missing transactional general model budget API."""

    def __init__(self, *, available: bool = True):
        self.available = available
        self.reservations: set[str] = set()

    def reserve(self, character_id: str, key: str) -> str | None:
        if not self.available or not character_id or not key:
            return None
        ticket = f"{character_id}:{key}:{uuid4()}"
        self.reservations.add(ticket)
        return ticket

    def release(self, ticket: str) -> None:
        self.reservations.discard(ticket)

    def settle(self, ticket: str) -> None:
        # Keep consumed tickets in the test double to model conservative billing.
        if ticket not in self.reservations:
            raise ValueError("general_budget_reservation_missing")


class AutonomousContactPipeline:
    """Explicitly constructed, isolated end-to-end path for D3-A tests."""

    def __init__(self, state: AutonomousIntentStateRepository, contact: ContactService,
                 drafts: DraftService, general_budget: MockGeneralBudgetGate, *,
                 clock=lambda: datetime.now(UTC), failure_hook=None):
        if (type(state) is not AutonomousIntentStateRepository or type(contact) is not ContactService
                or type(drafts) is not DraftService or type(drafts.generator) is not MockDraftGenerator
                or type(drafts.reviewer) is not MockEvidenceReviewer
                or type(general_budget) is not MockGeneralBudgetGate):
            raise TypeError("d3a_mock_adapters_required")
        if state.database.environment != "testing":
            raise RuntimeError("d3a_mock_pipeline_testing_only")
        if contact.repository.database is not state.database or drafts.database is not state.database:
            raise ValueError("d3a_shared_database_required")
        self.state, self.contact, self.drafts = state, contact, drafts
        self.general_budget, self.clock, self.failure_hook = general_budget, clock, failure_hook

    def run(self, candidate: Candidate, decision: Decision) -> PipelineResult:
        now = self.clock().astimezone(UTC)
        candidate_id = candidate.dedup_key if type(candidate) is Candidate else ""
        if (type(candidate) is not Candidate or type(decision) is not Decision
                or decision.result != DecisionKind.CONTACT or decision.dedup_key != candidate_id
                or decision.candidate_type != candidate.kind or candidate.character_id != current_character.get()):
            return PipelineResult("rejected", candidate_id, reason_code="invalid_candidate_or_decision")
        if (not self.state.enabled or not self.state.handoff_enabled
                or not self.state.contact_enabled or not self.contact.enabled):
            return PipelineResult("rejected", candidate_id, reason_code="autonomous_contact_disabled")

        # General provider budget is currently a strict mock contract. A real
        # adapter cannot be injected until its reservation shares a transaction
        # boundary with the SQLite autonomous sub-budget.
        ticket = self.general_budget.reserve(candidate.character_id, candidate_id)
        if ticket is None:
            return PipelineResult("deferred", candidate_id, reason_code="general_model_budget_unavailable")
        budget_settled = False
        try:
            try:
                saved = self.state.persist_candidate(candidate, now=now)
            except ValueError as exc:
                return PipelineResult("rejected", candidate_id, reason_code=str(exc)[:80])
            if saved.status not in {"pending", "deferred"}:
                return PipelineResult("suppressed", candidate_id, reason_code=saved.reason_code or saved.status)
            claimed = self.state.claim(candidate_id, candidate.character_id, now=now)
            if not claimed.token:
                return PipelineResult("suppressed", candidate_id, reason_code=claimed.reason_code or claimed.status)
            token = claimed.token
            started = self.state.mark_request_started(candidate_id, candidate.character_id, token, now=now)
            if not started.allowed:
                return PipelineResult("suppressed", candidate_id, reason_code=started.reason_code)
            if self.failure_hook:
                self.failure_hook("after_request_started")
            try:
                draft = self.drafts.generate(candidate.character_id, candidate, decision)
            except Exception:
                self.state.record_request_outcome(candidate_id, candidate.character_id, token,
                                                  outcome="unknown", now=self.clock())
                self.general_budget.settle(ticket)
                budget_settled = True
                return PipelineResult("outcome_unknown", candidate_id, reason_code="generation_outcome_unknown")
            if draft.status != "draft" or not draft.text:
                self.state.record_request_outcome(candidate_id, candidate.character_id, token,
                                                  outcome="known_failure", now=self.clock())
                self.general_budget.settle(ticket)
                budget_settled = True
                return PipelineResult("rejected", candidate_id, reason_code=draft.failure_code or "draft_rejected")
            settled = self.state.record_request_outcome(candidate_id, candidate.character_id, token,
                                                        outcome="success", now=self.clock())
            if settled.status != "claimed":
                self.general_budget.settle(ticket)
                budget_settled = True
                return PipelineResult("outcome_unknown", candidate_id, reason_code="request_settlement_failed")
            self.general_budget.settle(ticket)
            budget_settled = True
            if self.failure_hook:
                self.failure_hook("after_generation")
            persisted = self._persist_reviewed_draft(candidate_id, candidate.character_id,
                                                     claimed.reservation_id, candidate, draft)
            if not persisted:
                self.state.cancel_candidate(candidate_id, candidate.character_id,
                                            reason="draft_evidence_or_lease_invalid", now=self.clock())
                return PipelineResult("rejected", candidate_id, reason_code="draft_persistence_rejected")
            if self.failure_hook:
                self.failure_hook("after_draft_persistence")
            return self._handoff(candidate_id, candidate.character_id, token, now=self.clock())
        finally:
            if not budget_settled:
                self.general_budget.release(ticket)

    def _persist_reviewed_draft(self, candidate_id: str, character_id: str,
                                reservation_id: str | None, candidate: Candidate,
                                result: DraftResult) -> bool:
        current = self.clock().astimezone(UTC)
        if not reservation_id or not result.text or len(result.text) > 300:
            return False
        expected = {item.id for item in candidate.evidence}
        if not expected.issubset(set(result.evidence_ids)):
            return False
        evidence = json.dumps([{"kind": item.kind, "id": item.id} for item in candidate.evidence],
                              ensure_ascii=False, separators=(",", ":"))
        digest = hashlib.sha256(result.text.encode("utf-8")).hexdigest()
        with self.state.database.atomic() as db:
            row = db.execute("""SELECT c.status,c.lease_token,c.lease_expires_at,c.expires_at,r.request_status,r.message_status,r.id AS rid
                FROM autonomous_contact_candidates c JOIN autonomous_contact_reservations r
                ON r.candidate_id=c.id AND r.character_id=c.character_id
                WHERE c.id=? AND c.character_id=? AND r.id=? AND r.attempt_no=c.claim_count""",
                             (candidate_id, character_id, reservation_id)).fetchone()
            lease = _time(row["lease_expires_at"]) if row else None
            if (not row or row["status"] != "claimed" or row["lease_token"] is None
                    or row["request_status"] != "settled" or row["message_status"] != "reserved"
                    or not lease or lease <= current or row["expires_at"] <= _iso(current)):
                return False
            # Confirm evidence is still valid in the same DB snapshot as the draft write.
            fresh = db.execute("SELECT * FROM autonomous_contact_candidates WHERE id=? AND character_id=?",
                               (candidate_id, character_id)).fetchone()
            refs = json.loads(fresh["evidence_json"])
            if not self.state._evidence_valid(db, character_id, fresh["candidate_type"], refs, current):
                return False
            old = db.execute("SELECT review_status FROM autonomous_contact_drafts WHERE candidate_id=?", (candidate_id,)).fetchone()
            if old:
                return old["review_status"] == "mock_approved"
            expiry = min(_time(row["expires_at"]), current + timedelta(minutes=15))
            db.execute("""INSERT INTO autonomous_contact_drafts
                (id,candidate_id,character_id,reservation_id,evidence_json,content,content_hash,review_status,created_at,expires_at)
                VALUES(?,?,?,?,?,?,?,'mock_approved',?,?)""",
                       (str(uuid4()), candidate_id, character_id, reservation_id, evidence,
                        result.text.strip(), digest, _iso(current), _iso(expiry)))
            return True

    def _handoff(self, candidate_id: str, character_id: str, token: str, *, now: datetime) -> PipelineResult:
        current = now.astimezone(UTC)
        at = _iso(current)
        self.state._require_enabled_scope(character_id)
        with self.state.database.atomic() as db:
            row = db.execute("SELECT * FROM autonomous_contact_candidates WHERE id=? AND character_id=? AND status='claimed' AND lease_token=?",
                             (candidate_id, character_id, token)).fetchone()
            if not row:
                return PipelineResult("rejected", candidate_id, reason_code="claim_not_current")
            lease = _time(row["lease_expires_at"])
            reservation = db.execute("SELECT * FROM autonomous_contact_reservations WHERE candidate_id=? AND character_id=? AND attempt_no=?",
                                     (candidate_id, character_id, row["claim_count"])).fetchone()
            draft = db.execute("SELECT * FROM autonomous_contact_drafts WHERE candidate_id=? AND character_id=?",
                               (candidate_id, character_id)).fetchone()
            if (not lease or lease <= current or not reservation or reservation["request_status"] != "settled"
                    or reservation["message_status"] != "reserved" or not draft
                    or draft["review_status"] != "mock_approved" or draft["expires_at"] <= at
                    or draft["reservation_id"] != reservation["id"]):
                if draft and draft["review_status"] == "mock_approved":
                    db.execute("UPDATE autonomous_contact_drafts SET review_status='cancelled' WHERE id=?", (draft["id"],))
                if reservation and reservation["message_status"] == "reserved":
                    db.execute("UPDATE autonomous_contact_reservations SET message_status='released',updated_at=? WHERE id=?", (at, reservation["id"]))
                self.state._defer_or_end(db, row, "handoff_precondition_failed", None, "failed", current)
                return PipelineResult("rejected", candidate_id, reason_code="handoff_precondition_failed")
            day, _ = self.state._utc_day(current)
            expected_evidence = json.dumps(json.loads(row["evidence_json"]), ensure_ascii=False, separators=(",", ":"))
            if (reservation["local_day"] != day or draft["evidence_json"] != expected_evidence
                    or hashlib.sha256(draft["content"].encode("utf-8")).hexdigest() != draft["content_hash"]):
                db.execute("UPDATE autonomous_contact_drafts SET review_status='cancelled' WHERE id=?", (draft["id"],))
                db.execute("UPDATE autonomous_contact_reservations SET message_status='released',updated_at=? WHERE id=?", (at, reservation["id"]))
                self.state._defer_or_end(db, row, "draft_or_budget_changed", None, "cancelled", current)
                return PipelineResult("rejected", candidate_id, reason_code="draft_or_budget_changed")
            code, retry, terminal = self.state._policy(db, row, current)
            if code:
                db.execute("UPDATE autonomous_contact_drafts SET review_status='cancelled' WHERE id=?", (draft["id"],))
                db.execute("UPDATE autonomous_contact_reservations SET message_status='released',updated_at=? WHERE id=?", (at, reservation["id"]))
                self.state._defer_or_end(db, row, code, retry, terminal or "deferred", current)
                return PipelineResult("rejected", candidate_id, reason_code=code)
            evidence = json.loads(row["evidence_json"])
            ids = {item["kind"]: item["id"] for item in evidence if item["kind"] != "message"}
            if row["candidate_type"] == "real_discovery":
                source, source_id = "life_share", ids.get("share")
            else:
                source = "conversation_followup"
                message_ids = [item["id"] for item in evidence if item["kind"] == "message"]
                source_id = None
                if message_ids:
                    marks = ",".join("?" for _ in message_ids)
                    anchor = db.execute(f"SELECT m.id FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE m.id IN ({marks}) AND m.role='user' AND c.character_id=? ORDER BY m.created_at,m.id LIMIT 1",
                                        (*message_ids, character_id)).fetchone()
                    source_id = anchor["id"] if anchor else None
            if not source_id:
                db.execute("UPDATE autonomous_contact_drafts SET review_status='cancelled' WHERE id=?", (draft["id"],))
                db.execute("UPDATE autonomous_contact_reservations SET message_status='released',updated_at=? WHERE id=?", (at, reservation["id"]))
                self.state._defer_or_end(db, row, "contact_source_not_allowlisted", None, "cancelled", current)
                return PipelineResult("rejected", candidate_id, reason_code="contact_source_not_allowlisted")
            topic = self.state._topic_for_row(row, db)
            intent = self.contact.create_intent_in_transaction(
                db, character_id=character_id, source=source, source_id=source_id,
                reason="autonomous_evidence_backed_candidate", topic=topic,
                importance="normal", now=current, status="ready")
            if self.failure_hook:
                self.failure_hook("after_contact_intent")
            message_id = str(uuid4())
            db.execute("INSERT INTO proactive_messages(id,intent_id,attempt_no,content,status,created_at) VALUES(?,?,0,?,'ready',?)",
                       (message_id, intent["id"], draft["content"], at))
            changed = db.execute("UPDATE autonomous_contact_candidates SET status='completed',reason_code='handed_off_to_contact_service',lease_token=NULL,lease_expires_at=NULL WHERE id=? AND character_id=? AND status='claimed' AND lease_token=?",
                                 (candidate_id, character_id, token)).rowcount
            if changed != 1:
                raise RuntimeError("candidate_handoff_race")
            db.execute("UPDATE autonomous_contact_reservations SET message_status='committed',updated_at=? WHERE id=? AND message_status='reserved'",
                       (at, reservation["id"]))
            db.execute("UPDATE autonomous_contact_drafts SET review_status='consumed' WHERE id=? AND review_status='mock_approved'",
                       (draft["id"],))
            return PipelineResult("handed_off", candidate_id, intent["id"], message_id)
