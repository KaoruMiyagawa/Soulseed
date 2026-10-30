import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

MIGRATIONS = {1: """
CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE, role TEXT NOT NULL CHECK (role IN ('system', 'user', 'assistant')), content TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_messages_conversation_created ON messages(conversation_id, created_at, id);
""",
2: """
CREATE TABLE IF NOT EXISTS memory_items (
    id TEXT PRIMARY KEY,
    memory_type TEXT NOT NULL CHECK (memory_type IN ('user_preference', 'relationship', 'event', 'goal', 'growth')),
    content TEXT NOT NULL,
    canonical_key TEXT NOT NULL,
    importance REAL NOT NULL CHECK (importance >= 0 AND importance <= 1),
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    status TEXT NOT NULL CHECK (status IN ('active', 'invalid', 'superseded', 'deleted', 'expired')),
    expires_at TEXT,
    superseded_by_id TEXT REFERENCES memory_items(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_accessed_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_active_canonical_key
    ON memory_items(canonical_key) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_memory_items_type_status
    ON memory_items(memory_type, status, updated_at);
CREATE INDEX IF NOT EXISTS idx_memory_items_status_updated
    ON memory_items(status, updated_at);
CREATE TABLE IF NOT EXISTS memory_sources (
    id TEXT PRIMARY KEY,
    memory_id TEXT NOT NULL REFERENCES memory_items(id) ON DELETE CASCADE,
    message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE RESTRICT,
    source_role TEXT NOT NULL CHECK (source_role IN ('system', 'user', 'assistant')),
    evidence_type TEXT NOT NULL CHECK (evidence_type IN ('explicit_fact', 'summary', 'inference', 'correction')),
    excerpt TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(memory_id, message_id, evidence_type)
);
CREATE INDEX IF NOT EXISTS idx_memory_sources_memory
    ON memory_sources(memory_id, created_at);
CREATE INDEX IF NOT EXISTS idx_memory_sources_message
    ON memory_sources(message_id);
"""}


class Database:
    def __init__(self, path: str):
        self.path = path

    def initialize(self) -> None:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
            current = connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]
            for version, sql in sorted(MIGRATIONS.items()):
                if version > current:
                    connection.executescript(sql)
                    connection.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (?, datetime('now'))", (version,))

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

