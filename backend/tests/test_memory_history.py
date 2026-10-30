import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.db import Database
from app.memory import (
    MemoryActorType,
    MemoryChangeAction,
    MemoryCreate,
    IdempotencyConflictError,
    MemoryRepository,
    MemoryService,
    MemoryStatus,
    MemoryType,
    MemoryUpdate,
)
from app.repositories import ConversationRepository, MessageRepository


@pytest.fixture
def history_context(tmp_path):
    database = Database(str(tmp_path / "history.db"))
    database.initialize()
    return database, MemoryService(MemoryRepository(database)), ConversationRepository(database), MessageRepository(database)


def make_memory(service: MemoryService, content="原始记忆", key="pref:original"):
    return service.create_memory(MemoryCreate(MemoryType.USER_PREFERENCE, content, key, 0.7, 0.8))


def test_create_and_update_are_audited_with_old_and_new_values(history_context):
    _, service, _, _ = history_context
    memory = service.create_memory(MemoryCreate(MemoryType.USER_PREFERENCE, "喜欢简洁界面", "pref:ui", 0.7, 0.8), actor_type=MemoryActorType.USER, reason="用户明确表达")
    service.update_memory(memory.id, MemoryUpdate(content="喜欢深色简洁界面", canonical_key="pref:dark-ui"), actor_type=MemoryActorType.USER, reason="用户修正")

    changes = service.list_change_log(memory.id)
    assert [change.action for change in changes] == [MemoryChangeAction.CREATED, MemoryChangeAction.UPDATED]
    updated = changes[-1]
    assert updated.old_content == "喜欢简洁界面"
    assert updated.new_content == "喜欢深色简洁界面"
    assert updated.old_canonical_key == "pref:ui"
    assert updated.new_canonical_key == "pref:dark-ui"
    assert updated.actor_type is MemoryActorType.USER
    assert updated.reason == "用户修正"


def test_lifecycle_actions_are_audited(history_context):
    _, service, _, _ = history_context
    invalid = make_memory(service, key="pref:invalid")
    service.invalidate_memory(invalid.id, reason="事实不再适用")
    expired = make_memory(service, key="pref:expired")
    service.expire_memory(expired.id)
    old = make_memory(service, key="pref:old")
    replacement = make_memory(service, key="pref:new")
    service.supersede_memory(old.id, replacement.id, reason="新偏好替代旧偏好")
    deleted = make_memory(service, key="pref:deleted")
    service.soft_delete_memory(deleted.id, actor_type=MemoryActorType.USER)

    assert service.list_change_log(invalid.id)[-1].action is MemoryChangeAction.INVALIDATED
    assert service.list_change_log(expired.id)[-1].action is MemoryChangeAction.EXPIRED
    assert service.list_change_log(old.id)[-1].action is MemoryChangeAction.SUPERSEDED
    assert service.list_change_log(deleted.id)[-1].action is MemoryChangeAction.SOFT_DELETED


def test_permanent_delete_keeps_minimal_event_and_original_message(history_context):
    database, service, conversations, messages = history_context
    memory = make_memory(service, content="敏感的长期内容", key="pref:sensitive")
    conversation = conversations.create()
    message = messages.add(conversation["id"], "user", "敏感的来源消息")
    service.add_source(memory.id, message["id"], "user", "explicit_fact")
    service.permanently_delete_memory(memory.id, actor_type=MemoryActorType.USER, source_message_id=message["id"], reason="敏感的自由文本删除理由", idempotency_key=f"purge:{memory.id}")

    changes = [change for change in service.list_change_log() if change.action is MemoryChangeAction.PERMANENTLY_DELETED]
    assert len(changes) == 1
    deletion = changes[0]
    assert deletion.memory_id is None
    assert deletion.old_content is None
    assert deletion.new_content is None
    assert deletion.old_canonical_key is None
    assert deletion.new_canonical_key is None
    assert deletion.old_status is None
    assert deletion.new_status is None
    assert deletion.old_importance is None
    assert deletion.new_importance is None
    assert deletion.old_confidence is None
    assert deletion.new_confidence is None
    assert deletion.source_message_id is None
    assert deletion.reason is None
    assert messages.list_for_conversation(conversation["id"])[0]["id"] == message["id"]
    assert service.list_sources(memory.id) == []
    with database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM memory_items WHERE id = ?", (memory.id,)).fetchone()[0] == 0


def test_audit_failure_rolls_back_create_and_update(history_context, monkeypatch):
    _, service, _, _ = history_context
    repository = service.repository

    def fail(*_args, **_kwargs):
        raise RuntimeError("审计写入失败")

    monkeypatch.setattr(repository, "_insert_change_log", fail)
    with pytest.raises(RuntimeError):
        make_memory(service)
    assert service.list_memories(include_inactive=True) == []

    monkeypatch.undo()
    memory = make_memory(service)
    monkeypatch.setattr(repository, "_insert_change_log", fail)
    with pytest.raises(RuntimeError):
        service.update_memory(memory.id, MemoryUpdate(content="不应提交"))
    assert service.get_memory(memory.id).content == "原始记忆"
    assert len(service.list_change_log(memory.id)) == 1


def test_idempotent_create_and_lifecycle_operations(history_context):
    _, service, _, _ = history_context
    key = "create:message-1:pref"
    first = service.create_memory(MemoryCreate(MemoryType.USER_PREFERENCE, "可重复操作", "pref:idempotent"), idempotency_key=key)
    second = service.create_memory(MemoryCreate(MemoryType.USER_PREFERENCE, "可重复操作", "pref:idempotent"), idempotency_key=key)
    assert second.id == first.id
    assert len(service.list_change_log(first.id)) == 1

    status_key = "invalidate:message-1"
    service.invalidate_memory(first.id, idempotency_key=status_key)
    service.invalidate_memory(first.id, idempotency_key=status_key)
    assert len(service.list_change_log(first.id)) == 2
    assert service.get_memory(first.id, include_inactive=True).status is MemoryStatus.INVALID

    deleted = make_memory(service, key="pref:delete-idempotent")
    delete_key = "delete:memory-1"
    service.soft_delete_memory(deleted.id, idempotency_key=delete_key)
    service.soft_delete_memory(deleted.id, idempotency_key=delete_key)
    assert len(service.list_change_log(deleted.id)) == 2


def test_idempotency_key_cannot_cross_operation_or_memory(history_context):
    _, service, _, _ = history_context
    first = make_memory(service, key="pref:first")
    second = make_memory(service, key="pref:second")

    create_key = "shared:create-update"
    created = service.create_memory(MemoryCreate(MemoryType.USER_PREFERENCE, "创建内容", "pref:created"), idempotency_key=create_key)
    with pytest.raises(IdempotencyConflictError):
        service.update_memory(created.id, MemoryUpdate(content="更新内容"), idempotency_key=create_key)

    update_key = "shared:update"
    service.update_memory(first.id, MemoryUpdate(content="第一次更新"), idempotency_key=update_key)
    with pytest.raises(IdempotencyConflictError):
        service.update_memory(second.id, MemoryUpdate(content="第二次更新"), idempotency_key=update_key)
    with pytest.raises(IdempotencyConflictError):
        service.update_memory(first.id, MemoryUpdate(content="不同参数"), idempotency_key=update_key)

    status_key = "shared:status"
    service.invalidate_memory(first.id, idempotency_key=status_key)
    with pytest.raises(IdempotencyConflictError):
        service.expire_memory(first.id, idempotency_key=status_key)


def test_expires_at_is_lazy_and_does_not_create_expired_audit(history_context):
    _, service, _, _ = history_context
    memory = service.create_memory(MemoryCreate(MemoryType.USER_PREFERENCE, "临时内容", "pref:lazy-expiry", expires_at=(datetime.now(UTC) - timedelta(days=1)).isoformat()))

    assert service.get_memory(memory.id).status is MemoryStatus.ACTIVE
    assert [change.action for change in service.list_change_log(memory.id)] == [MemoryChangeAction.CREATED]


def test_existing_database_upgrade_preserves_data_and_adds_history_table(tmp_path):
    source = Path("backend/data/soulseed.db")
    if not source.exists():
        pytest.skip("当前工作区没有现有数据库文件")
    target = tmp_path / "upgraded.db"
    shutil.copy2(source, target)
    database = Database(str(target))
    with database.connection() as connection:
        before = (
            connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0],
            connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
        )
    database.initialize()
    with database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == before[0]
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == before[1]
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 6
        assert connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'memory_change_log'").fetchone()[0] == 1
