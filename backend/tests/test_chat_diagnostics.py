"""Offline fault injection only: synthetic data, MockTransport, no provider calls."""
import asyncio
import json

import httpx
import pytest

from app.chat_diagnostics import ChatTrace, current_trace
from app.config import Settings
from app.model_adapter import OpenAICompatibleAdapter, ModelTimeoutError, ModelRequestError
from app.chat_service import ChatService
from app.persona import assemble_system_prompt
from app.life import LifeRepository
from app.memory import MemoryContextBuilder, MemoryRepository, MemoryRetriever
from .test_chat_clarification import ctx, make_chat, persisted_clarification, StructuredFake
from .test_ownership import owned, paired
import app.main as main


def adapter(tmp_path, handler, **overrides):
    settings = Settings(model_api_key="FAKE-SECRET-NOT-FOR-LOGS", model_name="fictional",
                        model_base_url="http://127.0.0.1:9/v1",
                        model_request_log_path=str(tmp_path / "model.jsonl"), **overrides)
    return OpenAICompatibleAdapter(settings, client_factory=lambda **kwargs: httpx.AsyncClient(
        transport=httpx.MockTransport(handler), **kwargs))


@pytest.mark.asyncio
@pytest.mark.parametrize("error,kind", [(httpx.ReadTimeout,"read"), (httpx.ConnectTimeout,"connect"),
                                      (httpx.WriteTimeout,"write"), (httpx.PoolTimeout,"pool")])
async def test_timeout_source_is_explicit_and_never_retried(tmp_path, error, kind):
    calls = []
    def handler(request):
        calls.append(1)
        raise error("FAKE PRIVATE ERROR", request=request)
    with pytest.raises(ModelTimeoutError) as caught:
        await adapter(tmp_path, handler).reply([{"role":"user", "content":"FAKE PRIVATE INPUT"}])
    assert caught.value.kind == kind and caught.value.status_code == 504
    assert calls == [1]
    raw = (tmp_path / "model.jsonl").read_text()
    assert all(value not in raw for value in ("FAKE-SECRET", "FAKE PRIVATE", "Bearer"))
    result = json.loads(raw.splitlines()[-1])
    assert result["status"] == "timeout_" + kind
    assert result["elapsed_ms"] >= 0


@pytest.mark.asyncio
async def test_total_deadline_also_bounds_a_slow_response_body(tmp_path):
    class Trickle(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(100):
                await asyncio.sleep(.01)
                yield b" "
    calls = []
    def handler(request):
        calls.append(1)
        return httpx.Response(200, stream=Trickle())
    with pytest.raises(ModelTimeoutError) as caught:
        await adapter(tmp_path, handler, model_timeout_seconds=.04).reply([])
    assert caught.value.kind == "deadline" and calls == [1]
    record = json.loads((tmp_path / "model.jsonl").read_text().splitlines()[-1])
    assert record["response_headers_ms"] < record["elapsed_ms"]
    assert record["status"] == "timeout_deadline"


@pytest.mark.asyncio
async def test_provider_504_is_not_misreported_as_local_timeout(tmp_path):
    with pytest.raises(ModelRequestError) as caught:
        await adapter(tmp_path, lambda r: httpx.Response(504, json={"private":"FAKE PRIVATE"})).reply([])
    assert caught.value.status_code == 502
    assert "HTTP 504" in str(caught.value)
    assert not isinstance(caught.value, ModelTimeoutError)


@pytest.mark.asyncio
@pytest.mark.parametrize("continuation", [False, True])
async def test_clarification_transport_failure_does_not_make_a_second_paid_call(ctx, continuation):
    class Failed(StructuredFake):
        attempts = 0
        async def reply_structured(self, messages):
            self.attempts += 1
            raise ModelTimeoutError()
        async def reply(self, messages):
            pytest.fail("Unexpected paid fallback")
    chat = make_chat(ctx, StructuredFake({"content":"请确认这次变化", "action":"ask"}))
    if continuation:
        from app.model_adapter import ClarificationAction
        chat.model = StructuredFake({"content":"请确认这次变化", "action":ClarificationAction.ASK})
        await chat.send(ctx[5]["id"], "我已经换成 Linux 作为主要开发环境。")
    failed = Failed()
    chat.model = failed
    before = len(ctx[2].list_for_conversation(ctx[5]["id"]))
    with pytest.raises(ModelTimeoutError):
        await chat.send(ctx[5]["id"], "我已经换成 Linux 作为主要开发环境。")
    assert failed.attempts == 1
    history = ctx[2].list_for_conversation(ctx[5]["id"])
    assert len(history) == before + 1 and history[-1]["role"] == "user"
    record = persisted_clarification(ctx)
    assert record.status.value in {"deferred", "failed"}
    assert not record.processing_token


def test_http_timing_has_no_text_and_preserves_final_database_state(owned, monkeypatch, tmp_path):
    db, records, memories = owned
    calls = []
    async def handler(request):
        calls.append(1)
        await asyncio.sleep(.025)
        return httpx.Response(200, json={"choices":[{"message":{"content":"FAKE PRIVATE REPLY"}}],
                                       "usage":{"prompt_tokens":12,"completion_tokens":4,"total_tokens":16}})
    model = adapter(tmp_path, handler)
    log = tmp_path / "timings.jsonl"
    monkeypatch.setattr(main.settings, "chat_timing_log_path", str(log))
    chat = ChatService(main.conversations, main.messages, model, assemble_system_prompt(),
                       memory_retriever=MemoryRetriever(MemoryRepository(db)),
                       memory_context_builder=MemoryContextBuilder(), life_repository=LifeRepository(db))
    monkeypatch.setattr(main, "chat", chat)
    client = paired()
    cid = records["big_fish"][0]["id"]
    response = client.post(f"/api/conversations/{cid}/messages", json={"content":"FAKE PRIVATE INPUT"},
                           headers={"Origin":"http://127.0.0.1:5174"})
    assert response.status_code == 200
    assert calls == [1]
    assert "model_http" in response.headers["server-timing"]
    assert "X-Request-ID" in response.headers["access-control-expose-headers"]
    record = json.loads(log.read_text().splitlines()[-1])
    assert record["request_id"] == response.headers["x-request-id"]
    assert record["durations_ms"]["model_http"] >= 20
    assert record["durations_ms"]["sqlite"] > 0
    assert record["counts"]["plain_model_calls"] == 1
    assert record["counts"]["estimated_input_tokens"] > 0
    model_record = json.loads((tmp_path / "model.jsonl").read_text().splitlines()[-1])
    assert model_record["chat_request_id"] == record["request_id"]
    assert all(v not in log.read_text() for v in ("FAKE PRIVATE", "FAKE-SECRET", "Bearer"))
    history = main.messages.list_for_conversation(cid)
    assert [m["role"] for m in history[-2:]] == ["user", "assistant"]
    print("OFFLINE_HTTP_TIMING", json.dumps(record))


@pytest.mark.asyncio
async def test_concurrent_traces_remain_request_local(tmp_path):
    async def one(index):
        trace = ChatTrace()
        token = current_trace.set(trace)
        try:
            async def handler(request):
                await asyncio.sleep(.01)
                return httpx.Response(200, json={"choices":[{"message":{"content":"fake"}}]})
            directory = tmp_path / str(index)
            await adapter(directory, handler).reply([])
            result = json.loads((directory / "model.jsonl").read_text().splitlines()[-1])
            assert result["chat_request_id"] == trace.id
            assert trace.durations["model_http"] > 0
            return trace.id
        finally:
            current_trace.reset(token)
    ids = await asyncio.gather(one(1), one(2))
    assert len(set(ids)) == 2
