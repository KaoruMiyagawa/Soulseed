from __future__ import annotations

import re
import asyncio
import sqlite3

import pytest

from app.chat_service import ChatService
from app.db import Database
from app.memory import (
    ClarificationApplicationService,
    ClarificationRepository,
    ClarificationStateError,
    ClarificationStatus,
    CorrectionFlowService,
    CorrectionFlowResult,
    CorrectionFlowStatus,
    DeterministicMemoryExtractor,
    MemoryConflictDetector,
    MemoryCreate,
    MemoryCorrectionService,
    MemoryProposalRepository,
    MemoryProposalService,
    MemoryRepository,
    MemoryService,
    MemoryType,
    MemoryUpdate,
)
from app.repositories import ConversationRepository, MessageRepository
from app.model_adapter import ClarificationAction, ClarificationResolution, StructuredChatReply


class ContextFlow:
    def __init__(self, context):
        self.context = context

    def process(self, _message, candidates):
        return tuple(
            CorrectionFlowResult(
                status=CorrectionFlowStatus.CLARIFICATION_NEEDED,
                reason="needs context",
                clarification_context=self.context,
            )
            for _ in candidates
        )


class StructuredFake:
    def __init__(self, *decisions, ordinary="自然回复"):
        self.decisions = list(decisions)
        self.ordinary = ordinary
        self.structured_messages = []

    async def reply(self, _messages):
        return self.ordinary

    async def reply_structured(self, messages):
        self.structured_messages.append(messages)
        decision = self.decisions.pop(0)
        prompt = next(message["content"] for message in reversed(messages) if "clarification_id 必须原样为" in message["content"])
        clarification_id = re.search(r"clarification_id 必须原样为 '([^']+)'", prompt).group(1)
        return StructuredChatReply(
            content=decision["content"],
            clarification_id=clarification_id,
            action=decision.get("action", ClarificationAction.NORMAL),
            resolution=decision.get("resolution", ClarificationResolution.NONE),
            evidence_excerpt=decision.get("evidence_excerpt"),
        )


class PlainFake:
    async def reply(self, _messages):
        return "先聊眼前这件事。"


class FailingModel:
    async def reply(self, _messages):
        raise RuntimeError("model unavailable")

    async def reply_structured(self, _messages):
        raise RuntimeError("structured model unavailable")


class GatedModel:
    def __init__(self):
        self.structured_calls = 0
        self.response_started = asyncio.Event()
        self.release_response = asyncio.Event()

    async def reply(self, _messages):
        return "我先听你接着说。"

    async def reply_structured(self, messages):
        self.structured_calls += 1
        prompt = next(message["content"] for message in reversed(messages) if "clarification_id 必须原样为" in message["content"])
        clarification_id = re.search(r"clarification_id 必须原样为 '([^']+)'", prompt).group(1)
        if self.structured_calls == 1:
            return StructuredChatReply("你是已经换环境了吗？", clarification_id, ClarificationAction.ASK, ClarificationResolution.NONE, None)
        self.response_started.set()
        await self.release_response.wait()
        return StructuredChatReply(
            "知道啦。", clarification_id, ClarificationAction.NORMAL,
            ClarificationResolution.EXPLICIT_CHANGE, "我已经永久改用 Linux 作为主要开发环境",
        )


class FailsOnClarificationResponse:
    def __init__(self):
        self.calls = 0

    async def reply(self, _messages):
        raise RuntimeError("fallback failed")

    async def reply_structured(self, messages):
        self.calls += 1
        prompt = next(message["content"] for message in reversed(messages) if "clarification_id 必须原样为" in message["content"])
        clarification_id = re.search(r"clarification_id 必须原样为 '([^']+)'", prompt).group(1)
        if self.calls == 1:
            return StructuredChatReply("你愿意讲讲这次改变吗？", clarification_id, ClarificationAction.ASK, ClarificationResolution.NONE, None)
        raise RuntimeError("response model failed")


class FailsAfterValidatedAnswer(StructuredFake):
    async def reply(self, _messages):
        raise RuntimeError("final natural reply failed")


class SimulatedProcessStop(BaseException):
    pass


@pytest.fixture
def ctx(tmp_path):
    database = Database(str(tmp_path / "chat-clarification.db"))
    database.initialize()
    conversations = ConversationRepository(database)
    messages = MessageRepository(database)
    memory_service = MemoryService(MemoryRepository(database))
    repository = ClarificationRepository(database)
    conversation = conversations.create()
    old_memory = memory_service.create_memory(
        MemoryCreate(MemoryType.USER_PREFERENCE, "我主要使用 Windows 作为开发环境。", "environment:primary")
    )
    return database, conversations, messages, memory_service, repository, conversation, old_memory


def make_context(old_memory, current="我已经换成 Linux 作为主要开发环境。"):
    return {
        "candidate": {
            "memory_type": MemoryType.USER_PREFERENCE.value,
            "content": current,
            "canonical_key": "environment:primary",
        },
        "matched_memories": [{"id": old_memory.id, "content": old_memory.content, "updated_at": old_memory.updated_at}],
        "relation": "potential_conflict",
        "evidence": ["描述同一主要开发环境属性"],
        "reason": "用户表达可能代表长期环境变化",
    }


def make_chat(ctx, model):
    _, conversations, messages, memories, repository, _, old_memory = ctx
    content = "我已经换成 Linux 作为主要开发环境。"
    return ChatService(
        conversations,
        messages,
        model,
        "人格提示词",
        memories,
        DeterministicMemoryExtractor(),
        correction_flow=ContextFlow(make_context(old_memory, content)),
        clarification_repository=repository,
    )


def make_integrated_chat(ctx, model):
    proposals = MemoryProposalService(MemoryProposalRepository(ctx[0]))
    corrections = MemoryCorrectionService(ctx[0])
    flow = CorrectionFlowService(ctx[3], proposals, MemoryConflictDetector())
    application = ClarificationApplicationService(ctx[4], proposals, corrections)
    chat = ChatService(
        ctx[1], ctx[2], model, "人格提示词", ctx[3], DeterministicMemoryExtractor(),
        correction_flow=flow,
        clarification_repository=ctx[4],
        proposal_service=proposals,
        clarification_application=application,
        memory_correction_service=corrections,
    )
    return chat, application, proposals, corrections


def persisted_clarification(ctx):
    with ctx[0].connection() as db:
        row = db.execute(
            "SELECT id FROM memory_correction_clarifications WHERE conversation_id = ? ORDER BY created_at DESC LIMIT 1",
            (ctx[5]["id"],),
        ).fetchone()
    assert row is not None
    return ctx[4].get(row["id"])


@pytest.mark.asyncio
async def test_detected_conflict_can_be_naturally_asked_and_waiting_is_persisted(ctx):
    model = StructuredFake({"content": "咦，你这是已经把开发环境整个搬去 Linux 啦？", "action": ClarificationAction.ASK})
    flow = CorrectionFlowService(
        ctx[3], MemoryProposalService(MemoryProposalRepository(ctx[0])), MemoryConflictDetector()
    )
    chat = ChatService(
        ctx[1], ctx[2], model, "人格提示词", ctx[3], DeterministicMemoryExtractor(),
        correction_flow=flow, clarification_repository=ctx[4],
    )
    result = await chat.send(ctx[5]["id"], "我已经永久改用 Linux 作为主要开发环境。")
    record = persisted_clarification(ctx)
    assert result["content"].startswith("咦，")
    assert record.status is ClarificationStatus.AWAITING_USER
    assert next(item for item in ctx[2].list_for_conversation(ctx[5]["id"]) if item["id"] == record.assistant_message_id)["content"] == result["content"]
    assert len(ctx[2].list_for_conversation(ctx[5]["id"])) == 2
    assert "Proposal" not in result["content"] and "confidence" not in result["content"]
    assert "历史记忆只是曾记录的内容，可能已经过时" in model.structured_messages[0][-2]["content"]
    assert ctx[3].list_memories(limit=10)[0].content == "我主要使用 Windows 作为开发环境。"
    with ctx[0].connection() as db:
        proposal = db.execute("SELECT status, application_status FROM memory_change_proposals").fetchone()
        assert tuple(proposal) == ("pending", "not_applied")


@pytest.mark.asyncio
async def test_explicit_long_term_answer_creates_response_bound_proposal_and_applies(ctx):
    model = StructuredFake(
        {"content": "你这次是认真的呀，我先听清楚。", "action": ClarificationAction.ASK},
        {
            "content": "我听懂你的意思了。",
            "resolution": ClarificationResolution.EXPLICIT_CHANGE,
            "evidence_excerpt": "我已经永久改用 Linux 作为主要开发环境",
        },
        ordinary="嘿，这回真是整个搬家到 Linux 啦，我记住啦。",
    )
    proposals = MemoryProposalService(MemoryProposalRepository(ctx[0]))
    corrections = MemoryCorrectionService(ctx[0])
    flow = CorrectionFlowService(ctx[3], proposals, MemoryConflictDetector())
    chat = ChatService(
        ctx[1], ctx[2], model, "人格提示词", ctx[3], DeterministicMemoryExtractor(),
        correction_flow=flow,
        clarification_repository=ctx[4],
        proposal_service=proposals,
        clarification_application=ClarificationApplicationService(ctx[4], proposals, corrections),
        memory_correction_service=corrections,
    )
    await chat.send(ctx[5]["id"], "我已经永久改用 Linux 作为主要开发环境。")
    answer = await chat.send(ctx[5]["id"], "我已经永久改用 Linux 作为主要开发环境。")
    record = persisted_clarification(ctx)
    assert record.status is ClarificationStatus.APPLIED
    assert record.resolution == "explicit_change"
    assert answer["content"] == "嘿，这回真是整个搬家到 Linux 啦，我记住啦。"
    active = [item for item in ctx[3].list_memories(limit=10) if item.status.value == "active"]
    assert len(active) == 1 and "Linux" in active[0].content
    with ctx[0].connection() as db:
        rows = db.execute(
            "SELECT id, status, application_status, evidence_message_id FROM memory_change_proposals ORDER BY created_at"
        ).fetchall()
        assert len(rows) == 2
        assert tuple(rows[0])[1:3] == ("cancelled", "not_applied")
        assert tuple(rows[1])[1:3] == ("accepted", "applied")
        assert rows[1]["evidence_message_id"] == record.response_message_id
        assert db.execute("SELECT COUNT(*) FROM memory_proposal_application_log").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM memory_change_log WHERE proposal_id = ?", (rows[1]["id"],)).fetchone()[0] == 2
    await chat.send(ctx[5]["id"], "我已经永久改用 Linux 作为主要开发环境。")
    assert ctx[4].get(record.id).status is ClarificationStatus.APPLIED
    with ctx[0].connection() as db:
        assert db.execute("SELECT COUNT(*) FROM memory_proposal_application_log").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM memory_change_proposals WHERE application_status = 'applied'").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_original_proposal_already_rejected_is_never_overridden(ctx):
    model = StructuredFake(
        {"content": "你确定已经换了吗？", "action": ClarificationAction.ASK},
        {"content": "我听懂了。", "resolution": ClarificationResolution.EXPLICIT_CHANGE,
         "evidence_excerpt": "我已经永久改用 Linux 作为主要开发环境"},
        ordinary="我听见了，先不替你改动原来的记录。",
    )
    chat, _, proposals, _ = make_integrated_chat(ctx, model)
    await chat.send(ctx[5]["id"], "我已经永久改用 Linux 作为主要开发环境。")
    with ctx[0].connection() as db:
        source_id = db.execute("SELECT id FROM memory_change_proposals").fetchone()["id"]
    proposals.reject_proposal(source_id, reason="用户已拒绝")
    await chat.send(ctx[5]["id"], "我已经永久改用 Linux 作为主要开发环境。")
    record = persisted_clarification(ctx)
    assert record.status is ClarificationStatus.FAILED
    assert record.error_code == "application_conflict"
    assert ctx[3].list_memories(limit=10)[0].content == "我主要使用 Windows 作为开发环境。"
    with ctx[0].connection() as db:
        rows = db.execute("SELECT status FROM memory_change_proposals ORDER BY created_at").fetchall()
        assert [row["status"] for row in rows] == ["rejected", "cancelled"]
        assert db.execute("SELECT COUNT(*) FROM memory_proposal_application_log").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_changed_target_snapshot_blocks_reply_application(ctx):
    model = StructuredFake(
        {"content": "你已经确定换成 Linux 了吗？", "action": ClarificationAction.ASK},
        {"content": "好，我听清楚了。", "resolution": ClarificationResolution.EXPLICIT_CHANGE,
         "evidence_excerpt": "我已经永久改用 Linux 作为主要开发环境"},
    )
    chat, _, _, _ = make_integrated_chat(ctx, model)
    await chat.send(ctx[5]["id"], "我已经永久改用 Linux 作为主要开发环境。")
    ctx[3].update_memory(
        ctx[6].id,
        MemoryUpdate(content="我主要使用 Windows 作为开发环境，最近又更新过。"),
    )
    with pytest.raises(ClarificationStateError, match="目标已删除或版本发生变化"):
        await chat.send(ctx[5]["id"], "我已经永久改用 Linux 作为主要开发环境。")
    record = persisted_clarification(ctx)
    assert record.status is ClarificationStatus.FAILED
    assert ctx[3].get_memory(ctx[6].id).content == "我主要使用 Windows 作为开发环境，最近又更新过。"
    with ctx[0].connection() as db:
        assert db.execute("SELECT COUNT(*) FROM memory_proposal_application_log").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM memory_change_proposals WHERE application_status = 'applied'").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_accept_then_transient_apply_failure_recovers_after_restart(ctx, monkeypatch):
    model = StructuredFake(
        {"content": "你这是彻底换环境啦？", "action": ClarificationAction.ASK},
        {"content": "好，我明白了。", "resolution": ClarificationResolution.EXPLICIT_CHANGE,
         "evidence_excerpt": "我已经永久改用 Linux 作为主要开发环境"},
    )
    chat, application, _, corrections = make_integrated_chat(ctx, model)
    original_apply = corrections.apply_proposal
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("temporary sqlite interruption")
        return original_apply(*args, **kwargs)

    monkeypatch.setattr(corrections, "apply_proposal", fail_once)
    await chat.send(ctx[5]["id"], "我已经永久改用 Linux 作为主要开发环境。")
    with pytest.raises(sqlite3.OperationalError):
        await chat.send(ctx[5]["id"], "我已经永久改用 Linux 作为主要开发环境。")
    record = persisted_clarification(ctx)
    assert record.status is ClarificationStatus.FAILED
    assert record.error_code == "application_interrupted"
    assert record.application_proposal_id is not None
    with ctx[0].connection() as db:
        assert db.execute("SELECT status FROM memory_change_proposals WHERE id = ?", (record.application_proposal_id,)).fetchone()[0] == "accepted"
        assert db.execute("SELECT COUNT(*) FROM memory_proposal_application_log").fetchone()[0] == 0

    assert application.recover_after_restart() == (record.id,)
    assert persisted_clarification(ctx).status is ClarificationStatus.APPLIED
    with ctx[0].connection() as db:
        assert db.execute("SELECT COUNT(*) FROM memory_proposal_application_log").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM memory_change_proposals WHERE application_status = 'applied'").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_restart_recovery_waits_for_lease_expiry_then_resumes_linked_application(ctx, monkeypatch):
    model = StructuredFake(
        {"content": "你已经决定换环境了吗？", "action": ClarificationAction.ASK},
        {"content": "这回我听清楚了。", "resolution": ClarificationResolution.EXPLICIT_CHANGE,
         "evidence_excerpt": "我已经永久改用 Linux 作为主要开发环境"},
    )
    chat, application, _, _ = make_integrated_chat(ctx, model)
    await chat.send(ctx[5]["id"], "我已经永久改用 Linux 作为主要开发环境。")
    original_apply_linked = application.apply_linked

    def stop_after_link(*_args, **_kwargs):
        raise SimulatedProcessStop()

    monkeypatch.setattr(application, "apply_linked", stop_after_link)
    with pytest.raises(SimulatedProcessStop):
        await chat.send(ctx[5]["id"], "我已经永久改用 Linux 作为主要开发环境。")
    record = persisted_clarification(ctx)
    assert record.status is ClarificationStatus.PROCESSING
    assert record.application_proposal_id is not None
    original_token = record.processing_token

    assert application.recover_after_restart() == ()
    assert ctx[4].get(record.id).processing_token == original_token
    with ctx[0].connection() as db:
        assert db.execute("SELECT status FROM memory_change_proposals WHERE id = ?", (record.application_proposal_id,)).fetchone()[0] == "pending"

    with ctx[0].connection() as db:
        db.execute(
            "UPDATE memory_correction_clarifications SET lease_expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", record.id),
        )
    monkeypatch.setattr(application, "apply_linked", original_apply_linked)
    assert application.recover_after_restart() == (record.id,)
    assert ctx[4].get(record.id).status is ClarificationStatus.APPLIED
    with ctx[0].connection() as db:
        assert db.execute("SELECT COUNT(*) FROM memory_proposal_application_log").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_final_model_failure_keeps_applied_truth_and_does_not_apply_twice(ctx):
    model = FailsAfterValidatedAnswer(
        {"content": "你已经决定换了吗？", "action": ClarificationAction.ASK},
        {"content": "明白啦。", "resolution": ClarificationResolution.EXPLICIT_CHANGE,
         "evidence_excerpt": "我已经永久改用 Linux 作为主要开发环境"},
    )
    chat, application, _, _ = make_integrated_chat(ctx, model)
    await chat.send(ctx[5]["id"], "我已经永久改用 Linux 作为主要开发环境。")
    with pytest.raises(RuntimeError, match="final natural reply failed"):
        await chat.send(ctx[5]["id"], "我已经永久改用 Linux 作为主要开发环境。")
    assert persisted_clarification(ctx).status is ClarificationStatus.APPLIED
    assert len([m for m in ctx[2].list_for_conversation(ctx[5]["id"]) if m["role"] == "assistant"]) == 1
    assert application.recover_after_restart() == ()
    with ctx[0].connection() as db:
        assert db.execute("SELECT COUNT(*) FROM memory_proposal_application_log").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM memory_change_proposals WHERE application_status = 'applied'").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_user_says_original_memory_was_wrong_and_reply_evidence_is_applied(ctx):
    model = StructuredFake(
        {"content": "咦，是我记岔了吗？", "action": ClarificationAction.ASK},
        {"content": "明白，我之前理解错了。", "resolution": ClarificationResolution.ORIGINAL_WAS_WRONG,
         "evidence_excerpt": "我之前说错了，我主要用 Linux"},
        ordinary="啊，原来如此，我把这个记清楚啦。",
    )
    chat, _, _, _ = make_integrated_chat(ctx, model)
    await chat.send(ctx[5]["id"], "我之前说错了，我主要用 Linux 作为开发环境。")
    result = await chat.send(ctx[5]["id"], "我之前说错了，我主要用 Linux 作为开发环境。")
    record = persisted_clarification(ctx)
    assert record.status is ClarificationStatus.APPLIED
    assert record.resolution == "original_was_wrong"
    assert result["content"] == "啊，原来如此，我把这个记清楚啦。"
    with ctx[0].connection() as db:
        applied = db.execute(
            "SELECT evidence_message_id FROM memory_change_proposals WHERE application_status = 'applied'"
        ).fetchone()
        assert applied["evidence_message_id"] == record.response_message_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reply", "resolution", "excerpt", "expected", "cancel_source"),
    [
        ("我这周只是试试 Linux。", ClarificationResolution.TEMPORARY, "这周只是试试 Linux", ClarificationStatus.TEMPORARY, True),
        ("我没换，还是 Windows。", ClarificationResolution.DENIED, "我没换", ClarificationStatus.DENIED, True),
        ("两个环境我都会用，工作和家里不一样。", ClarificationResolution.COEXISTING,
         "两个环境我都会用", ClarificationStatus.DEFERRED, True),
        ("嗯", ClarificationResolution.EXPLICIT_CHANGE, "嗯", ClarificationStatus.DEFERRED, False),
        ("我这周只是试试 Linux。", ClarificationResolution.EXPLICIT_CHANGE,
         "这周只是试试 Linux", ClarificationStatus.DEFERRED, False),
        ("我朋友永久改用 Linux 了。", ClarificationResolution.EXPLICIT_CHANGE,
         "永久改用 Linux", ClarificationStatus.DEFERRED, False),
        ("我还是在考虑 Linux。", ClarificationResolution.DENIED,
         "还是在考虑 Linux", ClarificationStatus.DEFERRED, False),
        ("我都不知道怎么选。", ClarificationResolution.COEXISTING,
         "我都不知道怎么选", ClarificationStatus.DEFERRED, False),
        ("今天电影真好看。", ClarificationResolution.OFF_TOPIC, "电影真好看", ClarificationStatus.DEFERRED, False),
    ],
)
async def test_integrated_non_application_outcomes_never_change_memory(
    ctx, reply, resolution, excerpt, expected, cancel_source
):
    model = StructuredFake(
        {"content": "你愿意多说一点吗？", "action": ClarificationAction.ASK},
        {"content": "我听见啦。", "resolution": resolution, "evidence_excerpt": excerpt},
        ordinary="好，我先听你说。",
    )
    chat, _, _, _ = make_integrated_chat(ctx, model)
    await chat.send(ctx[5]["id"], "我已经永久改用 Linux 作为主要开发环境。")
    await chat.send(ctx[5]["id"], reply)
    record = persisted_clarification(ctx)
    assert record.status is expected
    assert ctx[3].list_memories(limit=10)[0].content == "我主要使用 Windows 作为开发环境。"
    with ctx[0].connection() as db:
        assert db.execute("SELECT COUNT(*) FROM memory_proposal_application_log").fetchone()[0] == 0
        expected_source_status = "cancelled" if cancel_source else "pending"
        assert db.execute("SELECT status FROM memory_change_proposals").fetchone()[0] == expected_source_status


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_reply", "resolution", "excerpt", "expected"),
    [
        ("其实我没改，还是 Windows。", ClarificationResolution.DENIED, "我没改", ClarificationStatus.DENIED),
        ("我这周只是试试 Linux。", ClarificationResolution.TEMPORARY, "这周只是试试 Linux", ClarificationStatus.TEMPORARY),
        ("嗯", ClarificationResolution.EXPLICIT_CHANGE, "嗯", ClarificationStatus.DEFERRED),
        ("我朋友说我已经永久改用 Linux 了。", ClarificationResolution.EXPLICIT_CHANGE, "我已经永久改用 Linux 了", ClarificationStatus.DEFERRED),
        ("你看今天下雨了。", ClarificationResolution.OFF_TOPIC, "今天下雨了", ClarificationStatus.DEFERRED),
    ],
)
async def test_response_categories_require_real_user_evidence(ctx, user_reply, resolution, excerpt, expected):
    model = StructuredFake(
        {"content": "想听听你怎么打算的。", "action": ClarificationAction.ASK},
        {"content": "我听见啦。", "resolution": resolution, "evidence_excerpt": excerpt},
    )
    chat = make_chat(ctx, model)
    await chat.send(ctx[5]["id"], "我已经换成 Linux 作为主要开发环境。")
    await chat.send(ctx[5]["id"], user_reply)
    record = persisted_clarification(ctx)
    assert record.status is expected
    assert record.response_message_id is not None


@pytest.mark.asyncio
async def test_off_topic_is_not_left_waiting_even_if_model_proposes_another_question(ctx):
    model = StructuredFake(
        {"content": "想聊聊这件事吗？", "action": ClarificationAction.ASK},
        {
            "content": "先说说你今天看的电影吧。",
            "action": ClarificationAction.ASK,
            "resolution": ClarificationResolution.OFF_TOPIC,
            "evidence_excerpt": "今天看的电影",
        },
    )
    chat = make_chat(ctx, model)
    await chat.send(ctx[5]["id"], "我已经换成 Linux 作为主要开发环境。")
    await chat.send(ctx[5]["id"], "顺便说下我今天看的电影。")
    assert persisted_clarification(ctx).status is ClarificationStatus.DEFERRED


@pytest.mark.asyncio
async def test_unsupported_structured_model_saves_normal_reply_as_deferred(ctx):
    chat = ChatService(
        ctx[1], ctx[2], PlainFake(), "人格", ctx[3], DeterministicMemoryExtractor(),
        correction_flow=ContextFlow(make_context(ctx[6])), clarification_repository=ctx[4],
    )
    result = await chat.send(ctx[5]["id"], "我已经换成 Linux 作为主要开发环境。")
    record = persisted_clarification(ctx)
    assert record.status is ClarificationStatus.DEFERRED
    assert result["content"] == "先聊眼前这件事。"
    assert record.assistant_message_id == result["id"]


@pytest.mark.asyncio
async def test_ordinary_chat_does_not_create_clarification(ctx):
    chat = ChatService(ctx[1], ctx[2], PlainFake(), "人格", clarification_repository=ctx[4])
    result = await chat.send(ctx[5]["id"], "今天我想聊聊海边的云。")
    assert result["content"] == "先聊眼前这件事。"
    assert ctx[4].list_recoverable(ctx[5]["id"]) == []


@pytest.mark.asyncio
async def test_service_restart_recovers_waiting_clarification(ctx):
    first = make_chat(ctx, StructuredFake({"content": "你要不要讲讲这次怎么决定的？", "action": ClarificationAction.ASK}))
    await first.send(ctx[5]["id"], "我已经换成 Linux 作为主要开发环境。")
    restarted = make_chat(
        ctx,
        StructuredFake({
            "content": "原来如此，我听明白啦。",
            "resolution": ClarificationResolution.EXPLICIT_CHANGE,
            "evidence_excerpt": "我已经换成 Linux 了",
        }),
    )
    await restarted.send(ctx[5]["id"], "我已经换成 Linux 了。")
    # This lightweight service has no safe Proposal application dependencies, so it defers.
    assert persisted_clarification(ctx).status is ClarificationStatus.DEFERRED


@pytest.mark.asyncio
async def test_model_failure_records_recoverable_deferred_state_without_fake_question(ctx):
    chat = make_chat(ctx, FailingModel())
    with pytest.raises(RuntimeError, match="model unavailable"):
        await chat.send(ctx[5]["id"], "我已经换成 Linux 作为主要开发环境。")
    record = persisted_clarification(ctx)
    assert record.status is ClarificationStatus.DEFERRED
    assert record.error_code == "initial_preparation_failed"
    assert record.assistant_message_id is None
    assert len(ctx[2].list_for_conversation(ctx[5]["id"])) == 1
    assert ctx[4].list_recoverable(ctx[5]["id"])[0].id == record.id


@pytest.mark.asyncio
async def test_response_model_failure_keeps_clarification_recoverable(ctx):
    chat = make_chat(ctx, FailsOnClarificationResponse())
    await chat.send(ctx[5]["id"], "我已经换成 Linux 作为主要开发环境。")
    with pytest.raises(RuntimeError, match="fallback failed"):
        await chat.send(ctx[5]["id"], "我已经换成 Linux 了。")
    record = persisted_clarification(ctx)
    assert record.status is ClarificationStatus.FAILED
    assert record.response_message_id is not None
    assert len([m for m in ctx[2].list_for_conversation(ctx[5]["id"]) if m["role"] == "assistant"]) == 1


@pytest.mark.asyncio
async def test_concurrent_replies_claim_one_clarification_processor(ctx):
    model = GatedModel()
    chat, _, _, _ = make_integrated_chat(ctx, model)
    await chat.send(ctx[5]["id"], "我已经永久改用 Linux 作为主要开发环境。")
    first = asyncio.create_task(chat.send(ctx[5]["id"], "我已经永久改用 Linux 作为主要开发环境。"))
    await asyncio.wait_for(model.response_started.wait(), timeout=2)
    second = await chat.send(ctx[5]["id"], "顺便说下我今天看了场电影。")
    model.release_response.set()
    first_result = await asyncio.wait_for(first, timeout=2)
    assert second["content"] == "我先听你接着说。"
    assert first_result["content"] == "我先听你接着说。"
    assert model.structured_calls == 2  # one initial decision and one claimed user response
    assert persisted_clarification(ctx).status is ClarificationStatus.APPLIED
    assert len([m for m in ctx[2].list_for_conversation(ctx[5]["id"]) if m["role"] == "assistant"]) == 3
    with ctx[0].connection() as db:
        assert db.execute("SELECT COUNT(*) FROM memory_proposal_application_log").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM memory_change_proposals WHERE application_status = 'applied'").fetchone()[0] == 1
