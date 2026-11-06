from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest

from app.chat_service import ChatService
from app.contact import ContactService
from app.db import Database
from app.life import LifeRepository
from app.ownership import character_scope
from app.persona import resolve_system_prompt
from app.repositories import ConversationRepository, MessageRepository


class CapturingModel:
    def __init__(self):
        self.prompts: list[list[dict[str, str]]] = []

    async def reply(self, prompt):
        self.prompts.append(prompt)
        return "受控测试回复"


def _chat(tmp_path, *, contact_enabled=True, break_life_snapshot=False):
    database = Database(str(tmp_path / "preference-contact.db"))
    database.initialize()
    repository = LifeRepository(database)
    conversations = ConversationRepository(database)
    messages = MessageRepository(database)
    with character_scope("big_fish"):
        conversation = conversations.create()
    model = CapturingModel()
    if break_life_snapshot:
        repository.expression_snapshot = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("test failure"))
    chat = ChatService(
        conversations, messages, model, resolve_system_prompt(None),
        life_repository=repository,
        contact_service=ContactService(repository, enabled=contact_enabled),
        runtime_features={"contact_background_enabled": True, "contact_model_generation_enabled": False},
    )
    return repository, conversation["id"], model, chat


def _send(chat, conversation_id, text):
    with character_scope("big_fish"):
        asyncio.run(chat.send(conversation_id, text))


def _expression(prompt):
    rendered = next(
        message["content"] for message in prompt
        if message["role"] == "system" and "<expression_context" in message["content"]
    )
    payload_text = rendered.split("\n{", 1)[1].split("\n</expression_context>", 1)[0]
    return json.loads("{" + payload_text), rendered


def test_music_and_food_preferences_are_not_gated_on_recorded_experience(tmp_path, monkeypatch):
    from app.life_engine import LifeEngine

    monkeypatch.setattr(LifeEngine, "advance", lambda self, character_id: [])
    repository, conversation_id, model, chat = _chat(tmp_path)
    for question in ("你最喜欢哪首歌呀", "你最喜欢吃什么"):
        _send(chat, conversation_id, question)
        persona = model.prompts[-1][0]["content"]
        assert "没有相关经历记录，不妨碍形成当下偏好" in persona
        payload, rendered = _expression(model.prompts[-1])
        assert payload["evidence"] == []
        assert payload["life_scope"]["matching_evidence_count"] == 0
        assert "没有经历记录时仍可自然表达当下喜好" in rendered

    now = datetime.now(UTC)
    repository.record("听过一首轻快的歌", event_type="music", now=now)
    repository.record("吃过草莓蛋糕", event_type="meal", now=now)
    _send(chat, conversation_id, "你最喜欢哪首歌呀")
    music_payload, _ = _expression(model.prompts[-1])
    assert any(item["kind"] == "music" and item["status"] == "completed" for item in music_payload["evidence"])
    _send(chat, conversation_id, "你最喜欢吃什么")
    payload, rendered = _expression(model.prompts[-1])
    assert any(item["kind"] == "meal" and item["status"] == "completed" for item in payload["evidence"])
    assert "没有经历记录时仍可自然表达当下喜好" in rendered


@pytest.mark.parametrize("question", [
    "你会主动给我发信息吗",
    "你能主动联系我吗",
    "你会自己来找我聊天吗",
    "你会先给我发消息吗",
])
def test_common_proactive_message_wording_gets_enabled_runtime_facts(tmp_path, monkeypatch, question):
    from app.life_engine import LifeEngine

    monkeypatch.setattr(LifeEngine, "advance", lambda self, character_id: [])
    _, conversation_id, model, chat = _chat(tmp_path, contact_enabled=True)
    _send(chat, conversation_id, question)
    payload, rendered = _expression(model.prompts[-1])
    capability = payload["runtime_capabilities"]["proactive_contact"]
    assert capability["supported"] is True
    assert capability["enabled"] is True
    assert capability["background_checks_enabled"] is True
    assert capability["policy_allows_now"] is True
    assert capability["ready_in_app_messages"] == 0
    assert capability["previous_message_delivered"] is False
    assert capability["external_push"] is False
    assert "enabled 或 policy_allows_now 不代表消息已经送达" in rendered


@pytest.mark.parametrize("question", ["你会主动联系我吗", "你会主动给我发信息吗"])
def test_disabled_contact_is_distinguished_from_unsupported_and_life_failure_keeps_snapshot(tmp_path, monkeypatch, question):
    from app.life_engine import LifeEngine

    monkeypatch.setattr(LifeEngine, "advance", lambda self, character_id: [])
    _, conversation_id, model, chat = _chat(tmp_path, contact_enabled=False, break_life_snapshot=True)
    _send(chat, conversation_id, question)
    payload, _ = _expression(model.prompts[-1])
    capability = payload["runtime_capabilities"]["proactive_contact"]
    assert capability["supported"] is True
    assert capability["enabled"] is False
    assert capability["policy_allows_now"] is False
    assert capability["ready_in_app_messages"] == 0


def test_new_and_existing_conversations_assemble_different_history_with_current_rules(tmp_path, monkeypatch):
    from app.life_engine import LifeEngine

    monkeypatch.setattr(LifeEngine, "advance", lambda self, character_id: [])
    _, new_conversation_id, model, chat = _chat(tmp_path)
    _send(chat, new_conversation_id, "你会主动给我发信息吗")
    new_prompt = model.prompts[-1]
    assert not any(message["role"] == "assistant" for message in new_prompt)
    assert "没有相关经历记录，不妨碍形成当下偏好" in new_prompt[0]["content"]
    new_payload, _ = _expression(new_prompt)
    assert new_payload["runtime_capabilities"]["proactive_contact"]["enabled"] is True

    with character_scope("big_fish"):
        existing = chat.conversations.create()
        chat.messages.add(
            existing["id"], "assistant",
            "我不能主动联系你，而且我没有最喜欢的歌，因为没有存下来。",
        )
    _send(chat, existing["id"], "你会主动给我发信息吗")
    existing_prompt = model.prompts[-1]
    old_assistant = next(message["content"] for message in existing_prompt if message["role"] == "assistant")
    assert "我不能主动联系你" in old_assistant
    persona = next(message["content"] for message in existing_prompt if message["role"] == "system")
    assert "旧对话中的助手回答不是能力状态或过去经历的证据" in persona
    payload, rendered = _expression(existing_prompt)
    assert payload["runtime_capabilities"]["proactive_contact"]["enabled"] is True
    assert "旧对话中的助手回答不是能力状态的证据" in rendered


def test_contact_delivery_context_distinguishes_ready_from_delivered(tmp_path):
    database = Database(str(tmp_path / "delivery-status.db"))
    database.initialize()
    repository = LifeRepository(database)
    conversations = ConversationRepository(database)
    now = datetime(2026, 11, 4, 12, tzinfo=UTC)
    with character_scope("big_fish"):
        conversation = conversations.create()
    with database.connection() as connection:
        connection.execute(
            "INSERT INTO messages(id, conversation_id, role, content, created_at) VALUES ('u1', ?, 'user', '之前提到那本书', ?)",
            (conversation["id"], "2026-11-04T08:00:00+00:00"),
        )
    service = ContactService(repository)
    service.create_intent(source="reminder", source_id="saved-reminder", reason="saved", topic="那本书", now=now)
    ready = asyncio.run(service.tick(now=now))
    assert ready is not None
    snapshot = service.delivery_context(now=now)
    assert snapshot["supported"] is True
    assert snapshot["enabled"] is True
    assert snapshot["policy_allows_now"] is True
    assert snapshot["ready_in_app_messages"] == 1
    assert snapshot["previous_message_delivered"] is False

    delivered = service.deliver(ready["id"], conversation["id"], now=now)
    assert delivered["status"] == "delivered"
    snapshot = service.delivery_context(now=now)
    assert snapshot["ready_in_app_messages"] == 0
    assert snapshot["previous_message_delivered"] is True
