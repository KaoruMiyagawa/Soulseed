from .models import EvidenceType, MemoryActorType, MemoryChangeLog, MemoryCreate, MemoryItem, MemoryStatus, MemoryType, MemoryUpdate, SourceRole
from .repositories import DuplicateMemoryError, MemoryRepository, MemorySource


class MemoryService:
    def __init__(self, repository: MemoryRepository):
        self.repository = repository

    def create_memory(self, memory: MemoryCreate, **kwargs) -> MemoryItem:
        return self.repository.create(memory, **kwargs)

    def get_memory(self, memory_id: str, *, include_inactive: bool = False, include_deleted: bool = False) -> MemoryItem:
        return self.repository.get(memory_id, include_inactive=include_inactive, include_deleted=include_deleted)

    def list_memories(self, *, limit: int = 50, offset: int = 0, memory_type: MemoryType | str | None = None, include_inactive: bool = False, include_deleted: bool = False) -> list[MemoryItem]:
        return self.repository.list(limit=limit, offset=offset, memory_type=memory_type, include_inactive=include_inactive, include_deleted=include_deleted)

    def update_memory(self, memory_id: str, updates: MemoryUpdate, **kwargs) -> MemoryItem:
        return self.repository.update(memory_id, updates, **kwargs)

    def invalidate_memory(self, memory_id: str, **kwargs) -> MemoryItem:
        return self.repository.mark_status(memory_id, MemoryStatus.INVALID, **kwargs)

    def expire_memory(self, memory_id: str, **kwargs) -> MemoryItem:
        return self.repository.mark_status(memory_id, MemoryStatus.EXPIRED, **kwargs)

    def supersede_memory(self, memory_id: str, replacement_id: str, **kwargs) -> MemoryItem:
        return self.repository.mark_status(memory_id, MemoryStatus.SUPERSEDED, superseded_by_id=replacement_id, **kwargs)

    def soft_delete_memory(self, memory_id: str, **kwargs) -> MemoryItem:
        return self.repository.soft_delete(memory_id, **kwargs)

    def permanently_delete_memory(self, memory_id: str, **kwargs) -> None:
        self.repository.permanently_delete(memory_id, **kwargs)

    def list_change_log(self, memory_id: str | None = None, *, limit: int = 100, offset: int = 0) -> list[MemoryChangeLog]:
        return self.repository.list_change_log(memory_id, limit=limit, offset=offset)

    def add_source(self, memory_id: str, message_id: str, source_role: SourceRole | str, evidence_type: EvidenceType | str, excerpt: str | None = None) -> MemorySource:
        return self.repository.add_source(memory_id, message_id, source_role, evidence_type, excerpt)

    def list_sources(self, memory_id: str) -> list[MemorySource]:
        return self.repository.list_sources(memory_id)

    def persist_candidate(self, candidate, source_message: dict) -> MemoryItem:
        """Persist one extractor candidate and attach its real source message."""
        if source_message.get("role") != SourceRole.USER.value:
            raise ValueError("记忆候选只能关联 user 消息")
        try:
            memory = self.create_memory(
                MemoryCreate(
                    memory_type=candidate.memory_type,
                    content=candidate.content,
                    canonical_key=candidate.canonical_key,
                    importance=candidate.importance,
                    confidence=candidate.confidence,
                    expires_at=candidate.expires_at,
                ), actor_type=MemoryActorType.SYSTEM_RULE, source_message_id=source_message["id"], idempotency_key=f"memory:{source_message['id']}:created:{candidate.canonical_key}"
            )
        except DuplicateMemoryError:
            memory = self.repository.find_active_by_canonical_key(candidate.canonical_key)
            if memory is None:
                raise
        try:
            self.add_source(memory.id, source_message["id"], SourceRole.USER, candidate.evidence_type, candidate.excerpt)
        except DuplicateMemoryError:
            # Same message/candidate was already processed: idempotent success.
            pass
        return memory

