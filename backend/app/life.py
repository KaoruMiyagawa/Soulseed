from __future__ import annotations

from datetime import UTC, datetime, timedelta, tzinfo
import sqlite3
from contextlib import contextmanager
from uuid import uuid4
from zoneinfo import ZoneInfo

from .db import Database
from .life_habits import LifeHabit, infer_habits


def _iso(value: datetime | None = None) -> str:
    value = value or datetime.now(UTC)
    if value.tzinfo is None:
        raise ValueError("时间必须包含时区")
    return value.astimezone(UTC).isoformat()


def _preference_growth(db, character_id: str, *, limit: int = 5) -> list[dict]:
    """Summarize repeated, completed character experiences from the existing weight ledger."""
    if limit <= 0:
        return []
    rows = db.execute(
        """SELECT c.id AS change_id,c.event_id,c.event_type,c.old_weight,c.new_weight,c.changed_at
           FROM life_preference_changes c
           JOIN life_events e ON e.id=c.event_id AND e.character_id=c.character_id
           WHERE c.character_id=? AND c.reason='completed_feeling'
             AND e.status='completed' AND e.activity_status='completed'
           ORDER BY c.changed_at DESC,c.id DESC LIMIT 512""",
        (character_id,),
    ).fetchall()
    grouped: dict[str, list[dict]] = {}
    seen_events: set[str] = set()
    for row in rows:
        if not row["event_id"] or row["event_id"] in seen_events:
            continue
        seen_events.add(row["event_id"])
        item = dict(row)
        item["delta"] = round(float(row["new_weight"]) - float(row["old_weight"]), 2)
        grouped.setdefault(row["event_type"], []).append(item)

    from .life_engine import ACTIVITIES

    priority = {"established": 0, "waning": 1, "recovering": 2, "forming": 3, "cooling": 4, "mixed": 5}
    result = []
    for event_type, history in grouped.items():
        positive = sum(item["delta"] > 0 for item in history)
        negative = sum(item["delta"] < 0 for item in history)
        total = len(history)
        if total < 2:
            # One pleasant or unpleasant event may affect the LifeEngine's
            # small numeric weight, but never becomes a personality claim.
            continue
        net = round(sum(item["delta"] for item in history), 2)
        recent = history[:4]
        recent_deltas = [item["delta"] for item in recent]
        recent_positive = sum(delta > 0 for delta in recent_deltas)
        recent_negative = sum(delta < 0 for delta in recent_deltas)
        if len(recent_deltas) >= 3 and all(delta > 0 for delta in recent_deltas[:3]) and negative >= 2:
            tendency = "recovering"
        elif negative >= 3 and negative > positive and net <= -0.03:
            tendency = "cooling"
        elif positive >= 3 and positive > negative and net >= 0.03:
            tendency = "established"
        elif len(recent_deltas) >= 3 and all(delta < 0 for delta in recent_deltas[:3]) and positive:
            tendency = "waning"
        elif positive >= 2 and positive > negative:
            tendency = "forming"
        elif negative >= 2 and negative > positive:
            tendency = "cooling"
        else:
            tendency = "mixed"
        label = ACTIVITIES.get(event_type, (event_type, 0, 0))[0]
        recent_tendency = (
            "strengthening" if recent_positive > recent_negative else
            "weakening" if recent_negative > recent_positive else "mixed"
        )
        result.append({
            "activity_type": event_type,
            "activity_name": label,
            "tendency": tendency,
            "completed_experiences": total,
            "positive_experiences": positive,
            "negative_experiences": negative,
            "net_change": net,
            "recent_tendency": recent_tendency,
        })
    result.sort(key=lambda item: (priority[item["tendency"]], -item["completed_experiences"], item["activity_type"]))
    return result[:min(limit, 10)]


class LifeRepository:
    """Small persistent store for a character's current activity and life events."""

    def __init__(self, database: Database, *, default_timezone: str = "Asia/Shanghai"):
        self.database = database
        self.default_timezone = default_timezone
        self.instance_token: str | None = None
        self.instance_owner = None
        ZoneInfo(default_timezone)

    def ensure_state(self, character_id: str = "big_fish", *, now: datetime | None = None) -> dict:
        timestamp = _iso(now)
        with self.database.connection() as db:
            db.execute("INSERT OR IGNORE INTO characters(id, owner_user_id, ownership_status, created_at) VALUES (?, NULL, 'quarantined', ?)", (character_id, timestamp))
            db.execute("INSERT OR IGNORE INTO life_states(character_id, updated_at, timezone_id) VALUES (?, ?, ?)", (character_id, timestamp, self.default_timezone))
            return dict(db.execute("SELECT * FROM life_states WHERE character_id = ?", (character_id,)).fetchone())

    def set_timezone(self, character_id: str, timezone_id: str, *, now: datetime | None = None) -> dict:
        ZoneInfo(timezone_id)
        self.ensure_state(character_id, now=now)
        with self.database.connection() as db:
            db.execute("UPDATE life_states SET timezone_id = ?, updated_at = ? WHERE character_id = ?", (timezone_id, _iso(now), character_id))
        return self.get_state(character_id, now=now)

    def timezone(self, character_id: str) -> ZoneInfo:
        return ZoneInfo(self.ensure_state(character_id)["timezone_id"])

    def preference_growth(self, character_id: str = "big_fish", *, limit: int = 5) -> list[dict]:
        """Return only tendencies supported by multiple completed experiences.

        This is a view over life_preferences and life_preference_changes, not a
        second preference or habit store. User memories are never queried here.
        """
        with self.database.connection() as db:
            return _preference_growth(db, character_id, limit=limit)

    def habits(self, character_id: str = "big_fish", *, now: datetime | None = None,
               zone: tzinfo | None = None) -> tuple[LifeHabit, ...]:
        from .life_engine import ACTIVITIES

        with self.database.connection() as db:
            if zone is None:
                state = db.execute("SELECT timezone_id FROM life_states WHERE character_id = ?",
                                   (character_id,)).fetchone()
                zone = ZoneInfo(state["timezone_id"] if state else self.default_timezone)
            return infer_habits(
                db, character_id, as_of=_iso(now), zone=zone,
                activity_names={kind: activity[0] for kind, activity in ACTIVITIES.items()},
            )

    @contextmanager
    def advance_lease(self, character_id: str):
        if self.instance_owner is not None:
            self.instance_owner.assert_owned()
        self.ensure_state(character_id)
        token, wall_now = str(uuid4()), datetime.now(UTC)
        expires = _iso(wall_now + timedelta(seconds=60))
        acquired = False
        with self.database.connection() as db:
            if not db.in_transaction:
                db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO life_advance_leases(character_id, token, expires_at) VALUES (?, ?, ?) ON CONFLICT(character_id) DO UPDATE SET token = excluded.token, expires_at = excluded.expires_at WHERE life_advance_leases.expires_at <= ?", (character_id, token, expires, _iso(wall_now)))
            acquired = db.execute("SELECT token FROM life_advance_leases WHERE character_id = ?", (character_id,)).fetchone()[0] == token
        try:
            yield token if acquired else None
        finally:
            if acquired:
                with self.database.connection() as db:
                    db.execute("DELETE FROM life_advance_leases WHERE character_id = ? AND token = ?", (character_id, token))

    def interrupt_active(self, *, owner_token: str | None, reason: str, now: datetime | None = None,
                         orphaned: bool = False) -> list[dict]:
        """Record execution interruption without inventing completion or character cancellation.

        The caller must hold the process lock. Normal shutdown is token-scoped;
        orphan recovery is allowed only after acquiring that lock from a dead owner.
        """
        if reason not in {"service_stopped", "owner_lost"}:
            raise ValueError("无效的中断原因")
        if not owner_token:
            raise ValueError("中断活动必须指定实例令牌")
        if orphaned and reason != "owner_lost":
            raise ValueError("孤儿活动只能标记为失去所有权")
        if not orphaned and reason != "service_stopped":
            raise ValueError("正常关闭只能处理本实例活动")
        at = _iso(now)
        with self.database.connection() as db:
            if not db.in_transaction:
                db.execute("BEGIN IMMEDIATE")
            if orphaned:
                rows = db.execute("SELECT id, character_id, runtime_owner_token FROM life_events WHERE activity_status = 'active' AND execution_status = 'running' AND (runtime_owner_token IS NULL OR runtime_owner_token != ?)", (owner_token,)).fetchall()
            else:
                rows = db.execute("SELECT id, character_id, runtime_owner_token FROM life_events WHERE activity_status = 'active' AND execution_status = 'running' AND runtime_owner_token = ?", (owner_token,)).fetchall()
            for row in rows:
                db.execute("UPDATE life_events SET execution_status = 'interrupted', interrupted_at = ? WHERE id = ? AND execution_status = 'running'", (at, row["id"]))
                db.execute("UPDATE life_states SET current_activity_event_id = NULL, updated_at = ? WHERE character_id = ? AND current_activity_event_id = ?", (at, row["character_id"], row["id"]))
                db.execute("INSERT OR IGNORE INTO life_activity_interruptions(event_id, character_id, instance_token, reason, interrupted_at) VALUES (?, ?, ?, ?, ?)", (row["id"], row["character_id"], row["runtime_owner_token"], reason, at))
            return [dict(row) for row in rows]

    def get_state(self, character_id: str = "big_fish", *, now: datetime | None = None) -> dict:
        now = now or datetime.now(UTC)
        state = self.ensure_state(character_id, now=now)
        with self.database.connection() as db:
            event = db.execute("SELECT * FROM life_events WHERE id = ? AND character_id = ? AND execution_status = 'running'", (state["current_activity_event_id"], character_id)).fetchone() if state["current_activity_event_id"] else None
        state["current_activity"] = self.present_event(dict(event)) if event else None
        return state

    def present_event(self, event: dict) -> dict:
        """Return a lifecycle view that cannot confuse stale active with running."""
        result = dict(event)
        if "interruption_reason" not in result or "interrupted_at" not in result:
            try:
                with self.database.connection() as db:
                    interruption = db.execute("SELECT reason, interrupted_at FROM life_activity_interruptions WHERE event_id=? AND character_id=?",
                                              (result["id"], result["character_id"])).fetchone()
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc):
                    raise
                interruption = None
            if interruption:
                result["interruption_reason"] = interruption["reason"]
                result["interrupted_at"] = interruption["interrupted_at"]
        status = result.get("activity_status") or result.get("status")
        if result.get("activity_status") == "active" and result.get("execution_status") != "running":
            status = "interrupted"
        result["effective_status"] = status
        return result

    def list_events(self, character_id: str = "big_fish", *, limit: int = 50) -> list[dict]:
        with self.database.connection() as db:
            rows = db.execute("SELECT e.*, i.reason AS interruption_reason, i.interrupted_at AS interruption_time FROM life_events e LEFT JOIN life_activity_interruptions i ON i.event_id=e.id AND i.character_id=e.character_id WHERE e.character_id = ? ORDER BY e.created_at DESC, e.id LIMIT ?", (character_id, max(1, min(limit, 100)))).fetchall()
        result = []
        for row in rows:
            event = dict(row)
            if event.get("interruption_time"):
                event["interrupted_at"] = event.pop("interruption_time")
            else:
                event.pop("interruption_time", None)
            result.append(self.present_event(event))
        return result

    def record(self, content: str, *, character_id: str = "big_fish", event_type: str = "activity",
               status: str = "completed", occurred_at: datetime | None = None,
               planned_start_at: datetime | None = None,
               planned_end_at: datetime | None = None, location: str | None = None,
               feeling: str | None = None, source: str = "character", generation_method: str = "manual",
               idempotency_key: str | None = None, now: datetime | None = None,
               selection_reason: str | None = None) -> dict:
        if status not in {"completed", "planned", "imagined", "wish"}:
            raise ValueError("无效的生活事件状态")
        if not content.strip():
            raise ValueError("活动内容不能为空")
        now = now or datetime.now(UTC)
        state = self.ensure_state(character_id, now=now)
        timestamp = _iso(now)
        event_id = str(uuid4())
        occurred = _iso(occurred_at) if occurred_at else (_iso(now) if status == "completed" else None)
        if status == "completed" and occurred > timestamp:
            raise ValueError("已完成事件不能发生在未来")
        planned_end = _iso(planned_end_at) if planned_end_at else None
        planned_start = _iso(planned_start_at) if planned_start_at else None
        if planned_start and planned_end and planned_start > planned_end:
            raise ValueError("计划开始时间不能晚于结束时间")
        with self.database.connection() as db:
            if idempotency_key:
                old = db.execute("SELECT * FROM life_events WHERE character_id = ? AND idempotency_key = ?", (character_id, idempotency_key)).fetchone()
                if old:
                    return dict(old)
            activity_status = status if status in {"completed", "planned"} else None
            try:
                db.execute("INSERT INTO life_events(id, character_id, event_type, status, occurred_at, planned_end_at, content, location, feeling, source, generation_method, state_id, idempotency_key, created_at, activity_status, planned_start_at, started_at, ended_at, selection_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (event_id, character_id, event_type, status, occurred, planned_end, content.strip(), location, feeling, source, generation_method, state["character_id"], idempotency_key, timestamp, activity_status, planned_start, occurred if status == "completed" else None, occurred if status == "completed" else None, selection_reason))
            except sqlite3.IntegrityError:
                if idempotency_key:
                    old = db.execute("SELECT * FROM life_events WHERE character_id = ? AND idempotency_key = ?", (character_id, idempotency_key)).fetchone()
                    if old:
                        return dict(old)
                raise
            if status == "completed":
                db.execute("UPDATE life_states SET mood = COALESCE(?, mood), updated_at = ? WHERE character_id = ?", (feeling, timestamp, character_id))
            if activity_status:
                self._transition_log(db, character_id, event_id, None, activity_status, "recorded", timestamp)
            return self.present_event(dict(db.execute("SELECT * FROM life_events WHERE id = ?", (event_id,)).fetchone()))

    @staticmethod
    def _transition_log(db, character_id: str, event_id: str, old: str | None, new: str, reason: str, at: str) -> None:
        db.execute("INSERT INTO life_activity_transitions(id, character_id, event_id, from_status, to_status, reason, changed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (str(uuid4()), character_id, event_id, old, new, reason, at))

    def transition(self, event_id: str, target: str, *, character_id: str = "big_fish", now: datetime | None = None, reason: str = "time_advance", feeling: str | None = None) -> dict:
        at = _iso(now)
        with self.database.connection() as db:
            if not db.in_transaction:
                db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM life_events WHERE id = ? AND character_id = ?", (event_id, character_id)).fetchone()
            if row is None:
                raise KeyError(event_id)
            old = row["activity_status"]
            if old == target:
                return dict(row)
            if (old, target) not in {("planned", "active"), ("active", "completed"), ("planned", "cancelled"), ("active", "cancelled")}:
                raise ValueError(f"无效的活动状态变化：{old} → {target}")
            if row["execution_status"] == "interrupted":
                raise ValueError("已中断的活动不能追认成完成或角色主动取消")
            if target == "active":
                occupied = db.execute("SELECT current_activity_event_id FROM life_states WHERE character_id = ?", (character_id,)).fetchone()
                if occupied and occupied[0] and occupied[0] != event_id:
                    raise ValueError("角色已有进行中的活动")
                db.execute("UPDATE life_events SET activity_status = 'active', started_at = ?, runtime_owner_token = ?, execution_status = 'running' WHERE id = ?", (at, self.instance_token, event_id))
                db.execute("UPDATE life_states SET current_activity_event_id = ?, updated_at = ? WHERE character_id = ?", (event_id, at, character_id))
            else:
                db.execute("UPDATE life_events SET activity_status = ?, execution_status = NULL, status = CASE WHEN ? = 'completed' THEN 'completed' ELSE status END, occurred_at = CASE WHEN ? = 'completed' THEN ? ELSE occurred_at END, ended_at = ?, feeling = COALESCE(?, feeling) WHERE id = ?", (target, target, target, at, at, feeling, event_id))
                db.execute("UPDATE life_states SET current_activity_event_id = NULL, mood = COALESCE(?, mood), updated_at = ? WHERE character_id = ? AND current_activity_event_id = ?", (feeling or row["feeling"] if target == "completed" else None, at, character_id, event_id))
            self._transition_log(db, character_id, event_id, old, target, reason, at)
            return self.present_event(dict(db.execute("SELECT * FROM life_events WHERE id = ?", (event_id,)).fetchone()))

    def advance_due(self, *, now: datetime | None = None, character_id: str = "big_fish", limit: int = 1) -> list[dict]:
        """Compatibility entry point; it follows the same bounded lifecycle rules."""
        if limit <= 0:
            return []
        from .life_engine import LifeEngine
        return LifeEngine(self).advance(character_id, now=now, allow_autonomous=False)

    def context(self, query: str, *, character_id: str = "big_fish", now: datetime | None = None) -> str:
        state = self.ensure_state(character_id, now=now)
        if any(word in query for word in ("想你", "喜欢你", "爱你", "好累", "难过", "伤心", "烦")):
            return ""
        cues = ("吃", "饭", "做什么", "干嘛", "在干", "今天", "开心", "心情", "活动")
        if not any(cue in query for cue in cues):
            return ""
        rows = self.list_events(character_id, limit=8)
        lines = [f"- {row['effective_status']}: {row['content']} (type={row['event_type']}; occurred={row['occurred_at'] or 'none'}; started={row['started_at'] or 'none'}; planned_end={row['planned_end_at'] or 'none'}; interruption={row.get('interruption_reason') or 'none'}; interrupted_at={row.get('interrupted_at') or 'none'})" for row in rows]
        current = ""
        if state["current_activity_event_id"]:
            row = next((item for item in rows if item["id"] == state["current_activity_event_id"] and item["effective_status"] == "active"), None)
            if row:
                current = f"\n正在进行：{row['content']}，开始于 {row['started_at']}，预计结束 {row['planned_end_at'] or '未定'}"
        if not lines and not current:
            return "<life_context>没有保存的相关生活经历；不要声称已发生，可自然表达愿望或不确定。不要把每次回答写成日报。</life_context>"
        zone_id = state["timezone_id"]
        local_today = (now or datetime.now(UTC)).astimezone(ZoneInfo(zone_id)).date().isoformat()
        return f"<life_context>角色当地日期：{local_today}（{zone_id}）。以下是已保存的生活记录；问‘今天’时只把当地今天的已完成事件算作今天经历。completed 已完成；active 正在进行；interrupted 表示服务停止时执行中断，不能说成已完成或仍在进行；planned 尚未开始；cancelled 是角色取消；imagined 是想象；wish 是愿望。只有 completed 可说成已经做过。自然回应，不必强行提及活动。\n" + "\n".join(lines) + current + "\n</life_context>"

    def expression_snapshot(self, query: str, *, character_id: str = "big_fish", now: datetime | None = None) -> dict:
        """Return a bounded, character-scoped snapshot for natural expression."""
        now = now or datetime.now(UTC)
        excluded = ("想你", "喜欢你", "爱你", "好累", "难过", "伤心", "烦")
        topic_cues = (
            ("exploring", ("探索", "探险", "发现了什么", "探索了什么", "探索发现", "研究经过", "研究过程", "怎么查到", "怎么知道", "如何得知", "从哪里看到", "来源", "出处", "根据什么"), ("research", "exploring")),
            ("reading", ("阅读", "读书", "读的", "书名", "什么书"), ("reading",)),
            ("music", ("音乐", "歌曲", "首歌", "曲名", "旋律"), ("music",)),
            ("meal", ("吃", "饭", "食物", "做菜", "菜"), ("meal", "cooking")),
        )
        topic = next((name for name, cues, _ in topic_cues if any(cue in query for cue in cues)), None)
        topic_types = next((types for name, _, types in topic_cues if name == topic), ())
        emotional = any(word in query for word in excluded)
        relevant = topic is not None or (not emotional and any(
            cue in query for cue in ("做什么", "干嘛", "在干", "今天", "开心", "心情", "活动", "最近", "发现", "刚才", "做了什么")
        ))
        if not relevant:
            topic = None
            topic_types = ()
        state = self.get_state(character_id, now=now)
        zone = ZoneInfo(state["timezone_id"])
        local_date = now.astimezone(zone).date().isoformat()
        recent = self.list_events(character_id, limit=40) if relevant else []
        if relevant:
            with self.database.connection() as db:
                for event in recent:
                    if event.get("event_type") != "research" or event.get("effective_status") != "completed":
                        continue
                    run = db.execute(
                        "SELECT summary,claims_json,unanswered_json FROM exploration_runs WHERE life_event_id=? AND character_id=? AND status='completed'",
                        (event["id"], character_id),
                    ).fetchone()
                    if not run:
                        continue
                    import json
                    sources = db.execute(
                        "SELECT id,source_url,title,retrieved_at,excerpt FROM exploration_evidence "
                        "WHERE run_id=(SELECT id FROM exploration_runs WHERE life_event_id=? AND character_id=?) "
                        "AND tool_name='read_webpage' AND outcome='success' AND page_relevance='relevant' ORDER BY sequence_no LIMIT 4",
                        (event["id"], character_id),
                    ).fetchall()
                    import json
                    claims = json.loads(run["claims_json"] or "[]")
                    if not claims or not sources:
                        # Keep legacy records untouched in storage, but do not
                        # present an unsubstantiated historical run as learned knowledge.
                        event["_omit_from_expression"] = True
                        continue
                    event["research_result"] = {
                        "summary": run["summary"],
                        "claims": claims,
                        "unanswered": json.loads(run["unanswered_json"] or "[]"),
                        "sources": [{"evidence_id": source["id"], "url": source["source_url"],
                                     "title": source["title"], "retrieved_at": source["retrieved_at"],
                                     "excerpt": source["excerpt"][:500] if source["excerpt"] else None}
                                    for source in sources],
                    }
            recent = [event for event in recent if not event.get("_omit_from_expression")]
        today_only = "今天" in query or "今日" in query
        if today_only:
            today = datetime.fromisoformat(local_date).date()
            recent = [event for event in recent if event.get("effective_status") != "completed"
                      or (event.get("occurred_at") and datetime.fromisoformat(event["occurred_at"]).astimezone(zone).date() == today)]
        if topic_types:
            matching = [event for event in recent if event.get("event_type") in topic_types]
            selected_ids = {event["id"] for event in matching[:4]}
            events = (matching[:4] + [event for event in recent if event["id"] not in selected_ids][:4])[:8]
        else:
            events = recent[:8]
        life_scope = {
            "topic": topic or "general",
            "matching_evidence_count": sum(event.get("event_type") in topic_types for event in events)
            if topic_types else len(events),
        } if relevant else None
        with self.database.connection() as db:
            personal_growth = _preference_growth(db, character_id)
        preferences = [item["activity_name"] for item in personal_growth if item["tendency"] == "established"]
        return {
            "current_activity": state.get("current_activity") if relevant else None,
            "events": events,
            "preferences": preferences,
            "personal_growth": personal_growth,
            "habits": self.habits(character_id, now=now, zone=zone),
            "timezone_id": state.get("timezone_id"),
            "local_date": local_date,
            "today_only": today_only,
            "life_scope": life_scope,
        }

    def exploration_candidates(self, character_id: str = "big_fish", limit: int = 8) -> list[str]:
        """Only interests reinforced by completed character experiences count as candidates."""
        return [item["topic"] for item in self.exploration_candidate_items(character_id, limit)]

    def exploration_candidate_items(self, character_id: str = "big_fish", limit: int = 8) -> list[dict]:
        """Return deduplicated exploration themes with their life/research basis."""
        with self.database.connection() as db:
            rows = db.execute(
                "SELECT p.event_type, MAX(p.weight) AS weight FROM life_preferences p "
                "JOIN life_preference_changes c ON c.character_id=p.character_id AND c.event_type=p.event_type "
                "WHERE p.character_id=? AND c.reason IN ('completed_feeling','explicit_preference') "
                "GROUP BY p.event_type ORDER BY weight DESC,p.event_type LIMIT ?", (character_id, max(1, min(limit, 12))),
            ).fetchall()
            candidates = []
            from .life_engine import ACTIVITIES
            for row in rows:
                changes = db.execute(
                    "SELECT c.id,c.event_id,e.content,c.changed_at FROM life_preference_changes c "
                    "LEFT JOIN life_events e ON e.id=c.event_id AND e.character_id=c.character_id "
                    "WHERE c.character_id=? AND c.event_type=? AND c.reason IN ('completed_feeling','explicit_preference') "
                    "ORDER BY c.changed_at DESC,c.id DESC LIMIT 3", (character_id, row["event_type"]),
                ).fetchall()
                candidates.append({
                    "topic": str(row["event_type"]), "basis": "stable_interest",
                    "basis_detail": {
                        "source": "life_preference_history",
                        "subject": ACTIVITIES.get(str(row["event_type"]), (str(row["event_type"]),))[0],
                        "change_ids": [item["id"] for item in changes],
                        "event_ids": list(dict.fromkeys(item["event_id"] for item in changes if item["event_id"])),
                        "experience_summaries": [" ".join(str(item["content"] or "").split())[:240]
                                                  for item in changes if item["content"]],
                    },
                })
            goals = db.execute(
                "SELECT content FROM life_events WHERE character_id=? AND event_type='goal' "
                "AND status IN ('completed','planned') ORDER BY created_at DESC LIMIT ?",
                (character_id, max(1, min(limit, 8))),
            ).fetchall()
            candidates.extend({"topic": "目标：" + " ".join(str(row["content"]).split())[:160], "basis": "planned_goal",
                               "basis_detail": {"source": "life_goal", "content": " ".join(str(row["content"]).split())[:240]}}
                              for row in goals)
            open_runs = db.execute(
                "SELECT id,requested_topic,selected_topic,unanswered_json,status,error_code FROM exploration_runs "
                "WHERE character_id=? AND (error_code='insufficient_evidence' OR (status='completed' AND unanswered_json IS NOT NULL)) "
                "ORDER BY updated_at DESC LIMIT 8", (character_id,),
            ).fetchall()
            import json
            unresolved_count = 0
            for run in open_runs:
                topic = run["selected_topic"] or run["requested_topic"]
                if not topic:
                    continue
                questions = json.loads(run["unanswered_json"] or "[]")
                if not questions and run["error_code"] == "insufficient_evidence":
                    questions = [topic]
                for question in questions:
                    if unresolved_count >= 2:
                        break
                    if isinstance(question, str) and question.strip():
                        candidates.append({"topic": "继续查清：" + " ".join(question.split())[:200],
                                           "basis": "unresolved_question",
                                           "basis_detail": {"source": "unresolved_exploration", "run_id": run["id"],
                                                            "prior_topic": topic}})
                        unresolved_count += 1

            # Sourced conclusions from completed runs can seed a genuinely new,
            # follow-up question. The exact claim and its readable evidence IDs
            # travel with the candidate instead of becoming unattributed lore.
            completed = db.execute(
                "SELECT id,selected_topic,requested_topic,claims_json FROM exploration_runs "
                "WHERE character_id=? AND status='completed' ORDER BY ended_at DESC LIMIT 12", (character_id,),
            ).fetchall()
            import json
            research_count = 0
            for run in completed:
                if research_count >= min(3, limit):
                    break
                topic = run["selected_topic"] or run["requested_topic"]
                if not topic:
                    continue
                try:
                    claims = json.loads(run["claims_json"] or "[]")
                except (TypeError, json.JSONDecodeError):
                    claims = []
                for claim in claims:
                    if not isinstance(claim, dict) or claim.get("kind") != "sourced":
                        continue
                    source_ids = claim.get("source_ids")
                    if not isinstance(source_ids, list) or not source_ids:
                        continue
                    placeholders = ",".join("?" for _ in source_ids)
                    valid = db.execute(
                        f"SELECT id FROM exploration_evidence WHERE run_id=? AND character_id=? AND tool_name='read_webpage' "
                        f"AND outcome='success' AND page_relevance='relevant' AND id IN ({placeholders}) ORDER BY sequence_no",
                        (run["id"], character_id, *source_ids),
                    ).fetchall()
                    if not valid:
                        continue
                    candidates.append({
                        "topic": str(topic), "basis": "completed_research",
                        "basis_detail": {"source": "verified_exploration_claim", "run_id": run["id"],
                                         "claim": " ".join(str(claim.get("text", "")).split())[:500],
                                         "evidence_ids": [item["id"] for item in valid]},
                    })
                    research_count += 1
                    if research_count >= min(3, limit):
                        break

            # De-duplicate equivalent display strings without losing the first
            # candidate's strongest/most direct provenance.
            import re
            unique = {}
            for item in candidates:
                key = re.sub(r"[^\w\u3400-\u9fff]+", "", item["topic"].casefold())
                if key:
                    existing = unique.get(key)
                    if existing is None:
                        unique[key] = item
                    elif item["basis"] == "completed_research":
                        item["basis_detail"]["related_basis"] = {
                            "basis": existing["basis"],
                            "detail": existing.get("basis_detail", {}),
                        }
                        unique[key] = item
            priority = {"completed_research": 0, "unresolved_question": 1,
                        "stable_interest": 2, "planned_goal": 3}
            ranked = sorted(unique.values(), key=lambda item: priority.get(item["basis"], 9))
            return ranked[:limit]
