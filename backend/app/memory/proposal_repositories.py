from __future__ import annotations

import hashlib
import json
import sqlite3
from uuid import uuid4

from ..db import Database
from ..repositories import now_iso
from .models import (
    CorrectionIntent,
    MemoryActorType,
    MemoryChangeProposal,
    ProposalApplicationStatus,
    MemoryStatus,
    MemoryType,
    ProposalChangeAction,
    ProposalChangeLog,
    ProposalCreate,
    ProposalCreationResult,
    ProposalStatus,
    ProposalType,
    normalize_canonical_key,
    validate_content,
    validate_score,
)
from .repositories import IdempotencyConflictError, MemoryNotFoundError, MessageNotFoundError


class ProposalNotFoundError(LookupError):
    pass


class ProposalStateError(ValueError):
    pass


class ProposalTargetVersionConflictError(ProposalStateError):
    pass


class ProposalTargetInactiveError(ProposalStateError):
    pass


class MemoryProposalRepository:
    _relations = {"conflict", "potential_conflict", "coexisting", "unrelated", "needs_review"}

    def __init__(self, database: Database):
        self.database = database

    def create(self, proposal: ProposalCreate, *, actor_type: MemoryActorType | str = MemoryActorType.SYSTEM_RULE) -> MemoryChangeProposal:
        return self.create_with_outcome(proposal, actor_type=actor_type).proposal

    def create_with_outcome(self, proposal: ProposalCreate, *, actor_type: MemoryActorType | str = MemoryActorType.SYSTEM_RULE) -> ProposalCreationResult:
        proposal_type = ProposalType(proposal.proposal_type)
        memory_type = MemoryType(proposal.proposed_memory_type)
        intent = CorrectionIntent(proposal.correction_intent)
        content = validate_content(proposal.proposed_content)
        canonical_key = normalize_canonical_key(proposal.proposed_canonical_key)
        evidence = validate_content(proposal.evidence)
        confidence = validate_score(proposal.confidence, "confidence")
        if proposal.relation not in self._relations:
            raise ValueError("无效的 proposal relation")
        fingerprint = self._fingerprint("created", {
            "proposal_type": proposal_type.value,
            "target_memory_id": proposal.target_memory_id,
            "proposed_memory_type": memory_type.value,
            "proposed_content": content,
            "proposed_canonical_key": canonical_key,
            "evidence_message_id": proposal.evidence_message_id,
            "correction_intent": intent.value,
            "relation": proposal.relation,
            "confidence": confidence,
            "evidence": evidence,
            "expected_target_memory_updated_at": proposal.expected_target_memory_updated_at,
        })
        proposal_id, timestamp = str(uuid4()), now_iso()
        try:
            with self.database.connection() as db:
                existing_log = self._log_by_idempotency(db, proposal.idempotency_key)
                if existing_log is not None:
                    self._validate_replay(existing_log, ProposalChangeAction.CREATED, None, fingerprint)
                    return ProposalCreationResult(
                        proposal=self._proposal_from_row(self._proposal_row(db, existing_log.proposal_id)),
                        created=False,
                    )
                target_memory_updated_at = self._validate_references(
                    db,
                    proposal.target_memory_id,
                    proposal.evidence_message_id,
                    expected_target_memory_updated_at=proposal.expected_target_memory_updated_at,
                )
                db.execute(
                    "INSERT INTO memory_change_proposals (id, proposal_type, target_memory_id, proposed_memory_type, proposed_content, proposed_canonical_key, evidence_message_id, correction_intent, relation, confidence, evidence, status, idempotency_key, request_fingerprint, created_at, resolved_at, resolution_reason, target_memory_updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (proposal_id, proposal_type.value, proposal.target_memory_id, memory_type.value, content, canonical_key, proposal.evidence_message_id, intent.value, proposal.relation, confidence, evidence, ProposalStatus.PENDING.value, proposal.idempotency_key, fingerprint, timestamp, None, None, target_memory_updated_at),
                )
                self._insert_log(db, proposal_id, ProposalChangeAction.CREATED, None, ProposalStatus.PENDING, None, actor_type, proposal.idempotency_key, fingerprint, timestamp)
        except sqlite3.IntegrityError as exc:
            if not self._is_proposal_idempotency_conflict(exc, proposal.idempotency_key):
                raise
            return self._replay_after_idempotency_conflict(proposal.idempotency_key, fingerprint, exc)
        return ProposalCreationResult(proposal=self.get(proposal_id), created=True)

    @staticmethod
    def _is_proposal_idempotency_conflict(exc: sqlite3.IntegrityError, idempotency_key: str | None) -> bool:
        if not idempotency_key:
            return False
        message = str(exc)
        return (
            "memory_change_proposals.idempotency_key" in message
            or "memory_proposal_change_log.idempotency_key" in message
        )

    def _replay_after_idempotency_conflict(self, idempotency_key: str, fingerprint: str, original: sqlite3.IntegrityError) -> ProposalCreationResult:
        with self.database.connection() as db:
            existing_log = self._log_by_idempotency(db, idempotency_key)
            if existing_log is None:
                raise original
            self._validate_replay(existing_log, ProposalChangeAction.CREATED, None, fingerprint)
            return ProposalCreationResult(
                proposal=self._proposal_from_row(self._proposal_row(db, existing_log.proposal_id)),
                created=False,
            )

    def get(self, proposal_id: str) -> MemoryChangeProposal:
        with self.database.connection() as db:
            return self._proposal_from_row(self._proposal_row(db, proposal_id))

    def find_by_source_and_candidate(self, evidence_message_id: str, proposed_memory_type: MemoryType | str, proposed_content: str, proposed_canonical_key: str) -> list[MemoryChangeProposal]:
        memory_type = MemoryType(proposed_memory_type).value
        with self.database.connection() as db:
            rows = db.execute(
                "SELECT * FROM memory_change_proposals WHERE proposal_type = ? AND evidence_message_id = ? AND proposed_memory_type = ? AND proposed_content = ? AND proposed_canonical_key = ? ORDER BY created_at, id",
                (ProposalType.CORRECTION.value, evidence_message_id, memory_type, proposed_content, proposed_canonical_key),
            ).fetchall()
        return [self._proposal_from_row(row) for row in rows]

    def list(self, *, status: ProposalStatus | str | None = None, limit: int = 50, offset: int = 0) -> list[MemoryChangeProposal]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit 必须在 1 到 1000 之间")
        if offset < 0:
            raise ValueError("offset 不能小于 0")
        params: list[str | int] = []
        where = ""
        if status is not None:
            where = " WHERE status = ?"
            params.append(ProposalStatus(status).value)
        params.extend([limit, offset])
        with self.database.connection() as db:
            rows = db.execute(f"SELECT * FROM memory_change_proposals{where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?", params).fetchall()
        return [self._proposal_from_row(row) for row in rows]

    def transition(self, proposal_id: str, target: ProposalStatus | str, *, reason: str | None = None, actor_type: MemoryActorType | str = MemoryActorType.SYSTEM_RULE, idempotency_key: str | None = None) -> MemoryChangeProposal:
        target_status = ProposalStatus(target)
        action = {
            ProposalStatus.ACCEPTED: ProposalChangeAction.ACCEPTED,
            ProposalStatus.REJECTED: ProposalChangeAction.REJECTED,
            ProposalStatus.CANCELLED: ProposalChangeAction.CANCELLED,
            ProposalStatus.EXPIRED: ProposalChangeAction.EXPIRED,
        }.get(target_status)
        if action is None:
            raise ProposalStateError("Proposal 只能转换到终态")
        fingerprint = self._fingerprint("transition", {"proposal_id": proposal_id, "target_status": target_status.value, "reason": reason or ""})
        with self.database.connection() as db:
            existing_log = self._log_by_idempotency(db, idempotency_key)
            if existing_log is not None:
                self._validate_replay(existing_log, action, proposal_id, fingerprint)
                return self._proposal_from_row(self._proposal_row(db, proposal_id))
            current = self._proposal_from_row(self._proposal_row(db, proposal_id))
            if current.status != ProposalStatus.PENDING:
                if current.status == target_status:
                    return current
                raise ProposalStateError(f"不能从 {current.status.value} 转换到 {target_status.value}")
            timestamp = now_iso()
            db.execute("UPDATE memory_change_proposals SET status = ?, resolved_at = ?, resolution_reason = ? WHERE id = ?", (target_status.value, timestamp, reason, proposal_id))
            self._insert_log(db, proposal_id, action, current.status, target_status, reason, actor_type, idempotency_key, fingerprint, timestamp)
        return self.get(proposal_id)

    def list_changes(self, proposal_id: str, *, limit: int = 100, offset: int = 0) -> list[ProposalChangeLog]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit 必须在 1 到 1000 之间")
        if offset < 0:
            raise ValueError("offset 不能小于 0")
        with self.database.connection() as db:
            rows = db.execute("SELECT * FROM memory_proposal_change_log WHERE proposal_id = ? ORDER BY created_at, id LIMIT ? OFFSET ?", (proposal_id, limit, offset)).fetchall()
        return [self._log_from_row(row) for row in rows]

    @staticmethod
    def _fingerprint(operation: str, payload: dict) -> str:
        encoded = json.dumps({"operation": operation, "payload": payload}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _proposal_row(db: sqlite3.Connection, proposal_id: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM memory_change_proposals WHERE id = ?", (proposal_id,)).fetchone()
        if row is None:
            raise ProposalNotFoundError(proposal_id)
        return row

    @staticmethod
    def _log_by_idempotency(db: sqlite3.Connection, idempotency_key: str | None) -> ProposalChangeLog | None:
        if not idempotency_key:
            return None
        row = db.execute("SELECT * FROM memory_proposal_change_log WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
        return MemoryProposalRepository._log_from_row(row) if row is not None else None

    @staticmethod
    def _validate_replay(existing: ProposalChangeLog, action: ProposalChangeAction, proposal_id: str | None, fingerprint: str) -> None:
        if existing.action is not action:
            raise IdempotencyConflictError("Proposal 幂等键已用于其他操作")
        if proposal_id is not None and existing.proposal_id != proposal_id:
            raise IdempotencyConflictError("Proposal 幂等键已用于其他 Proposal")
        if existing.request_fingerprint != fingerprint:
            raise IdempotencyConflictError("Proposal 幂等键对应的请求参数不一致")

    @staticmethod
    def _validate_references(
        db: sqlite3.Connection,
        target_memory_id: str | None,
        evidence_message_id: str,
        *,
        expected_target_memory_updated_at: str | None = None,
    ) -> str | None:
        target_updated_at = None
        if target_memory_id is not None:
            target = db.execute("SELECT id, status, updated_at FROM memory_items WHERE id = ?", (target_memory_id,)).fetchone()
            if target is None:
                raise MemoryNotFoundError(target_memory_id)
            if expected_target_memory_updated_at is not None:
                if target["status"] != MemoryStatus.ACTIVE.value:
                    raise ProposalTargetInactiveError("目标记忆在 Proposal 创建时已不是 active")
                if target["updated_at"] != expected_target_memory_updated_at:
                    raise ProposalTargetVersionConflictError("目标记忆在 Proposal 创建时已发生变化")
                target_updated_at = expected_target_memory_updated_at
            else:
                target_updated_at = target["updated_at"]
        message = db.execute("SELECT role FROM messages WHERE id = ?", (evidence_message_id,)).fetchone()
        if message is None:
            raise MessageNotFoundError(evidence_message_id)
        if message["role"] != "user":
            raise ValueError("Proposal evidence_message_id 必须指向 user 消息")
        return target_updated_at

    @staticmethod
    def _insert_log(db: sqlite3.Connection, proposal_id: str, action: ProposalChangeAction, old_status: ProposalStatus | None, new_status: ProposalStatus, reason: str | None, actor_type: MemoryActorType | str, idempotency_key: str | None, request_fingerprint: str | None, created_at: str) -> None:
        db.execute("INSERT INTO memory_proposal_change_log (id, proposal_id, action, old_status, new_status, reason, actor_type, idempotency_key, request_fingerprint, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (str(uuid4()), proposal_id, action.value, old_status.value if old_status else None, new_status.value, reason, MemoryActorType(actor_type).value, idempotency_key, request_fingerprint, created_at))

    @staticmethod
    def _proposal_from_row(row: sqlite3.Row) -> MemoryChangeProposal:
        return MemoryChangeProposal(id=row["id"], proposal_type=ProposalType(row["proposal_type"]), target_memory_id=row["target_memory_id"], proposed_memory_type=MemoryType(row["proposed_memory_type"]), proposed_content=row["proposed_content"], proposed_canonical_key=row["proposed_canonical_key"], evidence_message_id=row["evidence_message_id"], correction_intent=CorrectionIntent(row["correction_intent"]), relation=row["relation"], confidence=row["confidence"], evidence=row["evidence"], status=ProposalStatus(row["status"]), idempotency_key=row["idempotency_key"], request_fingerprint=row["request_fingerprint"], created_at=row["created_at"], resolved_at=row["resolved_at"], resolution_reason=row["resolution_reason"], application_status=ProposalApplicationStatus(row["application_status"]), target_memory_updated_at=row["target_memory_updated_at"], applied_at=row["applied_at"], applied_memory_id=row["applied_memory_id"], replacement_memory_id=row["replacement_memory_id"])

    @staticmethod
    def _log_from_row(row: sqlite3.Row) -> ProposalChangeLog:
        return ProposalChangeLog(id=row["id"], proposal_id=row["proposal_id"], action=ProposalChangeAction(row["action"]), old_status=ProposalStatus(row["old_status"]) if row["old_status"] else None, new_status=ProposalStatus(row["new_status"]), reason=row["reason"], actor_type=MemoryActorType(row["actor_type"]), idempotency_key=row["idempotency_key"], request_fingerprint=row["request_fingerprint"], created_at=row["created_at"])
