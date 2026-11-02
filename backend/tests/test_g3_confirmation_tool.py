from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from scripts import confirm_legacy_ownership as tool


def test_snapshot_opens_readonly_and_does_not_change_file(tmp_path):
    database_path = tmp_path / "synthetic.db"
    with sqlite3.connect(database_path) as db:
        db.execute("CREATE TABLE synthetic(value TEXT)")
        db.execute("INSERT INTO synthetic VALUES ('private-test-body')")
    before = hashlib.sha256(database_path.read_bytes()).hexdigest().upper()

    db = tool.open_readonly_database(database_path, before)
    try:
        assert db.execute("SELECT COUNT(*) FROM synthetic").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            db.execute("INSERT INTO synthetic VALUES ('not-allowed')")
    finally:
        db.close()
    assert hashlib.sha256(database_path.read_bytes()).hexdigest().upper() == before


def test_hash_mismatch_rejects_before_sqlite_open(tmp_path, monkeypatch):
    database_path = tmp_path / "synthetic.db"
    database_path.write_bytes(b"synthetic only")
    called = []
    monkeypatch.setattr(tool.sqlite3, "connect", lambda *a, **k: called.append(True))
    with pytest.raises(tool.SnapshotError, match="SHA-256"):
        tool.open_readonly_database(database_path, "0" * 64)
    assert called == []


def test_confirmation_manifest_contains_choices_but_no_content():
    manifest = tool.manifest_for_hash("a" * 64)
    tool.save_message_decision(manifest, "conversation-id", "big_fish")
    tool.save_memory_decision(manifest, "uncertain")
    serialized = json.dumps(manifest, ensure_ascii=False)
    assert "private-test-body" not in serialized
    assert set(manifest) == {"analysis_sha256", "conversations", "memory"}
    assert set(manifest["conversations"][0]) == {"conversation_id", "decision", "confirmed_at", "method"}
    assert set(manifest["memory"]) == {"memory_id", "decision", "confirmed_at", "method"}
    assert manifest["conversations"][0]["decision"] == "big_fish"
    assert manifest["memory"]["decision"] == "uncertain"


def test_memory_assignment_requires_source_conversation_confirmation():
    assert not tool.memory_can_be_assigned_to_big_fish(None)
    assert not tool.memory_can_be_assigned_to_big_fish("quarantined")
    assert not tool.memory_can_be_assigned_to_big_fish("uncertain")
    assert tool.memory_can_be_assigned_to_big_fish("big_fish")
