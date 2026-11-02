"""Local pairing sessions; no client-supplied user identity or embedded secret."""

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException

from .db import Database

COOKIE = "soulseed_session"
ALLOWED_ORIGINS = {"http://127.0.0.1:5174", "http://localhost:5174"}


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class LocalAuth:
    def __init__(self, database: Database, pairing_secret: str | None):
        self.database, self.pairing_secret = database, pairing_secret

    def pair(self, supplied: str, character_id: str = "big_fish") -> tuple[str, dict]:
        if not self.pairing_secret:
            raise HTTPException(503, "尚未配置本地配对口令，请查看本地启动说明")
        now = datetime.now(UTC)
        with self.database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            state = db.execute("SELECT * FROM auth_login_throttle WHERE id=1").fetchone()
            if state and state["blocked_until"] and state["blocked_until"] > now.isoformat():
                raise HTTPException(429, "配对尝试过多，请稍后再试")
            valid = hmac.compare_digest(digest(supplied), digest(self.pairing_secret))
            if not valid:
                failures = state["failures"] + 1 if state and not state["blocked_until"] else 1
                blocked = (now + timedelta(minutes=5)).isoformat() if failures >= 5 else None
                db.execute("INSERT INTO auth_login_throttle VALUES (1, ?, ?) ON CONFLICT(id) DO UPDATE SET failures=excluded.failures, blocked_until=excluded.blocked_until", (failures, blocked))
            else:
                db.execute("DELETE FROM auth_login_throttle WHERE id=1")
                # Pairing authenticates the configured local user, not an owner_id from the request.
                character = db.execute("SELECT id FROM characters WHERE id=? AND owner_user_id='local' AND ownership_status='owned'", (character_id,)).fetchone()
                if not character:
                    raise HTTPException(403, "无权访问该角色")
                token = secrets.token_urlsafe(32)
                expires = now + timedelta(hours=8)
                db.execute("DELETE FROM auth_sessions WHERE expires_at <= ?", (now.isoformat(),))
                db.execute("INSERT INTO auth_sessions VALUES (?, 'local', ?, ?, ?)", (digest(token), character_id, now.isoformat(), expires.isoformat()))
        if not valid:
            raise HTTPException(401, "配对口令不正确")
        return token, {"user_id": "local", "character_id": character_id, "expires_at": expires.isoformat()}

    def authenticate(self, token: str | None) -> dict:
        if not self.pairing_secret or not token:
            raise HTTPException(401, "请先完成本地配对")
        with self.database.connection() as db:
            row = db.execute("SELECT s.user_id, s.character_id, s.expires_at FROM auth_sessions s JOIN characters c ON c.id=s.character_id WHERE s.token_hash=? AND s.expires_at>? AND c.owner_user_id=s.user_id AND c.ownership_status='owned'", (digest(token), datetime.now(UTC).isoformat())).fetchone()
        if not row:
            raise HTTPException(401, "会话已失效，请重新配对")
        return dict(row)

    def logout(self, token: str | None) -> None:
        if token:
            with self.database.connection() as db:
                db.execute("DELETE FROM auth_sessions WHERE token_hash=?", (digest(token),))
