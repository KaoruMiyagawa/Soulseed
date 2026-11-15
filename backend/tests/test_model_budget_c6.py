"""C6 Development cutover simulation. All model traffic uses MockTransport."""

from datetime import UTC, datetime
import json
import json

import httpx
import pytest
from fastapi import HTTPException
from types import SimpleNamespace

from app.chat_attempts import ChatAttempt, chat_attempt_factory
from app.chat_reply_worker import ChatReplyWorker, ReplyQueue
from app.config import Settings
from app.db import Database
from app.model_adapter import ModelBudgetContext, OpenAICompatibleAdapter
from app.model_budget import BudgetRejected, ModelBudgetService
from app.model_budget_history import CutoverRejected, LegacyIdentity, ModelBudgetCutover
from app.ownership import character_scope


@pytest.fixture
def development_budget(tmp_path):
    db_path = tmp_path / "development-simulation.db"
    bootstrap = Database(str(db_path), environment="testing", test_data_root=str(tmp_path))
    bootstrap.initialize()
    database = Database(str(db_path), environment="development",
                        allow_protected_database_access=True,
                        protected_database_confirmation=str(db_path))
    database.initialize()
    now = datetime(2030, 1, 2, 12, tzinfo=UTC)
    files = {"general": tmp_path / "general.jsonl", "exploration": tmp_path / "exploration.jsonl"}
    for path in files.values():
        path.touch()
    service = ModelBudgetService(database, global_limit=4,
                                 source_limits={"chat": 3, "exploration": 1}, clock=lambda: now)
    settings = Settings(environment="development", model_budget_strict_enabled=False,
                        chat_async_real_enabled=False, model_enabled=True,
                        model_api_key="mock-only-placeholder", model_name="mock-model",
                        model_request_log_path=str(files["general"]),
                        exploration_model_request_log_path=str(files["exploration"]),
                        _env_file=None)
    return database, files, service, settings, now


@pytest.mark.asyncio
async def test_legacy_drain_import_and_c5_attempt_share_one_development_ledger(development_budget):
    database, files, service, settings, now = development_budget
    legacy_id = "legacy-before-cutover"
    cutover = ModelBudgetCutover(service, files=files,
                                 inventory={legacy_id: LegacyIdentity("big_fish", "chat")})
    calls = []

    def respond(request):
        calls.append(request)
        body = json.loads(request.content)
        content = (json.dumps({"reply": "结构化 mock", "clarification": {
                    "clarification_id": None, "action": "normal", "resolution": "none",
                    "evidence_excerpt": None}}) if body.get("response_format") else "mock answer")
        return httpx.Response(200, json={"choices": [{"message": {"content": content},
                                                       "finish_reason": "stop"}]})

    transport = httpx.MockTransport(respond)
    legacy = OpenAICompatibleAdapter(settings,
        client_factory=lambda **kwargs: httpx.AsyncClient(transport=transport, **kwargs),
        clock=lambda: now, legacy_cutover=cutover)

    def attempt_factory(_phase, max_tokens):
        return ChatAttempt(legacy_id, "big_fish", max_tokens or 512,
                           lambda: None, lambda *_: None, lambda *_: None)

    token = chat_attempt_factory.set(attempt_factory)
    try:
        with character_scope("big_fish"):
            assert await legacy.reply([{"role": "user", "content": "legacy"}]) == "mock answer"
    finally:
        chat_attempt_factory.reset(token)
    assert len(calls) == 1

    cutover.begin_drain()
    with pytest.raises(CutoverRejected, match="legacy_admission_closed"):
        with character_scope("big_fish"):
            await legacy.reply([{"role": "user", "content": "closed"}])
    assert len(calls) == 1
    cutover.seal()
    report = cutover.import_history()
    assert report.gate_eligible and report.inserted_requests == 1

    strict_settings = settings.model_copy(update={
        "chat_async_real_enabled": True, "model_budget_strict_enabled": True,
        "chat_async_conversation_id": "synthetic-conversation",
    })
    strict = OpenAICompatibleAdapter(strict_settings, strict_budget=True,
                                     budget_service=service, budget_transport=transport,
                                     clock=lambda: now)
    c5_ids = []

    def c5_attempt(_phase, max_tokens):
        attempt_id = "saved-user-message:1" if len(c5_ids) == 0 else "saved-user-message:2"
        c5_ids.append(attempt_id)
        return ChatAttempt(attempt_id, "big_fish", max_tokens or 512,
                           lambda: None, lambda *_: None, lambda *_: None)

    token = chat_attempt_factory.set(c5_attempt)
    try:
        with character_scope("big_fish"):
            structured = await strict.reply_structured([{"role": "user", "content": "strict"}])
            assert structured.content == "结构化 mock"
            assert await strict.reply([{"role": "user", "content": "strict fallback"}]) == "mock answer"
            with pytest.raises(BudgetRejected, match="request_already_reserved"):
                await strict.reply([{"role": "user", "content": "duplicate"}])
    finally:
        chat_attempt_factory.reset(token)

    assert len(calls) == 3
    assert c5_ids == ["saved-user-message:1", "saved-user-message:2", "saved-user-message:2"]
    with database.connection() as sql:
        rows = sql.execute("SELECT id,status FROM model_budget_requests ORDER BY created_at,id").fetchall()
    assert {row["id"]: row["status"] for row in rows} == {
        legacy_id: "completed", "saved-user-message:1": "completed",
        "saved-user-message:2": "completed"}
    assert service.usage_snapshot("big_fish")["global_used"] == 3


def test_development_strict_gate_rejects_requests_until_verified_cutover(development_budget):
    database, _files, service, settings, _now = development_budget
    settings = settings.model_copy(update={"chat_async_real_enabled": True,
                                           "model_budget_strict_enabled": True,
                                           "chat_async_conversation_id": "synthetic-conversation"})
    calls = []
    adapter = OpenAICompatibleAdapter(settings, strict_budget=True, budget_service=service,
                                      budget_transport=httpx.MockTransport(
                                          lambda request: calls.append(request) or httpx.Response(200)))
    assert not service.ready(require_development_cutover=True)
    with character_scope("big_fish"), pytest.raises(BudgetRejected, match="history_not_confirmed"):
        import asyncio
        asyncio.run(adapter.reply([{"role": "user", "content": "blocked"}],
                                  budget_context=ModelBudgetContext("attempt", "big_fish", "chat", "2030-01-02")))
    assert not calls


@pytest.mark.asyncio
async def test_worker_admission_rejects_unbudgeted_message_before_persistence(development_budget):
    database, _files, service, settings, _now = development_budget
    settings = settings.model_copy(update={"chat_async_real_enabled": True,
                                           "model_budget_strict_enabled": True,
                                           "chat_async_conversation_id": "test-conversation"})
    adapter = OpenAICompatibleAdapter(settings, strict_budget=True, budget_service=service,
                                      budget_transport=httpx.MockTransport(
                                          lambda _request: pytest.fail("network must not be reached")))
    chat = SimpleNamespace(messages=SimpleNamespace(database=database), model=adapter)
    worker = ChatReplyWorker(ReplyQueue(database, conversation_id="test-conversation"), chat)
    worker.start(concurrency=1)
    try:
        with database.atomic() as sql, pytest.raises(HTTPException) as error:
            worker.admit(sql, "test-conversation")
        assert error.value.status_code == 503
        assert error.value.detail["code"] == "async_chat_budget_unavailable"
        with database.connection() as sql:
            assert sql.execute("SELECT COUNT(*) FROM client_message_submissions").fetchone()[0] == 0
    finally:
        await worker.drain(0)
