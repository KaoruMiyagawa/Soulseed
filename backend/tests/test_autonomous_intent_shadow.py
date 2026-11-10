"""D1 shadow scheduling is deterministic, bounded, and read-only."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.autonomous_intent_shadow import ShadowModeScheduler
from app.contact import ContactService
from app.config import Settings
from app.ownership import character_scope
from .test_autonomous_intent import ctx, conversation, discovery, dump


def test_shadow_tick_reports_contact_without_touching_business_state(ctx):
    db, life = ctx
    conversation(db, user='你好', reply='你好呀')
    run, _ = discovery(db, life)
    scheduler = ShadowModeScheduler(db, enabled=True)
    before = dump(db)

    outcomes = asyncio.run(scheduler.tick())

    assert len(outcomes) == 1
    result = outcomes[0]
    assert result.character_id == 'big_fish'
    assert result.decision == 'CONTACT'
    assert result.candidate_type == 'real_discovery'
    assert run['life_event_id'] in result.evidence_ids
    assert result.dedup_key and not result.failure_code
    assert scheduler.diagnostics.latest('big_fish') == (result,)
    assert dump(db) == before


def test_explicit_refusal_and_global_contact_switch_are_silent(ctx):
    db, life = ctx
    conversation(db, user='下次聊聊海獭，但别主动联系', reply='好呀，下次聊海獭')
    refused = asyncio.run(ShadowModeScheduler(db, enabled=True).tick())[0]
    assert refused.decision == 'SILENT'
    assert 'user_refusal' in refused.reason_codes

    conversation(db, user='你好', reply='你好呀')
    discovery(db, life)
    disabled_contact = asyncio.run(ShadowModeScheduler(db, enabled=True, contact_enabled=False).tick())[0]
    assert disabled_contact.decision == 'SILENT'
    assert disabled_contact.reason_codes == ('global_contact_disabled',)
    assert Settings().autonomous_intent_shadow_enabled is False


def test_duplicate_topic_and_expired_discovery_have_reason_codes(ctx):
    db, life = ctx
    _, anchor = conversation(db)
    ContactService(life).create_intent(source='conversation_followup', source_id=anchor, reason='already handled')
    duplicate = asyncio.run(ShadowModeScheduler(db, enabled=True).tick())[0]
    assert duplicate.decision == 'SILENT'
    assert 'candidate_duplicate' in duplicate.reason_codes

    conversation(db, user='你好', reply='你好呀')
    discovery(db, life)
    expired = asyncio.run(ShadowModeScheduler(db, enabled=True).tick(now=datetime.now(UTC) + timedelta(hours=25)))[0]
    assert expired.decision == 'SILENT'
    assert 'candidate_expired' in expired.reason_codes


def test_busy_candidate_is_deferred_and_quiet_empty_or_refused_is_silent(ctx):
    db, life = ctx
    conversation(db, user='你好', reply='你好呀')
    discovery(db, life)
    now = datetime.now(UTC)
    ContactService(life).set_preferences(busy_until=now + timedelta(hours=1))
    scheduler = ShadowModeScheduler(db, enabled=True)
    result = asyncio.run(scheduler.tick(now=now))[0]
    assert result.decision == 'DEFER'
    assert result.reason_codes == ('busy',)
    assert result.next_evaluation_at == (now + timedelta(hours=1)).isoformat()

    ContactService(life).set_preferences(busy_until=None)
    result = asyncio.run(scheduler.tick(now=now + timedelta(hours=25)))[0]
    # The research candidate has aged out by this evaluation time.
    assert result.decision == 'SILENT'
    assert 'no_eligible_candidate' in result.reason_codes


def test_multiple_shadow_runs_are_read_only_and_report_dedup_consistently(ctx):
    db, life = ctx
    conversation(db, user='你好', reply='你好呀')
    _, source = discovery(db, life)
    scheduler = ShadowModeScheduler(db, enabled=True)
    now = datetime.now(UTC) + timedelta(seconds=1)
    before = dump(db)
    first = asyncio.run(scheduler.tick(now=now))[0]
    second = asyncio.run(scheduler.tick(now=now))[0]
    assert first.decision == second.decision == 'CONTACT'
    assert first.dedup_key == second.dedup_key
    assert source['id'] in first.evidence_ids
    assert dump(db) == before


def test_roles_are_scoped_and_one_role_failure_does_not_stop_the_next(ctx, monkeypatch):
    db, life = ctx
    with db.connection() as sql:
        sql.execute("INSERT INTO characters(id,owner_user_id,ownership_status,created_at) VALUES('calm_one','local','owned',?)", (datetime.now(UTC).isoformat(),))
    with character_scope('calm_one'):
        life.ensure_state('calm_one')
        ContactService(life).set_preferences('calm_one', quiet_start_minute=0, quiet_end_minute=0)
    conversation(db, user='你好', reply='你好呀')
    discovery(db, life, 'big_fish')
    scheduler = ShadowModeScheduler(db, enabled=True)
    original = scheduler.evaluate_character

    def one_failure(character_id, *, now=None):
        if character_id == 'big_fish':
            raise RuntimeError('isolated test failure')
        return original(character_id, now=now)

    monkeypatch.setattr(scheduler, 'evaluate_character', one_failure)
    results = asyncio.run(scheduler.tick())
    assert [item.character_id for item in results] == ['big_fish', 'calm_one']
    # tick catches the per-character exception itself and proceeds to later roles.
    assert results[0].failure_code == 'RuntimeError'
    assert results[0].decision == 'SILENT'
    assert results[1].decision == 'SILENT'
    assert results[1].reason_codes == ('no_user_interaction', 'no_eligible_candidate')
    assert results[1].failure_code is None


def test_character_data_error_is_a_bounded_diagnostic_and_not_an_exception(ctx, monkeypatch):
    db, _ = ctx
    scheduler = ShadowModeScheduler(db, enabled=True)
    monkeypatch.setattr(scheduler, '_owned_characters', lambda: ('missing_role', 'big_fish'))
    results = asyncio.run(scheduler.tick())
    assert results[0].failure_code == 'ValueError'
    assert results[0].reason_codes == ('shadow_evaluation_failed',)
    assert results[1].character_id == 'big_fish'


def test_disabled_shadow_does_not_open_database_or_run_loop():
    class ForbiddenDatabase:
        def connection(self):
            raise AssertionError('disabled scheduler must not read')

    scheduler = ShadowModeScheduler(ForbiddenDatabase(), enabled=False)
    assert asyncio.run(scheduler.tick()) == ()

    async def run():
        await scheduler.run_forever(asyncio.Event())

    asyncio.run(run())


@pytest.mark.asyncio
async def test_scheduler_loop_triggers_without_manual_request_and_stops_cleanly(ctx):
    db, life = ctx
    conversation(db, user='你好', reply='你好呀')
    discovery(db, life)
    scheduler = ShadowModeScheduler(db, enabled=True, interval=timedelta(minutes=5))
    stop = asyncio.Event()
    task = asyncio.create_task(scheduler.run_forever(stop))
    for _ in range(100):
        if scheduler.diagnostics.latest('big_fish'):
            break
        await asyncio.sleep(0.001)
    stop.set()
    await asyncio.wait_for(task, timeout=1)
    assert scheduler.diagnostics.latest('big_fish')[0].decision == 'CONTACT'


@pytest.mark.asyncio
async def test_concurrent_ticks_serialize_and_contact_scheduler_remains_independent(ctx):
    db, life = ctx
    conversation(db, user='你好', reply='你好呀')
    discovery(db, life)
    scheduler = ShadowModeScheduler(db, enabled=True)
    before = dump(db)
    a, b = await asyncio.gather(scheduler.tick(), scheduler.tick())
    assert a[0].dedup_key == b[0].dedup_key
    assert dump(db) == before

    class ContactStub:
        enabled = True

        def __init__(self):
            self.calls = 0

        async def tick(self):
            self.calls += 1

    from app.contact_scheduler import ContactScheduler
    contact = ContactStub()
    stop = asyncio.Event()
    task = asyncio.create_task(ContactScheduler(contact, interval=timedelta(minutes=30)).run_forever(stop))
    await asyncio.sleep(0)
    stop.set()
    await asyncio.wait_for(task, timeout=1)
    assert contact.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('shadow_enabled', [False, True])
async def test_application_lifespan_wires_shadow_only_when_enabled(ctx, monkeypatch, shadow_enabled):
    import app.main as main

    db, _ = ctx
    monkeypatch.setattr(main, 'database', db)
    monkeypatch.setattr(main, 'exploration_repository', main.exploration_repository)
    monkeypatch.setattr(main, 'exploration_agent', main.exploration_agent)
    monkeypatch.setattr(main.life_repository, 'database', db)
    monkeypatch.setattr(main.exploration_repository, 'database', db)
    monkeypatch.setattr(main.exploration_agent, 'repository', main.exploration_repository)
    db.initialize()
    monkeypatch.setattr(main.settings, 'autonomous_intent_shadow_enabled', shadow_enabled)
    monkeypatch.setattr(main.settings, 'life_background_enabled', False)
    monkeypatch.setattr(main.settings, 'contact_background_enabled', False)
    monkeypatch.setattr(main.settings, 'contact_enabled', False)
    started = asyncio.Event()
    created = []

    class SchedulerStub:
        def __init__(self, *_args, **_kwargs):
            created.append(self)

        async def run_forever(self, stop):
            started.set()
            await stop.wait()

    monkeypatch.setattr(main, 'ShadowModeScheduler', SchedulerStub)
    async with main.lifespan(main.app):
        if shadow_enabled:
            await asyncio.wait_for(started.wait(), timeout=1)
        else:
            await asyncio.sleep(0)
    assert bool(created) is shadow_enabled
