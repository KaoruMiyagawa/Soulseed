"""Protocol regressions: synthetic content + observed metadata, no provider calls."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.exploration_agent import ExplorationAgent, ExplorationError
from app.model_adapter import ActionModelReply, OpenAICompatibleAdapter
from .test_exploration_agent import ScriptedModel, setup


OBSERVED = json.loads((Path(__file__).parent / "fixtures/exploration_failure_metadata.json").read_text(encoding="utf-8"))


def model_adapter(tmp_path, handler):
    log_path = tmp_path / "exploration-audit.jsonl"
    settings = Settings(
        model_enabled=True, model_api_key="PRIVATE_FAKE_KEY", model_name=OBSERVED["model"],
        model_base_url="http://127.0.0.1:9/v1", model_max_output_tokens=512,
        model_daily_request_limit=8, model_request_log_path=str(log_path),
    )
    adapter = OpenAICompatibleAdapter(settings, client_factory=lambda **kwargs: httpx.AsyncClient(
        transport=httpx.MockTransport(handler), **kwargs,
    ))
    return adapter, log_path


@pytest.mark.parametrize("body,reason", [
    ('```json\n{"action":"search_web","query":"whale song"}\n```', "invalid_json"),
    ('Here is the action: {"action":"search_web","query":"whale song"}', "invalid_json"),
    ('{"action":"search_web",', "invalid_json"),
    ('{"query":"whale song"}', "missing_field"),
    ('{"action":"read_webpage"}', "missing_field"),
    ('{"action":"search_web","arguments":{"query":"whale song"}}', "missing_field"),
    ('{"action":["search_web"],"query":"whale song"}', "invalid_arguments"),
    ('{"action":"search_web","query":23}', "invalid_arguments"),
    ('{"action":"search_web","query":"whale song","thought":"private"}', "invalid_arguments"),
    ('{"action":"search_web","query":""}', "invalid_arguments"),
    ('{"action":"search_web","query":"one","query":"two"}', "duplicate_field"),
    ('{"action":"search_web","query":NaN}', "invalid_json"),
    ('[]', "invalid_arguments"),
    ('{"action":"finish","summary":"result","claims":[{"text":"x","kind":[],"source_ids":["id"]}],"unanswered":[]}', "invalid_arguments"),
    ('{"action":"finish","summary":"result","claims":[{"text":"x"}],"unanswered":[]}', "missing_field"),
])
def test_observed_stop_metadata_does_not_hide_validation_subreason(body, reason):
    # These bodies are illustrative counterexamples, NOT the missing original response.
    reply = ActionModelReply(body, finish_reason=OBSERVED["finish_reason"],
                             http_status=OBSERVED["http_status"], usage=OBSERVED["usage"])
    shape = {}
    with pytest.raises(ExplorationError) as error:
        ExplorationAgent._parse_action(reply, step=0, shape=shape)
    assert error.value.code == "invalid_model_action"
    assert error.value.reason == reason
    assert error.value.reason != "truncated_output"


def test_real_adapter_json_protocol_completes_controlled_exploration_and_correlates_audit(tmp_path):
    scripted = ScriptedModel()
    requests = []

    async def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["response_format"] == {"type": "json_object"}
        assert "tools" not in body and "tool_choice" not in body
        assert body["max_tokens"] == 512
        content = await scripted.reply(body["messages"])
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "stop", "message": {"content": content, "reasoning_content": "PRIVATE_REASONING"}}],
            "usage": OBSERVED["usage"],
        })

    adapter, log_path = model_adapter(tmp_path, handler)
    _, _, repo, agent, run = setup(tmp_path, model=adapter)
    result = asyncio.run(agent.run(run["id"], "big_fish"))
    assert result["status"] == "completed"
    assert result["model_call_count"] == len(requests) == 2
    assert result["tool_call_count"] == 2
    assert len(repo.list_evidence(run["id"], "big_fish")) == 2
    entries = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert {entry["exploration_run_id"] for entry in entries} == {run["id"]}
    for step in (1, 2):
        events = [entry for entry in entries if entry["exploration_step"] == step]
        assert {entry["event"] for entry in events} == {"started", "finished", "action_validation"}
        assert len({entry["id"] for entry in events}) == 1
        audit = next(entry for entry in events if entry["event"] == "action_validation")
        assert audit["reason"] is None and audit["finish_reason"] == "stop"
        assert audit["usage"]["completion_tokens"] == 101
        assert audit["http_status"] == 200
    assert "PRIVATE_REASONING" not in log_path.read_text(encoding="utf-8")
    assert "PRIVATE_REASONING" not in json.dumps(requests)


@pytest.mark.parametrize("choice,reason", [
    ({"finish_reason": "length", "message": {"content": '{"action":"search_web","query":"whale song"}'}}, "truncated_output"),
    ({"finish_reason": "max_tokens", "message": {"content": '{"action":"search_web"'}}, "truncated_output"),
    ({"finish_reason": "tool_calls", "message": {"content": None, "tool_calls": [{"function": {"name": "search_web", "arguments": "{}"}}]}}, "unexpected_tool_calls"),
    ({"finish_reason": "stop", "message": {"content": '{"action":"search_web","query":"whale song"}', "tool_calls": [{"function": {"name": "shell"}}]}}, "unexpected_tool_calls"),
    ({"finish_reason": "content_filter", "message": {"content": '{}'}}, "unexpected_finish_reason"),
])
def test_incomplete_or_native_tool_output_never_executes_or_retries(tmp_path, choice, reason):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": [choice], "usage": OBSERVED["usage"]})
    adapter, log_path = model_adapter(tmp_path, handler)
    _, _, _, agent, run = setup(tmp_path, model=adapter)
    result = asyncio.run(agent.run(run["id"], "big_fish"))
    assert result["error_code"] == f"invalid_model_action:{reason}"
    assert result["status"] == "failed" and result["model_call_count"] == len(calls) == 1
    assert result["tool_call_count"] == 0 and result["evidence"] == []
    assert result["life_event_id"] is None
    entry = json.loads(log_path.read_text(encoding="utf-8").splitlines()[-1])
    assert entry["reason"] == reason


def test_validation_diagnostics_retain_only_schema_and_transport_metadata(tmp_path, caplog):
    content = json.dumps({
        "action": "search_web", "query": "PRIVATE_QUERY_VALUE",
        "PRIVATE_UNKNOWN_KEY": "PRIVATE_MODEL_VALUE",
    })
    def handler(_request):
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": content, "reasoning_content": "PRIVATE_REASONING",
        }}], "usage": OBSERVED["usage"]})
    adapter, log_path = model_adapter(tmp_path, handler)
    _, _, repo, agent, run = setup(tmp_path, model=adapter)
    with caplog.at_level("INFO", logger="soulseed.exploration"):
        result = asyncio.run(agent.run(run["id"], "big_fish"))
    assert result["error_code"] == "invalid_model_action:invalid_arguments"
    assert repo.get(run["id"], "big_fish")["error_code"] == result["error_code"]
    text = log_path.read_text(encoding="utf-8")
    assert "PRIVATE_" not in text and "PRIVATE_" not in caplog.text
    audit = json.loads(text.splitlines()[-1])
    assert audit["shape"]["field_types"] == {"action": "str", "query": "str"}
    assert audit["shape"]["unknown_field_count"] == 1
    assert audit["shape"]["content_chars"] == len(content)


def test_unsupported_json_mode_fails_once_without_text_fallback(tmp_path):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(400, json={"message": "response_format unsupported"})
    adapter, log_path = model_adapter(tmp_path, handler)
    _, _, _, agent, run = setup(tmp_path, model=adapter)
    result = asyncio.run(agent.run(run["id"], "big_fish"))
    assert result["error_code"] == "model_unavailable"
    assert result["tool_call_count"] == 0 and len(calls) == 1
    entries = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert entries[-1]["status"] == "http_400"
    assert entries[-1]["exploration_run_id"] == run["id"]


def test_unknown_action_does_not_leak_name_or_weaken_tool_allowlist():
    shape = {}
    with pytest.raises(ExplorationError) as error:
        ExplorationAgent._parse_action(ActionModelReply('{"action":"PRIVATE_SHELL_COMMAND"}'), step=0, shape=shape)
    assert error.value.code == "disallowed_tool" and error.value.reason == "unknown_action"
    assert shape["action"] == "unknown" and "PRIVATE_" not in json.dumps(shape)
