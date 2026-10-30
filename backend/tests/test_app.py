import pytest
import httpx

from app.chat_service import ChatService
from app.config import Settings
from app.db import Database
from app.model_adapter import ModelConfigurationError, ModelRequestError, OpenAICompatibleAdapter
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
    assert [m["role"] for m in messages.list_for_conversation(conversation["id"])] == ["user", "assistant"]


def test_database_migration_is_idempotent(repositories):
    database, _, _ = repositories
    database.initialize()
    with database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 2


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


def test_persona_profile_contains_required_sections():
    profile = load_persona_profile()
    for section in ["核心身份", "核心气质", "表达习惯", "关系与情感表达", "人格成长原则", "诚实与能力边界"]:
        assert section in profile
    assert "大肥鱼" in profile
    assert "童真" in profile
    assert "The user says" in profile


def test_custom_prompt_is_additive_and_cannot_replace_profile():
    prompt = assemble_system_prompt("本地测试补充")
    assert prompt.index("# 大肥鱼人格资料") < prompt.index("本地测试补充")
    assert "核心身份" in prompt
    assert "只能补充实现细节" in prompt


def test_blank_custom_prompt_uses_profile_without_extra_section():
    profile = load_persona_profile()
    assert assemble_system_prompt("  ") == profile

