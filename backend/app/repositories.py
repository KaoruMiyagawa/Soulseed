from datetime import UTC, datetime
from uuid import uuid4

from .db import Database


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


class ConversationRepository:
    def __init__(self, database: Database):
        self.database = database

    def create(self, title: str = "新会话") -> dict:
        conversation_id, timestamp = str(uuid4()), now_iso()
        with self.database.connection() as db:
            db.execute("INSERT INTO conversations(id, title, created_at, updated_at) VALUES (?, ?, ?, ?)", (conversation_id, title, timestamp, timestamp))
        return self.get(conversation_id)

    def get(self, conversation_id: str) -> dict:
        with self.database.connection() as db:
            row = db.execute("SELECT id, title, created_at, updated_at FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
        if row is None:
            raise KeyError(conversation_id)
        return dict(row)

    def list(self) -> list[dict]:
        with self.database.connection() as db:
            rows = db.execute("SELECT id, title, created_at, updated_at FROM conversations ORDER BY updated_at DESC").fetchall()
        return [dict(row) for row in rows]

    def touch(self, conversation_id: str) -> None:
        with self.database.connection() as db:
            db.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now_iso(), conversation_id))


class MessageRepository:
    def __init__(self, database: Database):
        self.database = database

    def add(self, conversation_id: str, role: str, content: str) -> dict:
        message = {"id": str(uuid4()), "conversation_id": conversation_id, "role": role, "content": content, "created_at": now_iso()}
        with self.database.connection() as db:
            db.execute("INSERT INTO messages(id, conversation_id, role, content, created_at) VALUES (:id, :conversation_id, :role, :content, :created_at)", message)
        return message

    def list_for_conversation(self, conversation_id: str) -> list[dict]:
        with self.database.connection() as db:
            rows = db.execute("SELECT id, conversation_id, role, content, created_at FROM messages WHERE conversation_id = ? ORDER BY created_at, id", (conversation_id,)).fetchall()
        return [dict(row) for row in rows]

