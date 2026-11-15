import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

from app.contact import ContactService
from app.db import MIGRATIONS
from app.desktop_contact import DesktopContact
from app.desktop_runtime import DesktopRuntime, ModelInput, RuntimeLimitsInput
from app.life import LifeRepository
from app.message_delivery import MessageDeliveryRepository
from app.model_adapter import OpenAICompatibleAdapter
from app.ownership import character_scope
from app.repositories import ConversationRepository
from app.repositories import MessageRepository
from .test_autonomous_intent import discovery


def setup_desktop(tmp_path, monkeypatch, *, outcome="contact", before_transport=None, with_discovery=True):
    runtime = DesktopRuntime(tmp_path, "a" * 64)
    runtime.database.initialize()
    runtime.save(ModelInput(base_url="http://127.0.0.1:12345/v1", model="local-qa", api_key="qa-only"))
    runtime.save_runtime_limits(RuntimeLimitsInput(daily_limit=3, autonomous_paused=False, contact_enabled=True))
    life = LifeRepository(runtime.database)
    conversation = ConversationRepository(runtime.database).create()
    ContactService(life).set_preferences(quiet_start_minute=0, quiet_end_minute=0)
    if with_discovery:
        discovery(runtime.database, life)
    attempts = []

    def respond(request):
        payload = json.loads(request.content)
        signals = json.loads(payload["messages"][-1]["content"])
        with runtime.database.connection() as db:
            assert db.execute("SELECT status FROM model_budget_requests").fetchone()[0] == "started"
            assert db.execute("SELECT request_status FROM autonomous_contact_reservations").fetchone()[0] == "started"
        attempts.append(signals)
        if outcome == "timeout":
            raise httpx.ReadTimeout("test transport timeout", request=request)
        option = signals["choices"][0]
        texts = {"contact": "海獭使用石块，真有意思！", "subjective": "哇，好有趣呀！",
                 "friendly": "我有点想你，想和你聊聊天。", "greeting": "嗨，最近过得怎么样？想聊聊天吗？",
                 "natural": "今天想给你丢一颗小小的快乐泡泡，希望它不会在半路上啪的一声破掉！",
                 "secret": "这里有个 qa-only，千万别忘了。",
                 "fake_url": "看到了https://fake.invalid/abc！", "fake_user": "你昨天答应陪我看海。",
                 "fake_life": "我昨天亲自去了海边。", "fake_fact": "海獭使用石块，海獭会飞。",
                 "fake_preference": "你喜欢吃榴莲。", "fake_past": "我上周爬了山。",
                 "mixed": "真有意思！海獭住在火星。"}
        if outcome in texts:
            plan = {"decision": "contact", "choice": option["id"], "style": "curious",
                    "message": texts[outcome], "evidence_ids": option["evidence_ids"],
                    "claims": [{"text": option["text"], "evidence_ids": option["evidence_ids"]}]}
        else:
            plan = {"decision": "silent"} if outcome == "silent" else {"decision": "invalid"}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(plan)}, "finish_reason": "stop"}]})

    client = httpx.AsyncClient
    def create_client(**kwargs):
        if before_transport:
            before_transport(runtime)
        return client(**{**kwargs, "transport": httpx.MockTransport(respond)})

    monkeypatch.setattr(httpx, "AsyncClient", create_client)
    model = OpenAICompatibleAdapter(runtime.model_settings(), strict_budget=True, budget_service=runtime.budget)
    return runtime, life, model, conversation, attempts


def test_discovery_without_user_message_concurrent_handoff_and_restart(tmp_path, monkeypatch):
    with character_scope("big_fish"):
        runtime, life, model, conversation, attempts = setup_desktop(tmp_path, monkeypatch)
        pipeline = DesktopContact(runtime, life, model)
        results = asyncio.run(run_concurrently(pipeline))
        assert results.count("delivered") == 1
        assert len(attempts) == 1
        feed = MessageDeliveryRepository(runtime.database).feed(
            user_id="local", character_id="big_fish", conversation_id=conversation["id"], after_sequence=0, limit=10,
        )
        [message] = feed["messages"]
        assert message["role"] == "assistant" and message["content"] == "海獭使用石块，真有意思！"
        assert pipeline.consume_notification(foreground=False) == message["id"]
        runtime.database.initialize()
        restarted = DesktopContact(runtime, life, model)
        assert restarted.consume_notification(foreground=False) is None
        assert asyncio.run(restarted.tick()) != "delivered"
        assert len(attempts) == 1
        with runtime.database.connection() as db:
            assert db.execute("SELECT status FROM contact_intents").fetchone()[0] == "delivered"
            assert db.execute("SELECT status FROM desktop_generated_reviews").fetchone()[0] == "APPROVED"


async def run_concurrently(pipeline):
    return await asyncio.gather(pipeline.tick(), pipeline.tick())


@pytest.mark.parametrize('outcome,expected', [('friendly', 'delivered'), ('silent', 'silent')])
def test_self_social_without_any_user_message_or_discovery(tmp_path, monkeypatch, outcome, expected):
    with character_scope('big_fish'):
        runtime, life, model, conversation, attempts = setup_desktop(tmp_path, monkeypatch, outcome=outcome, with_discovery=False)
        with runtime.database.connection() as db:
            db.execute("UPDATE life_states SET mood='好奇' WHERE character_id='big_fish'")
        pipeline = DesktopContact(runtime, life, model)
        assert asyncio.run(pipeline.tick()) == expected
        assert attempts[0]['candidate_type'] == 'self_social'
        with runtime.database.connection() as db:
            assert db.execute('SELECT candidate_type FROM autonomous_contact_candidates').fetchone()[0] == 'self_social'
            assert db.execute('SELECT count(*) FROM messages WHERE role=\'user\'').fetchone()[0] == 0
        if outcome == 'friendly':
            feed = MessageDeliveryRepository(runtime.database).feed(user_id='local', character_id='big_fish', conversation_id=conversation['id'], after_sequence=0, limit=10)
            assert feed['messages'][0]['content'] == '我有点想你，想和你聊聊天。'
            assert pipeline.consume_notification(foreground=False)
        assert asyncio.run(DesktopContact(runtime, life, model).tick()) != 'delivered'
        assert len(attempts) == 1


def test_self_social_respects_pause_and_quiet_hours(tmp_path, monkeypatch):
    with character_scope('big_fish'):
        runtime, life, model, _, attempts = setup_desktop(tmp_path, monkeypatch, with_discovery=False)
        with runtime.database.connection() as db:
            db.execute("UPDATE life_states SET mood='好奇' WHERE character_id='big_fish'")
        pipeline = DesktopContact(runtime, life, model)
        runtime.save_runtime_limits(RuntimeLimitsInput(daily_limit=3, autonomous_paused=True, contact_enabled=True))
        assert asyncio.run(pipeline.tick()) == 'paused'
        runtime.save_runtime_limits(RuntimeLimitsInput(daily_limit=3, autonomous_paused=False, contact_enabled=True))
        local = datetime.now(life.timezone('big_fish'))
        minute = local.hour * 60 + local.minute
        pipeline.contact.set_preferences(quiet_start_minute=minute, quiet_end_minute=(minute + 10) % 1440)
        assert asyncio.run(pipeline.tick()) == 'quiet_hours'
        assert not attempts


def test_self_social_can_use_interest_without_saved_mood(tmp_path, monkeypatch):
    with character_scope('big_fish'):
        runtime, life, model, _, attempts = setup_desktop(tmp_path, monkeypatch, outcome='friendly', with_discovery=False)
        with runtime.database.connection() as db:
            db.execute("UPDATE life_states SET mood=NULL WHERE character_id='big_fish'")
            db.execute("INSERT INTO life_preferences(character_id,event_type,weight,updated_at) VALUES('big_fish','reading',1.0,?)", (datetime.now(UTC).isoformat(),))
        assert asyncio.run(DesktopContact(runtime, life, model).tick()) == 'delivered'
        assert attempts[0]['interests'][0]['type'] == 'reading'


def test_self_social_does_not_require_recent_interaction(tmp_path, monkeypatch):
    with character_scope('big_fish'):
        runtime, life, model, conversation, attempts = setup_desktop(tmp_path, monkeypatch, outcome='friendly', with_discovery=False)
        message = MessageRepository(runtime.database).add(conversation['id'], 'user', '很久以前的一句问候')
        with runtime.database.connection() as db:
            db.execute("UPDATE messages SET created_at=? WHERE id=?", ((datetime.now(UTC) - timedelta(days=100)).isoformat(), message['id']))
            db.execute("UPDATE life_states SET mood='好奇' WHERE character_id='big_fish'")
        assert asyncio.run(DesktopContact(runtime, life, model).tick()) == 'delivered'
        assert attempts[0]['candidate_type'] == 'self_social'


def test_web_uses_shared_contact_flow_with_existing_legacy_budget(tmp_path, monkeypatch):
    from app.autonomous_social import AutonomousContact
    from app.main import WebContactRuntime
    real_client = httpx.AsyncClient
    with character_scope('big_fish'):
        runtime, life, _, conversation, _ = setup_desktop(tmp_path, monkeypatch, with_discovery=False)
        with runtime.database.connection() as db:
            db.execute("UPDATE life_states SET mood='好奇' WHERE character_id='big_fish'")
        def respond(request):
            signals = json.loads(json.loads(request.content)['messages'][-1]['content'])
            assert signals['candidate_type'] == 'self_social'
            plan = {'decision': 'contact', 'choice': signals['choices'][0]['id'], 'style': 'curious',
                    'message': '我有点想你，想和你聊聊天。', 'evidence_ids': [], 'claims': []}
            return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(plan)}, 'finish_reason': 'stop'}]})
        settings = runtime.model_settings().model_copy(update={'contact_enabled': True})
        model = OpenAICompatibleAdapter(settings, client_factory=lambda **kw: real_client(transport=httpx.MockTransport(respond), **kw))
        core = AutonomousContact(WebContactRuntime(runtime.database, settings, model), life, model)
        assert asyncio.run(core.tick()) == 'delivered'
        feed = MessageDeliveryRepository(runtime.database).feed(user_id='local', character_id='big_fish', conversation_id=conversation['id'], after_sequence=0, limit=10)
        assert feed['messages'][0]['content'] == '我有点想你，想和你聊聊天。'
        with runtime.database.connection() as db:
            assert db.execute('SELECT request_status,message_status FROM autonomous_contact_reservations').fetchone()[:] == ('settled', 'committed')
            assert not db.execute('SELECT 1 FROM desktop_contact_notifications').fetchone()


@pytest.mark.parametrize("text,kind", [("最近在听音乐", "smalltalk"), ("改天聊音乐", "conversation_followup")])
def test_old_smalltalk_end_to_end(tmp_path, monkeypatch, text, kind):
    with character_scope("big_fish"):
        runtime, life, model, conversation, attempts = setup_desktop(tmp_path, monkeypatch, outcome="friendly", with_discovery=False)
        message = MessageRepository(runtime.database).add(conversation["id"], "user", text)
        with runtime.database.connection() as db:
            db.execute("UPDATE messages SET created_at=? WHERE id=?", ((datetime.now(UTC)-timedelta(hours=25)).isoformat(), message["id"]))
        pipeline = DesktopContact(runtime, life, model)
        assert asyncio.run(pipeline.tick()) == "delivered"
        with runtime.database.connection() as db:
            assert db.execute("SELECT candidate_type,status FROM autonomous_contact_candidates").fetchone()[:] == (kind, "completed")
            assert db.execute("SELECT status FROM desktop_generated_reviews").fetchone()[0] == "APPROVED"
            assert db.execute("SELECT status FROM model_budget_requests").fetchone()[0] == "completed"
        feed = MessageDeliveryRepository(runtime.database).feed(user_id="local", character_id="big_fish", conversation_id=conversation["id"], after_sequence=0, limit=10)
        assert any(item["content"] == "我有点想你，想和你聊聊天。" for item in feed["messages"])
        assert pipeline.consume_notification(foreground=False)
        runtime.database.initialize()
        assert asyncio.run(DesktopContact(runtime, life, model).tick()) != "delivered"
        assert not pipeline.consume_notification(foreground=False)
        assert len(attempts) == 1


def test_smalltalk_plain_greeting_is_delivered(tmp_path, monkeypatch):
    with character_scope("big_fish"):
        runtime, life, model, conversation, _ = setup_desktop(tmp_path, monkeypatch, outcome="greeting", with_discovery=False)
        message = MessageRepository(runtime.database).add(conversation["id"], "user", "最近在听音乐")
        with runtime.database.connection() as db:
            db.execute("UPDATE messages SET created_at=? WHERE id=?", ((datetime.now(UTC)-timedelta(hours=25)).isoformat(), message["id"]))
        assert asyncio.run(DesktopContact(runtime, life, model).tick()) == "delivered"


@pytest.mark.parametrize("outcome,expected", [("natural", "delivered"), ("secret", "fact_review_rejected")])
def test_natural_social_and_credential_protection(tmp_path, monkeypatch, outcome, expected):
    with character_scope("big_fish"):
        runtime, life, model, conversation, _ = setup_desktop(tmp_path, monkeypatch, outcome=outcome, with_discovery=False)
        message = MessageRepository(runtime.database).add(conversation["id"], "user", "最近在听音乐")
        with runtime.database.connection() as db:
            db.execute("UPDATE messages SET created_at=? WHERE id=?", ((datetime.now(UTC)-timedelta(hours=25)).isoformat(), message["id"]))
        assert asyncio.run(DesktopContact(runtime, life, model).tick()) == expected


@pytest.mark.parametrize("outcome,expected", [("silent", "silent"), ("fake_life", "fact_review_required")])
def test_smalltalk_silent_and_unverified_experience(tmp_path, monkeypatch, outcome, expected):
    with character_scope("big_fish"):
        runtime, life, model, conversation, attempts = setup_desktop(tmp_path, monkeypatch, outcome=outcome, with_discovery=False)
        message = MessageRepository(runtime.database).add(conversation["id"], "user", "最近在听音乐")
        with runtime.database.connection() as db:
            db.execute("UPDATE messages SET created_at=? WHERE id=?", ((datetime.now(UTC)-timedelta(hours=25)).isoformat(), message["id"]))
        pipeline = DesktopContact(runtime, life, model)
        runtime.save_runtime_limits(RuntimeLimitsInput(daily_limit=3, autonomous_paused=True, contact_enabled=True))
        assert asyncio.run(pipeline.tick()) == "paused"
        assert not attempts
        runtime.save_runtime_limits(RuntimeLimitsInput(daily_limit=3, autonomous_paused=False, contact_enabled=True))
        assert asyncio.run(pipeline.tick()) == expected
        assert asyncio.run(DesktopContact(runtime, life, model).tick()) == "cancelled"
        assert len(attempts) == 1
        with runtime.database.connection() as db:
            assert not db.execute("SELECT 1 FROM proactive_messages").fetchone()


def test_candidate_migration_preserves_existing_reservations(tmp_path, monkeypatch):
    with character_scope("big_fish"):
        with monkeypatch.context() as old:
            old.delitem(MIGRATIONS, 27)
            old.delitem(MIGRATIONS, 28)
            runtime, life, model, _, _ = setup_desktop(tmp_path, monkeypatch)
            pipeline = DesktopContact(runtime, life, model)
            from app.autonomous_intent import build_snapshot, generate_candidates
            candidate = generate_candidates(build_snapshot(runtime.database, "big_fish"))[0]
            pipeline.state.persist_candidate(candidate)
            assert pipeline.state.claim(candidate.dedup_key, "big_fish").token
        runtime.database.initialize()
        with runtime.database.connection() as db:
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []
            assert db.execute("SELECT status FROM autonomous_contact_candidates").fetchone()[0] == "claimed"
            assert db.execute("SELECT candidate_id FROM autonomous_contact_reservations").fetchone()[0] == candidate.dedup_key


def test_self_social_migration_preserves_contact_rows_and_foreign_keys(tmp_path, monkeypatch):
    with character_scope('big_fish'):
        with monkeypatch.context() as old:
            old.delitem(MIGRATIONS, 28)
            runtime, life, model, conversation, _ = setup_desktop(tmp_path, monkeypatch, with_discovery=False)
            message = MessageRepository(runtime.database).add(conversation['id'], 'user', '最近在听音乐')
            intent = ContactService(life).create_intent(source='smalltalk', source_id=message['id'], reason='saved')
            with runtime.database.connection() as db:
                db.execute("INSERT INTO proactive_messages(id,intent_id,attempt_no,content,status,created_at) VALUES(?,?,0,'你好','ready',?)",
                           (str(uuid4()), intent['id'], datetime.now(UTC).isoformat()))
        runtime.database.initialize()
        with runtime.database.connection() as db:
            assert db.execute('PRAGMA foreign_key_check').fetchall() == []
            assert db.execute('SELECT source FROM contact_intents').fetchone()[0] == 'smalltalk'
            assert db.execute('SELECT intent_id FROM proactive_messages').fetchone()[0] == intent['id']


def test_smalltalk_candidate_uses_saved_message_and_legacy_rules(tmp_path, monkeypatch):
    with character_scope("big_fish"):
        runtime, life, model, conversation, _ = setup_desktop(tmp_path, monkeypatch)
        message = MessageRepository(runtime.database).add(conversation["id"], "user", "最近在听音乐")
        old_time = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
        with runtime.database.connection() as db:
            db.execute("UPDATE messages SET created_at=? WHERE id=?", (old_time, message["id"]))
        pipeline = DesktopContact(runtime, life, model)
        candidate = pipeline.smalltalk_candidate("big_fish")
        assert candidate.kind == "smalltalk"
        assert candidate.evidence[0].id == message["id"]
        assert pipeline.smalltalk_candidate("big_fish").dedup_key == candidate.dedup_key
        assert pipeline.smalltalk_candidate("another-character") is None
        with runtime.database.connection() as db:
            db.execute("UPDATE messages SET content='你好' WHERE id=?", (message["id"],))
        assert pipeline.smalltalk_candidate("big_fish") is None


def test_pause_quiet_and_exhausted_budget_never_reach_transport(tmp_path, monkeypatch):
    with character_scope("big_fish"):
        runtime, life, model, _, attempts = setup_desktop(tmp_path, monkeypatch)
        pipeline = DesktopContact(runtime, life, model)
        runtime.save_runtime_limits(RuntimeLimitsInput(daily_limit=1, autonomous_paused=True))
        assert asyncio.run(pipeline.tick()) == "paused"
        runtime.save_runtime_limits(RuntimeLimitsInput(daily_limit=1, autonomous_paused=False))
        local = datetime.now(life.timezone("big_fish"))
        minute = local.hour * 60 + local.minute
        pipeline.contact.set_preferences(quiet_start_minute=minute, quiet_end_minute=(minute + 10) % 1440)
        assert asyncio.run(pipeline.tick()) == "quiet_hours"
        pipeline.contact.set_preferences(quiet_start_minute=0, quiet_end_minute=0)
        used = uuid4().hex
        runtime.budget.reserve(used, "big_fish", "chat")
        runtime.budget.mark_started(used, "big_fish")
        runtime.budget.complete(used, "big_fish")
        assert asyncio.run(pipeline.tick()) == "global_budget_exhausted"
        assert not attempts


def test_unknown_model_outcome_is_charged_and_never_retried(tmp_path, monkeypatch):
    with character_scope("big_fish"):
        runtime, life, model, _, attempts = setup_desktop(tmp_path, monkeypatch, outcome="timeout")
        pipeline = DesktopContact(runtime, life, model)
        assert asyncio.run(pipeline.tick()) == "model_failed"
        pipeline.state.recover_expired_claims("big_fish", now=datetime.now(UTC) + timedelta(minutes=3))
        assert asyncio.run(DesktopContact(runtime, life, model).tick()) == "outcome_unknown"
        assert len(attempts) == 1
        with runtime.database.connection() as db:
            assert db.execute("SELECT status FROM model_budget_requests").fetchone()[0] == "unknown"
            assert not db.execute("SELECT 1 FROM proactive_messages").fetchone()


def test_pause_between_reservation_and_transport_releases_both_budgets(tmp_path, monkeypatch):
    def pause(runtime):
        runtime.save_runtime_limits(RuntimeLimitsInput(daily_limit=3, autonomous_paused=True))

    with character_scope("big_fish"):
        runtime, life, model, _, attempts = setup_desktop(tmp_path, monkeypatch, before_transport=pause)
        assert asyncio.run(DesktopContact(runtime, life, model).tick()) == "autonomous_paused"
        assert not attempts
        with runtime.database.connection() as db:
            assert db.execute("SELECT status FROM model_budget_requests").fetchone()[0] == "released"
            assert db.execute("SELECT request_status,message_status FROM autonomous_contact_reservations").fetchone()[:] == ("released", "released")


def test_model_can_decline_a_valid_discovery(tmp_path, monkeypatch):
    with character_scope("big_fish"):
        runtime, life, model, _, attempts = setup_desktop(tmp_path, monkeypatch, outcome="silent")
        pipeline = DesktopContact(runtime, life, model)
        assert asyncio.run(pipeline.tick()) == "silent"
        assert asyncio.run(pipeline.tick()) == "cancelled"
        assert len(attempts) == 1
        with runtime.database.connection() as db:
            assert not db.execute("SELECT 1 FROM proactive_messages").fetchone()


def test_invalid_model_output_is_not_reported_as_a_silent_decision(tmp_path, monkeypatch):
    with character_scope("big_fish"):
        runtime, life, model, _, attempts = setup_desktop(tmp_path, monkeypatch, outcome="invalid")
        assert asyncio.run(DesktopContact(runtime, life, model).tick()) == "invalid_model_plan"
        assert len(attempts) == 1
        with runtime.database.connection() as db:
            assert db.execute("SELECT reason_code FROM autonomous_contact_candidates").fetchone()[0] == "invalid_model_plan"
            assert not db.execute("SELECT 1 FROM proactive_messages").fetchone()


def test_subjective_message_is_delivered_unchanged(tmp_path, monkeypatch):
    with character_scope("big_fish"):
        runtime, life, model, _, attempts = setup_desktop(tmp_path, monkeypatch, outcome="subjective")
        assert asyncio.run(DesktopContact(runtime, life, model).tick()) == "delivered"
        assert len(attempts) == 1
        with runtime.database.connection() as db:
            assert db.execute("SELECT content FROM proactive_messages").fetchone()[0] == "哇，好有趣呀！"


def test_friendly_generated_message_is_delivered_unchanged(tmp_path, monkeypatch):
    with character_scope("big_fish"):
        runtime, life, model, conversation, attempts = setup_desktop(tmp_path, monkeypatch, outcome="friendly")
        pipeline = DesktopContact(runtime, life, model)
        assert asyncio.run(pipeline.tick()) == "delivered"
        feed = MessageDeliveryRepository(runtime.database).feed(
            user_id="local", character_id="big_fish", conversation_id=conversation["id"], after_sequence=0, limit=10,
        )
        assert feed["messages"][0]["content"] == "我有点想你，想和你聊聊天。"
        assert len(attempts) == 1


@pytest.mark.parametrize("outcome", ["fake_url", "fake_user", "fake_life", "fake_fact", "fake_preference", "fake_past", "mixed"])
def test_unsupported_generated_claims_never_deliver(tmp_path, monkeypatch, outcome):
    with character_scope("big_fish"):
        runtime, life, model, _, _ = setup_desktop(tmp_path, monkeypatch, outcome=outcome)
        assert asyncio.run(DesktopContact(runtime, life, model).tick()) in ("fact_review_required", "fact_review_rejected")
        with runtime.database.connection() as db:
            assert db.execute("SELECT status FROM desktop_generated_reviews").fetchone()[0] in ("REVIEW_REQUIRED", "REJECTED")
            assert not db.execute("SELECT 1 FROM proactive_messages").fetchone()


@pytest.mark.parametrize("excerpt", ["海獭生活在海洋中。", "海獭使用石块。这个说法是假的。"])
def test_cited_source_must_contain_the_claim(tmp_path, monkeypatch, excerpt):
    with character_scope("big_fish"):
        runtime, life, model, _, attempts = setup_desktop(tmp_path, monkeypatch)
        with runtime.database.connection() as db:
            db.execute("UPDATE exploration_evidence SET excerpt=? WHERE tool_name='read_webpage'", (excerpt,))
        assert asyncio.run(DesktopContact(runtime, life, model).tick()) == "fact_review_required"
        assert len(attempts) == 1
        with runtime.database.connection() as db:
            assert not db.execute("SELECT 1 FROM proactive_messages").fetchone()
