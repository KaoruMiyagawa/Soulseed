"""Read-only background evaluation for autonomous contact decisions.

This module deliberately stops before proactive expression and ContactService.
It records only bounded, non-content diagnostic summaries in process memory.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import RLock

from .autonomous_intent import (DecisionKind, _INVITATION, _NEGATIVE, _STOP, _time, _topic_key,
                                build_snapshot, dry_run, generate_candidates)
from .db import Database
from .ownership import character_scope

logger = logging.getLogger(__name__)
_CONTACT_REFUSAL = re.compile(r"别主动联系|不要主动联系|不要联系|别联系|别打扰|不要打扰|不要再打扰|别再打扰|不要再找我|别主动找我|先别找我|暂时别找我|先别联系|暂时别联系|先别打扰|暂时别打扰")


def _suppression_reasons(snapshot, candidates) -> tuple[str, ...]:
    """Explain common evidence-level exclusions without exposing message text."""
    if candidates:
        return ()
    reasons: set[str] = set()
    now = snapshot.observed_at
    for discovery in snapshot.discoveries:
        created = _time(discovery.share.observed_at)
        if created and now >= created + timedelta(hours=24):
            reasons.add("candidate_expired")
    handled_topics = {item.normalized_topic for item in snapshot.handled_topics}
    for item in snapshot.interactions:
        created = _time(item.evidence.observed_at)
        if item.role != "user" or not created:
            continue
        match = _INVITATION.search(item.content)
        if not match:
            continue
        topic = _topic_key(match.group(1))
        if item.handled or topic in handled_topics:
            reasons.add("candidate_duplicate")
        elif now - created >= timedelta(hours=72):
            reasons.add("candidate_expired")
        elif (item.has_later_user or item.truncated or _NEGATIVE.search(item.content)
              or _CONTACT_REFUSAL.search(item.content) or any(stop in item.content for stop in _STOP)):
            reasons.add("candidate_unsuitable")
    return tuple(sorted(reasons))


@dataclass(frozen=True)
class ShadowDiagnostic:
    character_id: str
    checked_at: str
    decision: str
    candidate_type: str | None
    evidence_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]
    dedup_key: str | None
    next_evaluation_at: str | None
    failure_code: str | None = None


class ShadowDiagnostics:
    """Bounded process-local latest result per character; never persists content."""

    def __init__(self, max_characters: int = 100):
        if max_characters < 1:
            raise ValueError("max_characters must be positive")
        self.max_characters = max_characters
        self._items: OrderedDict[str, ShadowDiagnostic] = OrderedDict()
        self._lock = RLock()

    def record(self, item: ShadowDiagnostic) -> None:
        with self._lock:
            self._items.pop(item.character_id, None)
            self._items[item.character_id] = item
            while len(self._items) > self.max_characters:
                self._items.popitem(last=False)

    def latest(self, character_id: str | None = None) -> tuple[ShadowDiagnostic, ...]:
        with self._lock:
            if character_id is not None:
                item = self._items.get(character_id)
                return (item,) if item else ()
            return tuple(reversed(self._items.values()))


class ShadowModeScheduler:
    """Run the deterministic B kernel at a low frequency without side effects."""

    def __init__(self, database: Database, *, enabled: bool = False,
                 interval: timedelta = timedelta(hours=1), contact_enabled: bool = True,
                 diagnostics: ShadowDiagnostics | None = None):
        if interval < timedelta(minutes=5):
            raise ValueError("自主意图 shadow 检查间隔不能短于 5 分钟")
        self.database = database
        self.enabled = enabled
        self.interval = interval
        self.contact_enabled = contact_enabled
        self.diagnostics = diagnostics or ShadowDiagnostics()
        self._tick_lock = asyncio.Lock()

    def _owned_characters(self) -> tuple[str, ...]:
        # No ensure/init calls: schema must already exist and this query is read-only.
        with self.database.connection() as db:
            old = db.execute("PRAGMA query_only").fetchone()[0]
            db.execute("PRAGMA query_only=ON")
            try:
                db.execute("BEGIN")
                return tuple(row[0] for row in db.execute(
                    "SELECT id FROM characters WHERE ownership_status='owned' ORDER BY id LIMIT 100"
                ))
            finally:
                db.rollback()
                db.execute(f"PRAGMA query_only={int(old)}")

    def evaluate_character(self, character_id: str, *, now: datetime | None = None) -> ShadowDiagnostic:
        checked = now or datetime.now(UTC)
        if checked.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        checked = checked.astimezone(UTC)
        try:
            with character_scope(character_id):
                snapshot = build_snapshot(self.database, character_id, now=checked)
                candidates = generate_candidates(snapshot)
                decision = dry_run(snapshot)
                suppressed = _suppression_reasons(snapshot, candidates)
                if not self.contact_enabled:
                    decision_kind = DecisionKind.SILENT
                    reason_codes = ("global_contact_disabled",)
                    candidate = next((x for x in candidates if x.dedup_key == decision.dedup_key), None)
                    candidate_type = candidate.kind if candidate else None
                    evidence_ids = tuple(ref.id for ref in candidate.evidence[:8]) if candidate else ()
                    dedup_key = candidate.dedup_key if candidate else None
                    retry_at = None
                else:
                    decision_kind = decision.result
                    reason_codes = tuple(dict.fromkeys((*decision.reasons, *suppressed)))[:8]
                    candidate = next((x for x in candidates if x.dedup_key == decision.dedup_key), None)
                    candidate_type = decision.candidate_type
                    # Evidence IDs only; never serialize evidence content or source text.
                    evidence_ids = tuple(item[:120] for item in decision.evidence_ids[:8])
                    dedup_key = decision.dedup_key
                    retry_at = decision.retry_at
                return ShadowDiagnostic(
                    character_id=character_id,
                    checked_at=checked.isoformat(),
                    decision=decision_kind.value,
                    candidate_type=candidate_type,
                    evidence_ids=evidence_ids,
                    reason_codes=reason_codes,
                    dedup_key=dedup_key,
                    next_evaluation_at=(retry_at.isoformat() if retry_at else None),
                )
        except Exception as exc:
            logger.warning("autonomous_intent_shadow_failed character_id=%s error_type=%s",
                           character_id, type(exc).__name__)
            return ShadowDiagnostic(
                character_id=character_id,
                checked_at=checked.isoformat(),
                decision=DecisionKind.SILENT.value,
                candidate_type=None,
                evidence_ids=(),
                reason_codes=("shadow_evaluation_failed",),
                dedup_key=None,
                next_evaluation_at=None,
                failure_code=type(exc).__name__,
            )

    async def tick(self, *, now: datetime | None = None) -> tuple[ShadowDiagnostic, ...]:
        if not self.enabled:
            return ()
        async with self._tick_lock:
            # SQLite snapshot reads are synchronous. Move the bounded read cycle
            # off the event loop; per-connection lock waits are capped by the
            # Database's 10-second SQLite timeout and the role scan is capped at 100.
            return await asyncio.to_thread(self._tick_sync, now)

    def _tick_sync(self, now: datetime | None) -> tuple[ShadowDiagnostic, ...]:
        try:
            character_ids = self._owned_characters()
        except Exception as exc:
            logger.warning("autonomous_intent_shadow_character_scan_failed error_type=%s", type(exc).__name__)
            return ()
        outcomes = []
        for character_id in character_ids:
            try:
                outcome = self.evaluate_character(character_id, now=now)
            except Exception as exc:
                # Keep role failures local even if a future evaluator adapter
                # regresses its own catch boundary.
                logger.warning("autonomous_intent_shadow_failed character_id=%s error_type=%s",
                               character_id, type(exc).__name__)
                checked = now or datetime.now(UTC)
                if checked.tzinfo is None:
                    checked = checked.replace(tzinfo=UTC)
                outcome = ShadowDiagnostic(
                    character_id=character_id,
                    checked_at=checked.astimezone(UTC).isoformat(),
                    decision=DecisionKind.SILENT.value,
                    candidate_type=None,
                    evidence_ids=(),
                    reason_codes=("shadow_evaluation_failed",),
                    dedup_key=None,
                    next_evaluation_at=None,
                    failure_code=type(exc).__name__,
                )
            self.diagnostics.record(outcome)
            outcomes.append(outcome)
        return tuple(outcomes)

    async def run_forever(self, stop: asyncio.Event) -> None:
        if not self.enabled:
            return
        while not stop.is_set():
            await self.tick()
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.interval.total_seconds())
            except TimeoutError:
                pass
