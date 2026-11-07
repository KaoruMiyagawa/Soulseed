"""Character-scoped persistence for bounded, evidence-backed exploration."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from .db import Database
from .life import LifeRepository


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ExplorationRepository:
    def __init__(self, database: Database, life: LifeRepository):
        self.database = database
        self.life = life

    def create(self, *, character_id: str, idempotency_key: str,
               requested_topic: str | None, trigger_mode: str = "manual",
               topic_basis: str | None = None, enforce_single_active: bool = False) -> tuple[dict, bool]:
        if trigger_mode not in {"manual", "autonomous"}:
            raise ValueError("无效的探索触发方式")
        at, run_id = _now(), str(uuid4())
        origin = "user" if requested_topic else "character"
        with self.database.atomic() as db:
            existing = db.execute(
                "SELECT * FROM exploration_runs WHERE character_id=? AND idempotency_key=?",
                (character_id, idempotency_key),
            ).fetchone()
            if existing:
                return self._run(db, dict(existing)), False
            if enforce_single_active and db.execute("SELECT 1 FROM exploration_runs WHERE character_id=? AND status IN ('planned','active') LIMIT 1", (character_id,)).fetchone():
                raise RuntimeError("该角色已有进行中的探索任务")
            db.execute(
                "INSERT INTO exploration_runs(id,character_id,idempotency_key,requested_topic,topic_origin,status,created_at,updated_at,trigger_mode,topic_basis) VALUES(?,?,?,?,?,'planned',?,?,?,?)",
                (run_id, character_id, idempotency_key, requested_topic, origin, at, at, trigger_mode, topic_basis),
            )
            return self._run(db, dict(db.execute("SELECT * FROM exploration_runs WHERE id=?", (run_id,)).fetchone())), True

    def activate(self, run_id: str, character_id: str) -> dict:
        at = _now()
        with self.database.atomic() as db:
            row = self._get(db, run_id, character_id)
            if row["status"] == "planned":
                db.execute("UPDATE exploration_runs SET status='active',started_at=?,updated_at=? WHERE id=? AND character_id=? AND status='planned'", (at, at, run_id, character_id))
            return self._run(db, self._get(db, run_id, character_id))

    def set_topic(self, run_id: str, character_id: str, topic: str) -> None:
        with self.database.connection() as db:
            db.execute("UPDATE exploration_runs SET selected_topic=?,updated_at=? WHERE id=? AND character_id=? AND status='active'", (topic[:240], _now(), run_id, character_id))

    def set_phase(self, run_id: str, character_id: str, phase: str) -> None:
        if phase not in {"planning", "searching", "reading", "evaluating", "completed", "inconclusive", "failed", "cancelled"}:
            raise ValueError("无效的探索阶段")
        with self.database.connection() as db:
            db.execute("UPDATE exploration_runs SET phase=?,updated_at=? WHERE id=? AND character_id=? AND status IN ('planned','active')", (phase, _now(), run_id, character_id))

    def record_timing(self, run_id: str, character_id: str, stage: str, duration_ms: int) -> None:
        if stage not in {"model", "search", "read", "evaluation", "persistence"}:
            raise ValueError("无效的阶段计时")
        with self.database.atomic() as db:
            row = self._get(db, run_id, character_id)
            timings = json.loads(row.get("timings_json") or "{}")
            value = timings.setdefault(stage, {"count": 0, "total_ms": 0, "last_ms": 0})
            value["count"] += 1
            value["total_ms"] += max(0, int(duration_ms))
            value["last_ms"] = max(0, int(duration_ms))
            db.execute("UPDATE exploration_runs SET timings_json=?,updated_at=? WHERE id=? AND character_id=?", (json.dumps(timings, separators=(",", ":")), _now(), run_id, character_id))

    def autonomy_preferences(self, character_id: str) -> dict:
        with self.database.connection() as db:
            row = db.execute("SELECT enabled,paused_until,cooldown_hours,last_started_at,last_run_id,last_checked_at,last_skip_reason,updated_at FROM exploration_autonomy WHERE character_id=?", (character_id,)).fetchone()
            if not row:
                return {"enabled": False, "paused_until": None, "cooldown_hours": 24, "last_started_at": None, "last_run_id": None, "last_checked_at": None, "last_skip_reason": None, "updated_at": None}
            result = dict(row)
            result["enabled"] = bool(result["enabled"])
            return result

    def set_autonomy_preferences(self, character_id: str, *, enabled: bool | None = None,
                                 paused_until: str | None = None, clear_pause: bool = False,
                                 cooldown_hours: int | None = None) -> dict:
        if cooldown_hours is not None and not 24 <= cooldown_hours <= 720:
            raise ValueError("自主探索间隔必须为 24 小时至 30 天")
        current = self.autonomy_preferences(character_id)
        values = {
            "enabled": int(current["enabled"] if enabled is None else enabled),
            "paused_until": None if clear_pause else current["paused_until"] if paused_until is None else paused_until,
            "cooldown_hours": current["cooldown_hours"] if cooldown_hours is None else cooldown_hours,
        }
        at = _now()
        with self.database.atomic() as db:
            db.execute("INSERT INTO exploration_autonomy(character_id,enabled,paused_until,cooldown_hours,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(character_id) DO UPDATE SET enabled=excluded.enabled,paused_until=excluded.paused_until,cooldown_hours=excluded.cooldown_hours,updated_at=excluded.updated_at", (character_id, values["enabled"], values["paused_until"], values["cooldown_hours"], at))
        return self.autonomy_preferences(character_id)

    def create_autonomous_if_due(self, character_id: str, *, now: datetime | None = None) -> tuple[dict | None, bool]:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        at = current.isoformat()
        with self.database.atomic() as db:
            prefs = db.execute("SELECT * FROM exploration_autonomy WHERE character_id=?", (character_id,)).fetchone()
            if not prefs or not prefs["enabled"]:
                return None, False
            paused_until = prefs["paused_until"]
            if paused_until and datetime.fromisoformat(paused_until).astimezone(UTC) > current:
                db.execute("UPDATE exploration_autonomy SET last_checked_at=?,last_skip_reason='paused',updated_at=? WHERE character_id=?", (at, at, character_id))
                return None, False
            active = db.execute("SELECT id FROM exploration_runs WHERE character_id=? AND status IN ('planned','active') LIMIT 1", (character_id,)).fetchone()
            if active:
                db.execute("UPDATE exploration_autonomy SET last_checked_at=?,last_skip_reason='active_task',updated_at=? WHERE character_id=?", (at, at, character_id))
                return None, False
            last_started = prefs["last_started_at"]
            if last_started and current - datetime.fromisoformat(last_started).astimezone(UTC) < timedelta(hours=prefs["cooldown_hours"]):
                db.execute("UPDATE exploration_autonomy SET last_checked_at=?,last_skip_reason='cooldown',updated_at=? WHERE character_id=?", (at, at, character_id))
                return None, False
            if not self.life.exploration_candidate_items(character_id):
                db.execute("UPDATE exploration_autonomy SET last_checked_at=?,last_skip_reason='no_eligible_topic',updated_at=? WHERE character_id=?", (at, at, character_id))
                return None, False
            run_id = str(uuid4())
            key = "autonomous:" + current.strftime("%Y-%m-%d")
            db.execute("INSERT INTO exploration_runs(id,character_id,idempotency_key,requested_topic,topic_origin,status,created_at,updated_at,trigger_mode,topic_basis) VALUES(?,?,?,NULL,'character','planned',?,?, 'autonomous','pending_selection')", (run_id, character_id, key, at, at))
            db.execute("UPDATE exploration_autonomy SET last_started_at=?,last_run_id=?,last_checked_at=?,last_skip_reason=NULL,updated_at=? WHERE character_id=?", (at, run_id, at, at, character_id))
            return self._run(db, dict(db.execute("SELECT * FROM exploration_runs WHERE id=?", (run_id,)).fetchone())), True

    def set_topic_basis(self, run_id: str, character_id: str, basis: str) -> None:
        if basis not in {"stable_interest", "unresolved_question", "planned_goal", "derived_question"}:
            raise ValueError("无效的探索选题依据")
        with self.database.connection() as db:
            db.execute("UPDATE exploration_runs SET topic_basis=?,updated_at=? WHERE id=? AND character_id=? AND status='active'", (basis, _now(), run_id, character_id))

    def step(self, run_id: str, character_id: str, *, model_call: bool = False,
             input_chars: int = 0) -> None:
        with self.database.connection() as db:
            db.execute("UPDATE exploration_runs SET step_count=step_count+1, model_call_count=model_call_count+?,input_char_count=input_char_count+?,updated_at=? WHERE id=? AND character_id=? AND status='active'", (int(model_call), max(0, input_chars), _now(), run_id, character_id))

    def record_tool_call(self, run_id: str, character_id: str) -> None:
        with self.database.connection() as db:
            db.execute("UPDATE exploration_runs SET tool_call_count=tool_call_count+1,updated_at=? WHERE id=? AND character_id=? AND status='active'", (_now(), run_id, character_id))

    def add_evidence(self, run_id: str, character_id: str, *, tool_name: str,
                     query_text: str | None = None, source_url: str | None = None,
                     title: str | None = None, retrieved_at: str | None = None,
                     excerpt: str | None = None, outcome: str,
                     error_code: str | None = None, duration_ms: int | None = None,
                     provider: str | None = None, relevance: str | None = None,
                     page_relevance: str | None = None) -> dict:
        if provider is not None and tool_name != "search_web":
            raise ValueError("搜索来源只能附加到 search_web 证据")
        if relevance is not None and (tool_name != "search_web" or relevance not in {"relevant", "irrelevant"}):
            raise ValueError("搜索相关性状态无效")
        if page_relevance is not None and (tool_name != "read_webpage" or page_relevance not in {"relevant", "irrelevant"}):
            raise ValueError("网页正文相关性状态无效")
        at, evidence_id = _now(), str(uuid4())
        clean_excerpt = " ".join((excerpt or "").split())[:8000] or None
        digest = hashlib.sha256(clean_excerpt.encode("utf-8")).hexdigest() if clean_excerpt else None
        with self.database.atomic() as db:
            run = self._get(db, run_id, character_id)
            if run["status"] != "active":
                raise ValueError("只有进行中的探索可以追加资料")
            seq = db.execute("SELECT COALESCE(MAX(sequence_no),0)+1 FROM exploration_evidence WHERE run_id=? AND character_id=?", (run_id, character_id)).fetchone()[0]
            db.execute(
                "INSERT INTO exploration_evidence(id,run_id,character_id,sequence_no,tool_name,query_text,source_url,title,retrieved_at,excerpt,content_sha256,outcome,error_code,duration_ms,created_at,provider,relevance,page_relevance) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (evidence_id, run_id, character_id, seq, tool_name, (query_text or "")[:240] or None,
                 (source_url or "")[:2000] or None, (title or "")[:240] or None, retrieved_at,
                 clean_excerpt, digest, outcome, error_code, duration_ms, at, provider, relevance, page_relevance),
            )
            return dict(db.execute("SELECT * FROM exploration_evidence WHERE id=?", (evidence_id,)).fetchone())

    def list_evidence(self, run_id: str, character_id: str) -> list[dict]:
        with self.database.connection() as db:
            self._get(db, run_id, character_id)
            return [dict(row) for row in db.execute("SELECT * FROM exploration_evidence WHERE run_id=? AND character_id=? ORDER BY sequence_no", (run_id, character_id))]

    def complete(self, run_id: str, character_id: str, *, summary: str,
                 claims: list[dict], unanswered: list[str]) -> dict:
        at = _now()
        summary = " ".join(summary.split())[:1800]
        claims_json = json.dumps(claims[:8], ensure_ascii=False, separators=(",", ":"))
        unanswered_json = json.dumps([" ".join(x.split())[:240] for x in unanswered[:8]], ensure_ascii=False, separators=(",", ":"))
        with self.database.atomic() as db:
            row = self._get(db, run_id, character_id)
            if row["status"] != "active":
                raise ValueError("只有进行中的探索可以完成")
            sources = db.execute("SELECT COUNT(*) FROM exploration_evidence WHERE run_id=? AND character_id=? AND tool_name='read_webpage' AND outcome='success' AND page_relevance='relevant' AND excerpt IS NOT NULL", (run_id, character_id)).fetchone()[0]
            if sources < 1 or not claims:
                raise ValueError("缺少相关来源或有依据的结论，不能记录为已完成探索")
            title = row["selected_topic"] or row["requested_topic"] or "自主探索"
            content = f"探索主题：{title}。{summary}"
            event = self.life.record(content, character_id=character_id, event_type="research", status="completed",
                                     source="web_research", generation_method="exploration_agent",
                                     idempotency_key=f"exploration:{run_id}")
            db.execute("UPDATE exploration_runs SET status='completed',phase='completed',summary=?,claims_json=?,unanswered_json=?,life_event_id=?,ended_at=?,updated_at=?,duration_ms=? WHERE id=? AND character_id=?",
                       (summary, claims_json, unanswered_json, event["id"], at, at, self._duration_ms(row, at), run_id, character_id))
            completed = self._run(db, self._get(db, run_id, character_id))
        # Share eligibility is evaluated only after the completed result and its
        # source links are durable. LifeShareService records a skip decision too.
        from .life_share import LifeShareService
        LifeShareService(self.life).consider(event["id"])
        return completed

    def terminate(self, run_id: str, character_id: str, status: str, *, error_code: str | None = None,
                  summary: str | None = None, unanswered: list[str] | None = None) -> dict:
        if status not in {"failed", "cancelled", "interrupted"}:
            raise ValueError("无效的探索结束状态")
        at = _now()
        with self.database.connection() as db:
            row = self._get(db, run_id, character_id)
            if row["status"] in {"completed", "failed", "cancelled", "interrupted"}:
                return self._run(db, row)
            phase = "inconclusive" if error_code == "insufficient_evidence" else status
            unanswered_json = json.dumps([" ".join(item.split())[:240] for item in (unanswered or [])[:8]], ensure_ascii=False, separators=(",", ":")) if unanswered is not None else row.get("unanswered_json")
            db.execute("UPDATE exploration_runs SET status=?,phase=?,error_code=?,summary=COALESCE(?,summary),unanswered_json=?,ended_at=?,updated_at=?,duration_ms=? WHERE id=? AND character_id=?", (status, phase, (error_code or "")[:80] or None, " ".join(summary.split())[:1800] if summary else None, unanswered_json, at, at, self._duration_ms(row, at), run_id, character_id))
            return self._run(db, self._get(db, run_id, character_id))

    def recover_active(self, character_id: str = "big_fish") -> int:
        at = _now()
        with self.database.atomic() as db:
            rows = db.execute("SELECT id,started_at FROM exploration_runs WHERE status='active' AND character_id=?", (character_id,)).fetchall()
            for row in rows:
                duration = self._duration_ms(dict(row), at)
                db.execute("UPDATE exploration_runs SET status='interrupted',error_code='service_restarted',ended_at=?,updated_at=?,duration_ms=? WHERE id=? AND character_id=? AND status='active'", (at, at, duration, row["id"], character_id))
            return len(rows)

    def get(self, run_id: str, character_id: str) -> dict:
        with self.database.connection() as db:
            return self._run(db, self._get(db, run_id, character_id))

    def list(self, character_id: str, limit: int = 20) -> list[dict]:
        with self.database.connection() as db:
            rows = db.execute("SELECT * FROM exploration_runs WHERE character_id=? ORDER BY created_at DESC,id LIMIT ?", (character_id, max(1, min(limit, 50)))).fetchall()
            return [self._run(db, dict(row)) for row in rows]

    def _get(self, db, run_id: str, character_id: str) -> dict:
        row = db.execute("SELECT * FROM exploration_runs WHERE id=? AND character_id=?", (run_id, character_id)).fetchone()
        if not row:
            raise KeyError(run_id)
        return dict(row)

    def _run(self, db, row: dict) -> dict:
        result = dict(row)
        result["claims"] = json.loads(result.pop("claims_json") or "[]")
        result["unanswered"] = json.loads(result.pop("unanswered_json") or "[]")
        result["timings"] = json.loads(result.pop("timings_json") or "{}")
        result["evidence"] = [dict(item) for item in db.execute("SELECT * FROM exploration_evidence WHERE run_id=? AND character_id=? ORDER BY sequence_no", (row["id"], row["character_id"]))]
        return result

    @staticmethod
    def _duration_ms(row: dict, ended_at: str) -> int | None:
        if not row.get("started_at"):
            return None
        try:
            return max(0, int((datetime.fromisoformat(ended_at) - datetime.fromisoformat(row["started_at"])).total_seconds() * 1000))
        except (TypeError, ValueError):
            return None
