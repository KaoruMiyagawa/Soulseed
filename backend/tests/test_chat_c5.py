"""C5 tests use a disposable DB and HTTPX MockTransport only."""

import asyncio
import json
import sqlite3

import httpx
import pytest

import app.main as main
from app.chat_reply_worker import ChatReplyWorker, ReplyQueue
from app.chat_service import ChatService
from app.config import Settings, get_settings
from app.db import Database
from app.model_adapter import OpenAICompatibleAdapter
from app.model_budget import ModelBudgetService
from app.repositories import ConversationRepository, MessageRepository
from .test_chat_reply_worker import (
    GatedMock, adapter_with_mock_http, clock, ctx, database, make_integrated_chat, pair, payload,
    state, submit, transport, wire_api,
)


OPERATOR = "isolated-operator-test-credential-123456"
HEADERS = {"Authorization": f"Bearer {OPERATOR}"}


def response(status=200, content="已生成"):
    return httpx.Response(status, headers={"x-request-id": "offline-receipt"}, json={
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
    })


@pytest.mark.asyncio
@pytest.mark.parametrize("local_failure", [True, False])
async def test_attempt_distinguishes_local_rejection_from_explicit_http_failure(database, clock, local_failure):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return response(429)

    conversations = ConversationRepository(database)
    item = submit(database, conversations.create()["id"], "failure", 1, "测试调用失败")
    adapter = adapter_with_mock_http(handler)
    if local_failure:
        adapter.settings.model_enabled = False
    worker = ChatReplyWorker(ReplyQueue(database, clock=clock, max_output_tokens=192),
                             ChatService(conversations, MessageRepository(database), adapter, "大肥鱼"))
    assert await worker.run_one()
    assert not await worker.run_one()
    task = state(database, item)
    with database.connection() as sql:
        attempt = sql.execute("SELECT * FROM chat_model_attempts").fetchone()
    assert attempt["attempt_id"] == f"{item['message_id']}:1"
    assert attempt["started_at"] and attempt["ended_at"] and attempt["reply_message_id"] is None
    assert task["role_status"] == "failed"
    if local_failure:
        assert not calls and attempt["outcome"] == "local_failure" and attempt["sent_at"] is None
        assert task["reply_error_code"] == "model_local_failure"
    else:
        assert len(calls) == 1 and calls[0]["max_tokens"] == 192
        assert attempt["outcome"] == "response" and attempt["http_status"] == 429 and attempt["sent_at"]
        assert attempt["provider_request_id"] == "offline-receipt"
        assert task["reply_error_code"] == "model_http_failure"


@pytest.mark.asyncio
@pytest.mark.parametrize("budget_ready", [True, False])
async def test_attempt_id_binds_strict_budget_and_gate_stays_closed(database, clock, seed_budget_gate, budget_ready):
    calls = []

    def handler(request):
        calls.append(request)
        return response()

    service = ModelBudgetService(database, global_limit=1, clock=clock)
    seed_budget_gate(service, clock().date().isoformat(), ready=budget_ready)
    settings = get_settings().model_copy(update={"model_enabled": True, "model_api_key": "mock-only"})
    adapter = OpenAICompatibleAdapter(settings, strict_budget=True, budget_service=service, clock=clock,
                                      budget_transport=httpx.MockTransport(handler))
    conversations = ConversationRepository(database)
    item = submit(database, conversations.create()["id"], "strict", 1, "严格预算")
    worker = ChatReplyWorker(ReplyQueue(database, clock=clock),
                             ChatService(conversations, MessageRepository(database), adapter, "大肥鱼"))
    assert await worker.run_one()
    with database.connection() as sql:
        attempt = sql.execute("SELECT * FROM chat_model_attempts").fetchone()
        budget = sql.execute("SELECT id,status FROM model_budget_requests").fetchone()
    if budget_ready:
        assert tuple(budget) == (attempt["attempt_id"], "completed")
        assert len(calls) == 1 and attempt["reply_message_id"] == state(database, item)["reply"]["id"]
    else:
        assert budget is None and calls == []
        assert attempt["outcome"] == "local_failure" and attempt["sent_at"] is None


@pytest.mark.asyncio
async def test_per_turn_attempt_cap_blocks_structured_fallback(ctx, clock):
    database, _, _, _, _, conversation, _ = ctx
    item = submit(database, conversation["id"], "cap", 1, "我已经永久改用 Linux 作为主要开发环境。")
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return response(content="不是 JSON")

    chat, _, _, _ = make_integrated_chat(ctx, adapter_with_mock_http(handler))
    worker = ChatReplyWorker(ReplyQueue(database, clock=clock, max_attempts=1), chat)
    assert await worker.run_one()
    assert len(calls) == 1 and calls[0]["response_format"] == {"type": "json_object"}
    assert state(database, item)["reply_error_code"] == "reply_attempt_limit"
    with database.connection() as sql:
        assert sql.execute("SELECT COUNT(*) FROM chat_model_attempts").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_disabled_and_real_requested_remain_closed_without_preventing_legacy(database, monkeypatch):
    conversations = wire_api(monkeypatch, database, GatedMock())
    conversation = conversations.create()["id"]
    endpoint = f"/api/conversations/{conversation}"
    requested = Settings(environment="development", chat_async_real_enabled=True, model_budget_strict_enabled=True,
                         chat_async_conversation_id=conversation, _env_file=None)
    monkeypatch.setattr(main.settings, "chat_async_real_enabled", requested.chat_async_real_enabled)
    monkeypatch.setattr(main.app.state, "chat_worker", None, raising=False)
    async with httpx.AsyncClient(transport=transport(), base_url="http://127.0.0.1:8001") as api:
        await pair(api)
        health = (await api.get("/api/health")).json()
        assert health["chat_reply_mode"] == "disabled"
        assert health["chat_reply_blocker"] == "verified_budget_cutover_required"
        denied = await api.post(f"{endpoint}/user-messages", json=payload("closed", 1, "不能创建等待任务"))
        assert denied.status_code == 503 and denied.json()["detail"]["code"] == "async_chat_unavailable"
        feed = (await api.get(f"{endpoint}/messages")).json()
        assert not feed["async_chat_accepting"] and feed["messages"] == []
        result = await api.post(f"{endpoint}/messages", json={"content": "原有同步聊天"})
        assert result.status_code == 200
        assert result.json()["role"] == "assistant"
    with database.connection() as sql:
        assert not sql.execute("SELECT 1 FROM client_message_submissions").fetchone()
        assert not sql.execute("SELECT 1 FROM client_message_streams").fetchone()


@pytest.mark.asyncio
@pytest.mark.parametrize("allow_finish", [True, False])
async def test_scoped_admission_and_drain_fence_with_restart(database, monkeypatch, allow_finish):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def handler(request):
        calls.append(json.loads(request.content))
        entered.set()
        await release.wait()
        return response()

    conversations = wire_api(monkeypatch, database, GatedMock())
    chosen, other = [conversations.create()["id"] for _ in range(2)]
    endpoint = f"/api/conversations/{chosen}"
    monkeypatch.setattr(main, "DelayedMockChatModel", lambda _delay: adapter_with_mock_http(handler))
    monkeypatch.setattr(main.settings, "chat_async_mock_enabled", True)
    monkeypatch.setattr(main.settings, "chat_async_conversation_id", chosen)
    monkeypatch.setattr(main.settings, "chat_async_max_messages", 2)
    monkeypatch.setattr(main.settings, "chat_async_drain_seconds", 2 if allow_finish else 0)
    monkeypatch.setattr(main.settings, "chat_operator_secret", OPERATOR)
    async with main.app.router.lifespan_context(main.app):
        worker = main.app.state.chat_worker
        assert worker.accepting and len(worker.tasks) == 1
        async with httpx.AsyncClient(transport=transport(), base_url="http://127.0.0.1:8001") as api:
            await pair(api)
            first_payload = payload("drain", 1, "已经开始的消息")
            first = await api.post(f"{endpoint}/user-messages", json=first_payload)
            assert first.status_code == 201
            await asyncio.wait_for(entered.wait(), 5)
            second = await api.post(f"{endpoint}/user-messages", json=payload("drain", 2, "待领取的消息"))
            assert second.status_code == 201
            cap = await api.post(f"{endpoint}/user-messages", json=payload("drain", 3, "超过任务上限"))
            assert cap.status_code == 409 and cap.json()["detail"]["code"] == "async_chat_task_limit"
            assert (await api.post(f"/api/conversations/{other}/user-messages",
                                   json=payload("outside", 1, "范围外"))).status_code == 503
            assert (await api.post("/api/admin/chat/drain")).status_code == 401
            stop = asyncio.create_task(api.post("/api/admin/chat/drain", headers=HEADERS))
            await asyncio.wait_for(worker.stopping.wait(), 5)
            if allow_finish:
                release.set()
            assert (await asyncio.wait_for(stop, 5)).status_code == 200
            assert (await api.get("/api/health")).json()["chat_reply_mode"] == "disabled"
            assert (await api.post(f"{endpoint}/user-messages", json=payload("drain", 3, "已经停止接收"))).status_code == 503
            replay = await api.post(f"{endpoint}/user-messages", json=first_payload)
            assert replay.status_code == 201 and replay.json()["duplicate"]
            assert len(calls) == 1
            assert all(task.done() for task in worker.tasks)
            expected = "completed" if allow_finish else "unknown"
            assert state(database, first.json())["role_status"] == expected
            assert state(database, second.json())["role_status"] == "waiting"
            with database.connection() as sql:
                attempt = sql.execute("SELECT outcome,reply_message_id FROM chat_model_attempts").fetchone()
                assert attempt["outcome"] == ("response" if allow_finish else "unknown")
                assert bool(attempt["reply_message_id"]) == allow_finish
    reopened = Database(database.path)
    reopened.initialize()
    next_claim = ReplyQueue(reopened, conversation_id=chosen).claim()
    if allow_finish:
        assert next_claim.message_id == second.json()["message_id"]
    else:
        assert next_claim is None


@pytest.mark.asyncio
async def test_review_requires_owner_auth_and_current_evidence_and_keeps_side_effects(ctx, clock, monkeypatch):
    database, _, _, _, _, conversation, _ = ctx
    wire_api(monkeypatch, database, GatedMock())
    monkeypatch.setattr(main.settings, "chat_operator_secret", OPERATOR)
    calls = []

    def handler(request):
        calls.append(request.url.path)
        raise httpx.ReadTimeout("lost response", request=request)

    text = "我已经永久改用 Linux 作为主要开发环境。"
    first = submit(database, conversation["id"], "review", 1, text)
    later = submit(database, conversation["id"], "review", 2, "后续聊天")
    chat, _, _, _ = make_integrated_chat(ctx, adapter_with_mock_http(handler))
    worker = ChatReplyWorker(ReplyQueue(database, clock=clock), chat)
    assert await worker.run_one()
    assert not await worker.run_one()
    message_id = first["message_id"]
    with database.connection() as sql, pytest.raises(sqlite3.IntegrityError):
        sql.execute("UPDATE client_message_submissions SET role_status='failed' WHERE message_id=?", (message_id,))

    endpoint = f"/api/admin/chat/replies/{message_id}"
    async with httpx.AsyncClient(transport=transport(), base_url="http://127.0.0.1:8001") as api:
        assert (await api.get(endpoint, headers=HEADERS)).status_code == 401
        await pair(api)
        assert (await api.get(endpoint)).status_code == 401
        detail = (await api.get(endpoint, headers=HEADERS)).json()
        assert detail["attempts"][0]["outcome"] == "unknown"
        assert detail["saved_reply"] is None
        assert detail["clarifications"][0]["status"] == "preparing"
        assert detail["proposals"][0]["application_log_id"] is None
        body = {"decision": "close_without_reply", "snapshot_token": detail["snapshot_token"],
                "evidence_reference": "isolated-incident-42"}
        assert (await api.post(f"{endpoint}/resolution", headers=HEADERS, json=body)).status_code == 409
        body["provider_checked"] = True
        wrong = {**body, "decision": "confirm_saved_reply"}
        assert (await api.post(f"{endpoint}/resolution", headers=HEADERS, json=wrong)).status_code == 409
        stale = {**body, "snapshot_token": "0" * 64}
        assert (await api.post(f"{endpoint}/resolution", headers=HEADERS, json=stale)).status_code == 409
        accepted = await api.post(f"{endpoint}/resolution", headers=HEADERS, json=body)
        assert accepted.status_code == 200
        result = accepted.json()
        assert result["task"]["role_status"] == "failed" and result["saved_reply"] is None
        assert result["clarifications"][0]["status"] == "deferred"
        assert result["proposals"] == detail["proposals"] and result["memory_changes"] == detail["memory_changes"]
        assert result["review"]["reviewer_user_id"] == "local" and result["review"]["provider_checked"] == 1
        assert (await api.post(f"{endpoint}/resolution", headers=HEADERS, json=body)).json()["review"] == result["review"]
    assert calls == ["/v1/chat/completions"]
    assert ReplyQueue(database, clock=clock).claim().message_id == later["message_id"]
