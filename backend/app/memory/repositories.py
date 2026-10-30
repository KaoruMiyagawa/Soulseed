from __future__ import annotations

import sqlite3
import hashlib
import json
from dataclasses import dataclass
from uuid import uuid4

from ..db import Database
from ..repositories import now_iso
from .models import (
    EvidenceType,
    MemoryActorType,
    MemoryChangeAction,
    MemoryChangeLog,
    MemoryCreate,
    MemoryItem,
    MemoryStatus,
    MemoryType,
    MemoryUpdate,
    SourceRole,
    normalize_canonical_key,
    validate_content,
    validate_score,
)


class MemoryNotFoundError(LookupError):
    pass


class DuplicateMemoryError(ValueError):
    pass


class IdempotencyConflictError(ValueError):
    pass


class MessageNotFoundError(LookupError):
    pass


class InvalidMemoryStateError(ValueError):
    pass


class MemorySourceNotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class MemorySource:
    id: str
    memory_id: str
    message_id: str
    source_role: SourceRole
    evidence_type: EvidenceType
    excerpt: str | None
    created_at: str


@dataclass(frozen=True)
class _ChangeSnapshot:
    content: str | None
    canonical_key: str | None
    status: str | None
    importance: float | None
    confidence: float | None


class MemoryRepository:
    def __init__(self, database: Database):
        self.database = database

    def create(self, memory: MemoryCreate, *, reason: str | None = None, source_message_id: str | None = None, actor_type: MemoryActorType | str = MemoryActorType.SYSTEM_RULE, idempotency_key: str | None = None) -> MemoryItem:
        memory_type = MemoryType(memory.memory_type)
        content = validate_content(memory.content)
        canonical_key = normalize_canonical_key(memory.canonical_key)
        importance = validate_score(memory.importance, "importance")
        confidence = validate_score(memory.confidence, "confidence")
        memory_id, timestamp = str(uuid4()), now_iso()
        with self.database.connection() as db:
            fingerprint = self._fingerprint("created", {"memory_type": memory_type.value, "content": content, "canonical_key": canonical_key, "importance": importance, "confidence": confidence, "expires_at": memory.expires_at})
            existing = self._change_by_idempotency(db, idempotency_key)
            if existing is not None:
                self._validate_idempotent_replay(existing, MemoryChangeAction.CREATED, None, fingerprint)
                if existing.memory_id is None:
                    raise MemoryNotFoundError("幂等审计记录没有关联记忆")
                return self._item_from_row(self._memory_row(db, existing.memory_id, include_deleted=True))
            self._ensure_no_active_duplicate(db, canonical_key)
            try:
                db.execute("INSERT INTO memory_items (id, memory_type, content, canonical_key, importance, confidence, status, expires_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (memory_id, memory_type.value, content, canonical_key, importance, confidence, MemoryStatus.ACTIVE.value, memory.expires_at, timestamp, timestamp))
            except sqlite3.IntegrityError as exc:
                if "idx_memory_active_canonical_key" in str(exc) or "UNIQUE constraint failed: memory_items.canonical_key" in str(exc):
                    raise DuplicateMemoryError(f"已存在 active canonical_key：{canonical_key}") from exc
                raise
            self._insert_change_log(db, memory_id=memory_id, action=MemoryChangeAction.CREATED, old_snapshot=_ChangeSnapshot(None, None, None, None, None), new_snapshot=_ChangeSnapshot(content, canonical_key, MemoryStatus.ACTIVE.value, importance, confidence), reason=reason, source_message_id=source_message_id, actor_type=actor_type, idempotency_key=idempotency_key, request_fingerprint=fingerprint, created_at=timestamp)
        return self.get(memory_id, include_inactive=True, include_deleted=True)

    def get(self, memory_id: str, *, include_inactive: bool = False, include_deleted: bool = False) -> MemoryItem:
        with self.database.connection() as db:
            row = self._memory_row(db, memory_id, include_deleted=include_deleted)
            if not include_inactive and row["status"] != MemoryStatus.ACTIVE.value:
                raise MemoryNotFoundError(memory_id)
            if row["status"] == MemoryStatus.ACTIVE.value:
                db.execute("UPDATE memory_items SET last_accessed_at = ? WHERE id = ?", (now_iso(), memory_id))
                row = self._memory_row(db, memory_id, include_deleted=True)
        return self._item_from_row(row)

    def find_active_by_canonical_key(self, canonical_key: str) -> MemoryItem | None:
        normalized = normalize_canonical_key(canonical_key)
        with self.database.connection() as db:
            row = db.execute("SELECT * FROM memory_items WHERE canonical_key = ? AND status = ?", (normalized, MemoryStatus.ACTIVE.value)).fetchone()
        return self._item_from_row(row) if row is not None else None

    def list(self, *, limit: int = 50, offset: int = 0, memory_type: MemoryType | str | None = None, include_inactive: bool = False, include_deleted: bool = False) -> list[MemoryItem]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit 必须在 1 到 1000 之间")
        if offset < 0:
            raise ValueError("offset 不能小于 0")
        clauses, params = [], []
        if not include_deleted:
            clauses.append("status != ?")
            params.append(MemoryStatus.DELETED.value)
        if not include_inactive:
            clauses.append("status = ?")
            params.append(MemoryStatus.ACTIVE.value)
        if memory_type is not None:
            clauses.append("memory_type = ?")
            params.append(MemoryType(memory_type).value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.extend([limit, offset])
        with self.database.connection() as db:
            rows = db.execute(f"SELECT * FROM memory_items{where} ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?", params).fetchall()
        return [self._item_from_row(row) for row in rows]

    def update(self, memory_id: str, updates: MemoryUpdate, *, reason: str | None = None, source_message_id: str | None = None, actor_type: MemoryActorType | str = MemoryActorType.SYSTEM_RULE, idempotency_key: str | None = None) -> MemoryItem:
        with self.database.connection() as db:
            current = self._item_from_row(self._memory_row(db, memory_id, include_deleted=True))
            if current.status in (MemoryStatus.DELETED, MemoryStatus.SUPERSEDED):
                raise InvalidMemoryStateError(f"不能更新状态为 {current.status.value} 的记忆")
            content = validate_content(updates.content) if updates.content is not None else current.content
            canonical_key = normalize_canonical_key(updates.canonical_key) if updates.canonical_key is not None else current.canonical_key
            importance = validate_score(updates.importance, "importance") if updates.importance is not None else current.importance
            confidence = validate_score(updates.confidence, "confidence") if updates.confidence is not None else current.confidence
            status = MemoryStatus(updates.status).value if updates.status is not None else current.status.value
            expires_at = updates.expires_at if updates.expires_at is not None else current.expires_at
            fingerprint = self._fingerprint("updated", {"memory_id": memory_id, "content": content, "canonical_key": canonical_key, "importance": importance, "confidence": confidence, "status": status, "expires_at": expires_at})
            existing = self._change_by_idempotency(db, idempotency_key)
            if existing is not None:
                self._validate_idempotent_replay(existing, MemoryChangeAction.UPDATED, memory_id, fingerprint)
                return self._item_from_row(self._memory_row(db, existing.memory_id or memory_id, include_deleted=True))
            if (content, canonical_key, importance, confidence, status, expires_at) == (current.content, current.canonical_key, current.importance, current.confidence, current.status.value, current.expires_at):
                return current
            self._ensure_no_active_duplicate(db, canonical_key, exclude_id=memory_id, active_status=status)
            timestamp = now_iso()
            db.execute("UPDATE memory_items SET content = ?, canonical_key = ?, importance = ?, confidence = ?, status = ?, expires_at = ?, updated_at = ? WHERE id = ?", (content, canonical_key, importance, confidence, status, expires_at, timestamp, memory_id))
            self._insert_change_log(db, memory_id=memory_id, action=MemoryChangeAction.UPDATED, old_snapshot=self._snapshot(current), new_snapshot=_ChangeSnapshot(content, canonical_key, status, importance, confidence), reason=reason, source_message_id=source_message_id, actor_type=actor_type, idempotency_key=idempotency_key, request_fingerprint=fingerprint, created_at=timestamp)
        return self.get(memory_id, include_inactive=True, include_deleted=True)

    def mark_status(self, memory_id: str, status: MemoryStatus | str, *, superseded_by_id: str | None = None, reason: str | None = None, source_message_id: str | None = None, actor_type: MemoryActorType | str = MemoryActorType.SYSTEM_RULE, idempotency_key: str | None = None) -> MemoryItem:
        target = MemoryStatus(status)
        with self.database.connection() as db:
            existing = self._change_by_idempotency(db, idempotency_key)
            if existing is not None:
                self._validate_idempotent_replay(existing, {MemoryStatus.INVALID: MemoryChangeAction.INVALIDATED, MemoryStatus.EXPIRED: MemoryChangeAction.EXPIRED, MemoryStatus.SUPERSEDED: MemoryChangeAction.SUPERSEDED, MemoryStatus.DELETED: MemoryChangeAction.SOFT_DELETED}.get(target, MemoryChangeAction.UPDATED), memory_id, self._fingerprint("status", {"memory_id": memory_id, "status": target.value, "superseded_by_id": superseded_by_id}))
                return self._item_from_row(self._memory_row(db, existing.memory_id or memory_id, include_deleted=True))
            current = self._item_from_row(self._memory_row(db, memory_id, include_deleted=True))
            if current.status == MemoryStatus.DELETED and target != MemoryStatus.DELETED:
                raise InvalidMemoryStateError("已软删除的记忆不能再次变更状态")
            if current.status == target and (target != MemoryStatus.SUPERSEDED or current.superseded_by_id == superseded_by_id):
                return current
            if target == MemoryStatus.SUPERSEDED:
                if not superseded_by_id or superseded_by_id == memory_id:
                    raise ValueError("替代记忆必须提供不同的 superseded_by_id")
                replacement = self._item_from_row(self._memory_row(db, superseded_by_id, include_deleted=True))
                if replacement.status == MemoryStatus.DELETED:
                    raise InvalidMemoryStateError("不能指向已软删除的替代记忆")
            timestamp = now_iso()
            db.execute("UPDATE memory_items SET status = ?, superseded_by_id = ?, updated_at = ? WHERE id = ?", (target.value, superseded_by_id, timestamp, memory_id))
            action = {MemoryStatus.INVALID: MemoryChangeAction.INVALIDATED, MemoryStatus.EXPIRED: MemoryChangeAction.EXPIRED, MemoryStatus.SUPERSEDED: MemoryChangeAction.SUPERSEDED, MemoryStatus.DELETED: MemoryChangeAction.SOFT_DELETED}.get(target, MemoryChangeAction.UPDATED)
            fingerprint = self._fingerprint("status", {"memory_id": memory_id, "status": target.value, "superseded_by_id": superseded_by_id})
            self._insert_change_log(db, memory_id=memory_id, action=action, old_snapshot=self._snapshot(current), new_snapshot=_ChangeSnapshot(current.content, current.canonical_key, target.value, current.importance, current.confidence), reason=reason, source_message_id=source_message_id, actor_type=actor_type, idempotency_key=idempotency_key, request_fingerprint=fingerprint, created_at=timestamp)
        return self.get(memory_id, include_inactive=True, include_deleted=True)

    def soft_delete(self, memory_id: str, **kwargs) -> MemoryItem:
        return self.mark_status(memory_id, MemoryStatus.DELETED, **kwargs)

    def permanently_delete(self, memory_id: str, *, reason: str | None = None, source_message_id: str | None = None, actor_type: MemoryActorType | str = MemoryActorType.SYSTEM_RULE, idempotency_key: str | None = None) -> None:
        with self.database.connection() as db:
            fingerprint = self._fingerprint("permanently_deleted", {"memory_id": memory_id})
            existing = self._change_by_idempotency(db, idempotency_key)
            if existing is not None:
                self._validate_idempotent_replay(existing, MemoryChangeAction.PERMANENTLY_DELETED, memory_id, fingerprint)
                return
            row = db.execute("SELECT id FROM memory_items WHERE id = ?", (memory_id,)).fetchone()
            if row is None:
                raise MemoryNotFoundError(memory_id)
            # Do not copy any memory content into the deletion event. ON DELETE SET NULL
            # removes the memory reference after the row is deleted in this transaction.
            self._insert_change_log(db, memory_id=memory_id, action=MemoryChangeAction.PERMANENTLY_DELETED, old_snapshot=_ChangeSnapshot(None, None, None, None, None), new_snapshot=_ChangeSnapshot(None, None, None, None, None), reason=None, source_message_id=None, actor_type=actor_type, idempotency_key=idempotency_key, request_fingerprint=fingerprint, created_at=now_iso())
            db.execute("DELETE FROM memory_items WHERE id = ?", (memory_id,))

    def list_change_log(self, memory_id: str | None = None, *, limit: int = 100, offset: int = 0) -> list[MemoryChangeLog]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit 必须在 1 到 1000 之间")
        if offset < 0:
            raise ValueError("offset 不能小于 0")
        with self.database.connection() as db:
            if memory_id is None:
                rows = db.execute("SELECT * FROM memory_change_log ORDER BY created_at, id LIMIT ? OFFSET ?", (limit, offset)).fetchall()
            else:
                rows = db.execute("SELECT * FROM memory_change_log WHERE memory_id = ? ORDER BY created_at, id LIMIT ? OFFSET ?", (memory_id, limit, offset)).fetchall()
        return [self._change_from_row(row) for row in rows]

    def add_source(self, memory_id: str, message_id: str, source_role: SourceRole | str, evidence_type: EvidenceType | str, excerpt: str | None = None) -> MemorySource:
        memory = self.get(memory_id, include_inactive=True)
        if memory.status == MemoryStatus.DELETED:
            raise InvalidMemoryStateError("已软删除的记忆不能新增来源")
        role = SourceRole(source_role)
        evidence = EvidenceType(evidence_type)
        with self.database.connection() as db:
            message = db.execute("SELECT role FROM messages WHERE id = ?", (message_id,)).fetchone()
            if message is None:
                raise MessageNotFoundError(message_id)
            if message["role"] != role.value:
                raise ValueError("source_role 与原始消息角色不一致")
            source_id, timestamp = str(uuid4()), now_iso()
            try:
                db.execute("INSERT INTO memory_sources (id, memory_id, message_id, source_role, evidence_type, excerpt, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (source_id, memory_id, message_id, role.value, evidence.value, excerpt.strip() if excerpt else None, timestamp))
            except sqlite3.IntegrityError as exc:
                raise DuplicateMemoryError("相同记忆、消息和证据类型的来源已存在") from exc
        return self.get_source(source_id)

    def get_source(self, source_id: str) -> MemorySource:
        with self.database.connection() as db:
            row = db.execute("SELECT * FROM memory_sources WHERE id = ?", (source_id,)).fetchone()
        if row is None:
            raise MemorySourceNotFoundError(source_id)
        return self._source_from_row(row)

    def list_sources(self, memory_id: str) -> list[MemorySource]:
        with self.database.connection() as db:
            rows = db.execute("SELECT * FROM memory_sources WHERE memory_id = ? ORDER BY created_at, id", (memory_id,)).fetchall()
        return [self._source_from_row(row) for row in rows]

    @staticmethod
    def _memory_row(db: sqlite3.Connection, memory_id: str, *, include_deleted: bool = False) -> sqlite3.Row:
        row = db.execute("SELECT * FROM memory_items WHERE id = ?", (memory_id,)).fetchone()
        if row is None or (not include_deleted and row["status"] == MemoryStatus.DELETED.value):
            raise MemoryNotFoundError(memory_id)
        return row

    @staticmethod
    def _snapshot(memory: MemoryItem) -> _ChangeSnapshot:
        return _ChangeSnapshot(memory.content, memory.canonical_key, memory.status.value, memory.importance, memory.confidence)

    @staticmethod
    def _fingerprint(operation: str, payload: dict) -> str:
        encoded = json.dumps({"operation": operation, "payload": payload}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _validate_idempotent_replay(existing: MemoryChangeLog, action: MemoryChangeAction, memory_id: str | None, fingerprint: str) -> None:
        if existing.action != action:
            raise IdempotencyConflictError("幂等键已用于其他记忆操作")
        if action != MemoryChangeAction.CREATED and not (action == MemoryChangeAction.PERMANENTLY_DELETED and existing.memory_id is None) and existing.memory_id != memory_id:
            raise IdempotencyConflictError("幂等键已用于其他记忆")
        if existing.request_fingerprint != fingerprint:
            raise IdempotencyConflictError("幂等键对应的请求参数不一致")

    @staticmethod
    def _change_by_idempotency(db: sqlite3.Connection, idempotency_key: str | None) -> MemoryChangeLog | None:
        if not idempotency_key:
            return None
        row = db.execute("SELECT * FROM memory_change_log WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
        return MemoryRepository._change_from_row(row) if row is not None else None

    @staticmethod
    def _insert_change_log(db: sqlite3.Connection, *, memory_id: str | None, action: MemoryChangeAction, old_snapshot: _ChangeSnapshot, new_snapshot: _ChangeSnapshot, reason: str | None, source_message_id: str | None, actor_type: MemoryActorType | str, idempotency_key: str | None, request_fingerprint: str | None, created_at: str) -> None:
        db.execute("INSERT INTO memory_change_log (id, memory_id, action, old_content, new_content, old_canonical_key, new_canonical_key, old_status, new_status, old_importance, new_importance, old_confidence, new_confidence, reason, source_message_id, actor_type, idempotency_key, request_fingerprint, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (str(uuid4()), memory_id, action.value, old_snapshot.content, new_snapshot.content, old_snapshot.canonical_key, new_snapshot.canonical_key, old_snapshot.status, new_snapshot.status, old_snapshot.importance, new_snapshot.importance, old_snapshot.confidence, new_snapshot.confidence, reason, source_message_id, MemoryActorType(actor_type).value, idempotency_key, request_fingerprint, created_at))

    @staticmethod
    def _change_from_row(row: sqlite3.Row) -> MemoryChangeLog:
        return MemoryChangeLog(id=row["id"], memory_id=row["memory_id"], action=MemoryChangeAction(row["action"]), old_content=row["old_content"], new_content=row["new_content"], old_canonical_key=row["old_canonical_key"], new_canonical_key=row["new_canonical_key"], old_status=MemoryStatus(row["old_status"]) if row["old_status"] else None, new_status=MemoryStatus(row["new_status"]) if row["new_status"] else None, old_importance=row["old_importance"], new_importance=row["new_importance"], old_confidence=row["old_confidence"], new_confidence=row["new_confidence"], reason=row["reason"], source_message_id=row["source_message_id"], actor_type=MemoryActorType(row["actor_type"]), idempotency_key=row["idempotency_key"], request_fingerprint=row["request_fingerprint"], created_at=row["created_at"])

    @staticmethod
    def _ensure_no_active_duplicate(db: sqlite3.Connection, canonical_key: str, *, exclude_id: str | None = None, active_status: str = "active") -> None:
        if active_status != MemoryStatus.ACTIVE.value:
            return
        query = "SELECT id FROM memory_items WHERE canonical_key = ? AND status = 'active'"
        params: list[str] = [canonical_key]
        if exclude_id:
            query += " AND id != ?"
            params.append(exclude_id)
        if db.execute(query, params).fetchone() is not None:
            raise DuplicateMemoryError(f"已存在 active canonical_key：{canonical_key}")

    @staticmethod
    def _item_from_row(row: sqlite3.Row) -> MemoryItem:
        return MemoryItem(id=row["id"], memory_type=MemoryType(row["memory_type"]), content=row["content"], canonical_key=row["canonical_key"], importance=row["importance"], confidence=row["confidence"], status=MemoryStatus(row["status"]), expires_at=row["expires_at"], superseded_by_id=row["superseded_by_id"], created_at=row["created_at"], updated_at=row["updated_at"], last_accessed_at=row["last_accessed_at"])

    @staticmethod
    def _source_from_row(row: sqlite3.Row) -> MemorySource:
        return MemorySource(id=row["id"], memory_id=row["memory_id"], message_id=row["message_id"], source_role=SourceRole(row["source_role"]), evidence_type=EvidenceType(row["evidence_type"]), excerpt=row["excerpt"], created_at=row["created_at"])
