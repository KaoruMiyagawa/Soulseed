"""Cutover protocol tests with isolated SQLite and synthetic Legacy ledgers."""

from datetime import UTC, datetime, timedelta
import json
import sqlite3
from types import SimpleNamespace

import httpx
import pytest

from app.config import get_settings
from app.db import Database, MIGRATIONS
from app.life import LifeRepository
from app.model_adapter import ModelBudgetContext, OpenAICompatibleAdapter
from app.model_budget import BudgetRejected, ModelBudgetService
from app.model_budget_history import CutoverRejected, LegacyIdentity, ModelBudgetCutover
from app.ownership import character_scope


DAY = "2030-01-02"
START = "2030-01-02T06:00:00+00:00"
END = "2030-01-02T06:00:01+00:00"


def event(request_id, kind="started", **fields):
    row = {"event": kind, "id": request_id, "at": START if kind == "started" else END}
    if kind == "started":
        row.update(day=DAY, model="mock")
    return row | fields


def append(path, *rows):
    with path.open("ab") as handle:
        for row in rows:
            handle.write(json.dumps(row).encode() + b"\n")


@pytest.fixture
def env(tmp_path):
    db = Database(str(tmp_path / "history.db"))
    db.initialize()
    life = LifeRepository(db)
    life.ensure_state("big_fish")
    with db.atomic() as sql:
        sql.execute("INSERT INTO characters VALUES ('other','local','owned',?)", (START,))
    life.ensure_state("other")
    clock = [datetime(2030, 1, 2, 12, tzinfo=UTC)]
    files = {kind: tmp_path / f"{kind}.jsonl" for kind in ("general", "exploration")}
    for path in files.values():
        path.touch()

    def service(database=db):
        return ModelBudgetService(database, global_limit=4, source_limits={"chat": 1, "exploration": 1},
                                  clock=lambda: clock[0])

    budget = service()

    def coordinator(inventory, *, budget=budget):
        return ModelBudgetCutover(budget, files=files, inventory=inventory)

    return SimpleNamespace(db=db, budget=budget, files=files, clock=clock, coordinator=coordinator, service=service)


def read_requests(env):
    with env.db.connection() as sql:
        return [tuple(row) for row in sql.execute(
            "SELECT id,character_id,source,day,status FROM model_budget_requests ORDER BY id")]


def assert_closed(env):
    assert not env.budget.usage_snapshot("big_fish", day=DAY)["history_ready"]
    with pytest.raises(BudgetRejected, match="history_not_confirmed"):
        env.budget.reserve("strict-attempt", "big_fish", "chat")


def seal(cutover):
    cutover.begin_drain()
    cutover.seal()


def test_merges_events_and_deduplicates_files_on_reimport(env):
    chat = event("chat-1", prompt="discard this synthetic field")
    run = {"exploration_run_id": "run-1", "exploration_step": 2}
    append(env.files["general"], chat, chat, event("chat-1", "finished", status="http_200"),
           event("contact-1"), event("contact-1", "finished", status="http_503"))
    append(env.files["exploration"], event("step-1", **run), event("step-1", "http_started", **run),
           event("step-1", "finished", status="timeout_read", **run),
           event("step-1", "action_validation", reason="invalid_action", **run))
    inventory = {"chat-1": LegacyIdentity("big_fish", "chat"), "contact-1": LegacyIdentity("other", "contact"),
                 "step-1": LegacyIdentity("other", "exploration")}
    cutover = env.coordinator(inventory)
    seal(cutover)
    assert_closed(env)
    report = cutover.import_history()
    assert (report.processed_records, report.unique_requests, report.duplicate_records) == (9, 3, 1)
    assert (report.unknown_outcomes, report.unconfirmed_records, report.inserted_requests) == (1, 0, 3)
    assert report.gate_eligible and report.failure_reason is None
    assert read_requests(env) == [("chat-1", "big_fish", "chat", DAY, "completed"),
                                  ("contact-1", "other", "contact", DAY, "failed"),
                                  ("step-1", "other", "exploration", DAY, "unknown")]
    restarted = env.coordinator(inventory, budget=env.service(Database(env.db.path)))
    repeated = restarted.import_history()
    assert repeated.gate_eligible and repeated.inserted_requests == 0
    with env.db.connection() as sql:
        assert "discard this synthetic field" not in "\n".join(sql.iterdump())
    assert env.budget.usage_snapshot("big_fish")["global_used"] == 3
    assert env.budget.usage_snapshot("big_fish")["source_used"] == {"chat": 1, "exploration": 0}
    with character_scope("other"):
        assert env.budget.usage_snapshot("other")["source_used"] == {"chat": 0, "exploration": 1, "contact": 1}
        with pytest.raises(BudgetRejected, match="source_budget_exhausted"):
            env.budget.reserve("another-step", "other", "exploration")
        env.budget.reserve("other-chat", "other", "chat")
    with pytest.raises(BudgetRejected, match="global_budget_exhausted"):
        env.budget.reserve("new-contact", "big_fish", "contact")


@pytest.mark.parametrize("rows,reason", [
    ([event("one"), event("one", character_id="other")], "request_identity_conflict"),
    ([event("one"), event("one", "finished", status="http_200"),
      event("one", "finished", status="http_503")], "request_event_conflict"),
    ([event("one", "finished", status="http_200")], "orphaned_history_event"),
    ([event(None)], "request_identity_unconfirmed"),
    ([event("unlisted")], "request_identity_unconfirmed"),
    ([event("one", at="2030-01-02T06:00:00")], "timestamp_invalid"),
    ([event("one", day="2030-01-01")], "reservation_day_mismatch"),
    ([event("one", at="2030-01-02T13:00:00+00:00")], "event_after_cutoff"),
    ([event("one"), event("one", "finished")], "result_status_missing"),
    ([], "history_inventory_incomplete"),
])
def test_unresolved_history_fails_closed(env, rows, reason):
    append(env.files["general"], *rows)
    cutover = env.coordinator({"one": LegacyIdentity("big_fish", "chat")})
    seal(cutover)
    report = cutover.import_history()
    assert not report.gate_eligible and report.unconfirmed_records >= 1
    assert reason in report.failure_reason
    assert read_requests(env) == []
    assert_closed(env)


@pytest.mark.parametrize("data,reason", [(b"{bad}\n", "malformed_jsonl"), (b"{\"id\":", "truncated_jsonl"),
                                        (b"{\"event\":\"started\",\"id\":1,\"id\":2}\n", "duplicate_json_key")])
def test_bad_jsonl_is_never_skipped(env, data, reason):
    env.files["general"].write_bytes(data)
    cutover = env.coordinator({})
    seal(cutover)
    report = cutover.import_history()
    assert not report.gate_eligible and reason in report.failure_reason
    assert_closed(env)


def test_missing_required_file_and_duplicate_file_alias_are_rejected(env):
    env.files["exploration"].unlink()
    cutover = env.coordinator({})
    cutover.begin_drain()
    with pytest.raises(CutoverRejected, match="required_log_missing"):
        cutover.seal()
    with pytest.raises(CutoverRejected, match="required_log_inventory_missing"):
        ModelBudgetCutover(env.budget, files={"general": env.files["general"]}, inventory={})
    alias = ModelBudgetCutover(env.budget, files={"general": env.files["general"],
                                               "exploration": env.files["general"]}, inventory={})
    with pytest.raises(CutoverRejected, match="duplicate_history_file"):
        alias.seal()
    assert_closed(env)


def test_cross_file_id_conflict_is_not_deduplicated(env):
    append(env.files["general"], event("same"))
    append(env.files["exploration"], event("same", exploration_run_id="run", exploration_step=1))
    cutover = env.coordinator({"same": LegacyIdentity("big_fish", "chat")})
    seal(cutover)
    assert "request_source_conflict" in cutover.import_history().failure_reason
    assert_closed(env)


def test_drain_waits_for_open_handle_and_rejects_new_admissions(env):
    inventory = {"inflight": LegacyIdentity("big_fish", "chat")}
    writer = env.coordinator(inventory, budget=env.service(Database(env.db.path)))
    coordinator = env.coordinator(inventory)
    with writer.legacy_request(), env.files["general"].open("ab") as handle:
        coordinator.begin_drain()
        with pytest.raises(CutoverRejected, match="legacy_admission_closed"), writer.legacy_request():
            pytest.fail("new request admitted during drain")
        with pytest.raises(CutoverRejected, match="legacy_writers_still_active"):
            coordinator.seal()
        assert not coordinator.import_history().gate_eligible
        assert_closed(env)
        handle.write(json.dumps(event("inflight")).encode() + b"\n")
    coordinator.seal()
    report = coordinator.import_history()
    assert report.gate_eligible and report.unknown_outcomes == 1
    assert read_requests(env)[0][-1] == "unknown"
    with pytest.raises(CutoverRejected, match="legacy_admission_closed"), writer.legacy_request():
        pytest.fail("rollback admitted a Legacy request")
    with pytest.raises(CutoverRejected, match="legacy_rollback_forbidden"):
        writer.begin_drain()


@pytest.mark.parametrize("after_insert", [False, True])
def test_uncoordinated_append_is_detected_before_opening(env, monkeypatch, after_insert):
    append(env.files["general"], event("one"))
    cutover = env.coordinator({"one": LegacyIdentity("big_fish", "chat")})
    seal(cutover)
    if after_insert:
        original = cutover._store_requests
        def write_after_insert(*args, **kwargs):
            original(*args, **kwargs)
            append(env.files["general"], event("rogue"))
        monkeypatch.setattr(cutover, "_store_requests", write_after_insert)
    else:
        append(env.files["general"], event("rogue"))
    assert cutover.import_history().failure_reason == "sealed_history_changed"
    assert not read_requests(env)
    assert_closed(env)


def test_revalidation_invalidates_open_gate_without_reopening_legacy(env):
    cutover = env.coordinator({})
    seal(cutover)
    assert cutover.import_history().gate_eligible
    append(env.files["general"], event("rogue"))
    assert cutover.import_history().failure_reason == "sealed_history_changed"
    assert_closed(env)
    with pytest.raises(CutoverRejected, match="legacy_admission_closed"), cutover.legacy_request():
        pytest.fail("invalidated cutover reopened Legacy")


@pytest.mark.parametrize("interrupt", [sqlite3.OperationalError, SystemExit])
def test_interrupted_import_rolls_back_and_restart_is_idempotent(env, monkeypatch, interrupt):
    append(env.files["general"], event("one"), event("two"))
    inventory = {key: LegacyIdentity("big_fish", "chat") for key in ("one", "two")}
    cutover = env.coordinator(inventory)
    seal(cutover)
    original = env.budget.set_compatibility_gate
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise interrupt("injected after rows and gate were written")
    monkeypatch.setattr(env.budget, "set_compatibility_gate", fail)
    if interrupt is SystemExit:
        with pytest.raises(SystemExit):
            cutover.import_history()
    else:
        assert cutover.import_history().failure_reason == "history_transaction_failed"
    assert read_requests(env) == []
    assert_closed(env)
    restarted = env.coordinator(inventory, budget=env.service(Database(env.db.path)))
    assert restarted.import_history().inserted_requests == 2
    assert restarted.import_history().inserted_requests == 0
    assert len(read_requests(env)) == 2


def test_utc_cutoff_and_cross_day_outcome(env):
    append(env.files["general"], event("old", at="2030-01-02T07:59:00+08:00", day="2030-01-01"),
           event("old", "finished", at="2030-01-02T08:01:00+08:00", status="http_200"))
    cutover = env.coordinator({"old": LegacyIdentity("big_fish", "chat")})
    seal(cutover)
    assert cutover.import_history().gate_eligible
    assert read_requests(env) == [("old", "big_fish", "chat", "2030-01-01", "completed")]
    assert env.budget.usage_snapshot("big_fish")["global_used"] == 0
    assert env.budget.usage_snapshot("big_fish", day="2030-01-01")["global_used"] == 1


def test_cutover_day_rollover_is_closed(env):
    cutover = env.coordinator({})
    seal(cutover)
    env.clock[0] += timedelta(days=1)
    assert cutover.import_history().failure_reason == "cutover_day_changed"
    assert_closed(env)


def test_inventory_and_character_must_be_verified(env):
    append(env.files["general"], event("one"))
    cutover = env.coordinator({"one": LegacyIdentity("missing-character", "chat")})
    seal(cutover)
    changed = env.coordinator({"one": LegacyIdentity("big_fish", "chat")})
    assert changed.import_history().failure_reason == "request_inventory_changed"
    assert cutover.import_history().failure_reason == "history_character_unconfirmed"
    assert_closed(env)


@pytest.mark.asyncio
async def test_mock_transport_requires_verified_cutover_and_sees_imported_caps(env):
    append(env.files["general"], event("historical"))
    cutover = env.coordinator({"historical": LegacyIdentity("big_fish", "chat")})
    settings = get_settings().model_copy(update={"model_enabled": True, "model_api_key": "mock-only-placeholder"})
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "mock response"}}]})
    adapter = OpenAICompatibleAdapter(settings, strict_budget=True, budget_service=env.budget,
                                     budget_transport=httpx.MockTransport(respond))
    messages = [{"role": "user", "content": "synthetic"}]
    with pytest.raises(BudgetRejected, match="verified_cutover_required"):
        env.budget.set_compatibility_gate(DAY, ready=True)
    with pytest.raises(BudgetRejected, match="history_not_confirmed"):
        await adapter.reply(messages, budget_context=ModelBudgetContext("fresh", "big_fish", "contact", DAY))
    assert calls == []
    seal(cutover)
    assert cutover.import_history().gate_eligible
    with pytest.raises(BudgetRejected, match="source_budget_exhausted"):
        await adapter.reply(messages, budget_context=ModelBudgetContext("chat-new", "big_fish", "chat", DAY))
    assert await adapter.reply(messages, budget_context=ModelBudgetContext("fresh", "big_fish", "contact", DAY)) == "mock response"
    assert len(calls) == 1


def test_migration_22_closes_v21_boolean_gate_and_preserves_charges(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "v21.db"))
    with monkeypatch.context() as patch:
        patch.delitem(MIGRATIONS, 22)
        patch.delitem(MIGRATIONS, 23)
        patch.delitem(MIGRATIONS, 24)
        patch.delitem(MIGRATIONS, 25)
        patch.delitem(MIGRATIONS, 26)
        patch.delitem(MIGRATIONS, 27)
        patch.delitem(MIGRATIONS, 28)
        patch.delitem(MIGRATIONS, 29)
        database.initialize()
        LifeRepository(database).ensure_state("big_fish")
        with database.atomic() as sql:
            sql.execute("INSERT INTO model_budget_windows VALUES(?, ?, 1)", (DAY, "[null,{}]"))
            sql.execute("INSERT INTO model_budget_requests VALUES(?,?,?,?,'unknown',?,?,?,NULL)",
                        ("old-request", "big_fish", "chat", DAY, START, END, END))
    database.initialize()
    budget = ModelBudgetService(database, clock=lambda: datetime(2030, 1, 2, 12, tzinfo=UTC))
    snapshot = budget.usage_snapshot("big_fish")
    assert snapshot["global_used"] == 1 and not snapshot["history_ready"]
