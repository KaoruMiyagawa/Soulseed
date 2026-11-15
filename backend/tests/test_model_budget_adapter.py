"""Isolated adapter protocol tests. Every transport is MockTransport."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import httpx
import pytest

from app.config import get_settings
from app.db import Database
from app.life import LifeRepository
from app.model_adapter import (
    ModelBudgetContext, ModelConfigurationError, ModelRequestError,
    ModelTimeoutError, OpenAICompatibleAdapter, StructuredOutputError,
)
from app.model_budget import BudgetRejected, ModelBudgetService
from app.ownership import character_scope


MESSAGES = [{"role": "user", "content": "synthetic question"}]


@pytest.fixture
def env(tmp_path, seed_budget_gate):
    db = Database(str(tmp_path / "adapter-budget.db"))
    db.initialize()
    life = LifeRepository(db)
    life.ensure_state("big_fish")
    life.ensure_state("other")
    now = [datetime(2030, 1, 2, 12, tzinfo=UTC)]
    service = ModelBudgetService(db, global_limit=2, source_limits={"contact": 1}, clock=lambda: now[0])
    seed_budget_gate(service, "2030-01-02")
    settings = get_settings().model_copy(update={
        "model_enabled": True, "model_api_key": "mock-only-placeholder",
        "model_name": "mock-model", "model_trust_env": False,
        "model_daily_request_limit": 1,
        "model_request_log_path": str(tmp_path / "legacy-quota.jsonl"),
    })
    audit_path = tmp_path / "strict-audit.jsonl"
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "mock answer"}, "finish_reason": "stop"}]})

    def adapter(handler=respond, **kwargs):
        return OpenAICompatibleAdapter(settings, strict_budget=True, budget_service=service,
                                       budget_transport=httpx.MockTransport(handler), budget_audit_path=audit_path,
                                       clock=lambda: now[0], **kwargs)

    return SimpleNamespace(db=db, service=service, settings=settings, now=now, calls=calls,
                           respond=respond, adapter=adapter, audit_path=audit_path)


def context(request_id="one", character="big_fish", source="chat"):
    return ModelBudgetContext(request_id, character, source, "2030-01-02")


def status(env, request_id="one"):
    with env.db.connection() as sql:
        row = sql.execute("SELECT status FROM model_budget_requests WHERE id=?", (request_id,)).fetchone()
    return row[0] if row else None


@pytest.mark.asyncio
async def test_legacy_keeps_jsonl_quota_and_no_sqlite_writes(env):
    adapter = OpenAICompatibleAdapter(env.settings, clock=lambda: env.now[0], client_factory=lambda **kw:
                                     httpx.AsyncClient(transport=httpx.MockTransport(env.respond), **kw))
    assert await adapter.reply(MESSAGES) == "mock answer"
    with pytest.raises(ModelConfigurationError):
        await adapter.reply(MESSAGES)
    assert len(env.calls) == 1
    assert status(env) is None
    assert not env.audit_path.exists()
    rows = [json.loads(line) for line in Path(env.settings.model_request_log_path).read_text(encoding="utf-8").splitlines()]
    assert [row["event"] for row in rows] == ["started", "finished"]


@pytest.mark.asyncio
async def test_strict_success_once_and_request_id_in_audit(env):
    def respond(request):
        assert status(env) == "started"
        return env.respond(request)
    adapter = env.adapter(respond)
    assert await adapter.reply(MESSAGES, budget_context=context()) == "mock answer"
    assert status(env) == "completed"
    with pytest.raises(BudgetRejected):
        await adapter.reply(MESSAGES, budget_context=context())
    assert len(env.calls) == 1
    assert not Path(env.settings.model_request_log_path).exists()
    rows = [json.loads(line) for line in env.audit_path.read_text(encoding="utf-8").splitlines()]
    assert {row["id"] for row in rows} == {"one"}
    with pytest.raises(ModelConfigurationError, match="sqlite_snapshot"):
        adapter.quota_snapshot()
    assert env.service.usage_snapshot("big_fish")["global_used"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["global", "source", "history"])
async def test_refusal_never_reaches_http(env, reason, seed_budget_gate):
    if reason == "history":
        seed_budget_gate(env.service, "2030-01-02", ready=False)
    else:
        env.service.reserve("used", "big_fish", "contact")
        if reason == "global":
            env.service.reserve("used2", "big_fish", "chat")
    with pytest.raises(BudgetRejected):
        await env.adapter().reply(MESSAGES, budget_context=context(source="contact"))
    assert not env.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, context(""), replace(context(), utc_day=""), replace(context(), utc_day="bad")])
async def test_trusted_context_required(env, value):
    with pytest.raises(ModelConfigurationError):
        await env.adapter().reply(MESSAGES, budget_context=value)
    assert not env.calls
    assert status(env) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("character,source", [("other", "chat"), ("big_fish", "contact")])
async def test_identity_conflict(env, character, source):
    await env.adapter().reply(MESSAGES, budget_context=context())
    with character_scope(character), pytest.raises(BudgetRejected, match="request_identity_conflict"):
        await env.adapter().reply(MESSAGES, budget_context=context(character=character, source=source))
    assert len(env.calls) == 1


@pytest.mark.asyncio
async def test_serialization_failure_releases_only_unstarted(env):
    with pytest.raises(TypeError):
        await env.adapter().reply([dict(MESSAGES[0], invalid=object())], budget_context=context())
    assert status(env) == "released"
    assert not env.calls
    with pytest.raises(BudgetRejected):
        await env.adapter().reply(MESSAGES, budget_context=context())
    assert env.service.usage_snapshot("big_fish")["global_used"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "network", "cancel"])
async def test_unknown_failures_are_charged_and_never_retried(env, failure):
    def fail(request):
        env.calls.append(request)
        if failure == "cancel":
            raise asyncio.CancelledError
        if failure == "timeout":
            raise httpx.ReadTimeout("mock timeout")
        raise httpx.ConnectError("mock connection failure")
    adapter = env.adapter(fail)
    error = {"timeout": ModelTimeoutError, "network": ModelRequestError, "cancel": asyncio.CancelledError}[failure]
    with pytest.raises(error):
        await adapter.reply(MESSAGES, budget_context=context())
    assert status(env) == "unknown"
    with pytest.raises(BudgetRejected):
        await adapter.reply(MESSAGES, budget_context=context())
    assert len(env.calls) == 1
    assert env.service.usage_snapshot("big_fish")["global_used"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [400, 429, 503, 307])
async def test_http_failure_and_redirect_have_one_attempt(env, code):
    def fail(request):
        env.calls.append(request)
        return httpx.Response(code, headers={"Location": "http://127.0.0.1/redirect"})
    with pytest.raises(ModelRequestError):
        await env.adapter(fail).reply(MESSAGES, budget_context=context())
    assert status(env) == "failed"
    assert len(env.calls) == 1


@pytest.mark.asyncio
async def test_audit_failure_does_not_retry_or_release(env, monkeypatch):
    adapter = env.adapter()
    def fail(record):
        raise OSError("mock audit disk failure")
    monkeypatch.setattr(adapter, "_append_log", fail)
    assert await adapter.reply(MESSAGES, budget_context=context()) == "mock answer"
    with pytest.raises(BudgetRejected):
        await adapter.reply(MESSAGES, budget_context=context())
    assert status(env) == "completed"
    assert len(env.calls) == 1


def test_concurrent_independent_connections_and_characters_share_global_cap(env):
    def run(index):
        character = "other" if index % 2 else "big_fish"
        service = ModelBudgetService(Database(env.db.path), global_limit=2,
                                     source_limits={"contact": 1}, clock=lambda: env.now[0])
        adapter = OpenAICompatibleAdapter(env.settings, strict_budget=True, budget_service=service,
                                         budget_transport=httpx.MockTransport(env.respond))
        with character_scope(character):
            try:
                asyncio.run(adapter.reply(MESSAGES, budget_context=context(str(index), character)))
                return True
            except BudgetRejected:
                return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(run, range(8))) == 2
    assert len(env.calls) == 2


@pytest.mark.asyncio
async def test_concurrent_same_id_and_duplicate_started_are_fenced(env):
    adapter = env.adapter()
    results = await asyncio.gather(*(adapter.reply(MESSAGES, budget_context=context()) for _ in range(6)),
                                   return_exceptions=True)
    assert results.count("mock answer") == 1
    assert sum(isinstance(result, BudgetRejected) for result in results) == 5
    env.service.reserve("started", "big_fish", "chat")
    env.service.mark_started("started", "big_fish")
    with pytest.raises(BudgetRejected):
        await adapter.reply(MESSAGES, budget_context=context("started"))
    assert len(env.calls) == 1


@pytest.mark.asyncio
async def test_mark_started_must_return_changed_true(env, monkeypatch):
    original = env.service.mark_started
    def duplicate(*args):
        original(*args)
        return original(*args)
    monkeypatch.setattr(env.service, "mark_started", duplicate)
    with pytest.raises(BudgetRejected, match="request_already_started"):
        await env.adapter().reply(MESSAGES, budget_context=context())
    assert not env.calls
    assert status(env) == "started"


@pytest.mark.asyncio
async def test_gate_revoked_before_start_releases_reservation(env, monkeypatch, seed_budget_gate):
    original = env.service.mark_started
    def revoke_then_start(*args):
        seed_budget_gate(env.service, "2030-01-02", ready=False)
        return original(*args)
    monkeypatch.setattr(env.service, "mark_started", revoke_then_start)
    with pytest.raises(BudgetRejected, match="history_not_confirmed"):
        await env.adapter().reply(MESSAGES, budget_context=context())
    assert status(env) == "released"
    assert not env.calls


@pytest.mark.asyncio
async def test_budget_database_failure_has_no_legacy_fallback(env, monkeypatch):
    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("mock database unavailable")
    monkeypatch.setattr(env.service, "reserve", unavailable)
    with pytest.raises(sqlite3.OperationalError):
        await env.adapter().reply(MESSAGES, budget_context=context())
    assert not env.calls
    assert not Path(env.settings.model_request_log_path).exists()
    assert not env.audit_path.exists()


@pytest.mark.asyncio
async def test_restart_after_settlement_failure_retains_budget(env, monkeypatch):
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("mock database unavailable")
    with monkeypatch.context() as patch:
        patch.setattr(env.service, "complete", fail)
        patch.setattr(env.service, "mark_unknown", fail)
        with pytest.raises(sqlite3.OperationalError):
            await env.adapter().reply(MESSAGES, budget_context=context())
    assert status(env) == "started"
    env.now[0] += timedelta(minutes=6)
    service = ModelBudgetService(Database(env.db.path), global_limit=2, source_limits={"contact": 1}, clock=lambda: env.now[0])
    assert service.recover("big_fish") == 1
    assert status(env) == "unknown"
    with pytest.raises(BudgetRejected):
        await env.adapter().reply(MESSAGES, budget_context=context())
    assert len(env.calls) == 1


@pytest.mark.asyncio
async def test_no_outer_write_transaction_and_no_network_lock(env):
    with env.db.atomic(), pytest.raises(ModelConfigurationError, match="inside_transaction"):
        await env.adapter().reply(MESSAGES, budget_context=context())
    def respond(request):
        # Independent SQLite connection can acquire a write lock during HTTP.
        with Database(env.db.path).atomic() as sql:
            sql.execute("UPDATE model_budget_windows SET history_ready=1")
        return env.respond(request)
    await env.adapter(respond).reply(MESSAGES, budget_context=context())


@pytest.mark.asyncio
async def test_utc_window_mismatch_and_scope_mismatch(env):
    for ctx in (replace(context(), utc_day="2030-01-01"), context(character="other")):
        with pytest.raises(BudgetRejected):
            await env.adapter().reply(MESSAGES, budget_context=ctx)
    assert not env.calls


@pytest.mark.asyncio
async def test_structured_and_action_entry_points_use_same_budget(env):
    payload = json.dumps({"reply": "mock", "clarification": {"clarification_id": None, "action": "normal",
                         "resolution": "none", "evidence_excerpt": None}})
    def respond(request):
        env.calls.append(request)
        assert json.loads(request.content)["response_format"] == {"type": "json_object"}
        return httpx.Response(200, json={"choices": [{"message": {"content": payload}}]})
    adapter = env.adapter(respond)
    assert (await adapter.reply_structured(MESSAGES, budget_context=context())).content == "mock"
    result = await adapter.reply_action(MESSAGES, run_id="mock-run", step=1,
                                        budget_context=context("run-step-1", source="exploration"))
    assert result.request_id == "run-step-1"
    with pytest.raises(BudgetRejected):
        await adapter.reply_action(MESSAGES, run_id="mock-run", step=2,
                                   budget_context=context("run-step-2", source="exploration"))
    assert len(env.calls) == 2


@pytest.mark.asyncio
async def test_two_exploration_decisions_have_distinct_ledger_entries(env):
    adapter = env.adapter()
    for step in (1, 2):
        request_id = f"exploration:mock-run:{step}"
        result = await adapter.reply_action(MESSAGES, run_id="mock-run", step=step,
                                            budget_context=context(request_id, source="exploration"))
        assert result.request_id == request_id
        assert status(env, request_id) == "completed"
    assert len(env.calls) == 2
    assert env.service.usage_snapshot("big_fish")["source_used"]["exploration"] == 2


@pytest.mark.asyncio
async def test_schema_failure_is_still_a_consumed_http_completion(env):
    with pytest.raises(StructuredOutputError):
        await env.adapter().reply_structured(MESSAGES, budget_context=context())
    assert status(env) == "completed"
    assert len(env.calls) == 1


@pytest.mark.asyncio
async def test_d2_uncoordinated_lifecycle_is_rejected(env):
    for ctx in (context(source="autonomous"), replace(context(), autonomous_reservation_id="mock-reservation")):
        with pytest.raises(ModelConfigurationError, match="d2_joint_lifecycle"):
            await env.adapter().reply(MESSAGES, budget_context=ctx)
    assert not env.calls
    assert status(env) is None


def test_strict_mode_requires_testing_and_mock_transport(env):
    for settings, transport in ((env.settings, None), (env.settings.model_copy(update={"environment": "development"}),
                                                        httpx.MockTransport(env.respond))):
        with pytest.raises(ModelConfigurationError):
            OpenAICompatibleAdapter(settings, strict_budget=True, budget_service=env.service,
                                    budget_transport=transport)


@pytest.mark.asyncio
async def test_strict_never_uses_custom_client_factory(env):
    def forbidden(**kwargs):
        raise AssertionError("custom transport could retry")
    await env.adapter(client_factory=forbidden).reply(MESSAGES, budget_context=context())
    assert len(env.calls) == 1
