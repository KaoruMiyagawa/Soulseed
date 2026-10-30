from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from uuid import uuid4

from ..db import Database
from ..repositories import now_iso
from .models import (
    CorrectionIntent,
    EvidenceType,
    MemoryActorType,
    MemoryChangeProposal,
    MemoryItem,
    MemoryStatus,
    MemoryType,
    ProposalApplicationStatus,
    ProposalStatus,
    ProposalType,
    SourceRole,
    normalize_canonical_key,
    validate_content,
)
from .proposal_repositories import MemoryProposalRepository, ProposalNotFoundError, ProposalStateError
from .repositories import DuplicateMemoryError, IdempotencyConflictError, MemoryNotFoundError, MessageNotFoundError


class ProposalApplicationError(ValueError):
    pass


@dataclass(frozen=True)
class CorrectionApplicationResult:
    proposal: MemoryChangeProposal
    applied_memory: MemoryItem | None
    replacement_memory: MemoryItem | None


class MemoryCorrectionService:
    """Apply an accepted proposal and its memory effects in one SQLite transaction."""

    def __init__(self, database: Database):
        self.database = database

    def apply_proposal(
        self,
        proposal_id: str,
        *,
        reason: str | None = None,
        actor_type: MemoryActorType | str = MemoryActorType.USER,
        idempotency_key: str | None = None,
    ) -> CorrectionApplicationResult:
        fingerprint = self._fingerprint(proposal_id, reason, actor_type)
        with self.database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._proposal_row(db, proposal_id)
            if row["application_status"] == ProposalApplicationStatus.APPLIED.value:
                self._validate_replay(db, proposal_id, fingerprint, idempotency_key)
                return self._result(db, proposal_id)
            if row["status"] != ProposalStatus.ACCEPTED.value:
                raise ProposalStateError("只有 accepted Proposal 才能应用")
            if row["target_memory_id"] is not None and row["target_memory_updated_at"] is None:
                raise ProposalApplicationError("Proposal 缺少目标记忆快照，需要重新生成 Proposal")

            evidence_content = self._validate_evidence(db, row["evidence_message_id"])
            target = self._validate_target(db, row)
            self._validate_conflict_snapshot(row, target)
            timestamp = now_iso()

            if row["proposal_type"] in (ProposalType.INVALIDATION.value, ProposalType.EXPIRATION.value):
                if target is None:
                    raise ProposalApplicationError("状态纠正 Proposal 必须指定目标记忆")
                target_status = MemoryStatus.INVALID if row["proposal_type"] == ProposalType.INVALIDATION.value else MemoryStatus.EXPIRED
                self._update_status(db, target, target_status, proposal_id, row["evidence_message_id"], reason, actor_type, timestamp)
                applied_id, replacement_id = target["id"], None
                applied_row = self._memory_row(db, applied_id)
                replacement_row = None
            else:
                replacement_id = self._insert_memory(db, row, timestamp, actor_type)
                self._insert_source(db, replacement_id, row["evidence_message_id"], evidence_content, timestamp)
                replacement_row = self._memory_row(db, replacement_id)
                if target is not None:
                    self._update_superseded(db, target, replacement_id, proposal_id, row["evidence_message_id"], reason, actor_type, timestamp)
                    applied_id = target["id"]
                    applied_row = self._memory_row(db, applied_id)
                else:
                    applied_id = replacement_id
                    applied_row = None

            db.execute(
                "UPDATE memory_change_proposals SET application_status = ?, applied_at = ?, applied_memory_id = ?, replacement_memory_id = ? WHERE id = ?",
                (ProposalApplicationStatus.APPLIED.value, timestamp, applied_id, replacement_id, proposal_id),
            )
            db.execute(
                "INSERT INTO memory_proposal_application_log (id, proposal_id, application_status, applied_memory_id, replacement_memory_id, actor_type, reason, idempotency_key, request_fingerprint, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(uuid4()), proposal_id, ProposalApplicationStatus.APPLIED.value, applied_id, replacement_id, MemoryActorType(actor_type).value, reason, idempotency_key, fingerprint, timestamp),
            )
            return self._result(db, proposal_id)

    def _insert_memory(self, db: sqlite3.Connection, proposal: sqlite3.Row, timestamp: str, actor_type: MemoryActorType | str) -> str:
        memory_id = str(uuid4())
        canonical_key = normalize_canonical_key(proposal["proposed_canonical_key"])
        content = validate_content(proposal["proposed_content"])
        if db.execute("SELECT id FROM memory_items WHERE canonical_key = ? AND status = 'active'", (canonical_key,)).fetchone() is not None:
            raise DuplicateMemoryError(f"已存在 active canonical_key：{canonical_key}")
        db.execute(
            "INSERT INTO memory_items (id, memory_type, content, canonical_key, importance, confidence, status, expires_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'active', NULL, ?, ?)",
            (memory_id, proposal["proposed_memory_type"], content, canonical_key, 0.5, proposal["confidence"], timestamp, timestamp),
        )
        self._insert_change_log(db, memory_id, "created", None, content, None, canonical_key, None, "active", proposal["evidence_message_id"], proposal["id"], timestamp, None, actor_type)
        return memory_id

    def _update_superseded(self, db: sqlite3.Connection, target: sqlite3.Row, replacement_id: str, proposal_id: str, source_message_id: str, reason: str | None, actor_type: MemoryActorType | str, timestamp: str) -> None:
        db.execute("UPDATE memory_items SET status = 'superseded', superseded_by_id = ?, updated_at = ? WHERE id = ? AND status = 'active'", (replacement_id, timestamp, target["id"]))
        self._insert_change_log(db, target["id"], "superseded", target["content"], target["content"], target["canonical_key"], target["canonical_key"], "active", "superseded", source_message_id, proposal_id, timestamp, reason, actor_type)

    def _update_status(self, db: sqlite3.Connection, target: sqlite3.Row, status: MemoryStatus, proposal_id: str, source_message_id: str, reason: str | None, actor_type: MemoryActorType | str, timestamp: str) -> None:
        db.execute("UPDATE memory_items SET status = ?, updated_at = ? WHERE id = ? AND status = 'active'", (status.value, timestamp, target["id"]))
        action = "invalidated" if status is MemoryStatus.INVALID else "expired"
        self._insert_change_log(db, target["id"], action, target["content"], target["content"], target["canonical_key"], target["canonical_key"], "active", status.value, source_message_id, proposal_id, timestamp, reason, actor_type)

    @staticmethod
    def _insert_source(db: sqlite3.Connection, memory_id: str, message_id: str, excerpt: str, timestamp: str) -> None:
        db.execute("INSERT INTO memory_sources (id, memory_id, message_id, source_role, evidence_type, excerpt, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (str(uuid4()), memory_id, message_id, SourceRole.USER.value, EvidenceType.CORRECTION.value, excerpt, timestamp))

    @staticmethod
    def _insert_change_log(db: sqlite3.Connection, memory_id: str, action: str, old_content: str | None, new_content: str | None, old_key: str | None, new_key: str | None, old_status: str | None, new_status: str | None, source_message_id: str | None, proposal_id: str, timestamp: str, reason: str | None, actor_type: MemoryActorType | str) -> None:
        db.execute("INSERT INTO memory_change_log (id, memory_id, action, old_content, new_content, old_canonical_key, new_canonical_key, old_status, new_status, reason, source_message_id, actor_type, idempotency_key, request_fingerprint, created_at, proposal_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (str(uuid4()), memory_id, action, old_content, new_content, old_key, new_key, old_status, new_status, reason, source_message_id, MemoryActorType(actor_type).value, None, None, timestamp, proposal_id))

    def _validate_target(self, db: sqlite3.Connection, proposal: sqlite3.Row) -> sqlite3.Row | None:
        if proposal["target_memory_id"] is None:
            return None
        target = db.execute("SELECT * FROM memory_items WHERE id = ?", (proposal["target_memory_id"],)).fetchone()
        if target is None:
            raise MemoryNotFoundError(proposal["target_memory_id"])
        if target["status"] != MemoryStatus.ACTIVE.value:
            raise ProposalApplicationError("目标记忆已不是 active，不能盲目覆盖")
        return target

    @staticmethod
    def _validate_conflict_snapshot(proposal: sqlite3.Row, target: sqlite3.Row | None) -> None:
        if target is None:
            return
        if target["updated_at"] != proposal["target_memory_updated_at"]:
            raise ProposalApplicationError("目标记忆在 Proposal 确认后已发生变化，需要重新确认")
        if target["memory_type"] != proposal["proposed_memory_type"]:
            raise ProposalApplicationError("目标记忆类型已发生冲突")

    @staticmethod
    def _validate_evidence(db: sqlite3.Connection, message_id: str | None) -> str:
        if not message_id:
            raise MessageNotFoundError("Proposal 缺少来源消息")
        message = db.execute("SELECT role, content FROM messages WHERE id = ?", (message_id,)).fetchone()
        if message is None:
            raise MessageNotFoundError(message_id)
        if message["role"] != SourceRole.USER.value:
            raise ProposalApplicationError("Proposal 来源必须是 user 消息")
        return message["content"]

    @staticmethod
    def _proposal_row(db: sqlite3.Connection, proposal_id: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM memory_change_proposals WHERE id = ?", (proposal_id,)).fetchone()
        if row is None:
            raise ProposalNotFoundError(proposal_id)
        return row

    def _validate_replay(self, db: sqlite3.Connection, proposal_id: str, fingerprint: str, idempotency_key: str | None) -> None:
        row = db.execute("SELECT request_fingerprint, idempotency_key FROM memory_proposal_application_log WHERE proposal_id = ?", (proposal_id,)).fetchone()
        if row is None or row["request_fingerprint"] != fingerprint or (idempotency_key and row["idempotency_key"] != idempotency_key):
            raise IdempotencyConflictError("Proposal 已应用，但重试请求参数不一致")

    def _result(self, db: sqlite3.Connection, proposal_id: str) -> CorrectionApplicationResult:
        proposal = MemoryProposalRepository._proposal_from_row(self._proposal_row(db, proposal_id))
        applied = self._memory_item(db, proposal.applied_memory_id) if proposal.applied_memory_id else None
        replacement = self._memory_item(db, proposal.replacement_memory_id) if proposal.replacement_memory_id else None
        return CorrectionApplicationResult(proposal, applied, replacement)

    @staticmethod
    def _memory_row(db: sqlite3.Connection, memory_id: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM memory_items WHERE id = ?", (memory_id,)).fetchone()
        if row is None:
            raise MemoryNotFoundError(memory_id)
        return row

    @staticmethod
    def _memory_item(db: sqlite3.Connection, memory_id: str) -> MemoryItem:
        row = MemoryCorrectionService._memory_row(db, memory_id)
        return MemoryItem(id=row["id"], memory_type=MemoryType(row["memory_type"]), content=row["content"], canonical_key=row["canonical_key"], importance=row["importance"], confidence=row["confidence"], status=MemoryStatus(row["status"]), expires_at=row["expires_at"], superseded_by_id=row["superseded_by_id"], created_at=row["created_at"], updated_at=row["updated_at"], last_accessed_at=row["last_accessed_at"])

    @staticmethod
    def _fingerprint(proposal_id: str, reason: str | None, actor_type: MemoryActorType | str) -> str:
        payload = {"proposal_id": proposal_id, "reason": reason or "", "actor_type": MemoryActorType(actor_type).value}
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
