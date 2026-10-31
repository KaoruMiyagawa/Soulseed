import pytest

from app.db import Database
from app.memory import (
    CorrectionFlowStatus,
    CorrectionFlowService,
    CorrectionIntent,
    DeterministicMemoryExtractor,
    EvidenceType,
    MemoryCreate,
    MemoryCandidate,
    MemoryProposalRepository,
    MemoryProposalService,
    MemoryRepository,
    MemoryService,
    MemoryStatus,
    MemoryType,
    MemoryUpdate,
    ProposalStatus,
)
from app.repositories import ConversationRepository, MessageRepository


@pytest.fixture
def flow_context(tmp_path):
    database = Database(str(tmp_path / "correction-flow.db"))
    database.initialize()
    conversations = ConversationRepository(database)
    messages = MessageRepository(database)
    memory_service = MemoryService(MemoryRepository(database))
    proposal_service = MemoryProposalService(MemoryProposalRepository(database))
    flow = CorrectionFlowService(memory_service, proposal_service)
    conversation = conversations.create()
    return database, conversations, messages, memory_service, proposal_service, flow, conversation


def add_user_message(messages, conversation, content, *, role="user"):
    return messages.add(conversation["id"], role, content)


def candidate(text):
    extracted = DeterministicMemoryExtractor().extract_text(text)
    assert extracted, text
    return extracted[0]


def add_memory(memory_service, memory_type, content, canonical_key):
    return memory_service.create_memory(MemoryCreate(memory_type, content, canonical_key))


def test_none_does_not_create_proposal(flow_context):
    _, _, messages, _, proposal_service, flow, conversation = flow_context
    message = add_user_message(messages, conversation, "我喜欢简洁的界面。")

    result = flow.process(message, [candidate(message["content"])])[0]

    assert result.status is CorrectionFlowStatus.NO_ACTION
    assert proposal_service.list_proposals() == []


def test_explicit_correction_with_unique_conflict_creates_proposal(flow_context):
    _, _, messages, memory_service, proposal_service, flow, conversation = flow_context
    old = add_memory(memory_service, MemoryType.GOAL, "我的目标是雅思 6.5 分。", "goal:ielts:6.5")
    message = add_user_message(messages, conversation, "我之前说错了，我的目标是雅思 7 分。")

    result = flow.process(message, [candidate(message["content"])])[0]

    assert result.status is CorrectionFlowStatus.PROPOSAL_CREATED
    assert result.proposal is not None
    assert result.proposal.target_memory_id == old.id
    created = proposal_service.get_proposal(result.proposal.proposal_id)
    assert created.target_memory_id == old.id
    assert created.status.value == "pending"


def test_long_term_change_with_unique_conflict_creates_proposal(flow_context):
    _, _, messages, memory_service, proposal_service, flow, conversation = flow_context
    old = add_memory(memory_service, MemoryType.USER_PREFERENCE, "我喜欢 Python。", "preference:python")
    message = add_user_message(messages, conversation, "我现在更喜欢 Rust 了。")

    result = flow.process(message, [candidate(message["content"])])[0]

    assert result.status is CorrectionFlowStatus.PROPOSAL_CREATED
    assert result.proposal.target_memory_id == old.id
    assert len(proposal_service.list_proposals()) == 1


def test_long_term_learning_objects_require_clarification_and_do_not_create_proposal(flow_context):
    _, _, messages, memory_service, proposal_service, flow, conversation = flow_context
    add_memory(memory_service, MemoryType.GROWTH, "我正在学习 C++。", "growth:cpp")
    message = add_user_message(messages, conversation, "我现在已经改为学习 Rust。")

    result = flow.process(message, [candidate(message["content"])])[0]

    assert result.status is CorrectionFlowStatus.CLARIFICATION_NEEDED
    assert result.proposal is None
    assert proposal_service.list_proposals() == []


def test_external_long_term_temporary_python_attempt_does_not_create_proposal(flow_context):
    _, _, messages, memory_service, proposal_service, flow, conversation = flow_context
    add_memory(memory_service, MemoryType.GOAL, "我计划学习 Python。", "goal:python")
    message = add_user_message(messages, conversation, "我最近尝试了 Python。")
    temporary_candidate = MemoryCandidate(
        memory_type=MemoryType.GOAL,
        content=message["content"],
        canonical_key="goal:python:attempt",
        importance=0.5,
        confidence=0.8,
        evidence_type=EvidenceType.EXPLICIT_FACT,
        excerpt=message["content"],
        intent=CorrectionIntent.LONG_TERM_CHANGE,
    )

    result = flow.process(message, [temporary_candidate])[0]

    assert result.status is CorrectionFlowStatus.COEXISTING
    assert result.proposal is None
    assert proposal_service.list_proposals() == []


def test_external_long_term_one_time_linux_run_does_not_create_proposal(flow_context):
    _, _, messages, memory_service, proposal_service, flow, conversation = flow_context
    add_memory(memory_service, MemoryType.USER_PREFERENCE, "我主要使用 Windows。", "preference:windows")
    message = add_user_message(messages, conversation, "我最近在 Linux 上运行过一次程序。")
    one_time_candidate = MemoryCandidate(
        memory_type=MemoryType.USER_PREFERENCE,
        content=message["content"],
        canonical_key="preference:linux:one-time",
        importance=0.5,
        confidence=0.8,
        evidence_type=EvidenceType.EXPLICIT_FACT,
        excerpt=message["content"],
        intent=CorrectionIntent.LONG_TERM_CHANGE,
    )

    result = flow.process(message, [one_time_candidate])[0]

    assert result.status is not CorrectionFlowStatus.PROPOSAL_CREATED
    assert result.proposal is None
    assert proposal_service.list_proposals() == []


def test_temporary_experiment_does_not_create_correction_proposal(flow_context):
    _, _, messages, memory_service, proposal_service, flow, conversation = flow_context
    add_memory(memory_service, MemoryType.GROWTH, "我平时每周健身三次。", "growth:exercise-frequency")
    message = add_user_message(messages, conversation, "这周我想暂时减少健身频率。")

    result = flow.process(message, [candidate(message["content"])])[0]

    assert result.status is not CorrectionFlowStatus.PROPOSAL_CREATED
    assert proposal_service.list_proposals() == []


def test_ambiguous_change_requests_clarification(flow_context):
    _, _, messages, memory_service, proposal_service, flow, conversation = flow_context
    add_memory(memory_service, MemoryType.USER_PREFERENCE, "我喜欢 Python。", "preference:python")
    message = add_user_message(messages, conversation, "Rust 好像也不错。")

    result = flow.process(message, [candidate(message["content"])])[0]

    assert result.status is CorrectionFlowStatus.CLARIFICATION_NEEDED
    assert result.proposal is None
    assert result.clarification
    assert proposal_service.list_proposals() == []


def test_coexisting_information_preserves_both(flow_context):
    _, _, messages, memory_service, proposal_service, flow, conversation = flow_context
    add_memory(memory_service, MemoryType.USER_PREFERENCE, "我喜欢 Python。", "preference:python")
    message = add_user_message(messages, conversation, "我也喜欢 Rust。")

    result = flow.process(message, [candidate(message["content"])])[0]

    assert result.status is CorrectionFlowStatus.COEXISTING
    assert proposal_service.list_proposals() == []


def test_unrelated_memory_does_not_create_proposal(flow_context):
    _, _, messages, memory_service, proposal_service, flow, conversation = flow_context
    add_memory(memory_service, MemoryType.GOAL, "我的目标是完成 Soulseed 项目。", "goal:soulseed")
    message = add_user_message(messages, conversation, "我之前说错了，我喜欢 Rust。")

    result = flow.process(message, [candidate(message["content"])])[0]

    assert result.status is CorrectionFlowStatus.NO_ACTION
    assert proposal_service.list_proposals() == []


def test_no_related_active_memory_does_not_create_proposal(flow_context):
    _, _, messages, _, proposal_service, flow, conversation = flow_context
    message = add_user_message(messages, conversation, "我现在更喜欢 Rust 了。")

    result = flow.process(message, [candidate(message["content"])])[0]

    assert result.status is CorrectionFlowStatus.NO_ACTION
    assert proposal_service.list_proposals() == []


def test_multiple_targets_request_clarification_without_selecting_first(flow_context):
    _, _, messages, memory_service, proposal_service, flow, conversation = flow_context
    first = add_memory(memory_service, MemoryType.GOAL, "我的目标是雅思 6 分。", "goal:ielts:6")
    second = add_memory(memory_service, MemoryType.GOAL, "我的目标是雅思 6.5 分。", "goal:ielts:6.5")
    message = add_user_message(messages, conversation, "我之前说错了，我的目标是雅思 7 分。")

    result = flow.process(message, [candidate(message["content"])])[0]

    assert result.status is CorrectionFlowStatus.CLARIFICATION_NEEDED
    assert result.proposal is None
    assert {first.id, second.id} == set(
        item.removeprefix("候选目标：") for item in result.clarification
    )
    assert proposal_service.list_proposals() == []


@pytest.mark.parametrize("role", ["assistant", "system"])
def test_non_user_source_is_rejected_without_proposal(flow_context, role):
    _, _, messages, memory_service, proposal_service, flow, conversation = flow_context
    add_memory(memory_service, MemoryType.GOAL, "我的目标是雅思 6.5 分。", "goal:ielts:6.5")
    message = add_user_message(messages, conversation, "我之前说错了，我的目标是雅思 7 分。", role=role)

    result = flow.process(message, [candidate(message["content"])])[0]

    assert result.status is CorrectionFlowStatus.PROCESSING_FAILED
    assert result.error_code == "invalid_user_message"
    assert proposal_service.list_proposals() == []


def test_missing_source_message_is_rejected_by_proposal_repository(flow_context):
    _, _, _, memory_service, proposal_service, flow, conversation = flow_context
    add_memory(memory_service, MemoryType.GOAL, "我的目标是雅思 6.5 分。", "goal:ielts:6.5")
    fake_message = {"id": "missing-message", "conversation_id": conversation["id"], "role": "user", "content": "我之前说错了，我的目标是雅思 7 分。"}

    result = flow.process(fake_message, [candidate(fake_message["content"])])[0]

    assert result.status is CorrectionFlowStatus.PROCESSING_FAILED
    assert result.error_code == "proposal_creation_failed"
    assert proposal_service.list_proposals() == []


def test_reprocessing_same_message_is_idempotent(flow_context):
    _, _, messages, memory_service, proposal_service, flow, conversation = flow_context
    add_memory(memory_service, MemoryType.GOAL, "我的目标是雅思 6.5 分。", "goal:ielts:6.5")
    message = add_user_message(messages, conversation, "我之前说错了，我的目标是雅思 7 分。")
    extracted = candidate(message["content"])

    first = flow.process(message, [extracted])[0]
    second = flow.process(message, [extracted])[0]

    assert first.status is CorrectionFlowStatus.PROPOSAL_CREATED
    assert second.status is CorrectionFlowStatus.PROPOSAL_REUSED
    assert second.proposal.proposal_id == first.proposal.proposal_id
    assert len(proposal_service.list_proposals()) == 1
    assert len(proposal_service.list_changes(first.proposal.proposal_id)) == 1


def test_proposal_creation_failure_is_structured_and_not_no_action(flow_context):
    _, _, messages, memory_service, _, _, conversation = flow_context

    class FailingProposalService:
        def create_proposal_with_outcome(self, *_args, **_kwargs):
            raise RuntimeError("proposal store unavailable")

    flow = CorrectionFlowService(memory_service, FailingProposalService())
    add_memory(memory_service, MemoryType.GOAL, "我的目标是雅思 6.5 分。", "goal:ielts:6.5")
    message = add_user_message(messages, conversation, "我之前说错了，我的目标是雅思 7 分。")

    result = flow.process(message, [candidate(message["content"])])[0]

    assert result.status is CorrectionFlowStatus.PROCESSING_FAILED
    assert result.error_code == "proposal_creation_failed"
    assert result.status is not CorrectionFlowStatus.NO_ACTION


def test_target_change_between_second_read_and_create_is_rejected(flow_context):
    database, _, messages, memory_service, proposal_service, _, conversation = flow_context
    old = add_memory(memory_service, MemoryType.GOAL, "我的目标是雅思 6.5 分。", "goal:ielts:6.5")

    class RacingProposalService:
        def create_proposal_with_outcome(self, proposal, **kwargs):
            memory_service.update_memory(old.id, MemoryUpdate(content="我的目标是雅思 6 分。"))
            return proposal_service.create_proposal_with_outcome(proposal, **kwargs)

    flow = CorrectionFlowService(memory_service, RacingProposalService())
    message = add_user_message(messages, conversation, "我之前说错了，我的目标是雅思 7 分。")

    result = flow.process(message, [candidate(message["content"])])[0]

    assert result.status is CorrectionFlowStatus.PROCESSING_FAILED
    assert result.error_code == "target_changed_during_creation"
    assert proposal_service.list_proposals() == []
    with database.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM memory_change_proposals").fetchone()[0] == 0


def test_target_becoming_inactive_between_second_read_and_create_is_rejected(flow_context):
    _, _, messages, memory_service, proposal_service, _, conversation = flow_context
    old = add_memory(memory_service, MemoryType.GOAL, "我的目标是雅思 6.5 分。", "goal:ielts:6.5")

    class RacingProposalService:
        def create_proposal_with_outcome(self, proposal, **kwargs):
            memory_service.invalidate_memory(old.id)
            return proposal_service.create_proposal_with_outcome(proposal, **kwargs)

    flow = CorrectionFlowService(memory_service, RacingProposalService())
    message = add_user_message(messages, conversation, "我之前说错了，我的目标是雅思 7 分。")

    result = flow.process(message, [candidate(message["content"])])[0]

    assert result.status is CorrectionFlowStatus.PROCESSING_FAILED
    assert result.error_code == "target_inactive_during_creation"
    assert proposal_service.list_proposals() == []


def test_expected_version_is_saved_from_detection_snapshot(flow_context):
    _, _, messages, memory_service, proposal_service, flow, conversation = flow_context
    old = add_memory(memory_service, MemoryType.GOAL, "我的目标是雅思 6.5 分。", "goal:ielts:6.5")
    expected_version = old.updated_at
    message = add_user_message(messages, conversation, "我之前说错了，我的目标是雅思 7 分。")

    result = flow.process(message, [candidate(message["content"])])[0]
    created = proposal_service.get_proposal(result.proposal.proposal_id)

    assert created.target_memory_updated_at == expected_version


def test_low_confidence_explicit_potential_conflict_requests_clarification(flow_context):
    _, _, messages, memory_service, proposal_service, flow, conversation = flow_context
    add_memory(memory_service, MemoryType.USER_PREFERENCE, "我喜欢 Python。", "preference:python")
    message = add_user_message(messages, conversation, "我之前说错了，我其实偏好 Rust。")
    candidate_value = MemoryCandidate(
        memory_type=MemoryType.USER_PREFERENCE,
        content=message["content"],
        canonical_key="user_preference:rust",
        importance=0.68,
        confidence=0.95,
        evidence_type=EvidenceType.CORRECTION,
        excerpt=message["content"],
        intent=CorrectionIntent.EXPLICIT_CORRECTION,
    )

    result = flow.process(message, [candidate_value])[0]

    assert result.status is CorrectionFlowStatus.CLARIFICATION_NEEDED
    assert result.proposal is None
    assert proposal_service.list_proposals() == []


@pytest.mark.parametrize(
    ("terminal", "expected_status"),
    [
        (ProposalStatus.ACCEPTED, CorrectionFlowStatus.PROPOSAL_ACCEPTED),
        (ProposalStatus.REJECTED, CorrectionFlowStatus.PROPOSAL_REJECTED),
        (ProposalStatus.CANCELLED, CorrectionFlowStatus.PROPOSAL_CANCELLED),
        (ProposalStatus.EXPIRED, CorrectionFlowStatus.PROPOSAL_EXPIRED),
    ],
)
def test_terminal_proposal_replay_preserves_terminal_status(flow_context, terminal, expected_status):
    _, _, messages, memory_service, proposal_service, flow, conversation = flow_context
    add_memory(memory_service, MemoryType.GOAL, "我的目标是雅思 6.5 分。", "goal:ielts:6.5")
    message = add_user_message(messages, conversation, "我之前说错了，我的目标是雅思 7 分。")
    first = flow.process(message, [candidate(message["content"])])[0]
    proposal_id = first.proposal.proposal_id
    transitions = {
        ProposalStatus.ACCEPTED: proposal_service.accept_proposal,
        ProposalStatus.REJECTED: proposal_service.reject_proposal,
        ProposalStatus.CANCELLED: proposal_service.cancel_proposal,
        ProposalStatus.EXPIRED: proposal_service.expire_proposal,
    }
    transitions[terminal](proposal_id)

    replay = flow.process(message, [candidate(message["content"])])[0]

    assert replay.status is expected_status
    assert replay.status is not CorrectionFlowStatus.PROPOSAL_CREATED
    assert replay.proposal.proposal_id == proposal_id


def test_flow_does_not_modify_formal_memory(flow_context):
    database, _, messages, memory_service, proposal_service, flow, conversation = flow_context
    old = add_memory(memory_service, MemoryType.GOAL, "我的目标是雅思 6.5 分。", "goal:ielts:6.5")
    with database.connection() as db:
        before_memory = dict(db.execute("SELECT content, canonical_key, status, updated_at, last_accessed_at, superseded_by_id FROM memory_items WHERE id = ?", (old.id,)).fetchone())
    before_log = memory_service.list_change_log(old.id)
    message = add_user_message(messages, conversation, "我之前说错了，我的目标是雅思 7 分。")

    result = flow.process(message, [candidate(message["content"])])[0]

    assert result.status is CorrectionFlowStatus.PROPOSAL_CREATED
    with database.connection() as db:
        after_memory = dict(db.execute("SELECT content, canonical_key, status, updated_at, last_accessed_at, superseded_by_id FROM memory_items WHERE id = ?", (old.id,)).fetchone())
    assert after_memory == before_memory
    assert memory_service.list_change_log(old.id) == before_log
    assert len(memory_service.list_memories(include_inactive=True)) == 1
    assert proposal_service.get_proposal(result.proposal.proposal_id).application_status.value == "not_applied"
    with database.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM memory_proposal_application_log").fetchone()[0] == 0


def test_inactive_memories_are_not_targets(flow_context):
    _, _, messages, memory_service, proposal_service, flow, conversation = flow_context
    old = add_memory(memory_service, MemoryType.USER_PREFERENCE, "我喜欢 Python。", "preference:python")
    memory_service.invalidate_memory(old.id)
    message = add_user_message(messages, conversation, "我现在更喜欢 Rust 了。")

    result = flow.process(message, [candidate(message["content"])])[0]

    assert result.status is CorrectionFlowStatus.NO_ACTION
    assert proposal_service.list_proposals() == []
    assert memory_service.get_memory(old.id, include_inactive=True).status is MemoryStatus.INVALID
