import pytest

from app.chat_service import ChatService
from app.db import Database
from app.memory import CorrectionIntent, DeterministicMemoryExtractor, MemoryRepository, MemoryService, MemoryType
from app.repositories import ConversationRepository, MessageRepository


@pytest.fixture
def extractor():
    return DeterministicMemoryExtractor()


@pytest.fixture
def memory_chat(tmp_path):
    database = Database(str(tmp_path / "extractor.db"))
    database.initialize()
    conversations = ConversationRepository(database)
    messages = MessageRepository(database)
    memory_service = MemoryService(MemoryRepository(database))

    class FakeModel:
        async def reply(self, _messages):
            return "收到。"

    chat = ChatService(conversations, messages, FakeModel(), "测试人格", memory_service, DeterministicMemoryExtractor())
    return database, conversations, messages, memory_service, chat


def test_explicit_preference_is_extracted(extractor):
    candidates = extractor.extract_text("我喜欢简洁、清爽的界面。")
    assert len(candidates) == 1
    assert candidates[0].memory_type is MemoryType.USER_PREFERENCE
    assert candidates[0].confidence >= 0.9
    assert "喜欢简洁" in candidates[0].content


def test_goal_event_relationship_and_growth_are_classified(extractor):
    assert extractor.extract_text("以后我想学习 Rust。")[0].memory_type is MemoryType.GOAL
    assert extractor.extract_text("我完成了 Soulseed 项目的 MVP。")[0].memory_type is MemoryType.EVENT
    assert extractor.extract_text("我的姐姐正在准备考试。")[0].memory_type is MemoryType.RELATIONSHIP
    assert extractor.extract_text("我正在学习 Agent 开发。")[0].memory_type is MemoryType.GROWTH


@pytest.mark.parametrize("text", [
    "你好",
    "哈哈",
    "今天有点累",
    "如果我以后学 Rust，会不会很难？",
    "我喜欢 Python 吗？",
    "有人喜欢用 Python 写脚本。",
    "“我喜欢 Python。”",
    "",
    "!!!",
])
def test_low_value_hypothetical_question_quote_and_empty_text_are_skipped(extractor, text):
    assert extractor.extract_text(text) == []


def test_negative_preference_is_not_reversed(extractor):
    candidates = extractor.extract_text("我不喜欢复杂的界面。")
    assert len(candidates) == 1
    assert "不喜欢" in candidates[0].content
    assert "喜欢复杂" not in candidates[0].content.replace("不喜欢复杂", "")


def test_correction_is_marked_without_guessing_replacement(extractor):
    candidates = extractor.extract_text("我之前说错了，我其实更喜欢简洁的界面。")
    assert len(candidates) == 1
    assert candidates[0].evidence_type.value == "correction"
    assert candidates[0].confidence == 0.95
    assert candidates[0].intent is CorrectionIntent.EXPLICIT_CORRECTION


@pytest.mark.parametrize("text, expected", [
    ("我现在更喜欢 Rust 了。", CorrectionIntent.LONG_TERM_CHANGE),
    ("这周我想试试 Rust。", CorrectionIntent.TEMPORARY_EXPERIMENT),
    ("Rust 好像也不错。", CorrectionIntent.AMBIGUOUS_CHANGE),
    ("我喜欢 Python，也喜欢 Rust。", CorrectionIntent.COEXISTING_INFORMATION),
    ("我喜欢 Python。", CorrectionIntent.NONE),
])
def test_correction_intent_categories_are_deterministic(extractor, text, expected):
    candidates = extractor.extract_text(text)
    assert candidates
    assert {candidate.intent for candidate in candidates} == {expected}


def test_intent_priority_prefers_explicit_correction_and_temporary_experiment(extractor):
    assert extractor.classify_intent("我之前说错了，这周我想试试 Rust。") is CorrectionIntent.EXPLICIT_CORRECTION
    assert extractor.classify_intent("今天我已经改成先看代码。") is CorrectionIntent.TEMPORARY_EXPERIMENT


@pytest.mark.asyncio
async def test_special_intent_candidates_are_not_persisted_as_active_memory(memory_chat):
    _, conversations, messages, memory_service, _ = memory_chat
    conversation = conversations.create()
    user_message = messages.add(conversation["id"], "user", "我之前说错了，我其实更喜欢 Rust。")
    candidate = DeterministicMemoryExtractor().extract(user_message)[0]

    assert candidate.intent is CorrectionIntent.EXPLICIT_CORRECTION
    assert memory_service.persist_candidate(candidate, user_message) is None
    assert memory_service.list_memories() == []


def test_non_user_message_is_never_extracted(extractor):
    assert extractor.extract({"id": "assistant-1", "role": "assistant", "content": "我喜欢 Python。"}) == []


@pytest.mark.asyncio
async def test_chat_persists_extracted_memory_and_source(memory_chat):
    _, conversations, messages, memory_service, chat = memory_chat
    conversation = conversations.create()
    await chat.send(conversation["id"], "我喜欢简洁的界面。")
    saved_messages = messages.list_for_conversation(conversation["id"])
    memories = memory_service.list_memories()
    assert [message["role"] for message in saved_messages] == ["user", "assistant"]
    assert len(memories) == 1
    sources = memory_service.list_sources(memories[0].id)
    assert len(sources) == 1
    assert sources[0].message_id == saved_messages[0]["id"]
    assert sources[0].source_role.value == "user"


@pytest.mark.asyncio
async def test_reprocessing_same_message_is_idempotent(memory_chat):
    _, conversations, messages, memory_service, chat = memory_chat
    conversation = conversations.create()
    await chat.send(conversation["id"], "我喜欢简洁的界面。")
    user_message = messages.list_for_conversation(conversation["id"])[0]
    extractor = DeterministicMemoryExtractor()
    for candidate in extractor.extract(user_message):
        memory_service.persist_candidate(candidate, user_message)
    assert len(memory_service.list_memories()) == 1
    assert len(memory_service.list_sources(memory_service.list_memories()[0].id)) == 1


@pytest.mark.asyncio
async def test_duplicate_canonical_key_does_not_fail_chat(memory_chat):
    _, conversations, messages, memory_service, chat = memory_chat
    first = conversations.create()
    await chat.send(first["id"], "我喜欢简洁的界面。")
    second = conversations.create()
    result = await chat.send(second["id"], "我喜欢简洁的界面。")
    assert result["role"] == "assistant"
    assert len(memory_service.list_memories()) == 1
    assert len(memory_service.list_sources(memory_service.list_memories()[0].id)) == 2


@pytest.mark.asyncio
async def test_extraction_failure_does_not_fail_chat(tmp_path):
    database = Database(str(tmp_path / "failure.db"))
    database.initialize()
    conversations = ConversationRepository(database)
    messages = MessageRepository(database)

    class BrokenExtractor:
        def extract(self, _message):
            raise RuntimeError("测试提取失败")

    class FakeModel:
        async def reply(self, _messages):
            return "聊天仍然正常。"

    service = MemoryService(MemoryRepository(database))
    chat = ChatService(conversations, messages, FakeModel(), "测试人格", service, BrokenExtractor())
    conversation = conversations.create()
    result = await chat.send(conversation["id"], "我喜欢简洁的界面。")
    assert result["content"] == "聊天仍然正常。"
    assert [message["role"] for message in messages.list_for_conversation(conversation["id"])] == ["user", "assistant"]

