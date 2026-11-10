"""D2 candidate state and budget tests use only disposable databases."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from app.autonomous_intent import build_snapshot, generate_candidates
from app import db as db_module
from app.autonomous_intent_state import AutonomousIntentStateRepository, topic_fingerprint
from app.autonomous_personas import resolve_trusted_persona, trusted_persona_registry
from app.config import Settings
from app.contact import ContactService
from app.db import Database
from app.life import LifeRepository
from app.repositories import ConversationRepository, MessageRepository
from app.ownership import character_scope
from .test_autonomous_intent import ctx, conversation, discovery, dump


def clock():
    return datetime.now(UTC) + timedelta(seconds=2)


def candidates_for(db, character='big_fish', now=None):
    with character_scope(character):
        snapshot = build_snapshot(db, character, now=now or clock())
        return generate_candidates(snapshot)


def state_repo(db, **kwargs):
    return AutonomousIntentStateRepository(db, enabled=True, **kwargs)


def candidate_setup(db, life):
    conversation(db, user='你好', reply='你好呀')
    discovery(db, life)
    return candidates_for(db)[0]


def test_migration_19_is_repeatable_and_preserves_preexisting_rows(ctx):
    db, _ = ctx
    with db.connection() as sql:
        assert sql.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == 21
        candidate_columns = {row['name'] for row in sql.execute('PRAGMA table_info(autonomous_contact_candidates)')}
        reservation_columns = {row['name'] for row in sql.execute('PRAGMA table_info(autonomous_contact_reservations)')}
        assert {'id', 'character_id', 'candidate_type', 'evidence_json', 'topic_fingerprint', 'status', 'lease_token', 'lease_expires_at'} <= candidate_columns
        assert {'request_key', 'local_day', 'request_status', 'message_status', 'timezone_id'} <= reservation_columns
    before = dump(db)
    db.initialize()
    assert dump(db) == before


def test_migration_18_to_20_preserves_existing_life_contact_and_chat_rows(tmp_path, monkeypatch):
    db = Database(str(tmp_path / 'upgrade-18.db'))
    migration = db_module.MIGRATIONS[19]
    drafts_migration = db_module.MIGRATIONS[20]
    with monkeypatch.context() as patch:
        patch.delitem(db_module.MIGRATIONS, 19)
        patch.delitem(db_module.MIGRATIONS, 20)
        patch.delitem(db_module.MIGRATIONS, 21)
        db.initialize()
        life = LifeRepository(db)
        life.ensure_state('big_fish')
        service = ContactService(life)
        service.set_preferences('big_fish', allowed=True, quiet_start_minute=0, quiet_end_minute=0)
        with character_scope('big_fish'):
            conversation_row = ConversationRepository(db).create()
            message_row = MessageRepository(db).add(conversation_row['id'], 'user', 'migration old row')
        patch.setitem(db_module.MIGRATIONS, 19, migration)
        patch.setitem(db_module.MIGRATIONS, 20, drafts_migration)
    db.initialize()
    with db.connection() as sql:
        assert sql.execute('SELECT COUNT(*) FROM life_states WHERE character_id=?', ('big_fish',)).fetchone()[0] == 1
        assert sql.execute('SELECT allowed FROM contact_preferences WHERE character_id=?', ('big_fish',)).fetchone()[0] == 1
        assert sql.execute('SELECT content FROM messages WHERE id=?', (message_row['id'],)).fetchone()[0] == 'migration old row'
        assert sql.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == 21


def test_migration_19_failure_rolls_back_and_can_be_retried(tmp_path, monkeypatch):
    db = Database(str(tmp_path / 'migration-rollback.db'))
    with monkeypatch.context() as patch:
        patch.setitem(db_module.MIGRATIONS, 19, "CREATE TABLE migration_19_partial(id TEXT); SELECT * FROM missing_migration_table;")
        with pytest.raises(Exception):
            db.initialize()
        with db.connection() as sql:
            assert sql.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == 18
            assert sql.execute("SELECT 1 FROM sqlite_master WHERE name='migration_19_partial'").fetchone() is None
    db.initialize()
    with db.connection() as sql:
        assert sql.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == 21
        assert sql.execute("SELECT 1 FROM sqlite_master WHERE name='autonomous_contact_candidates'").fetchone()


def test_migration_20_failure_rolls_back_and_can_be_retried(tmp_path, monkeypatch):
    db = Database(str(tmp_path / 'draft-migration-rollback.db'))
    migration = db_module.MIGRATIONS[20]
    with monkeypatch.context() as patch:
        patch.delitem(db_module.MIGRATIONS, 20)
        patch.delitem(db_module.MIGRATIONS, 21)
        db.initialize()
        patch.setitem(db_module.MIGRATIONS, 20, "CREATE TABLE draft_migration_partial(id TEXT); SELECT * FROM missing_draft_migration_table;")
        with pytest.raises(Exception):
            db.initialize()
        with db.connection() as sql:
            assert sql.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == 19
            assert sql.execute("SELECT 1 FROM sqlite_master WHERE name='draft_migration_partial'").fetchone() is None
        patch.setitem(db_module.MIGRATIONS, 20, migration)
    db.initialize()
    with db.connection() as sql:
        assert sql.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == 21
        assert sql.execute("SELECT 1 FROM sqlite_master WHERE name='autonomous_contact_drafts'").fetchone()


def test_candidate_persistence_is_canonical_readable_and_idempotent(ctx):
    db, life = ctx
    candidate = candidate_setup(db, life)
    repository = state_repo(db)
    before = dump(db)
    first = repository.persist_candidate(candidate, now=clock())
    second = repository.persist_candidate(candidate, now=clock())
    assert first.created and second.created is False and first.candidate_id == second.candidate_id
    with db.connection() as sql:
        rows = sql.execute('SELECT evidence_json,status,topic_fingerprint FROM autonomous_contact_candidates').fetchall()
    assert len(rows) == 1 and rows[0]['status'] == 'pending'
    assert '海獭' not in rows[0]['evidence_json']
    assert dump(db) != before
    with db.connection() as sql:
        assert sql.execute('SELECT COUNT(*) FROM contact_intents').fetchone()[0] == 0
        assert sql.execute('SELECT COUNT(*) FROM proactive_messages').fetchone()[0] == 0


def test_candidate_input_cannot_forge_evidence_or_cross_character_scope(ctx):
    db, life = ctx
    candidate = candidate_setup(db, life)
    repository = state_repo(db)
    with character_scope('other_role'):
        with pytest.raises(ValueError, match='scope_mismatch'):
            repository.persist_candidate(candidate)
    with character_scope('big_fish'):
        from dataclasses import replace
        forged = replace(candidate, evidence=())
        saved = repository.persist_candidate(forged)
        with db.connection() as sql:
            stored = sql.execute('SELECT evidence_json FROM autonomous_contact_candidates WHERE id=?', (saved.candidate_id,)).fetchone()[0]
        assert stored != '[]' and candidate.dedup_key == saved.candidate_id
        with pytest.raises(ValueError, match='scope_mismatch'):
            repository.claim(candidate.dedup_key, 'other_role')


def test_concurrent_candidate_claims_have_one_winner(ctx):
    db, life = ctx
    candidate = candidate_setup(db, life)
    repository = state_repo(db, daily_message_limit=2)
    with character_scope('big_fish'):
        repository.persist_candidate(candidate, now=clock())
    barrier = __import__('threading').Barrier(2)

    def claim():
        with character_scope('big_fish'):
            barrier.wait()
            return repository.claim(candidate.dedup_key, 'big_fish', now=clock())

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: claim(), range(2)))
    assert sum(item.token is not None for item in results) == 1
    assert sum(item.reason_code == 'candidate_not_claimable' for item in results) == 1
    with db.connection() as sql:
        assert sql.execute('SELECT COUNT(*) FROM autonomous_contact_reservations').fetchone()[0] == 1


def test_daily_message_budget_reservation_is_atomic_and_role_scoped(ctx):
    db, life = ctx
    with character_scope('big_fish'):
        conversation(db, user='下次聊聊海獭', reply='好呀，下次一起聊海獭', age=30)
        discovery(db, life)
        fish_candidates = candidates_for(db)
    with db.connection() as sql:
        sql.execute("INSERT INTO characters(id,owner_user_id,ownership_status,created_at) VALUES('calm_one','local','owned',?)", (clock().isoformat(),))
    with character_scope('calm_one'):
        life.ensure_state('calm_one')
        ContactService(life).set_preferences('calm_one', quiet_start_minute=0, quiet_end_minute=0)
        conversation(db, user='你好', reply='你好呀')
        discovery(db, life, 'calm_one')
        calm_candidates = candidates_for(db, 'calm_one')
    repository = state_repo(db)
    now = clock()
    assert len(fish_candidates) >= 2
    with character_scope('big_fish'):
        saved = [repository.persist_candidate(c, now=now) for c in fish_candidates[:2]]
        first = repository.claim(saved[0].candidate_id, 'big_fish', now=now)
        second = repository.claim(saved[1].candidate_id, 'big_fish', now=now)
    assert first.status == 'claimed'
    assert second.status == 'deferred' and second.reason_code == 'message_budget_exhausted'
    with character_scope('calm_one'):
        saved_other = repository.persist_candidate(calm_candidates[0], now=now)
        other = repository.claim(saved_other.candidate_id, 'calm_one', now=now)
    assert other.status == 'claimed'
    with db.connection() as sql:
        assert sql.execute("SELECT COUNT(*) FROM autonomous_contact_reservations WHERE character_id='big_fish' AND local_day=? AND message_status='reserved'", (now.date().isoformat(),)).fetchone()[0] == 1
        assert sql.execute("SELECT COUNT(*) FROM autonomous_contact_reservations WHERE character_id='calm_one'").fetchone()[0] == 1


def test_model_budget_is_atomic_and_unknown_outcome_consumes_reservation(ctx):
    db, life = ctx
    with character_scope('big_fish'):
        conversation(db, user='下次聊聊海獭', reply='好呀，下次一起聊海獭', age=30)
        discovery(db, life)
        candidates = candidates_for(db)
    repository = state_repo(db, daily_model_request_limit=1, daily_message_limit=3)
    now = clock()
    with character_scope('big_fish'):
        saved = [repository.persist_candidate(item, now=now) for item in candidates[:2]]
        first = repository.claim(saved[0].candidate_id, 'big_fish', now=now)
        assert repository.mark_request_started(first.candidate_id, 'big_fish', first.token, now=now).allowed
        unknown = repository.record_request_outcome(first.candidate_id, 'big_fish', first.token, outcome='unknown', now=now)
        second = repository.claim(saved[1].candidate_id, 'big_fish', now=now)
    assert unknown.status == 'outcome_unknown'
    assert second.status == 'deferred' and second.reason_code == 'model_budget_exhausted'
    with db.connection() as sql:
        row = sql.execute('SELECT request_status,message_status FROM autonomous_contact_reservations WHERE candidate_id=?', (first.candidate_id,)).fetchone()
        assert tuple(row) == ('outcome_unknown', 'released')


def test_daily_model_limit_cannot_be_exceeded_by_parallel_claims(ctx):
    db, life = ctx
    with character_scope('big_fish'):
        conversation(db, user='下次聊聊海獭', reply='好呀，下次一起聊海獭', age=30)
        discovery(db, life)
        candidates = candidates_for(db)
    repository = state_repo(db, daily_model_request_limit=1, daily_message_limit=4)
    now = clock()
    with character_scope('big_fish'):
        saved = [repository.persist_candidate(item, now=now) for item in candidates[:2]]
    barrier = __import__('threading').Barrier(2)

    def claim(item):
        with character_scope('big_fish'):
            barrier.wait()
            return repository.claim(item.candidate_id, 'big_fish', now=now)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, saved))
    assert sum(item.status == 'claimed' for item in results) == 1
    assert sum(item.reason_code == 'model_budget_exhausted' for item in results) == 1


def test_safe_pre_request_restart_releases_and_started_restart_becomes_unknown(ctx):
    db, life = ctx
    candidate = candidate_setup(db, life)
    repository = state_repo(db, daily_message_limit=2)
    now = clock()
    with character_scope('big_fish'):
        repository.persist_candidate(candidate, now=now)
        before_start = repository.claim(candidate.dedup_key, 'big_fish', now=now)
        recovered = repository.recover_expired_claims('big_fish', now=now + timedelta(minutes=3))
        assert recovered[0].status == 'pending'
        retry = repository.claim(candidate.dedup_key, 'big_fish', now=now + timedelta(minutes=3))
        assert retry.status == 'claimed' and retry.token != before_start.token
        assert repository.mark_request_started(retry.candidate_id, 'big_fish', retry.token, now=now + timedelta(minutes=3)).allowed
        assert repository.release_before_request(retry.candidate_id, 'big_fish', retry.token,
                                                 now=now + timedelta(minutes=3)).reason_code == 'request_may_have_started'
        unknown = repository.recover_expired_claims('big_fish', now=now + timedelta(minutes=6))
    assert unknown[0].status == 'outcome_unknown'
    with character_scope('big_fish'):
        replay = repository.claim(candidate.dedup_key, 'big_fish', now=now + timedelta(minutes=7))
    assert replay.status == 'not_claimable' and replay.reason_code == 'candidate_not_claimable'
    with db.connection() as sql:
        statuses = [row[0] for row in sql.execute('SELECT request_status FROM autonomous_contact_reservations ORDER BY attempt_no')]
    assert statuses == ['released', 'outcome_unknown']


def test_revoked_permission_and_invalid_evidence_block_post_generation(ctx):
    db, life = ctx
    candidate = candidate_setup(db, life)
    repository = state_repo(db, daily_message_limit=2)
    now = clock()
    with character_scope('big_fish'):
        repository.persist_candidate(candidate, now=now)
        claim = repository.claim(candidate.dedup_key, 'big_fish', now=now)
        assert repository.mark_request_started(claim.candidate_id, 'big_fish', claim.token, now=now).allowed
        repository.record_request_outcome(claim.candidate_id, 'big_fish', claim.token, outcome='success', now=now)
        ContactService(life).set_preferences('big_fish', allowed=False, now=now + timedelta(seconds=1))
        check = repository.recheck(claim.candidate_id, 'big_fish', claim.token, stage='post_generation', now=now + timedelta(seconds=1))
    assert not check.allowed and check.reason_code == 'contact_not_allowed'
    with db.connection() as sql:
        assert sql.execute('SELECT status FROM autonomous_contact_candidates WHERE id=?', (candidate.dedup_key,)).fetchone()[0] == 'cancelled'
        assert sql.execute('SELECT message_status FROM autonomous_contact_reservations WHERE candidate_id=?', (candidate.dedup_key,)).fetchone()[0] == 'released'


def test_ownership_revocation_cancels_claim_and_releases_unstarted_reservation(ctx):
    db, life = ctx
    candidate = candidate_setup(db, life)
    repository = state_repo(db)
    now = clock()
    with character_scope('big_fish'):
        repository.persist_candidate(candidate, now=now)
        claim = repository.claim(candidate.dedup_key, 'big_fish', now=now)
        with db.connection() as sql:
            sql.execute("UPDATE characters SET owner_user_id=NULL,ownership_status='quarantined' WHERE id='big_fish'")
        check = repository.recheck(candidate.dedup_key, 'big_fish', claim.token, stage='pre_generation', now=now)
    assert not check.allowed and check.reason_code == 'character_not_owned'
    with db.connection() as sql:
        assert sql.execute('SELECT status FROM autonomous_contact_candidates WHERE id=?', (candidate.dedup_key,)).fetchone()[0] == 'cancelled'
        assert tuple(sql.execute('SELECT request_status,message_status FROM autonomous_contact_reservations WHERE candidate_id=?', (candidate.dedup_key,)).fetchone()) == ('released', 'released')


def test_quiet_hours_and_post_generation_busy_change_block_handoff(ctx):
    db, life = ctx
    candidate = candidate_setup(db, life)
    repository = state_repo(db, daily_message_limit=2)
    now = clock()
    local = now.astimezone(__import__('zoneinfo').ZoneInfo('Asia/Shanghai'))
    minute = local.hour * 60 + local.minute
    with character_scope('big_fish'):
        repository.persist_candidate(candidate, now=now)
        ContactService(life).set_preferences('big_fish', quiet_start_minute=(minute - 1) % 1440,
                                             quiet_end_minute=(minute + 1) % 1440, now=now)
        quiet = repository.claim(candidate.dedup_key, 'big_fish', now=now)
        ContactService(life).set_preferences('big_fish', quiet_start_minute=0, quiet_end_minute=0, now=now)
        later = now + timedelta(hours=2)
        claim = repository.claim(candidate.dedup_key, 'big_fish', now=later)
        assert repository.mark_request_started(claim.candidate_id, 'big_fish', claim.token, now=later).allowed
        repository.record_request_outcome(claim.candidate_id, 'big_fish', claim.token, outcome='success', now=later)
        ContactService(life).set_preferences('big_fish', busy_until=later + timedelta(hours=1), now=later)
        post = repository.recheck(candidate.dedup_key, 'big_fish', claim.token, stage='post_generation', now=later)
    assert quiet.status == 'deferred' and quiet.reason_code == 'quiet_hours'
    assert not post.allowed and post.reason_code == 'busy'


def test_pause_quiet_and_evidence_change_are_rechecked_from_current_database(ctx):
    db, life = ctx
    candidate = candidate_setup(db, life)
    repository = state_repo(db)
    now = clock()
    with character_scope('big_fish'):
        repository.persist_candidate(candidate, now=now)
        claim = repository.claim(candidate.dedup_key, 'big_fish', now=now)
        ContactService(life).set_preferences('big_fish', pause_until=now + timedelta(hours=1), now=now)
        before = repository.recheck(candidate.dedup_key, 'big_fish', claim.token, stage='pre_generation', now=now)
    assert not before.allowed and before.reason_code == 'paused'
    with character_scope('big_fish'):
        ContactService(life).set_preferences('big_fish', pause_until=None, now=now + timedelta(hours=1))
        claim2 = repository.claim(candidate.dedup_key, 'big_fish', now=now + timedelta(hours=2))
        with db.connection() as sql:
            share_id = next(ref.id for ref in candidate.evidence if ref.kind == 'share')
            sql.execute("UPDATE life_share_intents SET status='dismissed' WHERE id=?", (share_id,))
        check = repository.recheck(candidate.dedup_key, 'big_fish', claim2.token, stage='pre_generation', now=now + timedelta(hours=2))
    assert not check.allowed and check.reason_code == 'candidate_evidence_invalid'


def test_equivalent_contact_intent_blocks_candidate_without_creating_another_intent(ctx):
    db, life = ctx
    candidate = candidate_setup(db, life)
    repository = state_repo(db)
    now = clock()
    with character_scope('big_fish'):
        repository.persist_candidate(candidate, now=now)
        claim = repository.claim(candidate.dedup_key, 'big_fish', now=now)
        share_id = next(ref.id for ref in candidate.evidence if ref.kind == 'share')
        ContactService(life).create_intent(source='life_share', source_id=share_id, reason='existing', now=now)
        check = repository.recheck(candidate.dedup_key, 'big_fish', claim.token, stage='pre_generation', now=now)
    assert not check.allowed and check.reason_code == 'equivalent_contact_intent_exists'
    with db.connection() as sql:
        assert sql.execute('SELECT COUNT(*) FROM contact_intents').fetchone()[0] == 1
        assert sql.execute('SELECT COUNT(*) FROM proactive_messages').fetchone()[0] == 0


def test_topic_normalization_deduplicates_surface_variants_and_unowned_persona_fails(ctx):
    db, _ = ctx
    assert topic_fingerprint('海獭 工具！') == topic_fingerprint('海獭工具')
    with character_scope('big_fish'):
        profile = resolve_trusted_persona(db, 'big_fish')
        registry = trusted_persona_registry(db, 'big_fish')
    assert profile.character_id == 'big_fish'
    assert registry.get('big_fish') == profile
    with character_scope('other'):
        with pytest.raises(Exception):
            resolve_trusted_persona(db, 'other')


def test_same_normalized_topic_from_different_sources_is_cancelled_once(ctx):
    db, _ = ctx
    conversation(db, user='下次聊聊海獭 工具', reply='好呀，下次一起聊海獭 工具', age=30)
    conversation(db, user='下次聊聊海獭工具！', reply='好呀，下次一起聊海獭工具！', age=30)
    candidates = candidates_for(db)
    assert len(candidates) == 2
    repository = state_repo(db)
    now = clock()
    with character_scope('big_fish'):
        first = repository.persist_candidate(candidates[0], now=now)
        second = repository.persist_candidate(candidates[1], now=now)
    assert first.status == 'pending'
    assert second.status == 'cancelled' and second.reason_code == 'duplicate_topic'


def test_disabled_state_repository_has_no_write_side_effects(ctx):
    db, life = ctx
    candidate = candidate_setup(db, life)
    repository = AutonomousIntentStateRepository.from_settings(db, Settings())
    assert repository.enabled is False
    before = dump(db)
    with character_scope('big_fish'):
        with pytest.raises(RuntimeError, match='autonomous_contact_disabled'):
            repository.persist_candidate(candidate)
    assert dump(db) == before


def test_budget_uses_utc_day_and_candidate_timezone_cannot_reset_it(ctx):
    db, life = ctx
    conversation(db, user='下次聊聊海獭', reply='好呀，下次一起聊海獭', age=30)
    discovery(db, life)
    candidates = candidates_for(db)
    assert len(candidates) >= 2
    repository = state_repo(db, daily_message_limit=3)
    now = clock()
    with character_scope('big_fish'):
        stored = [repository.persist_candidate(candidate, now=now) for candidate in candidates[:2]]
        claim = repository.claim(stored[0].candidate_id, 'big_fish', now=now)
        life.set_timezone('big_fish', 'America/Los_Angeles')
        next_day = now + timedelta(days=1, minutes=1)
        next_claim = repository.claim(stored[1].candidate_id, 'big_fish', now=next_day)
    with db.connection() as sql:
        reservations = sql.execute('SELECT timezone_id,local_day FROM autonomous_contact_reservations ORDER BY created_at,id').fetchall()
    assert claim.status == next_claim.status == 'claimed'
    assert [tuple(row) for row in reservations] == [('UTC', now.date().isoformat()), ('UTC', next_day.date().isoformat())]
