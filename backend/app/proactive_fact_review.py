"""Durable fact review receipts. This service never generates or sends messages."""

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
import json
import sqlite3
import re
from typing import Protocol
from uuid import uuid4

from .autonomous_intent_state import AutonomousIntentStateRepository, _iso, _time
from .db import Database
from .ownership import current_character


POLICY_VERSION = "soulseed-fact-review-v1"
REVIEW_TTL = timedelta(minutes=5)
_RESEARCH_TEXT = "这次资料查阅完成了。我还想再看看有没有更多线索。"
_ACTIVE_CANDIDATE_STATES = {"pending", "claimed", "deferred"}


class ReviewStatus(StrEnum):
    APPROVED = "APPROVED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    REJECTED = "REJECTED"


class ReviewError(ValueError):
    pass


@dataclass(frozen=True)
class ReviewDraft:
    character_id: str
    candidate_id: str
    draft_id: str
    text: str = field(repr=False)


@dataclass(frozen=True)
class EvidenceVersion:
    kind: str
    reference_id: str
    content_sha256: str


@dataclass(frozen=True)
class FactReviewResult:
    review_id: str
    character_id: str
    candidate_id: str
    draft_id: str
    content_sha256: str
    evidence: tuple[EvidenceVersion, ...]
    status: ReviewStatus
    method: str
    reason_code: str
    policy_version: str
    reviewed_at: str
    expires_at: str
    reviewer_id: str | None

    @property
    def can_auto_handoff(self) -> bool:
        """Recorded eligibility only. A live transaction must still verify it."""
        return self.status is ReviewStatus.APPROVED and self.method in {"controlled_render", "human"}


class HumanReviewAuthorizer(Protocol):
    def authorized_reviewer(self, operation_context: object, character_id: str) -> str | None:
        """Resolve a currently authorized operator from trusted server context."""
        ...


def render_research_completion(character_id: str, candidate_id: str, draft_id: str) -> ReviewDraft:
    """Render only the local completion record, without quoting any claim."""
    return ReviewDraft(character_id, candidate_id, draft_id, _RESEARCH_TEXT)


def classify_review_text(text: str, *, evidence_available: bool, controlled: bool = False,
                         expected_text: str | None = None) -> tuple[ReviewStatus, str]:
    """Shared text policy. The host establishes evidence and rendering provenance."""
    if not evidence_available:
        return ReviewStatus.REJECTED, "evidence_unavailable"
    if not text.strip() or len(text) > 300 or any(ord(ch) < 32 for ch in text):
        return ReviewStatus.REJECTED, "invalid_draft_text"
    if not controlled:
        return ReviewStatus.REVIEW_REQUIRED, "free_text_requires_human"
    if text == expected_text:
        return ReviewStatus.APPROVED, "stored_research_completion_rendered"
    return ReviewStatus.REJECTED, "outside_controlled_render"


_EVIDENCE_QUERIES = {
    "life_state": "SELECT * FROM life_states WHERE character_id=? AND character_id=?",
    "message": """SELECT m.id,m.role,m.content,m.created_at,m.conversation_id,c.character_id
                   FROM messages m JOIN conversations c ON c.id=m.conversation_id
                   WHERE m.id=? AND c.character_id=?""",
    "share": "SELECT * FROM life_share_intents WHERE id=? AND character_id=?",
    "life_event": "SELECT * FROM life_events WHERE id=? AND character_id=?",
    "exploration": "SELECT * FROM exploration_runs WHERE id=? AND character_id=?",
    "source": "SELECT * FROM exploration_evidence WHERE id=? AND character_id=?",
}


def _evidence_versions(sql, candidate, now):
    """Hash current stored evidence, not URLs or a model's cited claim list."""
    expires = _time(candidate["expires_at"])
    if candidate["status"] not in _ACTIVE_CANDIDATE_STATES or expires is None or expires <= now:
        return ()
    try:
        refs = json.loads(candidate["evidence_json"])
        if not isinstance(refs, list) or not 1 <= len(refs) <= 8:
            return ()
        keys = [(ref["kind"], ref["id"]) for ref in refs]
        if len(set(keys)) != len(keys) or any(kind not in _EVIDENCE_QUERIES for kind, _ in keys):
            return ()
        kinds = [kind for kind, _ in keys]
        if candidate["candidate_type"] == "real_discovery":
            if (set(kinds) != {"share", "life_event", "exploration", "source"}
                    or any(kinds.count(kind) != 1 for kind in ("share", "life_event", "exploration"))):
                return ()
        elif candidate["candidate_type"] in ("smalltalk", "conversation_followup"):
            if kinds != ["message"]:
                return ()
        elif candidate["candidate_type"] == "self_social":
            if kinds != ["life_state"]:
                return ()
        elif candidate["candidate_type"] != "shared_topic" or kinds != ["message", "message"]:
            return ()
        character_id = candidate["character_id"]
        if not AutonomousIntentStateRepository._evidence_valid(sql, character_id, candidate["candidate_type"], refs, now):
            return ()
        versions = []
        for kind, ref_id in keys:
            row = sql.execute(_EVIDENCE_QUERIES[kind], (character_id if kind == 'life_state' else ref_id, character_id)).fetchone()
            if row is None:
                return ()
            related = None
            if kind == 'life_state':
                related = {
                    'preferences': [dict(item) for item in sql.execute('SELECT * FROM life_preferences WHERE character_id=? ORDER BY event_type', (character_id,))],
                    'activity': [dict(item) for item in sql.execute('SELECT * FROM life_events WHERE character_id=? AND id=?', (character_id, row['current_activity_event_id']))],
                }
            payload = json.dumps([character_id, kind, dict(row)] + ([related] if related is not None else []), ensure_ascii=False, sort_keys=True)
            versions.append(EvidenceVersion(kind, ref_id, sha256(payload.encode("utf-8")).hexdigest()))
        return tuple(versions)
    except (KeyError, ValueError, TypeError):
        return ()


def _encode_evidence(versions):
    return json.dumps([asdict(item) for item in versions], separators=(",", ":"), ensure_ascii=False)


def _content_digest(draft):
    return sha256(draft.text.encode("utf-8")).hexdigest()


def _result(row):
    return FactReviewResult(
        row["id"], row["character_id"], row["candidate_id"], row["draft_id"], row["content_sha256"],
        tuple(EvidenceVersion(**item) for item in json.loads(row["evidence_json"])),
        ReviewStatus(row["status"]), row["method"], row["reason_code"], row["policy_version"],
        row["reviewed_at"], row["expires_at"], row["reviewer_id"],
    )


class FactReviewService:
    def __init__(self, database: Database, *, human_authorizer: HumanReviewAuthorizer | None = None,
                 clock=lambda: datetime.now(UTC)):
        self.database = database
        self._human_authorizer = human_authorizer
        self._clock = clock

    def _now(self):
        now = self._clock()
        if now.utcoffset() is None:
            raise ReviewError("review_clock_requires_timezone")
        return now.astimezone(UTC)

    @staticmethod
    def _check_draft(draft):
        if type(draft) is not ReviewDraft or not isinstance(draft.text, str) or any(
            not isinstance(value, str) or not value.strip() or len(value) > 128
            for value in (draft.character_id, draft.candidate_id, draft.draft_id)
        ):
            raise ReviewError("invalid_review_draft")
        if draft.character_id != current_character.get():
            raise ReviewError("review_character_scope_mismatch")

    @staticmethod
    def _candidate(sql, draft):
        return sql.execute("""SELECT c.* FROM autonomous_contact_candidates c
            JOIN characters ch ON ch.id=c.character_id AND ch.ownership_status='owned'
            WHERE c.id=? AND c.character_id=?""", (draft.candidate_id, draft.character_id)).fetchone()

    @staticmethod
    def _load(sql, draft):
        return sql.execute("SELECT * FROM proactive_fact_reviews WHERE draft_id=? AND character_id=?",
                           (draft.draft_id, draft.character_id)).fetchone()

    @staticmethod
    def _bound(row, draft, versions):
        return (row["candidate_id"] == draft.candidate_id and row["content_sha256"] == _content_digest(draft)
                and row["evidence_json"] == _encode_evidence(versions) and row["policy_version"] == POLICY_VERSION)

    @staticmethod
    def _current(row, now):
        reviewed, expires = _time(row["reviewed_at"]), _time(row["expires_at"])
        return bool(reviewed and expires and reviewed <= now < expires <= reviewed + REVIEW_TTL)

    def review_controlled(self, draft: ReviewDraft) -> FactReviewResult:
        return self._issue(draft, method="controlled_render")

    def review_free_text(self, draft: ReviewDraft) -> FactReviewResult:
        return self._issue(draft, method="free_text")

    def review_generated(self, draft, option, evidence_ids, claims, *, secret=None):
        """Allow social expression; verify concrete assertions against stored evidence."""
        self._check_draft(draft)
        with self.database.atomic() as db:
            now = self._now()
            candidate = self._candidate(db, draft)
            versions = _evidence_versions(db, candidate, now) if candidate else ()
            allowed = {v.reference_id for v in versions}
            valid_refs = (isinstance(evidence_ids, list) and all(isinstance(x, str) for x in evidence_ids)
                          and len(set(evidence_ids)) == len(evidence_ids) and set(evidence_ids) <= allowed)
            valid_claims = (isinstance(claims, list) and all(
                isinstance(c, dict) and set(c) == {"text", "evidence_ids"}
                and isinstance(c["text"], str) and isinstance(c["evidence_ids"], list)
                and all(isinstance(i, str) for i in c["evidence_ids"])
                and bool(c["evidence_ids"]) and set(c["evidence_ids"]) <= set(evidence_ids)
                for c in claims))
            status, reason = "REVIEW_REQUIRED", "unverified_clause"
            if not versions or not valid_refs or not valid_claims:
                status, reason = "REJECTED", "invalid_evidence_references"
            elif not draft.text.strip() or len(draft.text) > 300 or re.search(r"[\x00-\x1f<>]|https?://|www\.", draft.text, re.I):
                status, reason = "REJECTED", "invalid_or_external_reference"
            else:
                # These patterns identify assertions needing provenance, rather than
                # enumerating permitted social sentences. Questions and feelings
                # remain natural; a pattern cannot prove arbitrary language true.
                assertion = re.compile(
                    r"(?:你(?:昨天|前天|上次|之前|曾经|已经|答应|告诉|说过|喜欢吃|喜欢听|住在|工作在)|"
                    r"我(?:昨天|前天|上周|上个月|上次|之前|曾经|亲自|刚刚)(?:[^，。！？?]{0,20})(?:去|看|听|读|遇|做|发现|查|买|说|爬)|"
                    r"(?:资料|网页|研究|新闻|报道|调查)(?:显示|表明|发现|说|提到)|"
                    r"(?:[\u4e00-\u9fff]{2,16})(?:会飞|生活在|住在|使用))"
                )
                fact = option.text.rstrip("。！？!?，,；;：: ")
                clauses = [part.strip(" \"'“”‘’「」") for part in re.split(r"[，,。！？!?；;：:\n]+", draft.text) if part.strip()]
                verified_option = False
                if candidate["candidate_type"] == "real_discovery":
                    run = next((v.reference_id for v in versions if v.kind == "exploration"), None)
                    row = db.execute("SELECT claims_json FROM exploration_runs WHERE id=? AND character_id=?", (run, draft.character_id)).fetchone()
                    saved = json.loads(row["claims_json"]) if row else []
                    verified_option = any(isinstance(c, dict) and c.get("kind") == "sourced"
                        and c.get("text", "").strip() == option.text and c.get("source_ids") == list(option.source_ids)
                        and set(option.source_ids) <= {v.reference_id for v in versions if v.kind == "source"}
                        for c in saved)
                    if verified_option:
                        source_rows = db.execute(
                            "SELECT excerpt FROM exploration_evidence WHERE run_id=? AND character_id=? AND id IN (%s)"
                            % ",".join("?" for _ in option.source_ids),
                            (run, draft.character_id, *option.source_ids),
                        ).fetchall()
                        verified_option = any(fact == (row["excerpt"] or "").strip().rstrip("。！？!?，,；;：: ") for row in source_rows)
                used_claims = [c for c in claims if c["text"].strip().rstrip("。！？!?，,；;：: ") in draft.text]
                unverified_claim = any(not (verified_option and c["text"].strip().rstrip("。！？!?，,；;：: ") == fact
                    and set(option.source_ids) <= set(c["evidence_ids"])) for c in used_claims)
                unverified_assertion = any(assertion.search(clause) and not (verified_option and clause == fact) for clause in clauses)
                if secret and len(secret) >= 4 and secret in draft.text:
                    status, reason = "REJECTED", "credential_in_message"
                elif not clauses:
                    status, reason = "REJECTED", "empty_message"
                elif unverified_claim or unverified_assertion:
                    status, reason = "REVIEW_REQUIRED", "unverified_fact"
                else:
                    status, reason = "APPROVED", "social_or_verified_fact"
            expires = min(now + REVIEW_TTL, _time(candidate["expires_at"])) if versions else now + REVIEW_TTL
            db.execute("""INSERT INTO desktop_generated_reviews VALUES(?,?,?,?,?,?,?,?)""",
                       (draft.draft_id, draft.character_id, draft.candidate_id, _content_digest(draft),
                        _encode_evidence(versions), status, reason, _iso(expires)))
            return status, reason

    def verify_generated_in_transaction(self, db, draft):
        self._check_draft(draft)
        if not self.database.in_transaction:
            raise ReviewError("review_verification_requires_atomic")
        row = db.execute("SELECT * FROM desktop_generated_reviews WHERE draft_id=? AND character_id=?",
                         (draft.draft_id, draft.character_id)).fetchone()
        candidate = self._candidate(db, draft)
        now = self._now()
        return bool(row and row["status"] == "APPROVED" and candidate and row["candidate_id"] == draft.candidate_id
                    and row["content_sha256"] == _content_digest(draft) and _time(row["expires_at"]) > now
                    and row["evidence_json"] == _encode_evidence(_evidence_versions(db, candidate, now)))

    def approve_evidence_option(self, sql, draft: ReviewDraft, option, style: str, persona) -> bool:
        """Record approval for an exact server rendering of current saved evidence."""
        from .proactive_expression import render_evidence_option

        self._check_draft(draft)
        if not self.database.in_transaction:
            raise ReviewError("review_verification_requires_atomic")
        with self.database.connection() as active:
            if sql is not active:
                raise ReviewError("review_database_mismatch")
        now = self._now()
        candidate = self._candidate(sql, draft)
        versions = _evidence_versions(sql, candidate, now) if candidate else ()
        if not versions or style not in persona.allowed_styles or persona.character_id != draft.character_id:
            return False
        kind = candidate["candidate_type"]
        if kind == "real_discovery":
            run = next(ref.reference_id for ref in versions if ref.kind == "exploration")
            row = sql.execute("SELECT claims_json FROM exploration_runs WHERE id=?", (run,)).fetchone()
            claims = json.loads(row["claims_json"])
            matches = any(
                option.key == f"{run}:{index}" and option.kind == "sourced_claim"
                and claim.get("kind") == "sourced" and isinstance(claim.get("text"), str)
                and claim["text"].strip() == option.text and claim.get("source_ids") == list(option.source_ids)
                and set(option.source_ids) <= {ref.reference_id for ref in versions if ref.kind == "source"}
                for index, claim in enumerate(claims[:8]) if isinstance(claim, dict)
            )
        else:
            matches = (option.kind == "shared_topic" and
                       option.text == AutonomousIntentStateRepository._topic_for_row(candidate, sql))
        expected = render_evidence_option(style, kind, option)
        status, _ = classify_review_text(draft.text, evidence_available=matches, controlled=True, expected_text=expected)
        if status != ReviewStatus.APPROVED:
            return False
        sql.execute("""INSERT INTO proactive_fact_reviews
            (id,character_id,candidate_id,draft_id,content_sha256,evidence_json,
             policy_version,status,method,reason_code,reviewed_at,expires_at)
            VALUES(?,?,?,?,?,?,?,'APPROVED','controlled_render','current_evidence_option_rendered',?,?)""",
                    (uuid4().hex, draft.character_id, draft.candidate_id, draft.draft_id, _content_digest(draft),
                     _encode_evidence(versions), POLICY_VERSION, _iso(now),
                     _iso(min(now + REVIEW_TTL, _time(candidate["expires_at"])))))
        return True

    def _issue(self, draft, *, method):
        self._check_draft(draft)
        if self.database.in_transaction:
            raise ReviewError("review_issuance_requires_standalone_atomic")
        with self.database.atomic() as sql:
            now = self._now()
            candidate = self._candidate(sql, draft)
            if candidate is None:
                raise ReviewError("review_candidate_not_owned_or_missing")
            versions = _evidence_versions(sql, candidate, now)
            existing = self._load(sql, draft)
            if existing:
                if not self._bound(existing, draft, versions):
                    raise ReviewError("review_binding_changed")
                return _result(existing)
            status, reason = classify_review_text(
                draft.text, evidence_available=bool(versions), controlled=method == "controlled_render",
                expected_text=_RESEARCH_TEXT if candidate["candidate_type"] == "real_discovery" else None,
            )
            expires = min(now + REVIEW_TTL, _time(candidate["expires_at"])) if versions else now + REVIEW_TTL
            try:
                sql.execute("""INSERT INTO proactive_fact_reviews
                    (id,character_id,candidate_id,draft_id,content_sha256,evidence_json,
                     policy_version,status,method,reason_code,reviewed_at,expires_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (uuid4().hex, draft.character_id, draft.candidate_id, draft.draft_id, _content_digest(draft),
                             _encode_evidence(versions), POLICY_VERSION, status.value, method, reason, _iso(now), _iso(expires)))
            except sqlite3.IntegrityError as exc:
                raise ReviewError("review_draft_conflict") from exc
            return _result(self._load(sql, draft))

    def decide_human(self, review_id: str, draft: ReviewDraft, *, operation_context: object,
                     approve: bool) -> FactReviewResult:
        self._check_draft(draft)
        if type(approve) is not bool or not isinstance(review_id, str) or not review_id:
            raise ReviewError("invalid_human_review_decision")
        if self.database.environment != "testing" or self._human_authorizer is None:
            raise ReviewError("human_review_unavailable")
        if self.database.in_transaction:
            raise ReviewError("review_issuance_requires_standalone_atomic")
        with self.database.atomic() as sql:
            reviewer = self._human_authorizer.authorized_reviewer(operation_context, draft.character_id)
            if not isinstance(reviewer, str) or not reviewer.strip() or len(reviewer) > 128:
                raise ReviewError("human_reviewer_unauthorized")
            now = self._now()
            row = self._load(sql, draft)
            if row is None or row["id"] != review_id:
                raise ReviewError("review_not_found")
            candidate = self._candidate(sql, draft)
            versions = _evidence_versions(sql, candidate, now) if candidate is not None else ()
            if not versions or not self._current(row, now) or not self._bound(row, draft, versions):
                raise ReviewError("review_stale_or_binding_changed")
            status = ReviewStatus.APPROVED if approve else ReviewStatus.REJECTED
            if row["status"] != ReviewStatus.REVIEW_REQUIRED:
                if row["method"] == "human" and row["reviewer_id"] == reviewer and row["status"] == status:
                    return _result(row)
                raise ReviewError("review_already_decided")
            reason = "human_confirmed_exact_draft" if approve else "human_declined_exact_draft"
            sql.execute("""UPDATE proactive_fact_reviews SET status=?,method='human',reason_code=?,reviewer_id=?,reviewed_at=?
                           WHERE id=?""", (status.value, reason, reviewer, _iso(now), review_id))
            return _result(self._load(sql, draft))

    def verify_approval_in_transaction(self, sql, draft: ReviewDraft) -> bool:
        """Validate stored approval against the draft and live evidence under the handoff lock."""
        self._check_draft(draft)
        if not self.database.in_transaction:
            raise ReviewError("review_verification_requires_atomic")
        with self.database.connection() as active:
            if sql is not active:
                raise ReviewError("review_database_mismatch")
        now = self._now()
        row = self._load(sql, draft)
        if row is None or row["status"] != ReviewStatus.APPROVED or not self._current(row, now):
            return False
        candidate = self._candidate(sql, draft)
        versions = _evidence_versions(sql, candidate, now) if candidate is not None else ()
        if not versions or not self._bound(row, draft, versions):
            return False
        if row["method"] == "controlled_render":
            return candidate["candidate_type"] == "real_discovery" and draft.text == _RESEARCH_TEXT
        return self.database.environment == "testing" and row["method"] == "human" and bool(row["reviewer_id"])
