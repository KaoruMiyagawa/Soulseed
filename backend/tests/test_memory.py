import shutil
from pathlib import Path

import pytest

from app.db import Database
from app.memory import (
    EvidenceType,
    MemoryCreate,
    MemoryRepository,
    MemoryService,
    MemoryStatus,
    MemoryType,
    MemoryUpdate,
    SourceRole,
)
from app.repositories import ConversationRepository, MessageRepository
from app.memory.repositories import DuplicateMemoryError, MessageNotFoundError


@pytest.fixture
def memory_context(tmp_path):
    database = Database(str(tmp_path / "memory.db"))
    database.initialize()
    return database, MemoryService(MemoryRepository(database)), ConversationRepository(database), MessageRepository(database)


def make_memory(service: MemoryService, *, memory_type=MemoryType.USER_PREFERENCE, content="喜欢 Rust", canonical_key="user:language:rust"):
    return service.create_memory(MemoryCreate(memory_type=memory_type, content=content, canonical_key=canonical_key, importance=0.8, confidence=0.9))


def test_new_database_migration_creates_memory_tables(tmp_path):
    database = Database(str(tmp_path / "new.db"))
    database.initialize()
    with database.connection() as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"memory_items", "memory_sources"}.issubset(tables)
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 2


def test_existing_database_upgrade_preserves_conversations_and_messages(tmp_path):
    source = Path("backend/data/soulseed.db")
    if not source.exists():
        pytest.skip("当前工作区没有现有数据库文件")
    target = tmp_path / "upgraded.db"
    shutil.copy2(source, target)
    before = Database(str(target))
    with before.connection() as connection:
        conversation_count = connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        message_count = connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    before.initialize()
    with before.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == conversation_count
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == message_count
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 2


def test_migration_is_idempotent(memory_context):
    database, _, _, _ = memory_context
    database.initialize()
    database.initialize()
    with database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'memory_items'").fetchone()[0] == 1


def test_create_read_update_memory(memory_context):
    _, service, _, _ = memory_context
    created = make_memory(service)
    loaded = service.get_memory(created.id)
    assert loaded.content == "喜欢 Rust"
    assert loaded.memory_type is MemoryType.USER_PREFERENCE
    updated = service.update_memory(created.id, MemoryUpdate(content="现在更喜欢 Rust 和 C++", confidence=0.95))
    assert updated.content == "现在更喜欢 Rust 和 C++"
    assert updated.confidence == 0.95


def test_type_filter_and_limit(memory_context):
    _, service, _, _ = memory_context
    make_memory(service, canonical_key="pref:one")
    make_memory(service, memory_type=MemoryType.GOAL, content="完成 Soulseed 阶段二", canonical_key="goal:soulseed:phase2")
    make_memory(service, memory_type=MemoryType.EVENT, content="参加项目评审", canonical_key="event:review")
    assert len(service.list_memories(memory_type=MemoryType.GOAL)) == 1
    assert len(service.list_memories(limit=2)) == 2
    with pytest.raises(ValueError):
        service.list_memories(limit=0)


def test_source_association_requires_real_message(memory_context):
    _, service, conversations, messages = memory_context
    memory = make_memory(service)
    conversation = conversations.create()
    message = messages.add(conversation["id"], "user", "我喜欢 Rust")
    source = service.add_source(memory.id, message["id"], SourceRole.USER, EvidenceType.EXPLICIT_FACT, "我喜欢 Rust")
    assert source.message_id == message["id"]
    assert len(service.list_sources(memory.id)) == 1
    with pytest.raises(MessageNotFoundError):
        service.add_source(memory.id, "missing-message", SourceRole.USER, EvidenceType.EXPLICIT_FACT)


def test_source_role_must_match_original_message(memory_context):
    _, service, conversations, messages = memory_context
    memory = make_memory(service)
    conversation = conversations.create()
    message = messages.add(conversation["id"], "assistant", "这是之前的回答")
    with pytest.raises(ValueError):
        service.add_source(memory.id, message["id"], SourceRole.USER, EvidenceType.SUMMARY)


def test_duplicate_canonical_key_is_rejected(memory_context):
    _, service, _, _ = memory_context
    make_memory(service, canonical_key=" User:Language:Rust ")
    with pytest.raises(DuplicateMemoryError):
        make_memory(service, content="重复记忆", canonical_key="user:language:rust")


def test_invalid_memory_is_not_returned_by_default(memory_context):
    _, service, _, _ = memory_context
    memory = make_memory(service)
    invalid = service.invalidate_memory(memory.id)
    assert invalid.status is MemoryStatus.INVALID
    assert service.list_memories() == []
    assert service.get_memory(memory.id, include_inactive=True).status is MemoryStatus.INVALID


def test_soft_delete_is_hidden_but_not_destroyed(memory_context):
    _, service, _, _ = memory_context
    memory = make_memory(service)
    deleted = service.soft_delete_memory(memory.id)
    assert deleted.status is MemoryStatus.DELETED
    assert service.list_memories() == []
    assert service.get_memory(memory.id, include_inactive=True, include_deleted=True).status is MemoryStatus.DELETED


def test_permanent_delete_removes_memory_and_sources_but_keeps_original_message(memory_context):
    database, service, conversations, messages = memory_context
    memory = make_memory(service)
    conversation = conversations.create()
    message = messages.add(conversation["id"], "user", "我有一个长期目标")
    service.add_source(memory.id, message["id"], SourceRole.USER, EvidenceType.EXPLICIT_FACT)
    service.permanently_delete_memory(memory.id)
    assert service.list_sources(memory.id) == []
    assert messages.list_for_conversation(conversation["id"])[0]["id"] == message["id"]
    with database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM memory_items WHERE id = ?", (memory.id,)).fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM memory_sources WHERE memory_id = ?", (memory.id,)).fetchone()[0] == 0


def test_superseded_memory_keeps_traceable_replacement(memory_context):
    _, service, _, _ = memory_context
    old = make_memory(service, content="用户喜欢 Python", canonical_key="user:language:python")
    replacement = make_memory(service, content="用户现在更喜欢 Rust", canonical_key="user:language:rust")
    updated_old = service.supersede_memory(old.id, replacement.id)
    assert updated_old.status is MemoryStatus.SUPERSEDED
    assert updated_old.superseded_by_id == replacement.id
    active = service.list_memories()
    assert [item.id for item in active] == [replacement.id]

