"""Fault injection for local instance ownership and atomic life ticks."""

from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from app.db import Database
from app.life import LifeRepository, _iso
from app.life_engine import LifeEngine
from app.life_instance import InstanceConflict, LifeInstance
from app.life_scheduler import LifeScheduler
from app.life_share import LifeShareService


T0 = datetime(2026, 11, 2, 5, tzinfo=UTC)


def _store(tmp_path):
    db = Database(str(tmp_path / "life_consistency.db"))
    db.initialize()
    return db, LifeRepository(db)


def _counts(db, event_id):
    with db.connection() as sql:
        event = dict(sql.execute("SELECT activity_status, execution_status, occurred_at, runtime_owner_token FROM life_events WHERE id=?", (event_id,)).fetchone())
        return {
            "event": event,
            "transitions": sql.execute("SELECT COUNT(*) FROM life_activity_transitions WHERE event_id=?", (event_id,)).fetchone()[0],
            "learning": sql.execute("SELECT COUNT(*) FROM life_preference_changes WHERE event_id=?", (event_id,)).fetchone()[0],
            "share_decisions": sql.execute("SELECT COUNT(*) FROM life_share_decisions WHERE event_id=?", (event_id,)).fetchone()[0],
            "share_intents": sql.execute("SELECT COUNT(*) FROM life_share_intents WHERE event_id=?", (event_id,)).fetchone()[0],
            "interruptions": sql.execute("SELECT COUNT(*) FROM life_activity_interruptions WHERE event_id=?", (event_id,)).fetchone()[0],
            "checkpoint": sql.execute("SELECT last_tick_at FROM life_scheduler_ticks WHERE character_id='fish'").fetchone(),
        }


def test_live_owner_rejects_second_instance_before_and_after_lease_expiry(tmp_path):
    db, repo = _store(tmp_path)
    first = LifeInstance(db, repo)
    token = first.acquire(now=T0)
    event = LifeEngine(repo).advance("fish", now=T0)[0]
    other_db = Database(db.path)
    second = LifeInstance(other_db, LifeRepository(other_db))
    with pytest.raises(InstanceConflict):
        second.acquire(now=T0 + timedelta(minutes=1))
    second.release(now=T0 + timedelta(minutes=1))
    contender = """
import sys
from app.db import Database
from app.life import LifeRepository
from app.life_instance import InstanceConflict, LifeInstance
db=Database(sys.argv[1])
try:
    LifeInstance(db, LifeRepository(db)).acquire()
except InstanceConflict:
    sys.exit(19)
sys.exit(0)
"""
    raced = subprocess.run([sys.executable, "-c", contender, db.path],
                           cwd=os.path.dirname(os.path.dirname(__file__)),
                           capture_output=True, text=True, timeout=30, check=False)
    assert raced.returncode == 19, raced.stderr
    with db.connection() as sql:
        sql.execute("UPDATE life_service_instances SET expires_at=? WHERE token=?", (_iso(T0 - timedelta(seconds=1)), token))
    with pytest.raises(InstanceConflict):
        second.acquire(now=T0 + timedelta(minutes=16))
    assert _counts(db, event["id"])["event"]["execution_status"] == "running"
    assert _counts(db, event["id"])["interruptions"] == 0
    first.heartbeat(now=T0 + timedelta(minutes=16))
    first.release(now=T0 + timedelta(minutes=17))
    closed = _counts(db, event["id"])
    assert closed["event"]["activity_status"] == "active"
    assert closed["event"]["execution_status"] == "interrupted"
    assert closed["event"]["occurred_at"] is None
    assert closed["interruptions"] == 1
    second.acquire(now=T0 + timedelta(minutes=18))
    try:
        assert _counts(db, event["id"])["interruptions"] == 1
    finally:
        second.release(now=T0 + timedelta(minutes=18))


def test_normal_shutdown_only_interrupts_its_own_activity(tmp_path):
    db, repo = _store(tmp_path)
    owner = LifeInstance(db, repo)
    token = owner.acquire(now=T0)
    mine = LifeEngine(repo).advance("fish", now=T0)[0]
    foreign = LifeEngine(repo).advance("bird", now=T0)[0]
    with db.connection() as sql:
        sql.execute("UPDATE life_events SET runtime_owner_token='foreign-token' WHERE id=?", (foreign["id"],))
    owner.release(now=T0 + timedelta(minutes=1))
    assert _counts(db, mine["id"])["event"]["execution_status"] == "interrupted"
    assert _counts(db, foreign["id"])["event"]["execution_status"] == "running"
    with db.connection() as sql:
        assert sql.execute("SELECT COUNT(*) FROM life_activity_interruptions WHERE instance_token=?", (token,)).fetchone()[0] == 1
    next_owner = LifeInstance(db, repo)
    next_owner.acquire(now=T0 + timedelta(minutes=2))
    try:
        assert _counts(db, foreign["id"])["event"]["execution_status"] == "interrupted"
        assert _counts(db, foreign["id"])["interruptions"] == 1
    finally:
        next_owner.release(now=T0 + timedelta(minutes=2))


def test_abrupt_exit_during_sqlite_write_rolls_back_and_recovery_never_completes_offline(tmp_path):
    db, _ = _store(tmp_path)
    script = """
import os, sys
from datetime import UTC, datetime, timedelta
from app.db import Database
from app.life import LifeRepository
from app.life_engine import LifeEngine
from app.life_instance import LifeInstance
db=Database(sys.argv[1]); repo=LifeRepository(db)
owner=LifeInstance(db, repo); owner.acquire(now=datetime(2026,11,2,5,tzinfo=UTC))
t=datetime(2026,11,2,5,tzinfo=UTC)
event=repo.record('发现一个有趣的小角落', character_id='fish', event_type='exploring', status='planned', planned_start_at=t, planned_end_at=t+timedelta(minutes=20), feeling='开心', now=t)
repo.transition(event['id'], 'active', character_id='fish', now=t)
with db.atomic():
    LifeEngine(repo)._advance_locked('fish', now=t+timedelta(minutes=30), allow_autonomous=True)
    os._exit(17)
"""
    run = subprocess.run([sys.executable, "-c", script, db.path], cwd=os.path.dirname(os.path.dirname(__file__)),
                         capture_output=True, text=True, timeout=30, check=False)
    assert run.returncode == 17, run.stderr
    with db.connection() as sql:
        event_id = sql.execute("SELECT id FROM life_events WHERE character_id='fish'").fetchone()[0]
    crashed = _counts(db, event_id)
    assert crashed["event"]["activity_status"] == "active"
    assert crashed["event"]["execution_status"] == "running"
    assert crashed["event"]["occurred_at"] is None
    assert crashed["transitions"] == 2
    assert crashed["learning"] == crashed["share_decisions"] == crashed["share_intents"] == 0
    recovered_repo = LifeRepository(db)
    restarted = LifeInstance(db, recovered_repo)
    restarted.acquire(now=T0 + timedelta(days=20))
    try:
        after = _counts(db, event_id)
        assert after["event"]["activity_status"] == "active"
        assert after["event"]["execution_status"] == "interrupted"
        assert after["event"]["occurred_at"] is None
        assert after["interruptions"] == 1
        assert recovered_repo.get_state("fish")["current_activity"] is None
        with pytest.raises(ValueError):
            recovered_repo.transition(event_id, "completed", character_id="fish", now=T0 + timedelta(days=20))
        assert len(LifeEngine(recovered_repo).advance("fish", now=T0 + timedelta(days=20))) <= 1
        assert len(recovered_repo.list_events("fish")) <= 2
    finally:
        restarted.release(now=T0 + timedelta(days=20))


def test_quick_restart_reclaims_unexpired_lease_only_after_process_exit(tmp_path):
    db, _ = _store(tmp_path)
    script = """
import os, sys
from datetime import UTC, datetime
from app.db import Database
from app.life import LifeRepository
from app.life_engine import LifeEngine
from app.life_instance import LifeInstance
db=Database(sys.argv[1]); repo=LifeRepository(db)
LifeInstance(db, repo).acquire(now=datetime(2026,11,2,5,tzinfo=UTC))
LifeEngine(repo).advance('fish', now=datetime(2026,11,2,5,tzinfo=UTC))
with db.connection() as sql:
    sql.execute("INSERT INTO life_advance_leases(character_id,token,expires_at) VALUES ('fish','stale-token','2026-11-02T05:15:00+00:00')")
os._exit(17)
"""
    run = subprocess.run([sys.executable, "-c", script, db.path], cwd=os.path.dirname(os.path.dirname(__file__)),
                         capture_output=True, text=True, timeout=30, check=False)
    assert run.returncode == 17, run.stderr
    with db.connection() as sql:
        event_id = sql.execute("SELECT id FROM life_events WHERE character_id='fish'").fetchone()[0]
        lease = sql.execute("SELECT expires_at FROM life_service_instances WHERE id=1").fetchone()[0]
        assert lease > _iso(T0 + timedelta(minutes=1))
    repo = LifeRepository(db)
    restart = LifeInstance(db, repo)
    restart.acquire(now=T0 + timedelta(minutes=1))
    try:
        state = _counts(db, event_id)
        assert state["event"]["execution_status"] == "interrupted"
        assert state["event"]["occurred_at"] is None
        assert state["interruptions"] == 1
        with db.connection() as sql:
            assert sql.execute("SELECT COUNT(*) FROM life_advance_leases").fetchone()[0] == 0
        with repo.advance_lease("fish") as lease_token:
            assert lease_token is not None
    finally:
        restart.release(now=T0 + timedelta(minutes=1))


def test_failed_tick_rolls_back_event_learning_share_and_checkpoint_then_retries(tmp_path, monkeypatch):
    db, repo = _store(tmp_path)
    event = repo.record("发现一个有趣的小角落", character_id="fish", event_type="exploring", status="planned",
                        planned_start_at=T0, planned_end_at=T0 + timedelta(minutes=20), feeling="开心", now=T0)
    scheduler = LifeScheduler(repo, character_ids=("fish",), interval=timedelta(minutes=30))
    assert scheduler.tick(now=T0)[0]["activity_status"] == "active"
    original = LifeShareService.consider

    def fail_after_share(self, event_id, *, now=None):
        original(self, event_id, now=now)
        raise RuntimeError("injected after share write")

    monkeypatch.setattr(LifeShareService, "consider", fail_after_share)
    with pytest.raises(RuntimeError, match="injected"):
        scheduler.tick(now=T0 + timedelta(minutes=30))
    failed = _counts(db, event["id"])
    assert failed["event"]["activity_status"] == "active"
    assert failed["event"]["occurred_at"] is None
    assert failed["transitions"] == 2
    assert failed["learning"] == failed["share_decisions"] == failed["share_intents"] == 0
    assert failed["checkpoint"][0] == _iso(T0)
    monkeypatch.setattr(LifeShareService, "consider", original)
    assert scheduler.tick(now=T0 + timedelta(minutes=30))[0]["activity_status"] == "completed"
    assert scheduler.tick(now=T0 + timedelta(minutes=30)) == []
    retried = _counts(db, event["id"])
    assert retried["event"]["activity_status"] == "completed"
    assert retried["transitions"] == 3
    assert retried["learning"] == retried["share_decisions"] == retried["share_intents"] == 1
    assert retried["checkpoint"][0] == _iso(T0 + timedelta(minutes=30))


def test_two_scheduler_instances_and_long_running_loop_do_not_duplicate(tmp_path):
    db, repo = _store(tmp_path)
    other_db = Database(db.path)
    other = LifeRepository(other_db)
    schedulers = [LifeScheduler(repo, character_ids=("fish",)), LifeScheduler(other, character_ids=("fish",))]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda scheduler: scheduler.tick(now=T0), schedulers))
    assert sum(map(len, results)) == 1
    event = repo.list_events("fish")[0]
    for minute in (0, 1, 5, 10, 20, 29):
        assert schedulers[minute % 2].tick(now=T0 + timedelta(minutes=minute)) == []
    assert len(repo.list_events("fish")) == 1
    assert _counts(db, event["id"])["transitions"] == 2


def test_sqlite_write_lock_failure_leaves_no_checkpoint_and_can_retry(tmp_path, monkeypatch):
    import sqlite3

    db, repo = _store(tmp_path)
    repo.ensure_state("fish", now=T0)
    scheduler = LifeScheduler(repo, character_ids=("fish",))
    locker = sqlite3.connect(db.path)
    locker.execute("BEGIN IMMEDIATE")
    original_connect = sqlite3.connect

    def short_timeout(*args, **kwargs):
        kwargs["timeout"] = 0.05
        return original_connect(*args, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(sqlite3, "connect", short_timeout)
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                scheduler.tick(now=T0)
    finally:
        locker.rollback()
        locker.close()
    with db.connection() as sql:
        assert sql.execute("SELECT COUNT(*) FROM life_scheduler_ticks WHERE character_id='fish'").fetchone()[0] == 0
        assert sql.execute("SELECT COUNT(*) FROM life_events WHERE character_id='fish'").fetchone()[0] == 0
    assert len(scheduler.tick(now=T0)) == 1
    assert len(repo.list_events("fish")) == 1


def test_migration_12_to_14_preserves_legacy_event_and_marks_it_interrupted(tmp_path, monkeypatch):
    import app.db as db_module

    with monkeypatch.context() as patch:
        patch.setattr(db_module, "MIGRATIONS", {version: sql for version, sql in db_module.MIGRATIONS.items() if version <= 12})
        old_db = Database(str(tmp_path / "old.db"))
        old_db.initialize()
        old_repo = LifeRepository(old_db)
        event = old_repo.record("旧版本活动", character_id="fish", event_type="reading", status="planned",
                                planned_start_at=T0, planned_end_at=T0 + timedelta(minutes=20), now=T0)
        with old_db.connection() as sql:
            sql.execute("UPDATE life_events SET activity_status='active', started_at=? WHERE id=?", (_iso(T0), event["id"]))
            sql.execute("UPDATE life_states SET current_activity_event_id=? WHERE character_id='fish'", (event["id"],))
    new_db = Database(old_db.path)
    new_db.initialize()
    with new_db.connection() as sql:
        assert [row[0] for row in sql.execute("SELECT version FROM schema_migrations ORDER BY version")] == list(range(1, 17))
        assert sql.execute("SELECT content FROM life_events WHERE id=?", (event["id"],)).fetchone()[0] == event["content"]
    repo = LifeRepository(new_db)
    owner = LifeInstance(new_db, repo)
    owner.acquire(now=T0 + timedelta(days=1))
    try:
        assert _counts(new_db, event["id"])["event"]["execution_status"] == "interrupted"
        assert _counts(new_db, event["id"])["event"]["occurred_at"] is None
    finally:
        owner.release(now=T0 + timedelta(days=1))


def test_failed_migration_13_leaves_version_12_schema_intact(tmp_path, monkeypatch):
    import sqlite3
    import app.db as db_module

    db = Database(str(tmp_path / "migration_failure.db"))
    with monkeypatch.context() as patch:
        patch.setattr(db_module, "MIGRATIONS", {version: sql for version, sql in db_module.MIGRATIONS.items() if version <= 12})
        db.initialize()
    with monkeypatch.context() as patch:
        patch.setitem(db_module.MIGRATIONS, 13, db_module.MIGRATIONS[13] + "\nSELECT * FROM injected_missing_table;")
        with pytest.raises(sqlite3.OperationalError):
            db.initialize()
    with db.connection() as sql:
        assert sql.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 12
        assert "runtime_owner_token" not in {row[1] for row in sql.execute("PRAGMA table_info(life_events)")}
        assert sql.execute("SELECT name FROM sqlite_master WHERE name='life_service_instances'").fetchone() is None
    db.initialize()
    with db.connection() as sql:
        assert sql.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 16
