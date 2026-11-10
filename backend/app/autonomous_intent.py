"""Deterministic, read-only contact planning. Never schedules or sends messages."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .db import Database
from .ownership import current_character


class DecisionKind(StrEnum):
    CONTACT = "CONTACT"
    DEFER = "DEFER"
    SILENT = "SILENT"


@dataclass(frozen=True)
class EvidenceRef:
    kind: str
    id: str
    character_id: str
    observed_at: str
    status: str
    source: str


@dataclass(frozen=True)
class Interaction:
    evidence: EvidenceRef
    conversation_id: str
    role: str
    content: str = field(repr=False)
    handled: bool = False
    has_later_user: bool = False
    truncated: bool = False
    is_proactive: bool = False


@dataclass(frozen=True)
class Discovery:
    share: EvidenceRef
    event: EvidenceRef
    run: EvidenceRef
    sources: tuple[EvidenceRef, ...]


@dataclass(frozen=True)
class LifeObservation:
    evidence: EvidenceRef
    content: str = field(repr=False)
    trust: str = 'saved_not_independently_verified'


@dataclass(frozen=True)
class PreferenceObservation:
    evidence: EvidenceRef
    weight: float


@dataclass(frozen=True)
class MemoryObservation:
    evidence: EvidenceRef
    source_message: EvidenceRef
    memory_type: str
    confidence: float
    content: str = field(repr=False)


@dataclass(frozen=True)
class HandledTopic:
    message: EvidenceRef
    intent: EvidenceRef
    normalized_topic: str = field(repr=False)


@dataclass(frozen=True)
class ContactState:
    allowed: bool | None
    quiet_start_minute: int | None
    quiet_end_minute: int | None
    busy_until: str | None
    pause_until: str | None
    latest_created_at: str | None
    latest_delivered_at: str | None
    ready_exists: bool
    last_message_id: str | None
    last_status: str | None
    preference_updated_at: str | None


@dataclass(frozen=True)
class CharacterSnapshot:
    character_id: str
    observed_at: datetime
    timezone_id: str | None
    contact: ContactState
    discoveries: tuple[Discovery, ...]
    life_events: tuple[LifeObservation, ...]
    current_activity: LifeObservation | None
    interactions: tuple[Interaction, ...]
    latest_user: Interaction | None
    preferences: tuple[PreferenceObservation, ...]
    memories: tuple[MemoryObservation, ...]
    handled_topics: tuple[HandledTopic, ...]
    handled_topics_complete: bool


@dataclass(frozen=True)
class Candidate:
    kind: str
    character_id: str
    evidence: tuple[EvidenceRef, ...]
    reason: str
    relevance: str
    dedup_key: str
    expires_at: datetime


@dataclass(frozen=True)
class Decision:
    result: DecisionKind
    candidate_type: str | None
    evidence_ids: tuple[str, ...]
    reasons: tuple[str, ...]
    basic_contact_allowed: bool
    dedup_key: str | None = None
    retry_at: datetime | None = None


def _time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(UTC) if parsed.tzinfo else None
    except (TypeError, ValueError, OverflowError):
        return None


def _valid_url(value: str | None) -> bool:
    try:
        parsed = urlsplit(value or "")
        host = (parsed.hostname or '').rstrip('.').casefold()
        if parsed.scheme not in {'http', 'https'} or not host or parsed.username or parsed.password:
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return '.' in host and not host.endswith(('.local', '.localhost', '.internal'))
    except ValueError:
        return False


def _ref(kind: str, identifier: str, character_id: str, when: str, status: str, source: str) -> EvidenceRef:
    return EvidenceRef(kind, identifier, character_id, when, status, source)


def build_snapshot(database: Database, character_id: str, *, now: datetime | None = None) -> CharacterSnapshot:
    """Read a bounded, consistent snapshot without repository initialization or writes.

    The caller must already be in the server-selected character scope. An absent
    preference or timezone is unknown, not implicit permission to contact.
    """
    if not character_id or current_character.get() != character_id:
        raise ValueError("character scope mismatch")
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    current = current.astimezone(UTC)
    with database.connection() as db:
        if db.in_transaction:
            raise RuntimeError("snapshot requires a separate read-only connection")
        old_query_only = db.execute("PRAGMA query_only").fetchone()[0]
        db.execute("PRAGMA query_only=ON")
        try:
            db.execute("BEGIN")
            owner = db.execute("SELECT 1 FROM characters WHERE id=? AND ownership_status='owned'", (character_id,)).fetchone()
            if not owner:
                raise ValueError("character is not owned")
            state = db.execute("SELECT timezone_id,current_activity_event_id FROM life_states WHERE character_id=?", (character_id,)).fetchone()
            pref = db.execute("SELECT allowed,quiet_start_minute,quiet_end_minute,busy_until,pause_until,updated_at FROM contact_preferences WHERE character_id=?", (character_id,)).fetchone()
            last = db.execute("SELECT m.id,m.status,m.created_at,m.delivered_at FROM proactive_messages m JOIN contact_intents i ON i.id=m.intent_id WHERE i.character_id=? AND i.ownership_verified=1 ORDER BY m.created_at DESC,m.id DESC LIMIT 1", (character_id,)).fetchone()
            ready = db.execute("SELECT 1 FROM proactive_messages m JOIN contact_intents i ON i.id=m.intent_id WHERE i.character_id=? AND i.ownership_verified=1 AND i.status='ready' AND m.status='ready' LIMIT 1", (character_id,)).fetchone()
            contact = ContactState(bool(pref['allowed']) if pref else None, pref['quiet_start_minute'] if pref else None,
                                   pref['quiet_end_minute'] if pref else None, pref['busy_until'] if pref else None,
                                   pref['pause_until'] if pref else None, last['created_at'] if last else None,
                                   last['delivered_at'] if last else None, bool(ready), last['id'] if last else None,
                                   last['status'] if last else None, pref['updated_at'] if pref else None)
            rows = db.execute("""SELECT m.id,m.conversation_id,m.role,substr(m.content,1,501) AS content,m.created_at,
                EXISTS(SELECT 1 FROM contact_intents i WHERE i.character_id=c.character_id AND i.ownership_verified=1
                  AND i.source IN ('smalltalk','conversation_followup') AND i.source_id=m.id) AS handled,
                EXISTS(SELECT 1 FROM messages n WHERE n.conversation_id=m.conversation_id AND n.role='user'
                  AND (n.created_at>m.created_at OR (n.created_at=m.created_at AND n.id>m.id))) AS has_later_user,
                EXISTS(SELECT 1 FROM proactive_messages p WHERE p.chat_message_id=m.id) AS is_proactive
                FROM messages m JOIN conversations c ON c.id=m.conversation_id
                WHERE c.character_id=? AND m.role IN ('user','assistant')
                AND NOT EXISTS (SELECT 1 FROM proactive_messages p JOIN contact_intents i ON i.id=p.intent_id
                  WHERE p.chat_message_id=m.id AND (i.ownership_verified=0 OR i.character_id<>c.character_id))
                ORDER BY m.created_at DESC,m.id DESC LIMIT 30""", (character_id,)).fetchall()
            interactions = tuple(Interaction(_ref('message', r['id'], character_id, r['created_at'], 'saved', r['role']),
                                             r['conversation_id'], r['role'], r['content'][:500], bool(r['handled']), bool(r['has_later_user']), len(r['content']) > 500, bool(r['is_proactive'])) for r in rows)
            # Latest user is queried separately: bounded history must not hide the contact gate.
            user = db.execute("SELECT m.id,m.conversation_id,m.created_at,substr(m.content,1,501) AS content FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE c.character_id=? AND m.role='user' ORDER BY m.created_at DESC,m.id DESC LIMIT 1", (character_id,)).fetchone()
            latest_user = (Interaction(_ref('message', user['id'], character_id, user['created_at'], 'saved', 'user'), user['conversation_id'], 'user', user['content'][:500], truncated=len(user['content']) > 500) if user else None)
            prefs = tuple(PreferenceObservation(_ref('preference', r['event_type'], character_id, r['updated_at'], 'saved_weight', 'life_preferences'), r['weight']) for r in db.execute("SELECT event_type,weight,updated_at FROM life_preferences WHERE character_id=? ORDER BY updated_at DESC,event_type LIMIT 20", (character_id,)))
            memory_rows = db.execute("""SELECT m.id,m.updated_at,m.expires_at,m.memory_type,m.content,m.confidence,s.message_id,s.evidence_type,msg.role,msg.created_at AS message_at FROM memory_items m JOIN memory_sources s ON s.memory_id=m.id AND s.character_id=m.character_id JOIN messages msg ON msg.id=s.message_id JOIN conversations c ON c.id=msg.conversation_id AND c.character_id=m.character_id WHERE m.character_id=? AND m.status='active' ORDER BY m.updated_at DESC,m.id,s.created_at,s.id LIMIT 40""", (character_id,)).fetchall()
            memories, seen_memories = [], set()
            for r in memory_rows:
                if r['expires_at'] and (_time(r['expires_at']) is None or _time(r['expires_at']) <= current):
                    continue
                if r['id'] in seen_memories:
                    continue
                seen_memories.add(r['id'])
                memories.append(MemoryObservation(_ref('memory', r['id'], character_id, r['updated_at'], 'active', r['evidence_type']), _ref('message', r['message_id'], character_id, r['message_at'], 'saved', r['role']), r['memory_type'], r['confidence'], r['content'][:500]))
                if len(memories) == 20:
                    break
            event_rows = db.execute("""SELECT id,content,occurred_at,source,generation_method FROM life_events WHERE character_id=? AND status='completed' AND activity_status='completed' AND COALESCE(execution_status,'')<>'interrupted' AND generation_method='manual' ORDER BY occurred_at DESC,id LIMIT 20""", (character_id,)).fetchall()
            life_events = tuple(LifeObservation(_ref('life_event', r['id'], character_id, r['occurred_at'], 'completed', r['source'] + ':' + r['generation_method']), r['content'][:500]) for r in event_rows if _time(r['occurred_at']) and _time(r['occurred_at']) <= current)
            active = db.execute("SELECT id,content,started_at,source,generation_method FROM life_events WHERE id=? AND character_id=? AND activity_status='active' AND execution_status='running'", (state['current_activity_event_id'], character_id)).fetchone() if state and state['current_activity_event_id'] else None
            current_activity = (LifeObservation(_ref('life_event', active['id'], character_id, active['started_at'], 'active', active['source'] + ':' + active['generation_method']), active['content'][:500], 'active_not_completed') if active else None)
            handled_rows = db.execute("""SELECT i.id AS intent_id,i.status AS intent_status,i.created_at AS intent_at,m.id AS message_id,m.created_at AS message_at,substr(m.content,1,501) AS content FROM contact_intents i JOIN messages m ON m.id=i.source_id JOIN conversations c ON c.id=m.conversation_id AND c.character_id=i.character_id WHERE i.character_id=? AND i.ownership_verified=1 AND i.source IN ('smalltalk','conversation_followup') AND m.role='user' ORDER BY i.created_at DESC,i.id DESC LIMIT 101""", (character_id,)).fetchall()
            handled_topics = []
            for handled in handled_rows[:100]:
                match = _INVITATION.search(handled['content']) if len(handled['content']) <= 500 else None
                if match:
                    handled_topics.append(HandledTopic(_ref('message', handled['message_id'], character_id, handled['message_at'], 'saved', 'user'), _ref('contact_intent', handled['intent_id'], character_id, handled['intent_at'], handled['intent_status'], 'contact_service'), _topic_key(match.group(1))))
            rows = db.execute("""SELECT s.id AS share_id,s.created_at AS share_at,s.selection_token,s.selection_expires_at,
                e.id AS event_id,e.occurred_at,r.id AS run_id,r.ended_at,r.claims_json,r.summary
                FROM life_share_intents s JOIN life_events e ON e.id=s.event_id AND e.character_id=s.character_id
                JOIN exploration_runs r ON r.life_event_id=e.id AND r.character_id=e.character_id
                WHERE s.character_id=? AND s.status='pending' AND s.reason='evidence_backed_discovery'
                  AND (s.delivery_channel IS NULL OR s.delivery_channel='contact')
                  AND e.status='completed' AND e.activity_status='completed' AND COALESCE(e.execution_status,'')<>'interrupted' AND e.event_type='research'
                  AND e.source='web_research' AND e.generation_method='exploration_agent' AND r.status='completed'
                  AND NOT EXISTS(SELECT 1 FROM contact_intents i WHERE i.share_intent_id=s.id
                      AND i.character_id=s.character_id AND i.status IN ('generating','ready','delivered','dismissed'))
                ORDER BY s.created_at DESC,s.id LIMIT 20""", (character_id,)).fetchall()
            discoveries = []
            for row in rows:
                selection_end = _time(row['selection_expires_at'])
                if row['selection_token'] and (selection_end is None or selection_end > current):
                    continue
                try:
                    claims = json.loads(row['claims_json'] or '[]')
                except (ValueError, TypeError):
                    continue
                sources = db.execute("SELECT id,created_at,source_url,retrieved_at,excerpt FROM exploration_evidence WHERE run_id=? AND character_id=? AND tool_name='read_webpage' AND outcome='success' AND page_relevance='relevant' LIMIT 8", (row['run_id'], character_id)).fetchall()
                verified = {s['id']: s for s in sources if s['excerpt'] and _valid_url(s['source_url']) and _time(s['retrieved_at']) and _time(s['retrieved_at']) <= current}
                if not isinstance(claims, list):
                    continue
                cited, sourced_count = set(), 0
                for claim in claims:
                    if isinstance(claim, dict) and claim.get('kind') == 'sourced' and isinstance(claim.get('text'), str) and claim['text'].strip():
                        ids = claim.get('source_ids')
                        if isinstance(ids, list) and ids and all(isinstance(x, str) and x in verified for x in ids):
                            cited.update(ids)
                            sourced_count += 1
                event_at, ended_at = _time(row['occurred_at']), _time(row['ended_at'])
                if not cited or (sourced_count < 2 and len((row['summary'] or '').strip()) < 90) or not event_at or not ended_at or event_at > current or ended_at > current:
                    continue
                refs = tuple(_ref('source', sid, character_id, verified[sid]['retrieved_at'], 'relevant', 'read_webpage') for sid in sorted(cited))
                discoveries.append(Discovery(_ref('share', row['share_id'], character_id, row['share_at'], 'pending', 'life_share'),
                    _ref('life_event', row['event_id'], character_id, row['occurred_at'], 'completed', 'web_research'),
                    _ref('exploration', row['run_id'], character_id, row['ended_at'], 'completed', 'exploration_agent'), refs))
            return CharacterSnapshot(character_id, current, state['timezone_id'] if state else None, contact,
                                     tuple(discoveries), life_events, current_activity, interactions, latest_user, prefs, tuple(memories), tuple(handled_topics), len(handled_rows) <= 100)
        finally:
            db.rollback()
            db.execute(f"PRAGMA query_only={int(old_query_only)}")


_INVITATION = re.compile(r"(?:下次|改天|回头|等会)(?:再)?聊(?:一聊|聊)?(?:一下|关于)?[：: ]*([^，。！？?!；;]{2,30})")
_STOP = ("不聊", "别聊", "不要聊", "不想聊", "别提", "不要提", "别再提", "不要再提", "不要讨论", "不再讨论", "不再聊", "不继续", "不用再聊", "到此为止", "聊完了", "结束这个话题", "换个话题", "不必再聊", "别主动联系", "不要主动联系", "不要联系", "别联系", "别打扰", "不要打扰", "不要再打扰", "别再打扰", "不要再找我", "别主动找我", "先别找我", "暂时别找我", "先别联系", "暂时别联系", "先别打扰", "暂时别打扰")
_NEGATIVE = re.compile(r'不想|不能|不愿|不要|别|不必|不许|不用|不好|不行|不喜欢|不爱|讨厌|厌烦|反感|没兴趣|不感兴趣|拒绝|算了|换个话题')


def _key(character_id: str, kind: str, source_id: str) -> str:
    value = json.dumps([character_id, kind, source_id], ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _topic_key(topic: str) -> str:
    return re.sub(r'\s+', '', topic.strip(' 的呀吧啊啦哦 ')).casefold()


def interest_candidates(snapshot: CharacterSnapshot) -> tuple[Candidate, ...]:
    """Extension point: current preference weights alone cannot prove a new interest."""
    return ()


def generate_candidates(snapshot: CharacterSnapshot) -> tuple[Candidate, ...]:
    candidates = []
    now = snapshot.observed_at
    for discovery in snapshot.discoveries:
        created = _time(discovery.share.observed_at)
        if not created or created > now or now >= created + timedelta(hours=24):
            continue
        evidence = (discovery.share, discovery.event, discovery.run, *discovery.sources)
        candidates.append(Candidate('real_discovery', snapshot.character_id, evidence, 'evidence_backed_discovery', 'sourced', _key(snapshot.character_id, 'real_discovery', discovery.event.id), created + timedelta(hours=24)))
    for msg in snapshot.interactions:
        created = _time(msg.evidence.observed_at)
        if msg.role != 'user' or not snapshot.handled_topics_complete or msg.truncated or msg.handled or msg.has_later_user or not created or not timedelta(hours=24) <= now - created < timedelta(hours=72):
            continue
        if _NEGATIVE.search(msg.content) or any(stop in msg.content for stop in _STOP):
            continue
        match = _INVITATION.search(msg.content)
        if not match:
            continue
        topic = match.group(1).strip(' 的呀吧啊啦哦 ')
        if len(topic) < 2 or len(topic) > 30 or any(_topic_key(topic) == item.normalized_topic for item in snapshot.handled_topics):
            continue
        replies = [other for other in snapshot.interactions if other.conversation_id == msg.conversation_id and other.role == 'assistant' and not other.is_proactive and _time(other.evidence.observed_at) and created < _time(other.evidence.observed_at) <= now]
        if not replies:
            continue
        reply = min(replies, key=lambda item: (_time(item.evidence.observed_at), item.evidence.id))
        if reply.truncated or topic not in reply.content or _NEGATIVE.search(reply.content) or any(stop in reply.content for stop in _STOP) or not any(word in reply.content for word in ('好', '可以', '愿意', '期待', '想聊', '下次', '改天', '回头')):
            continue
        candidates.append(Candidate('shared_topic', snapshot.character_id, (msg.evidence, reply.evidence), 'explicit_followup_with_reply', 'explicit', _key(snapshot.character_id, 'shared_topic', msg.evidence.id), created + timedelta(hours=72)))
    return tuple(candidates[:20])


def dry_run(snapshot: CharacterSnapshot) -> Decision:
    """Evaluate only this snapshot; CONTACT is not a send authorization."""
    now, contact = snapshot.observed_at, snapshot.contact
    if contact.allowed is not True:
        return Decision(DecisionKind.SILENT, None, (), ('contact_not_allowed' if contact.allowed is False else 'contact_preferences_unknown',), False)
    if snapshot.latest_user and snapshot.latest_user.truncated:
        return Decision(DecisionKind.SILENT, None, (), ('latest_user_message_truncated',), False)
    if snapshot.latest_user and any(phrase in snapshot.latest_user.content for phrase in _STOP):
        return Decision(DecisionKind.SILENT, None, (), ('user_refusal',), False)
    try:
        if snapshot.timezone_id is None or contact.quiet_start_minute is None or contact.quiet_end_minute is None:
            raise ValueError('missing timezone or quiet hours')
        local = now.astimezone(ZoneInfo(snapshot.timezone_id))
    except (ZoneInfoNotFoundError, ValueError):
        return Decision(DecisionKind.SILENT, None, (), ('contact_policy_unknown',), False)
    candidates = generate_candidates(snapshot)
    candidate = candidates[0] if candidates else None
    ids = tuple(ref.id for ref in candidate.evidence) if candidate else ()
    def deferred(reason: str, retry: datetime | None = None) -> Decision:
        if candidate is None:
            return Decision(DecisionKind.SILENT, None, (), (reason, 'no_eligible_candidate'), False)
        return Decision(DecisionKind.DEFER, candidate.kind, ids, (reason,), False, candidate.dedup_key, retry)
    start, end = contact.quiet_start_minute, contact.quiet_end_minute
    minute = local.hour * 60 + local.minute
    if start != end and (start <= minute < end if start < end else minute >= start or minute < end):
        return deferred('quiet_hours')
    for value, reason in ((contact.pause_until, 'paused'), (contact.busy_until, 'busy')):
        parsed = _time(value)
        if value and parsed is None:
            return deferred('contact_policy_unknown')
        if parsed and parsed > now:
            return deferred(reason, parsed)
    if contact.ready_exists:
        return deferred('ready_message_pending')
    if not snapshot.latest_user:
        return deferred('no_user_interaction')
    user_at = _time(snapshot.latest_user.evidence.observed_at)
    if user_at is None or user_at > now:
        return deferred('user_interaction_time_unknown')
    if now - user_at < timedelta(hours=2):
        return deferred('recent_user_interaction', user_at + timedelta(hours=2))
    delivered = _time(contact.latest_delivered_at)
    if contact.last_status in ('delivered', 'read') and not contact.latest_delivered_at:
        return deferred('contact_time_unknown')
    if contact.latest_delivered_at and delivered is None:
        return deferred('contact_time_unknown')
    if delivered and user_at <= delivered:
        return deferred('awaiting_user_reply')
    last_at = _time(contact.latest_created_at)
    if contact.latest_created_at and last_at is None:
        return deferred('contact_time_unknown')
    if last_at and now - last_at < timedelta(hours=24):
        return deferred('contact_cooldown', last_at + timedelta(hours=24))
    if candidate is None:
        return Decision(DecisionKind.SILENT, None, (), ('no_eligible_candidate',), True)
    return Decision(DecisionKind.CONTACT, candidate.kind, ids, ('eligible_for_evaluation',), True, candidate.dedup_key)
