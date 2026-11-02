"""Bounded, deterministic virtual activity decisions; no model calls or background loop."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta, tzinfo

from .life import LifeRepository, _iso
from .life_share import LifeShareService


ACTIVITIES = {
    "reading": ("翻看一本有趣的书", 30, 1.2),
    "music": ("听音乐，跟着节奏晃一晃", 25, 1.4),
    "tidying": ("整理自己的小角落", 20, 0.8),
    "cooking": ("琢磨一道新菜", 35, 0.9),
    "resting": ("安静休息一会儿", 30, 1.0),
    "exploring": ("去探索一个没注意过的角落", 30, 1.2),
    "daydreaming": ("发一会儿呆，想些古怪点子", 20, 1.0),
}


class LifeEngine:
    def __init__(self, repository: LifeRepository, *, seed: int = 0, cooldown: timedelta = timedelta(minutes=90), local_timezone: tzinfo | None = None):
        self.repository = repository
        self.seed = seed
        self.cooldown = cooldown
        self.timezone = local_timezone

    def _unit(self, *parts: object) -> float:
        raw = "|".join(map(str, (self.seed, *parts))).encode("utf-8")
        return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big") / 2**64

    def preferences(self, character_id: str, *, now: datetime | None = None) -> dict[str, float]:
        self.repository.ensure_state(character_id, now=now)
        at = _iso(now)
        with self.repository.database.connection() as db:
            for kind, (_, _, initial) in ACTIVITIES.items():
                db.execute("INSERT OR IGNORE INTO life_preferences(character_id, event_type, weight, updated_at) VALUES (?, ?, ?, ?)", (character_id, kind, initial, at))
            rows = db.execute("SELECT event_type, weight FROM life_preferences WHERE character_id = ?", (character_id,)).fetchall()
        return {row["event_type"]: row["weight"] for row in rows}

    def set_preference(self, character_id: str, event_type: str, weight: float, *, now: datetime | None = None, reason: str = "explicit_preference") -> None:
        if event_type not in ACTIVITIES or not 0.25 <= weight <= 2.0:
            raise ValueError("无效的活动偏好")
        old = self.preferences(character_id, now=now)[event_type]
        at = _iso(now)
        with self.repository.database.connection() as db:
            db.execute("UPDATE life_preferences SET weight = ?, updated_at = ? WHERE character_id = ? AND event_type = ?", (weight, at, character_id, event_type))
            db.execute("INSERT INTO life_preference_changes(id, character_id, event_type, old_weight, new_weight, reason, changed_at) VALUES (lower(hex(randomblob(16))), ?, ?, ?, ?, ?, ?)", (character_id, event_type, old, weight, reason, at))

    def learn_from_completion(self, event: dict, now: datetime) -> None:
        if event.get("activity_status") != "completed" or event.get("status") != "completed":
            return
        kind, feeling = event["event_type"], (event["feeling"] or "")
        if kind not in ACTIVITIES:
            return
        step = -0.03 if any(word in feeling for word in ("难过", "讨厌", "疲惫", "不开心")) else 0.03 if any(word in feeling for word in ("开心", "喜欢", "愉快", "满足")) else 0
        if not step:
            return
        self.preferences(event["character_id"], now=now)
        at = _iso(now)
        with self.repository.database.connection() as db:
            if not db.in_transaction:
                db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT 1 FROM life_preference_changes WHERE event_id = ? AND reason = 'completed_feeling'", (event["id"],)).fetchone()
            if existing:
                return
            old = db.execute("SELECT weight FROM life_preferences WHERE character_id = ? AND event_type = ?", (event["character_id"], kind)).fetchone()[0]
            new = min(2.0, max(0.25, round(old + step, 2)))
            if new == old:
                return
            db.execute("UPDATE life_preferences SET weight = ?, updated_at = ? WHERE character_id = ? AND event_type = ?", (new, at, event["character_id"], kind))
            db.execute("INSERT INTO life_preference_changes(id, character_id, event_type, old_weight, new_weight, reason, event_id, changed_at) VALUES (lower(hex(randomblob(16))), ?, ?, ?, ?, ?, ?, ?)", (event["character_id"], kind, old, new, "completed_feeling", event["id"], at))

    def choose(self, character_id: str, *, now: datetime) -> tuple[str, str]:
        weights = self.preferences(character_id, now=now)
        with self.repository.database.connection() as db:
            recent = db.execute("SELECT event_type FROM life_events WHERE character_id = ? AND activity_status = 'completed' ORDER BY ended_at DESC LIMIT 5", (character_id,)).fetchall()
        recent_types = [row["event_type"] for row in recent]
        slot = int(now.timestamp() // 5400)
        zone = self.timezone or self.repository.timezone(character_id)
        local_time = now.astimezone(zone)
        local_hour = local_time.hour
        scores = {}
        for kind in ACTIVITIES:
            repeats = sum(1 for item in recent_types[:3] if item == kind)
            immediate = 0.9 if recent_types and recent_types[0] == kind else 0
            time_bias = 0.12 if kind == "resting" and (local_hour < 7 or local_hour >= 22) else 0
            time_bias += 0.12 if kind == "cooking" and local_hour in (11, 12, 17, 18) else 0
            surprise = 0.45 if kind == "exploring" and self._unit(character_id, slot, "surprise") > 0.94 else 0
            scores[kind] = weights[kind] - 0.35 * repeats - immediate + time_bias + surprise + 0.22 * self._unit(character_id, slot, kind)
        selected = max(scores, key=lambda kind: (scores[kind], kind))
        reason = f"preference={weights[selected]:.2f}; recent={recent_types[:3]}; score={scores[selected]:.3f}; local={local_time.isoformat()}; timezone={getattr(zone, 'key', zone)}; slot={slot}; seed={self.seed}"
        return selected, reason

    def advance(self, character_id: str = "big_fish", *, now: datetime | None = None, allow_autonomous: bool = True) -> list[dict]:
        with self.repository.advance_lease(character_id) as lease_token:
            if not lease_token:
                return []
            with self.repository.database.atomic() as db:
                row = db.execute("SELECT token FROM life_advance_leases WHERE character_id = ?", (character_id,)).fetchone()
                if row is None or row["token"] != lease_token:
                    return []
                return self._advance_locked(character_id, now=now, allow_autonomous=allow_autonomous)

    def _advance_locked(self, character_id: str, *, now: datetime | None, allow_autonomous: bool) -> list[dict]:
        """At most one completion and one start/selection per call; never backfill idle periods."""
        now = now or datetime.now(UTC)
        at = _iso(now)
        self.repository.ensure_state(character_id, now=now)
        changes = []
        with self.repository.database.connection() as db:
            active = db.execute("SELECT * FROM life_events WHERE character_id = ? AND activity_status = 'active' AND execution_status = 'running' ORDER BY started_at LIMIT 1", (character_id,)).fetchone()
        if active:
            if self.repository.instance_token is not None and active["runtime_owner_token"] != self.repository.instance_token:
                raise RuntimeError("活动属于其他服务实例；需要先恢复所有权")
            if active["planned_end_at"] and active["planned_end_at"] <= at:
                done = self.repository.transition(active["id"], "completed", character_id=character_id, now=now)
                self.learn_from_completion(done, now)
                LifeShareService(self.repository).consider(done["id"], now=now)
                changes.append(done)
            return changes

        with self.repository.database.connection() as db:
            planned = db.execute("SELECT * FROM life_events WHERE character_id = ? AND activity_status = 'planned' ORDER BY COALESCE(planned_start_at, created_at), id LIMIT 1", (character_id,)).fetchone()
        if planned:
            if planned["planned_start_at"] and planned["planned_start_at"] > at:
                return changes
            if planned["planned_end_at"] and planned["planned_end_at"] <= at:
                changes.append(self.repository.transition(planned["id"], "cancelled", character_id=character_id, now=now, reason="missed_plan_window"))
                return changes
            changes.append(self.repository.transition(planned["id"], "active", character_id=character_id, now=now, reason="plan_started"))
            return changes

        if not allow_autonomous:
            return changes

        with self.repository.database.connection() as db:
            last = db.execute("SELECT created_at FROM life_events WHERE character_id = ? AND generation_method = 'rule_engine' ORDER BY created_at DESC LIMIT 1", (character_id,)).fetchone()
        if last and now.astimezone(UTC) - datetime.fromisoformat(last["created_at"]) < self.cooldown:
            return changes
        kind, reason = self.choose(character_id, now=now)
        content, duration, _ = ACTIVITIES[kind]
        slot = int(now.timestamp() // 5400)
        event = self.repository.record(content, character_id=character_id, event_type=kind, status="planned", planned_start_at=now,
                                       planned_end_at=now + timedelta(minutes=duration), source="character", generation_method="rule_engine",
                                       idempotency_key=f"auto:{character_id}:{slot}", selection_reason=reason, now=now)
        changes.append(self.repository.transition(event["id"], "active", character_id=character_id, now=now, reason="autonomous_choice"))
        return changes
