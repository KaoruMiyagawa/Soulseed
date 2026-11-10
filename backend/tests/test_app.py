import json

import pytest
import httpx

from app.chat_service import ChatService
from app.config import Settings
from app.db import Database
from app.model_adapter import (
    ClarificationAction,
    ClarificationResolution,
    ModelConfigurationError,
    ModelRequestError,
    OpenAICompatibleAdapter,
    StructuredOutputError,
)
from app.persona import assemble_system_prompt, load_persona_profile
from app.repositories import ConversationRepository, MessageRepository


class FakeModel:
    async def reply(self, messages):
        assert messages[-1]["role"] == "user"
        return "收到。"


@pytest.fixture
def repositories(tmp_path):
    database = Database(str(tmp_path / "soulseed.db"))
    database.initialize()
    return database, ConversationRepository(database), MessageRepository(database)


@pytest.mark.asyncio
async def test_chat_persists_user_and_assistant_messages(repositories):
    _, conversations, messages = repositories
    conversation = conversations.create()
    result = await ChatService(conversations, messages, FakeModel(), assemble_system_prompt()).send(conversation["id"], "你好")
    assert result["content"] == "收到。"
    assert result["correction"] == {"status": "no_action", "items": []}
    assert [m["role"] for m in messages.list_for_conversation(conversation["id"])] == ["user", "assistant"]


def test_database_migration_is_idempotent(repositories):
    database, _, _ = repositories
    database.initialize()
    with database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 21


def test_health_exposes_non_secret_runtime_build_and_startup_ids():
    from app.main import health

    response = health()
    assert response["status"] == "ok"
    assert response["environment"] == "testing"
    assert response["build_id"].startswith("dev-") and len(response["build_id"]) == 20
    assert len(response["startup_id"]) == 16


@pytest.mark.asyncio
async def test_missing_model_key_is_explicit():
    adapter = OpenAICompatibleAdapter(Settings(model_api_key=None, model_name="test"))
    with pytest.raises(ModelConfigurationError, match="API 密钥"):
        await adapter.reply([])


class FakeClient:
    def __init__(self, response=None, error=None, **_kwargs):
        self.response = response
        self.error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, *_args, **_kwargs):
        if self.error:
            raise self.error
        return self.response


def adapter_with_response(response=None, error=None):
    settings = Settings(model_api_key="test-key", model_name="deepseek-ai/DeepSeek-V4.1-Flash")
    return OpenAICompatibleAdapter(settings, client_factory=lambda **kwargs: FakeClient(response, error, **kwargs))


@pytest.mark.asyncio
async def test_model_response_is_extracted_without_exposing_auth():
    response = httpx.Response(200, json={"choices": [{"message": {"content": "真实接口格式回复"}}]})
    result = await adapter_with_response(response).reply([{"role": "user", "content": "你好"}])
    assert result == "真实接口格式回复"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 404, 429, 503])
async def test_provider_errors_are_explicit(status):
    response = httpx.Response(status, json={"message": "provider detail"})
    with pytest.raises(ModelRequestError):
        await adapter_with_response(response).reply([])


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 413])
async def test_provider_context_limit_error_is_clear_and_does_not_echo_raw_body(status):
    response = httpx.Response(status, json={"message": "maximum context length exceeded; private-provider-detail"})
    with pytest.raises(ModelRequestError) as error:
        await adapter_with_response(response).reply([])
    assert error.value.status_code == 413
    assert "上下文限制" in str(error.value)
    assert "private-provider-detail" not in str(error.value)


@pytest.mark.asyncio
async def test_model_failure_does_not_persist_fake_assistant_message(repositories):
    _, conversations, messages = repositories

    class FailingModel:
        async def reply(self, _messages):
            raise ModelRequestError("模型失败")

    conversation = conversations.create()
    with pytest.raises(ModelRequestError):
        await ChatService(conversations, messages, FailingModel(), assemble_system_prompt()).send(conversation["id"], "测试失败")
    saved = messages.list_for_conversation(conversation["id"])
    assert [message["role"] for message in saved] == ["user"]
    assert saved[0]["content"] == "测试失败"


@pytest.mark.asyncio
async def test_system_prompt_is_first_and_history_roles_are_preserved(repositories):
    _, conversations, messages = repositories
    captured = []

    class CapturingModel:
        async def reply(self, model_messages):
            captured.extend(model_messages)
            return "好的。"

    conversation = conversations.create()
    messages.add(conversation["id"], "user", "之前的问题")
    messages.add(conversation["id"], "assistant", "之前的回答")
    await ChatService(conversations, messages, CapturingModel(), "测试人格提示词").send(conversation["id"], "当前问题")

    assert captured[0] == {"role": "system", "content": "测试人格提示词"}
    assert [message["role"] for message in captured[1:]] == ["user", "assistant", "user"]
    assert captured[-1]["content"] == "当前问题"


@pytest.mark.asyncio
async def test_reasoning_content_is_not_used_as_final_reply():
    response = httpx.Response(200, json={"choices": [{"message": {"content": "最终回答", "reasoning_content": "内部推理"}}]})
    result = await adapter_with_response(response).reply([])
    assert result == "最终回答"


@pytest.mark.asyncio
async def test_structured_model_reply_is_parsed_and_requests_json_mode():
    captured = {}

    class CapturingClient(FakeClient):
        async def post(self, _url, **kwargs):
            captured.update(kwargs)
            return self.response

    payload = {
        "reply": "你想讲讲这次变化吗？",
        "clarification": {
            "clarification_id": "clarification-test",
            "action": "ask",
            "resolution": "none",
            "evidence_excerpt": None,
        },
    }
    response = httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]})
    adapter = OpenAICompatibleAdapter(
        Settings(model_api_key="test-key", model_name="test"),
        client_factory=lambda **kwargs: CapturingClient(response, **kwargs),
    )
    result = await adapter.reply_structured([{"role": "user", "content": "我换了环境"}])
    assert result.content == payload["reply"]
    assert result.action is ClarificationAction.ASK
    assert result.resolution is ClarificationResolution.NONE
    assert captured["json"]["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_structured_model_reply_ignores_additive_unknown_fields():
    response = httpx.Response(
        200,
        json={"choices": [{"message": {"content": json.dumps({
            "reply": "你好",
            "proposal_id": "ignored",
            "clarification": {
                "clarification_id": None,
                "action": "normal",
                "resolution": "none",
                "evidence_excerpt": None,
                "commentary": "ignored",
            },
        })}}]},
    )
    result = await adapter_with_response(response).reply_structured([])
    assert result.content == "你好"
    assert result.ignored_extra_fields is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "diagnostic"),
    [
        ("not json", "json_syntax_error"),
        ('{"reply":"x","clarification":', "json_truncated"),
        ('{"reply":"x"}', "missing_top_level_fields"),
        ('{"reply":"x","clarification":{}}', "missing_clarification_fields"),
        ('{"reply":"x","clarification":{"clarification_id":null,"action":"maybe","resolution":"none","evidence_excerpt":null}}', "invalid_action_enum"),
        ('{"reply":"x","clarification":{"clarification_id":null,"action":"normal","resolution":"maybe","evidence_excerpt":null}}', "invalid_resolution_enum"),
    ],
)
async def test_structured_output_errors_have_safe_diagnostic_codes(content, diagnostic):
    response = httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
    with pytest.raises(StructuredOutputError) as error:
        await adapter_with_response(response).reply_structured([])
    assert error.value.code == diagnostic
    assert content not in str(error.value)


@pytest.mark.asyncio
async def test_structured_output_truncation_finish_reason_is_detected():
    valid_payload = json.dumps({
        "reply": "你好",
        "clarification": {
            "clarification_id": None,
            "action": "normal",
            "resolution": "none",
            "evidence_excerpt": None,
        },
    })
    response = httpx.Response(200, json={"choices": [{
        "message": {"content": valid_payload}, "finish_reason": "length"
    }]})
    with pytest.raises(StructuredOutputError) as error:
        await adapter_with_response(response).reply_structured([])
    assert error.value.code == "json_truncated"


def test_persona_profile_contains_required_sections():
    profile = load_persona_profile()
    for section in ["核心身份", "核心气质", "表达习惯", "个人声音与真实经历", "关系与情感表达", "人格成长原则", "诚实与能力边界"]:
        assert section in profile
    assert "大肥鱼" in profile
    assert "童真" in profile
    assert "The user says" in profile


def test_persona_prioritizes_relationship_and_explicit_help_intent():
    profile = load_persona_profile()
    assert "先回应用户此刻的情绪、关系意味或分享本身" in profile
    assert "技术名词本身不代表用户在请求讲解" in profile
    assert "只有用户明确要求解释、分析、排错或编程帮助时" in profile
    assert "不必每次以问题结束" in profile
    assert "没有记录就坦诚说明，不编造生活日志" in profile


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "user_text",
    [
        "吃了吗鱼儿？",
        "今天好累啊。",
        "我今天吃了好吃的！",
        "你在干嘛呢？",
        "我最近有点想你。",
        "今天遇到一件特别开心的事。",
        "我决定换 Linux 了。",
        "给我解释一下 Linux 文件权限。",
        "你是不是只会问问题呀？",
        "我先去吃饭啦，待会儿找你。",
    ],
)
async def test_everyday_and_explicit_help_messages_share_persona_prompt(repositories, user_text):
    _, conversations, messages = repositories
    captured = []

    class CapturingModel:
        async def reply(self, model_messages):
            captured.extend(model_messages)
            return "测试模型回复"

    conversation = conversations.create()
    result = await ChatService(
        conversations, messages, CapturingModel(), assemble_system_prompt()
    ).send(conversation["id"], user_text)

    assert result["content"] == "测试模型回复"
    assert captured[0]["role"] == "system"
    assert "技术名词本身不代表用户在请求讲解" in captured[0]["content"]
    assert captured[-1] == {"role": "user", "content": user_text}
    assert [item["role"] for item in captured[1:]] == ["user"]


def test_custom_prompt_is_additive_and_cannot_replace_profile():
    prompt = assemble_system_prompt("本地测试补充")
    assert prompt.index("# 大肥鱼人格资料") < prompt.index("本地测试补充")
    assert "核心身份" in prompt
    assert "只能补充实现细节" in prompt


def test_blank_custom_prompt_uses_profile_without_extra_section():
    profile = load_persona_profile()
    assert assemble_system_prompt("  ") == profile

