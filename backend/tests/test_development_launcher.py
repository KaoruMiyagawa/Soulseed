from __future__ import annotations

import importlib.util
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.model_adapter import ModelConfigurationError, OpenAICompatibleAdapter


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "start_development.py"
SPEC = importlib.util.spec_from_file_location("start_development", SCRIPT)
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)


def test_launcher_discards_inherited_database_and_uses_only_local_model_file(tmp_path, monkeypatch):
    root = tmp_path / "fictional-project"
    root.mkdir()
    (root / ".env").write_text(
        "SOULSEED_MODEL_API_KEY=fake-local-key\n"
        "SOULSEED_MODEL_BASE_URL=https://api.siliconflow.cn/v1\n"
        "SOULSEED_MODEL_NAME=fictional-model\n"
        "SOULSEED_DATABASE_PATH=C:/fictional-formal.db\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(launcher, "ROOT", root)
    monkeypatch.setattr(launcher, "DATABASE", root / "backend" / "data" / "soulseed-development.db")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("SOULSEED_DATABASE_PATH", "C:/fictional-formal.db")
    monkeypatch.setenv("SOULSEED_LOCAL_PAIRING_SECRET", "ignored-parent-secret-" * 3)
    env = launcher._child_environment(model_enabled=False)
    assert env["SOULSEED_DATABASE_PATH"] == str(launcher.DATABASE.resolve())
    assert env["SOULSEED_MODEL_API_KEY"] == "fake-local-key"
    assert env["SOULSEED_MODEL_ENABLED"] == "false"
    assert "SOULSEED_LOCAL_PAIRING_SECRET" not in env
    frontend = launcher._frontend_environment(env)
    assert frontend["VITE_API_URL"] == "http://127.0.0.1:8001/api"
    assert frontend["VITE_SOULSEED_DEVELOPMENT_MODE"] == "true"
    assert not any(key.upper().startswith("SOULSEED_") for key in frontend)
    assert not (root / "backend" / "data").exists()


@pytest.mark.asyncio
async def test_model_request_limit_is_persistent_and_log_omits_secret(tmp_path):
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.headers["Authorization"] == "Bearer fake-key"
        assert request.read().decode().count('"max_tokens":1024') == 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}],
                                         "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}})

    transport = httpx.MockTransport(respond)
    settings = Settings(
        environment="development", model_api_key="fake-key", model_name="fictional-model",
        model_base_url="http://127.0.0.1:9/v1", model_max_output_tokens=1024,
        model_daily_request_limit=1, model_request_log_path=str(tmp_path / "model-requests.jsonl"),
        _env_file=None,
    )
    make_client = lambda **kwargs: httpx.AsyncClient(transport=transport, **kwargs)
    adapter = OpenAICompatibleAdapter(settings, client_factory=make_client)
    assert await adapter.reply([{"role": "user", "content": "hello"}]) == "hi"
    with pytest.raises(ModelConfigurationError, match="上限"):
        await OpenAICompatibleAdapter(settings, client_factory=make_client).reply([{"role": "user", "content": "again"}])
    assert calls == 1
    log = (tmp_path / "model-requests.jsonl").read_text(encoding="utf-8")
    assert "fake-key" not in log and "hello" not in log
    assert '"total_tokens":12' in log


@pytest.mark.asyncio
async def test_model_off_refuses_before_network(tmp_path):
    settings = Settings(environment="development", model_enabled=False, model_api_key="fake-key", _env_file=None)
    adapter = OpenAICompatibleAdapter(settings)
    with pytest.raises(ModelConfigurationError, match="关闭"):
        await adapter.reply([{"role": "user", "content": "hello"}])
