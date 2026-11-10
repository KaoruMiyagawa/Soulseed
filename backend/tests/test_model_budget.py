from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.db import Database
from app.life import LifeRepository
from app.model_budget import BudgetRejected, ModelBudgetService
from app.ownership import character_scope


@pytest.fixture
def budget(tmp_path):
    db = Database(str(tmp_path/'budget.db'))
    db.initialize()
    LifeRepository(db).ensure_state('big_fish')
    LifeRepository(db).ensure_state('other')
    instant = [datetime(2030, 1, 2, 12, tzinfo=UTC)]
    service = ModelBudgetService(db, global_limit=2, source_limits={'autonomous': 1}, clock=lambda: instant[0])
    service.set_compatibility_gate('2030-01-02', ready=True)
    return db, service, instant


def test_reserve_idempotence_and_two_dimensions(budget):
    _, s, _ = budget
    assert s.reserve('one', 'big_fish', 'autonomous').changed
    assert not s.reserve('one', 'big_fish', 'autonomous').changed
    with pytest.raises(BudgetRejected, match='source_budget_exhausted'):
        s.reserve('two', 'big_fish', 'autonomous')
    s.reserve('two', 'big_fish', 'chat')
    with pytest.raises(BudgetRejected, match='global_budget_exhausted'):
        s.reserve('three', 'big_fish', 'chat')
    assert s.usage_snapshot('big_fish')['global_used'] == 2


def test_started_and_completion_are_idempotent_but_never_release(budget):
    _, s, _ = budget
    s.reserve('one', 'big_fish', 'chat')
    assert s.mark_started('one', 'big_fish').changed
    assert not s.mark_started('one', 'big_fish').changed
    with pytest.raises(BudgetRejected):
        s.release_unstarted('one', 'big_fish')
    assert s.complete('one', 'big_fish').changed
    assert not s.complete('one', 'big_fish').changed
    with pytest.raises(BudgetRejected):
        s.mark_unknown('one', 'big_fish')
    assert s.usage_snapshot('big_fish')['global_used'] == 1


@pytest.mark.parametrize('outcome', ['failed', 'unknown'])
def test_failure_and_timeout_stay_charged(budget, outcome):
    _, s, _ = budget
    s.reserve('one', 'big_fish', 'chat')
    s.mark_started('one', 'big_fish')
    if outcome=='failed':
        s.complete('one', 'big_fish', success=False)
    else:
        s.mark_unknown('one', 'big_fish')
    assert s.reserve('one', 'big_fish', 'chat').status == outcome
    with pytest.raises(BudgetRejected):
        s.mark_started('one', 'big_fish')
    assert s.usage_snapshot('big_fish')['global_used'] == 1


def test_unstarted_release_fences_original_id(budget):
    _, s, _ = budget
    s.reserve('one', 'big_fish', 'chat')
    assert s.release_unstarted('one', 'big_fish').changed
    assert not s.release_unstarted('one', 'big_fish').changed
    assert s.reserve('one', 'big_fish', 'chat').status == 'released'
    with pytest.raises(BudgetRejected):
        s.mark_started('one', 'big_fish')
    assert s.usage_snapshot('big_fish')['global_used'] == 0


def test_independent_connections_and_characters_compete_for_one_slot(budget):
    db, _, instant = budget
    day = instant[0].date().isoformat()
    # Use next UTC day to establish a different, explicitly trusted test policy.
    instant[0] += timedelta(days=1)
    def run(i):
        other_db = Database(db.path)
        s = ModelBudgetService(other_db, global_limit=1, clock=lambda: instant[0])
        character = 'big_fish' if i%2 else 'other'
        with character_scope(character):
            s.set_compatibility_gate(instant[0].date().isoformat(), ready=True)
            try:
                return s.reserve(str(i), character, 'chat').changed
            except BudgetRejected:
                return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(run, range(8))) == 1


def test_restart_recovery_retains_started_across_midnight(budget):
    db, s, instant = budget
    s.reserve('unstarted', 'big_fish', 'chat')
    s.reserve('started', 'big_fish', 'chat')
    s.mark_started('started', 'big_fish')
    instant[0] += timedelta(days=1)
    restarted = ModelBudgetService(Database(db.path), global_limit=2, source_limits={'autonomous':1}, clock=lambda: instant[0])
    assert restarted.recover('big_fish') == 2
    assert restarted.recover('big_fish') == 0
    assert restarted.reserve('started', 'big_fish', 'chat').status == 'unknown'
    assert restarted.usage_snapshot('big_fish', day='2030-01-02')['global_used'] == 1
    assert restarted.usage_snapshot('big_fish')['global_used'] == 0
    with pytest.raises(BudgetRejected, match='history_not_confirmed'):
        restarted.reserve('new', 'big_fish', 'chat')


def test_policy_conflict_scope_conflict_and_history_gate(budget):
    db, s, instant = budget
    s.reserve('id', 'big_fish', 'chat')
    with pytest.raises(BudgetRejected, match='request_identity_conflict'):
        s.reserve('id', 'big_fish', 'exploration')
    with pytest.raises(BudgetRejected, match='character_scope_mismatch'):
        s.reserve('x', 'other', 'chat')
    conflict = ModelBudgetService(db, global_limit=3, clock=lambda:instant[0])
    with pytest.raises(BudgetRejected, match='budget_policy_conflict'):
        conflict.reserve('x','big_fish','chat')
    s.set_compatibility_gate('2030-01-02', ready=False)
    with pytest.raises(BudgetRejected, match='history_not_confirmed'):
        s.mark_started('id', 'big_fish')


def test_unlimited_global_and_utc_conversion(budget):
    db, _, instant = budget
    instant[0] = datetime(2030,1,4,1,tzinfo=timezone(timedelta(hours=8)))
    s = ModelBudgetService(db, clock=lambda:instant[0])
    s.set_compatibility_gate('2030-01-03',ready=True)
    for i in range(4):
        s.reserve(f'no-limit-{i}','big_fish','chat')
    usage=s.usage_snapshot('big_fish')
    assert usage['global_limit'] is None and usage['global_used']==4 and usage['day']=='2030-01-03'


def test_expired_unstarted_cannot_start_and_atomic_rollback(budget):
    db,s,instant=budget
    s.reserve('old','big_fish','chat')
    instant[0]+=timedelta(minutes=6)
    with pytest.raises(BudgetRejected,match='reservation_expired'):
        s.mark_started('old','big_fish')
    with pytest.raises(RuntimeError):
        with db.atomic():
            s.reserve('rolled-back','big_fish','chat')
            raise RuntimeError('fault injection')
    with db.connection() as sql:
        assert sql.execute("SELECT 1 FROM model_budget_requests WHERE id='rolled-back'").fetchone() is None


def test_migration_21_upgrade_rollback_and_idempotence(tmp_path, monkeypatch):
    from app import db as module
    db=Database(str(tmp_path/'upgrade.db'))
    with monkeypatch.context() as patch:
        patch.delitem(module.MIGRATIONS,21)
        db.initialize()
        LifeRepository(db).ensure_state('big_fish')
    with monkeypatch.context() as patch:
        patch.setitem(module.MIGRATIONS,21,'CREATE TABLE partial_budget(id TEXT); SELECT * FROM missing_budget_table;')
        with pytest.raises(Exception):
            db.initialize()
        with db.connection() as sql:
            assert sql.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0]==20
            assert sql.execute("SELECT 1 FROM sqlite_master WHERE name='partial_budget'").fetchone() is None
    db.initialize()
    db.initialize()
    with db.connection() as sql:
        assert sql.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0]==21
        assert sql.execute("SELECT 1 FROM life_states WHERE character_id='big_fish'").fetchone()


def test_snapshot_is_readonly_and_link_requires_actual_d2_reservation(budget):
    db,s,_=budget
    with db.connection() as sql:
        before='\n'.join(sql.iterdump())
    s.usage_snapshot('big_fish')
    with pytest.raises(BudgetRejected,match='autonomous_reservation_mismatch'):
        s.reserve('no-d2-link','big_fish','autonomous',autonomous_reservation_id='missing')
    with db.connection() as sql:
        assert '\n'.join(sql.iterdump())==before
