from concurrent.futures import ThreadPoolExecutor

import pytest

from app.db import Database
from app.memory import (
    CorrectionIntent,
    MemoryCorrectionService,
    MemoryCreate,
    MemoryProposalRepository,
    MemoryProposalService,
    MemoryRepository,
    MemoryService,
    MemoryStatus,
    MemoryType,
    MemoryUpdate,
    ProposalCreate,
    ProposalStateError,
    ProposalStatus,
    ProposalType,
)
from app.repositories import ConversationRepository, MessageRepository


@pytest.fixture
def correction_context(tmp_path):
    database = Database(str(tmp_path / "correction.db"))
    database.initialize()
    conversations = ConversationRepository(database)
    messages = MessageRepository(database)
    memory_service = MemoryService(MemoryRepository(database))
    proposals = MemoryProposalService(MemoryProposalRepository(database))
    correction = MemoryCorrectionService(database)
    conversation = conversations.create()
    message = messages.add(conversation["id"], "user", "我之前说错了，我的目标是雅思 7 分。")
    return database, memory_service, proposals, correction, messages, message


def make_proposal(message_id, *, target_memory_id=None, key="correction:create", proposal_type=ProposalType.CORRECTION):
    return ProposalCreate(
        proposal_type=proposal_type,
        target_memory_id=target_memory_id,
        proposed_memory_type=MemoryType.GOAL,
        proposed_content="我的目标是雅思 7 分。",
        proposed_canonical_key="goal:ielts:7",
        evidence_message_id=message_id,
        correction_intent=CorrectionIntent.EXPLICIT_CORRECTION,
        relation="conflict",
        confidence=0.9,
        evidence="用户消息明确纠正了目标。",
        idempotency_key=key,
    )


def test_accepted_proposal_atomically_supersedes_target_and_records_trace(correction_context):
    database, memories, proposals, correction, messages, message = correction_context
    old = memories.create_memory(MemoryCreate(MemoryType.GOAL, "我的目标是雅思 6.5 分。", "goal:ielts:6.5"))
    proposal = proposals.create_proposal(make_proposal(message["id"], target_memory_id=old.id))
    proposals.accept_proposal(proposal.id)

    result = correction.apply_proposal(proposal.id, idempotency_key="apply:1")

    assert result.proposal.application_status.value == "applied"
    assert result.applied_memory.status is MemoryStatus.SUPERSEDED
    assert result.replacement_memory.status is MemoryStatus.ACTIVE
    assert result.applied_memory.superseded_by_id == result.replacement_memory.id
    source = memories.list_sources(result.replacement_memory.id)[0]
    assert source.message_id == message["id"]
    assert source.excerpt == message["content"]
    with database.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM memory_proposal_application_log WHERE proposal_id = ?", (proposal.id,)).fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM memory_change_log WHERE proposal_id = ?", (proposal.id,)).fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM messages WHERE id = ?", (message["id"],)).fetchone()[0] == 1


@pytest.mark.parametrize("terminal", [None, ProposalStatus.REJECTED, ProposalStatus.CANCELLED, ProposalStatus.EXPIRED])
def test_only_accepted_proposal_can_apply(correction_context, terminal):
    _, _, proposals, correction, _, message = correction_context
    proposal = proposals.create_proposal(make_proposal(message["id"], key=f"state:{terminal}"))
    if terminal is not None:
        transition = {
            ProposalStatus.REJECTED: proposals.reject_proposal,
            ProposalStatus.CANCELLED: proposals.cancel_proposal,
            ProposalStatus.EXPIRED: proposals.expire_proposal,
        }[terminal]
        transition(proposal.id)
    with pytest.raises(ProposalStateError):
        correction.apply_proposal(proposal.id)


def test_repeated_application_is_idempotent(correction_context):
    database, memories, proposals, correction, _, message = correction_context
    proposal = proposals.create_proposal(make_proposal(message["id"]))
    proposals.accept_proposal(proposal.id)
    first = correction.apply_proposal(proposal.id, idempotency_key="apply:retry")
    second = correction.apply_proposal(proposal.id, idempotency_key="apply:retry")

    assert second.replacement_memory.id == first.replacement_memory.id
    with database.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM memory_proposal_application_log WHERE proposal_id = ?", (proposal.id,)).fetchone()[0] == 1


def test_concurrent_application_has_one_effect(correction_context):
    database, _, proposals, _, _, message = correction_context
    proposal = proposals.create_proposal(make_proposal(message["id"]))
    proposals.accept_proposal(proposal.id)

    def apply_once():
        return MemoryCorrectionService(database).apply_proposal(proposal.id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: apply_once(), range(2)))

    assert results[0].replacement_memory.id == results[1].replacement_memory.id
    with database.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM memory_proposal_application_log").fetchone()[0] == 1


def test_changed_target_is_rejected_without_partial_application(correction_context):
    database, memories, proposals, correction, _, message = correction_context
    old = memories.create_memory(MemoryCreate(MemoryType.GOAL, "我的目标是雅思 6.5 分。", "goal:ielts:6.5"))
    proposal = proposals.create_proposal(make_proposal(message["id"], target_memory_id=old.id))
    proposals.accept_proposal(proposal.id)
    memories.update_memory(old.id, MemoryUpdate(content="我的目标是雅思 6 分。"))

    with pytest.raises(Exception, match="发生变化"):
        correction.apply_proposal(proposal.id)
    assert memories.get_memory(old.id).status is MemoryStatus.ACTIVE
    with database.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0] == 1
        assert db.execute("SELECT application_status FROM memory_change_proposals WHERE id = ?", (proposal.id,)).fetchone()[0] == "not_applied"


def test_failure_rolls_back_and_can_retry(correction_context, monkeypatch):
    database, memories, proposals, correction, _, message = correction_context
    proposal = proposals.create_proposal(make_proposal(message["id"]))
    proposals.accept_proposal(proposal.id)

    def fail(*_args, **_kwargs):
        raise RuntimeError("source write failed")

    monkeypatch.setattr(correction, "_insert_source", fail)
    with pytest.raises(RuntimeError):
        correction.apply_proposal(proposal.id)
    assert memories.list_memories() == []
    assert proposals.get_proposal(proposal.id).application_status.value == "not_applied"

    monkeypatch.undo()
    result = correction.apply_proposal(proposal.id)
    assert result.replacement_memory is not None
