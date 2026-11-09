"""Regression tests for multi-character exploration isolation and scope safety.

Uses isolated temporary databases and controlled test tools only.
No external network, no paid model, no formal database access.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.config import Settings
from app.db import Database
from app.exploration_agent import ExplorationAgent, ExplorationError
from app.exploration_repository import ExplorationRepository
from app.life import LifeRepository
from app.model_adapter import ActionModelReply
from app.ownership import character_scope, current_character
from app.web_research import ReadPage, SearchResult


class ControlledScriptedModel:
    """Deterministic model that searches, reads a webpage, and finishes with sourced claims."""

    def __init__(self, *, fail_with: Exception | None = None):
        self.calls = []
        self.fail_with = fail_with

    async def reply(self, messages):
        self.calls.append(messages)
        if self.fail_with:
            raise self.fail_with
        payload = json.loads(messages[-1]["content"])
        observations = payload.get("observations", [])
        if not observations:
            return json.dumps({"action": "search_web", "query": "海獭如何使用工具"}, ensure_ascii=False)
        last = observations[-1]
        if last.get("status") == "ok" and "results" in last:
            return json.dumps({"action": "read_webpage", "evidence_id": last["results"][0]["evidence_id"]})
        if last.get("status") == "ok" and "untrusted_page_text" in last:
            return json.dumps({
                "action": "finish",
                "summary": "海獭会使用石头敲开贝类食物。",
                "claims": [{"text": "海獭会使用石头敲开贝类。", "kind": "sourced", "source_ids": [last["evidence_id"]]}],
                "unanswered": [],
            }, ensure_ascii=False)
        return json.dumps({"action": "finish", "summary": "未获得充足资料。", "claims": [], "unanswered": ["待查明"]}, ensure_ascii=False)


class ControlledSearch:
    def __init__(self, results=None):
        self.results = results or [SearchResult("海獭觅食", "https://example.org/otters", "海獭使用石头敲开贝类")]

    async def search(self, _query):
        return self.results


class ControlledReader:
    async def read(self, url):
        return ReadPage("海獭行为", url, "2026-11-06T00:00:00+00:00", "海獭会使用石头敲开贝类外壳获取食物。")


def create_isolated_env(tmp_path: Path):
    db_path = tmp_path / "exploration-isolation-test.db"
    db = Database(str(db_path))
    db.initialize()
    with db.connection() as c:
        c.execute("INSERT INTO characters VALUES ('fish_b', 'local', 'owned', datetime('now'))")
    life = LifeRepository(db)
    repo = ExplorationRepository(db, life)
    settings = Settings(exploration_enabled=True, model_enabled=False)
    return db, life, repo, settings


def test_default_and_non_default_character_normal_exploration(tmp_path):
    """Both default ('big_fish') and non-default ('fish_b') can run explorations to completion."""
    db, life, repo, settings = create_isolated_env(tmp_path)
    model = ControlledScriptedModel()
    search = ControlledSearch()
    reader = ControlledReader()
    agent = ExplorationAgent(repo, life, settings, model=model, search=search, reader=reader)

    initial_scope = current_character.get()
    assert initial_scope == "big_fish"

    # 1. Default character ('big_fish') runs exploration
    with character_scope("big_fish"):
        run_a, _ = repo.create(character_id="big_fish", idempotency_key="key-a-1", requested_topic="海獭如何使用工具")
    result_a = asyncio.run(agent.run(run_a["id"], "big_fish"))
    assert result_a["status"] == "completed"
    assert result_a["character_id"] == "big_fish"
    assert current_character.get() == initial_scope

    # 2. Non-default character ('fish_b') runs exploration from caller context without prior scope
    with character_scope("fish_b"):
        run_b, _ = repo.create(character_id="fish_b", idempotency_key="key-b-1", requested_topic="海獭如何使用工具")
    result_b = asyncio.run(agent.run(run_b["id"], "fish_b"))
    assert result_b["status"] == "completed"
    assert result_b["character_id"] == "fish_b"
    assert current_character.get() == initial_scope

    # Verify database persistence and character ownership integrity
    with db.connection() as c:
        runs_a = c.execute("SELECT id, character_id, status FROM exploration_runs WHERE character_id='big_fish'").fetchall()
        runs_b = c.execute("SELECT id, character_id, status FROM exploration_runs WHERE character_id='fish_b'").fetchall()
        assert len(runs_a) == 1 and runs_a[0]["status"] == "completed"
        assert len(runs_b) == 1 and runs_b[0]["status"] == "completed"

        evidence_a = c.execute("SELECT character_id FROM exploration_evidence WHERE run_id=?", (run_a["id"],)).fetchall()
        evidence_b = c.execute("SELECT character_id FROM exploration_evidence WHERE run_id=?", (run_b["id"],)).fetchall()
        assert all(row["character_id"] == "big_fish" for row in evidence_a)
        assert all(row["character_id"] == "fish_b" for row in evidence_b)


def test_cross_character_data_isolation(tmp_path):
    """Exploration data between different characters is strictly isolated."""
    db, life, repo, settings = create_isolated_env(tmp_path)
    agent = ExplorationAgent(repo, life, settings, model=ControlledScriptedModel(),
                             search=ControlledSearch(), reader=ControlledReader())

    with character_scope("big_fish"):
        run_a, _ = repo.create(character_id="big_fish", idempotency_key="key-iso-a", requested_topic="海獭如何使用工具")
    with character_scope("fish_b"):
        run_b, _ = repo.create(character_id="fish_b", idempotency_key="key-iso-b", requested_topic="海獭如何使用工具")

    asyncio.run(agent.run(run_a["id"], "big_fish"))
    asyncio.run(agent.run(run_b["id"], "fish_b"))

    # Listing by character returns only that character's runs
    list_a = repo.list("big_fish")
    list_b = repo.list("fish_b")
    assert {r["id"] for r in list_a} == {run_a["id"]}
    assert {r["id"] for r in list_b} == {run_b["id"]}

    # Reading another character's run raises KeyError
    with pytest.raises(KeyError):
        repo.get(run_a["id"], "fish_b")
    with pytest.raises(KeyError):
        repo.get(run_b["id"], "big_fish")

    # Reading another character's evidence raises KeyError
    with pytest.raises(KeyError):
        repo.list_evidence(run_a["id"], "fish_b")
    with pytest.raises(KeyError):
        repo.list_evidence(run_b["id"], "big_fish")


def test_cross_character_mismatched_evidence_insert_rejected_by_trigger(tmp_path):
    """Database triggers abort any attempt to insert evidence with mismatched character_id."""
    db, life, repo, settings = create_isolated_env(tmp_path)
    with character_scope("big_fish"):
        run_a, _ = repo.create(character_id="big_fish", idempotency_key="key-trig-a", requested_topic="海獭如何使用工具")

    # 1. Repository-level protection: querying run for another character raises KeyError
    with pytest.raises(KeyError):
        with character_scope("fish_b"):
            repo.add_evidence(run_a["id"], "fish_b", tool_name="search_web", query_text="query", outcome="empty")

    # 2. Database trigger-level protection: direct SQL insert with mismatched run ownership is aborted
    with pytest.raises(sqlite3.IntegrityError, match="cross-character exploration evidence"):
        with db.connection() as c:
            with character_scope("fish_b"):
                c.execute(
                    "INSERT INTO exploration_evidence(id, run_id, character_id, sequence_no, tool_name, outcome, created_at) "
                    "VALUES ('ev-bad', ?, 'fish_b', 1, 'search_web', 'empty', datetime('now'))",
                    (run_a["id"],),
                )


def test_non_default_character_abnormal_termination_and_cleanup(tmp_path):
    """Non-default character handles failures and cleans up cleanly without ownership error."""
    db, life, repo, settings = create_isolated_env(tmp_path)

    # 1. Unhandled model exception mid-exploration for non-default character
    failing_model = ControlledScriptedModel(fail_with=RuntimeError("simulated unhandled provider crash"))
    agent = ExplorationAgent(repo, life, settings, model=failing_model, search=ControlledSearch(), reader=ControlledReader())

    with character_scope("fish_b"):
        run_fail, _ = repo.create(character_id="fish_b", idempotency_key="key-fail-1", requested_topic="海獭如何使用工具")

    initial_scope = current_character.get()
    result = asyncio.run(agent.run(run_fail["id"], "fish_b"))

    assert result["status"] == "failed"
    assert result["error_code"] == "internal_error"
    assert result["character_id"] == "fish_b"
    assert current_character.get() == initial_scope

    # Verify record in DB is properly terminated as failed
    stored = repo.get(run_fail["id"], "fish_b")
    assert stored["status"] == "failed"
    assert stored["error_code"] == "internal_error"


def test_non_default_character_cancellation_cleanup(tmp_path):
    """Cancelling an active non-default character exploration terminates cleanly."""
    db, life, repo, settings = create_isolated_env(tmp_path)

    class BlockingSearch:
        def __init__(self):
            self.started = asyncio.Event()

        async def search(self, _query):
            self.started.set()
            await asyncio.sleep(100)
            return []

    blocking_search = BlockingSearch()
    agent = ExplorationAgent(repo, life, settings, model=ControlledScriptedModel(),
                             search=blocking_search, reader=ControlledReader())

    with character_scope("fish_b"):
        run_cancel, _ = repo.create(character_id="fish_b", idempotency_key="key-cancel-1", requested_topic="海獭如何使用工具")

    initial_scope = current_character.get()

    async def run_and_cancel():
        task = asyncio.create_task(agent.run(run_cancel["id"], "fish_b"))
        await blocking_search.started.wait()
        agent.stop(run_cancel["id"], status="cancelled")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run_and_cancel())
    assert current_character.get() == initial_scope

    # Verify that the run is properly marked as cancelled in SQLite without crashing
    stored = repo.get(run_cancel["id"], "fish_b")
    assert stored["status"] == "cancelled"
    assert stored["error_code"] == "cancelled_by_user"


def test_scope_restoration_across_multiple_contexts(tmp_path):
    """Scope is always restored even when agent.run is invoked inside an outer custom scope."""
    db, life, repo, settings = create_isolated_env(tmp_path)
    agent = ExplorationAgent(repo, life, settings, model=ControlledScriptedModel(),
                             search=ControlledSearch(), reader=ControlledReader())

    with character_scope("fish_b"):
        run, _ = repo.create(character_id="fish_b", idempotency_key="key-nested-1", requested_topic="海獭如何使用工具")

    # Case A: Caller is in 'big_fish' scope, runs agent for 'fish_b'
    assert current_character.get() == "big_fish"
    result = asyncio.run(agent.run(run["id"], "fish_b"))
    assert result["status"] == "completed"
    assert current_character.get() == "big_fish"

    # Case B: Caller is explicitly in 'fish_b' scope, runs agent for 'fish_b'
    with character_scope("fish_b"):
        assert current_character.get() == "fish_b"
        result2 = asyncio.run(agent.run(run["id"], "fish_b"))
        assert current_character.get() == "fish_b"
    assert current_character.get() == "big_fish"
