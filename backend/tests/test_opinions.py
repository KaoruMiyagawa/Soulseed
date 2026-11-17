import asyncio
import json
from datetime import UTC, datetime, timedelta

from app import db as db_module
from app.chat_service import ChatService
from app.db import Database
from app.desktop_contact import DesktopContact
from app.life import LifeRepository
from app.memory.context_builder import MemoryContextBuilder
from app.memory.extractor import DeterministicMemoryExtractor
from app.memory.repositories import MemoryRepository
from app.memory.retriever import MemoryRetriever
from app.memory.service import MemoryService
from app.ownership import character_scope
from app.repositories import ConversationRepository, MessageRepository

from .test_desktop_contact import setup_desktop
from .test_exploration_agent import setup as setup_exploration


TOPIC = "海獭展览"
INITIAL = "我倒觉得海獭展览用简单的插画就好，留点空间给大家自己猜。"
REVISED = "我改主意啦，海獭展览该把敲开贝壳的过程也画出来。"
REASON = "想给参观的人留一些想象和提问的空间"


def proposed(stance=INITIAL, *, previous=None, basis=None, reason=REASON):
    return stance + "\n<opinion_update>" + json.dumps({
        "topic": TOPIC, "stance": stance, "reason": reason, "previous": previous, "basis": basis,
    }, ensure_ascii=False)


class ChatMock:
    def __init__(self, reply):
        self.answer = reply
        self.calls = []

    async def reply(self, messages):
        self.calls.append(messages)
        return self.answer(messages) if callable(self.answer) else self.answer


def expression(messages):
    for message in messages:
        if message["content"].startswith("<expression_context"):
            return json.loads(next(line for line in message["content"].splitlines() if line.startswith("{")))
    return {}


def review(messages):
    instructions = next(message["content"] for message in messages if "供复核的话题及新依据" in message["content"])
    return json.loads(instructions.splitlines()[-1])


def send(db, model, text, *, life=None):
    conversations = ConversationRepository(db)
    conversation = conversations.create()
    memories = MemoryService(MemoryRepository(db))
    chat = ChatService(
        conversations, MessageRepository(db), model, "大肥鱼",
        memory_service=memories, memory_extractor=DeterministicMemoryExtractor(),
        memory_retriever=MemoryRetriever(memories), memory_context_builder=MemoryContextBuilder(),
        life_repository=life,
    )
    return asyncio.run(chat.send(conversation["id"], text))


def history(db):
    with db.connection() as connection:
        return [dict(row) for row in connection.execute(
            "SELECT * FROM character_opinion_revisions WHERE character_id=current_character() ORDER BY id",
        )]


def test_opinion_survives_new_conversation_and_restart_without_copying_user_position(tmp_path):
    path = str(tmp_path / "opinions.db")
    db = Database(path)
    db.initialize()
    model = ChatMock(proposed())
    reply = send(db, model, "我觉得海獭展览应该塞满图解，你怎么看？")
    assert reply["content"] == INITIAL
    assert "opinions" not in expression(model.calls[0])
    [first] = history(db)
    assert first["stance"] == INITIAL and first["basis_json"] is None
    assert first["assistant_message_id"] == reply["id"] and first["user_message_id"]

    restarted = Database(path)
    restarted.initialize()

    def continue_same_view(messages):
        [opinion] = expression(messages)["opinions"]
        assert opinion == {"topic": TOPIC, "stance": INITIAL, "reason": REASON}
        assert not any(message["role"] == "assistant" for message in messages)
        return "我还是会留点空白，让大家猜猜海獭接下来会做什么。"

    result = send(restarted, ChatMock(continue_same_view), "再聊聊海獭展览，你会怎样布置？")
    assert result["content"].startswith("我还是会留点空白")
    assert history(restarted) == [first]
    unrelated = ChatMock("我还没想好。")
    assert send(restarted, unrelated, "你对城市夜景有什么长期看法？")["content"] == "我还没想好。"
    assert "opinions" not in expression(unrelated.calls[0])
    assert history(restarted) == [first]


def test_updates_require_new_same_topic_evidence_and_save_both_kinds_of_basis(tmp_path):
    db, life, _, agent, run = setup_exploration(tmp_path, topic="海獭展览怎么呈现海獭觅食")
    send(db, ChatMock(proposed()), "海獭展览应以互动为主吗，你怎么看？", life=life)
    [initial] = history(db)
    planned = life.record("海獭展览准备试一试动手绘图", status="planned")
    other = life.record("读完了关于星空的图册")

    def unjustified(messages):
        [entry] = review(messages)
        assert entry["new_basis"] == []
        return proposed(REVISED, previous=initial["id"], basis=planned["id"])

    assert send(db, ChatMock(unjustified), "海獭展览的看法变了吗？", life=life)["content"] == REVISED
    assert history(db) == [initial]

    completed_run = asyncio.run(agent.run(run["id"], "big_fish"))
    assert completed_run["status"] == "completed"

    def from_knowledge(messages):
        [entry] = review(messages)
        [basis] = entry["new_basis"]
        assert basis["kind"] == "sourced_research" and "海獭" in basis["text"]
        assert basis["source_ids"] == completed_run["claims"][0]["source_ids"]
        return proposed(REVISED, previous=entry["previous"], basis=basis["id"],
                        reason="查到觅食行为后，觉得过程比结果更值得展示")

    send(db, ChatMock(from_knowledge), "现在怎么看海獭展览？", life=life)
    first, second = history(db)
    assert first == initial and second["stance"] == REVISED
    assert json.loads(second["basis_json"])["id"] == f"{run['id']}:0"
    experience = life.record("完成了一次海獭展览绘图，连环画比长段说明更容易读")
    final = "我更想把海獭展览画成两格小漫画，来个贝壳啪的一声！"

    def from_experience(messages):
        [entry] = review(messages)
        [basis] = entry["new_basis"]
        assert basis["id"] == experience["id"] and basis["id"] != other["id"]
        assert basis["kind"] == "completed_experience"
        assert expression(messages)["opinions"][0]["stance"] == REVISED
        return proposed(final, previous=entry["previous"], basis=basis["id"],
                        reason="已经试过连环画，觉得画短一些更好读")

    send(db, ChatMock(from_experience), "海獭展览还想怎样调整？", life=life)
    _, _, third = history(db)
    assert third["stance"] == final
    assert json.loads(third["basis_json"])["source_ids"] == [experience["id"]]


def test_user_memory_and_other_character_never_supply_a_character_opinion(tmp_path, monkeypatch):
    path = str(tmp_path / "isolated-opinions.db")
    db = Database(path)
    with monkeypatch.context() as old:
        old.delitem(db_module.MIGRATIONS, 29)
        db.initialize()
        conversation = ConversationRepository(db).create()
        user = MessageRepository(db).add(conversation["id"], "user", "我喜欢海獭展览。")
        memories = MemoryService(MemoryRepository(db))
        for candidate in DeterministicMemoryExtractor().extract(user):
            memories.persist_candidate(candidate, user)
        assert MemoryRepository(db).list()
    db.initialize()
    assert history(db) == []
    assert MemoryRepository(db).list()
    reply = ChatMock(proposed())
    send(db, reply, "我喜欢海獭展览。你怎么看海獭展览？")
    user_data = expression(reply.calls[0])
    assert "opinions" not in user_data
    assert any(item["source"] == "long_term_memory" for item in user_data["evidence"])
    with db.connection() as connection:
        connection.execute("INSERT INTO characters VALUES ('other','local','owned',datetime('now'))")
    with character_scope("other"):
        other = ChatMock("我没形成看法。\n<opinion_update>{\"topic\":")
        assert send(db, other, "你怎么看海獭展览？")["content"] == "我没形成看法。"
        assert "opinions" not in expression(other.calls[0])
        assert history(db) == []
    assert len(history(db)) == 1


def test_shared_autonomous_contact_receives_the_character_opinion(tmp_path, monkeypatch):
    runtime, life, model, _, attempts = setup_desktop(
        tmp_path, monkeypatch, outcome="friendly", with_discovery=False,
    )
    send(runtime.database, ChatMock(proposed()), "海獭展览，你怎么看？")
    with runtime.database.connection() as connection:
        old = (datetime.now(UTC) - timedelta(hours=4)).isoformat()
        connection.execute("UPDATE messages SET created_at=?", (old,))
        connection.execute("UPDATE life_states SET mood='好奇' WHERE character_id='big_fish'")
    assert asyncio.run(DesktopContact(runtime, life, model).tick()) == "delivered"
    [signals] = attempts
    assert signals["candidate_type"] == "self_social"
    assert signals["opinions"] == [{"topic": TOPIC, "stance": INITIAL, "reason": REASON}]
    assert len(history(runtime.database)) == 1
