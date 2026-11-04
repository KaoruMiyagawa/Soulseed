from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.chat_service import ChatService
from app.contact import ContactService
from app.db import Database
from app.life import LifeRepository
from app.life_engine import LifeEngine
from app.life_instance import LifeInstance
from app.life_instance import InstanceConflict
from app.life_share import LifeShareService
from app.repositories import ConversationRepository, MessageRepository


T0 = datetime(2026, 11, 2, 5, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    db = Database(str(tmp_path / "f3.db"))
    db.initialize()
    return db, LifeRepository(db)


def test_interrupted_event_has_effective_status_diagnostics_and_never_learns_or_shares(store, monkeypatch):
    db, repo = store
    owner = LifeInstance(db, repo)
    owner.acquire(now=T0)
    event = LifeEngine(repo).advance("big_fish", now=T0)[0]
    with db.connection() as sql:
        sql.execute("UPDATE life_events SET feeling='开心' WHERE id=?", (event["id"],))
    owner.release(now=T0 + timedelta(minutes=1))

    interrupted = repo.list_events("big_fish")[0]
    assert interrupted["activity_status"] == "active"
    assert interrupted["execution_status"] == "interrupted"
    assert interrupted["effective_status"] == "interrupted"
    assert interrupted["interruption_reason"] == "service_stopped"
    assert interrupted["interrupted_at"] == (T0 + timedelta(minutes=1)).isoformat()
    assert repo.get_state("big_fish")["current_activity"] is None
    context = repo.context("鱼儿你现在在干嘛？", character_id="big_fish", now=T0 + timedelta(minutes=2))
    assert "interrupted" in context and "正在进行：" not in context

    engine = LifeEngine(repo)
    engine.learn_from_completion(interrupted, T0 + timedelta(minutes=2))
    assert LifeShareService(repo).consider(event["id"], now=T0 + timedelta(minutes=2)) is None
    with db.connection() as sql:
        assert sql.execute("SELECT COUNT(*) FROM life_preference_changes WHERE event_id=?", (event["id"],)).fetchone()[0] == 0
        assert sql.execute("SELECT COUNT(*) FROM life_share_decisions WHERE event_id=?", (event["id"],)).fetchone()[0] == 0

    later = engine.advance("big_fish", now=T0 + timedelta(days=2))
    assert len(later) == 1 and later[0]["id"] != event["id"] and later[0]["effective_status"] == "active"

    import app.main as main
    from .conftest import PairedTestClient
    monkeypatch.setattr(main, "database", db)
    monkeypatch.setattr(main, "life_repository", repo)
    with PairedTestClient(main.app) as client:
        response = client.get("/api/characters/big_fish/life")
        assert response.status_code == 200
        old = next(item for item in response.json()["events"] if item["id"] == event["id"])
        assert old["effective_status"] == "interrupted"
        assert old["interruption_reason"] == "service_stopped"
        assert old["interrupted_at"]


def test_life_api_reports_live_activity_as_effective_active(store, monkeypatch):
    db, repo = store
    import app.main as main
    from .conftest import PairedTestClient
    monkeypatch.setattr(main, "database", db)
    monkeypatch.setattr(main, "life_repository", repo)
    monkeypatch.setattr(main.settings, "life_background_enabled", False)
    monkeypatch.setattr(main.settings, "contact_enabled", False)
    with PairedTestClient(main.app) as client:
        response = client.get("/api/characters/big_fish/life")
        assert response.status_code == 200
        body = response.json()
        assert body["state"]["current_activity"]["effective_status"] == "active"
        assert next(event for event in body["events"] if event["id"] == body["state"]["current_activity_event_id"])["effective_status"] == "active"
    assert repo.list_events("big_fish")[0]["effective_status"] == "interrupted"


def test_instance_lock_normalizes_equivalent_database_paths(store, tmp_path):
    db, repo = store
    owner = LifeInstance(db, repo)
    owner.acquire(now=T0)
    alternate_path = str(tmp_path / "unused" / ".." / "f3.db")
    other_db = Database(alternate_path)
    with pytest.raises(InstanceConflict):
        LifeInstance(other_db, LifeRepository(other_db)).acquire(now=T0)
    owner.release(now=T0 + timedelta(minutes=1))


def test_busy_pause_restore_long_refusal_and_history_guard(store):
    _, repo = store
    service = ContactService(repo)
    service.observe_user_message("我现在有点忙", now=T0)
    busy = service.preferences(now=T0)
    assert busy["busy_until"] == (T0 + timedelta(hours=3)).isoformat()
    assert busy["allowed"] == 1 and busy["pause_until"] is None
    assert not service._allowed_now("big_fish", T0)

    service.set_preferences("big_fish", busy_until=None, now=T0 + timedelta(minutes=1))
    assert service.preferences("big_fish")["busy_until"] is None

    service.observe_user_message("我现在有点忙，先别找我", character_id="big_fish", now=T0)
    paused = service.preferences("big_fish", now=T0)
    assert paused["allowed"] == 1 and paused["busy_until"] is None
    assert paused["pause_until"] == (T0 + timedelta(hours=24)).isoformat()
    service.observe_user_message("上周忙着准备考试", character_id="big_fish", now=T0 + timedelta(minutes=2))
    assert service.preferences("big_fish")["pause_until"] == paused["pause_until"]
    service.observe_user_message("现在有空了，可以来找我啦", character_id="big_fish", now=T0 + timedelta(minutes=3))
    restored = service.preferences("big_fish", now=T0 + timedelta(minutes=3))
    assert restored["allowed"] == 1 and restored["busy_until"] is None and restored["pause_until"] is None

    service.observe_user_message("不要主动联系我", character_id="big_fish", now=T0)
    service.observe_user_message("你好呀", character_id="big_fish", now=T0 + timedelta(minutes=1))
    assert service.preferences("big_fish")["allowed"] == 0
    assert not service._allowed_now("big_fish", T0 + timedelta(days=3))
    service.observe_user_message("恢复正常联系吧", character_id="big_fish", now=T0 + timedelta(days=3))
    assert service.preferences("big_fish")["allowed"] == 1


def test_contact_preferences_api_distinguishes_omitted_and_explicit_null(store, monkeypatch):
    db, repo = store
    service = ContactService(repo)
    service.set_preferences("big_fish", busy_until=T0 + timedelta(hours=3), pause_until=T0 + timedelta(days=1), now=T0)
    import app.main as main
    from .conftest import PairedTestClient
    monkeypatch.setattr(main, "database", db)
    monkeypatch.setattr(main, "life_repository", repo)
    with PairedTestClient(main.app) as client:
        url = "/api/characters/big_fish/contact/preferences"
        assert client.post(url, json={}).status_code == 200
        unchanged = client.get(url).json()
        assert unchanged["busy_until"] and unchanged["pause_until"]
        cleared = client.post(url, json={"busy_until": None, "pause_until": None})
        assert cleared.status_code == 200
        assert cleared.json()["busy_until"] is None and cleared.json()["pause_until"] is None


def test_chat_share_generation_failure_releases_selection_and_success_does_not_fake_delivery(store):
    db, repo = store
    event = repo.record("发现一片会发光的虚拟海草", character_id="big_fish", event_type="exploring", now=datetime.now(UTC))
    share = LifeShareService(repo).consider(event["id"])
    conversations, messages = ConversationRepository(db), MessageRepository(db)
    conversation = conversations.create()

    class Model:
        def __init__(self, fail):
            self.fail = fail
            self.prompts = []

        async def reply(self, prompt):
            self.prompts.append(prompt)
            if self.fail:
                raise RuntimeError("injected model failure")
            return "你好呀，今天感觉不错。"

    failed_model = Model(True)
    failed_chat = ChatService(conversations, messages, failed_model, "persona", life_repository=repo)
    with pytest.raises(RuntimeError, match="injected"):
        asyncio.run(failed_chat.send(conversation["id"], "鱼儿你在干嘛？"))
    with db.connection() as sql:
        failed_state = dict(sql.execute("SELECT status, offered_at, delivery_channel, selection_token FROM life_share_intents WHERE id=?", (share["id"],)).fetchone())
    assert failed_state == {"status": "pending", "offered_at": None, "delivery_channel": None, "selection_token": None}

    model = Model(False)
    chat = ChatService(conversations, messages, model, "persona", life_repository=repo)
    asyncio.run(chat.send(conversation["id"], "鱼儿你在干嘛？"))
    assert any('"source":"pending_life_share"' in item["content"] for item in model.prompts[-1] if item["role"] == "system")
    with db.connection() as sql:
        selected = dict(sql.execute("SELECT status, offered_at, delivery_channel, selection_token FROM life_share_intents WHERE id=?", (share["id"],)).fetchone())
    assert selected == {"status": "pending", "offered_at": None, "delivery_channel": "chat", "selection_token": None}
    assert ContactService(repo).sync_shares("fish") == []
    assert LifeShareService(repo).list_intents("fish", status="delivered") == []


def test_chat_and_contact_claims_are_exclusive_and_retry_after_cooldown(store):
    db, repo = store
    event = repo.record("看到一艘很奇怪的小船", character_id="big_fish", event_type="exploring", now=datetime.now(UTC))
    share = LifeShareService(repo).consider(event["id"])
    shares = LifeShareService(repo)
    selected = shares.reserve_for_chat("你今天做什么啦？", character_id="big_fish")
    assert selected
    with pytest.raises(ValueError):
        ContactService(repo).create_intent(character_id="big_fish", source="life_share", source_id=share["id"], reason="share")
    assert shares.release_chat_reservation(selected["token"])
    contact = ContactService(repo)
    intent = contact.create_intent(character_id="big_fish", source="life_share", source_id=share["id"], reason="share")
    assert intent["share_intent_id"] == share["id"]
    assert shares.reserve_for_chat("你今天做什么啦？", character_id="big_fish") is None


def test_chat_and_contact_race_assigns_share_to_only_one_channel(store):
    db, repo = store
    event = repo.record("发现一张有趣的地图", character_id="big_fish", event_type="exploring", now=datetime.now(UTC))
    share = LifeShareService(repo).consider(event["id"])
    shares, contacts = LifeShareService(repo), ContactService(repo)

    def chat_claim():
        return shares.reserve_for_chat("你今天做什么啦？", character_id="big_fish")

    def contact_claim():
        try:
            return contacts.create_intent(character_id="big_fish", source="life_share", source_id=share["id"], reason="share")
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda fn: fn(), (chat_claim, contact_claim)))
    with db.connection() as sql:
        state = dict(sql.execute("SELECT delivery_channel, selection_token, status FROM life_share_intents WHERE id=?", (share["id"],)).fetchone())
        contact_count = sql.execute("SELECT COUNT(*) FROM contact_intents WHERE share_intent_id=?", (share["id"],)).fetchone()[0]
    assert state["status"] == "pending"
    if state["selection_token"]:
        assert state["delivery_channel"] is None and contact_count == 0
    else:
        assert state["delivery_channel"] == "contact" and state["selection_token"] is None and contact_count == 1


def test_delivered_contact_retry_is_idempotent_after_preferences_change(store):
    db, repo = store
    conversation = ConversationRepository(db).create()
    messages = MessageRepository(db)
    source = messages.add(conversation["id"], "user", "你好")
    service = ContactService(repo)
    intent = service.create_intent(source="smalltalk", source_id=source["id"], reason="topic", topic="童话", now=T0)
    with db.connection() as sql:
        sql.execute("UPDATE contact_intents SET status='ready' WHERE id=?", (intent["id"],))
        sql.execute("INSERT INTO proactive_messages(id,intent_id,attempt_no,content,status,created_at) VALUES ('same-message',?,0,'你好呀','ready',?)", (intent["id"], T0.isoformat()))
    delivered = service.deliver("same-message", conversation["id"], now=T0)
    service.observe_user_message("不要主动联系我", now=T0 + timedelta(minutes=1))
    again = service.deliver("same-message", conversation["id"], now=T0 + timedelta(minutes=2))
    assert delivered["status"] == again["status"] == "delivered"
    with db.connection() as sql:
        assert sql.execute("SELECT COUNT(*) FROM messages WHERE id='same-message'").fetchone()[0] == 1


def test_two_concurrent_delivery_acks_insert_one_message(store):
    db, repo = store
    conversation = ConversationRepository(db).create()
    source = MessageRepository(db).add(conversation["id"], "user", "你好")
    service = ContactService(repo)
    intent = service.create_intent(source="smalltalk", source_id=source["id"], reason="topic", topic="童话", now=T0)
    with db.connection() as sql:
        sql.execute("UPDATE contact_intents SET status='ready' WHERE id=?", (intent["id"],))
        sql.execute("INSERT INTO proactive_messages(id,intent_id,attempt_no,content,status,created_at) VALUES ('dedupe-message',?,0,'你好呀','ready',?)", (intent["id"], T0.isoformat()))

    def acknowledge(_):
        return ContactService(LifeRepository(Database(db.path))).deliver("dedupe-message", conversation["id"], now=T0)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(acknowledge, range(2)))
    assert [row["status"] for row in results] == ["delivered", "delivered"]
    with db.connection() as sql:
        assert sql.execute("SELECT COUNT(*) FROM messages WHERE id='dedupe-message'").fetchone()[0] == 1


def test_migration_14_failure_rolls_back_and_retry_preserves_schema(tmp_path, monkeypatch):
    import sqlite3
    import app.db as db_module

    db = Database(str(tmp_path / "migration14.db"))
    with monkeypatch.context() as patch:
        patch.setattr(db_module, "MIGRATIONS", {version: sql for version, sql in db_module.MIGRATIONS.items() if version <= 13})
        db.initialize()
    with monkeypatch.context() as patch:
        patch.setitem(db_module.MIGRATIONS, 14, db_module.MIGRATIONS[14] + "\nSELECT * FROM injected_missing_table;")
        with pytest.raises(sqlite3.OperationalError):
            db.initialize()
    with db.connection() as sql:
        assert sql.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 13
        assert "pause_until" not in {row[1] for row in sql.execute("PRAGMA table_info(contact_preferences)")}
        assert "delivery_channel" not in {row[1] for row in sql.execute("PRAGMA table_info(life_share_intents)")}
    db.initialize()
    with db.connection() as sql:
        assert sql.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 14
