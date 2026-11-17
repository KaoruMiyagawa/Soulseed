import asyncio
import json
import re
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

import app.main as main
from app.chat_reply_worker import ChatReplyWorker, ReplyQueue
from app.chat_service import ChatService, ReplyExecutionError
from app.config import get_settings
from app.db import Database
from app.desktop_runtime import DesktopRuntime, ModelInput
from app.life import LifeRepository
from app.local_auth import COOKIE, digest
from app.memory import DeterministicMemoryExtractor, MemoryRepository, MemoryService
from app.memory.context_builder import MemoryContextBuilder
from app.memory.retriever import MemoryRetriever
from app.message_delivery import MessageDeliveryRepository
from app.model_adapter import ClarificationAction, ClarificationResolution, OpenAICompatibleAdapter
from app.ownership import character_scope, current_character
from app.persona import load_persona_profile, resolve_system_prompt
from app.repositories import ConversationRepository, MessageRepository
from .conftest import TEST_PAIRING_SECRET
from .test_chat_clarification import StructuredFake, make_integrated_chat, persisted_clarification, ctx


class Clock:
    now = datetime(2030, 1, 1, tzinfo=UTC)

    def __call__(self):
        return self.now

    def expire(self):
        self.now += timedelta(minutes=3)


class GatedMock:
    def __init__(self, blocked=None):
        self.blocked = blocked
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = []

    async def reply(self, messages):
        await asyncio.sleep(0)
        history = [message["content"] for message in messages if message["role"] in ("user", "assistant")]
        self.calls.append(history)
        if history[-1] == self.blocked:
            self.started.set()
            await self.release.wait()
        return f"回复：{history[-1]}"


@pytest.fixture
def database(tmp_path):
    db = Database(str(tmp_path / "replies.db"))
    db.initialize()
    return db


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr("app.repositories.now_iso", lambda: clock().isoformat())
    monkeypatch.setattr("app.message_delivery.now_iso", lambda: clock().isoformat())
    monkeypatch.setattr("app.memory.clarification_repository.now_iso", lambda: clock().isoformat())
    return clock


def payload(stream, sequence, content):
    return {
        "client_message_id": f"{stream}-message-{sequence}",
        "client_stream_id": f"{stream}-stream", "client_sequence": sequence, "content": content,
    }


def submit(db, conversation, stream, sequence, content):
    return MessageDeliveryRepository(db).submit(
        user_id="local", character_id="big_fish", conversation_id=conversation, **payload(stream, sequence, content),
    )


def state(db, message):
    return MessageDeliveryRepository(db).lookup(
        user_id="local", character_id="big_fish", conversation_id=message["conversation_id"],
        client_message_id=message["client_message_id"],
    )


def transport():
    return httpx.ASGITransport(app=main.app, client=("127.0.0.1", 50000))


def wire_api(monkeypatch, db, model):
    conversations, messages = ConversationRepository(db), MessageRepository(db)
    chat = ChatService(conversations, messages, model, "你是大肥鱼。")
    for name, value in (
        ("database", db), ("conversations", conversations), ("messages", messages), ("chat", chat),
        ("message_delivery", MessageDeliveryRepository(db)), ("life_repository", LifeRepository(db)),
    ):
        monkeypatch.setattr(main, name, value)
    return conversations


async def pair(api):
    response = await api.post("/api/auth/session", json={"secret": TEST_PAIRING_SECRET})
    assert response.status_code == 200


async def wait_for_reply(api, url):
    async with asyncio.timeout(5):
        while True:
            response = await api.get(url)
            assert response.status_code == 200
            record = response.json()
            if record["role_status"] in ("completed", "failed", "unknown"):
                assert record["role_status"] == "completed", record
                return record["reply"]
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_api_lifespan_worker_parallel_conversations_and_incremental_refresh(database, clock, monkeypatch):
    model = GatedMock("第一条")
    conversations = wire_api(monkeypatch, database, GatedMock())
    monkeypatch.setattr(main, "DelayedMockChatModel", lambda _delay: model)
    monkeypatch.setattr(main, "ReplyQueue", lambda db, **limits: ReplyQueue(db, clock=clock, **limits))
    monkeypatch.setattr(main.settings, "chat_async_mock_enabled", True)
    first, other = (f"/api/conversations/{conversations.create()['id']}" for _ in range(2))
    async with main.app.router.lifespan_context(main.app):
        async with httpx.AsyncClient(transport=transport(), base_url="http://127.0.0.1:8001", timeout=5) as api:
            await pair(api)
            one = await api.post(f"{first}/user-messages", json=payload("first", 1, "第一条"))
            assert one.status_code == 201 and one.json()["role_status"] == "waiting"
            await asyncio.wait_for(model.started.wait(), 5)
            two = await asyncio.wait_for(api.post(f"{first}/user-messages", json=payload("first", 2, "第二条")), 5)
            assert two.status_code == 201 and two.json()["role_status"] == "waiting"
            replay = await api.post(f"{first}/user-messages", json=payload("first", 1, "第一条"))
            assert replay.json()["message_id"] == one.json()["message_id"]
            assert replay.json()["role_status"] == "processing"
            assert (await api.post(f"{first}/messages", json={"content": "不应越过前序"})).status_code == 409
            assert (await api.post(f"{other}/user-messages", json=payload("other", 1, "另一个会话"))).status_code == 201
            await wait_for_reply(api, f"{other}/user-messages/other-message-1")
            assert model.calls == [["第一条"], ["另一个会话"]]
            page = (await api.get(f"{first}/messages?limit=2")).json()
            assert [m["role_status"] for m in page["messages"]] == ["processing", "waiting"]
            assert page["next_after_sequence"] == 2
            model.release.set()
            second_reply = await wait_for_reply(api, f"{first}/user-messages/first-message-2")
            assert model.calls[-1] == ["第一条", "回复：第一条", "第二条"]
            assert second_reply["user_message_id"] == two.json()["message_id"]
            incremental = (await api.get(f"{first}/messages?after_sequence=2")).json()
            assert [m["user_message_id"] for m in incremental["messages"]] == [
                one.json()["message_id"], two.json()["message_id"],
            ]
            assert incremental["next_after_sequence"] == 4
            assert not incremental["has_more"]
            with database.connection() as sql:
                assert not sql.execute("SELECT 1 FROM proactive_messages").fetchone()


@pytest.mark.asyncio
async def test_legacy_started_first_finishes_before_async_and_preserves_order(database, clock, monkeypatch):
    model = GatedMock("同步")
    conversations = wire_api(monkeypatch, database, model)
    monkeypatch.setattr(main, "ReplyQueue", lambda db: ReplyQueue(db, clock=clock))
    endpoint = f"/api/conversations/{conversations.create()['id']}"
    worker = ChatReplyWorker(ReplyQueue(database, clock=clock), main.chat)
    monkeypatch.setattr(main.app.state, "chat_worker", SimpleNamespace(
        queue=worker.queue, accepting=True, admit=lambda _db, _conversation: None,
    ), raising=False)
    async with httpx.AsyncClient(transport=transport(), base_url="http://127.0.0.1:8001") as api:
        await pair(api)
        legacy = asyncio.create_task(api.post(f"{endpoint}/messages", json={"content": "同步"}))
        await asyncio.wait_for(model.started.wait(), 5)
        assert (await api.post(f"{endpoint}/user-messages", json=payload("mixed", 1, "异步"))).status_code == 201
        assert not await worker.run_one()
        model.release.set()
        assert (await asyncio.wait_for(legacy, 5)).status_code == 200
        assert await worker.run_one()
        assert model.calls == [["同步"], ["同步", "回复：同步", "异步"]]
        page = (await api.get(f"{endpoint}/messages")).json()["messages"]
        assert [m["server_sequence"] for m in page] == [1, 2, 3, 4]
        assert [m["user_message_id"] for m in page[2:]] == [page[0]["id"], page[1]["id"]]


@pytest.mark.asyncio
async def test_async_save_between_legacy_lease_and_user_insert_wins_order(database, clock, monkeypatch):
    model = GatedMock()
    conversations = wire_api(monkeypatch, database, model)
    conversation = conversations.create()["id"]
    queue = ReplyQueue(database, clock=clock)
    legacy_turn = queue.legacy_turn

    @contextmanager
    def interleave_c1_save(conversation_id):
        with legacy_turn(conversation_id) as execution:
            submit(database, conversation_id, "race", 1, "先落库的异步消息")
            yield execution

    monkeypatch.setattr(queue, "legacy_turn", interleave_c1_save)
    monkeypatch.setattr(main, "ReplyQueue", lambda _db: queue)
    async with httpx.AsyncClient(transport=transport(), base_url="http://127.0.0.1:8001") as api:
        await pair(api)
        result = await api.post(f"/api/conversations/{conversation}/messages", json={"content": "后落库的同步消息"})
        assert result.status_code == 409
        assert result.json()["detail"]["code"] == "conversation_reply_pending"
        assert model.calls == []
        worker = ChatReplyWorker(queue, main.chat)
        assert await worker.run_one()
        assert not await worker.run_one()
        assert model.calls == [["先落库的异步消息"]]
        page = (await api.get(f"/api/conversations/{conversation}/messages")).json()["messages"]
        assert [item["role"] for item in page] == ["user", "assistant"]
        assert page[1]["user_message_id"] == page[0]["id"]


@pytest.mark.asyncio
async def test_unstarted_claim_can_be_reclaimed_after_restart(database, clock):
    conversation = ConversationRepository(database).create()["id"]
    message = submit(database, conversation, "restart", 1, "租约恢复")
    original = ReplyQueue(database, clock=clock)
    competing = ReplyQueue(Database(database.path), clock=clock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(original.claim)
        second = pool.submit(competing.claim)
        claims = [claim for claim in (first.result(), second.result()) if claim]
    assert len(claims) == 1
    claim = claims[0]
    assert claim.message_id == message["message_id"]
    reopened = Database(database.path)
    reopened.initialize()
    queue = ReplyQueue(reopened, clock=clock)
    assert queue.claim() is None
    clock.expire()
    model = GatedMock()
    chat = ChatService(ConversationRepository(reopened), MessageRepository(reopened), model, "大肥鱼")
    worker = ChatReplyWorker(queue, chat)
    assert await worker.run_one()
    assert not await worker.run_one()
    with pytest.raises(ReplyExecutionError):
        original.begin(claim)
    assert len(model.calls) == 1
    assert state(database, message)["role_status"] == "completed"


@pytest.mark.asyncio
async def test_expired_worker_cannot_commit_late_model_reply(database, clock):
    conversation = ConversationRepository(database).create()["id"]
    first = submit(database, conversation, "expiry", 1, "过期")
    second = submit(database, conversation, "expiry", 2, "后续")
    queue = ReplyQueue(database, clock=clock)
    model = GatedMock("过期")
    worker = ChatReplyWorker(queue, ChatService(ConversationRepository(database), MessageRepository(database), model, "大肥鱼"))
    task = asyncio.create_task(worker.run_one())
    await asyncio.wait_for(model.started.wait(), 5)
    clock.expire()
    assert ReplyQueue(Database(database.path), clock=clock).claim() is None
    model.release.set()
    await asyncio.wait_for(task, 5)
    record = state(database, first)
    assert record["role_status"] == "unknown" and record["reply"] is None
    assert record["reply_error_code"] == "execution_lease_expired"
    assert record["model_started_at"]
    assert state(database, second)["role_status"] == "waiting"
    assert len(model.calls) == 1
    assert len(MessageRepository(database).list_for_conversation(conversation)) == 2


class ProcessLost(BaseException):
    pass


@pytest.mark.asyncio
async def test_process_loss_after_clarification_model_start_is_not_replayed(ctx, clock):
    class LostModel(StructuredFake):
        calls = 0

        async def reply_structured(self, messages):
            self.calls += 1
            await asyncio.sleep(0)
            raise ProcessLost()

    db, _, _, _, _, conversation, _ = ctx
    model = LostModel()
    chat, _, _, _ = make_integrated_chat(ctx, model)
    message = submit(db, conversation["id"], "clarify-crash", 1, "我已经永久改用 Linux 作为主要开发环境。")
    with pytest.raises(ProcessLost):
        await ChatReplyWorker(ReplyQueue(db, clock=clock), chat).run_one()
    assert state(db, message)["role_status"] == "processing"
    clock.expire()
    assert ReplyQueue(Database(db.path), clock=clock).claim() is None
    assert state(db, message)["role_status"] == "unknown"
    assert model.calls == 1
    with db.connection() as sql:
        assert sql.execute("SELECT COUNT(*) FROM memory_correction_clarifications").fetchone()[0] == 1
        assert sql.execute("SELECT COUNT(*) FROM memory_change_proposals").fetchone()[0] == 1


class DelayedStructuredMock(StructuredFake):
    async def reply_structured(self, messages):
        await asyncio.sleep(0)
        return await super().reply_structured(messages)

    async def reply(self, messages):
        await asyncio.sleep(0)
        return await super().reply(messages)


@pytest.mark.asyncio
async def test_clarification_and_memory_application_are_bound_to_saved_messages(ctx, clock):
    model = DelayedStructuredMock(
        {"content": "你确定已经换了吗？", "action": ClarificationAction.ASK},
        {"content": "我听懂了。", "resolution": ClarificationResolution.EXPLICIT_CHANGE,
         "evidence_excerpt": "我已经永久改用 Linux 作为主要开发环境"},
    )
    db, _, _, _, _, conversation, _ = ctx
    content = "我已经永久改用 Linux 作为主要开发环境。"
    first = submit(db, conversation["id"], "clarify", 1, content)
    early = submit(db, conversation["id"], "clarify", 2, content)
    chat, application, _, _ = make_integrated_chat(ctx, model)
    worker = ChatReplyWorker(ReplyQueue(db, clock=clock), chat)
    assert await worker.run_one()
    assert await worker.run_one()
    assert state(db, early)["role_status"] == "completed"
    question = persisted_clarification(ctx)
    assert question.status.value == "awaiting_user"
    assert question.response_message_id is None
    assert len(model.structured_messages) == 1
    with db.connection() as sql:
        assert not sql.execute("SELECT 1 FROM memory_proposal_application_log").fetchone()

    second = submit(db, conversation["id"], "clarify", 3, content)
    assert await worker.run_one()
    assert submit(db, conversation["id"], "clarify", 3, content)["duplicate"]
    assert not await worker.run_one()
    application.recover_after_restart()
    record = persisted_clarification(ctx)
    assert record.status.value == "applied"
    assert (record.trigger_message_id, record.response_message_id) == (first["message_id"], second["message_id"])
    for message in (first, second):
        assert state(db, message)["reply"]["user_message_id"] == message["message_id"]
    assert len(model.structured_messages) == 2
    with db.connection() as sql:
        assert sql.execute("SELECT COUNT(*) FROM memory_proposal_application_log").fetchone()[0] == 1
        assert sql.execute("SELECT COUNT(*) FROM memory_change_log WHERE proposal_id IS NOT NULL").fetchone()[0] == 2


def adapter_with_mock_http(handler):
    settings = get_settings().model_copy(update={
        "model_enabled": True, "model_api_key": "offline-contract-test", "model_name": "offline-model",
        "model_base_url": "http://127.0.0.1:9/v1", "model_request_log_path": None,
    })
    return OpenAICompatibleAdapter(
        settings, client_factory=lambda **options: httpx.AsyncClient(transport=httpx.MockTransport(handler), **options),
    )


@pytest.mark.asyncio
async def test_desktop_worker_matches_sync_persona_context_and_output_cap(tmp_path, monkeypatch):
    runtime = DesktopRuntime(tmp_path, "a" * 64)
    runtime.database.initialize()
    runtime.save(ModelInput(base_url="http://127.0.0.1:9/v1", model="offline-model", api_key="offline-key"))
    db = runtime.database
    conversations, messages = ConversationRepository(db), MessageRepository(db)
    memories = MemoryService(MemoryRepository(db))
    extractor = DeterministicMemoryExtractor()
    sync_id = conversations.create()["id"]
    async_id = conversations.create()["id"]
    earlier = "我喜欢观察月球环形山。"
    earlier_reply = "那我们可以一起聊聊月球！"
    for conversation_id in (sync_id, async_id):
        source = messages.add(conversation_id, "user", earlier)
        messages.add(conversation_id, "assistant", earlier_reply)
    preference = memories.persist_candidate(extractor.extract(source)[0], source)
    relation_source = messages.add(
        conversations.create()["id"], "user", "我和朋友每周一起观察月球环形山。",
    )
    relationship = memories.persist_candidate(extractor.extract(relation_source)[0], relation_source)
    assert preference is not None and relationship is not None
    requests = []

    def respond(request):
        assert request.url.path == "/v1/chat/completions"
        assert current_character.get() == "big_fish"
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "隔离测试回复"}, "finish_reason": "stop"}]})

    async_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **options: async_client(**{**options, "transport": httpx.MockTransport(respond)}),
    )
    model = OpenAICompatibleAdapter(runtime.model_settings(), strict_budget=True, budget_service=runtime.budget)
    chat = ChatService(
        conversations, messages, model, resolve_system_prompt(runtime.settings.system_prompt),
        memory_retriever=MemoryRetriever(memories), memory_context_builder=MemoryContextBuilder(),
    )
    text = "你觉得月球环形山像不像被你挖了几口？"
    await chat.send(sync_id, text)
    queued = submit(db, async_id, "desktop-persona", 1, text)
    queue = ReplyQueue(db, max_output_tokens=runtime.settings.chat_async_max_output_tokens)
    with character_scope("unrelated-role"):
        assert await ChatReplyWorker(queue, chat).run_one()
        assert current_character.get() == "unrelated-role"
    assert state(db, queued)["role_status"] == "completed"
    sync, queued = requests
    assert sync == queued
    assert queued["model"] == "offline-model" and queued["max_tokens"] == 1024
    persona, opinion_instruction, expression, *dialogue = queued["messages"]
    assert persona == {"role": "system", "content": load_persona_profile()}
    assert opinion_instruction["role"] == "system"
    assert "<opinion_update>" in opinion_instruction["content"]
    assert expression["role"] == "system"
    context = json.loads(expression["content"].splitlines()[-2])
    assert context["character_id"] == "big_fish"
    assert {(item["source"], item["kind"], item["evidence_id"], tuple(item["source_ids"]))
            for item in context["evidence"]} == {
        ("long_term_memory", "user_preference", preference.id, (source["id"],)),
        ("long_term_memory", "relationship", relationship.id, (relation_source["id"],)),
    }
    assert dialogue == [
        {"role": "user", "content": earlier},
        {"role": "assistant", "content": earlier_reply},
        {"role": "user", "content": text},
    ]
    assert runtime.runtime_status()["chat_used"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["structured", "known_invalid_json", "lost"])
async def test_worker_with_real_adapter_and_offline_http_transport(ctx, clock, outcome):
    db, _, _, _, _, conversation, _ = ctx
    text = "我已经永久改用 Linux 作为主要开发环境。"
    saved = submit(db, conversation["id"], "adapter", 1, text)
    queued = submit(db, conversation["id"], "adapter", 2, "后发消息不能进入前一轮提示词")
    calls = []

    async def handle(request):
        assert request.url.path == "/v1/chat/completions"
        assert not db.in_transaction
        body = json.loads(request.content)
        assert text in [message["content"] for message in body["messages"] if message["role"] == "user"]
        assert all(queued["content"] not in message["content"] for message in body["messages"])
        assert state(db, saved)["model_started_at"] is not None
        structured = body.get("response_format") == {"type": "json_object"}
        calls.append(structured)
        if outcome == "lost":
            raise httpx.ReadTimeout("offline response lost", request=request)
        if structured and outcome == "structured":
            instruction = next(message["content"] for message in body["messages"]
                               if "clarification_id 必须原样为" in message["content"])
            binding = re.search(r"clarification_id 必须原样为 '([^']+)'", instruction).group(1)
            content = json.dumps({"reply": "你确定已经换了吗？", "clarification": {
                "clarification_id": binding, "action": "ask", "resolution": "none", "evidence_excerpt": None,
            }})
        else:
            content = "不是 JSON" if structured else "普通回复"
        return httpx.Response(200, json={"choices": [{"message": {"content": content}, "finish_reason": "stop"}]})

    chat, _, _, _ = make_integrated_chat(ctx, adapter_with_mock_http(handle))
    worker = ChatReplyWorker(ReplyQueue(db, clock=clock), chat)
    assert await worker.run_one()
    current = state(db, saved)
    assert calls == ([True, False] if outcome == "known_invalid_json" else [True])
    with db.connection() as sql:
        attempts = sql.execute(
            "SELECT * FROM chat_model_attempts WHERE user_message_id=? ORDER BY ordinal", (saved["message_id"],),
        ).fetchall()
    assert [item["attempt_id"] for item in attempts] == [
        f"{saved['message_id']}:{ordinal}" for ordinal in range(1, len(calls) + 1)
    ]
    assert [item["phase"] for item in attempts] == ["structured" if call else "plain" for call in calls]
    assert all(item["sent_at"] and item["ended_at"] for item in attempts)
    if outcome == "lost":
        assert current["role_status"] == "unknown" and current["reply"] is None
        assert current["reply_error_code"] == "model_outcome_unknown"
        assert not await worker.run_one()
        assert attempts[0]["outcome"] == "unknown" and attempts[0]["http_status"] is None
    else:
        assert current["role_status"] == "completed"
        assert current["reply"]["user_message_id"] == saved["message_id"]
        assert attempts[-1]["outcome"] == "response"
        assert attempts[-1]["reply_message_id"] == current["reply"]["id"]
    assert state(db, queued)["role_status"] == "waiting"


@pytest.mark.asyncio
async def test_worker_cancellation_during_adapter_request_survives_restart(database, clock):
    started = asyncio.Event()
    calls = []

    async def handle(request):
        calls.append(request.url.path)
        started.set()
        await asyncio.Event().wait()

    conversations = ConversationRepository(database)
    conversation = conversations.create()["id"]
    first = submit(database, conversation, "stop", 1, "正在生成的回复")
    second = submit(database, conversation, "stop", 2, "后续回复")
    chat = ChatService(conversations, MessageRepository(database), adapter_with_mock_http(handle), "大肥鱼")
    worker = ChatReplyWorker(ReplyQueue(database, clock=clock), chat)
    task = asyncio.create_task(worker.run())
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert state(database, first)["role_status"] == "unknown"
    assert state(database, first)["reply_error_code"] == "worker_cancelled"
    assert state(database, second)["role_status"] == "waiting"
    reopened = Database(database.path)
    reopened.initialize()
    assert ReplyQueue(reopened, clock=clock).claim() is None
    assert calls == ["/v1/chat/completions"]


@pytest.mark.asyncio
async def test_reply_and_clarification_rollback_on_failed_completion_commit(ctx, clock):
    db, _, _, _, _, conversation, _ = ctx
    model = DelayedStructuredMock({"content": "你确定已经换了吗？", "action": ClarificationAction.ASK})
    chat, _, _, _ = make_integrated_chat(ctx, model)
    message = submit(db, conversation["id"], "rollback", 1, "我已经永久改用 Linux 作为主要开发环境。")
    with db.connection() as sql:
        sql.execute("""CREATE TRIGGER inject_reply_commit_fault BEFORE UPDATE OF role_status
                       ON client_message_submissions WHEN NEW.role_status='completed'
                       BEGIN SELECT RAISE(ABORT, 'test completion failure'); END""")
    worker = ChatReplyWorker(ReplyQueue(db, clock=clock), chat)
    assert await worker.run_one()
    record = state(db, message)
    assert record["role_status"] == "unknown" and record["reply"] is None
    assert record["reply_error_code"] == "reply_commit_failed"
    assert not await worker.run_one()
    assert len(model.structured_messages) == 1
    with db.connection() as sql:
        assert not sql.execute("SELECT 1 FROM messages WHERE role='assistant'").fetchone()
    assert persisted_clarification(ctx).status.value == "preparing"


@pytest.mark.asyncio
async def test_model_transport_failure_does_not_replay_memory_extraction(database, clock):
    class DisconnectedMock:
        calls = 0

        async def reply(self, messages):
            self.calls += 1
            await asyncio.sleep(0)
            raise ConnectionError("response lost")

    conversations = ConversationRepository(database)
    conversation = conversations.create()["id"]
    text = "我喜欢喝咖啡。"
    message = submit(database, conversation, "memory", 1, text)
    model = DisconnectedMock()
    chat = ChatService(conversations, MessageRepository(database), model, "大肥鱼",
                       MemoryService(MemoryRepository(database)), DeterministicMemoryExtractor())
    worker = ChatReplyWorker(ReplyQueue(database, clock=clock), chat)
    assert await worker.run_one()
    record = state(database, message)
    assert record["role_status"] == "unknown" and record["reply_error_code"] == "model_outcome_unknown"
    assert submit(database, conversation, "memory", 1, text)["duplicate"]
    clock.expire()
    assert not await worker.run_one()
    assert model.calls == 1
    with database.connection() as sql:
        assert sql.execute("SELECT COUNT(*) FROM memory_sources WHERE message_id=?",
                           (message["message_id"],)).fetchone()[0] == 1
        assert sql.execute("SELECT COUNT(*) FROM memory_change_log WHERE source_message_id=?",
                           (message["message_id"],)).fetchone()[0] == 1


@pytest.mark.asyncio
async def test_known_pre_model_error_is_failed_and_releases_conversation(database, clock):
    conversations = ConversationRepository(database)
    conversation = conversations.create()["id"]
    first = submit(database, conversation, "failure", 1, "测" * 20000)
    second = submit(database, conversation, "failure", 2, "还能发送")
    model = GatedMock()
    chat = ChatService(conversations, MessageRepository(database), model, "大肥鱼")
    worker = ChatReplyWorker(ReplyQueue(database, clock=clock), chat)
    assert await worker.run_one()
    failed = state(database, first)
    assert failed["role_status"] == "failed" and failed["model_started_at"] is None
    assert model.calls == []
    assert await worker.run_one()
    assert state(database, second)["role_status"] == "completed"


@pytest.mark.asyncio
async def test_cross_user_and_cross_character_read_isolation(database, clock, monkeypatch):
    conversations = wire_api(monkeypatch, database, GatedMock())
    conversation = conversations.create()["id"]
    message = submit(database, conversation, "private", 1, "只有自己的角色可读")
    assert await ChatReplyWorker(ReplyQueue(database, clock=clock), main.chat).run_one()
    with database.connection() as sql:
        sql.execute("INSERT INTO users(id,created_at) VALUES ('other-user',?)", (clock().isoformat(),))
        for character, owner, token in (
            ("another-owned-role", "local", "same-user-token"), ("foreign-role", "other-user", "other-user-token"),
        ):
            sql.execute("INSERT INTO characters VALUES (?,?,'owned',?)", (character, owner, clock().isoformat()))
            sql.execute("INSERT INTO auth_sessions VALUES (?,?,?,?,?)",
                        (digest(token), owner, character, clock().isoformat(), "2099-01-01T00:00:00+00:00"))
    endpoint = f"/api/conversations/{conversation}"
    for token in ("same-user-token", "other-user-token"):
        async with httpx.AsyncClient(transport=transport(), base_url="http://127.0.0.1:8001",
                                     cookies={COOKIE: token}) as api:
            assert (await api.get(f"{endpoint}/messages")).status_code == 404
            assert (await api.get(f"{endpoint}/user-messages/{message['client_message_id']}")).status_code == 404
