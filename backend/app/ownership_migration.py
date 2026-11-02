"""Migration 12: preserve unknown legacy ownership as NULL, never guess it."""

MEMORY_TABLES = (
    "memory_items", "memory_sources", "memory_change_log", "memory_change_proposals",
    "memory_proposal_change_log", "memory_proposal_application_log",
    "memory_correction_clarifications",
)

SQL = """
CREATE TABLE users (id TEXT PRIMARY KEY, created_at TEXT NOT NULL);
INSERT INTO users VALUES ('local', datetime('now'));
CREATE TABLE characters (
    id TEXT PRIMARY KEY,
    owner_user_id TEXT REFERENCES users(id) ON DELETE RESTRICT,
    ownership_status TEXT NOT NULL CHECK (ownership_status IN ('owned', 'quarantined')),
    created_at TEXT NOT NULL,
    CHECK ((ownership_status = 'owned' AND owner_user_id IS NOT NULL)
        OR (ownership_status = 'quarantined' AND owner_user_id IS NULL))
);
INSERT INTO characters VALUES ('big_fish', 'local', 'owned', datetime('now'));
INSERT OR IGNORE INTO characters
    SELECT character_id, NULL, 'quarantined', datetime('now') FROM (
        SELECT character_id FROM life_states UNION SELECT character_id FROM life_events
        UNION SELECT character_id FROM contact_preferences UNION SELECT character_id FROM contact_intents
    );
CREATE INDEX idx_characters_owner ON characters(owner_user_id, ownership_status, id);
-- Optional, explicitly confirmed offline manifest. Old proactive delivery is NOT proof of ownership.
CREATE TABLE IF NOT EXISTS legacy_conversation_ownership (
    conversation_id TEXT PRIMARY KEY, character_id TEXT NOT NULL,
    evidence TEXT NOT NULL CHECK (length(trim(evidence)) > 0)
);
ALTER TABLE conversations ADD COLUMN character_id TEXT REFERENCES characters(id) ON DELETE RESTRICT;
UPDATE conversations SET character_id = (
    SELECT character_id FROM legacy_conversation_ownership WHERE conversation_id=conversations.id AND length(trim(evidence)) > 0
) WHERE id IN (SELECT conversation_id FROM legacy_conversation_ownership WHERE length(trim(evidence)) > 0);
CREATE INDEX idx_conversations_character ON conversations(character_id, updated_at, id);
ALTER TABLE contact_intents ADD COLUMN ownership_verified INTEGER NOT NULL DEFAULT 0 CHECK (ownership_verified IN (0,1));
UPDATE contact_intents SET ownership_verified=1 WHERE
    source='reminder' OR
    (source='life_share' AND character_id=(SELECT character_id FROM life_events WHERE id=event_id)
        AND character_id=(SELECT character_id FROM life_share_intents WHERE id=share_intent_id)) OR
    (source IN ('smalltalk','conversation_followup') AND character_id=(
        SELECT c.character_id FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE m.id=source_id AND m.role='user'
    ));
UPDATE contact_intents SET ownership_verified=0 WHERE EXISTS (
    SELECT 1 FROM proactive_messages p LEFT JOIN conversations c ON c.id=p.conversation_id
    WHERE p.intent_id=contact_intents.id AND p.conversation_id IS NOT NULL
        AND c.character_id IS NOT contact_intents.character_id
);
"""
for table in MEMORY_TABLES:
    SQL += f"ALTER TABLE {table} ADD COLUMN character_id TEXT REFERENCES characters(id) ON DELETE RESTRICT;\n"
    SQL += f"CREATE INDEX idx_{table}_character ON {table}(character_id, id);\n"

SQL += """
UPDATE memory_items SET character_id = (
    SELECT MIN(c.character_id) FROM memory_sources s JOIN messages m ON m.id=s.message_id
    JOIN conversations c ON c.id=m.conversation_id WHERE s.memory_id=memory_items.id
) WHERE id IN (
    SELECT s.memory_id FROM memory_sources s JOIN messages m ON m.id=s.message_id
    JOIN conversations c ON c.id=m.conversation_id GROUP BY s.memory_id
    HAVING COUNT(*)=COUNT(c.character_id) AND COUNT(DISTINCT c.character_id)=1
);
UPDATE memory_items SET character_id=NULL WHERE superseded_by_id IS NOT NULL
    AND character_id IS NOT (SELECT r.character_id FROM memory_items r WHERE r.id=memory_items.superseded_by_id);
UPDATE memory_change_proposals SET character_id = (
    SELECT c.character_id FROM messages m JOIN conversations c ON c.id=m.conversation_id
    WHERE m.id=memory_change_proposals.evidence_message_id
) WHERE evidence_message_id IS NOT NULL AND (
    target_memory_id IS NULL OR
    (SELECT character_id FROM memory_items WHERE id=target_memory_id) =
    (SELECT c.character_id FROM messages m JOIN conversations c ON c.id=m.conversation_id
        WHERE m.id=memory_change_proposals.evidence_message_id)
);
UPDATE memory_change_proposals SET character_id=NULL WHERE
    (applied_memory_id IS NOT NULL AND character_id IS NOT (SELECT character_id FROM memory_items WHERE id=applied_memory_id)) OR
    (replacement_memory_id IS NOT NULL AND character_id IS NOT (SELECT character_id FROM memory_items WHERE id=replacement_memory_id));
UPDATE memory_correction_clarifications SET character_id =
    (SELECT character_id FROM conversations WHERE id=conversation_id)
WHERE (source_proposal_id IS NULL OR
    (SELECT character_id FROM memory_change_proposals WHERE id=source_proposal_id) =
    (SELECT character_id FROM conversations WHERE id=conversation_id))
AND (application_proposal_id IS NULL OR
    (SELECT character_id FROM memory_change_proposals WHERE id=application_proposal_id) =
    (SELECT character_id FROM conversations WHERE id=conversation_id));
UPDATE memory_correction_clarifications SET character_id=NULL WHERE EXISTS (
    SELECT 1 FROM json_each(conflict_context, '$.matched_memories') snapshot
    LEFT JOIN memory_items m ON m.id=json_extract(snapshot.value, '$.id')
    WHERE m.character_id IS NOT memory_correction_clarifications.character_id
);
UPDATE memory_sources SET character_id=(SELECT character_id FROM memory_items WHERE id=memory_id)
WHERE (SELECT character_id FROM memory_items WHERE id=memory_id)=
    (SELECT c.character_id FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE m.id=message_id);
UPDATE memory_change_log SET character_id=(SELECT character_id FROM memory_items WHERE id=memory_id);
UPDATE memory_change_log SET character_id=NULL WHERE
    (source_message_id IS NOT NULL AND character_id IS NOT (SELECT c.character_id FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE m.id=source_message_id)) OR
    (proposal_id IS NOT NULL AND character_id IS NOT (SELECT character_id FROM memory_change_proposals WHERE id=proposal_id));
UPDATE memory_proposal_change_log SET character_id=(SELECT character_id FROM memory_change_proposals WHERE id=proposal_id);
UPDATE memory_proposal_application_log SET character_id=(SELECT character_id FROM memory_change_proposals WHERE id=proposal_id);
UPDATE memory_proposal_application_log SET character_id=NULL WHERE
    (applied_memory_id IS NOT NULL AND character_id IS NOT (SELECT character_id FROM memory_items WHERE id=applied_memory_id)) OR
    (replacement_memory_id IS NOT NULL AND character_id IS NOT (SELECT character_id FROM memory_items WHERE id=replacement_memory_id));
DROP INDEX idx_memory_active_canonical_key;
CREATE UNIQUE INDEX idx_memory_active_canonical_key ON memory_items(character_id, canonical_key) WHERE status='active';
"""
for table, index in (
    ("memory_change_log", "idx_memory_change_log_idempotency"),
    ("memory_change_proposals", "idx_memory_proposals_idempotency"),
    ("memory_proposal_change_log", "idx_memory_proposal_log_idempotency"),
    ("memory_proposal_application_log", "idx_memory_proposal_application_idempotency"),
):
    SQL += f"DROP INDEX {index}; CREATE UNIQUE INDEX {index} ON {table}(character_id, idempotency_key) WHERE idempotency_key IS NOT NULL;\n"

# Existing NULL rows remain intact for explicit, offline ownership reconciliation.
# All new writes require ownership; an existing owner cannot be silently changed.
for table in ("conversations", *MEMORY_TABLES):
    SQL += f"""
CREATE TRIGGER {table}_ownership_insert BEFORE INSERT ON {table}
WHEN NEW.character_id IS NULL BEGIN SELECT RAISE(ABORT, 'character ownership required'); END;
CREATE TRIGGER {table}_ownership_update BEFORE UPDATE OF character_id ON {table}
WHEN NEW.character_id IS NOT OLD.character_id BEGIN SELECT RAISE(ABORT, 'ownership is immutable'); END;
CREATE TRIGGER {table}_scope_insert BEFORE INSERT ON {table}
WHEN NEW.character_id IS NOT current_character() BEGIN SELECT RAISE(ABORT, 'wrong character scope'); END;
CREATE TRIGGER {table}_scope_update BEFORE UPDATE ON {table}
WHEN OLD.character_id IS NOT current_character() BEGIN SELECT RAISE(ABORT, 'wrong character scope'); END;
CREATE TRIGGER {table}_scope_delete BEFORE DELETE ON {table}
WHEN OLD.character_id IS NOT current_character() BEGIN SELECT RAISE(ABORT, 'wrong character scope'); END;
"""

# Relational guards protect writes made outside repository methods too.
RELATIONS = {
    "memory_items": (("superseded_by_id", "memory_items"),),
    "memory_sources": (("memory_id", "memory_items"), ("message_id", "messages")),
    "memory_change_log": (("memory_id", "memory_items"), ("source_message_id", "messages"), ("proposal_id", "memory_change_proposals")),
    "memory_change_proposals": (("target_memory_id", "memory_items"), ("evidence_message_id", "messages"), ("applied_memory_id", "memory_items"), ("replacement_memory_id", "memory_items")),
    "memory_proposal_change_log": (("proposal_id", "memory_change_proposals"),),
    "memory_proposal_application_log": (("proposal_id", "memory_change_proposals"), ("applied_memory_id", "memory_items"), ("replacement_memory_id", "memory_items")),
    "memory_correction_clarifications": (("conversation_id", "conversations"), ("trigger_message_id", "messages"), ("assistant_message_id", "messages"), ("response_message_id", "messages"), ("source_proposal_id", "memory_change_proposals"), ("application_proposal_id", "memory_change_proposals")),
    "contact_intents": (("event_id", "life_events"), ("share_intent_id", "life_share_intents")),
    "life_share_intents": (("event_id", "life_events"),),
    "life_preference_changes": (("event_id", "life_events"),),
    "life_share_decisions": (("event_id", "life_events"),),
    "life_activity_transitions": (("event_id", "life_events"),),
    "life_states": (("current_activity_event_id", "life_events"),),
    "life_events": (("state_id", "life_states"),),
}
for table, relations in RELATIONS.items():
    checks = []
    for column, parent in relations:
        owner = (f"SELECT c.character_id FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE m.id=NEW.{column}"
                 if parent == "messages" else f"SELECT character_id FROM {parent} WHERE {'character_id' if parent == 'life_states' else 'id'}=NEW.{column}")
        checks.append(f"(NEW.{column} IS NOT NULL AND NEW.character_id IS NOT ({owner}))")
    for operation in ("INSERT", "UPDATE"):
        SQL += f"CREATE TRIGGER {table}_references_{operation.lower()} BEFORE {operation} ON {table} WHEN " + " OR ".join(checks) + " BEGIN SELECT RAISE(ABORT, 'cross-character reference'); END;\n"

for table in ("life_states", "life_events", "life_preferences", "life_preference_changes", "life_activity_transitions", "life_advance_leases", "life_scheduler_ticks", "life_share_intents", "life_share_decisions", "contact_preferences", "contact_intents"):
    SQL += f"""
CREATE TRIGGER {table}_character_insert BEFORE INSERT ON {table}
WHEN NOT EXISTS (SELECT 1 FROM characters WHERE id=NEW.character_id)
BEGIN SELECT RAISE(ABORT, 'unknown character'); END;
CREATE TRIGGER {table}_character_update BEFORE UPDATE OF character_id ON {table}
WHEN NEW.character_id IS NOT OLD.character_id
BEGIN SELECT RAISE(ABORT, 'ownership is immutable'); END;
CREATE TRIGGER {table}_character_delete_guard BEFORE DELETE ON characters
WHEN EXISTS (SELECT 1 FROM {table} WHERE character_id=OLD.id)
BEGIN SELECT RAISE(ABORT, 'character has dependent data'); END;
"""

SQL += """
CREATE TRIGGER messages_scope_insert BEFORE INSERT ON messages
WHEN (SELECT character_id FROM conversations WHERE id=NEW.conversation_id) IS NOT current_character()
BEGIN SELECT RAISE(ABORT, 'wrong conversation scope'); END;
CREATE TRIGGER messages_scope_update BEFORE UPDATE ON messages
WHEN (SELECT character_id FROM conversations WHERE id=OLD.conversation_id) IS NOT current_character()
    OR NEW.conversation_id IS NOT OLD.conversation_id
BEGIN SELECT RAISE(ABORT, 'message ownership is immutable'); END;
CREATE TRIGGER contact_source_insert BEFORE INSERT ON contact_intents
WHEN NEW.source IN ('smalltalk','conversation_followup') AND NEW.character_id IS NOT
    (SELECT c.character_id FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE m.id=NEW.source_id AND m.role='user')
BEGIN SELECT RAISE(ABORT, 'cross-character contact source'); END;
CREATE TABLE auth_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX idx_auth_sessions_expiry ON auth_sessions(expires_at);
CREATE TABLE auth_login_throttle (id INTEGER PRIMARY KEY CHECK (id=1), failures INTEGER NOT NULL, blocked_until TEXT);
CREATE TRIGGER proactive_conversation_insert BEFORE INSERT ON proactive_messages
WHEN NEW.conversation_id IS NOT NULL AND
    (SELECT character_id FROM conversations WHERE id=NEW.conversation_id) IS NOT
    (SELECT character_id FROM contact_intents WHERE id=NEW.intent_id)
BEGIN SELECT RAISE(ABORT, 'cross-character delivery'); END;
CREATE TRIGGER proactive_conversation_update BEFORE UPDATE ON proactive_messages
WHEN NEW.conversation_id IS NOT NULL AND
    (SELECT character_id FROM conversations WHERE id=NEW.conversation_id) IS NOT
    (SELECT character_id FROM contact_intents WHERE id=NEW.intent_id)
BEGIN SELECT RAISE(ABORT, 'cross-character delivery'); END;
"""
