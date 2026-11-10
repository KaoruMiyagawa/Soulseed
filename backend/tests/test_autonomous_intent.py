"""The planning kernel operates only on scoped, temporary test databases."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from app.autonomous_intent import DecisionKind, build_snapshot, dry_run, generate_candidates, interest_candidates
from app.contact import ContactService
from app.db import Database
from app.exploration_repository import ExplorationRepository
from app.life import LifeRepository
from app.memory.models import MemoryCreate
from app.memory.repositories import MemoryRepository
from app.ownership import character_scope
from app.repositories import ConversationRepository


@pytest.fixture
def ctx(tmp_path):
    db = Database(str(tmp_path / 'intent.db'))
    db.initialize()
    life = LifeRepository(db)
    life.ensure_state('big_fish')
    ContactService(life).set_preferences(quiet_start_minute=0, quiet_end_minute=0)
    return db, life


def message(db, conversation_id, role, content, at):
    message_id = str(uuid4())
    with db.connection() as sql:
        sql.execute('INSERT INTO messages(id,conversation_id,role,content,created_at) VALUES(?,?,?,?,?)',
                    (message_id, conversation_id, role, content, at.isoformat()))
    return message_id


def conversation(db, *, user='下次聊聊海獭', reply='好呀，下次一起聊海獭。', age=30):
    conv = ConversationRepository(db).create()
    at = datetime.now(UTC) - timedelta(hours=age)
    uid = message(db, conv['id'], 'user', user, at)
    if reply:
        message(db, conv['id'], 'assistant', reply, at + timedelta(minutes=1))
    return conv['id'], uid


def discovery(db, life, character='big_fish'):
    repo = ExplorationRepository(db, life)
    run, _ = repo.create(character_id=character, idempotency_key=str(uuid4()), requested_topic='海獭工具')
    repo.activate(run['id'], character)
    source = repo.add_evidence(run['id'], character, tool_name='read_webpage',
                               source_url='https://example.org/otters', title='海獭',
                               retrieved_at=datetime.now(UTC).isoformat(), excerpt='海獭使用石块。',
                               outcome='success', page_relevance='relevant')
    result = repo.complete(run['id'], character, summary='阅读资料后发现海獭会使用工具。', claims=[
        {'text': '海獭使用石块。', 'kind': 'sourced', 'source_ids': [source['id']]},
        {'text': '石块帮助海獭觅食。', 'kind': 'sourced', 'source_ids': [source['id']]},
    ], unanswered=[])
    return result, source


def snap(db, character='big_fish', now=None):
    return build_snapshot(db, character, now=now or datetime.now(UTC) + timedelta(seconds=1))


def dump(db):
    with db.connection() as sql:
        return '\n'.join(sql.iterdump())


def test_sourced_discovery_is_eligible_and_dedup_key_is_stable(ctx):
    db, life = ctx
    conversation(db, user='你好', reply='你好呀')
    run, source = discovery(db, life)
    first, second = snap(db), snap(db)
    candidate = generate_candidates(first)[0]
    assert candidate.kind == 'real_discovery'
    assert source['id'] in {x.id for x in candidate.evidence}
    assert run['life_event_id'] in {x.id for x in candidate.evidence}
    assert candidate.dedup_key == generate_candidates(second)[0].dedup_key
    result = dry_run(first)
    assert result.result == DecisionKind.CONTACT and result.basic_contact_allowed
    assert result.candidate_type == 'real_discovery'


def test_explicit_followup_with_real_reply_is_a_candidate(ctx):
    db, _ = ctx
    _, anchor = conversation(db)
    result = dry_run(snap(db))
    assert result.result == DecisionKind.CONTACT
    assert result.candidate_type == 'shared_topic' and anchor in result.evidence_ids
    assert '海獭' not in repr(result)


@pytest.mark.parametrize('user,reply', [
    ('我喜欢音乐', '我也喜欢音乐'), ('下次聊聊海獭', '好的'),
    ('下次聊聊海獭，但我不想聊海獭', '好呀，聊海獭'),
    ('下次聊聊海獭', '不想聊海獭'),
    ('下次聊聊海獭', '好吧，但我对海獭没兴趣'),
    ('下次聊聊海獭', '我不喜欢海獭，但好呀'),
    ('下次聊聊海獭', '我讨厌海獭，不过好呀'),
])
def test_weak_or_refused_topic_is_silent(ctx, user, reply):
    db, _ = ctx
    conversation(db, user=user, reply=reply)
    assert dry_run(snap(db)).result == DecisionKind.SILENT


def test_no_user_message_cannot_create_shared_topic(ctx):
    db, _ = ctx
    conv = ConversationRepository(db).create()
    message(db, conv['id'], 'assistant', '下次聊聊海獭', datetime.now(UTC) - timedelta(hours=25))
    assert not generate_candidates(snap(db))
    assert dry_run(snap(db)).result == DecisionKind.SILENT


def test_later_user_message_closes_old_topic_and_handled_intent_is_not_repeated(ctx):
    db, life = ctx
    conv, anchor = conversation(db)
    now = datetime.now(UTC)
    message(db, conv, 'user', '这个话题不用再聊了', now - timedelta(hours=3))
    assert not generate_candidates(snap(db))
    conv2, anchor2 = conversation(db)
    ContactService(life).create_intent(source='conversation_followup', source_id=anchor2, reason='saved', now=now)
    assert not generate_candidates(snap(db))
    assert anchor != anchor2


def test_same_topic_with_new_anchor_is_suppressed_after_handled(ctx):
    db, life = ctx
    _, anchor = conversation(db)
    ContactService(life).create_intent(source='conversation_followup', source_id=anchor, reason='saved')
    conversation(db)
    assert not generate_candidates(snap(db))


def test_expired_and_long_messages_are_not_candidates(ctx):
    db, _ = ctx
    conversation(db, age=73)
    assert not generate_candidates(snap(db))
    conversation(db, user='下次聊聊海獭' + '字' * 501, reply='好呀，聊海獭')
    result = dry_run(snap(db))
    assert result.result == DecisionKind.SILENT
    assert result.reasons == ('latest_user_message_truncated',)


def test_proactive_message_is_not_treated_as_conversation_reply(ctx):
    db, life = ctx
    conv, _ = conversation(db)
    with db.connection() as sql:
        reply = sql.execute("SELECT id FROM messages WHERE conversation_id=? AND role='assistant'", (conv,)).fetchone()['id']
    at = datetime.now(UTC) - timedelta(hours=25)
    intent = ContactService(life).create_intent(source='reminder', source_id='prior', reason='user_requested', topic='海獭', now=at)
    with db.connection() as sql:
        sql.execute("INSERT INTO proactive_messages(id,intent_id,attempt_no,content,status,created_at,delivered_at,conversation_id,chat_message_id) VALUES(?,?,0,'好呀，下次一起聊海獭','delivered',?,?,?,?)", (str(uuid4()), intent['id'], at.isoformat(), at.isoformat(), conv, reply))
    assert not generate_candidates(snap(db))


def test_simulated_wish_and_imagined_are_not_real_research(ctx):
    db, life = ctx
    conversation(db, user='你好', reply='你好')
    life.record('想研究海獭', status='wish')
    life.record('想象发现海獭', status='imagined')
    life.record('探索了海獭', event_type='exploring', generation_method='rule_engine')
    saved = life.record('手工保存的活动', event_type='reading', generation_method='manual')
    snapshot = snap(db)
    assert not snapshot.discoveries and not generate_candidates(snapshot)
    assert [item.evidence.id for item in snapshot.life_events] == [saved['id']]
    assert snapshot.life_events[0].trust == 'saved_not_independently_verified'
    assert interest_candidates(snapshot) == ()
    assert dry_run(snapshot).result == DecisionKind.SILENT


def test_missing_or_unrelated_source_is_not_a_discovery(ctx):
    db, life = ctx
    conversation(db, user='你好', reply='你好')
    run, _ = discovery(db, life)
    with db.connection() as sql:
        sql.execute("UPDATE exploration_evidence SET page_relevance='irrelevant' WHERE run_id=?", (run['id'],))
    assert not snap(db).discoveries
    assert dry_run(snap(db)).result == DecisionKind.SILENT


def test_private_source_and_expired_discovery_are_not_candidates(ctx):
    db, life = ctx
    conversation(db, user='你好', reply='你好')
    run, _ = discovery(db, life)
    assert not generate_candidates(snap(db, now=datetime.now(UTC) + timedelta(days=2)))
    with db.connection() as sql:
        sql.execute("UPDATE exploration_evidence SET source_url='http://127.0.0.1/private' WHERE run_id=?", (run['id'],))
    assert not generate_candidates(snap(db))


def test_processed_discovery_is_not_repeated(ctx):
    db, life = ctx
    conversation(db, user='你好', reply='你好')
    run, _ = discovery(db, life)
    with db.connection() as sql:
        sql.execute("UPDATE life_share_intents SET status='delivered' WHERE event_id=?", (run['life_event_id'],))
    assert not generate_candidates(snap(db))


def test_refusal_is_silent(ctx):
    db, life = ctx
    conversation(db)
    ContactService(life).set_preferences(allowed=False)
    decision = dry_run(snap(db))
    assert decision.result == DecisionKind.SILENT and decision.reasons == ('contact_not_allowed',)


@pytest.mark.parametrize('field', ['busy', 'pause', 'quiet'])
def test_time_boundaries_defer_without_writing(ctx, field):
    db, life = ctx
    conversation(db)
    now = datetime.now(UTC)
    service = ContactService(life)
    if field == 'busy':
        service.set_preferences(busy_until=now + timedelta(hours=1))
    elif field == 'pause':
        service.set_preferences(pause_until=now + timedelta(hours=1))
    else:
        local = now.astimezone(ZoneInfo('Asia/Shanghai'))
        minute = local.hour * 60 + local.minute
        service.set_preferences(quiet_start_minute=(minute - 1) % 1440, quiet_end_minute=(minute + 2) % 1440)
    result = dry_run(snap(db, now=now))
    assert result.result == DecisionKind.DEFER
    assert result.reasons == ({'busy': 'busy', 'pause': 'paused', 'quiet': 'quiet_hours'}[field],)
    assert not result.basic_contact_allowed


def test_recent_contact_cooldown_and_unanswered(ctx):
    db, life = ctx
    now = datetime.now(UTC)
    conversation(db, user='你好', reply='你好', age=3)
    discovery(db, life)
    intent = ContactService(life).create_intent(source='reminder', source_id='example', reason='user_requested', topic='提醒', now=now-timedelta(hours=6))
    with db.connection() as sql:
        sql.execute("INSERT INTO proactive_messages(id,intent_id,attempt_no,content,status,created_at,delivered_at) VALUES(?,?,0,'示例','delivered',?,?)",
                    (str(uuid4()), intent['id'], (now-timedelta(hours=6)).isoformat(), (now-timedelta(hours=6)).isoformat()))
    result = dry_run(snap(db, now=now+timedelta(seconds=1)))
    assert result.result == DecisionKind.DEFER and result.reasons == ('contact_cooldown',)
    with db.connection() as sql:
        sql.execute("UPDATE proactive_messages SET delivered_at=? WHERE intent_id=?", ((now-timedelta(hours=1)).isoformat(), intent['id']))
    assert dry_run(snap(db, now=now+timedelta(seconds=1))).reasons == ('awaiting_user_reply',)


def test_multicharacter_scope_and_memories_are_isolated(ctx):
    db, life = ctx
    with db.connection() as sql:
        sql.execute("INSERT INTO characters VALUES ('fish_b','local','owned',datetime('now'))")
    conversation(db)
    with character_scope('fish_b'):
        life.ensure_state('fish_b')
        ContactService(life).set_preferences('fish_b', quiet_start_minute=0, quiet_end_minute=0)
        conv, source = conversation(db, user='下次聊聊树蛙', reply='好呀，下次聊树蛙')
        memories = MemoryRepository(db)
        memory = memories.create(MemoryCreate('user_preference', '喜欢树蛙', '树蛙'))
        memories.add_source(memory.id, source, 'user', 'explicit_fact')
        run, _ = discovery(db, life, 'fish_b')
        b = snap(db, 'fish_b')
        assert memory.id in {x.evidence.id for x in b.memories}
        assert source in {x.source_message.id for x in b.memories}
        assert run['life_event_id'] in {x.event.id for x in b.discoveries}
        assert dry_run(b).result == DecisionKind.CONTACT
    a = snap(db)
    assert memory.id not in {x.evidence.id for x in a.memories}
    assert source not in {x.evidence.id for x in a.interactions}
    assert run['life_event_id'] not in {x.event.id for x in a.discoveries}
    with pytest.raises(ValueError, match='scope'):
        snap(db, 'fish_b')


def test_dry_run_is_read_only_and_never_uses_model(ctx, monkeypatch):
    from app.model_adapter import OpenAICompatibleAdapter
    db, _ = ctx
    conversation(db)
    def forbidden(*args, **kwargs):
        raise AssertionError('model must never be called')
    monkeypatch.setattr(OpenAICompatibleAdapter, 'reply', forbidden)
    before = dump(db)
    for _ in range(3):
        assert dry_run(snap(db)).result == DecisionKind.CONTACT
    assert dump(db) == before


def test_missing_preferences_are_unknown_and_do_not_initialize(ctx):
    db, life = ctx
    with db.connection() as sql:
        sql.execute("DELETE FROM contact_preferences WHERE character_id='big_fish'")
    before = dump(db)
    result = dry_run(snap(db))
    assert result.result == DecisionKind.SILENT and result.reasons == ('contact_preferences_unknown',)
    assert dump(db) == before
