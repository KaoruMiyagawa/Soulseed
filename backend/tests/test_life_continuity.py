from concurrent.futures import ThreadPoolExecutor
import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.chat_service import ChatService
from app.db import Database
from app.life import LifeRepository
from app.life_engine import LifeEngine
from app.life_instance import LifeInstance
from app.life_scheduler import LifeScheduler
from app.life_share import LifeShareService
from app.repositories import ConversationRepository, MessageRepository


@pytest.fixture
def services(tmp_path):
    db = Database(str(tmp_path / "continuity.db"))
    db.initialize()
    repo = LifeRepository(db)
    return db, repo, LifeEngine(repo, seed=7), LifeShareService(repo)


def test_background_switch_interval_and_restart_tick(services):
    db, repo, _, _ = services
    now = datetime(2026, 11, 2, 5, tzinfo=UTC)
    disabled = LifeScheduler(repo, enabled=False)
    assert disabled.tick(now=now) == []
    assert repo.list_events() == []
    scheduler = LifeScheduler(repo, interval=timedelta(minutes=30), seed=7)
    first = scheduler.tick(now=now)
    assert len(first) == 1 and first[0]["activity_status"] == "active"
    assert scheduler.tick(now=now + timedelta(minutes=10)) == []
    assert scheduler.tick(now=now + timedelta(minutes=30))[0]["activity_status"] == "completed"
    assert LifeScheduler(LifeRepository(Database(db.path))).tick(now=now + timedelta(minutes=31)) == []
    assert len(repo.list_events()) == 1


@pytest.mark.asyncio
async def test_background_loop_advances_without_chat(services):
    _, repo, _, _ = services
    stop = asyncio.Event()
    task = asyncio.create_task(LifeScheduler(repo).run_forever(stop))
    await asyncio.sleep(0)
    stop.set()
    await task
    assert len(repo.list_events()) == 1


def test_concurrent_advance_creates_one_activity(services):
    _, repo, engine, _ = services
    now = datetime(2026, 11, 2, 5, tzinfo=UTC)
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(lambda _: engine.advance("fish", now=now), range(8)))
    assert sum(len(outcome) for outcome in outcomes) == 1
    assert len(repo.list_events("fish")) == 1
    with repo.database.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM life_activity_transitions WHERE character_id = 'fish'").fetchone()[0] == 2


def test_restart_interrupts_active_without_fabricating_offline_completion(services):
    db, repo, engine, _ = services
    now = datetime(2026, 11, 2, 5, tzinfo=UTC)
    first = engine.advance("fish", now=now)[0]
    restarted_db = Database(db.path)
    restarted = LifeRepository(restarted_db)
    instance = LifeInstance(restarted_db, restarted)
    instance.acquire(now=now + timedelta(days=20))
    try:
        interrupted = restarted.list_events("fish")[0]
        assert interrupted["id"] == first["id"]
        assert interrupted["activity_status"] == "active"
        assert interrupted["execution_status"] == "interrupted"
        assert interrupted["occurred_at"] is None
        assert restarted.get_state("fish")["current_activity"] is None
        assert len(LifeEngine(restarted).advance("fish", now=now + timedelta(days=20))) <= 1
        assert len(restarted.list_events("fish")) <= 2
    finally:
        instance.release(now=now + timedelta(days=20))


def test_iana_timezone_and_dst_change_local_clock(services):
    _, repo, engine, _ = services
    repo.set_timezone("ny", "America/New_York")
    repo.set_timezone("sh", "Asia/Shanghai")
    before = datetime(2026, 3, 8, 6, 59, tzinfo=UTC)
    after = before + timedelta(minutes=2)
    assert "2026-03-08T01:59:00-05:00" in engine.choose("ny", now=before)[1]
    assert "2026-03-08T03:01:00-04:00" in engine.choose("ny", now=after)[1]
    assert "2026-03-08T15:01:00+08:00" in engine.choose("sh", now=after)[1]
    repo.record("发现一只小鱼", character_id="ny", event_type="exploring", now=datetime(2026, 3, 8, 4, tzinfo=UTC))
    assert "角色当地日期：2026-03-07" in repo.context("今天做什么", character_id="ny", now=datetime(2026, 3, 8, 4, 30, tzinfo=UTC))
    assert "角色当地日期：2026-03-08" in repo.context("今天做什么", character_id="ny", now=after)
    with pytest.raises(Exception):
        repo.set_timezone("ny", "Mars/Olympus")


def test_share_intent_is_bound_unique_rate_limited_and_lifecycle(services):
    db, repo, engine, shares = services
    now = datetime(2026, 11, 2, 5, tzinfo=UTC)
    first = repo.record("发现一片会发光的虚拟海草", character_id="fish", event_type="exploring", now=now)
    intent = shares.consider(first["id"], now=now)
    assert intent["event_id"] == first["id"]
    assert intent["event_content"] == first["content"]
    assert shares.consider(first["id"], now=now)["id"] == intent["id"]
    second = repo.record("发现了奇怪的贝壳", character_id="fish", event_type="exploring", now=now + timedelta(minutes=5))
    assert shares.consider(second["id"], now=now + timedelta(minutes=5)) is None
    assert len(shares.list_intents("fish")) == 1
    delivered = shares.transition(intent["id"], "delivered", character_id="fish", now=now + timedelta(minutes=6))
    assert delivered["status"] == "delivered"
    assert shares.transition(intent["id"], "delivered", character_id="fish")["status"] == "delivered"
    with pytest.raises(ValueError):
        shares.transition(intent["id"], "dismissed", character_id="fish")
    third = repo.record("发现新地图", character_id="fish", event_type="exploring", now=now + timedelta(hours=7))
    later = shares.consider(third["id"], now=now + timedelta(hours=7))
    assert shares.transition(later["id"], "dismissed", character_id="fish")["status"] == "dismissed"
    assert LifeShareService(LifeRepository(Database(db.path))).list_intents("fish", status="delivered")[0]["id"] == intent["id"]


def test_ordinary_or_unfinished_event_never_creates_share(services):
    _, repo, _, shares = services
    now = datetime(2026, 11, 2, 5, tzinfo=UTC)
    ordinary = repo.record("整理房间", event_type="tidying", now=now)
    planned = repo.record("想去探索", event_type="exploring", status="planned", now=now)
    wish = repo.record("想吃烤鱼", event_type="cooking", status="wish", now=now)
    assert shares.consider(ordinary["id"], now=now) is None
    assert shares.consider(planned["id"], now=now) is None
    assert shares.consider(wish["id"], now=now) is None
    assert shares.list_intents() == []


def test_concurrent_preference_learning_applies_once(services):
    _, repo, engine, _ = services
    now = datetime(2026, 11, 2, 5, tzinfo=UTC)
    event = repo.record("听完喜欢的音乐", character_id="fish", event_type="music", feeling="开心", now=now)
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _: engine.learn_from_completion(event, now), range(6)))
    assert engine.preferences("fish")["music"] == 1.43
    with repo.database.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM life_preference_changes WHERE event_id = ?", (event["id"],)).fetchone()[0] == 1


@pytest.mark.asyncio
async def test_chat_offers_share_on_life_topic_but_not_emotional_message(services):
    db, repo, _, shares = services
    now = datetime.now(UTC)
    event = repo.record("发现一片会发光的虚拟海草", event_type="exploring", now=now)
    shares.consider(event["id"], now=now)
    conversations, messages = ConversationRepository(db), MessageRepository(db)
    conversation = conversations.create()

    class FakeModel:
        def __init__(self):
            self.prompts = []

        async def reply(self, prompt):
            self.prompts.append(prompt)
            return "我在这儿呢。"

    model = FakeModel()
    chat = ChatService(conversations, messages, model, "persona", life_repository=repo)
    await chat.send(conversation["id"], "鱼儿，在干嘛？")
    assert any('"source":"pending_life_share"' in item["content"] for item in model.prompts[-1] if item["role"] == "system")
    await chat.send(conversation["id"], "我今天好累，想你了")
    assert not any("life_share_intent" in item["content"] or "life_context" in item["content"] or "<expression_context" in item["content"] for item in model.prompts[-1] if item["role"] == "system")
    await chat.send(conversation["id"], "你今天做什么啦？")
    assert not any("life_share_intent" in item["content"] for item in model.prompts[-1] if item["role"] == "system")
    assert shares.list_intents(status="pending")[0]["status"] == "pending"
