"""Character-scoped persistence for bounded, evidence-backed exploration."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from collections.abc import Callable

from .db import Database
from .life import LifeRepository
from .model_adapter import ModelDailyQuotaExhausted, ModelQuotaUnavailable


def _now() -> str:
    return datetime.now(UTC).isoformat()


def verified_read_ids(evidence: list[dict]) -> set[str]:
    return {item["id"] for item in evidence
            if item["tool_name"] == "read_webpage" and item["outcome"] == "success"
            and item.get("page_relevance") == "relevant" and item["excerpt"]}


def _question_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold().strip()
    return re.sub(r"^继续查清\s*:\s*", "", value)


def _question_key(value: str) -> str:
    # Ignore display punctuation but retain numeric separators, operators and
    # word boundaries in Latin text (e.g. "1.5" vs "15", "re sign" vs "resign").
    value = re.sub(r"\s+", " ", _question_text(value)).strip(" ?!。！？.,，、")
    return re.sub(r"(?<=[\u3400-\u9fff]) +(?=[\u3400-\u9fff])", "", value)


def _question_intent(value: str) -> tuple[str, str] | None:
    """Recognize narrow question rewrites while retaining the entire subject."""
    key = _question_key(value)
    if match := re.fullmatch(r"(.+?)的原因(?:是什么|有哪些)?", key):
        return "cause", match[1]
    markers = list(re.finditer(r"为什么|为何|如何|怎样|怎么", key))
    if len(markers) != 1:
        return None
    marker = markers[0]
    # “怎么会” can ask for a reason or express surprise.
    if key[marker.start():].startswith(("怎么会", "怎么可能")):
        return None
    subject = (key[:marker.start()] + key[marker.end():]).strip(" ,，")
    if len(subject) < 4:
        return None
    return ("cause" if marker[0] in {"为什么", "为何"} else "method"), subject


def _same_question(left: str, right: str, *, shared_source: bool) -> bool:
    if _question_key(left) == _question_key(right):
        return True
    first, second = _question_intent(left), _question_intent(right)
    if first is None or second is None or first[0] != second[0]:
        return False
    if first[1] == second[1]:
        return True
    # A shared source and candidate can disambiguate narrow wording changes.
    # Preserve qualifiers, quantities, and every other word.
    return shared_source and first[1].replace("运用", "使用") == second[1].replace("运用", "使用")


def _basis_details(value: str | None) -> tuple[str | None, dict]:
    if value is None:
        return None, {}
    basis, _, detail = value.partition("\n")
    try:
        decoded = json.loads(detail) if detail else {}
    except json.JSONDecodeError:
        decoded = {}
    return basis, decoded if isinstance(decoded, dict) else {}


def _shares_basis(left: dict, right: dict) -> bool:
    candidate = left.get("candidate_topic")
    if not candidate or candidate != right.get("candidate_topic") or left.get("source") != right.get("source"):
        return False
    return any(set(left.get(key, [])) & set(right.get(key, []))
               for key in ("event_ids", "change_ids", "evidence_ids"))


class DuplicateResearchFinding(ValueError):
    pass


class ExplorationRepository:
    def __init__(self, database: Database, life: LifeRepository):
        self.database = database
        self.life = life

    def create(self, *, character_id: str, idempotency_key: str,
               requested_topic: str | None, trigger_mode: str = "manual",
               topic_basis: str | None = None, enforce_single_active: bool = False,
               quota_snapshot: Callable[[], dict] | None = None,
               retry_run_id: str | None = None,
               autonomous_paused: bool = False) -> tuple[dict, bool]:
        if trigger_mode not in {"manual", "autonomous"}:
            raise ValueError("无效的探索触发方式")
        if retry_run_id is not None:
            if requested_topic is not None:
                raise ValueError("重试时沿用原探索主题")
            idempotency_key = f"retry:{retry_run_id}"
        elif idempotency_key.startswith("retry:"):
            raise ValueError("无效的探索请求标识")
        current = datetime.now(UTC)
        at, run_id = current.isoformat(), str(uuid4())
        with self.database.atomic() as db:
            previous = self._get(db, retry_run_id, character_id) if retry_run_id is not None else None
            if previous and previous["status"] != "interrupted":
                raise RuntimeError("只有已中断的探索可以重试")
            existing = db.execute(
                "SELECT * FROM exploration_runs WHERE character_id=? AND idempotency_key=?",
                (character_id, idempotency_key),
            ).fetchone()
            if existing:
                return self._run(db, dict(existing)), False
            if (enforce_single_active or previous) and self._has_active(db, character_id):
                raise RuntimeError("该角色已有进行中的探索任务")
            selected_topic = None
            if previous:
                requested_topic = previous["requested_topic"]
                selected_topic = previous["selected_topic"]
                topic_basis = previous["topic_basis"]
                trigger_mode = previous["trigger_mode"]
                if trigger_mode == "autonomous":
                    prefs = db.execute("SELECT * FROM exploration_autonomy WHERE character_id=?", (character_id,)).fetchone()
                    blocker = "paused" if autonomous_paused else self._autonomy_blocker(prefs, current)
                    if blocker:
                        raise RuntimeError({
                            "disabled": "自主探索已关闭",
                            "paused": "自主探索已暂停",
                            "cooldown": "自主探索仍在冷却中",
                        }[blocker])
            origin = "user" if requested_topic else "character"
            if quota_snapshot:
                quota = quota_snapshot()
                if quota["reason"] == "exploration_quota_unavailable":
                    raise ModelQuotaUnavailable("探索额度暂时无法核验，请检查本地调用记录。")
                if not quota["available"]:
                    raise ModelDailyQuotaExhausted("今日探索额度已用完，请在额度重置后再试。")
            db.execute(
                "INSERT INTO exploration_runs(id,character_id,idempotency_key,requested_topic,selected_topic,topic_origin,status,created_at,updated_at,trigger_mode,topic_basis) VALUES(?,?,?,?,?,?,'planned',?,?,?,?)",
                (run_id, character_id, idempotency_key, requested_topic, selected_topic, origin, at, at, trigger_mode, topic_basis),
            )
            if previous and trigger_mode == "autonomous":
                self._record_autonomous_start(db, character_id, at, run_id)
            return self._run(db, dict(db.execute("SELECT * FROM exploration_runs WHERE id=?", (run_id,)).fetchone())), True

    def activate(self, run_id: str, character_id: str) -> dict | None:
        at = _now()
        with self.database.atomic() as db:
            claimed = db.execute("UPDATE exploration_runs SET status='active',started_at=?,updated_at=? WHERE id=? AND character_id=? AND status='planned'", (at, at, run_id, character_id))
            return self._run(db, self._get(db, run_id, character_id)) if claimed.rowcount else None

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

    def create_autonomous_if_due(self, character_id: str, *, now: datetime | None = None,
                                 quota_snapshot: Callable[[], dict] | None = None) -> tuple[dict | None, bool]:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        at = current.isoformat()
        with self.database.atomic() as db:
            prefs = db.execute("SELECT * FROM exploration_autonomy WHERE character_id=?", (character_id,)).fetchone()
            if not prefs or not prefs["enabled"]:
                return None, False
            blocker = self._autonomy_blocker(prefs, current)
            if blocker != "paused" and self._has_active(db, character_id):
                blocker = "active_task"
            if blocker:
                db.execute("UPDATE exploration_autonomy SET last_checked_at=?,last_skip_reason=?,updated_at=? WHERE character_id=?",
                           (at, blocker, at, character_id))
                return None, False
            key = "autonomous:" + current.strftime("%Y-%m-%d")
            if db.execute("SELECT 1 FROM exploration_runs WHERE character_id=? AND idempotency_key=?",
                          (character_id, key)).fetchone():
                return None, False
            if not self.life.exploration_candidate_items(character_id):
                db.execute("UPDATE exploration_autonomy SET last_checked_at=?,last_skip_reason='no_eligible_topic',updated_at=? WHERE character_id=?", (at, at, character_id))
                return None, False
            if quota_snapshot:
                quota = quota_snapshot()
                if not quota["available"]:
                    db.execute("UPDATE exploration_autonomy SET last_checked_at=?,last_skip_reason=?,updated_at=? WHERE character_id=?",
                               (at, quota["reason"], at, character_id))
                    return None, False
            run_id = str(uuid4())
            db.execute("INSERT INTO exploration_runs(id,character_id,idempotency_key,requested_topic,topic_origin,status,created_at,updated_at,trigger_mode,topic_basis) VALUES(?,?,?,NULL,'character','planned',?,?, 'autonomous','pending_selection')", (run_id, character_id, key, at, at))
            self._record_autonomous_start(db, character_id, at, run_id)
            return self._run(db, dict(db.execute("SELECT * FROM exploration_runs WHERE id=?", (run_id,)).fetchone())), True

    @staticmethod
    def _has_active(db, character_id: str) -> bool:
        return db.execute("SELECT 1 FROM exploration_runs WHERE character_id=? AND status IN ('planned','active') LIMIT 1",
                          (character_id,)).fetchone() is not None

    @staticmethod
    def _autonomy_blocker(prefs, current: datetime) -> str | None:
        if not prefs or not prefs["enabled"]:
            return "disabled"
        if prefs["paused_until"] and datetime.fromisoformat(prefs["paused_until"]).astimezone(UTC) > current:
            return "paused"
        if prefs["last_started_at"] and current - datetime.fromisoformat(prefs["last_started_at"]).astimezone(UTC) < timedelta(hours=prefs["cooldown_hours"]):
            return "cooldown"
        return None

    @staticmethod
    def _record_autonomous_start(db, character_id: str, at: str, run_id: str) -> None:
        db.execute("UPDATE exploration_autonomy SET last_started_at=?,last_run_id=?,last_checked_at=?,last_skip_reason=NULL,updated_at=? WHERE character_id=?",
                   (at, run_id, at, at, character_id))

    def select_topic(self, run_id: str, character_id: str, topic: str, basis: str, detail: dict) -> bool:
        if basis not in {"stable_interest", "unresolved_question", "planned_goal", "derived_question", "completed_research"}:
            raise ValueError("无效的探索选题依据")
        stored_basis = basis + "\n" + json.dumps(detail, ensure_ascii=False, separators=(",", ":"))
        with self.database.atomic() as db:
            if self.topic_was_explored(character_id, topic, exclude_run_id=run_id, basis_detail=detail):
                return False
            return db.execute(
                "UPDATE exploration_runs SET selected_topic=?,topic_basis=?,updated_at=? WHERE id=? AND character_id=? AND status='active'",
                (topic, stored_basis, _now(), run_id, character_id),
            ).rowcount == 1

    def topic_was_explored(self, character_id: str, topic: str, *, exclude_run_id: str | None = None,
                           basis_detail: dict | None = None, retry: bool = False) -> bool:
        """Flag clear duplicates while leaving inquiries with insufficient evidence eligible."""
        if not any(char.isalnum() for char in _question_key(topic)):
            return True
        states = ("completed", "active", "planned") if retry else ("completed", "active", "planned", "interrupted")
        with self.database.connection() as db:
            rows = db.execute(
                "SELECT requested_topic,selected_topic,topic_basis FROM exploration_runs WHERE character_id=? "
                f"AND (status IN ({','.join('?' for _ in states)}) "
                "OR (status='failed' AND error_code='no_new_research_finding')) AND id<>COALESCE(?, '')",
                (character_id, *states, exclude_run_id),
            ).fetchall()
        for row in rows:
            previous = row["selected_topic"] or row["requested_topic"]
            if previous and _same_question(
                topic, previous, shared_source=_shares_basis(basis_detail or {}, _basis_details(row["topic_basis"])[1])
            ):
                return True
        return False

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
            if self._duplicates_prior_finding(db, row, character_id, claims):
                raise DuplicateResearchFinding("duplicate_research_evidence")
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

    @staticmethod
    def _duplicates_prior_finding(db, current: dict, character_id: str, claims: list[dict]) -> bool:
        """Do not record the same sourced claim from identical pages as a new discovery."""
        if current.get("trigger_mode") != "autonomous":
            return False
        hashes_by_id = dict(db.execute(
                "SELECT id,content_sha256 FROM exploration_evidence WHERE run_id=? AND character_id=? "
                "AND tool_name='read_webpage' AND outcome='success' AND page_relevance='relevant' AND content_sha256 IS NOT NULL",
                (current["id"], character_id),
        ).fetchall())
        if not hashes_by_id:
            return False
        current_hashes = sorted(set(hashes_by_id.values()))
        prior = {}
        for row in db.execute(
            "SELECT r.id AS run_id,r.claims_json,e.id AS evidence_id,e.content_sha256 "
            "FROM exploration_evidence e JOIN exploration_runs r ON r.id=e.run_id AND r.character_id=e.character_id "
            "WHERE r.character_id=? AND r.status='completed' AND e.tool_name='read_webpage' "
            "AND e.outcome='success' AND e.page_relevance='relevant' "
            f"AND e.content_sha256 IN ({','.join('?' for _ in current_hashes)})",
            (character_id, *current_hashes),
        ):
            recorded, sources = prior.setdefault(row["run_id"], (row["claims_json"], {}))
            sources[row["evidence_id"]] = row["content_sha256"]
        known: set[tuple[str, str]] = set()
        for recorded, sources in prior.values():
            try:
                old_claims = json.loads(recorded or "[]")
            except (TypeError, json.JSONDecodeError):
                continue
            for claim in old_claims:
                if isinstance(claim, dict) and isinstance(claim.get("text"), str):
                    known.update((_question_key(claim["text"]), sources[source])
                                 for source in claim.get("source_ids", []) if source in sources)
        for claim in claims:
            cited = [hashes_by_id.get(source) for source in claim["source_ids"]]
            if not cited or any((_question_key(claim["text"]), digest) not in known for digest in cited):
                return False
        return bool(claims)

    def terminate(self, run_id: str, character_id: str, status: str, *, error_code: str | None = None,
                  summary: str | None = None, unanswered: list[str] | None = None) -> dict:
        if status not in {"failed", "cancelled", "interrupted"}:
            raise ValueError("无效的探索结束状态")
        at = _now()
        with self.database.atomic() as db:
            row = self._get(db, run_id, character_id)
            if row["status"] in {"completed", "failed", "cancelled", "interrupted"}:
                return self._run(db, row)
            phase = "inconclusive" if error_code in {"insufficient_evidence", "no_new_research_finding"} else status
            unanswered_json = json.dumps([" ".join(item.split())[:240] for item in (unanswered or [])[:8]], ensure_ascii=False, separators=(",", ":")) if unanswered is not None else row.get("unanswered_json")
            db.execute("UPDATE exploration_runs SET status=?,phase=?,error_code=?,summary=COALESCE(?,summary),unanswered_json=?,ended_at=?,updated_at=?,duration_ms=? WHERE id=? AND character_id=?", (status, phase, (error_code or "")[:80] or None, " ".join(summary.split())[:1800] if summary else None, unanswered_json, at, at, self._duration_ms(row, at), run_id, character_id))
            return self._run(db, self._get(db, run_id, character_id))

    def recover_active(self, character_id: str = "big_fish") -> int:
        at = _now()
        with self.database.atomic() as db:
            # A process may exit after persisting a planned task but before its
            # asyncio task gets CPU time. Such work cannot be safely resumed;
            # close it just like active work so it cannot occupy the character
            # forever or be mistaken for a successful exploration.
            rows = db.execute("SELECT id,started_at FROM exploration_runs WHERE status IN ('planned','active') AND character_id=?", (character_id,)).fetchall()
            for row in rows:
                duration = self._duration_ms(dict(row), at)
                db.execute("UPDATE exploration_runs SET status='interrupted',phase='interrupted',error_code='service_restarted',ended_at=?,updated_at=?,duration_ms=? WHERE id=? AND character_id=? AND status IN ('planned','active')", (at, at, duration, row["id"], character_id))
            return len(rows)

    def get(self, run_id: str, character_id: str) -> dict:
        with self.database.connection() as db:
            return self._run(db, self._get(db, run_id, character_id))

    def list(self, character_id: str, limit: int = 20) -> list[dict]:
        with self.database.connection() as db:
            rows = db.execute("SELECT * FROM exploration_runs WHERE character_id=? ORDER BY created_at DESC,id LIMIT ?", (character_id, max(1, min(limit, 50)))).fetchall()
            return [self._run(db, dict(row)) for row in rows]

    def completed_since(self, character_id: str, since: str) -> list[dict]:
        with self.database.connection() as db:
            rows = db.execute(
                """SELECT * FROM exploration_runs
                   WHERE character_id=? AND status='completed' AND ended_at>?
                   ORDER BY ended_at DESC,id LIMIT 20""", (character_id, since),
            ).fetchall()
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
        result["topic_basis"], result["topic_basis_detail"] = _basis_details(result["topic_basis"])
        result["retry_of"] = result["idempotency_key"][len("retry:"):] if result["idempotency_key"].startswith("retry:") else None
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
