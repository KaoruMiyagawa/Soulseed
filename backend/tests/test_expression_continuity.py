"""Prompt evidence checks with fictional SQLite data and a capturing model."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

from app.chat_service import ChatService
from app.contact import ContactService
from app.db import Database
from app.life import LifeRepository
from app.life_engine import LifeEngine
from app.memory import MemoryContextBuilder, MemoryCreate, MemoryRepository, MemoryRetriever, MemoryService, MemoryType
from app.context_window import ContextWindowLimits, ModelContextWindow
from app.ownership import character_scope
from app.persona import resolve_system_prompt
from app.repositories import ConversationRepository, MessageRepository


class CapturingModel:
    def __init__(self):
        self.prompts: list[list[dict[str, str]]] = []

    async def reply(self, prompt):
        self.prompts.append(prompt)
        return "受控模型回复"


def make_chat(tmp_path, monkeypatch, *, contact_enabled=None):
    database = Database(str(tmp_path / "fictional-expression.db"))
    database.initialize()
    repository = LifeRepository(database)
    conversations = ConversationRepository(database)
    messages = MessageRepository(database)
    with character_scope("big_fish"):
        conversation = conversations.create()
    model = CapturingModel()
    monkeypatch.setattr(LifeEngine, "advance", lambda self, character_id: [])
    contact = None if contact_enabled is None else ContactService(repository, enabled=contact_enabled)
    chat = ChatService(
        conversations, messages, model, "测试人格", life_repository=repository,
        contact_service=contact,
    )
    return repository, messages, conversation["id"], model, chat


def send(chat, conversation_id, text):
    with character_scope("big_fish"):
        asyncio.run(chat.send(conversation_id, text))


def expression(prompt):
    rendered = next(item["content"] for item in prompt if item["role"] == "system" and "<expression_context" in item["content"])
    return json.loads("{" + rendered.split("\n{", 1)[1].split("\n</expression_context>", 1)[0]), rendered


def test_active_exploration_survives_a_specific_followup(tmp_path, monkeypatch):
    repository, _, conversation_id, model, chat = make_chat(tmp_path, monkeypatch)
    now = datetime.now(UTC)
    planned = repository.record("探索自己的小角落", event_type="exploring", status="planned",
                                planned_end_at=now + timedelta(hours=1), now=now)
    repository.transition(planned["id"], "active", now=now)
    send(chat, conversation_id, "鱼儿，你在干嘛？")
    send(chat, conversation_id, "探索到什么了？")
    payload, _ = expression(model.prompts[-1])
    assert any(item["evidence_id"] == planned["id"] and item["status"] == "active"
               for item in payload["evidence"])


def test_no_exploration_record_is_an_explicit_absence_of_retrieved_evidence(tmp_path, monkeypatch):
    _, _, conversation_id, model, chat = make_chat(tmp_path, monkeypatch)
    send(chat, conversation_id, "你探索了什么？")
    payload, _ = expression(model.prompts[-1])
    assert payload["life_scope"]["topic"] == "exploring"
    assert payload["life_scope"]["matching_evidence_count"] == 0
    assert payload["evidence"] == []


def test_reading_and_music_activity_do_not_supply_unrecorded_book_or_track(tmp_path, monkeypatch):
    repository, _, conversation_id, model, chat = make_chat(tmp_path, monkeypatch)
    now = datetime.now(UTC)
    repository.record("翻看一本有趣的书", event_type="reading", now=now)
    repository.record("听音乐，跟着节奏晃一晃", event_type="music", now=now)
    send(chat, conversation_id, "你读的什么书？")
    reading, reading_text = expression(model.prompts[-1])
    assert any(item["kind"] == "reading" and item["status"] == "completed" for item in reading["evidence"])
    assert "书名" in reading_text
    send(chat, conversation_id, "刚才听了哪首歌？")
    music, music_text = expression(model.prompts[-1])
    assert any(item["kind"] == "music" and item["status"] == "completed" for item in music["evidence"])
    assert "曲名" in music_text


def test_contact_capability_is_distinct_from_enabled_and_deliverable(tmp_path, monkeypatch):
    _, _, conversation_id, model, chat = make_chat(tmp_path, monkeypatch, contact_enabled=False)
    send(chat, conversation_id, "你现在能主动联系我吗？")
    payload, _ = expression(model.prompts[-1])
    capability = payload["runtime_capabilities"]["proactive_contact"]
    assert capability["supported"] is True
    assert capability["enabled"] is False
    assert capability["policy_allows_now"] is False
    assert capability["ready_in_app_messages"] == 0
    assert capability["external_push"] is False


def test_location_context_describes_only_implemented_digital_surface(tmp_path, monkeypatch):
    _, _, conversation_id, model, chat = make_chat(tmp_path, monkeypatch)
    send(chat, conversation_id, "鱼儿，你住在哪里？有自己的房间吗？")
    payload, _ = expression(model.prompts[-1])
    space = payload["runtime_capabilities"]["digital_space"]
    assert space["web_chat_surface"] is True
    assert space["immersive_room"] is False
    assert space["physical_location"] is False


def test_combined_lifecycle_states_keep_their_effective_meaning(tmp_path, monkeypatch):
    repository, _, conversation_id, model, chat = make_chat(tmp_path, monkeypatch)
    now = datetime.now(UTC)
    repository.instance_token = "fictional-owner"
    stopped = repository.record("探索一个角落", event_type="exploring", status="planned",
                                planned_end_at=now + timedelta(hours=1), now=now)
    repository.transition(stopped["id"], "active", now=now)
    repository.interrupt_active(owner_token="fictional-owner", reason="service_stopped", now=now)
    planned = repository.record("准备做饭", event_type="cooking", status="planned",
                                planned_end_at=now + timedelta(hours=1), now=now)
    completed = repository.record("听音乐", event_type="music", now=now)
    active = repository.record("翻看一本书", event_type="reading", status="planned",
                               planned_end_at=now + timedelta(hours=1), now=now)
    repository.transition(active["id"], "active", now=now)

    send(chat, conversation_id, "今天都做了什么？")
    payload, _ = expression(model.prompts[-1])
    statuses = {item["evidence_id"]: item["status"] for item in payload["evidence"]}
    assert statuses[stopped["id"]] == "interrupted"
    assert statuses[planned["id"]] == "planned"
    assert statuses[completed["id"]] == "completed"
    assert statuses[active["id"]] == "active"


def test_affection_and_jokes_keep_persona_without_forcing_life_report(tmp_path, monkeypatch):
    repository, _, conversation_id, model, chat = make_chat(tmp_path, monkeypatch)
    chat.system_prompt = resolve_system_prompt(None)
    repository.record("听音乐", event_type="music", now=datetime.now(UTC))
    for text in ("大肥鱼，我想你了。", "我喜欢你。", "哈哈哈，逗你玩呢。"):
        send(chat, conversation_id, text)
        systems = [item["content"] for item in model.prompts[-1] if item["role"] == "system"]
        assert "童真" in systems[0] and "不必每次以问题结束" in systems[0]
        assert all("<expression_context" not in item for item in systems[1:])


def test_trimmed_history_still_includes_retrieved_memory_and_running_life_event(tmp_path, monkeypatch):
    repository, messages, conversation_id, model, _ = make_chat(tmp_path, monkeypatch)
    memory_service = MemoryService(MemoryRepository(repository.database))
    with character_scope("big_fish"):
        memory = memory_service.create_memory(
            MemoryCreate(MemoryType.USER_PREFERENCE, "用户喜欢简洁的界面", "pref:concise-ui")
        )
    now = datetime.now(UTC)
    planned = repository.record("探索自己的小角落", event_type="exploring", status="planned",
                                planned_end_at=now + timedelta(hours=1), now=now)
    repository.transition(planned["id"], "active", now=now)
    with character_scope("big_fish"):
        for index in range(120):
            messages.add(conversation_id, "user", f"旧话题 {index:03d}")
            messages.add(conversation_id, "assistant", f"回应 {index:03d}")
    chat = ChatService(
        ConversationRepository(repository.database), messages, model, "测试人格",
        memory_retriever=MemoryRetriever(memory_service),
        memory_context_builder=MemoryContextBuilder(), life_repository=repository,
        context_window=ModelContextWindow(ContextWindowLimits(
            max_input_tokens=4000, max_persona_tokens=500, max_expression_tokens=3500,
            max_clarification_tokens=500, max_system_control_tokens=500,
            max_current_message_tokens=500, max_history_tokens=100,
        )),
    )
    send(chat, conversation_id, "你还记得我喜欢简洁的界面吗？你还在探索吗？")
    payload, _ = expression(model.prompts[-1])
    assert any(item["evidence_id"] == memory.id and item["source"] == "long_term_memory"
               for item in payload["evidence"])
    assert any(item["evidence_id"] == planned["id"] and item["status"] == "active"
               for item in payload["evidence"])
    assert chat.last_context_report.dropped_turns > 0
    with character_scope("big_fish"):
        assert len(messages.list_for_conversation(conversation_id)) == 242
