"""D3-A integration: temporary SQLite, fixed clock, and mock-only drafting."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from zoneinfo import ZoneInfo

import pytest

from app.autonomous_contact_pipeline import AutonomousContactPipeline, MockGeneralBudgetGate
from app.autonomous_intent import build_snapshot, dry_run, generate_candidates
from app.autonomous_intent_state import AutonomousIntentStateRepository
from app.autonomous_personas import resolve_trusted_persona
from app.contact import ContactService
from app.db import Database
from app.life import LifeRepository
from app.proactive_expression import (DraftPlan, DraftService, MockDraftGenerator,
                                      MockEvidenceReviewer, PersonaRegistry, build_draft_context)
from app.ownership import character_scope
from .test_autonomous_intent import conversation, discovery, dump


@pytest.fixture
def ctx(tmp_path):
    db = Database(str(tmp_path / 'd3a.db'))
    db.initialize()
    life = LifeRepository(db)
    life.ensure_state('big_fish')
    ContactService(life).set_preferences(quiet_start_minute=0, quiet_end_minute=0)
    return db, life


def make_pipeline(db, life, *, clock, candidate=None, available=True, fail_hook=None,
                  model_limit=2):
    now = clock()
    snapshot = build_snapshot(db, 'big_fish', now=now)
    candidates = generate_candidates(snapshot)
    candidate = candidate or candidates[0]
    decision = dry_run(snapshot)
    assert decision.result.value == 'CONTACT' and decision.dedup_key == candidate.dedup_key
    persona = resolve_trusted_persona(db, 'big_fish')
    context = build_draft_context(db, 'big_fish', candidate, decision, persona, now=now)
    option = context.options[0]
    reviewer = MockEvidenceReviewer(frozenset({option.fingerprint('big_fish')}))
    generator = MockDraftGenerator(DraftPlan(persona.allowed_styles[0], option.key))
    drafts = DraftService(db, generator, reviewer, PersonaRegistry((persona,)), clock=clock)
    state = AutonomousIntentStateRepository(db, enabled=True, handoff_enabled=True,
                                            daily_model_request_limit=model_limit)
    contact = ContactService(life, enabled=True, clock=clock)
    gate = MockGeneralBudgetGate(available=available)
    pipeline = AutonomousContactPipeline(state, contact, drafts, gate, clock=clock, failure_hook=fail_hook)
    return pipeline, candidate, decision, generator, state, contact, gate


def frozen_clock():
    return lambda: datetime.now(UTC) + timedelta(seconds=3)


def setup_discovery(ctx, *, clock=frozen_clock()):
    db, life = ctx
    with character_scope('big_fish'):
        ContactService(life).set_preferences(allowed=True, busy_until=None, pause_until=None,
                                             quiet_start_minute=0, quiet_end_minute=0, now=clock())
        conv, _ = conversation(db, user='你好', reply='你好呀', age=30)
        discovery(db, life)
        return db, life, conv


def test_real_discovery_mock_pipeline_hands_off_to_contact_and_uses_existing_delivery_read(ctx):
    db, life, conv = setup_discovery(ctx)
    clock = frozen_clock()
    with character_scope('big_fish'):
        pipeline, candidate, _, generator, state, contact, _ = make_pipeline(db, life, clock=clock)
        result = pipeline.run(candidate, dry_run(build_snapshot(db, 'big_fish', now=clock())))
        assert result.status == 'handed_off'
        assert generator.calls == 1
        assert contact.inbox('big_fish', now=clock())
        delivered = contact.deliver(result.proactive_message_id, conv, character_id='big_fish', now=clock())
        assert delivered['status'] == 'delivered'
        read = contact.mark_read(result.proactive_message_id, character_id='big_fish', now=clock())
        assert read['status'] == 'read'
        with db.connection() as sql:
            assert sql.execute('SELECT status FROM autonomous_contact_candidates WHERE id=?', (candidate.dedup_key,)).fetchone()[0] == 'completed'
            assert sql.execute('SELECT request_status,message_status FROM autonomous_contact_reservations WHERE candidate_id=?', (candidate.dedup_key,)).fetchone()[:] == ('settled', 'committed')
            assert sql.execute('SELECT review_status FROM autonomous_contact_drafts WHERE candidate_id=?', (candidate.dedup_key,)).fetchone()[0] == 'consumed'


def test_real_user_message_anchor_maps_to_conversation_followup(ctx):
    db, life = ctx
    with character_scope('big_fish'):
        conv, anchor = conversation(db, age=30)
        clock = frozen_clock()
        pipeline, candidate, decision, _, _, contact, _ = make_pipeline(db, life, clock=clock)
        result = pipeline.run(candidate, decision)
        assert result.status == 'handed_off'
        intent = next(row for row in contact.list_intents('big_fish') if row['id'] == result.contact_intent_id)
        assert (intent['source'], intent['source_id']) == ('conversation_followup', anchor)
        assert contact.inbox('big_fish', now=clock())[0]['content']


def test_contactservice_rechecks_permission_when_delivering_ready_message(ctx):
    db, life, conv = setup_discovery(ctx)
    clock = frozen_clock()
    with character_scope('big_fish'):
        pipeline, candidate, decision, _, _, contact, _ = make_pipeline(db, life, clock=clock)
        result = pipeline.run(candidate, decision)
        assert result.status == 'handed_off'
        contact.set_preferences('big_fish', allowed=False, now=clock())
        assert contact.inbox('big_fish', now=clock()) == []
        with pytest.raises(ValueError, match='当前不适合投递'):
            contact.deliver(result.proactive_message_id, conv, character_id='big_fish', now=clock())


def test_concurrent_runs_create_only_one_contact_intent_and_message(ctx):
    db, life, _ = setup_discovery(ctx)
    clock = frozen_clock()
    with character_scope('big_fish'):
        pipeline, candidate, decision, generator, _, _, _ = make_pipeline(db, life, clock=clock)
    barrier = Barrier(2)

    def run_once():
        with character_scope('big_fish'):
            barrier.wait()
            return pipeline.run(candidate, decision)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run_once(), range(2)))
    assert sum(item.status == 'handed_off' for item in results) == 1
    assert generator.calls == 1
    with db.connection() as sql:
        assert sql.execute('SELECT COUNT(*) FROM contact_intents WHERE character_id=?', ('big_fish',)).fetchone()[0] == 1
        assert sql.execute('SELECT COUNT(*) FROM proactive_messages').fetchone()[0] == 1
        assert sql.execute('SELECT COUNT(*) FROM autonomous_contact_reservations WHERE candidate_id=?', (candidate.dedup_key,)).fetchone()[0] == 1


def test_handoff_transaction_rolls_back_intent_message_candidate_and_budgets(ctx):
    db, life, _ = setup_discovery(ctx)
    clock = frozen_clock()

    def fail(stage):
        if stage == 'after_contact_intent':
            raise RuntimeError('injected handoff failure')

    with character_scope('big_fish'):
        pipeline, candidate, decision, _, _, _, _ = make_pipeline(db, life, clock=clock, fail_hook=fail)
        with pytest.raises(RuntimeError, match='injected handoff failure'):
            pipeline.run(candidate, decision)
        with db.connection() as sql:
            assert sql.execute('SELECT COUNT(*) FROM contact_intents').fetchone()[0] == 0
            assert sql.execute('SELECT COUNT(*) FROM proactive_messages').fetchone()[0] == 0
            assert sql.execute('SELECT status FROM autonomous_contact_candidates WHERE id=?', (candidate.dedup_key,)).fetchone()[0] == 'claimed'
            assert sql.execute('SELECT message_status FROM autonomous_contact_reservations WHERE candidate_id=?', (candidate.dedup_key,)).fetchone()[0] == 'reserved'
            assert sql.execute('SELECT review_status FROM autonomous_contact_drafts WHERE candidate_id=?', (candidate.dedup_key,)).fetchone()[0] == 'mock_approved'


def test_revoking_contact_after_generation_cancels_draft_without_handoff(ctx):
    db, life, _ = setup_discovery(ctx)
    clock = frozen_clock()
    contact = ContactService(life, enabled=True, clock=clock)

    def revoke(stage):
        if stage == 'after_generation':
            contact.set_preferences('big_fish', allowed=False, now=clock())

    with character_scope('big_fish'):
        pipeline, candidate, decision, generator, _, _, _ = make_pipeline(db, life, clock=clock, fail_hook=revoke)
        result = pipeline.run(candidate, decision)
        assert result.status == 'rejected' and result.reason_code == 'contact_not_allowed'
        assert generator.calls == 1 and contact.list_intents('big_fish') == []
        with db.connection() as sql:
            assert sql.execute('SELECT COUNT(*) FROM proactive_messages').fetchone()[0] == 0
            assert sql.execute('SELECT review_status FROM autonomous_contact_drafts WHERE candidate_id=?', (candidate.dedup_key,)).fetchone()[0] == 'cancelled'
            assert sql.execute('SELECT message_status FROM autonomous_contact_reservations WHERE candidate_id=?', (candidate.dedup_key,)).fetchone()[0] == 'released'


def test_restart_cancels_persisted_draft_and_never_replays(ctx):
    db, life, _ = setup_discovery(ctx)
    base = datetime.now(UTC) + timedelta(seconds=3)
    instant = [base]
    clock = lambda: instant[0]

    def crash(stage):
        if stage == 'after_draft_persistence':
            raise RuntimeError('injected process stop')

    with character_scope('big_fish'):
        pipeline, candidate, decision, generator, state, _, _ = make_pipeline(db, life, clock=clock, fail_hook=crash)
        with pytest.raises(RuntimeError, match='injected process stop'):
            pipeline.run(candidate, decision)
        instant[0] += timedelta(minutes=3)
        recovered = state.recover_expired_claims('big_fish', now=instant[0])
        assert recovered[0].status == 'failed'
        assert generator.calls == 1
        assert state.claim(candidate.dedup_key, 'big_fish', now=instant[0]).token is None
        with db.connection() as sql:
            assert sql.execute('SELECT review_status FROM autonomous_contact_drafts WHERE candidate_id=?', (candidate.dedup_key,)).fetchone()[0] == 'cancelled'
            assert sql.execute('SELECT COUNT(*) FROM contact_intents').fetchone()[0] == 0
            assert sql.execute('SELECT message_status FROM autonomous_contact_reservations WHERE candidate_id=?', (candidate.dedup_key,)).fetchone()[0] == 'released'


def test_lease_expiry_after_generation_blocks_draft_handoff(ctx):
    db, life, _ = setup_discovery(ctx)
    instant = [datetime.now(UTC) + timedelta(seconds=3)]
    clock = lambda: instant[0]

    def expire_lease(stage):
        if stage == 'after_generation':
            instant[0] += timedelta(minutes=3)

    with character_scope('big_fish'):
        pipeline, candidate, decision, generator, _, contact, _ = make_pipeline(db, life, clock=clock, fail_hook=expire_lease)
        result = pipeline.run(candidate, decision)
        assert result.reason_code == 'draft_persistence_rejected'
        assert generator.calls == 1 and contact.list_intents('big_fish') == []
        with db.connection() as sql:
            assert sql.execute('SELECT status FROM autonomous_contact_candidates WHERE id=?', (candidate.dedup_key,)).fetchone()[0] == 'cancelled'
            assert sql.execute('SELECT COUNT(*) FROM proactive_messages').fetchone()[0] == 0


@pytest.mark.parametrize('field', ('busy_until', 'pause_until', 'quiet_start_minute'))
def test_permission_change_to_busy_quiet_or_pause_after_generation_blocks_handoff(ctx, field):
    db, life, _ = setup_discovery(ctx)
    clock = frozen_clock()
    contact = ContactService(life, enabled=True, clock=clock)

    def change(stage):
        if stage == 'after_generation':
            if field == 'quiet_start_minute':
                local = clock().astimezone(ZoneInfo('Asia/Shanghai'))
                minute = local.hour * 60 + local.minute
                contact.set_preferences('big_fish', quiet_start_minute=minute,
                                        quiet_end_minute=(minute + 1) % 1440, now=clock())
            else:
                contact.set_preferences('big_fish', **{field: clock() + timedelta(hours=1)}, now=clock())

    with character_scope('big_fish'):
        pipeline, candidate, decision, _, _, _, _ = make_pipeline(db, life, clock=clock, fail_hook=change)
        result = pipeline.run(candidate, decision)
        assert result.status in {'rejected', 'suppressed'}
        assert result.reason_code in {'busy', 'paused', 'quiet_hours'}
        assert pipeline.contact.list_intents('big_fish') == []


def test_general_model_budget_unavailable_never_enters_draft_generation(ctx):
    db, life, _ = setup_discovery(ctx)
    with character_scope('big_fish'):
        pipeline, candidate, decision, generator, _, _, _ = make_pipeline(db, life, clock=frozen_clock(), available=False)
        result = pipeline.run(candidate, decision)
        assert result.reason_code == 'general_model_budget_unavailable'
        assert generator.calls == 0
        with db.connection() as sql:
            assert sql.execute('SELECT COUNT(*) FROM autonomous_contact_candidates').fetchone()[0] == 0


def test_disabled_feature_is_a_noop(ctx):
    db, life, _ = setup_discovery(ctx)
    with character_scope('big_fish'):
        pipeline, candidate, decision, generator, state, _, _ = make_pipeline(db, life, clock=frozen_clock())
        state.enabled = False
        before = dump(db)
        result = pipeline.run(candidate, decision)
        assert result.reason_code == 'autonomous_contact_disabled'
        assert generator.calls == 0 and dump(db) == before


def test_mock_pipeline_requires_testing_environment_and_shared_database(ctx, monkeypatch):
    db, life, _ = setup_discovery(ctx)
    with character_scope('big_fish'):
        pipeline, _, _, _, state, contact, gate = make_pipeline(db, life, clock=frozen_clock())
        other = Database(db.path, environment='testing', test_data_root=db.test_data_root)
        wrong_contact = ContactService(LifeRepository(other))
        with pytest.raises(ValueError, match='d3a_shared_database_required'):
            AutonomousContactPipeline(state, wrong_contact, pipeline.drafts, gate)
        monkeypatch.setattr(db, 'environment', 'development')
        with pytest.raises(RuntimeError, match='d3a_mock_pipeline_testing_only'):
            AutonomousContactPipeline(state, contact, pipeline.drafts, gate)


def test_handoff_switch_defaults_closed_even_when_d2_state_is_enabled(ctx):
    db, life, _ = setup_discovery(ctx)
    with character_scope('big_fish'):
        pipeline, candidate, decision, generator, state, _, _ = make_pipeline(db, life, clock=frozen_clock())
        state.handoff_enabled = False
        before = dump(db)
        result = pipeline.run(candidate, decision)
        assert result.reason_code == 'autonomous_contact_disabled'
        assert generator.calls == 0 and dump(db) == before


def test_existing_contact_intent_blocks_candidate_before_generation(ctx):
    db, life, _ = setup_discovery(ctx)
    clock = frozen_clock()
    with character_scope('big_fish'):
        snapshot = build_snapshot(db, 'big_fish', now=clock())
        candidate = generate_candidates(snapshot)[0]
        decision = dry_run(snapshot)
        ContactService(life).create_intent(source='life_share', source_id=next(ref.id for ref in candidate.evidence if ref.kind == 'share'),
                                            reason='existing-contact', now=clock())
        pipeline, _, _, generator, _, contact, _ = make_pipeline(db, life, clock=clock, candidate=candidate)
        result = pipeline.run(candidate, decision)
        assert result.status in {'rejected', 'suppressed'}
        assert generator.calls == 0 and len(contact.list_intents('big_fish')) == 1


def test_duplicate_reentry_after_handoff_does_not_consume_again(ctx):
    db, life, _ = setup_discovery(ctx)
    with character_scope('big_fish'):
        pipeline, candidate, decision, generator, _, _, _ = make_pipeline(db, life, clock=frozen_clock())
        first = pipeline.run(candidate, decision)
        before = dump(db)
        second = pipeline.run(candidate, decision)
        assert first.status == 'handed_off' and second.status == 'suppressed'
        assert generator.calls == 1 and dump(db) == before


def test_request_started_then_process_failure_becomes_unknown_and_is_not_retried(ctx):
    db, life, _ = setup_discovery(ctx)
    base = datetime.now(UTC) + timedelta(seconds=3)
    instant = [base]
    clock = lambda: instant[0]

    def crash(stage):
        if stage == 'after_request_started':
            raise RuntimeError('process lost after request may have started')

    with character_scope('big_fish'):
        pipeline, candidate, decision, generator, state, _, _ = make_pipeline(db, life, clock=clock, fail_hook=crash)
        with pytest.raises(RuntimeError, match='process lost'):
            pipeline.run(candidate, decision)
        instant[0] += timedelta(minutes=3)
        state.recover_expired_claims('big_fish', now=instant[0])
        with db.connection() as sql:
            assert sql.execute('SELECT request_status FROM autonomous_contact_reservations WHERE candidate_id=?', (candidate.dedup_key,)).fetchone()[0] == 'outcome_unknown'
        assert generator.calls == 0
        assert state.claim(candidate.dedup_key, 'big_fish', now=instant[0]).token is None


def test_evidence_revoked_during_generation_fails_closed(ctx):
    db, life, _ = setup_discovery(ctx)
    clock = frozen_clock()
    def revoke(stage):
        if stage == 'after_generation':
            with db.connection() as sql:
                sql.execute("UPDATE exploration_evidence SET outcome='failed' WHERE id IN (SELECT id FROM exploration_evidence WHERE run_id IN (SELECT id FROM exploration_runs WHERE character_id='big_fish'))")
    with character_scope('big_fish'):
        pipeline, candidate, decision, generator, _, contact, _ = make_pipeline(db, life, clock=clock, fail_hook=revoke)
        result = pipeline.run(candidate, decision)
        assert result.reason_code == 'draft_persistence_rejected'
        assert contact.list_intents('big_fish') == []
        with db.connection() as sql:
            assert sql.execute('SELECT status FROM autonomous_contact_candidates WHERE id=?', (candidate.dedup_key,)).fetchone()[0] == 'cancelled'
            assert sql.execute('SELECT COUNT(*) FROM proactive_messages').fetchone()[0] == 0
