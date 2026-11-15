from datetime import UTC, datetime
from uuid import uuid4

from .db import Database


# Shared by the transcript, model context and incremental feed.
VISIBLE_MESSAGE = """NOT EXISTS (
    SELECT 1 FROM proactive_messages p JOIN contact_intents i ON i.id=p.intent_id
    WHERE p.chat_message_id=m.id AND (i.ownership_verified=0 OR i.character_id IS NOT current_character())
)"""


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


class ConversationRepository:
    def __init__(self, database: Database):
        self.database = database

    def create(self, title: str = "新会话") -> dict:
        conversation_id, timestamp = str(uuid4()), now_iso()
        with self.database.connection() as db:
            db.execute("INSERT INTO conversations(id, title, created_at, updated_at, character_id) VALUES (?, ?, ?, ?, current_character())", (conversation_id, title, timestamp, timestamp))
        return self.get(conversation_id)

    def get(self, conversation_id: str) -> dict:
        with self.database.connection() as db:
            row = db.execute("SELECT id, title, created_at, updated_at, character_id FROM conversations WHERE id = ? AND character_id = current_character()", (conversation_id,)).fetchone()
        if row is None:
            raise KeyError(conversation_id)
        return dict(row)

    def list(self) -> list[dict]:
        with self.database.connection() as db:
            rows = db.execute("SELECT id, title, created_at, updated_at, character_id FROM conversations WHERE character_id = current_character() ORDER BY updated_at DESC").fetchall()
        return [dict(row) for row in rows]

    def touch(self, conversation_id: str) -> None:
        with self.database.connection() as db:
            db.execute("UPDATE conversations SET updated_at = ? WHERE id = ? AND character_id = current_character()", (now_iso(), conversation_id))


class MessageRepository:
    def __init__(self, database: Database):
        self.database = database

    def add(self, conversation_id: str, role: str, content: str) -> dict:
        ConversationRepository(self.database).get(conversation_id)
        message = {"id": str(uuid4()), "conversation_id": conversation_id, "role": role, "content": content, "created_at": now_iso()}
        with self.database.atomic() as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
            if "server_sequence" in columns:
                sequence = db.execute("SELECT COALESCE(MAX(server_sequence), 0) + 1 FROM messages WHERE conversation_id=?", (conversation_id,)).fetchone()[0]
                db.execute("INSERT INTO messages(id, conversation_id, role, content, created_at, server_sequence) VALUES (:id, :conversation_id, :role, :content, :created_at, :server_sequence)", {**message, "server_sequence": sequence})
                message["server_sequence"] = sequence
            else:
                db.execute("INSERT INTO messages(id, conversation_id, role, content, created_at) VALUES (:id, :conversation_id, :role, :content, :created_at)", message)
        return message

    def list_for_conversation(self, conversation_id: str) -> list[dict]:
        ConversationRepository(self.database).get(conversation_id)
        with self.database.connection() as db:
            rows = db.execute(
                f"""SELECT id, conversation_id, role, content, created_at FROM messages m
                    WHERE conversation_id=? AND {VISIBLE_MESSAGE} ORDER BY server_sequence, created_at, id""",
                (conversation_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def history_before(self, conversation_id: str, user_message_id: str) -> list[dict]:
        ConversationRepository(self.database).get(conversation_id)
        with self.database.connection() as db:
            rows = db.execute(
                f"""SELECT m.role,m.content FROM messages m
                    JOIN messages current ON current.id=? AND current.conversation_id=m.conversation_id
                    LEFT JOIN messages source ON source.id=m.user_message_id
                    WHERE m.conversation_id=? AND {VISIBLE_MESSAGE}
                      AND COALESCE(source.server_sequence,m.server_sequence)<current.server_sequence
                    ORDER BY COALESCE(source.server_sequence,m.server_sequence),m.server_sequence""",
                (user_message_id, conversation_id),
            ).fetchall()
        return [dict(row) for row in rows]

