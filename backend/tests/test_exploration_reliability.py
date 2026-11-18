"""Question deduplication and explicit restart with local test doubles only."""

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.exploration_agent import ExplorationAgent
from app.life_engine import LifeEngine
from app.model_adapter import ModelDailyQuotaExhausted, OpenAICompatibleAdapter
from app.ownership import character_scope
from app.web_research import SearchResult
from .conftest import TEST_PAIRING_SECRET
from .test_exploration_agent import FakeReader, FakeSearch, ScriptedModel, setup
from .test_exploration_quota import adapter_for


def test_obvious_question_rewordings_stop_before_search_but_distinct_questions_do_not(tmp_path):
    db, life, repo, _, prior = setup(tmp_path, topic="海獭如何使用石头敲开贝类？")
    detail = {"candidate_topic": "海獭工具", "source": "verified_exploration_claim", "evidence_ids": ["source-1"]}
    with db.connection() as sql:
        sql.execute("INSERT INTO characters VALUES ('fish_b', 'local', 'owned', datetime('now'))")
    with character_scope("big_fish"):
        repo.activate(prior["id"], "big_fish")
        assert repo.select_topic(prior["id"], "big_fish", prior["requested_topic"], "derived_question", detail)
        repo.recover_active()
        life.record("海獭工具", event_type="goal", status="planned")
        assert repo.topic_was_explored("big_fish", "海獭怎样使用石头敲开贝类？")
        assert repo.topic_was_explored("big_fish", "海獭怎样运用石头敲开贝类？", basis_detail=detail)
        assert not repo.topic_was_explored("big_fish", "海獭怎样运用石头敲开贝类？",
                                          basis_detail={**detail, "evidence_ids": ["different-source"]})
        assert not repo.topic_was_explored("big_fish", "海獭为什么使用石头敲开贝类？", basis_detail=detail)
        assert not repo.topic_was_explored("big_fish", "幼年海獭怎样使用石头敲开贝类？", basis_detail=detail)
        assert not repo.topic_was_explored("big_fish", "海獭怎样使用石头敲开螃蟹？", basis_detail=detail)
        failed, _ = repo.create(character_id="big_fish", requested_topic="海獭为什么在水面觅食？",
                                idempotency_key="insufficient-source")
        repo.terminate(failed["id"], "big_fish", "failed", error_code="insufficient_evidence")
        assert not repo.topic_was_explored("big_fish", "海獭在水面觅食的原因是什么？")
        run, _ = repo.create(character_id="big_fish", requested_topic=None, idempotency_key="duplicate-wording")

    async def select(_messages):
        return json.dumps({"action": "select_topic", "topic": "目标：海獭工具", "question": "海獭怎样使用石头敲开贝类？"})

    model = ScriptedModel()
    model.reply = select
    agent = ExplorationAgent(repo, life, Settings(model_enabled=False, exploration_enabled=True), model=model,
                             search=FakeSearch(error=AssertionError("duplicate must not start a search")), reader=FakeReader())
    result = asyncio.run(agent.run(run["id"], "big_fish"))
    assert result["error_code"] == "repeated_exploration_topic" and result["tool_call_count"] == 0
    with character_scope("fish_b"):
        assert not repo.topic_was_explored("fish_b", "海獭怎样使用石头敲开贝类？", basis_detail=detail)
        with pytest.raises(KeyError):
            repo.create(character_id="fish_b", requested_topic=None, idempotency_key="retry-foreign",
                        retry_run_id=prior["id"])


def test_cited_repeat_is_inconclusive_but_new_claim_from_same_page_completes(tmp_path):
    _, life, repo, agent, prior = setup(tmp_path)
    original = asyncio.run(agent.run(prior["id"], "big_fish"))
    assert original["status"] == "completed"
    with character_scope("big_fish"):
        preferences = LifeEngine(life).preferences("big_fish")
        repeat, _ = repo.create(character_id="big_fish", trigger_mode="autonomous",
                                requested_topic="海獭如何用工具打开贝类？", idempotency_key="duplicate-result")
    repeated = asyncio.run(agent.run(repeat["id"], "big_fish"))
    assert repeated["phase"] == "inconclusive" and repeated["error_code"] == "no_new_research_finding"
    assert repeated["summary"] is None and repeated["claims"] == [] and repeated["life_event_id"] is None
    assert repeated["evidence"][-1]["content_sha256"] == original["evidence"][-1]["content_sha256"]
    assert repo.topic_was_explored("big_fish", "海獭怎样用工具打开贝类？")

    class NewClaimModel(ScriptedModel):
        async def reply(self, messages):
            result = json.loads(await super().reply(messages))
            if result["action"] == "finish":
                result["claims"][0]["text"] = "贝类具有外壳。"
            return json.dumps(result)

    agent.model = NewClaimModel()
    with character_scope("big_fish"):
        different, _ = repo.create(character_id="big_fish", trigger_mode="autonomous",
                                   requested_topic="海獭敲开的贝类是什么结构？", idempotency_key="different-result")
    learned = asyncio.run(agent.run(different["id"], "big_fish"))
    assert learned["status"] == "completed" and learned["life_event_id"]
    with character_scope("big_fish"):
        assert LifeEngine(life).preferences("big_fish") == preferences


@pytest.mark.parametrize("selected", [False, True])
def test_retry_keeps_autonomy_gates_and_checks_current_quota(tmp_path, selected):
    _, life, repo, _, _ = setup(tmp_path)
    with character_scope("big_fish"):
        repo.recover_active()
        LifeEngine(life).set_preference("big_fish", "music", 1.9)
        repo.set_autonomy_preferences("big_fish", enabled=True)
        now = datetime.now(UTC)
        first, _ = repo.create_autonomous_if_due("big_fish", now=now)
        detail = {"candidate_topic": "music", "source": "life_preference_history"}
        question = "音乐节奏如何影响舞蹈表现？"
        if selected:
            repo.activate(first["id"], "big_fish")
            assert repo.select_topic(first["id"], "big_fish", question, "derived_question", detail)
        repo.recover_active()
        retry = dict(character_id="big_fish", idempotency_key="explicit-retry", requested_topic=None,
                     retry_run_id=first["id"])
        with pytest.raises(RuntimeError, match="冷却"):
            repo.create(**retry)
        with pytest.raises(RuntimeError, match="暂停"):
            repo.create(**retry, autonomous_paused=True)
        repo.set_autonomy_preferences("big_fish", paused_until=(now + timedelta(days=1)).isoformat())
        with pytest.raises(RuntimeError, match="暂停"):
            repo.create(**retry)
        repo.set_autonomy_preferences("big_fish", enabled=False, clear_pause=True)
        with pytest.raises(RuntimeError, match="关闭"):
            repo.create(**retry)
        repo.set_autonomy_preferences("big_fish", enabled=True)
        with repo.database.connection() as sql:
            sql.execute("UPDATE exploration_autonomy SET last_started_at=? WHERE character_id=?",
                        ((now - timedelta(days=2)).isoformat(), "big_fish"))
        with pytest.raises(ModelDailyQuotaExhausted):
            repo.create(**retry, quota_snapshot=lambda: {"available": False, "reason": "exploration_daily_quota_exhausted"})
        run, created = repo.create(**retry, quota_snapshot=lambda: {"available": True, "reason": None})
        assert created and run["status"] == "planned" and run["trigger_mode"] == "autonomous"
        assert run["selected_topic"] == (question if selected else None)
        assert run["topic_basis_detail"] == (detail if selected else {})
        if selected:
            assert repo.topic_was_explored("big_fish", question, exclude_run_id=run["id"])
            assert not repo.topic_was_explored("big_fish", question, exclude_run_id=run["id"], retry=True)
        assert repo.autonomy_preferences("big_fish")["last_run_id"] == run["id"]
        assert repo.create_autonomous_if_due("big_fish") == (None, False)


def test_restart_preserves_progress_and_ledger_and_retries_once_through_start_api(tmp_path, monkeypatch):
    import app.main as main

    db, life, repo, _, prior = setup(tmp_path)
    def respond(request):
        payload = json.loads(json.loads(request.content)["messages"][-1]["content"])
        if not payload["observations"]:
            action = {"action": "search_web", "query": "海獭如何使用工具"}
        else:
            source = payload["observations"][-1]["evidence_id"]
            action = {"action": "finish", "summary": "海獭用石头打开贝类。",
                      "claims": [{"text": "海獭用石头打开贝类。", "kind": "sourced", "source_ids": [source]}],
                      "unanswered": []}
        return httpx.Response(200, json={"choices": [
            {"message": {"content": json.dumps(action)}, "finish_reason": "stop"},
        ]})

    adapter, ledger, calls = adapter_for(tmp_path, handler=respond)
    old_id = prior["id"]
    with character_scope("big_fish"):
        repo.activate(old_id, "big_fish")
        repo.set_topic(old_id, "big_fish", prior["requested_topic"])
        repo.step(old_id, "big_fish", model_call=True, input_chars=100)
        messages = [{"role": "user", "content": json.dumps({"observations": []})}]
        asyncio.run(adapter.reply_action(messages, run_id=old_id, step=1))
        repo.record_tool_call(old_id, "big_fish")
        repo.add_evidence(old_id, "big_fish", tool_name="read_webpage", source_url="https://example.org/old",
                          excerpt="海獭使用石头打开贝类外壳。", outcome="success", page_relevance="relevant")
    recorded_ledger = ledger.read_bytes()
    monkeypatch.setattr(main, "database", db)
    monkeypatch.setattr(main, "life_repository", life)
    monkeypatch.setattr(main, "ExplorationAgent", lambda repo, life, settings: ExplorationAgent(
        repo, life, settings.model_copy(update={"exploration_enabled": True}), model=adapter,
        search=FakeSearch([SearchResult("海獭", "https://example.org/new", "海獭使用石头")]), reader=FakeReader(),
    ))
    path = "/api/characters/big_fish/explorations"
    with TestClient(main.app, base_url="http://127.0.0.1:8001", client=("127.0.0.1", 50000)) as client:
        assert client.post("/api/auth/session", json={"secret": TEST_PAIRING_SECRET}).status_code == 200
        old = client.get(f"{path}/{old_id}").json()
        assert old["status"] == "interrupted" and old["life_event_id"] is None
        assert old["selected_topic"] == prior["requested_topic"] and old["model_call_count"] == old["tool_call_count"] == 1
        assert old["evidence"][0]["source_url"] == "https://example.org/old"
        assert old["model_diagnostics"]["reservations"] == old["model_diagnostics"]["http_attempts"] == 1
        assert len(calls) == 1 and ledger.read_bytes() == recorded_ledger

        def retry(index):
            return client.post(path, json={"idempotency_key": f"explicit-retry-{index}", "retry_run_id": old_id})

        with ThreadPoolExecutor(max_workers=2) as workers:
            responses = list(workers.map(retry, range(2)))
        assert [response.status_code for response in responses] == [202, 202]
        attempts = [response.json() for response in responses]
        assert sum(item["created"] for item in attempts) == 1
        new_id = attempts[0]["id"]
        assert attempts[1]["id"] == new_id and new_id != old_id
        for _ in range(100):
            new = client.get(f"{path}/{new_id}").json()
            if new["status"] not in {"planned", "active"}:
                break
            time.sleep(0.02)
        assert new["status"] == "completed" and new["retry_of"] == old_id
        assert new["model_diagnostics"]["http_attempts"] == 2 and len(calls) == 3
        assert all(item["source_url"] != "https://example.org/old" for item in new["evidence"])
        assert client.get(f"{path}/{old_id}").json() == old
        assert client.post(path, json={"idempotency_key": "completed-retry", "retry_run_id": new_id}).status_code == 409
        assert len(client.get(path).json()) == 2


def test_desktop_manual_retry_reserves_and_consumes_exploration_quota(tmp_path, monkeypatch):
    from app.desktop_runtime import DesktopRuntime
    from app.exploration_repository import ExplorationRepository
    from app.life import LifeRepository

    runtime = DesktopRuntime(tmp_path, "a" * 64)
    runtime.database.initialize()
    life = LifeRepository(runtime.database)
    repo = ExplorationRepository(runtime.database, life)
    settings = runtime.settings.model_copy(update={
        "model_enabled": True, "exploration_enabled": True, "model_base_url": "http://127.0.0.1:9/v1",
        "model_name": "local-test-only", "model_api_key": "fake-key",
    })
    requests = []

    def respond(request):
        payload = json.loads(json.loads(request.content)["messages"][-1]["content"])
        requests.append(request)
        if not payload["observations"]:
            action = {"action": "search_web", "query": "海獭如何使用工具"}
        else:
            source = payload["observations"][-1]["evidence_id"]
            action = {"action": "finish", "summary": "海獭用石头敲贝类。", "unanswered": [],
                      "claims": [{"text": "海獭用石头敲贝类。", "kind": "sourced", "source_ids": [source]}]}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(action)}, "finish_reason": "stop"}]})

    client_factory = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client_factory(**{**kw, "transport": httpx.MockTransport(respond)}))
    adapter = OpenAICompatibleAdapter(settings, strict_budget=True, budget_service=runtime.budget)
    agent = ExplorationAgent(repo, life, settings, model=adapter, budget_service=runtime.budget,
                             search=FakeSearch([SearchResult("海獭", "https://example.org/otter", "海獭使用石头")]),
                             reader=FakeReader())
    with character_scope("big_fish"):
        prior, _ = repo.create(character_id="big_fish", requested_topic="海獭如何觅食", idempotency_key="manual-original")
        repo.recover_active()
        retry, _ = repo.create(character_id="big_fish", requested_topic=None, idempotency_key="desktop-retry",
                               retry_run_id=prior["id"], quota_snapshot=agent.quota_snapshot)
        result = asyncio.run(agent.run(retry["id"], "big_fish"))
        assert result["status"] == "completed" and len(requests) == 2, result["error_code"]
        assert runtime.runtime_status()["exploration_used"] == 2
        assert repo.get(prior["id"], "big_fish")["status"] == "interrupted"
