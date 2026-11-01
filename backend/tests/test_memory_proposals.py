from datetime import UTC, datetime
from threading import Barrier, Lock
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from app.db import Database
from app.memory import (
    CorrectionIntent,
    IdempotencyConflictError,
    MemoryChangeProposal,
    MemoryCreate,
    MemoryProposalRepository,
    MemoryProposalService,
    MemoryRelation,
    MemoryRepository,
    MemoryService,
    MemoryStatus,
    MemoryType,
    ProposalCreate,
    ProposalStatus,
    ProposalStateError,
    ProposalType,
    MemoryRetriever,
)
from app.repositories import ConversationRepository, MessageRepository


@pytest.fixture
def proposal_context(tmp_path):
    database = Database(str(tmp_path / "proposals.db"))
    database.initialize()
    conversations = ConversationRepository(database)
    messages = MessageRepository(database)
    memory_service = MemoryService(MemoryRepository(database))
    proposal_service = MemoryProposalService(MemoryProposalRepository(database))
    conversation = conversations.create()
    user_message = messages.add(conversation["id"], "user", "我之前说错了，我的目标是雅思 7 分。")
    assistant_message = messages.add(conversation["id"], "assistant", "我会记住这件事。")
    return database, memory_service, proposal_service, messages, user_message, assistant_message


def proposal(message_id, *, target_memory_id=None, key="proposal:create:1", content="我的目标是雅思 7 分。"):
    return ProposalCreate(
        proposal_type=ProposalType.CORRECTION,
        target_memory_id=target_memory_id,
        proposed_memory_type=MemoryType.GOAL,
        proposed_content=content,
        proposed_canonical_key="goal:ielts:7",
        evidence_message_id=message_id,
        correction_intent=CorrectionIntent.EXPLICIT_CORRECTION,
        relation=MemoryRelation.CONFLICT,
        confidence=0.88,
        evidence="候选包含明确纠正表达，且与目标主题相关。",
        idempotency_key=key,
    )


def test_create_get_list_and_audit_proposal(proposal_context):
    _, _, service, _, user_message, _ = proposal_context
    created = service.create_proposal(proposal(user_message["id"]))

    assert isinstance(created, MemoryChangeProposal)
    assert created.status is ProposalStatus.PENDING
    assert service.get_proposal(created.id).id == created.id
    assert service.list_proposals(status=ProposalStatus.PENDING)[0].id == created.id
    changes = service.list_changes(created.id)
    assert len(changes) == 1
    assert changes[0].action.value == "created"
    assert changes[0].new_status is ProposalStatus.PENDING


def test_all_legal_transitions_and_terminal_states(proposal_context):
    _, _, service, _, user_message, _ = proposal_context
    for index, method in enumerate((service.accept_proposal, service.reject_proposal, service.cancel_proposal, service.expire_proposal)):
        created = service.create_proposal(proposal(user_message["id"], key=f"proposal:transition:{index}"))
        updated = method(created.id, reason="用户确认的测试状态")
        assert updated.status in {ProposalStatus.ACCEPTED, ProposalStatus.REJECTED, ProposalStatus.CANCELLED, ProposalStatus.EXPIRED}
        assert updated.resolved_at is not None
        assert service.get_proposal(created.id).status is updated.status
        other_transition = service.reject_proposal if updated.status is not ProposalStatus.REJECTED else service.accept_proposal
        with pytest.raises(ProposalStateError):
            other_transition(created.id)


def test_same_terminal_operation_is_idempotent_but_other_operation_conflicts(proposal_context):
    _, _, service, _, user_message, _ = proposal_context
    created = service.create_proposal(proposal(user_message["id"]))
    accepted = service.accept_proposal(created.id, idempotency_key="proposal:accept:1")
    repeated = service.accept_proposal(created.id, idempotency_key="proposal:accept:1")
    assert repeated.id == accepted.id
    assert len(service.list_changes(created.id)) == 2
    with pytest.raises(IdempotencyConflictError):
        service.reject_proposal(created.id, idempotency_key="proposal:accept:1")


def test_idempotency_fingerprint_conflict_and_create_replay(proposal_context):
    _, _, service, _, user_message, _ = proposal_context
    first = service.create_proposal(proposal(user_message["id"]))
    replay = service.create_proposal(proposal(user_message["id"]))
    assert replay.id == first.id
    with pytest.raises(IdempotencyConflictError):
        service.create_proposal(proposal(user_message["id"], content="我的目标是雅思 8 分。"))


def test_invalid_source_and_assistant_source_are_rejected(proposal_context):
    _, _, service, _, user_message, assistant_message = proposal_context
    with pytest.raises(LookupError):
        service.create_proposal(proposal("missing-message"))
    with pytest.raises(ValueError):
        service.create_proposal(proposal(assistant_message["id"]))


def test_target_memory_is_set_null_after_permanent_delete(proposal_context):
    database, memory_service, proposal_service, messages, user_message, _ = proposal_context
    memory = memory_service.create_memory(MemoryCreate(MemoryType.GOAL, "我的目标是雅思 6.5 分。", "goal:ielts:6.5"))
    created = proposal_service.create_proposal(proposal(user_message["id"], target_memory_id=memory.id))
    memory_service.permanently_delete_memory(memory.id)

    loaded = proposal_service.get_proposal(created.id)
    assert loaded.target_memory_id is None
    assert messages.list_for_conversation(user_message["conversation_id"])[0]["id"] == user_message["id"]
    with database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM memory_change_proposals WHERE id = ?", (created.id,)).fetchone()[0] == 1


def test_historical_candidate_lookup_only_returns_correction_proposals(proposal_context):
    _, _, service, _, user_message, _ = proposal_context
    non_correction = service.create_proposal(
        replace(
            proposal(user_message["id"], key="proposal:update:same-candidate"),
            proposal_type=ProposalType.UPDATE,
        )
    )

    matches = service.find_by_source_and_candidate(
        user_message["id"],
        non_correction.proposed_memory_type,
        non_correction.proposed_content,
        non_correction.proposed_canonical_key,
    )

    assert non_correction.proposal_type is ProposalType.UPDATE
    assert matches == []


def test_accept_only_changes_proposal_and_does_not_create_or_replace_memory(proposal_context):
    _, memory_service, service, _, user_message, _ = proposal_context
    old = memory_service.create_memory(MemoryCreate(MemoryType.GOAL, "我的目标是雅思 6.5 分。", "goal:ielts:6.5"))
    created = service.create_proposal(proposal(user_message["id"], target_memory_id=old.id))
    service.accept_proposal(created.id)

    assert memory_service.list_memories(include_inactive=True)[0].id == old.id
    assert memory_service.get_memory(old.id).status is MemoryStatus.ACTIVE
    matches = MemoryRetriever(memory_service).retrieve("雅思 7 分")
    assert [match.memory.id for match in matches] == [old.id]


def test_transaction_failure_rolls_back_create_and_transition(proposal_context, monkeypatch):
    _, _, service, _, user_message, _ = proposal_context
    repository = service.repository

    def fail(*_args, **_kwargs):
        raise RuntimeError("proposal audit failure")

    monkeypatch.setattr(repository, "_insert_log", fail)
    with pytest.raises(RuntimeError):
        service.create_proposal(proposal(user_message["id"]))
    assert service.list_proposals() == []

    monkeypatch.undo()
    created = service.create_proposal(proposal(user_message["id"]))
    monkeypatch.setattr(repository, "_insert_log", fail)
    with pytest.raises(RuntimeError):
        service.reject_proposal(created.id)
    assert service.get_proposal(created.id).status is ProposalStatus.PENDING
    assert len(service.list_changes(created.id)) == 1


def test_proposal_is_not_returned_by_memory_retriever(proposal_context):
    _, memory_service, service, _, user_message, _ = proposal_context
    service.create_proposal(proposal(user_message["id"]))
    assert MemoryRetriever(memory_service).retrieve("雅思 7 分") == []


def _synchronize_first_idempotency_reads(monkeypatch, key):
    repository = MemoryProposalRepository
    original = repository._log_by_idempotency
    barrier = Barrier(2, timeout=5)
    lock = Lock()
    calls = {"count": 0}

    def synchronized(db, idempotency_key):
        result = original(db, idempotency_key)
        if idempotency_key == key:
            with lock:
                calls["count"] += 1
                should_wait = calls["count"] <= 2
            if should_wait:
                barrier.wait()
        return result

    monkeypatch.setattr(repository, "_log_by_idempotency", staticmethod(synchronized))


def test_concurrent_same_idempotency_key_replays_existing_proposal(proposal_context, monkeypatch):
    database, _, service, _, user_message, _ = proposal_context
    key = "proposal:concurrent:same"
    value = proposal(user_message["id"], key=key)
    _synchronize_first_idempotency_reads(monkeypatch, key)

    def create_once():
        return MemoryProposalService(MemoryProposalRepository(database)).create_proposal_with_outcome(value)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: create_once(), range(2)))

    assert sorted(result.created for result in results) == [False, True]
    assert len({result.proposal.id for result in results}) == 1
    assert len(service.list_proposals()) == 1
    assert len(service.list_changes(results[0].proposal.id)) == 1


def test_concurrent_same_idempotency_key_with_different_fingerprint_conflicts(proposal_context, monkeypatch):
    database, _, service, _, user_message, _ = proposal_context
    key = "proposal:concurrent:conflict"
    first = proposal(user_message["id"], key=key, content="我的目标是雅思 7 分。")
    second = proposal(user_message["id"], key=key, content="我的目标是雅思 8 分。")
    _synchronize_first_idempotency_reads(monkeypatch, key)

    def create_once(value):
        try:
            return ("ok", MemoryProposalService(MemoryProposalRepository(database)).create_proposal_with_outcome(value))
        except IdempotencyConflictError:
            return ("conflict", None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create_once, (first, second)))

    assert sorted(result[0] for result in results) == ["conflict", "ok"]
    assert len(service.list_proposals()) == 1
    proposal_id = next(result[1].proposal.id for result in results if result[1] is not None)
    assert len(service.list_changes(proposal_id)) == 1
