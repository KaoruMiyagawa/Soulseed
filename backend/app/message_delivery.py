"""Fast, idempotent persistence for client ordered user message streams."""

from hashlib import sha256
from collections.abc import Callable
from sqlite3 import Connection
from uuid import uuid4

from fastapi import HTTPException

from .db import Database
from .ownership import current_character
from .repositories import VISIBLE_MESSAGE, now_iso


class MessageDeliveryRepository:
    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _view(row, *, duplicate: bool) -> dict:
        return {
            "client_message_id": row["client_message_id"],
            "message_id": row["message_id"],
            "conversation_id": row["conversation_id"],
            "role": row["role"],
            "content": row["content"],
            "created_at": row["created_at"],
            "server_sequence": row["server_sequence"],
            "client_stream_id": row["client_stream_id"],
            "client_sequence": row["client_sequence"],
            "save_status": row["save_status"],
            "role_status": row["role_status"],
            "duplicate": duplicate,
        }

    def submit(self, *, user_id: str, character_id: str, conversation_id: str,
               client_message_id: str, client_stream_id: str, client_sequence: int,
               content: str, admit: Callable[[Connection, str], None] | None = None) -> dict:
        character = current_character.get()
        if character != character_id:
            raise HTTPException(403, "当前会话无权访问该角色")
        content_hash = sha256(content.encode("utf-8")).hexdigest()
        at = now_iso()
        with self.database.atomic() as db:
            conversation = db.execute(
                "SELECT id FROM conversations WHERE id=? AND character_id=? AND character_id=current_character()",
                (conversation_id, character_id),
            ).fetchone()
            if conversation is None:
                raise HTTPException(404, "会话不存在")

            prior = db.execute(
                """SELECT s.*,m.content,m.created_at,m.server_sequence
                   FROM client_message_submissions s JOIN messages m ON m.id=s.message_id
                   WHERE s.user_id=? AND s.client_message_id=?""",
                (user_id, client_message_id),
            ).fetchone()
            if prior is not None:
                same_binding = (
                    prior["character_id"] == character_id
                    and prior["conversation_id"] == conversation_id
                    and prior["role"] == "user"
                    and prior["client_stream_id"] == client_stream_id
                    and prior["client_sequence"] == client_sequence
                )
                if not same_binding or prior["content_sha256"] != content_hash:
                    raise HTTPException(409, "client_message_id 已绑定到不同消息或发送流")
                return self._view(prior, duplicate=True)

            if admit is not None:
                admit(db, conversation_id)

            stream = db.execute(
                "SELECT * FROM client_message_streams WHERE user_id=? AND client_stream_id=?",
                (user_id, client_stream_id),
            ).fetchone()
            expected = 1 if stream is None else stream["next_client_sequence"]
            if stream is not None and (
                stream["character_id"] != character_id
                or stream["conversation_id"] != conversation_id
            ):
                raise HTTPException(409, "client_stream_id 已绑定到其他角色或会话")
            if client_sequence != expected:
                raise HTTPException(
                    409,
                    {"code": "client_sequence_gap", "expected_client_sequence": expected,
                     "received_client_sequence": client_sequence},
                )

            server_sequence = db.execute(
                "SELECT COALESCE(MAX(server_sequence),0)+1 FROM messages WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()[0]
            message_id = str(uuid4())
            db.execute(
                "INSERT INTO messages(id,conversation_id,role,content,created_at,server_sequence) VALUES(?,?,'user',?,?,?)",
                (message_id, conversation_id, content, at, server_sequence),
            )
            db.execute(
                """INSERT INTO client_message_submissions(
                       user_id,client_message_id,character_id,conversation_id,role,client_stream_id,
                       client_sequence,content_sha256,message_id,save_status,role_status,created_at
                   ) VALUES(?,?,?,?,'user',?,?,?,?, 'saved','waiting',?)""",
                (user_id, client_message_id, character_id, conversation_id, client_stream_id,
                 client_sequence, content_hash, message_id, at),
            )
            if stream is None:
                db.execute(
                    "INSERT INTO client_message_streams(user_id,character_id,conversation_id,client_stream_id,next_client_sequence,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (user_id, character_id, conversation_id, client_stream_id, client_sequence + 1, at, at),
                )
            else:
                db.execute(
                    "UPDATE client_message_streams SET next_client_sequence=?,updated_at=? WHERE user_id=? AND client_stream_id=?",
                    (client_sequence + 1, at, user_id, client_stream_id),
                )
            db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (at, conversation_id))
            row = db.execute(
                """SELECT s.*,m.content,m.created_at,m.server_sequence
                   FROM client_message_submissions s JOIN messages m ON m.id=s.message_id
                   WHERE s.user_id=? AND s.client_message_id=?""",
                (user_id, client_message_id),
            ).fetchone()
            return self._view(row, duplicate=False)

    def lookup(self, *, user_id: str, character_id: str, conversation_id: str,
               client_message_id: str) -> dict:
        with self.database.connection() as db:
            row = db.execute(
                """SELECT s.*,m.content,m.created_at,m.server_sequence,
                          r.id AS reply_id,r.content AS reply_content,r.created_at AS reply_created_at,
                          r.server_sequence AS reply_server_sequence
                   FROM client_message_submissions s JOIN messages m ON m.id=s.message_id
                   JOIN conversations c ON c.id=s.conversation_id
                   JOIN characters ch ON ch.id=c.character_id AND ch.owner_user_id=s.user_id
                     AND ch.ownership_status='owned'
                   LEFT JOIN messages r ON r.user_message_id=s.message_id AND s.role_status='completed'
                   WHERE s.user_id=? AND s.character_id=? AND s.conversation_id=?
                     AND s.client_message_id=? AND c.character_id=s.character_id
                     AND c.character_id=current_character()""",
                (user_id, character_id, conversation_id, client_message_id),
            ).fetchone()
        if row is None:
            raise HTTPException(404, "消息保存记录不存在")
        reply = None
        if row["reply_id"] is not None:
            reply = {
                "id": row["reply_id"], "role": "assistant", "content": row["reply_content"],
                "conversation_id": conversation_id, "user_message_id": row["message_id"],
                "created_at": row["reply_created_at"], "server_sequence": row["reply_server_sequence"],
            }
        return {
            **self._view(row, duplicate=True), "reply": reply,
            "reply_error_code": row["reply_error_code"], "reply_started_at": row["reply_started_at"],
            "model_started_at": row["model_started_at"], "reply_lease_until": row["reply_lease_until"],
        }

    def feed(self, *, user_id: str, character_id: str, conversation_id: str,
             after_sequence: int, limit: int) -> dict:
        with self.database.connection() as db:
            owner = db.execute(
                """SELECT 1 FROM conversations c JOIN characters ch ON ch.id=c.character_id
                   WHERE c.id=? AND c.character_id=? AND c.character_id=current_character()
                     AND ch.owner_user_id=? AND ch.ownership_status='owned'""",
                (conversation_id, character_id, user_id),
            ).fetchone()
            if owner is None:
                raise HTTPException(404, "会话不存在")
            rows = db.execute(
                f"""SELECT m.id,m.role,m.conversation_id,m.content,m.created_at,m.server_sequence,m.user_message_id,
                           s.client_message_id,s.client_stream_id,s.client_sequence,s.role_status,s.reply_error_code
                    FROM messages m LEFT JOIN client_message_submissions s
                        ON s.message_id=m.id AND s.user_id=? AND s.character_id=current_character()
                    WHERE m.conversation_id=? AND m.server_sequence>? AND {VISIBLE_MESSAGE}
                    ORDER BY m.server_sequence LIMIT ?""",
                (user_id, conversation_id, after_sequence, limit + 1),
            ).fetchall()
        page = [dict(row) for row in rows[:limit]]
        return {"messages": page, "has_more": len(rows) > limit,
                "next_after_sequence": page[-1]["server_sequence"] if page else after_sequence}
