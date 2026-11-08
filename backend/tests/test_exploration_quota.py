"""Local quota regressions. Temporary ledgers, isolated DBs and mock HTTP only."""
import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.model_adapter import OpenAICompatibleAdapter, ModelDailyQuotaExhausted, ModelQuotaUnavailable
from app.ownership import character_scope
from .test_exploration_agent import setup


NOW = datetime(2030, 1, 1, 23, 59, 59, tzinfo=UTC)


def adapter_for(tmp_path, *, count=0, handler=None):
    ledger = tmp_path / "quota.jsonl"
    ledger.write_text("".join(json.dumps({"event": "started", "day": NOW.date().isoformat(),
                                        "id": f"prior-{i}"}) + "\n" for i in range(count)), encoding="utf-8")
    calls = []
    def respond(request):
        calls.append(request)
        if handler:
            return handler(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]})
    settings = Settings(model_enabled=True, model_api_key="FAKE_KEY", model_name="fictional-model",
                        model_base_url="http://127.0.0.1:9/v1", model_daily_request_limit=8,
                        model_request_log_path=str(ledger))
    adapter = OpenAICompatibleAdapter(settings, clock=lambda: NOW,
                                     client_factory=lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(respond), **kw))
    return adapter, ledger, calls


def test_exhausted_request_never_reaches_http_and_utc_day_resets(tmp_path):
    adapter, ledger, calls = adapter_for(tmp_path, count=8)
    original = ledger.read_bytes()
    quota = adapter.quota_snapshot()
    assert quota == {"limit": 8, "used": 8, "remaining": 0, "day": "2030-01-01",
                     "resets_at": "2030-01-02T00:00:00+00:00", "available": False,
                     "reason": "exploration_daily_quota_exhausted"}
    with pytest.raises(ModelDailyQuotaExhausted):
        asyncio.run(adapter.reply_action([], run_id="blocked", step=1))
    assert calls == [] and ledger.read_bytes() == original
    adapter.clock = lambda: NOW + timedelta(seconds=1)
    assert adapter.quota_snapshot()["remaining"] == 8
    asyncio.run(adapter.reply_action([], run_id="new-day", step=1))
    assert len(calls) == 1 and adapter.quota_snapshot()["remaining"] == 7
    assert "FAKE_KEY" not in ledger.read_text(encoding="utf-8")


@pytest.mark.parametrize("problem", ["unconfigured", "missing", "corrupt", "unreadable", "orphan", "bad_day"])
def test_bad_ledger_is_unknown_and_blocks_http(tmp_path, monkeypatch, problem):
    adapter, ledger, calls = adapter_for(tmp_path)
    if problem == "unconfigured":
        adapter.settings.model_request_log_path = None
    elif problem == "missing":
        ledger.unlink()
    elif problem == "corrupt":
        ledger.write_text('{"event":"started",', encoding="utf-8")
    elif problem == "orphan":
        ledger.write_text(json.dumps({"event": "finished", "id": "lost-reservation", "status": "http_200"}), encoding="utf-8")
    elif problem == "bad_day":
        ledger.write_text(json.dumps({"event": "started", "id": "bad-day", "day": "bad"}), encoding="utf-8")
    else:
        original_open = Path.open
        def deny(path, *args, **kwargs):
            if path == ledger:
                raise PermissionError("PRIVATE_PATH")
            return original_open(path, *args, **kwargs)
        monkeypatch.setattr(Path, "open", deny)
    quota = adapter.quota_snapshot()
    assert quota["reason"] == "exploration_quota_unavailable"
    assert quota["used"] is None and quota["remaining"] is None and not quota["available"]
    assert str(ledger) not in json.dumps(quota)
    with pytest.raises(ModelQuotaUnavailable):
        asyncio.run(adapter.reply_action([], run_id="blocked", step=1))
    assert calls == []


@pytest.mark.parametrize("failure,code", [(429, "model_provider_rate_limited"), (503, "model_provider_error"),
                                           ("network", "model_network_error"), ("timeout", "model_timeout")])
def test_provider_and_transport_errors_are_not_local_quota(tmp_path, failure, code):
    def handler(request):
        if failure == "network":
            raise httpx.ConnectError("private", request=request)
        if failure == "timeout":
            raise httpx.ReadTimeout("private", request=request)
        return httpx.Response(failure, json={"error": "private provider body"})
    adapter, ledger, calls = adapter_for(tmp_path, handler=handler)
    _, _, _, agent, run = setup(tmp_path, model=adapter)
    result = asyncio.run(agent.run(run["id"], "big_fish"))
    assert result["error_code"] == code and len(calls) == 1
    assert result["model_call_count"] == 1 and result["tool_call_count"] == 0
    assert adapter.quota_snapshot()["remaining"] == 7
    assert adapter.request_diagnostics(run["id"]) == {
        "reservations": 1, "http_attempts": 1, "http_attempts_complete": True,
        "provider_receipt": "unknown", "provider_billing": "unknown",
    }
    assert "private" not in ledger.read_text(encoding="utf-8")


def test_race_after_precheck_has_precise_error_without_http(tmp_path):
    adapter, ledger, calls = adapter_for(tmp_path, count=7)
    _, _, repo, agent, initial = setup(tmp_path, model=adapter)
    with character_scope("big_fish"):
        repo.terminate(initial["id"], "big_fish", "cancelled")
        run, created = repo.create(character_id="big_fish", idempotency_key="quota-race",
                                   requested_topic="海獭如何觅食", quota_snapshot=adapter.quota_snapshot)
    assert created
    # Another allowed reservation consumes the final slot before Agent executes.
    adapter._reserve_request({"exploration_run_id": "other", "exploration_step": 1})
    result = asyncio.run(agent.run(run["id"], "big_fish"))
    assert result["error_code"] == "exploration_daily_quota_exhausted"
    assert result["model_call_count"] == 1 and calls == []
    assert result["timings"]["model"]["count"] == 1
    assert adapter.request_diagnostics(run["id"])["reservations"] == 0


def test_parallel_reservations_cannot_overdraw_last_slot(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    adapter, _, calls = adapter_for(tmp_path, count=7)
    def reserve(i):
        try:
            adapter._reserve_request({"exploration_run_id": str(i), "exploration_step": 1})
            return "reserved"
        except ModelDailyQuotaExhausted:
            return "exhausted"
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(reserve, [1, 2])) == ["exhausted", "reserved"]
    assert adapter.quota_snapshot()["used"] == 8 and calls == []


def test_configuration_error_is_distinct_and_does_not_reserve(tmp_path):
    adapter, _, calls = adapter_for(tmp_path)
    adapter.settings.model_enabled = False
    _, _, _, agent, run = setup(tmp_path, model=adapter)
    result = asyncio.run(agent.run(run["id"], "big_fish"))
    assert result["error_code"] == "model_configuration_error"
    assert result["model_call_count"] == 1
    assert adapter.quota_snapshot()["used"] == 0 and calls == []


def test_scheduler_quota_skip_preserves_cooldown_and_can_retry_next_day(tmp_path, monkeypatch):
    import app.main as main
    from app.life_engine import LifeEngine
    adapter, ledger, calls = adapter_for(tmp_path, count=8)
    db, life, repo, agent, initial = setup(tmp_path, model=adapter)
    with character_scope("big_fish"):
        repo.terminate(initial["id"], "big_fish", "cancelled")
        repo.set_autonomy_preferences("big_fish", enabled=True)
        LifeEngine(life).set_preference("big_fish", "music", 1.8, reason="explicit_preference")
    monkeypatch.setattr(main, "database", db)
    monkeypatch.setattr(main, "exploration_repository", repo)
    monkeypatch.setattr(main, "exploration_agent", agent)
    launches = []
    monkeypatch.setattr(main, "_launch_exploration", lambda *args: launches.append(args))
    asyncio.run(main._autonomous_exploration_tick())
    with character_scope("big_fish"):
        prefs = repo.autonomy_preferences("big_fish")
        assert prefs["last_skip_reason"] == "exploration_daily_quota_exhausted"
        assert prefs["last_started_at"] is None and prefs["last_run_id"] is None
        assert len(repo.list("big_fish")) == 1
    assert launches == [] and calls == []
    ledger.unlink()
    asyncio.run(main._autonomous_exploration_tick())
    with character_scope("big_fish"):
        assert repo.autonomy_preferences("big_fish")["last_skip_reason"] == "exploration_quota_unavailable"
    ledger.write_text("", encoding="utf-8")
    adapter.clock = lambda: NOW + timedelta(seconds=1)
    asyncio.run(main._autonomous_exploration_tick())
    assert len(launches) == 1 and calls == []


def test_manual_api_rejects_without_task_and_preserves_idempotency_and_auth(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import app.main as main
    from app.db import Database
    from app.life import LifeRepository
    from app.exploration_agent import ExplorationAgent
    from .conftest import TEST_PAIRING_SECRET
    adapter, ledger, calls = adapter_for(tmp_path, count=8)
    db = Database(str(tmp_path / "api.db")); db.initialize()
    monkeypatch.setattr(main, "database", db)
    monkeypatch.setattr(main, "life_repository", LifeRepository(db))
    monkeypatch.setattr(main, "ExplorationAgent", lambda repo, life, settings: ExplorationAgent(repo, life, settings, model=adapter))
    with TestClient(main.app, base_url="http://127.0.0.1:8001", client=("127.0.0.1", 50000)) as client:
        path = "/api/characters/big_fish"
        assert client.get(path + "/exploration-quota").status_code == 401
        client.post("/api/auth/session", json={"secret": TEST_PAIRING_SECRET})
        assert client.get("/api/characters/other/exploration-quota").status_code == 403
        response = client.post(path + "/explorations", json={"topic": "海獭觅食", "idempotency_key": "manual-exhausted"})
        assert response.status_code == 429
        assert response.json()["detail"]["code"] == "exploration_daily_quota_exhausted"
        assert client.get(path + "/explorations").json() == []
        assert client.get(path + "/exploration-quota").json()["remaining"] == 0
        with character_scope("big_fish"):
            old, _ = main.exploration_repository.create(character_id="big_fish", requested_topic="old", idempotency_key="existing-key")
            main.exploration_repository.terminate(old["id"], "big_fish", "cancelled")
        duplicate = client.post(path + "/explorations", json={"topic": "海獭觅食", "idempotency_key": "existing-key"})
        assert duplicate.status_code == 202 and duplicate.json()["created"] is False
        ledger.unlink()
        response = client.post(path + "/explorations", json={"topic": "海獭觅食", "idempotency_key": "missing-ledger"})
        assert response.status_code == 503 and response.json()["detail"]["code"] == "exploration_quota_unavailable"
        assert len(client.get(path + "/explorations").json()) == 1
        assert str(ledger) not in response.text
    assert calls == []
