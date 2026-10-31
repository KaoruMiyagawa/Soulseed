import pytest
from fastapi.testclient import TestClient

from app.chat_service import ChatService
from app.db import Database
from app.memory import (
    CorrectionFlowResult,
    CorrectionFlowService,
    CorrectionFlowStatus,
    CorrectionIntent,
    DeterministicMemoryExtractor,
    EvidenceType,
    MemoryCandidate,
    MemoryCreate,
    MemoryProposalRepository,
    MemoryProposalService,
    MemoryRepository,
    MemoryService,
    MemoryType,
    MemoryRelation,
)
from app.memory.correction_flow_service import CorrectionProposalSummary
from app.model_adapter import ModelRequestError
from app.repositories import ConversationRepository, MessageRepository


class FakeModel:
    async def reply(self, _messages):
        return "收到。"


class CountingExtractor:
    def __init__(self, candidates):
        self.candidates = candidates
        self.calls = 0

    def extract(self, _message):
        self.calls += 1
        return list(self.candidates)


class RecordingFlow:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def process(self, message, candidates):
        self.calls.append((message, tuple(candidates)))
        return (self.result,) * len(tuple(candidates))


def candidate(content, *, intent=CorrectionIntent.NONE, memory_type=MemoryType.USER_PREFERENCE, key="pref:test"):
    return MemoryCandidate(
        memory_type=memory_type,
        content=content,
        canonical_key=key,
        importance=0.5,
        confidence=0.8,
        evidence_type=EvidenceType.CORRECTION if intent is not CorrectionIntent.NONE else EvidenceType.EXPLICIT_FACT,
        excerpt=content,
        intent=intent,
    )


@pytest.fixture
def chat_context(tmp_path):
    database = Database(str(tmp_path / "chat-correction.db"))
    database.initialize()
    conversations = ConversationRepository(database)
    messages = MessageRepository(database)
    memory_service = MemoryService(MemoryRepository(database))
    conversation = conversations.create()
    return database, conversations, messages, memory_service, conversation


def proposal_result():
    summary = CorrectionProposalSummary(
        proposal_id="proposal-1",
        target_memory_id="memory-1",
        relation=MemoryRelation.CONFLICT,
        confidence=0.9,
        evidence=("明确纠正",),
    )
    return CorrectionFlowResult(
        status=CorrectionFlowStatus.PROPOSAL_CREATED,
        reason="created",
        proposal=summary,
    )


def test_correction_summary_uses_mixed_for_different_candidate_statuses():
    summary = ChatService._correction_summary(
        (
            proposal_result(),
            CorrectionFlowResult(
                status=CorrectionFlowStatus.NO_ACTION,
                reason="no correction",
            ),
        )
    )

    assert summary["status"] == "mixed"
    assert [item["status"] for item in summary["items"]] == ["proposal_created", "no_action"]


def test_chat_http_response_keeps_message_fields_and_adds_correction(monkeypatch, tmp_path):
    import app.main as main_module

    class StubChat:
        async def send(self, _conversation_id, _content):
            return {
                "id": "assistant-1",
                "conversation_id": "conversation-1",
                "role": "assistant",
                "content": "收到。",
                "created_at": "2026-01-01T00:00:00+00:00",
                "correction": {"status": "no_action", "items": []},
            }

    monkeypatch.setattr(main_module, "database", Database(str(tmp_path / "api.db")))
    monkeypatch.setattr(main_module, "chat", StubChat())

    with TestClient(main_module.app) as client:
        response = client.post(
            "/api/conversations/conversation-1/messages",
            json={"content": "你好"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["role"] == "assistant"
    assert payload["content"] == "收到。"
    assert payload["correction"] == {"status": "no_action", "items": []}


@pytest.mark.asyncio
async def test_chat_extracts_once_splits_candidates_and_preserves_response_fields(chat_context):
    _, conversations, messages, memory_service, conversation = chat_context
    normal = candidate("我喜欢简洁界面。")
    correction = candidate(
        "我之前说错了，我喜欢 Rust。",
        intent=CorrectionIntent.EXPLICIT_CORRECTION,
        key="pref:rust",
    )
    extractor = CountingExtractor([normal, correction])
    flow = RecordingFlow(proposal_result())
    chat = ChatService(
        conversations,
        messages,
        FakeModel(),
        "人格",
        memory_service,
        extractor,
        correction_flow=flow,
    )

    result = await chat.send(conversation["id"], "组合消息")

    assert extractor.calls == 1
    assert len(flow.calls) == 1
    assert flow.calls[0][1] == (correction,)
    assert len(memory_service.list_memories()) == 1
    assert result["role"] == "assistant"
    assert result["content"] == "收到。"
    assert result["correction"] == {
        "status": "proposal_created",
        "items": [
            {
                "status": "proposal_created",
                "proposal_id": "proposal-1",
                "target_memory_id": "memory-1",
                "relation": "conflict",
                "confidence": 0.9,
                "evidence": ["明确纠正"],
            }
        ],
    }


@pytest.mark.asyncio
async def test_chat_without_flow_keeps_legacy_mode_and_reports_unavailable_correction(chat_context):
    _, conversations, messages, memory_service, conversation = chat_context
    extractor = CountingExtractor([
        candidate("我之前说错了，我喜欢 Rust。", intent=CorrectionIntent.EXPLICIT_CORRECTION)
    ])
    chat = ChatService(
        conversations,
        messages,
        FakeModel(),
        "人格",
        memory_service,
        extractor,
    )

    result = await chat.send(conversation["id"], "纠正")

    assert result["content"] == "收到。"
    assert result["correction"]["status"] == "processing_failed"
    assert result["correction"]["items"][0]["error_code"] == "correction_flow_unavailable"
    assert memory_service.list_memories() == []


@pytest.mark.asyncio
async def test_ordinary_chat_returns_no_action_correction_summary(chat_context):
    _, conversations, messages, memory_service, conversation = chat_context
    chat = ChatService(
        conversations,
        messages,
        FakeModel(),
        "人格",
        memory_service,
        CountingExtractor([]),
    )

    result = await chat.send(conversation["id"], "你好")

    assert result["correction"] == {"status": "no_action", "items": []}


@pytest.mark.asyncio
async def test_processing_failed_flow_does_not_block_model_reply(chat_context):
    _, conversations, messages, memory_service, conversation = chat_context
    flow = RecordingFlow(
        CorrectionFlowResult(
            status=CorrectionFlowStatus.PROCESSING_FAILED,
            reason="proposal unavailable",
            error_code="proposal_creation_failed",
        )
    )
    chat = ChatService(
        conversations,
        messages,
        FakeModel(),
        "人格",
        memory_service,
        CountingExtractor([
            candidate("我之前说错了，我喜欢 Rust。", intent=CorrectionIntent.EXPLICIT_CORRECTION)
        ]),
        correction_flow=flow,
    )

    result = await chat.send(conversation["id"], "纠正")

    assert result["content"] == "收到。"
    assert result["correction"] == {
        "status": "processing_failed",
        "items": [{"status": "processing_failed", "error_code": "proposal_creation_failed"}],
    }
    assert [message["role"] for message in messages.list_for_conversation(conversation["id"])] == ["user", "assistant"]


@pytest.mark.asyncio
async def test_unexpected_flow_exception_isolated_from_chat(chat_context):
    _, conversations, messages, memory_service, conversation = chat_context

    class BrokenFlow:
        def process(self, _message, _candidates):
            raise RuntimeError("unexpected")

    chat = ChatService(
        conversations,
        messages,
        FakeModel(),
        "人格",
        memory_service,
        CountingExtractor([
            candidate("我之前说错了，我喜欢 Rust。", intent=CorrectionIntent.EXPLICIT_CORRECTION)
        ]),
        correction_flow=BrokenFlow(),
    )

    result = await chat.send(conversation["id"], "纠正")

    assert result["content"] == "收到。"
    assert result["correction"]["status"] == "processing_failed"
    assert result["correction"]["items"][0]["error_code"] == "correction_flow_unexpected_error"


@pytest.mark.asyncio
async def test_chat_preserves_partial_flow_results_and_reports_mixed(chat_context):
    _, conversations, messages, memory_service, conversation = chat_context
    third_summary = CorrectionProposalSummary(
        proposal_id="proposal-3",
        target_memory_id="memory-3",
        relation=MemoryRelation.CONFLICT,
        confidence=0.9,
        evidence=("第三候选",),
    )

    class PartialFlow:
        def process(self, _message, candidates):
            assert len(tuple(candidates)) == 3
            return (
                proposal_result(),
                CorrectionFlowResult(
                    status=CorrectionFlowStatus.PROCESSING_FAILED,
                    reason="candidate failed",
                    error_code="candidate_processing_failed",
                ),
                CorrectionFlowResult(
                    status=CorrectionFlowStatus.PROPOSAL_CREATED,
                    reason="created",
                    proposal=third_summary,
                ),
            )

    candidates = [
        candidate("我之前说错了，我喜欢 Rust。", intent=CorrectionIntent.EXPLICIT_CORRECTION, key="pref:rust"),
        candidate("我之前说错了，我喜欢 Go。", intent=CorrectionIntent.EXPLICIT_CORRECTION, key="pref:go"),
        candidate("我之前说错了，我喜欢 Zig。", intent=CorrectionIntent.EXPLICIT_CORRECTION, key="pref:zig"),
    ]
    chat = ChatService(
        conversations,
        messages,
        FakeModel(),
        "人格",
        memory_service,
        CountingExtractor(candidates),
        correction_flow=PartialFlow(),
    )

    response = await chat.send(conversation["id"], "多候选纠正")

    assert response["correction"]["status"] == "mixed"
    assert [item["status"] for item in response["correction"]["items"]] == [
        "proposal_created",
        "processing_failed",
        "proposal_created",
    ]
    assert response["correction"]["items"][0]["proposal_id"] == "proposal-1"
    assert response["correction"]["items"][2]["proposal_id"] == "proposal-3"


@pytest.mark.asyncio
async def test_clarification_summary_has_no_proposal_id(chat_context):
    _, conversations, messages, memory_service, conversation = chat_context
    result = CorrectionFlowResult(
        status=CorrectionFlowStatus.CLARIFICATION_NEEDED,
        reason="ambiguous",
        clarification=("请确认长期变化还是暂时尝试。",),
    )
    chat = ChatService(
        conversations,
        messages,
        FakeModel(),
        "人格",
        memory_service,
        CountingExtractor([
            candidate("可能改用 Rust。", intent=CorrectionIntent.AMBIGUOUS_CHANGE)
        ]),
        correction_flow=RecordingFlow(result),
    )

    response = await chat.send(conversation["id"], "可能改用 Rust。")
    item = response["correction"]["items"][0]

    assert response["correction"]["status"] == "clarification_needed"
    assert item["clarification"] == ["请确认长期变化还是暂时尝试。"]
    assert "proposal_id" not in item


@pytest.mark.asyncio
async def test_reused_proposal_status_is_not_reported_as_created(chat_context):
    _, conversations, messages, memory_service, conversation = chat_context
    summary = CorrectionProposalSummary(
        proposal_id="proposal-existing",
        target_memory_id="memory-1",
        relation=MemoryRelation.CONFLICT,
        confidence=0.9,
        evidence=("历史 Proposal",),
    )
    reused = CorrectionFlowResult(
        status=CorrectionFlowStatus.PROPOSAL_REUSED,
        reason="reused",
        proposal=summary,
    )
    chat = ChatService(
        conversations,
        messages,
        FakeModel(),
        "人格",
        memory_service,
        CountingExtractor([
            candidate("我之前说错了，我喜欢 Rust。", intent=CorrectionIntent.EXPLICIT_CORRECTION)
        ]),
        correction_flow=RecordingFlow(reused),
    )

    response = await chat.send(conversation["id"], "重复纠正")

    assert response["correction"]["status"] == "proposal_reused"
    assert response["correction"]["items"][0]["proposal_id"] == "proposal-existing"


@pytest.mark.asyncio
async def test_real_correction_flow_creates_proposal_without_modifying_memory(chat_context):
    database, conversations, messages, memory_service, conversation = chat_context
    old = memory_service.create_memory(
        MemoryCreate(
            memory_type=MemoryType.GOAL,
            content="我的目标是雅思 6.5 分。",
            canonical_key="goal:ielts:6.5",
            importance=0.8,
            confidence=0.9,
        )
    )
    proposal_service = MemoryProposalService(MemoryProposalRepository(database))
    flow = CorrectionFlowService(memory_service, proposal_service)
    chat = ChatService(
        conversations,
        messages,
        FakeModel(),
        "人格",
        memory_service,
        DeterministicMemoryExtractor(),
        correction_flow=flow,
    )

    result = await chat.send(conversation["id"], "我之前说错了，我的目标是雅思 7 分。")

    assert result["correction"]["status"] == "proposal_created"
    assert result["correction"]["items"][0]["proposal_id"]
    assert memory_service.get_memory(old.id).status.value == "active"
    assert len(proposal_service.list_proposals()) == 1


@pytest.mark.asyncio
async def test_model_failure_keeps_created_proposal_pending(chat_context):
    database, conversations, messages, memory_service, conversation = chat_context
    memory_service.create_memory(
        MemoryCreate(
            memory_type=MemoryType.GOAL,
            content="我的目标是雅思 6.5 分。",
            canonical_key="goal:ielts:6.5",
        )
    )
    proposal_service = MemoryProposalService(MemoryProposalRepository(database))
    flow = CorrectionFlowService(memory_service, proposal_service)

    class FailingModel:
        async def reply(self, _messages):
            raise ModelRequestError("模型失败")

    chat = ChatService(
        conversations,
        messages,
        FailingModel(),
        "人格",
        memory_service,
        DeterministicMemoryExtractor(),
        correction_flow=flow,
    )

    with pytest.raises(ModelRequestError):
        await chat.send(conversation["id"], "我之前说错了，我的目标是雅思 7 分。")

    proposals = proposal_service.list_proposals()
    assert len(proposals) == 1
    assert proposals[0].status.value == "pending"
    assert proposals[0].application_status.value == "not_applied"
    assert [message["role"] for message in messages.list_for_conversation(conversation["id"])] == ["user"]


@pytest.mark.asyncio
async def test_assistant_save_failure_does_not_fake_chat_success_or_rollback_proposal(chat_context, monkeypatch):
    database, conversations, messages, memory_service, conversation = chat_context
    memory_service.create_memory(
        MemoryCreate(
            memory_type=MemoryType.GOAL,
            content="我的目标是雅思 6.5 分。",
            canonical_key="goal:ielts:6.5",
        )
    )
    proposal_service = MemoryProposalService(MemoryProposalRepository(database))
    flow = CorrectionFlowService(memory_service, proposal_service)
    original_add = messages.add

    def fail_assistant(conversation_id, role, content):
        if role == "assistant":
            raise RuntimeError("assistant save failed")
        return original_add(conversation_id, role, content)

    monkeypatch.setattr(messages, "add", fail_assistant)
    chat = ChatService(
        conversations,
        messages,
        FakeModel(),
        "人格",
        memory_service,
        DeterministicMemoryExtractor(),
        correction_flow=flow,
    )

    with pytest.raises(RuntimeError, match="assistant save failed"):
        await chat.send(conversation["id"], "我之前说错了，我的目标是雅思 7 分。")

    proposals = proposal_service.list_proposals()
    assert len(proposals) == 1
    assert proposals[0].status.value == "pending"
    assert [message["role"] for message in messages.list_for_conversation(conversation["id"])] == ["user"]


@pytest.mark.asyncio
async def test_model_failure_does_not_save_assistant_after_correction_processing(chat_context):
    _, conversations, messages, memory_service, conversation = chat_context

    class FailingModel:
        async def reply(self, _messages):
            raise ModelRequestError("模型失败")

    chat = ChatService(
        conversations,
        messages,
        FailingModel(),
        "人格",
        memory_service,
        CountingExtractor([]),
    )

    with pytest.raises(ModelRequestError):
        await chat.send(conversation["id"], "测试失败")

    assert [message["role"] for message in messages.list_for_conversation(conversation["id"])] == ["user"]
