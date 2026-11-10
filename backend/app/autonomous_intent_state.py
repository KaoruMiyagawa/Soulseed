"""Persistent candidate, budget reservation, and permission recheck foundation.

No method here calls a model or creates ContactService rows. Real handoff and
delivery remain owned by ContactService and are intentionally not wired in D2.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .autonomous_intent import (Candidate, _INVITATION, _NEGATIVE, _STOP, _time,
                                _key, _valid_url, build_snapshot, generate_candidates)
from .db import Database
from .ownership import current_character

_AFFIRMATIVE = ("好", "可以", "愿意", "期待", "想聊", "下次", "改天", "回头")


@dataclass(frozen=True)
class StateResult:
    status: str
    candidate_id: str
    created: bool = False
    token: str | None = None
    reservation_id: str | None = None
    reason_code: str | None = None
    retry_at: str | None = None


@dataclass(frozen=True)
class RecheckResult:
    allowed: bool
    candidate_id: str
    stage: str
    reason_code: str
    checked_at: str
    retry_at: str | None = None


def normalized_topic(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).casefold()
    return "".join(ch for ch in text if not ch.isspace() and unicodedata.category(ch)[0] not in {"P", "Z", "C"})[:200]


def topic_fingerprint(value: str) -> str | None:
    normalized = normalized_topic(value)
    if len(normalized) < 2:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


class AutonomousIntentStateRepository:
    """SQLite source of truth for candidate processing and autonomous budgets."""

    def __init__(self, database: Database, *, enabled: bool = False,
                 daily_model_request_limit: int = 2, daily_message_limit: int = 1,
                 lease_duration: timedelta = timedelta(minutes=2), topic_dedup_days: int = 30,
                 contact_enabled: bool = True, handoff_enabled: bool = False):
        if daily_model_request_limit < 1 or daily_message_limit < 1:
            raise ValueError("autonomous budgets must be positive")
        if lease_duration <= timedelta(0) or not 1 <= topic_dedup_days <= 365:
            raise ValueError("invalid lease or topic deduplication window")
        self.database = database
        self.enabled = enabled
        self.daily_model_request_limit = daily_model_request_limit
        self.daily_message_limit = daily_message_limit
        self.lease_duration = lease_duration
        self.topic_dedup_days = topic_dedup_days
        self.contact_enabled = contact_enabled
        self.handoff_enabled = handoff_enabled

    @classmethod
    def from_settings(cls, database: Database, settings) -> "AutonomousIntentStateRepository":
        """Bind fail-closed runtime configuration without starting a scheduler."""
        return cls(database,
                   enabled=settings.autonomous_contact_generation_enabled,
                   daily_model_request_limit=settings.autonomous_contact_daily_model_request_limit,
                   daily_message_limit=settings.autonomous_contact_daily_message_limit,
                   lease_duration=timedelta(minutes=settings.autonomous_contact_candidate_lease_minutes),
                   topic_dedup_days=settings.autonomous_contact_topic_dedup_days,
                   contact_enabled=settings.contact_enabled,
                   handoff_enabled=settings.autonomous_contact_handoff_enabled)

    def _require_enabled_scope(self, character_id: str) -> None:
        if not self.enabled:
            raise RuntimeError("autonomous_contact_disabled")
        if not character_id or current_character.get() != character_id:
            raise ValueError("character_scope_mismatch")

    def _topic_for_candidate(self, candidate: Candidate) -> str | None:
        ids = {ref.kind: ref.id for ref in candidate.evidence if ref.kind != "message"}
        with self.database.connection() as db:
            previous = db.execute("PRAGMA query_only").fetchone()[0]
            db.execute("PRAGMA query_only=ON")
            try:
                db.execute("BEGIN")
                if candidate.kind == "real_discovery":
                    event_id = ids.get("life_event")
                    row = db.execute("SELECT content FROM life_events WHERE id=? AND character_id=?", (event_id, candidate.character_id)).fetchone()
                    return row["content"] if row else None
                message_ids = tuple(ref.id for ref in candidate.evidence if ref.kind == "message")
                if len(message_ids) < 2:
                    return None
                marks = ",".join("?" for _ in message_ids)
                row = db.execute(f"SELECT m.content FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE m.id IN ({marks}) AND m.role='user' AND c.character_id=?", (*message_ids, candidate.character_id)).fetchone()
                match = _INVITATION.search(row["content"]) if row else None
                return match.group(1).strip(" 的呀吧啊啦哦 ") if match else None
            finally:
                db.rollback()
                db.execute(f"PRAGMA query_only={int(previous)}")

    def persist_candidate(self, candidate: Candidate, *, now: datetime | None = None) -> StateResult:
        """Persist only a currently reproducible B candidate; store IDs, never text."""
        current = now or datetime.now(UTC)
        self._require_enabled_scope(candidate.character_id)
        if current.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        current = current.astimezone(UTC)
        # Preserve idempotence even after the ContactService handoff makes the
        # source disappear from the current B candidate set.
        with self.database.connection() as db:
            existing = db.execute("SELECT * FROM autonomous_contact_candidates WHERE id=? AND character_id=?",
                                  (candidate.dedup_key, candidate.character_id)).fetchone()
            if existing:
                if existing["source_key"] != next((ref.id for ref in candidate.evidence
                                                    if ref.kind == ("life_event" if candidate.kind == "real_discovery" else "message")), None):
                    raise ValueError("candidate_identity_mismatch")
                return StateResult(existing["status"], existing["id"], created=False,
                                   reason_code=existing["reason_code"], retry_at=existing["retry_after"])
        snapshot = build_snapshot(self.database, candidate.character_id, now=current)
        fresh = next((item for item in generate_candidates(snapshot) if item.dedup_key == candidate.dedup_key), None)
        if fresh is None:
            raise ValueError("candidate_not_currently_supported")
        if fresh.expires_at <= current:
            raise ValueError("candidate_expired")
        source_kind = "life_event" if fresh.kind == "real_discovery" else "message"
        source_id = next((ref.id for ref in fresh.evidence if ref.kind == source_kind), None)
        if not source_id:
            raise ValueError("candidate_source_missing")
        topic = self._topic_for_candidate(fresh)
        fingerprint = topic_fingerprint(topic) if topic else None
        evidence = [{"kind": ref.kind, "id": ref.id} for ref in fresh.evidence[:8]]
        evidence_json = json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))
        at, expires = _iso(current), _iso(fresh.expires_at)
        with self.database.atomic() as db:
            owner = db.execute("SELECT 1 FROM characters WHERE id=? AND ownership_status='owned'", (fresh.character_id,)).fetchone()
            if not owner:
                raise ValueError("character_not_owned")
            if not self._evidence_valid(db, fresh.character_id, fresh.kind, evidence, current):
                raise ValueError("candidate_evidence_changed")
            existing = db.execute("SELECT * FROM autonomous_contact_candidates WHERE id=?", (fresh.dedup_key,)).fetchone()
            if existing:
                return StateResult(existing["status"], existing["id"], created=False,
                                   reason_code=existing["reason_code"], retry_at=existing["retry_after"])
            db.execute("INSERT INTO autonomous_contact_candidates(id,character_id,candidate_type,source_key,evidence_json,topic_fingerprint,status,created_at,expires_at) VALUES(?,?,?,?,?,?,'pending',?,?)",
                       (fresh.dedup_key, fresh.character_id, fresh.kind, source_id, evidence_json, fingerprint, at, expires))
            if fingerprint:
                earlier = db.execute("SELECT id FROM autonomous_contact_candidates WHERE character_id=? AND id<>? AND topic_fingerprint=? AND created_at>=? AND status IN ('pending','claimed','deferred','completed','outcome_unknown') ORDER BY created_at,id LIMIT 1",
                                     (fresh.character_id, fresh.dedup_key, fingerprint, _iso(current - timedelta(days=self.topic_dedup_days)))).fetchone()
                if earlier:
                    db.execute("UPDATE autonomous_contact_candidates SET status='cancelled',reason_code='duplicate_topic' WHERE id=?", (fresh.dedup_key,))
            row = db.execute("SELECT id,status FROM autonomous_contact_candidates WHERE id=?", (fresh.dedup_key,)).fetchone()
            return StateResult(row["status"], row["id"], created=True,
                               reason_code="duplicate_topic" if row["status"] == "cancelled" else None)

    @staticmethod
    def _evidence_valid(db, character_id: str, candidate_type: str, evidence: list[dict], now: datetime) -> bool:
        refs = {item.get("kind"): item.get("id") for item in evidence if isinstance(item, dict)}
        if candidate_type == "real_discovery":
            share_id, event_id, run_id = refs.get("share"), refs.get("life_event"), refs.get("exploration")
            if not share_id or not event_id or not run_id:
                return False
            share = db.execute("""SELECT s.id,s.status,s.reason,s.delivery_channel,s.selection_token,s.selection_expires_at,
                e.event_type,e.source,e.generation_method,e.status AS event_status,e.activity_status,e.execution_status,e.occurred_at,
                r.status AS run_status,r.ended_at,r.claims_json,r.summary
                FROM life_share_intents s JOIN life_events e ON e.id=s.event_id AND e.character_id=s.character_id
                JOIN exploration_runs r ON r.life_event_id=e.id AND r.character_id=e.character_id
                WHERE s.id=? AND s.character_id=? AND e.id=? AND r.id=?""", (share_id, character_id, event_id, run_id)).fetchone()
            if (not share or share["status"] != "pending" or share["reason"] != "evidence_backed_discovery"
                    or share["delivery_channel"] not in (None, "contact") or share["event_status"] != "completed"
                    or share["activity_status"] != "completed" or share["execution_status"] == "interrupted"
                    or share["event_type"] != "research" or share["source"] != "web_research"
                    or share["generation_method"] != "exploration_agent" or share["run_status"] != "completed"):
                return False
            occurred, ended = _time(share["occurred_at"]), _time(share["ended_at"])
            if not occurred or not ended or occurred > now or ended > now:
                return False
            expiry = _time(share["selection_expires_at"])
            if share["selection_token"] and (expiry is None or expiry > now):
                return False
            source_ids = {item.get("id") for item in evidence if item.get("kind") == "source"}
            if not source_ids:
                return False
            source_rows = db.execute("""SELECT id,source_url,retrieved_at,excerpt FROM exploration_evidence
                WHERE run_id=? AND character_id=? AND tool_name='read_webpage' AND outcome='success'
                  AND page_relevance='relevant'""", (run_id, character_id)).fetchall()
            valid = {row["id"] for row in source_rows if row["id"] in source_ids and row["excerpt"]
                     and _valid_url(row["source_url"])
                     and _time(row["retrieved_at"]) and _time(row["retrieved_at"]) <= now}
            if valid != source_ids:
                return False
            try:
                claims = json.loads(share["claims_json"] or "[]")
            except (TypeError, ValueError):
                return False
            cited_claims = [claim for claim in claims if isinstance(claim, dict) and claim.get("kind") == "sourced"
                            and isinstance(claim.get("text"), str) and claim["text"].strip()
                            and isinstance(claim.get("source_ids"), list)
                            and claim["source_ids"] and set(claim["source_ids"]).issubset(valid)] if isinstance(claims, list) else []
            cited_sources = {source_id for claim in cited_claims for source_id in claim["source_ids"]}
            return bool(cited_claims) and source_ids.issubset(cited_sources) and (len(cited_claims) >= 2 or len((share["summary"] or "").strip()) >= 90)

        message_ids = [item.get("id") for item in evidence if item.get("kind") == "message"]
        if len(message_ids) < 2:
            return False
        marks = ",".join("?" for _ in message_ids)
        rows = db.execute(f"""SELECT u.id AS user_id,u.conversation_id,u.content AS user_content,u.created_at AS user_at,
            a.id AS assistant_id,a.content AS assistant_content,a.created_at AS assistant_at
            FROM messages u JOIN conversations c ON c.id=u.conversation_id AND c.character_id=?
            JOIN messages a ON a.conversation_id=u.conversation_id AND a.role='assistant'
            WHERE u.id IN ({marks}) AND a.id IN ({marks}) AND u.role='user'
              AND NOT EXISTS(SELECT 1 FROM messages n WHERE n.conversation_id=u.conversation_id AND n.role='user'
                AND (n.created_at>u.created_at OR (n.created_at=u.created_at AND n.id>u.id)))""",
                            (character_id, *message_ids, *message_ids)).fetchone()
        if not rows:
            return False
        match = _INVITATION.search(rows["user_content"])
        topic = match.group(1).strip(" 的呀吧啊啦哦 ") if match else ""
        try:
            user_at, assistant_at = datetime.fromisoformat(rows["user_at"]), datetime.fromisoformat(rows["assistant_at"])
        except ValueError:
            return False
        if user_at.tzinfo is None or assistant_at.tzinfo is None:
            return False
        age = now - user_at.astimezone(UTC)
        return (len(topic) >= 2 and len(topic) <= 30 and len(rows["user_content"]) <= 500
                and len(rows["assistant_content"]) <= 500 and timedelta(hours=24) <= age < timedelta(hours=72)
                and not rows["user_content"].__contains__("\x00") and not _NEGATIVE.search(rows["user_content"])
                and not any(stop in rows["user_content"] for stop in _STOP)
                and user_at.astimezone(UTC) < assistant_at.astimezone(UTC) <= now
                and topic in rows["assistant_content"] and not _NEGATIVE.search(rows["assistant_content"])
                and not any(stop in rows["assistant_content"] for stop in _STOP)
                and any(word in rows["assistant_content"] for word in _AFFIRMATIVE))

    def _policy(self, db, row, now: datetime, *, check_duplicate: bool = True) -> tuple[str | None, datetime | None, str | None]:
        character_id = row["character_id"]
        owner = db.execute("SELECT 1 FROM characters WHERE id=? AND ownership_status='owned'", (character_id,)).fetchone()
        if not owner:
            return "character_not_owned", None, "cancelled"
        if row["expires_at"] <= _iso(now):
            return "candidate_expired", None, "expired"
        try:
            evidence = json.loads(row["evidence_json"])
        except (TypeError, ValueError):
            return "candidate_evidence_invalid", None, "cancelled"
        source_kind = "life_event" if row["candidate_type"] == "real_discovery" else "message"
        source_id = next((item.get("id") for item in evidence if isinstance(item, dict) and item.get("kind") == source_kind), None)
        if not source_id or row["source_key"] != source_id or row["id"] != _key(character_id, row["candidate_type"], source_id):
            return "candidate_evidence_invalid", None, "cancelled"
        fingerprint = topic_fingerprint(self._topic_for_row(row, db) or "")
        if fingerprint != row["topic_fingerprint"]:
            return "candidate_evidence_invalid", None, "cancelled"
        if not self._evidence_valid(db, character_id, row["candidate_type"], evidence, now):
            return "candidate_evidence_invalid", None, "cancelled"
        pref = db.execute("SELECT allowed,quiet_start_minute,quiet_end_minute,busy_until,pause_until FROM contact_preferences WHERE character_id=?", (character_id,)).fetchone()
        state = db.execute("SELECT timezone_id FROM life_states WHERE character_id=?", (character_id,)).fetchone()
        if not self.contact_enabled:
            return "contact_globally_disabled", None, "cancelled"
        if not pref or pref["allowed"] is None or not state or not state["timezone_id"]:
            return "contact_policy_unknown", None, "deferred"
        if not pref["allowed"]:
            return "contact_not_allowed", None, "cancelled"
        try:
            zone = ZoneInfo(state["timezone_id"])
            local = now.astimezone(zone)
        except (ZoneInfoNotFoundError, ValueError):
            return "contact_policy_unknown", None, "deferred"
        start, end = pref["quiet_start_minute"], pref["quiet_end_minute"]
        if start is None or end is None:
            return "contact_policy_unknown", None, "deferred"
        minute = local.hour * 60 + local.minute
        if start != end and (start <= minute < end if start < end else minute >= start or minute < end):
            target_day = local.date() + (timedelta(days=1) if start > end and minute >= start else timedelta())
            retry_local = datetime.combine(target_day, time(end // 60, end % 60), tzinfo=zone)
            return "quiet_hours", retry_local.astimezone(UTC), "deferred"
        for value, code in ((pref["busy_until"], "busy"), (pref["pause_until"], "paused")):
            parsed = _time(value)
            if value and parsed is None:
                return "contact_policy_unknown", None, "deferred"
            if parsed and parsed > now:
                return code, parsed, "deferred"
        ready = db.execute("SELECT 1 FROM proactive_messages m JOIN contact_intents i ON i.id=m.intent_id WHERE i.character_id=? AND i.ownership_verified=1 AND i.status='ready' AND m.status='ready' LIMIT 1", (character_id,)).fetchone()
        if ready:
            return "ready_message_pending", None, "deferred"
        latest = db.execute("SELECT m.created_at,m.content FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE c.character_id=? AND m.role='user' ORDER BY m.created_at DESC,m.id DESC LIMIT 1", (character_id,)).fetchone()
        if not latest:
            return "no_user_interaction", None, "deferred"
        if any(stop in latest["content"] for stop in _STOP):
            return "user_refusal", None, "cancelled"
        latest_at = _time(latest["created_at"])
        if not latest_at or latest_at > now:
            return "contact_policy_unknown", None, "deferred"
        if now - latest_at < timedelta(hours=2):
            return "recent_user_interaction", latest_at + timedelta(hours=2), "deferred"
        delivered = db.execute("SELECT m.delivered_at,m.created_at FROM proactive_messages m JOIN contact_intents i ON i.id=m.intent_id WHERE i.character_id=? AND i.ownership_verified=1 ORDER BY m.created_at DESC,m.id DESC LIMIT 1", (character_id,)).fetchone()
        if delivered and delivered["delivered_at"]:
            delivered_at = _time(delivered["delivered_at"])
            if not delivered_at:
                return "contact_policy_unknown", None, "deferred"
            if latest_at <= delivered_at:
                return "awaiting_user_reply", None, "deferred"
        if delivered:
            last_at = _time(delivered["created_at"])
            if not last_at:
                return "contact_policy_unknown", None, "deferred"
            if now - last_at < timedelta(hours=24):
                return "contact_cooldown", last_at + timedelta(hours=24), "deferred"
        if check_duplicate:
            evidence = json.loads(row["evidence_json"])
            refs = {item.get("kind"): item.get("id") for item in evidence if isinstance(item, dict) and item.get("kind") != "message"}
            if row["candidate_type"] == "real_discovery":
                equivalent = db.execute("SELECT 1 FROM contact_intents WHERE character_id=? AND share_intent_id=? LIMIT 1",
                                        (character_id, refs.get("share"))).fetchone()
            else:
                message_ids = [item.get("id") for item in evidence if isinstance(item, dict) and item.get("kind") == "message"]
                user_id = None
                if message_ids:
                    marks = ",".join("?" for _ in message_ids)
                    source = db.execute(f"SELECT id FROM messages WHERE id IN ({marks}) AND role='user' LIMIT 1", message_ids).fetchone()
                    user_id = source["id"] if source else None
                equivalent = db.execute("SELECT 1 FROM contact_intents WHERE character_id=? AND source_id=? AND source IN ('smalltalk','conversation_followup') LIMIT 1",
                                        (character_id, user_id)).fetchone() if user_id else None
            if equivalent:
                return "equivalent_contact_intent_exists", None, "cancelled"
            fingerprint = row["topic_fingerprint"]
            if fingerprint:
                other = db.execute("SELECT 1 FROM autonomous_contact_candidates WHERE character_id=? AND id<>? AND topic_fingerprint=? AND created_at>=? AND status IN ('pending','claimed','deferred','completed','outcome_unknown') LIMIT 1",
                                   (character_id, row["id"], fingerprint, _iso(now - timedelta(days=self.topic_dedup_days)))).fetchone()
                if other:
                    return "duplicate_topic", None, "cancelled"
                intents = db.execute("SELECT topic FROM contact_intents WHERE character_id=? AND ownership_verified=1 AND created_at>=? AND status IN ('generating','ready','delivered') ORDER BY created_at DESC LIMIT 100",
                                     (character_id, _iso(now - timedelta(days=self.topic_dedup_days)))).fetchall()
                norm = normalized_topic(self._topic_for_row(row, db) or "")
                if norm and any(normalized_topic(item["topic"]) == norm for item in intents):
                    return "duplicate_contact_topic", None, "cancelled"
        return None, None, None

    @staticmethod
    def _topic_for_row(row, db) -> str | None:
        evidence = json.loads(row["evidence_json"])
        refs = {item.get("kind"): item.get("id") for item in evidence if isinstance(item, dict) and item.get("kind") != "message"}
        if row["candidate_type"] == "real_discovery":
            result = db.execute("SELECT content FROM life_events WHERE id=? AND character_id=?", (refs.get("life_event"), row["character_id"])).fetchone()
            return result["content"] if result else None
        message_ids = tuple(item.get("id") for item in evidence if isinstance(item, dict) and item.get("kind") == "message")
        if len(message_ids) < 2:
            return None
        marks = ",".join("?" for _ in message_ids)
        result = db.execute(f"SELECT m.content FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE m.id IN ({marks}) AND c.character_id=? AND m.role='user'", (*message_ids, row["character_id"])).fetchone()
        match = _INVITATION.search(result["content"]) if result else None
        return match.group(1).strip(" 的呀吧啊啦哦 ") if match else None

    def _defer_or_end(self, db, row, code: str, retry: datetime | None, terminal: str, now: datetime) -> StateResult:
        db.execute("UPDATE autonomous_contact_candidates SET status=?,reason_code=?,retry_after=?,lease_token=NULL,lease_expires_at=NULL WHERE id=?",
                   (terminal, code, _iso(retry) if retry else None, row["id"]))
        return StateResult(terminal, row["id"], reason_code=code, retry_at=_iso(retry) if retry else None)

    @staticmethod
    def _utc_day(now: datetime) -> tuple[str, datetime]:
        day = now.astimezone(UTC).date()
        reset = datetime.combine(day + timedelta(days=1), time.min, tzinfo=UTC)
        return day.isoformat(), reset

    def claim(self, candidate_id: str, character_id: str, *, now: datetime | None = None) -> StateResult:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        self._require_enabled_scope(character_id)
        at = _iso(current)
        with self.database.atomic() as db:
            row = db.execute("SELECT * FROM autonomous_contact_candidates WHERE id=? AND character_id=?", (candidate_id, character_id)).fetchone()
            if not row:
                return StateResult("missing", candidate_id, reason_code="candidate_missing")
            if row["status"] not in {"pending", "deferred"}:
                return StateResult("not_claimable", candidate_id, reason_code="candidate_not_claimable")
            if row["status"] == "deferred" and row["retry_after"] and row["retry_after"] > at:
                return StateResult("deferred", candidate_id, reason_code=row["reason_code"], retry_at=row["retry_after"])
            code, retry, terminal = self._policy(db, row, current)
            if code:
                return self._defer_or_end(db, row, code, retry, terminal or "deferred", current)
            day, reset = self._utc_day(current)
            requests = db.execute("SELECT COUNT(*) FROM autonomous_contact_reservations WHERE character_id=? AND local_day=? AND request_status<>'released'", (character_id, day)).fetchone()[0]
            if requests >= self.daily_model_request_limit:
                return self._defer_or_end(db, row, "model_budget_exhausted", reset, "deferred", current)
            messages = db.execute("SELECT COUNT(*) FROM autonomous_contact_reservations WHERE character_id=? AND local_day=? AND message_status IN ('reserved','committed')", (character_id, day)).fetchone()[0]
            if messages >= self.daily_message_limit:
                return self._defer_or_end(db, row, "message_budget_exhausted", reset, "deferred", current)
            attempt = row["claim_count"] + 1
            reservation_id = str(uuid4())
            token = str(uuid4())
            request_key = _hash([character_id, candidate_id, attempt])
            db.execute("INSERT INTO autonomous_contact_reservations(id,candidate_id,character_id,attempt_no,request_key,timezone_id,local_day,request_status,message_status,created_at,updated_at) VALUES(?,?,?,?,?,'UTC',?,'reserved','reserved',?,?)",
                       (reservation_id, candidate_id, character_id, attempt, request_key, day, at, at))
            db.execute("UPDATE autonomous_contact_candidates SET status='claimed',reason_code=NULL,retry_after=NULL,claim_count=?,lease_token=?,lease_expires_at=? WHERE id=?",
                       (attempt, token, _iso(current + self.lease_duration), candidate_id))
            return StateResult("claimed", candidate_id, token=token, reservation_id=reservation_id)

    def recheck(self, candidate_id: str, character_id: str, token: str, *, stage: str,
                now: datetime | None = None) -> RecheckResult:
        if stage not in {"pre_generation", "post_generation", "before_contact"}:
            raise ValueError("invalid_recheck_stage")
        current = (now or datetime.now(UTC)).astimezone(UTC)
        self._require_enabled_scope(character_id)
        with self.database.atomic() as db:
            row = db.execute("SELECT * FROM autonomous_contact_candidates WHERE id=? AND character_id=? AND status='claimed' AND lease_token=?",
                             (candidate_id, character_id, token)).fetchone()
            if not row:
                return RecheckResult(False, candidate_id, stage, "claim_not_current", _iso(current))
            lease = _time(row["lease_expires_at"])
            if not lease or lease <= current:
                return RecheckResult(False, candidate_id, stage, "claim_lease_expired", _iso(current))
            reservation = db.execute("SELECT * FROM autonomous_contact_reservations WHERE candidate_id=? AND character_id=? AND attempt_no=?",
                                     (candidate_id, character_id, row["claim_count"])).fetchone()
            required = "reserved" if stage == "pre_generation" else "settled"
            if not reservation or reservation["request_status"] != required:
                return RecheckResult(False, candidate_id, stage, "request_state_mismatch", _iso(current))
            if reservation["message_status"] != "reserved":
                return RecheckResult(False, candidate_id, stage, "message_slot_not_reserved", _iso(current))
            day, _ = self._utc_day(current)
            if reservation["local_day"] != day:
                if stage == "pre_generation":
                    db.execute("UPDATE autonomous_contact_reservations SET request_status='released',message_status='released',updated_at=? WHERE id=? AND request_status='reserved'", (_iso(current), reservation["id"]))
                    self._defer_or_end(db, row, "budget_day_changed_before_request", None, "pending", current)
                else:
                    db.execute("UPDATE autonomous_contact_reservations SET message_status='released',updated_at=? WHERE id=?", (_iso(current), reservation["id"]))
                    self._defer_or_end(db, row, "budget_day_changed_after_request", None, "cancelled", current)
                return RecheckResult(False, candidate_id, stage, "budget_day_changed", _iso(current))
            code, retry, terminal = self._policy(db, row, current)
            if code:
                if stage == "pre_generation" and reservation["request_status"] == "reserved":
                    db.execute("UPDATE autonomous_contact_reservations SET request_status='released',message_status='released',updated_at=? WHERE id=?", (_iso(current), reservation["id"]))
                else:
                    db.execute("UPDATE autonomous_contact_reservations SET message_status='released',updated_at=? WHERE id=?", (_iso(current), reservation["id"]))
                self._defer_or_end(db, row, code, retry, terminal or "deferred", current)
                return RecheckResult(False, candidate_id, stage, code, _iso(current), _iso(retry) if retry else None)
            return RecheckResult(True, candidate_id, stage, "eligible", _iso(current))

    def mark_request_started(self, candidate_id: str, character_id: str, token: str, *, now: datetime | None = None) -> RecheckResult:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        self._require_enabled_scope(character_id)
        with self.database.atomic() as db:
            candidate = db.execute("SELECT * FROM autonomous_contact_candidates WHERE id=? AND character_id=? AND status='claimed' AND lease_token=?", (candidate_id, character_id, token)).fetchone()
            if not candidate:
                return RecheckResult(False, candidate_id, "pre_generation", "claim_not_current", _iso(current))
            lease = _time(candidate["lease_expires_at"])
            reservation = db.execute("SELECT * FROM autonomous_contact_reservations WHERE candidate_id=? AND character_id=? AND attempt_no=?",
                                     (candidate_id, character_id, candidate["claim_count"])).fetchone()
            if not lease or lease <= current or not reservation or reservation["request_status"] != "reserved":
                return RecheckResult(False, candidate_id, "pre_generation", "claim_lease_expired", _iso(current))
            day, _ = self._utc_day(current)
            if reservation["local_day"] != day:
                db.execute("UPDATE autonomous_contact_reservations SET request_status='released',message_status='released',updated_at=? WHERE id=?", (_iso(current), reservation["id"]))
                self._defer_or_end(db, candidate, "budget_day_changed_before_request", None, "pending", current)
                return RecheckResult(False, candidate_id, "pre_generation", "budget_day_changed", _iso(current))
            code, retry, terminal = self._policy(db, candidate, current)
            if code:
                db.execute("UPDATE autonomous_contact_reservations SET request_status='released',message_status='released',updated_at=? WHERE id=?", (_iso(current), reservation["id"]))
                self._defer_or_end(db, candidate, code, retry, terminal or "deferred", current)
                return RecheckResult(False, candidate_id, "pre_generation", code, _iso(current), _iso(retry) if retry else None)
            db.execute("UPDATE autonomous_contact_reservations SET request_status='started',updated_at=? WHERE id=?", (_iso(current), reservation["id"]))
            return RecheckResult(True, candidate_id, "pre_generation", "request_started_and_budgeted", _iso(current))

    def record_request_outcome(self, candidate_id: str, character_id: str, token: str, *, outcome: str,
                               now: datetime | None = None) -> StateResult:
        if outcome not in {"success", "known_failure", "unknown"}:
            raise ValueError("invalid_request_outcome")
        current = (now or datetime.now(UTC)).astimezone(UTC)
        self._require_enabled_scope(character_id)
        with self.database.atomic() as db:
            row = db.execute("SELECT * FROM autonomous_contact_candidates WHERE id=? AND character_id=? AND status='claimed' AND lease_token=?", (candidate_id, character_id, token)).fetchone()
            reservation = db.execute("SELECT * FROM autonomous_contact_reservations WHERE candidate_id=? AND character_id=? ORDER BY attempt_no DESC LIMIT 1", (candidate_id, character_id)).fetchone()
            if not row or not reservation or reservation["request_status"] != "started":
                return StateResult("rejected", candidate_id, reason_code="request_not_started")
            if outcome == "success":
                db.execute("UPDATE autonomous_contact_reservations SET request_status='settled',updated_at=? WHERE id=?", (_iso(current), reservation["id"]))
                return StateResult("claimed", candidate_id, token=token, reservation_id=reservation["id"], reason_code="request_settled")
            if outcome == "unknown":
                db.execute("UPDATE autonomous_contact_reservations SET request_status='outcome_unknown',message_status='released',updated_at=? WHERE id=?", (_iso(current), reservation["id"]))
                return self._defer_or_end(db, row, "request_outcome_unknown_budget_consumed", None, "outcome_unknown", current)
            db.execute("UPDATE autonomous_contact_reservations SET request_status='settled',message_status='released',updated_at=? WHERE id=?", (_iso(current), reservation["id"]))
            return self._defer_or_end(db, row, "request_failed_budget_consumed", None, "failed", current)

    def release_before_request(self, candidate_id: str, character_id: str, token: str, *, now: datetime | None = None) -> StateResult:
        """Release is permitted only while request_status is reserved, before transport."""
        current = (now or datetime.now(UTC)).astimezone(UTC)
        self._require_enabled_scope(character_id)
        with self.database.atomic() as db:
            row = db.execute("SELECT * FROM autonomous_contact_candidates WHERE id=? AND character_id=? AND status='claimed' AND lease_token=?", (candidate_id, character_id, token)).fetchone()
            reservation = db.execute("SELECT * FROM autonomous_contact_reservations WHERE candidate_id=? AND character_id=? ORDER BY attempt_no DESC LIMIT 1", (candidate_id, character_id)).fetchone()
            if not row or not reservation or reservation["request_status"] != "reserved":
                return StateResult("rejected", candidate_id, reason_code="request_may_have_started")
            db.execute("UPDATE autonomous_contact_reservations SET request_status='released',message_status='released',updated_at=? WHERE id=?", (_iso(current), reservation["id"]))
            db.execute("UPDATE autonomous_contact_candidates SET status='pending',reason_code='request_proven_not_started',lease_token=NULL,lease_expires_at=NULL WHERE id=?", (candidate_id,))
            return StateResult("pending", candidate_id, reason_code="request_proven_not_started")

    def cancel_candidate(self, candidate_id: str, character_id: str, *, reason: str = "cancelled",
                         now: datetime | None = None) -> StateResult:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        self._require_enabled_scope(character_id)
        with self.database.atomic() as db:
            row = db.execute("SELECT * FROM autonomous_contact_candidates WHERE id=? AND character_id=?", (candidate_id, character_id)).fetchone()
            if not row:
                return StateResult("missing", candidate_id, reason_code="candidate_missing")
            if row["status"] in {"completed", "expired", "cancelled", "failed", "outcome_unknown"}:
                return StateResult("not_cancellable", candidate_id, reason_code=row["status"])
            reservation = db.execute("SELECT * FROM autonomous_contact_reservations WHERE candidate_id=? AND character_id=? ORDER BY attempt_no DESC LIMIT 1", (candidate_id, character_id)).fetchone()
            if reservation:
                request_status = reservation["request_status"]
                if request_status == "reserved":
                    request_status = "released"
                elif request_status == "started":
                    request_status = "outcome_unknown"
                db.execute("UPDATE autonomous_contact_reservations SET request_status=?,message_status='released',updated_at=? WHERE id=?",
                           (request_status, _iso(current), reservation["id"]))
            db.execute("UPDATE autonomous_contact_candidates SET status='cancelled',reason_code=?,lease_token=NULL,lease_expires_at=NULL WHERE id=?",
                       (reason[:80], candidate_id))
            return StateResult("cancelled", candidate_id, reason_code=reason[:80])

    def expire_due_candidates(self, character_id: str, *, now: datetime | None = None, limit: int = 100) -> tuple[StateResult, ...]:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        self._require_enabled_scope(character_id)
        at = _iso(current)
        results = []
        with self.database.atomic() as db:
            rows = db.execute("SELECT * FROM autonomous_contact_candidates WHERE character_id=? AND status IN ('pending','deferred') AND expires_at<=? ORDER BY expires_at,id LIMIT ?",
                              (character_id, at, max(1, min(limit, 100)))).fetchall()
            for row in rows:
                db.execute("UPDATE autonomous_contact_drafts SET review_status='expired' WHERE candidate_id=? AND character_id=? AND review_status='mock_approved'", (row["id"], character_id))
                results.append(self._defer_or_end(db, row, "candidate_expired", None, "expired", current))
        return tuple(results)

    def recover_expired_claims(self, character_id: str, *, now: datetime | None = None, limit: int = 100) -> tuple[StateResult, ...]:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        self._require_enabled_scope(character_id)
        at = _iso(current)
        results = []
        with self.database.atomic() as db:
            rows = db.execute("SELECT * FROM autonomous_contact_candidates WHERE character_id=? AND status='claimed' AND lease_expires_at<=? ORDER BY lease_expires_at,id LIMIT ?", (character_id, at, max(1, min(limit, 100)))).fetchall()
            for row in rows:
                reservation = db.execute("SELECT * FROM autonomous_contact_reservations WHERE candidate_id=? AND character_id=? ORDER BY attempt_no DESC LIMIT 1", (row["id"], character_id)).fetchone()
                if not reservation:
                    results.append(self._defer_or_end(db, row, "reservation_missing", None, "failed", current))
                elif reservation["request_status"] == "reserved":
                    db.execute("UPDATE autonomous_contact_reservations SET request_status='released',message_status='released',updated_at=? WHERE id=?", (at, reservation["id"]))
                    if row["expires_at"] <= at:
                        results.append(self._defer_or_end(db, row, "candidate_expired", None, "expired", current))
                    else:
                        db.execute("UPDATE autonomous_contact_candidates SET status='pending',reason_code='lease_recovered_before_request',lease_token=NULL,lease_expires_at=NULL WHERE id=?", (row["id"],))
                        results.append(StateResult("pending", row["id"], reason_code="lease_recovered_before_request"))
                elif reservation["request_status"] == "started":
                    db.execute("UPDATE autonomous_contact_reservations SET request_status='outcome_unknown',message_status='released',updated_at=? WHERE id=?", (at, reservation["id"]))
                    results.append(self._defer_or_end(db, row, "request_outcome_unknown_budget_consumed", None, "outcome_unknown", current))
                elif reservation["request_status"] == "settled":
                    db.execute("UPDATE autonomous_contact_reservations SET message_status='released',updated_at=? WHERE id=?", (at, reservation["id"]))
                    db.execute("UPDATE autonomous_contact_drafts SET review_status='cancelled' WHERE candidate_id=? AND character_id=? AND review_status='mock_approved'", (row["id"], character_id))
                    results.append(self._defer_or_end(db, row, "settled_draft_not_persisted", None, "failed", current))
                else:
                    results.append(self._defer_or_end(db, row, "reservation_state_inconsistent", None, "failed", current))
        return tuple(results)
