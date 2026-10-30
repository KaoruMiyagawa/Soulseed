from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from uuid import uuid4

from ..db import Database
from ..repositories import now_iso
from .models import (
    EvidenceType,
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


class MemoryRepository:
    def __init__(self, database: Database):
        self.database = database

    def create(self, memory: MemoryCreate) -> MemoryItem:
        memory_type = MemoryType(memory.memory_type)
        content = validate_content(memory.content)
        canonical_key = normalize_canonical_key(memory.canonical_key)
        importance = validate_score(memory.importance, "importance")
        confidence = validate_score(memory.confidence, "confidence")
        memory_id, timestamp = str(uuid4()), now_iso()
        with self.database.connection() as db:
            self._ensure_no_active_duplicate(db, canonical_key)
            try:
                db.execute(
                    "INSERT INTO memory_items "
                    "(id, memory_type, content, canonical_key, importance, confidence, status, expires_at, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (memory_id, memory_type.value, content, canonical_key, importance, confidence, MemoryStatus.ACTIVE.value, memory.expires_at, timestamp, timestamp),
                )
            except sqlite3.IntegrityError as exc:
                if "idx_memory_active_canonical_key" in str(exc) or "UNIQUE constraint failed: memory_items.canonical_key" in str(exc):
                    raise DuplicateMemoryError(f"已存在 active canonical_key：{canonical_key}") from exc
                raise
        return self.get(memory_id, include_inactive=True, include_deleted=True)

    def get(self, memory_id: str, *, include_inactive: bool = False, include_deleted: bool = False) -> MemoryItem:
        with self.database.connection() as db:
            row = db.execute("SELECT * FROM memory_items WHERE id = ?", (memory_id,)).fetchone()
            if row is None:
                raise MemoryNotFoundError(memory_id)
            if not include_deleted and row["status"] == MemoryStatus.DELETED.value:
                raise MemoryNotFoundError(memory_id)
            if not include_inactive and row["status"] != MemoryStatus.ACTIVE.value:
                raise MemoryNotFoundError(memory_id)
            if row["status"] == MemoryStatus.ACTIVE.value:
                db.execute("UPDATE memory_items SET last_accessed_at = ? WHERE id = ?", (now_iso(), memory_id))
                row = db.execute("SELECT * FROM memory_items WHERE id = ?", (memory_id,)).fetchone()
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

    def update(self, memory_id: str, updates: MemoryUpdate) -> MemoryItem:
        current = self.get(memory_id, include_inactive=True)
        if current.status in (MemoryStatus.DELETED, MemoryStatus.SUPERSEDED):
            raise InvalidMemoryStateError(f"不能更新状态为 {current.status.value} 的记忆")
        content = validate_content(updates.content) if updates.content is not None else current.content
        canonical_key = normalize_canonical_key(updates.canonical_key) if updates.canonical_key is not None else current.canonical_key
        importance = validate_score(updates.importance, "importance") if updates.importance is not None else current.importance
        confidence = validate_score(updates.confidence, "confidence") if updates.confidence is not None else current.confidence
        status = MemoryStatus(updates.status).value if updates.status is not None else current.status.value
        with self.database.connection() as db:
            self._ensure_no_active_duplicate(db, canonical_key, exclude_id=memory_id, active_status=status)
            db.execute(
                "UPDATE memory_items SET content = ?, canonical_key = ?, importance = ?, confidence = ?, status = ?, expires_at = ?, updated_at = ? WHERE id = ?",
                (content, canonical_key, importance, confidence, status, updates.expires_at if updates.expires_at is not None else current.expires_at, now_iso(), memory_id),
            )
        return self.get(memory_id, include_inactive=True, include_deleted=True)

    def mark_status(self, memory_id: str, status: MemoryStatus | str, *, superseded_by_id: str | None = None) -> MemoryItem:
        target = MemoryStatus(status)
        current = self.get(memory_id, include_inactive=True)
        if current.status == MemoryStatus.DELETED:
            raise InvalidMemoryStateError("已软删除的记忆不能再次变更状态")
        if target == MemoryStatus.SUPERSEDED:
            if not superseded_by_id or superseded_by_id == memory_id:
                raise ValueError("替代记忆必须提供不同的 superseded_by_id")
            replacement = self.get(superseded_by_id, include_inactive=True)
            if replacement.status == MemoryStatus.DELETED:
                raise InvalidMemoryStateError("不能指向已软删除的替代记忆")
        with self.database.connection() as db:
            db.execute("UPDATE memory_items SET status = ?, superseded_by_id = ?, updated_at = ? WHERE id = ?", (target.value, superseded_by_id, now_iso(), memory_id))
        return self.get(memory_id, include_inactive=True, include_deleted=True)

    def soft_delete(self, memory_id: str) -> MemoryItem:
        return self.mark_status(memory_id, MemoryStatus.DELETED)

    def permanently_delete(self, memory_id: str) -> None:
        with self.database.connection() as db:
            row = db.execute("SELECT id FROM memory_items WHERE id = ?", (memory_id,)).fetchone()
            if row is None:
                raise MemoryNotFoundError(memory_id)
            db.execute("DELETE FROM memory_items WHERE id = ?", (memory_id,))

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
                db.execute(
                    "INSERT INTO memory_sources (id, memory_id, message_id, source_role, evidence_type, excerpt, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (source_id, memory_id, message_id, role.value, evidence.value, excerpt.strip() if excerpt else None, timestamp),
                )
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
        return MemoryItem(
            id=row["id"], memory_type=MemoryType(row["memory_type"]), content=row["content"], canonical_key=row["canonical_key"],
            importance=row["importance"], confidence=row["confidence"], status=MemoryStatus(row["status"]), expires_at=row["expires_at"],
            superseded_by_id=row["superseded_by_id"], created_at=row["created_at"], updated_at=row["updated_at"], last_accessed_at=row["last_accessed_at"],
        )

    @staticmethod
    def _source_from_row(row: sqlite3.Row) -> MemorySource:
        return MemorySource(
            id=row["id"], memory_id=row["memory_id"], message_id=row["message_id"], source_role=SourceRole(row["source_role"]),
            evidence_type=EvidenceType(row["evidence_type"]), excerpt=row["excerpt"], created_at=row["created_at"],
        )

