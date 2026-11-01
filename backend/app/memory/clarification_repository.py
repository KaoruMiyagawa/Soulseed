from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import uuid4

from ..db import Database
from ..repositories import now_iso
from .models import MemoryType, normalize_canonical_key


class ClarificationStatus(StrEnum):
    PREPARING = "preparing"
    AWAITING_USER = "awaiting_user"
    PROCESSING = "processing"
    CONTINUE = "continue"
    DEFERRED = "deferred"
    EXPLAINED = "explained"
    DENIED = "denied"
    TEMPORARY = "temporary"
    APPLIED = "applied"
    FAILED = "failed"


class ClarificationNotFoundError(LookupError):
    pass


class ClarificationStateError(ValueError):
    pass


@dataclass(frozen=True)
class ClarificationRecord:
    id: str
    conversation_id: str
    trigger_message_id: str
    source_proposal_id: str | None
    conflict_context: dict[str, Any]
    assistant_message_id: str | None
    response_message_id: str | None
    application_proposal_id: str | None
    status: ClarificationStatus
    version: int
    processing_token: str | None
    lease_expires_at: str | None
    error_code: str | None
    created_at: str
    updated_at: str
    resolution: str | None = None


@dataclass(frozen=True)
class ClarificationClaim:
    clarification: ClarificationRecord
    acquired: bool


class ClarificationRepository:
    """Durable clarification state with serialized, lease-based transitions."""

    _outcomes = {
        ClarificationStatus.CONTINUE,
        ClarificationStatus.DEFERRED,
        ClarificationStatus.EXPLAINED,
        ClarificationStatus.DENIED,
        ClarificationStatus.TEMPORARY,
        ClarificationStatus.FAILED,
        ClarificationStatus.APPLIED,
    }

    def __init__(self, database: Database, *, lease_seconds: int = 60):
        if lease_seconds < 1:
            raise ValueError("lease_seconds 必须大于 0")
        self.database = database
        self.lease_seconds = lease_seconds

    def create(
        self,
        conversation_id: str,
        trigger_message_id: str,
        conflict_context: dict[str, Any],
        *,
        source_proposal_id: str | None = None,
    ) -> ClarificationRecord:
        context = self._validate_context(conflict_context)
        encoded_context = json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        fingerprint = self._fingerprint(conversation_id, trigger_message_id, source_proposal_id, context)
        now = now_iso()
        with self.database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM memory_correction_clarifications WHERE idempotency_key = ?",
                (fingerprint,),
            ).fetchone()
            if existing is not None:
                return self._record(existing)
            if source_proposal_id is not None:
                existing_source = db.execute(
                    "SELECT * FROM memory_correction_clarifications WHERE source_proposal_id = ?",
                    (source_proposal_id,),
                ).fetchone()
                if existing_source is not None:
                    raise ClarificationStateError("该来源 Proposal 已关联其他澄清记录")

            trigger = db.execute(
                "SELECT role, conversation_id, content FROM messages WHERE id = ?",
                (trigger_message_id,),
            ).fetchone()
            if trigger is None or trigger["role"] != "user" or trigger["conversation_id"] != conversation_id:
                raise ClarificationStateError("触发消息必须是已持久化的同会话 user 消息")
            candidate_content = context["candidate"]["content"].strip()
            if candidate_content not in " ".join(trigger["content"].split()):
                raise ClarificationStateError("候选事实无法追溯到触发 user 消息")

            if source_proposal_id is not None:
                proposal = db.execute(
                    "SELECT proposal_type, evidence_message_id, target_memory_id, target_memory_updated_at, status "
                    "FROM memory_change_proposals WHERE id = ?",
                    (source_proposal_id,),
                ).fetchone()
                if proposal is None:
                    raise ClarificationStateError("关联 Proposal 不存在")
                if proposal["proposal_type"] != "correction":
                    raise ClarificationStateError("澄清只能关联 correction Proposal")
                if proposal["evidence_message_id"] != trigger_message_id:
                    raise ClarificationStateError("关联 Proposal 与触发消息不一致")
                if proposal["status"] != "pending":
                    raise ClarificationStateError("只能从 pending Proposal 建立澄清")
                if proposal["target_memory_id"] is not None and not any(
                    item["id"] == proposal["target_memory_id"]
                    and item["updated_at"] == proposal["target_memory_updated_at"]
                    for item in context["matched_memories"]
                ):
                    raise ClarificationStateError("冲突上下文没有包含 Proposal 的目标版本快照")

            self._validate_context_snapshots(db, context)
            clarification_id = str(uuid4())
            db.execute(
                "INSERT INTO memory_correction_clarifications "
                "(id, idempotency_key, conversation_id, trigger_message_id, source_proposal_id, conflict_context, "
                "status, version, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                (
                    clarification_id,
                    fingerprint,
                    conversation_id,
                    trigger_message_id,
                    source_proposal_id,
                    encoded_context,
                    ClarificationStatus.PREPARING.value,
                    now,
                    now,
                ),
            )
            return self._record(self._row(db, clarification_id))

    def persist_prompt_and_wait(self, clarification_id: str, content: str) -> tuple[dict[str, str], ClarificationRecord]:
        """Atomically persist the assistant prompt and enter awaiting_user."""
        text = content.strip()
        if not text:
            raise ValueError("澄清消息不能为空")
        with self.database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._record(self._row(db, clarification_id))
            if current.status is ClarificationStatus.AWAITING_USER and current.assistant_message_id:
                row = db.execute(
                    "SELECT id, conversation_id, role, content, created_at FROM messages WHERE id = ?",
                    (current.assistant_message_id,),
                ).fetchone()
                if row is not None and row["content"] == text:
                    return dict(row), current
                raise ClarificationStateError("澄清消息已保存，不能以不同内容重复提交")
            if current.status is not ClarificationStatus.PREPARING:
                raise ClarificationStateError("只有 preparing 状态可以保存澄清消息")

            message = {
                "id": str(uuid4()),
                "conversation_id": current.conversation_id,
                "role": "assistant",
                "content": text,
                "created_at": now_iso(),
            }
            db.execute(
                "INSERT INTO messages(id, conversation_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
                (message["id"], message["conversation_id"], message["role"], message["content"], message["created_at"]),
            )
            db.execute(
                "UPDATE memory_correction_clarifications SET assistant_message_id = ?, status = ?, "
                "version = version + 1, updated_at = ? WHERE id = ? AND status = ?",
                (
                    message["id"],
                    ClarificationStatus.AWAITING_USER.value,
                    now_iso(),
                    clarification_id,
                    ClarificationStatus.PREPARING.value,
                ),
            )
            return message, self._record(self._row(db, clarification_id))

    def persist_preparing_reply(
        self, clarification_id: str, content: str, *, awaiting_user: bool
    ) -> tuple[dict[str, str], ClarificationRecord]:
        """Atomically save the single assistant turn and choose waiting or deferred."""
        text = content.strip()
        if not text:
            raise ValueError("助手消息不能为空")
        target = ClarificationStatus.AWAITING_USER if awaiting_user else ClarificationStatus.DEFERRED
        with self.database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._record(self._row(db, clarification_id))
            if current.status is not ClarificationStatus.PREPARING:
                raise ClarificationStateError("只有 preparing 状态可以保存初次回应")
            message = self._insert_assistant_message(db, current.conversation_id, text)
            db.execute(
                "UPDATE memory_correction_clarifications SET assistant_message_id = ?, status = ?, "
                "version = version + 1, updated_at = ? WHERE id = ? AND status = ?",
                (message["id"], target.value, now_iso(), clarification_id, ClarificationStatus.PREPARING.value),
            )
            return message, self._record(self._row(db, clarification_id))

    def defer_preparing(self, clarification_id: str, error_code: str) -> ClarificationRecord:
        """Record a failed preparation without pretending a question was sent."""
        if not error_code or len(error_code) > 120:
            raise ValueError("澄清失败码无效")
        with self.database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._record(self._row(db, clarification_id))
            if current.status is not ClarificationStatus.PREPARING:
                raise ClarificationStateError("只有 preparing 状态可以标记准备失败")
            db.execute(
                "UPDATE memory_correction_clarifications SET status = ?, error_code = ?, version = version + 1, updated_at = ? "
                "WHERE id = ? AND status = ?",
                (ClarificationStatus.DEFERRED.value, error_code, now_iso(), clarification_id, ClarificationStatus.PREPARING.value),
            )
            return self._record(self._row(db, clarification_id))

    def persist_processing_reply(
        self,
        clarification_id: str,
        processing_token: str,
        content: str,
        outcome: ClarificationStatus | str,
        *,
        error_code: str | None = None,
        resolution: str | None = None,
        evidence_excerpt: str | None = None,
    ) -> tuple[dict[str, str], ClarificationRecord]:
        """Atomically save a follow-up reply and commit its leased state transition."""
        target = ClarificationStatus(outcome)
        if target not in self._outcomes:
            raise ClarificationStateError("无效的澄清处理结果")
        text = content.strip()
        if not text:
            raise ValueError("助手消息不能为空")
        with self.database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._record(self._row(db, clarification_id))
            if current.status is not ClarificationStatus.PROCESSING:
                raise ClarificationStateError(f"状态 {current.status.value} 不在处理中")
            if current.processing_token != processing_token:
                raise ClarificationStateError("处理租约已失效，拒绝提交过期结果")
            if target is ClarificationStatus.APPLIED and (
                current.application_proposal_id is None
                or not self._proposal_is_applied(db, current.application_proposal_id)
                or not self._proposal_evidence_matches(
                    db, current.application_proposal_id, current.response_message_id
                )
            ):
                raise ClarificationStateError("没有真实 application 记录，不能回复为已应用")
            context = self._with_resolution(current.conflict_context, resolution, evidence_excerpt)
            message = self._insert_assistant_message(db, current.conversation_id, text)
            db.execute(
                "UPDATE memory_correction_clarifications SET assistant_message_id = ?, conflict_context = ?, status = ?, "
                "processing_token = NULL, lease_expires_at = NULL, error_code = ?, version = version + 1, "
                "updated_at = ? WHERE id = ? AND processing_token = ?",
                (
                    message["id"], json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    target.value, error_code, now_iso(), clarification_id, processing_token,
                ),
            )
            return message, self._record(self._row(db, clarification_id))

    def link_application_proposal(
        self,
        clarification_id: str,
        processing_token: str,
        proposal_id: str,
        *,
        resolution: str,
        evidence_excerpt: str,
    ) -> ClarificationRecord:
        """Bind a newly validated Proposal to the claimed, real user answer."""
        if resolution not in {"explicit_change", "original_was_wrong"}:
            raise ClarificationStateError("只有明确长期变化或原记忆错误可绑定应用 Proposal")
        if not isinstance(evidence_excerpt, str) or not evidence_excerpt.strip():
            raise ClarificationStateError("应用依据必须来自用户答复原文")
        with self.database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._record(self._row(db, clarification_id))
            if current.status is not ClarificationStatus.PROCESSING or current.processing_token != processing_token:
                raise ClarificationStateError("澄清处理租约已失效，不能关联 Proposal")
            if current.application_proposal_id not in (None, proposal_id):
                raise ClarificationStateError("该澄清已关联其他应用 Proposal")
            user_message = db.execute(
                "SELECT role, conversation_id, content FROM messages WHERE id = ?",
                (current.response_message_id,),
            ).fetchone()
            if (
                user_message is None
                or user_message["role"] != "user"
                or user_message["conversation_id"] != current.conversation_id
                or " ".join(evidence_excerpt.split()) not in " ".join(user_message["content"].split())
            ):
                raise ClarificationStateError("应用证据必须是当前同会话 user 答复中的原文")
            snapshots = current.conflict_context.get("matched_memories", [])
            if len(snapshots) != 1:
                raise ClarificationStateError("多条可能目标不能自动应用")
            snapshot = snapshots[0]
            target = db.execute(
                "SELECT content, updated_at, status FROM memory_items WHERE id = ?", (snapshot["id"],)
            ).fetchone()
            if (
                target is None
                or target["status"] != "active"
                or target["content"] != snapshot["content"]
                or target["updated_at"] != snapshot["updated_at"]
            ):
                raise ClarificationStateError("澄清目标已删除或版本发生变化")
            proposal = db.execute(
                "SELECT proposal_type, target_memory_id, target_memory_updated_at, evidence_message_id, relation, status "
                "FROM memory_change_proposals WHERE id = ?",
                (proposal_id,),
            ).fetchone()
            if (
                proposal is None
                or proposal["proposal_type"] != "correction"
                or proposal["target_memory_id"] != snapshot["id"]
                or proposal["target_memory_updated_at"] != snapshot["updated_at"]
                or proposal["evidence_message_id"] != current.response_message_id
                or proposal["relation"] not in ("conflict", "potential_conflict")
                or proposal["status"] not in ("pending", "accepted")
            ):
                raise ClarificationStateError("Proposal 与本次澄清答复或目标快照不匹配")
            context = self._with_resolution(current.conflict_context, resolution, evidence_excerpt)
            db.execute(
                "UPDATE memory_correction_clarifications SET application_proposal_id = ?, conflict_context = ?, "
                "version = version + 1, updated_at = ? WHERE id = ? AND status = ? AND processing_token = ?",
                (
                    proposal_id,
                    json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    now_iso(), clarification_id, ClarificationStatus.PROCESSING.value, processing_token,
                ),
            )
            return self._record(self._row(db, clarification_id))

    @staticmethod
    def _insert_assistant_message(db: sqlite3.Connection, conversation_id: str, content: str) -> dict[str, str]:
        message = {
            "id": str(uuid4()),
            "conversation_id": conversation_id,
            "role": "assistant",
            "content": content,
            "created_at": now_iso(),
        }
        db.execute(
            "INSERT INTO messages(id, conversation_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (message["id"], message["conversation_id"], message["role"], message["content"], message["created_at"]),
        )
        return message

    def claim_response(self, clarification_id: str, response_message_id: str) -> ClarificationClaim:
        """Claim one persisted user reply; concurrent duplicates do no work."""
        now = datetime.now(UTC)
        now_text = now.isoformat()
        lease_until = (now + timedelta(seconds=self.lease_seconds)).isoformat()
        with self.database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._record(self._row(db, clarification_id))
            message = db.execute(
                "SELECT role, conversation_id FROM messages WHERE id = ?",
                (response_message_id,),
            ).fetchone()
            if message is None or message["role"] != "user" or message["conversation_id"] != current.conversation_id:
                raise ClarificationStateError("答复必须是已持久化的同会话 user 消息")

            if current.status is ClarificationStatus.PROCESSING:
                lease_is_live = current.lease_expires_at is not None and current.lease_expires_at > now_text
                if lease_is_live and current.response_message_id == response_message_id:
                    return ClarificationClaim(current, acquired=False)
                if lease_is_live:
                    raise ClarificationStateError("该澄清正在处理另一条答复")
                if current.response_message_id != response_message_id:
                    raise ClarificationStateError("过期处理只能使用原答复安全恢复")
            elif current.status not in (
                ClarificationStatus.AWAITING_USER,
                ClarificationStatus.CONTINUE,
                ClarificationStatus.FAILED,
            ):
                raise ClarificationStateError(f"状态 {current.status.value} 不能认领用户答复")
            elif (
                current.status in (ClarificationStatus.AWAITING_USER, ClarificationStatus.CONTINUE)
                and current.response_message_id == response_message_id
            ):
                raise ClarificationStateError("该用户答复已处理，不能重复认领")

            token = str(uuid4())
            db.execute(
                "UPDATE memory_correction_clarifications SET response_message_id = ?, status = ?, "
                "version = version + 1, processing_token = ?, lease_expires_at = ?, error_code = NULL, updated_at = ? "
                "WHERE id = ?",
                (
                    response_message_id,
                    ClarificationStatus.PROCESSING.value,
                    token,
                    lease_until,
                    now_text,
                    clarification_id,
                ),
            )
            updated = self._record(self._row(db, clarification_id))
            return ClarificationClaim(updated, acquired=True)

    def reclaim_application_after_restart(self, clarification_id: str) -> ClarificationClaim:
        """Rotate the lease for already-linked application work during single-process startup recovery."""
        now = datetime.now(UTC)
        now_text = now.isoformat()
        lease_until = (now + timedelta(seconds=self.lease_seconds)).isoformat()
        with self.database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._record(self._row(db, clarification_id))
            if (
                current.status is not ClarificationStatus.PROCESSING
                or current.application_proposal_id is None
                or current.response_message_id is None
            ):
                raise ClarificationStateError("只有已关联答复和 Proposal 的处理中事项可在启动时恢复")
            if current.lease_expires_at is None or current.lease_expires_at > now_text:
                raise ClarificationStateError("仍在有效租约内的澄清不能由恢复流程接管")
            message = db.execute(
                "SELECT role, conversation_id FROM messages WHERE id = ?", (current.response_message_id,)
            ).fetchone()
            if message is None or message["role"] != "user" or message["conversation_id"] != current.conversation_id:
                raise ClarificationStateError("恢复答复不再是有效的同会话 user 消息")
            token = str(uuid4())
            db.execute(
                "UPDATE memory_correction_clarifications SET processing_token = ?, lease_expires_at = ?, "
                "version = version + 1, updated_at = ? WHERE id = ? AND status = ?",
                (token, lease_until, now_text, clarification_id, ClarificationStatus.PROCESSING.value),
            )
            return ClarificationClaim(self._record(self._row(db, clarification_id)), acquired=True)

    def finish_processing(
        self,
        clarification_id: str,
        processing_token: str,
        outcome: ClarificationStatus | str,
        *,
        application_proposal_id: str | None = None,
        error_code: str | None = None,
        resolution: str | None = None,
        evidence_excerpt: str | None = None,
    ) -> ClarificationRecord:
        target = ClarificationStatus(outcome)
        if target not in self._outcomes:
            raise ClarificationStateError("无效的澄清处理结果")
        with self.database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._record(self._row(db, clarification_id))
            if current.status is not ClarificationStatus.PROCESSING:
                raise ClarificationStateError(f"状态 {current.status.value} 不在处理中")
            if current.processing_token != processing_token:
                raise ClarificationStateError("处理租约已失效，拒绝提交过期结果")

            if target is ClarificationStatus.EXPLAINED and application_proposal_id is not None:
                proposal = db.execute(
                    "SELECT evidence_message_id, status FROM memory_change_proposals WHERE id = ?",
                    (application_proposal_id,),
                ).fetchone()
                if proposal is None or proposal["evidence_message_id"] != current.response_message_id:
                    raise ClarificationStateError("应用 Proposal 必须以当前 user 答复为来源证据")
                if proposal["status"] not in ("pending", "accepted"):
                    raise ClarificationStateError("应用 Proposal 生命周期状态不允许关联")

            if target is ClarificationStatus.APPLIED:
                if application_proposal_id is None:
                    application_proposal_id = current.application_proposal_id
                if (
                    application_proposal_id is None
                    or not self._proposal_is_applied(db, application_proposal_id)
                    or not self._proposal_evidence_matches(db, application_proposal_id, current.response_message_id)
                ):
                    raise ClarificationStateError("缺少真实 Proposal application 记录，不能标记为 applied")

            if resolution is not None:
                if not isinstance(evidence_excerpt, str) or not evidence_excerpt.strip():
                    raise ClarificationStateError("澄清判断必须带有用户答复证据")
                user_message = db.execute(
                    "SELECT role, conversation_id, content FROM messages WHERE id = ?",
                    (current.response_message_id,),
                ).fetchone()
                if (
                    user_message is None
                    or user_message["role"] != "user"
                    or user_message["conversation_id"] != current.conversation_id
                    or " ".join(evidence_excerpt.split()) not in " ".join(user_message["content"].split())
                ):
                    raise ClarificationStateError("澄清判断证据必须来自当前同会话 user 答复")
            context = self._with_resolution(current.conflict_context, resolution, evidence_excerpt)

            db.execute(
                "UPDATE memory_correction_clarifications SET status = ?, conflict_context = ?, application_proposal_id = COALESCE(?, application_proposal_id), "
                "processing_token = NULL, lease_expires_at = NULL, error_code = ?, version = version + 1, updated_at = ? "
                "WHERE id = ?",
                (
                    target.value,
                    json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    application_proposal_id,
                    error_code,
                    now_iso(),
                    clarification_id,
                ),
            )
            return self._record(self._row(db, clarification_id))

    def get(self, clarification_id: str) -> ClarificationRecord:
        with self.database.connection() as db:
            return self._record(self._row(db, clarification_id))

    def find_pending_for_conversation(self, conversation_id: str) -> ClarificationRecord | None:
        """Find a persisted question that can receive a reply without guessing."""
        with self.database.connection() as db:
            rows = db.execute(
                "SELECT * FROM memory_correction_clarifications WHERE conversation_id = ? "
                "AND status IN (?, ?, ?, ?) ORDER BY updated_at DESC, id DESC LIMIT 2",
                (
                    conversation_id,
                    ClarificationStatus.AWAITING_USER.value,
                    ClarificationStatus.CONTINUE.value,
                    ClarificationStatus.PROCESSING.value,
                    ClarificationStatus.FAILED.value,
                ),
            ).fetchall()
        if len(rows) > 1:
            raise ClarificationStateError("会话中有多个待处理澄清，不能自动选择")
        return self._record(rows[0]) if rows else None

    def find_active_for_conversation(self, conversation_id: str) -> ClarificationRecord | None:
        """Find any clarification occupying this conversation, including preparation."""
        with self.database.connection() as db:
            rows = db.execute(
                "SELECT * FROM memory_correction_clarifications WHERE conversation_id = ? "
                "AND status IN (?, ?, ?, ?, ?) ORDER BY updated_at DESC, id DESC LIMIT 2",
                (
                    conversation_id,
                    ClarificationStatus.PREPARING.value,
                    ClarificationStatus.AWAITING_USER.value,
                    ClarificationStatus.CONTINUE.value,
                    ClarificationStatus.PROCESSING.value,
                    ClarificationStatus.FAILED.value,
                ),
            ).fetchall()
        if len(rows) > 1:
            raise ClarificationStateError("会话中有多个待处理澄清，不能自动选择")
        return self._record(rows[0]) if rows else None

    def list_recoverable(self, conversation_id: str | None = None) -> list[ClarificationRecord]:
        now = now_iso()
        where = "(status IN (?, ?, ?, ?) OR (status = ? AND (lease_expires_at <= ? OR application_proposal_id IS NOT NULL)) OR status = ? OR (status = ? AND error_code IS NOT NULL))"
        params: list[Any] = [
            ClarificationStatus.PREPARING.value,
            ClarificationStatus.AWAITING_USER.value,
            ClarificationStatus.CONTINUE.value,
            ClarificationStatus.FAILED.value,
            ClarificationStatus.PROCESSING.value,
            now,
            ClarificationStatus.EXPLAINED.value,
            ClarificationStatus.DEFERRED.value,
        ]
        if conversation_id is not None:
            where += " AND conversation_id = ?"
            params.append(conversation_id)
        with self.database.connection() as db:
            rows = db.execute(
                f"SELECT * FROM memory_correction_clarifications WHERE {where} ORDER BY updated_at, id",
                params,
            ).fetchall()
        return [self._record(row) for row in rows]

    def reconcile_completed_applications(self) -> tuple[ClarificationRecord, ...]:
        """Reconcile all clarification records against committed application logs."""
        with self.database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT * FROM memory_correction_clarifications "
                "WHERE application_proposal_id IS NOT NULL OR status = ?",
                (ClarificationStatus.APPLIED.value,),
            ).fetchall()
            reconciled: list[ClarificationRecord] = []
            for row in rows:
                current = self._record(row)
                actual = (
                    current.application_proposal_id is not None
                    and self._proposal_is_applied(db, current.application_proposal_id)
                    and self._proposal_evidence_matches(db, current.application_proposal_id, current.response_message_id)
                )
                if actual and current.status is not ClarificationStatus.APPLIED:
                    db.execute(
                        "UPDATE memory_correction_clarifications SET status = ?, processing_token = NULL, "
                        "lease_expires_at = NULL, error_code = NULL, version = version + 1, updated_at = ? WHERE id = ?",
                        (ClarificationStatus.APPLIED.value, now_iso(), current.id),
                    )
                elif not actual and current.status is ClarificationStatus.APPLIED:
                    db.execute(
                        "UPDATE memory_correction_clarifications SET status = ?, processing_token = NULL, "
                        "lease_expires_at = NULL, error_code = ?, version = version + 1, updated_at = ? WHERE id = ?",
                        (ClarificationStatus.FAILED.value, "application_record_missing", now_iso(), current.id),
                    )
                reconciled.append(self._record(self._row(db, current.id)))
            return tuple(reconciled)

    def reconcile_applied(self, clarification_id: str) -> ClarificationRecord:
        """Use the immutable application log as the authority for applied state."""
        with self.database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._record(self._row(db, clarification_id))
            if current.application_proposal_id is None:
                if current.status is ClarificationStatus.APPLIED:
                    db.execute(
                        "UPDATE memory_correction_clarifications SET status = ?, error_code = ?, version = version + 1, updated_at = ? WHERE id = ?",
                        (ClarificationStatus.FAILED.value, "application_record_missing", now_iso(), clarification_id),
                    )
                    return self._record(self._row(db, clarification_id))
                return current
            actual = self._proposal_is_applied(db, current.application_proposal_id) and self._proposal_evidence_matches(
                db, current.application_proposal_id, current.response_message_id
            )
            if actual and current.status is not ClarificationStatus.APPLIED:
                db.execute(
                    "UPDATE memory_correction_clarifications SET status = ?, processing_token = NULL, "
                    "lease_expires_at = NULL, error_code = NULL, version = version + 1, updated_at = ? WHERE id = ?",
                    (ClarificationStatus.APPLIED.value, now_iso(), clarification_id),
                )
            elif not actual and current.status is ClarificationStatus.APPLIED:
                db.execute(
                    "UPDATE memory_correction_clarifications SET status = ?, error_code = ?, version = version + 1, updated_at = ? WHERE id = ?",
                    (ClarificationStatus.FAILED.value, "application_record_missing", now_iso(), clarification_id),
                )
            return self._record(self._row(db, clarification_id))

    @staticmethod
    def _proposal_is_applied(db: sqlite3.Connection, proposal_id: str) -> bool:
        row = db.execute(
            "SELECT p.status, p.application_status, l.proposal_id AS logged_proposal_id "
            "FROM memory_change_proposals p LEFT JOIN memory_proposal_application_log l ON l.proposal_id = p.id "
            "WHERE p.id = ?",
            (proposal_id,),
        ).fetchone()
        return bool(
            row is not None
            and row["status"] == "accepted"
            and row["application_status"] == "applied"
            and row["logged_proposal_id"] == proposal_id
        )

    @staticmethod
    def _proposal_evidence_matches(db: sqlite3.Connection, proposal_id: str, message_id: str | None) -> bool:
        if message_id is None:
            return False
        row = db.execute(
            "SELECT evidence_message_id FROM memory_change_proposals WHERE id = ?",
            (proposal_id,),
        ).fetchone()
        return row is not None and row["evidence_message_id"] == message_id

    @staticmethod
    def _validate_context(value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("conflict_context 必须是对象")
        candidate = value.get("candidate")
        matches = value.get("matched_memories")
        reason = value.get("reason")
        if not isinstance(candidate, dict) or not all(
            isinstance(candidate.get(key), str) and candidate[key].strip()
            for key in ("memory_type", "content", "canonical_key")
        ):
            raise ValueError("conflict_context 必须包含 memory_type、content 和 canonical_key 候选事实")
        if not isinstance(matches, list) or not matches:
            raise ValueError("conflict_context 必须包含至少一条可核验的 active 记忆快照")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("conflict_context 必须说明需要澄清的原因")
        if candidate["memory_type"] not in {memory_type.value for memory_type in MemoryType}:
            raise ValueError("conflict_context 的候选 memory_type 无效")
        if normalize_canonical_key(candidate["canonical_key"]) != candidate["canonical_key"]:
            raise ValueError("conflict_context 的 candidate canonical_key 必须规范化")
        if value.get("relation") not in {"conflict", "potential_conflict", "coexisting", "needs_review"}:
            raise ValueError("conflict_context 的 relation 无效或不适用于澄清")
        evidence = value.get("evidence")
        if not isinstance(evidence, list) or not evidence or not all(isinstance(item, str) and item.strip() for item in evidence):
            raise ValueError("conflict_context 必须包含结构化 evidence")
        for match in matches:
            if not isinstance(match, dict) or not all(
                isinstance(match.get(key), str) and match[key].strip()
                for key in ("id", "content", "updated_at")
            ):
                raise ValueError("每条冲突记忆快照必须包含 id、content 和 updated_at")
        if len({match["id"] for match in matches}) != len(matches):
            raise ValueError("冲突记忆快照不能重复目标")
        return value

    @staticmethod
    def _validate_context_snapshots(db: sqlite3.Connection, context: dict[str, Any]) -> None:
        for match in context["matched_memories"]:
            row = db.execute(
                "SELECT content, updated_at, status FROM memory_items WHERE id = ?",
                (match["id"],),
            ).fetchone()
            if row is None or row["status"] != "active" or row["content"] != match["content"] or row["updated_at"] != match["updated_at"]:
                raise ClarificationStateError("冲突记忆快照已过期或目标不再 active")

    @staticmethod
    def _fingerprint(conversation_id: str, trigger_message_id: str, proposal_id: str | None, context: dict[str, Any]) -> str:
        payload = json.dumps(
            {"conversation_id": conversation_id, "trigger_message_id": trigger_message_id, "source_proposal_id": proposal_id, "context": context},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _row(db: sqlite3.Connection, clarification_id: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM memory_correction_clarifications WHERE id = ?", (clarification_id,)).fetchone()
        if row is None:
            raise ClarificationNotFoundError(clarification_id)
        return row

    @staticmethod
    def _record(row: sqlite3.Row) -> ClarificationRecord:
        conflict_context = json.loads(row["conflict_context"])
        return ClarificationRecord(
            id=row["id"],
            conversation_id=row["conversation_id"],
            trigger_message_id=row["trigger_message_id"],
            source_proposal_id=row["source_proposal_id"],
            conflict_context=conflict_context,
            assistant_message_id=row["assistant_message_id"],
            response_message_id=row["response_message_id"],
            application_proposal_id=row["application_proposal_id"],
            status=ClarificationStatus(row["status"]),
            version=row["version"],
            processing_token=row["processing_token"],
            lease_expires_at=row["lease_expires_at"],
            error_code=row["error_code"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            resolution=conflict_context.get("resolution"),
        )

    @staticmethod
    def _with_resolution(
        conflict_context: dict[str, Any], resolution: str | None, evidence_excerpt: str | None
    ) -> dict[str, Any]:
        context = dict(conflict_context)
        if resolution is not None:
            context["resolution"] = resolution
        if evidence_excerpt is not None:
            context["response_evidence_excerpt"] = evidence_excerpt
        return context
