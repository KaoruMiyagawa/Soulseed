from datetime import UTC, datetime, timedelta

import pytest

from app.chat_service import ChatService
from app.db import Database
from app.life import LifeRepository
from app.life_engine import ACTIVITIES, LifeEngine
from app.repositories import ConversationRepository, MessageRepository


@pytest.fixture
def life(tmp_path):
    db = Database(str(tmp_path / "life.db"))
    db.initialize()
    return db, LifeRepository(db)


def test_new_character_state_and_completed_food_are_persistent(life):
    db, repo = life
    now = datetime(2026, 11, 2, 5, tzinfo=UTC)
    repo.ensure_state("fish", now=now)
    event = repo.record("吃了番茄鸡蛋面", character_id="fish", event_type="meal", now=now)
    restarted = LifeRepository(Database(db.path))
    assert restarted.context("鱼儿吃了吗", character_id="fish", now=now).find("吃了番茄鸡蛋面") >= 0
    assert restarted.context("鱼儿吃了吗", character_id="other", now=now).find("吃了番茄鸡蛋面") == -1
    assert event["status"] == "completed"


def test_due_plan_advances_once_and_wish_is_not_a_fact(life):
    _, repo = life
    now = datetime(2026, 11, 2, 5, tzinfo=UTC)
    repo.record("听音乐", status="planned", planned_end_at=now + timedelta(minutes=10), now=now)
    repo.record("想吃烤鱼", status="wish", now=now)
    assert repo.advance_due(now=now + timedelta(minutes=1))[0]["activity_status"] == "active"
    assert repo.advance_due(now=now + timedelta(minutes=11))[0]["activity_status"] == "completed"
    assert repo.advance_due(now=now + timedelta(minutes=12)) == []
    context = repo.context("今天吃什么", now=now + timedelta(minutes=12))
    assert "completed: 听音乐" in context
    assert "wish: 想吃烤鱼" in context
    assert "completed: 想吃烤鱼" not in context


def test_chat_gets_life_context_only_when_relevant(life):
    _, repo = life
    # ChatService queries the current local day, so the fixture event must be
    # on that day rather than an old hard-coded calendar date.
    now = datetime.now(UTC)
    repo.record("读了一会儿童话", now=now)
    db = life[0]
    conversations, messages = ConversationRepository(db), MessageRepository(db)
    conversation = conversations.create()

    class Model:
        async def reply(self, prompt):
            self.prompt = prompt
            return "读到一只小狐狸啦。"

    model = Model()
    chat = ChatService(conversations, messages, model, "persona", life_repository=repo)
    import asyncio
    asyncio.run(chat.send(conversation["id"], "你今天做什么啦？"))
    assert any("读了一会儿童话" in item["content"] for item in model.prompt if item["role"] == "system")


def test_life_context_without_experience_does_not_claim_events(life):
    _, repo = life
    context = repo.context("你今天吃了吗？")
    assert "没有保存的相关生活经历" in context


def test_autonomous_choice_is_persistent_and_idempotent(life):
    db, repo = life
    now = datetime(2026, 11, 2, 5, tzinfo=UTC)
    engine = LifeEngine(repo, seed=7)
    first = engine.advance("fish", now=now)
    assert len(first) == 1
    assert first[0]["activity_status"] == "active"
    assert first[0]["selection_reason"]
    assert engine.advance("fish", now=now) == []
    restarted = LifeEngine(LifeRepository(Database(db.path)), seed=7)
    assert restarted.repository.get_state("fish")["current_activity"]["id"] == first[0]["id"]
    assert restarted.advance("fish", now=now) == []
    assert restarted.repository.get_state("other")["current_activity"] is None


def test_explicit_plan_has_planned_active_completed_lifecycle(life):
    _, repo = life
    now = datetime(2026, 11, 2, 5, tzinfo=UTC)
    engine = LifeEngine(repo)
    planned = repo.record("准备读书", character_id="fish", status="planned", planned_start_at=now + timedelta(minutes=10), planned_end_at=now + timedelta(minutes=35), now=now)
    assert engine.advance("fish", now=now) == []
    assert len(repo.list_events("fish")) == 1
    active = engine.advance("fish", now=now + timedelta(minutes=10))[0]
    assert active["id"] == planned["id"]
    assert active["activity_status"] == "active"
    assert "active: 准备读书" in repo.context("现在干嘛", character_id="fish")
    assert engine.advance("fish", now=now + timedelta(minutes=20)) == []
    completed = engine.advance("fish", now=now + timedelta(minutes=36))[0]
    assert completed["activity_status"] == "completed"
    assert repo.get_state("fish")["current_activity"] is None
    assert "completed: 准备读书" in repo.context("今天做什么", character_id="fish")
    with repo.database.connection() as db:
        transitions = [row[0] for row in db.execute("SELECT to_status FROM life_activity_transitions WHERE event_id = ? ORDER BY changed_at", (planned["id"],))]
    assert transitions == ["planned", "active", "completed"]


def test_preferences_and_recent_activity_affect_choice(life):
    _, repo = life
    now = datetime(2026, 11, 2, 5, tzinfo=UTC)
    engine = LifeEngine(repo, seed=3)
    for kind in ACTIVITIES:
        engine.set_preference("fish", kind, 0.25, now=now)
    engine.set_preference("fish", "music", 1.5, now=now)
    assert engine.choose("fish", now=now)[0] == "music"
    repo.record("刚听完音乐", character_id="fish", event_type="music", now=now, feeling="开心")
    event = repo.list_events("fish")[0]
    engine.learn_from_completion(event, now)
    engine.learn_from_completion(event, now)
    assert engine.preferences("fish")["music"] == 1.53
    assert engine.choose("fish", now=now)[1].find("recent=['music']") >= 0
    repo.record("刚听完音乐", character_id="other", event_type="music", now=now)
    assert engine.choose("other", now=now)[0] != "music"
    with repo.database.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM life_preference_changes WHERE character_id = 'fish'").fetchone()[0] >= 1


def test_offline_gap_is_bounded_and_missed_plan_is_cancelled(life):
    _, repo = life
    now = datetime(2026, 11, 2, 5, tzinfo=UTC)
    engine = LifeEngine(repo, seed=4)
    repo.record("明天短暂散步", character_id="fish", status="planned", planned_start_at=now + timedelta(hours=1), planned_end_at=now + timedelta(hours=2), now=now)
    result = engine.advance("fish", now=now + timedelta(days=30))
    assert len(result) == 1 and result[0]["activity_status"] == "cancelled"
    assert len(repo.list_events("fish")) == 1
    engine.advance("fish", now=now + timedelta(days=30, seconds=1))
    assert len(repo.list_events("fish")) == 2
    assert engine.advance("fish", now=now + timedelta(days=30, seconds=1)) == []
