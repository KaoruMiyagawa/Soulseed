import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.contact import ContactService
from app.contact import SafeContactGenerator
from app.db import Database
from app.life import LifeRepository
from app.life_share import LifeShareService
from app.repositories import ConversationRepository, MessageRepository


NOW = datetime(2026, 11, 2, 5, tzinfo=UTC)  # 13:00 in Asia/Shanghai


class TestContactGenerator:
    async def generate(self, intent, *, reminder=False):
        return f"这条发现我记下啦：{intent['topic']}"


@pytest.fixture
def context(tmp_path):
    db = Database(str(tmp_path / "contact.db"))
    db.initialize()
    repo = LifeRepository(db)
    conversation = ConversationRepository(db).create()
    return db, repo, ContactService(repo), conversation


def user_message(db, conversation_id, when=NOW - timedelta(hours=3), content="下次聊聊你读的书"):
    message_id = str(uuid4())
    with db.connection() as connection:
        connection.execute("INSERT INTO messages(id, conversation_id, role, content, created_at) VALUES (?, ?, 'user', ?, ?)", (message_id, conversation_id, content, when.isoformat()))
    return message_id


@pytest.mark.asyncio
async def test_saved_share_becomes_contact_intent_then_station_message(context):
    db, repo, service, conversation = context
    user_message(db, conversation["id"])
    service.generator = TestContactGenerator()
    event = repo.record("今天做了一份开心的晚餐", event_type="cooking", feeling="开心", now=NOW)
    share = LifeShareService(repo).consider(event["id"], now=NOW)
    ready = await service.tick(now=NOW)
    assert ready["status"] == "ready"
    intent = service.list_intents()[0]
    assert intent["source"] == "life_share"
    assert intent["event_id"] == event["id"]
    assert intent["share_intent_id"] == share["id"]
    assert service.inbox(now=NOW)[0]["id"] == ready["id"]
    assert await service.tick(now=NOW) is None
    delivered = service.deliver(ready["id"], conversation["id"], now=NOW)
    assert delivered["status"] == "delivered"
    assert service.deliver(ready["id"], conversation["id"], now=NOW)["id"] == ready["id"]
    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM messages WHERE id = ?", (ready["id"],)).fetchone()[0] == 1
    assert LifeShareService(repo).list_intents(status="delivered")[0]["id"] == share["id"]
    restarted = ContactService(LifeRepository(Database(db.path)))
    assert restarted.inbox(now=NOW) == []
    assert restarted.mark_read(ready["id"], now=NOW)["status"] == "read"
    assert restarted.mark_read(ready["id"], now=NOW)["status"] == "read"


@pytest.mark.asyncio
async def test_rule_engine_exploration_is_not_shared_as_a_real_discovery(context):
    db, repo, service, conversation = context
    user_message(db, conversation["id"])
    event = repo.record("去探索一个没注意过的角落", event_type="exploring",
                        generation_method="rule_engine", now=NOW)
    assert LifeShareService(repo).consider(event["id"], now=NOW) is None
    with db.connection() as connection:
        decision = connection.execute("SELECT outcome, reason FROM life_share_decisions WHERE event_id=?", (event["id"],)).fetchone()
        assert tuple(decision) == ("skipped", "simulated_activity_not_research")
        # Simulate a legacy pending intent already queued before this fix.
        share_id, contact_id = str(uuid4()), str(uuid4())
        connection.execute(
            "INSERT INTO life_share_intents(id,character_id,event_id,status,reason,created_at,delivery_channel) VALUES(?,?,?,'pending','interesting_discovery',?,'contact')",
            (share_id, "big_fish", event["id"], NOW.isoformat()),
        )
        connection.execute(
            "INSERT INTO contact_intents(ownership_verified,id,character_id,source,source_id,share_intent_id,event_id,importance,reason,topic,status,created_at,expires_at) VALUES(1,?,'big_fish','life_share',?,?,?,'normal','saved_life_experience',?,'pending',?,?)",
            (contact_id, share_id, share_id, event["id"], event["content"], NOW.isoformat(), (NOW + timedelta(days=2)).isoformat()),
        )
    ready = await service.tick(now=NOW)
    assert ready is None
    assert service.inbox(now=NOW) == []
    with db.connection() as connection:
        assert connection.execute("SELECT status FROM contact_intents WHERE id=?", (contact_id,)).fetchone()[0] == "pending"
        assert connection.execute("SELECT status FROM life_share_intents WHERE id=?", (share_id,)).fetchone()[0] == "pending"


@pytest.mark.asyncio
async def test_safe_fallback_does_not_send_a_fixed_exploration_share(context):
    db, repo, service, conversation = context
    user_message(db, conversation["id"])
    event = repo.record("探索主题：海獭。海獭会用石块敲开贝类。", event_type="research",
                        source="web_research", generation_method="exploration_agent", now=NOW)
    share = LifeShareService(repo).consider(event["id"], now=NOW)
    assert share is None
    with pytest.raises(ValueError, match="显式启用"):
        await SafeContactGenerator().generate({"source": "life_share", "topic": "海獭会用石块敲开贝类"})
    assert await service.tick(now=NOW) is None


@pytest.mark.asyncio
async def test_disabled_model_generator_keeps_a_real_research_share_pending(context):
    db, repo, _, conversation = context
    from app.exploration_repository import ExplorationRepository

    user_message(db, conversation["id"], when=NOW - timedelta(hours=3), content="你好")
    exploration = ExplorationRepository(db, repo)
    run, created = exploration.create(
        character_id="big_fish", idempotency_key="safe-share-run-001", requested_topic="海獭觅食",
    )
    assert created
    exploration.activate(run["id"], "big_fish")
    source = exploration.add_evidence(
        run["id"], "big_fish", tool_name="read_webpage", source_url="https://example.org/otters",
        title="海獭觅食", retrieved_at=NOW.isoformat(), excerpt="海獭会用石块敲开贝类外壳。",
        outcome="success", page_relevance="relevant",
    )
    result = exploration.complete(
        run["id"], "big_fish",
        summary="这份资料说明海獭会使用石块处理贝类，并展示了这种觅食行为的细节。这个发现有真实阅读来源支持，也可以继续追问相关过程。",
        claims=[
            {"text": "海獭会用石块敲开贝类外壳。", "kind": "sourced", "source_ids": [source["id"]]},
            {"text": "这种工具使用帮助海獭取得食物。", "kind": "sourced", "source_ids": [source["id"]]},
        ],
        unanswered=[],
    )
    assert result["status"] == "completed"
    share = LifeShareService(repo).list_intents(status="pending")[0]

    safe_contact = ContactService(repo, generator=SafeContactGenerator())
    assert await safe_contact.tick(now=NOW) is None
    pending = safe_contact.list_intents()
    assert len(pending) == 1 and pending[0]["status"] == "pending"
    assert pending[0]["share_intent_id"] == share["id"]
    assert safe_contact.inbox(now=NOW) == []
    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM proactive_messages").fetchone()[0] == 0
        assert connection.execute("SELECT status FROM life_share_intents WHERE id=?", (share["id"],)).fetchone()[0] == "pending"
    with pytest.raises(ValueError, match="显式启用"):
        await SafeContactGenerator().generate(pending[0])


@pytest.mark.asyncio
async def test_smalltalk_is_derived_once_from_recent_real_interaction(context):
    db, repo, service, conversation = context
    user_message(db, conversation["id"], NOW - timedelta(hours=25), "下次聊聊那本童话")
    ready = await service.tick(now=NOW)
    assert ready and service.list_intents()[0]["source"] == "conversation_followup"
    assert "那本童话" in ready["content"]
    assert await service.tick(now=NOW) is None
    assert len(service.list_intents()) == 1
    assert ContactService(repo, enabled=False).inbox(now=NOW) == []


@pytest.mark.asyncio
async def test_quiet_busy_and_refusal_suppress_contact(context):
    db, _, service, conversation = context
    user_message(db, conversation["id"])
    service.create_intent(source="reminder", source_id="one", reason="user_requested", topic="看那本书", importance="important", now=NOW)
    assert await service.tick(now=NOW + timedelta(hours=10)) is None  # 23:00 local
    service.set_preferences(busy_until=NOW + timedelta(hours=4), now=NOW)
    assert await service.tick(now=NOW) is None
    service.observe_user_message("不要主动联系我", now=NOW)
    assert service.preferences()["allowed"] == 0
    assert service.list_intents()[0]["status"] == "dismissed"
    assert await service.tick(now=NOW + timedelta(days=1)) is None
    assert service.inbox(now=NOW + timedelta(days=1)) == []


@pytest.mark.asyncio
async def test_unanswered_normal_contact_does_not_nag(context):
    db, _, service, conversation = context
    user_id = user_message(db, conversation["id"], content="你好")
    service.create_intent(source="smalltalk", source_id=user_id, reason="real_topic", now=NOW)
    ready = await service.tick(now=NOW)
    service.deliver(ready["id"], conversation["id"], now=NOW)
    service.create_intent(source="conversation_followup", source_id=user_id, reason="real_topic", now=NOW + timedelta(hours=25))
    assert await service.tick(now=NOW + timedelta(hours=25)) is None
    assert len(service.list_intents()) == 2


@pytest.mark.asyncio
async def test_important_intent_can_remind_only_once(context):
    db, _, service, conversation = context
    user_message(db, conversation["id"])
    service.create_intent(source="reminder", source_id="important", reason="user_requested", topic="那件约好的事", importance="important", now=NOW)
    first = await service.tick(now=NOW)
    service.deliver(first["id"], conversation["id"], now=NOW)
    second = await service.tick(now=NOW + timedelta(hours=25))
    assert second and second["attempt_no"] == 1
    assert "等你方便" in second["content"]
    service.deliver(second["id"], conversation["id"], now=NOW + timedelta(hours=25))
    assert await service.tick(now=NOW + timedelta(hours=50)) is None
    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM proactive_messages").fetchone()[0] == 2


@pytest.mark.asyncio
async def test_generation_failure_keeps_intent_without_message(context):
    db, repo, _, conversation = context
    user_message(db, conversation["id"])

    class FailingGenerator:
        async def generate(self, intent, *, reminder=False):
            raise RuntimeError("private model error")

    service = ContactService(repo, generator=FailingGenerator())
    intent = service.create_intent(source="reminder", source_id="failure", reason="user_requested", topic="那本书", now=NOW)
    assert await service.tick(now=NOW) is None
    assert service.list_intents()[0]["status"] == "deferred"
    assert service.inbox(now=NOW) == []
    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM proactive_messages").fetchone()[0] == 0
        assert connection.execute("SELECT retry_after FROM contact_intents WHERE id = ?", (intent["id"],)).fetchone()[0]


@pytest.mark.asyncio
async def test_concurrent_ticks_claim_only_one_generation(context):
    db, repo, _, conversation = context
    user_id = user_message(db, conversation["id"])
    class SlowGenerator:
        async def generate(self, intent, *, reminder=False):
            await asyncio.sleep(0.01)
            return "之前聊到童话，等你有空再说。"

    service = ContactService(repo, generator=SlowGenerator())
    service.create_intent(source="smalltalk", source_id=user_id, reason="real_topic", now=NOW)

    async def run():
        return await service.tick(now=NOW)

    results = await asyncio.gather(*(run() for _ in range(8)))
    assert sum(item is not None for item in results) == 1
    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM proactive_messages").fetchone()[0] == 1


def test_share_contact_requires_real_completed_event(context):
    _, repo, service, _ = context
    with pytest.raises(ValueError):
        service.create_intent(source="life_share", source_id="imaginary", reason="share", now=NOW)
    wish = repo.record("想吃烤鱼", status="wish", now=NOW)
    assert LifeShareService(repo).consider(wish["id"], now=NOW) is None
    with pytest.raises(ValueError):
        service.create_intent(source="smalltalk", source_id="fictional-user-message", reason="fake", topic="不存在的经历", now=NOW)


@pytest.mark.asyncio
async def test_disabled_contact_and_expired_chat_do_not_generate(context):
    db, repo, service, conversation = context
    user_message(db, conversation["id"], when=NOW - timedelta(days=5))
    assert await ContactService(repo, enabled=False).tick(now=NOW) is None
    assert await service.tick(now=NOW) is None
    assert service.list_intents() == []


@pytest.mark.asyncio
async def test_refusal_dismisses_ready_card_before_delivery(context):
    db, _, service, conversation = context
    user_message(db, conversation["id"])
    service.create_intent(source="reminder", source_id="refusal", reason="user_requested", topic="那本书", now=NOW)
    ready = await service.tick(now=NOW)
    service.observe_user_message("别主动联系我", now=NOW)
    assert service.inbox(now=NOW) == []
    with pytest.raises(ValueError):
        service.deliver(ready["id"], conversation["id"], now=NOW)
    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM messages WHERE id = ?", (ready["id"],)).fetchone()[0] == 0


def test_in_app_api_acknowledges_display_then_read(context, monkeypatch):
    from .conftest import PairedTestClient as TestClient
    import app.main as main_module

    db, repo, service, conversation = context
    service.set_preferences(quiet_start_minute=0, quiet_end_minute=0)
    service.generator = TestContactGenerator()
    user_message(db, conversation["id"], when=datetime.now(UTC) - timedelta(hours=3))
    current = datetime.now(UTC)
    event = repo.record("今天做了一份开心的晚餐", event_type="cooking", feeling="开心", now=current)
    LifeShareService(repo).consider(event["id"], now=current)
    ready = asyncio.run(service.tick(now=current))
    assert ready is not None
    monkeypatch.setattr(main_module, "database", db)
    monkeypatch.setattr(main_module, "life_repository", repo)
    monkeypatch.setattr(main_module, "conversations", ConversationRepository(db))
    monkeypatch.setattr(main_module, "messages", MessageRepository(db))
    with TestClient(main_module.app) as client:
        assert client.post("/api/characters/big_fish/contact/check").status_code == 200
        inbox = client.get("/api/characters/big_fish/contact/inbox")
        assert inbox.status_code == 200 and inbox.json()[0]["id"] == ready["id"]
        delivered = client.post(f"/api/characters/big_fish/contact/messages/{ready['id']}/delivered", json={"conversation_id": conversation["id"]})
        assert delivered.status_code == 200 and delivered.json()["status"] == "delivered"
        assert client.get("/api/characters/big_fish/contact/inbox").json() == []
        assert client.get(f"/api/conversations/{conversation['id']}").json()["messages"][-1]["id"] == ready["id"]
        assert client.post(f"/api/characters/big_fish/contact/messages/{ready['id']}/read").json()["status"] == "read"
