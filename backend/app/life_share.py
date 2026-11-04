"""Durable intentions to mention real life events in a future conversation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from html import escape
from uuid import uuid4

from .life import LifeRepository, _iso


class LifeShareService:
    def __init__(self, repository: LifeRepository, *, minimum_gap: timedelta = timedelta(hours=6)):
        self.repository = repository
        self.minimum_gap = minimum_gap

    @staticmethod
    def relevant_chat(query: str) -> bool:
        if any(word in query for word in ("想你", "喜欢你", "爱你", "好累", "难过", "伤心", "烦")):
            return False
        return any(word in query for word in ("在干嘛", "干什么", "做什么", "今天", "最近", "有什么新鲜", "发现"))

    def consider(self, event_id: str, *, now: datetime | None = None) -> dict | None:
        at = _iso(now)
        with self.repository.database.connection() as db:
            if not db.in_transaction:
                db.execute("BEGIN IMMEDIATE")
            event = db.execute("SELECT * FROM life_events WHERE id = ? AND activity_status = 'completed' AND status = 'completed'", (event_id,)).fetchone()
            if not event:
                return None
            decision = db.execute("SELECT outcome FROM life_share_decisions WHERE event_id = ?", (event_id,)).fetchone()
            if decision:
                row = db.execute("SELECT i.*, e.content AS event_content, e.occurred_at FROM life_share_intents i JOIN life_events e ON e.id = i.event_id AND e.character_id=i.character_id WHERE i.event_id = ?", (event_id,)).fetchone()
                return dict(row) if row else None
            feeling = event["feeling"] or ""
            positive = any(word in feeling for word in ("开心", "喜欢", "有趣", "惊喜", "满足")) and "不开心" not in feeling
            kind = event["event_type"]
            pref = db.execute("SELECT weight FROM life_preferences WHERE character_id = ? AND event_type = ?", (event["character_id"], kind)).fetchone()
            liked = bool(pref and pref[0] >= 1.25)
            worthy = kind == "exploring" or kind == "cooking" and positive or kind in ("music", "reading") and positive and liked
            reason = "interesting_discovery" if kind == "exploring" else "positive_preferred_activity" if worthy else "ordinary_activity"
            last = db.execute("SELECT created_at FROM life_share_intents WHERE character_id = ? ORDER BY created_at DESC LIMIT 1", (event["character_id"],)).fetchone()
            if worthy and last and datetime.fromisoformat(at) - datetime.fromisoformat(last[0]) < self.minimum_gap:
                worthy, reason = False, "frequency_limit"
            outcome = "created" if worthy else "skipped"
            db.execute("INSERT INTO life_share_decisions(event_id, character_id, outcome, reason, decided_at) VALUES (?, ?, ?, ?, ?)", (event_id, event["character_id"], outcome, reason, at))
            if not worthy:
                return None
            intent_id = str(uuid4())
            db.execute("INSERT INTO life_share_intents(id, character_id, event_id, status, reason, created_at) VALUES (?, ?, ?, 'pending', ?, ?)", (intent_id, event["character_id"], event_id, reason, at))
            row = db.execute("SELECT i.*, e.content AS event_content, e.occurred_at FROM life_share_intents i JOIN life_events e ON e.id = i.event_id AND e.character_id=i.character_id WHERE i.id = ?", (intent_id,)).fetchone()
            return dict(row)

    def reconcile_recent(self, *, now: datetime | None = None, character_id: str = "big_fish", limit: int = 20) -> list[dict]:
        current = now or datetime.now(UTC)
        cutoff = _iso(current - timedelta(hours=24))
        with self.repository.database.connection() as db:
            rows = db.execute("SELECT e.id FROM life_events e LEFT JOIN life_share_decisions d ON d.event_id = e.id WHERE e.character_id = ? AND e.activity_status = 'completed' AND e.occurred_at >= ? AND d.event_id IS NULL ORDER BY e.occurred_at, e.id LIMIT ?", (character_id, cutoff, max(0, min(limit, 20)))).fetchall()
        return [intent for row in rows if (intent := self.consider(row["id"], now=current))]

    def list_intents(self, character_id: str = "big_fish", *, status: str | None = None, limit: int = 20) -> list[dict]:
        if status is not None and status not in {"pending", "delivered", "dismissed"}:
            raise ValueError("无效的分享状态")
        with self.repository.database.connection() as db:
            rows = db.execute("SELECT i.*, e.content AS event_content, e.occurred_at FROM life_share_intents i JOIN life_events e ON e.id = i.event_id AND e.character_id=i.character_id WHERE i.character_id = ? AND (? IS NULL OR i.status = ?) ORDER BY i.created_at DESC LIMIT ?", (character_id, status, status, max(1, min(limit, 100)))).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _chat_context(row: dict) -> str:
        return ("<life_share_intent>这是一条 pending 的已保存经历候选，不代表本轮已经提及或用户已经看到。"
                "只有在自然适合时才可真实提到，不要为了完成分享而强行插入。"
                f"事件：{escape(row['content'])}；发生于：{row['occurred_at']}。"
                "不要声称这件事已经在此前分享过。</life_share_intent>")

    def reserve_for_chat(self, query: str, *, character_id: str = "big_fish", now: datetime | None = None) -> dict | None:
        if not self.relevant_chat(query):
            return None
        current = now or datetime.now(UTC)
        at = _iso(current)
        token = str(uuid4())
        with self.repository.database.connection() as db:
            if not db.in_transaction:
                db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT i.id, e.id AS event_id, e.event_type, e.content, e.occurred_at, e.status, e.source, e.generation_method FROM life_share_intents i JOIN life_events e ON e.id = i.event_id AND e.character_id=i.character_id WHERE i.character_id = ? AND i.status = 'pending' AND (i.delivery_channel IS NULL OR i.delivery_channel='chat') AND (i.chat_retry_after IS NULL OR i.chat_retry_after <= ?) AND (i.selection_token IS NULL OR i.selection_expires_at <= ?) AND e.status='completed' AND e.activity_status='completed' ORDER BY i.created_at LIMIT 1", (character_id, at, at)).fetchone()
            if not row:
                return None
            changed = db.execute("UPDATE life_share_intents SET selection_token=?, selection_expires_at=? WHERE id=? AND status='pending' AND (selection_token IS NULL OR selection_expires_at <= ?)", (token, _iso(current + timedelta(minutes=10)), row["id"], at)).rowcount
            if changed != 1:
                return None
            event = {key: row[key] for key in ("event_id", "event_type", "content", "occurred_at", "status", "source", "generation_method")}
            return {"token": token, "intent_id": row["id"], "event": event,
                    "context": self._chat_context(dict(row))}

    def release_chat_reservation(self, token: str) -> bool:
        with self.repository.database.connection() as db:
            changed = db.execute("UPDATE life_share_intents SET selection_token=NULL, selection_expires_at=NULL WHERE selection_token=? AND status='pending'", (token,)).rowcount
        return changed == 1

    def commit_chat_selection(self, token: str, *, now: datetime | None = None) -> bool:
        """Record successful generation routing; keep the share pending until explicitly confirmed."""
        at = _iso(now)
        with self.repository.database.connection() as db:
            changed = db.execute("UPDATE life_share_intents SET delivery_channel='chat', selection_token=NULL, selection_expires_at=NULL, chat_retry_after=? WHERE selection_token=? AND status='pending'", (_iso((now or datetime.now(UTC)) + timedelta(hours=6)), token)).rowcount
        return changed == 1

    def offer_for_chat(self, query: str, *, character_id: str = "big_fish", now: datetime | None = None) -> str:
        """Compatibility read-only candidate lookup; it never changes delivery state."""
        if not self.relevant_chat(query):
            return ""
        at = _iso(now)
        with self.repository.database.connection() as db:
            row = db.execute("SELECT e.content, e.occurred_at FROM life_share_intents i JOIN life_events e ON e.id=i.event_id AND e.character_id=i.character_id WHERE i.character_id=? AND i.status='pending' AND (i.delivery_channel IS NULL OR i.delivery_channel='chat') AND (i.chat_retry_after IS NULL OR i.chat_retry_after<=?) AND (i.selection_token IS NULL OR i.selection_expires_at<=?) AND e.status='completed' AND e.activity_status='completed' ORDER BY i.created_at LIMIT 1", (character_id, at, at)).fetchone()
        return self._chat_context(dict(row)) if row else ""

    def transition(self, intent_id: str, target: str, *, character_id: str = "big_fish", now: datetime | None = None) -> dict:
        if target not in {"delivered", "dismissed"}:
            raise ValueError("无效的分享状态变化")
        with self.repository.database.connection() as db:
            if not db.in_transaction:
                db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM life_share_intents WHERE id = ? AND character_id = ?", (intent_id, character_id)).fetchone()
            if not row:
                raise KeyError(intent_id)
            if row["status"] == target:
                return dict(row)
            if row["status"] != "pending":
                raise ValueError("分享意图已经结束")
            column = "delivered_at" if target == "delivered" else "dismissed_at"
            db.execute(f"UPDATE life_share_intents SET status = ?, {column} = ?, selection_token=NULL, selection_expires_at=NULL WHERE id = ?", (target, _iso(now), intent_id))
            return dict(db.execute("SELECT * FROM life_share_intents WHERE id = ?", (intent_id,)).fetchone())
