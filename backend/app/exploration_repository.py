"""Character-scoped persistence for bounded, evidence-backed exploration."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
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
               requested_topic: str | None) -> tuple[dict, bool]:
        at, run_id = _now(), str(uuid4())
        origin = "user" if requested_topic else "character"
        with self.database.atomic() as db:
            existing = db.execute(
                "SELECT * FROM exploration_runs WHERE character_id=? AND idempotency_key=?",
                (character_id, idempotency_key),
            ).fetchone()
            if existing:
                return self._run(db, dict(existing)), False
            db.execute(
                "INSERT INTO exploration_runs(id,character_id,idempotency_key,requested_topic,topic_origin,status,created_at,updated_at) VALUES(?,?,?,?,?,'planned',?,?)",
                (run_id, character_id, idempotency_key, requested_topic, origin, at, at),
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
                     provider: str | None = None, relevance: str | None = None) -> dict:
        if provider is not None and tool_name != "search_web":
            raise ValueError("搜索来源只能附加到 search_web 证据")
        if relevance is not None and (tool_name != "search_web" or relevance not in {"relevant", "irrelevant"}):
            raise ValueError("搜索相关性状态无效")
        at, evidence_id = _now(), str(uuid4())
        clean_excerpt = " ".join((excerpt or "").split())[:8000] or None
        digest = hashlib.sha256(clean_excerpt.encode("utf-8")).hexdigest() if clean_excerpt else None
        with self.database.atomic() as db:
            run = self._get(db, run_id, character_id)
            if run["status"] != "active":
                raise ValueError("只有进行中的探索可以追加资料")
            seq = db.execute("SELECT COALESCE(MAX(sequence_no),0)+1 FROM exploration_evidence WHERE run_id=? AND character_id=?", (run_id, character_id)).fetchone()[0]
            db.execute(
                "INSERT INTO exploration_evidence(id,run_id,character_id,sequence_no,tool_name,query_text,source_url,title,retrieved_at,excerpt,content_sha256,outcome,error_code,duration_ms,created_at,provider,relevance) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (evidence_id, run_id, character_id, seq, tool_name, (query_text or "")[:240] or None,
                 (source_url or "")[:2000] or None, (title or "")[:240] or None, retrieved_at,
                 clean_excerpt, digest, outcome, error_code, duration_ms, at, provider, relevance),
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
            sources = db.execute("SELECT COUNT(*) FROM exploration_evidence WHERE run_id=? AND character_id=? AND tool_name='read_webpage' AND outcome='success' AND excerpt IS NOT NULL", (run_id, character_id)).fetchone()[0]
            if sources < 1:
                raise ValueError("没有成功阅读的来源，不能记录为已完成探索")
            title = row["selected_topic"] or row["requested_topic"] or "自主探索"
            content = f"探索主题：{title}。{summary}"
            event = self.life.record(content, character_id=character_id, event_type="research", status="completed",
                                     source="web_research", generation_method="exploration_agent",
                                     idempotency_key=f"exploration:{run_id}")
            db.execute("UPDATE exploration_runs SET status='completed',summary=?,claims_json=?,unanswered_json=?,life_event_id=?,ended_at=?,updated_at=?,duration_ms=? WHERE id=? AND character_id=?",
                       (summary, claims_json, unanswered_json, event["id"], at, at, self._duration_ms(row, at), run_id, character_id))
            return self._run(db, self._get(db, run_id, character_id))

    def terminate(self, run_id: str, character_id: str, status: str, *, error_code: str | None = None) -> dict:
        if status not in {"failed", "cancelled", "interrupted"}:
            raise ValueError("无效的探索结束状态")
        at = _now()
        with self.database.connection() as db:
            row = self._get(db, run_id, character_id)
            if row["status"] in {"completed", "failed", "cancelled", "interrupted"}:
                return self._run(db, row)
            db.execute("UPDATE exploration_runs SET status=?,error_code=?,ended_at=?,updated_at=?,duration_ms=? WHERE id=? AND character_id=?", (status, (error_code or "")[:80] or None, at, at, self._duration_ms(row, at), run_id, character_id))
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
