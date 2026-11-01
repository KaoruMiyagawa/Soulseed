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
""",
3: """
CREATE TABLE IF NOT EXISTS memory_change_log (
    id TEXT PRIMARY KEY,
    memory_id TEXT REFERENCES memory_items(id) ON DELETE SET NULL,
    action TEXT NOT NULL CHECK (action IN ('created', 'updated', 'invalidated', 'expired', 'superseded', 'soft_deleted', 'permanently_deleted')),
    old_content TEXT,
    new_content TEXT,
    old_canonical_key TEXT,
    new_canonical_key TEXT,
    old_status TEXT,
    new_status TEXT,
    old_importance REAL,
    new_importance REAL,
    old_confidence REAL,
    new_confidence REAL,
    reason TEXT,
    source_message_id TEXT REFERENCES messages(id) ON DELETE SET NULL,
    actor_type TEXT NOT NULL CHECK (actor_type IN ('user', 'system_rule', 'admin')),
    idempotency_key TEXT,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_change_log_idempotency
    ON memory_change_log(idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_memory_change_log_memory_created
    ON memory_change_log(memory_id, created_at, id);
CREATE INDEX IF NOT EXISTS idx_memory_change_log_source
    ON memory_change_log(source_message_id);
""",
4: """
ALTER TABLE memory_change_log ADD COLUMN request_fingerprint TEXT;
CREATE INDEX IF NOT EXISTS idx_memory_change_log_fingerprint
    ON memory_change_log(request_fingerprint);
""",
5: """
CREATE TABLE IF NOT EXISTS memory_change_proposals (
    id TEXT PRIMARY KEY,
    proposal_type TEXT NOT NULL CHECK (proposal_type IN ('correction', 'update', 'invalidation', 'expiration')),
    target_memory_id TEXT REFERENCES memory_items(id) ON DELETE SET NULL,
    proposed_memory_type TEXT NOT NULL CHECK (proposed_memory_type IN ('user_preference', 'relationship', 'event', 'goal', 'growth')),
    proposed_content TEXT NOT NULL,
    proposed_canonical_key TEXT NOT NULL,
    evidence_message_id TEXT REFERENCES messages(id) ON DELETE SET NULL,
    correction_intent TEXT NOT NULL CHECK (correction_intent IN ('none', 'explicit_correction', 'long_term_change', 'temporary_experiment', 'ambiguous_change', 'coexisting_information')),
    relation TEXT NOT NULL CHECK (relation IN ('conflict', 'potential_conflict', 'coexisting', 'unrelated', 'needs_review')),
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    evidence TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'accepted', 'rejected', 'cancelled', 'expired')),
    idempotency_key TEXT,
    request_fingerprint TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolution_reason TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_proposals_idempotency
    ON memory_change_proposals(idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_memory_proposals_status_created
    ON memory_change_proposals(status, created_at, id);
CREATE INDEX IF NOT EXISTS idx_memory_proposals_target
    ON memory_change_proposals(target_memory_id);
CREATE INDEX IF NOT EXISTS idx_memory_proposals_evidence
    ON memory_change_proposals(evidence_message_id);
CREATE TABLE IF NOT EXISTS memory_proposal_change_log (
    id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL REFERENCES memory_change_proposals(id) ON DELETE CASCADE,
    action TEXT NOT NULL CHECK (action IN ('created', 'accepted', 'rejected', 'cancelled', 'expired')),
    old_status TEXT,
    new_status TEXT NOT NULL,
    reason TEXT,
    actor_type TEXT NOT NULL CHECK (actor_type IN ('user', 'system_rule', 'admin')),
    idempotency_key TEXT,
    request_fingerprint TEXT,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_proposal_log_idempotency
    ON memory_proposal_change_log(idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_memory_proposal_log_proposal_created
    ON memory_proposal_change_log(proposal_id, created_at, id);
""",
6: """
ALTER TABLE memory_change_proposals ADD COLUMN application_status TEXT NOT NULL DEFAULT 'not_applied' CHECK (application_status IN ('not_applied', 'applied'));
ALTER TABLE memory_change_proposals ADD COLUMN target_memory_updated_at TEXT;
ALTER TABLE memory_change_proposals ADD COLUMN applied_at TEXT;
ALTER TABLE memory_change_proposals ADD COLUMN applied_memory_id TEXT REFERENCES memory_items(id) ON DELETE SET NULL;
ALTER TABLE memory_change_proposals ADD COLUMN replacement_memory_id TEXT REFERENCES memory_items(id) ON DELETE SET NULL;
ALTER TABLE memory_change_log ADD COLUMN proposal_id TEXT REFERENCES memory_change_proposals(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_memory_proposals_application_status
    ON memory_change_proposals(application_status, status, created_at);
CREATE TABLE IF NOT EXISTS memory_proposal_application_log (
    id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL UNIQUE REFERENCES memory_change_proposals(id) ON DELETE CASCADE,
    application_status TEXT NOT NULL CHECK (application_status IN ('applied')),
    applied_memory_id TEXT REFERENCES memory_items(id) ON DELETE SET NULL,
    replacement_memory_id TEXT REFERENCES memory_items(id) ON DELETE SET NULL,
    actor_type TEXT NOT NULL CHECK (actor_type IN ('user', 'system_rule', 'admin')),
    reason TEXT,
    idempotency_key TEXT,
    request_fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_proposal_application_idempotency
    ON memory_proposal_application_log(idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_memory_proposal_application_created
    ON memory_proposal_application_log(created_at, id);
CREATE INDEX IF NOT EXISTS idx_memory_change_log_proposal
    ON memory_change_log(proposal_id, created_at, id);
""",
7: """
CREATE TABLE IF NOT EXISTS memory_correction_clarifications (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    trigger_message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    source_proposal_id TEXT REFERENCES memory_change_proposals(id) ON DELETE SET NULL,
    conflict_context TEXT NOT NULL CHECK (json_valid(conflict_context) AND json_type(conflict_context) = 'object'),
    assistant_message_id TEXT REFERENCES messages(id) ON DELETE CASCADE,
    response_message_id TEXT REFERENCES messages(id) ON DELETE CASCADE,
    application_proposal_id TEXT REFERENCES memory_change_proposals(id) ON DELETE RESTRICT,
    status TEXT NOT NULL CHECK (status IN (
        'preparing', 'awaiting_user', 'processing', 'continue', 'deferred',
        'explained', 'denied', 'temporary', 'applied', 'failed'
    )),
    version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
    processing_token TEXT,
    lease_expires_at TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        (status = 'processing' AND processing_token IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR (status <> 'processing' AND processing_token IS NULL AND lease_expires_at IS NULL)
    )
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_clarifications_source_proposal
    ON memory_correction_clarifications(source_proposal_id) WHERE source_proposal_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_clarifications_assistant_message
    ON memory_correction_clarifications(assistant_message_id) WHERE assistant_message_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_clarifications_response_message
    ON memory_correction_clarifications(response_message_id) WHERE response_message_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_clarifications_application_proposal
    ON memory_correction_clarifications(application_proposal_id) WHERE application_proposal_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_memory_clarifications_conversation_status
    ON memory_correction_clarifications(conversation_id, status, updated_at, id);
CREATE INDEX IF NOT EXISTS idx_memory_clarifications_recovery
    ON memory_correction_clarifications(status, lease_expires_at, updated_at);
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
                    # Keep each migration and its marker atomic. If any DDL or
                    # the marker insert fails, the connection context rolls
                    # back the whole migration instead of leaving a half-schema.
                    connection.executescript(
                        "BEGIN;\n"
                        + sql
                        + "\nINSERT INTO schema_migrations(version, applied_at) VALUES ("
                        + str(version)
                        + ", datetime('now'));\nCOMMIT;"
                    )

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

