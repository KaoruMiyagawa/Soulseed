import hashlib
import os
import sqlite3
from contextvars import ContextVar
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
    duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
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
""",
8: """
CREATE TABLE IF NOT EXISTS life_states (
    character_id TEXT PRIMARY KEY,
    current_activity_event_id TEXT,
    mood TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS life_events (
    id TEXT PRIMARY KEY,
    character_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('completed', 'planned', 'imagined', 'wish')),
    occurred_at TEXT,
    planned_end_at TEXT,
    content TEXT NOT NULL,
    location TEXT,
    feeling TEXT,
    source TEXT NOT NULL,
    generation_method TEXT NOT NULL,
    state_id TEXT REFERENCES life_states(character_id) ON DELETE SET NULL,
    idempotency_key TEXT,
    created_at TEXT NOT NULL,
    CHECK ((status = 'completed' AND occurred_at IS NOT NULL) OR status <> 'completed')
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_life_event_idempotency
    ON life_events(character_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_life_events_character_time
    ON life_events(character_id, status, occurred_at, planned_end_at);
""",
9: """
ALTER TABLE life_events ADD COLUMN activity_status TEXT CHECK (activity_status IN ('planned', 'active', 'completed', 'cancelled'));
ALTER TABLE life_events ADD COLUMN planned_start_at TEXT;
ALTER TABLE life_events ADD COLUMN started_at TEXT;
ALTER TABLE life_events ADD COLUMN ended_at TEXT;
ALTER TABLE life_events ADD COLUMN selection_reason TEXT;
UPDATE life_events SET activity_status = CASE status WHEN 'planned' THEN 'planned' WHEN 'completed' THEN 'completed' ELSE NULL END;
UPDATE life_events SET started_at = occurred_at, ended_at = occurred_at WHERE status = 'completed';
UPDATE life_states SET current_activity_event_id = NULL;
CREATE TABLE life_activity_transitions (
    id TEXT PRIMARY KEY,
    character_id TEXT NOT NULL,
    event_id TEXT NOT NULL REFERENCES life_events(id) ON DELETE CASCADE,
    from_status TEXT,
    to_status TEXT NOT NULL,
    reason TEXT NOT NULL,
    changed_at TEXT NOT NULL
);
CREATE INDEX idx_life_transitions_event ON life_activity_transitions(event_id, changed_at, id);
CREATE TABLE life_preferences (
    character_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    weight REAL NOT NULL CHECK (weight >= 0.25 AND weight <= 2.0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(character_id, event_type)
);
CREATE TABLE life_preference_changes (
    id TEXT PRIMARY KEY,
    character_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    old_weight REAL NOT NULL,
    new_weight REAL NOT NULL,
    reason TEXT NOT NULL,
    event_id TEXT REFERENCES life_events(id) ON DELETE SET NULL,
    changed_at TEXT NOT NULL
);
CREATE INDEX idx_life_preference_changes_character ON life_preference_changes(character_id, changed_at, id);
CREATE UNIQUE INDEX idx_life_preference_once_per_event ON life_preference_changes(event_id, reason) WHERE event_id IS NOT NULL;
""",
10: """
ALTER TABLE life_states ADD COLUMN timezone_id TEXT NOT NULL DEFAULT 'Asia/Shanghai';
CREATE TABLE life_advance_leases (
    character_id TEXT PRIMARY KEY,
    token TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE life_scheduler_ticks (
    character_id TEXT PRIMARY KEY,
    last_tick_at TEXT NOT NULL
);
CREATE TABLE life_share_intents (
    id TEXT PRIMARY KEY,
    character_id TEXT NOT NULL,
    event_id TEXT NOT NULL UNIQUE REFERENCES life_events(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('pending', 'delivered', 'dismissed')),
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    offered_at TEXT,
    delivered_at TEXT,
    dismissed_at TEXT
);
CREATE INDEX idx_life_share_pending ON life_share_intents(character_id, status, created_at);
CREATE TABLE life_share_decisions (
    event_id TEXT PRIMARY KEY REFERENCES life_events(id) ON DELETE CASCADE,
    character_id TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('created', 'skipped')),
    reason TEXT NOT NULL,
    decided_at TEXT NOT NULL
);
""",
11: """
CREATE TABLE contact_preferences (
    character_id TEXT PRIMARY KEY,
    allowed INTEGER NOT NULL DEFAULT 1 CHECK (allowed IN (0, 1)),
    busy_until TEXT,
    quiet_start_minute INTEGER NOT NULL DEFAULT 1320 CHECK (quiet_start_minute BETWEEN 0 AND 1439),
    quiet_end_minute INTEGER NOT NULL DEFAULT 480 CHECK (quiet_end_minute BETWEEN 0 AND 1439),
    updated_at TEXT NOT NULL
);
CREATE TABLE contact_intents (
    id TEXT PRIMARY KEY,
    character_id TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('life_share', 'smalltalk', 'conversation_followup', 'reminder')),
    source_id TEXT NOT NULL,
    share_intent_id TEXT REFERENCES life_share_intents(id) ON DELETE SET NULL,
    event_id TEXT REFERENCES life_events(id) ON DELETE SET NULL,
    importance TEXT NOT NULL CHECK (importance IN ('normal', 'important')),
    reason TEXT NOT NULL,
    topic TEXT,
    status TEXT NOT NULL CHECK (status IN ('pending', 'generating', 'deferred', 'ready', 'delivered', 'dismissed')),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    retry_after TEXT,
    generation_token TEXT,
    lease_expires_at TEXT,
    UNIQUE(character_id, source, source_id)
);
CREATE INDEX idx_contact_intents_queue ON contact_intents(character_id, status, importance, created_at);
CREATE TABLE proactive_messages (
    id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL REFERENCES contact_intents(id) ON DELETE CASCADE,
    attempt_no INTEGER NOT NULL CHECK (attempt_no IN (0, 1)),
    content TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ready', 'delivered', 'read')),
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    read_at TEXT,
    conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
    chat_message_id TEXT UNIQUE REFERENCES messages(id) ON DELETE SET NULL,
    UNIQUE(intent_id, attempt_no)
);
CREATE INDEX idx_proactive_inbox ON proactive_messages(status, created_at, id);
"""}


from .ownership_migration import SQL as OWNERSHIP_MIGRATION

MIGRATIONS[12] = OWNERSHIP_MIGRATION

MIGRATIONS[13] = """
ALTER TABLE life_events ADD COLUMN runtime_owner_token TEXT;
ALTER TABLE life_events ADD COLUMN execution_status TEXT
    CHECK (execution_status IN ('running', 'interrupted'));
ALTER TABLE life_events ADD COLUMN interrupted_at TEXT;
UPDATE life_events SET execution_status = 'running' WHERE activity_status = 'active';
CREATE INDEX idx_life_running_owner ON life_events(runtime_owner_token, character_id, activity_status, execution_status);
CREATE TABLE life_activity_interruptions (
    event_id TEXT PRIMARY KEY REFERENCES life_events(id) ON DELETE CASCADE,
    character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE RESTRICT,
    instance_token TEXT,
    reason TEXT NOT NULL CHECK (reason IN ('service_stopped', 'owner_lost')),
    interrupted_at TEXT NOT NULL
);
CREATE TRIGGER life_activity_interruptions_owner_insert BEFORE INSERT ON life_activity_interruptions
WHEN NEW.character_id IS NOT (SELECT character_id FROM life_events WHERE id=NEW.event_id)
BEGIN SELECT RAISE(ABORT, 'cross-character interruption'); END;
CREATE TRIGGER life_activity_interruptions_owner_update BEFORE UPDATE ON life_activity_interruptions
WHEN NEW.character_id IS NOT OLD.character_id OR NEW.event_id IS NOT OLD.event_id
BEGIN SELECT RAISE(ABORT, 'interruption ownership is immutable'); END;
CREATE TABLE life_service_instances (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    token TEXT NOT NULL UNIQUE,
    process_id INTEGER NOT NULL,
    host_name TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
"""

MIGRATIONS[14] = """
ALTER TABLE contact_preferences ADD COLUMN pause_until TEXT;
ALTER TABLE life_share_intents ADD COLUMN delivery_channel TEXT
    CHECK (delivery_channel IN ('chat', 'contact'));
ALTER TABLE life_share_intents ADD COLUMN selection_token TEXT;
ALTER TABLE life_share_intents ADD COLUMN selection_expires_at TEXT;
ALTER TABLE life_share_intents ADD COLUMN chat_retry_after TEXT;
UPDATE life_share_intents SET delivery_channel = 'contact'
WHERE status = 'pending' AND EXISTS (
    SELECT 1 FROM contact_intents c WHERE c.share_intent_id = life_share_intents.id
      AND c.status IN ('pending', 'generating', 'deferred', 'ready')
);
UPDATE life_share_intents SET delivery_channel = 'chat', offered_at = NULL
WHERE status = 'pending' AND offered_at IS NOT NULL AND delivery_channel IS NULL;
CREATE INDEX idx_life_share_channel ON life_share_intents(character_id, status, delivery_channel, created_at);
"""

MIGRATIONS[15] = """
CREATE TABLE exploration_runs (
    id TEXT PRIMARY KEY,
    character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE RESTRICT,
    idempotency_key TEXT NOT NULL,
    requested_topic TEXT,
    selected_topic TEXT,
    topic_origin TEXT NOT NULL CHECK (topic_origin IN ('user', 'character')),
    status TEXT NOT NULL CHECK (status IN ('planned', 'active', 'completed', 'interrupted', 'failed', 'cancelled')),
    step_count INTEGER NOT NULL DEFAULT 0 CHECK (step_count >= 0),
    tool_call_count INTEGER NOT NULL DEFAULT 0 CHECK (tool_call_count >= 0),
    model_call_count INTEGER NOT NULL DEFAULT 0 CHECK (model_call_count >= 0),
    input_char_count INTEGER NOT NULL DEFAULT 0 CHECK (input_char_count >= 0),
    duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
    summary TEXT,
    claims_json TEXT,
    unanswered_json TEXT,
    error_code TEXT,
    life_event_id TEXT REFERENCES life_events(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(character_id, idempotency_key),
    UNIQUE(id, character_id)
);
CREATE INDEX idx_exploration_runs_character_created
    ON exploration_runs(character_id, created_at DESC, id);
CREATE INDEX idx_exploration_runs_recovery
    ON exploration_runs(status, started_at);
CREATE TABLE exploration_evidence (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    character_id TEXT NOT NULL,
    sequence_no INTEGER NOT NULL CHECK (sequence_no > 0),
    tool_name TEXT NOT NULL CHECK (tool_name IN ('search_web', 'read_webpage')),
    query_text TEXT,
    source_url TEXT,
    title TEXT,
    retrieved_at TEXT,
    excerpt TEXT,
    content_sha256 TEXT,
    outcome TEXT NOT NULL CHECK (outcome IN ('success', 'empty', 'failed')),
    error_code TEXT,
    duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
    created_at TEXT NOT NULL,
    FOREIGN KEY(run_id, character_id) REFERENCES exploration_runs(id, character_id) ON DELETE CASCADE,
    UNIQUE(run_id, sequence_no),
    UNIQUE(id, character_id)
);
CREATE INDEX idx_exploration_evidence_run ON exploration_evidence(run_id, sequence_no);
CREATE TRIGGER exploration_runs_owner_insert BEFORE INSERT ON exploration_runs
WHEN current_character() IS NULL OR NEW.character_id IS NOT current_character()
BEGIN SELECT RAISE(ABORT, 'cross-character exploration run'); END;
CREATE TRIGGER exploration_runs_owner_update BEFORE UPDATE ON exploration_runs
WHEN NEW.character_id IS NOT OLD.character_id OR NEW.id IS NOT OLD.id
  OR current_character() IS NULL OR NEW.character_id IS NOT current_character()
BEGIN SELECT RAISE(ABORT, 'exploration ownership is immutable'); END;
CREATE TRIGGER exploration_evidence_owner_insert BEFORE INSERT ON exploration_evidence
WHEN current_character() IS NULL OR NEW.character_id IS NOT current_character()
  OR NEW.character_id IS NOT (SELECT character_id FROM exploration_runs WHERE id=NEW.run_id)
BEGIN SELECT RAISE(ABORT, 'cross-character exploration evidence'); END;
CREATE TRIGGER exploration_evidence_owner_update BEFORE UPDATE ON exploration_evidence
WHEN NEW.character_id IS NOT OLD.character_id OR NEW.run_id IS NOT OLD.run_id
  OR current_character() IS NULL OR NEW.character_id IS NOT current_character()
BEGIN SELECT RAISE(ABORT, 'exploration evidence ownership is immutable'); END;
"""

MIGRATIONS[16] = """
ALTER TABLE exploration_evidence ADD COLUMN provider TEXT
    CHECK (provider IS NULL OR provider IN ('duckduckgo', 'wikimedia_zh', 'wikimedia_en', 'controlled', 'unknown'));
ALTER TABLE exploration_evidence ADD COLUMN relevance TEXT
    CHECK (relevance IS NULL OR relevance IN ('relevant', 'irrelevant'));
"""

MIGRATIONS[17] = """
ALTER TABLE exploration_runs ADD COLUMN trigger_mode TEXT NOT NULL DEFAULT 'manual'
    CHECK (trigger_mode IN ('manual', 'autonomous'));
ALTER TABLE exploration_runs ADD COLUMN topic_basis TEXT;
ALTER TABLE exploration_runs ADD COLUMN phase TEXT NOT NULL DEFAULT 'planned';
ALTER TABLE exploration_runs ADD COLUMN timings_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE exploration_evidence ADD COLUMN page_relevance TEXT
    CHECK (page_relevance IS NULL OR page_relevance IN ('relevant', 'irrelevant'));
CREATE TABLE exploration_autonomy (
    character_id TEXT PRIMARY KEY REFERENCES characters(id) ON DELETE CASCADE,
    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0,1)),
    paused_until TEXT,
    cooldown_hours INTEGER NOT NULL DEFAULT 24 CHECK (cooldown_hours BETWEEN 24 AND 720),
    last_started_at TEXT,
    last_run_id TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(last_run_id, character_id) REFERENCES exploration_runs(id, character_id) ON DELETE RESTRICT
);
"""

MIGRATIONS[18] = """
ALTER TABLE exploration_autonomy ADD COLUMN last_checked_at TEXT;
ALTER TABLE exploration_autonomy ADD COLUMN last_skip_reason TEXT;
"""

MIGRATIONS[19] = """
CREATE TABLE autonomous_contact_candidates (
    id TEXT PRIMARY KEY CHECK (length(id)=64),
    character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    candidate_type TEXT NOT NULL CHECK (candidate_type IN ('real_discovery','shared_topic')),
    source_key TEXT NOT NULL,
    evidence_json TEXT NOT NULL CHECK (json_valid(evidence_json) AND json_type(evidence_json)='array'),
    topic_fingerprint TEXT CHECK (topic_fingerprint IS NULL OR length(topic_fingerprint)=64),
    status TEXT NOT NULL CHECK (status IN ('pending','claimed','deferred','completed','expired','cancelled','failed','outcome_unknown')),
    reason_code TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    retry_after TEXT,
    claim_count INTEGER NOT NULL DEFAULT 0 CHECK (claim_count >= 0),
    lease_token TEXT,
    lease_expires_at TEXT,
    CHECK ((status='claimed' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR (status<>'claimed' AND lease_token IS NULL AND lease_expires_at IS NULL)),
    UNIQUE(id, character_id),
    UNIQUE(character_id, candidate_type, source_key)
);
CREATE INDEX idx_autonomous_candidates_recovery
    ON autonomous_contact_candidates(status, lease_expires_at, expires_at);
CREATE INDEX idx_autonomous_candidates_topic
    ON autonomous_contact_candidates(character_id, topic_fingerprint, created_at);

CREATE TABLE autonomous_contact_reservations (
    id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    attempt_no INTEGER NOT NULL CHECK (attempt_no >= 1),
    request_key TEXT NOT NULL UNIQUE,
    timezone_id TEXT NOT NULL,
    local_day TEXT NOT NULL,
    request_status TEXT NOT NULL CHECK (request_status IN ('reserved','started','settled','released','outcome_unknown')),
    message_status TEXT NOT NULL CHECK (message_status IN ('reserved','committed','released')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(candidate_id, character_id) REFERENCES autonomous_contact_candidates(id, character_id) ON DELETE RESTRICT,
    UNIQUE(candidate_id, attempt_no)
);
CREATE INDEX idx_autonomous_reservations_budget
    ON autonomous_contact_reservations(character_id, local_day, request_status, message_status);

CREATE TRIGGER autonomous_candidates_owner_insert BEFORE INSERT ON autonomous_contact_candidates
WHEN NEW.character_id IS NOT current_character()
  OR NOT EXISTS (SELECT 1 FROM characters WHERE id=NEW.character_id AND ownership_status='owned')
BEGIN SELECT RAISE(ABORT, 'autonomous candidate character is not owned in current scope'); END;
CREATE TRIGGER autonomous_candidates_owner_update BEFORE UPDATE ON autonomous_contact_candidates
WHEN NEW.character_id IS NOT current_character()
  OR (NOT EXISTS (SELECT 1 FROM characters WHERE id=NEW.character_id AND ownership_status='owned')
      AND NOT (NEW.character_id=OLD.character_id AND NEW.status IN ('cancelled','expired','failed','outcome_unknown')
        AND NEW.lease_token IS NULL AND NEW.lease_expires_at IS NULL))
BEGIN SELECT RAISE(ABORT, 'autonomous candidate character is not owned in current scope'); END;
CREATE TRIGGER autonomous_candidates_state_transition BEFORE UPDATE OF status ON autonomous_contact_candidates
WHEN NEW.status<>OLD.status AND NOT (
    (OLD.status='pending' AND NEW.status IN ('claimed','deferred','expired','cancelled','failed')) OR
    (OLD.status='deferred' AND NEW.status IN ('claimed','expired','cancelled','failed')) OR
    (OLD.status='claimed' AND NEW.status IN ('pending','deferred','completed','expired','cancelled','failed','outcome_unknown'))
)
BEGIN SELECT RAISE(ABORT, 'invalid autonomous candidate state transition'); END;
CREATE TRIGGER autonomous_reservations_owner_insert BEFORE INSERT ON autonomous_contact_reservations
WHEN NEW.character_id IS NOT current_character()
  OR NOT EXISTS (SELECT 1 FROM characters WHERE id=NEW.character_id AND ownership_status='owned')
  OR NOT EXISTS (SELECT 1 FROM autonomous_contact_candidates c WHERE c.id=NEW.candidate_id AND c.character_id=NEW.character_id)
BEGIN SELECT RAISE(ABORT, 'autonomous reservation character is not owned in current scope'); END;
CREATE TRIGGER autonomous_reservations_owner_update BEFORE UPDATE ON autonomous_contact_reservations
WHEN NEW.character_id IS NOT current_character()
  OR (NOT EXISTS (SELECT 1 FROM characters WHERE id=NEW.character_id AND ownership_status='owned')
      AND NOT (NEW.character_id=OLD.character_id AND NEW.request_status IN ('settled','released','outcome_unknown')
        AND NEW.message_status='released'))
  OR NOT EXISTS (SELECT 1 FROM autonomous_contact_candidates c WHERE c.id=NEW.candidate_id AND c.character_id=NEW.character_id)
BEGIN SELECT RAISE(ABORT, 'autonomous reservation character is not owned in current scope'); END;
"""

MIGRATIONS[20] = """
CREATE TABLE autonomous_contact_drafts (
    id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL UNIQUE,
    character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    reservation_id TEXT NOT NULL UNIQUE REFERENCES autonomous_contact_reservations(id) ON DELETE RESTRICT,
    evidence_json TEXT NOT NULL CHECK (json_valid(evidence_json) AND json_type(evidence_json)='array'),
    content TEXT NOT NULL CHECK (length(trim(content)) BETWEEN 1 AND 300),
    content_hash TEXT NOT NULL CHECK (length(content_hash)=64),
    review_status TEXT NOT NULL CHECK (review_status IN ('mock_approved','consumed','cancelled','expired')),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    UNIQUE(id, character_id),
    FOREIGN KEY(candidate_id, character_id) REFERENCES autonomous_contact_candidates(id, character_id) ON DELETE RESTRICT
);
CREATE INDEX idx_autonomous_drafts_recovery
    ON autonomous_contact_drafts(review_status, expires_at, character_id);
CREATE TRIGGER autonomous_drafts_owner_insert BEFORE INSERT ON autonomous_contact_drafts
WHEN NEW.character_id IS NOT current_character()
  OR NOT EXISTS (SELECT 1 FROM characters WHERE id=NEW.character_id AND ownership_status='owned')
  OR NOT EXISTS (SELECT 1 FROM autonomous_contact_candidates c WHERE c.id=NEW.candidate_id AND c.character_id=NEW.character_id)
  OR NOT EXISTS (SELECT 1 FROM autonomous_contact_reservations r WHERE r.id=NEW.reservation_id AND r.candidate_id=NEW.candidate_id AND r.character_id=NEW.character_id)
BEGIN SELECT RAISE(ABORT, 'autonomous draft character is not owned in current scope'); END;
CREATE TRIGGER autonomous_drafts_owner_update BEFORE UPDATE ON autonomous_contact_drafts
WHEN NEW.character_id IS NOT current_character()
  OR (NOT EXISTS (SELECT 1 FROM characters WHERE id=NEW.character_id AND ownership_status='owned')
      AND NOT (NEW.character_id=OLD.character_id AND NEW.review_status IN ('cancelled','expired')))
BEGIN SELECT RAISE(ABORT, 'autonomous draft character is not owned in current scope'); END;
CREATE TRIGGER autonomous_drafts_state_transition BEFORE UPDATE OF review_status ON autonomous_contact_drafts
WHEN NEW.review_status<>OLD.review_status AND NOT (
    OLD.review_status='mock_approved' AND NEW.review_status IN ('consumed','cancelled','expired')
)
BEGIN SELECT RAISE(ABORT, 'invalid autonomous draft state transition'); END;
CREATE TRIGGER autonomous_drafts_content_immutable BEFORE UPDATE OF
    candidate_id,character_id,reservation_id,evidence_json,content,content_hash,created_at,expires_at
ON autonomous_contact_drafts
BEGIN SELECT RAISE(ABORT, 'autonomous draft content is immutable'); END;
"""


MIGRATIONS[21] = """
CREATE TABLE model_budget_windows (
    day TEXT PRIMARY KEY,
    policy_json TEXT NOT NULL,
    history_ready INTEGER NOT NULL DEFAULT 0 CHECK(history_ready IN (0,1))
);
CREATE TABLE model_budget_requests (
    id TEXT PRIMARY KEY,
    character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE RESTRICT,
    source TEXT NOT NULL,
    day TEXT NOT NULL REFERENCES model_budget_windows(day),
    status TEXT NOT NULL CHECK(status IN ('reserved','started','completed','failed','unknown','released')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    lease_expires_at TEXT NOT NULL,
    autonomous_reservation_id TEXT UNIQUE REFERENCES autonomous_contact_reservations(id) ON DELETE RESTRICT
);
CREATE INDEX idx_model_budget_usage ON model_budget_requests(day,source,character_id,status);
CREATE INDEX idx_model_budget_recovery ON model_budget_requests(character_id,status,lease_expires_at);
CREATE TRIGGER model_budget_identity_immutable BEFORE UPDATE OF id,character_id,source,day,created_at,autonomous_reservation_id ON model_budget_requests
BEGIN SELECT RAISE(ABORT, 'model budget identity is immutable'); END;
CREATE TRIGGER model_budget_transition BEFORE UPDATE OF status ON model_budget_requests
WHEN NEW.status<>OLD.status AND NOT (
    (OLD.status='reserved' AND NEW.status IN ('started','released')) OR
    (OLD.status='started' AND NEW.status IN ('completed','failed','unknown'))
)
BEGIN SELECT RAISE(ABORT, 'invalid model budget transition'); END;
"""


class Database:
    def __init__(self, path: str, *, environment: str | None = None,
                 test_data_root: str | None = None, migration_backup_path: str | None = None,
                 migration_confirmation: str | None = None,
                 allow_protected_database_access: bool = False,
                 protected_database_confirmation: str | None = None):
        self.environment = environment or os.environ.get("SOULSEED_ENVIRONMENT", "development")
        if self.environment == "testing":
            if test_data_root is None:
                from .config import get_settings
                test_data_root = get_settings().test_data_root
            from .config import validate_test_database_path
            path = str(validate_test_database_path(path, test_data_root))
        self.path = path
        self.test_data_root = test_data_root
        self.migration_backup_path = migration_backup_path
        self.migration_confirmation = migration_confirmation
        self.allow_protected_database_access = allow_protected_database_access
        self.protected_database_confirmation = protected_database_confirmation
        self._active_transaction: ContextVar[sqlite3.Connection | None] = ContextVar(
            f"soulseed_transaction_{id(self)}", default=None
        )

    def _validate_path_before_access(self) -> None:
        if self.environment == "testing":
            from .config import validate_test_database_path
            self.path = str(validate_test_database_path(self.path, self.test_data_root))

    def _protected_database_target(self) -> Path | None:
        from .config import DEVELOPMENT_DATABASE_PATH, FORMAL_DATABASE_PATHS

        # The testing path has already passed the strict temporary-root fence.
        # Do not even stat the user's protected database while running tests.
        if self.environment == "testing":
            return None

        target = Path(self.path).expanduser().resolve(strict=False)
        for protected_path in FORMAL_DATABASE_PATHS:
            protected = Path(protected_path).expanduser().resolve(strict=False)
            if target == protected:
                return protected
            # File identity catches hard links and aliases that retain a
            # different path. This check reads metadata, not database content.
            try:
                if target.is_file() and protected.is_file() and os.path.samefile(target, protected):
                    return protected
            except OSError:
                return target

        if self.environment == "development":
            development_path = DEVELOPMENT_DATABASE_PATH.resolve(strict=False)
            if target != development_path:
                # A custom development path may contain user data or a
                # relocated copy, so it requires the same explicit approval.
                return target
        return None

    def _require_protected_database_access(self, protected: Path) -> None:
        confirmation = self.protected_database_confirmation
        confirmed_path = None
        if confirmation:
            try:
                confirmed_path = Path(confirmation).expanduser().resolve(strict=False)
            except (OSError, RuntimeError):
                confirmed_path = None
        if (not self.allow_protected_database_access or confirmed_path is None
                or os.path.normcase(str(confirmed_path)) != os.path.normcase(str(protected))):
            raise RuntimeError(
                "该路径属于受保护数据；需显式启用受保护数据库访问并确认其规范化绝对路径"
            )

    @staticmethod
    def _readonly_schema_version(path: Path) -> int:
        uri = path.resolve(strict=True).as_uri() + "?mode=ro"
        try:
            with sqlite3.connect(uri, uri=True, timeout=5) as db:
                integrity = db.execute("PRAGMA quick_check").fetchone()[0]
                if integrity != "ok":
                    raise RuntimeError("数据库或备份完整性检查失败")
                exists = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
                ).fetchone()
                if not exists:
                    return 0
                return int(db.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0])
        except sqlite3.Error as exc:
            raise RuntimeError("无法只读核验数据库迁移版本") from exc

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def _require_migration_approval(self) -> None:
        database = Path(self.path).resolve(strict=False)
        if not database.is_file():
            raise RuntimeError("受保护数据库不存在；自动建库和迁移已拒绝")
        current = self._readonly_schema_version(database)
        latest = max(MIGRATIONS)
        if current > latest:
            raise RuntimeError("数据库迁移版本高于当前程序支持版本")
        if current == latest:
            return

        confirmation = f"{current}->{latest}"
        if self.migration_confirmation != confirmation or not self.migration_backup_path:
            raise RuntimeError(
                f"数据库有待执行迁移 {confirmation}；需提供字节一致的独立备份并显式确认"
            )

        backup = Path(self.migration_backup_path).expanduser().resolve(strict=True)
        if not backup.is_file() or os.path.samefile(database, backup):
            raise RuntimeError("迁移备份必须是另一个现存普通文件")
        project_root = Path(__file__).resolve().parents[2]
        try:
            backup.relative_to(project_root)
        except ValueError:
            pass
        else:
            raise RuntimeError("production 迁移备份必须位于项目仓库之外")
        for suffix in ("-wal", "-shm", "-journal"):
            if Path(str(database) + suffix).exists() or Path(str(backup) + suffix).exists():
                raise RuntimeError("存在 SQLite sidecar 文件；须先完成一致性备份")
        if self._readonly_schema_version(backup) != current:
            raise RuntimeError("迁移备份的 schema 版本与正式库不一致")
        if self._sha256(database) != self._sha256(backup):
            raise RuntimeError("迁移备份哈希与待升级数据库不一致")

    @contextmanager
    def atomic(self) -> Iterator[sqlite3.Connection]:
        """One SQLite write transaction shared by nested repository calls."""
        active = self._active_transaction.get()
        if active is not None:
            yield active
            return
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            token = self._active_transaction.set(connection)
            try:
                yield connection
            finally:
                self._active_transaction.reset(token)

    def initialize(self) -> None:
        self._validate_path_before_access()
        protected = self._protected_database_target()
        if protected is not None:
            self._require_protected_database_access(protected)
        if self.environment == "production" or protected is not None:
            self._require_migration_approval()
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
        from .chat_diagnostics import measure
        with measure("sqlite"):
            with self._connection() as connection:
                yield connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        active = self._active_transaction.get()
        if active is not None:
            yield active
            return
        self._validate_path_before_access()
        protected = self._protected_database_target()
        if protected is not None:
            self._require_protected_database_access(protected)
        connection = sqlite3.connect(self.path, timeout=10)
        from .ownership import current_character
        scope = current_character.get()
        connection.create_function("current_character", 0, lambda: scope)
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

