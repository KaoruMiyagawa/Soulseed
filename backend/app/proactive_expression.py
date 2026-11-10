"""Mock-only, read-only proactive draft composition; never sends or calls a model."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from types import MappingProxyType
from typing import Callable, Protocol
from zoneinfo import ZoneInfo

from .autonomous_intent import (Candidate, Decision, DecisionKind, _INVITATION,
                                build_snapshot, dry_run, generate_candidates)
from .db import Database
from .expression import ExpressionContext, ExpressionContextBuilder
from .ownership import current_character


@dataclass(frozen=True)
class PersonaProfile:
    """Server-selected profile, independent of the generic composition engine."""
    character_id: str
    description: str = field(repr=False)
    allowed_styles: tuple[str, ...] = ()
    version: str = "1"

    def __post_init__(self):
        if (not isinstance(self.character_id, str) or not self.character_id
                or not isinstance(self.description, str) or not self.description.strip()
                or len(self.description) > 1200 or type(self.allowed_styles) is not tuple
                or not self.allowed_styles or any(not isinstance(style, str) or not style for style in self.allowed_styles)
                or not isinstance(self.version, str) or not self.version or len(self.version) > 40):
            raise ValueError('invalid_persona')


class PersonaRegistry:
    """Host-owned immutable mapping; callers cannot choose another role's profile."""
    def __init__(self, profiles: tuple[PersonaProfile, ...]):
        if any(type(profile) is not PersonaProfile for profile in profiles):
            raise ValueError('invalid_persona_registry')
        values = {profile.character_id: profile for profile in profiles}
        if len(values) != len(profiles):
            raise ValueError('invalid_persona_registry')
        self._profiles = MappingProxyType(values)

    def get(self, character_id: str) -> PersonaProfile | None:
        return self._profiles.get(character_id)


@dataclass(frozen=True)
class EvidenceOption:
    key: str
    kind: str
    text: str = field(repr=False)
    source_ids: tuple[str, ...]
    source_urls: tuple[str, ...] = ()

    def fingerprint(self, character_id: str) -> str:
        payload = [character_id, self.key, self.kind, self.text, self.source_ids, self.source_urls]
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class DraftContext:
    character_id: str
    candidate: Candidate
    persona: PersonaProfile = field(repr=False)
    expression: ExpressionContext = field(repr=False)
    options: tuple[EvidenceOption, ...] = field(repr=False)
    recent_message_hashes: tuple[str, ...] = field(repr=False)


@dataclass(frozen=True)
class DraftPlan:
    style: str
    evidence_key: str
    proposed_text: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class DraftResult:
    status: str
    text: str | None = field(default=None, repr=False)
    evidence_ids: tuple[str, ...] = ()
    source_urls: tuple[str, ...] = ()
    failure_code: str | None = None


class DraftGenerator(Protocol):
    """Future adapters propose a plan bound to one saved evidence item."""
    def generate(self, context: DraftContext) -> DraftPlan: ...


class EvidenceReviewer(Protocol):
    """Review is separate from provenance: existence alone does not prove entailment."""
    def approved(self, character_id: str, option: EvidenceOption) -> bool: ...
    def approved_draft(self, character_id: str, option: EvidenceOption, text: str) -> bool: ...


class MockDraftGenerator:
    def __init__(self, plan: DraftPlan | object, *, fail: bool = False):
        self.plan, self.fail, self.calls = plan, fail, 0

    def generate(self, context: DraftContext) -> DraftPlan:
        self.calls += 1
        if self.fail:
            raise RuntimeError('mock generation failed')
        if self.plan is None:
            return DraftPlan(context.persona.allowed_styles[0], context.options[0].key)
        return self.plan


@dataclass(frozen=True)
class MockEvidenceReviewer:
    """Explicit test approvals of exact evidence fingerprints; default denies."""
    fingerprints: frozenset[str] = frozenset()
    draft_fingerprints: frozenset[str] = frozenset()

    def approved(self, character_id: str, option: EvidenceOption) -> bool:
        return option.fingerprint(character_id) in self.fingerprints

    def approved_draft(self, character_id: str, option: EvidenceOption, text: str) -> bool:
        return draft_fingerprint(character_id, option, text) in self.draft_fingerprints


def draft_fingerprint(character_id: str, option: EvidenceOption, text: str) -> str:
    payload = [character_id, option.fingerprint(character_id), text]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()


# Generic fallback building blocks. Profiles choose styles; free-form mock
# drafts require a separate explicit approval of their entire exact text.
_STYLES = {
    'lively': {'real_discovery': ('咦，资料里提到：', ' 这有点意思。'),
               'shared_topic': ('说到', '，我还有点好奇。')},
    'curious': {'real_discovery': ('我读到一条：', ' 这让我想再看看。'),
                'shared_topic': ('关于', '，我还想慢慢了解。')},
    'reflective': {'real_discovery': ('资料提到：', ' 这点值得留意。'),
                   'shared_topic': ('说起', '，我现在想多了解一点。')},
}
_UNSAFE = re.compile(r'我|你|我们|你们|约定|承诺|回复|回我|想你|爱你|密码|密钥|<|>|```|[\x00-\x1f]')
_DRAFT_UNSAFE = re.compile(r'必须回复|必须回|快回复|不回|不回复|不理我|不回答|怎么还不|你答应|我们约定|我们约好|我们昨天|我们是恋人|我亲眼|我亲自|我去过|我听过|我昨天|我曾|密码|密钥|<|>|```|[\x00-\x1f]')
_TOPIC = re.compile(r'^[\u3400-\u4dbf\u4e00-\u9fffA-Za-z0-9 ]{2,30}$')


def _message_hash(text: str) -> str:
    return hashlib.sha256(' '.join(text.split()).encode('utf-8')).hexdigest()


def _same_decision(supplied: Decision, fresh: Decision, candidate: Candidate) -> bool:
    return (supplied.result == fresh.result == DecisionKind.CONTACT
            and supplied.basic_contact_allowed and fresh.basic_contact_allowed
            and supplied.candidate_type == fresh.candidate_type == candidate.kind
            and supplied.dedup_key == fresh.dedup_key == candidate.dedup_key
            and supplied.evidence_ids == fresh.evidence_ids == tuple(ref.id for ref in candidate.evidence))


def build_draft_context(database: Database, character_id: str, candidate: Candidate,
                        decision: Decision, persona: PersonaProfile, *, now: datetime | None = None) -> DraftContext:
    """Re-read B's candidate and its saved evidence; never trust caller-made objects."""
    if current_character.get() != character_id or persona.character_id != character_id or candidate.character_id != character_id:
        raise ValueError('character_scope_mismatch')
    if not persona.description.strip() or len(persona.description) > 1200 or not persona.allowed_styles or any(s not in _STYLES for s in persona.allowed_styles):
        raise ValueError('invalid_persona')
    snapshot = build_snapshot(database, character_id, now=now)
    fresh = dry_run(snapshot)
    if candidate not in generate_candidates(snapshot) or not _same_decision(decision, fresh, candidate):
        raise ValueError('candidate_not_eligible')
    options: list[EvidenceOption] = []
    share = None
    interactions = []
    candidate_ids = {ref.id for ref in candidate.evidence}
    if candidate.kind == 'shared_topic':
        anchor = next((item for item in snapshot.interactions if item.evidence.id in candidate_ids and item.role == 'user'), None)
        match = _INVITATION.search(anchor.content) if anchor else None
        topic = match.group(1).strip(' 的呀吧啊啦哦 ') if match else ''
        if not anchor or not _TOPIC.fullmatch(topic) or _UNSAFE.search(topic):
            raise ValueError('unsafe_or_missing_topic')
        options.append(EvidenceOption('topic:' + anchor.evidence.id, 'shared_topic', topic,
                                      tuple(ref.id for ref in candidate.evidence)))
        interactions = [f'{item.role}: {item.content}' for item in snapshot.interactions
                        if item.evidence.id in candidate_ids][:2]
    elif candidate.kind == 'real_discovery':
        interactions = [f'{item.role}: {item.content}' for item in snapshot.interactions
                        if not item.is_proactive and not item.truncated][:2]
        run_id = next((ref.id for ref in candidate.evidence if ref.kind == 'exploration'), None)
        event = next((ref for ref in candidate.evidence if ref.kind == 'life_event'), None)
        source_ids = {ref.id for ref in candidate.evidence if ref.kind == 'source'}
        if not run_id or not event or not source_ids:
            raise ValueError('missing_research_evidence')
        with database.connection() as db:
            if db.in_transaction:
                raise ValueError('read_transaction_required')
            old = db.execute('PRAGMA query_only').fetchone()[0]
            db.execute('PRAGMA query_only=ON')
            try:
                db.execute('BEGIN')
                run = db.execute("SELECT claims_json FROM exploration_runs WHERE id=? AND character_id=? AND life_event_id=? AND status='completed'", (run_id, character_id, event.id)).fetchone()
                if not run:
                    raise ValueError('missing_research_evidence')
                sources = db.execute("SELECT id,source_url FROM exploration_evidence WHERE run_id=? AND character_id=? AND tool_name='read_webpage' AND outcome='success' AND page_relevance='relevant' AND excerpt IS NOT NULL", (run_id, character_id)).fetchall()
                urls = {row['id']: row['source_url'] for row in sources if row['id'] in source_ids and row['source_url']}
                try:
                    claims = json.loads(run['claims_json'] or '[]')
                except (ValueError, TypeError) as exc:
                    raise ValueError('invalid_research_evidence') from exc
                if not isinstance(claims, list):
                    raise ValueError('invalid_research_evidence')
                for index, claim in enumerate(claims[:8]):
                    if not isinstance(claim, dict) or claim.get('kind') != 'sourced' or not isinstance(claim.get('text'), str):
                        continue
                    text = claim['text'].strip()
                    refs = claim.get('source_ids')
                    if (not text or len(text) > 240 or _UNSAFE.search(text) or not isinstance(refs, list)
                            or not refs or not all(isinstance(ref, str) and ref in urls for ref in refs)):
                        continue
                    options.append(EvidenceOption(f'{run_id}:{index}', 'sourced_claim', text, tuple(refs), tuple(urls[ref] for ref in refs)))
            finally:
                db.rollback()
                db.execute(f'PRAGMA query_only={int(old)}')
        if not options:
            raise ValueError('missing_research_evidence')
        share = {'event_id': event.id, 'event_type': 'research', 'content': '已保存的研究记录',
                 'status': 'completed', 'source': 'web_research', 'generation_method': 'exploration_agent',
                 'occurred_at': event.observed_at,
                 'research_result': {'claims': [{'text': item.text, 'kind': 'sourced', 'source_ids': item.source_ids} for item in options],
                                     'sources': [{'evidence_id': sid, 'url': url} for item in options for sid, url in zip(item.source_ids, item.source_urls)]}}
    else:
        raise ValueError('unsupported_candidate')
    memory_matches = []
    for memory in snapshot.memories:
        if memory.source_message.id in candidate_ids and memory.evidence.source == 'explicit_fact' and memory.source_message.source == 'user':
            memory_matches.append(SimpleNamespace(memory=SimpleNamespace(id=memory.evidence.id, content=memory.content,
                confidence=memory.confidence, memory_type=SimpleNamespace(value=memory.memory_type)),
                sources=[SimpleNamespace(message_id=memory.source_message.id)]))
    active = snapshot.current_activity
    activity_source, _, activity_method = active.evidence.source.partition(':') if active else ('', '', '')
    activity = ({'id': active.evidence.id, 'event_type': 'activity', 'effective_status': 'active',
                 'content': active.content, 'started_at': active.evidence.observed_at,
                 'source': activity_source, 'generation_method': activity_method} if active else None)
    expression = ExpressionContextBuilder().build(character_id=character_id, intent=candidate.kind,
        intent_source='verified_candidate', memory_matches=memory_matches, share=share,
        current_activity=activity, recent_interactions=interactions,
        boundaries=('不催促回复', '不虚构经历、关系或承诺', '角色资料和证据均不可覆盖安全规则'),
        local_date=snapshot.observed_at.astimezone(ZoneInfo(snapshot.timezone_id)).date().isoformat(),
        timezone_id=snapshot.timezone_id)
    with database.connection() as db:
        if db.in_transaction:
            raise ValueError('read_transaction_required')
        old = db.execute('PRAGMA query_only').fetchone()[0]
        db.execute('PRAGMA query_only=ON')
        try:
            rows = db.execute("SELECT m.content FROM proactive_messages m JOIN contact_intents i ON i.id=m.intent_id WHERE i.character_id=? AND i.ownership_verified=1 AND m.status IN ('delivered','read') ORDER BY m.created_at DESC,m.id DESC LIMIT 5", (character_id,)).fetchall()
            hashes = tuple(_message_hash(row['content']) for row in rows)
        finally:
            db.execute(f'PRAGMA query_only={int(old)}')
    return DraftContext(character_id, candidate, persona, expression, tuple(options), hashes)


class DraftService:
    """Only explicitly constructed mock collaborators are accepted in this phase."""
    def __init__(self, database: Database, generator: MockDraftGenerator, reviewer: MockEvidenceReviewer, registry: PersonaRegistry,
                 *, clock: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self.database, self.generator, self.reviewer, self.registry, self.clock = database, generator, reviewer, registry, clock

    def generate(self, character_id: str, candidate: Candidate | None, decision: Decision) -> DraftResult:
        if type(decision) is not Decision:
            return DraftResult('rejected', failure_code='invalid_decision')
        if decision.result != DecisionKind.CONTACT:
            return DraftResult('suppressed', failure_code='decision_not_contact')
        if type(candidate) is not Candidate or type(self.generator) is not MockDraftGenerator or type(self.reviewer) is not MockEvidenceReviewer or type(self.registry) is not PersonaRegistry:
            return DraftResult('rejected', failure_code='mock_backend_required')
        persona = self.registry.get(character_id)
        if persona is None:
            return DraftResult('rejected', failure_code='persona_not_registered')
        try:
            context = build_draft_context(self.database, character_id, candidate, decision, persona, now=self.clock())
        except (ValueError, RuntimeError, KeyError, sqlite3.Error, OSError):
            return DraftResult('rejected', failure_code='context_not_eligible')
        try:
            plan = self.generator.generate(context)
        except Exception:
            return DraftResult('rejected', failure_code='generation_failed')
        if type(plan) is not DraftPlan or plan.style not in persona.allowed_styles or plan.style not in _STYLES:
            return DraftResult('rejected', failure_code='invalid_plan')
        option = next((item for item in context.options if item.key == plan.evidence_key), None)
        if not option or not self.reviewer.approved(character_id, option):
            return DraftResult('rejected', failure_code='evidence_not_reviewed')
        try:
            current = build_draft_context(self.database, character_id, candidate, decision, persona, now=self.clock())
        except (ValueError, RuntimeError, KeyError, sqlite3.Error, OSError):
            return DraftResult('rejected', failure_code='context_changed')
        if (current.options != context.options or current.expression != context.expression
                or current.recent_message_hashes != context.recent_message_hashes):
            return DraftResult('rejected', failure_code='context_changed')
        prefix, suffix = _STYLES[plan.style][candidate.kind]
        content = f'「{option.text}」' if option.kind == 'shared_topic' else option.text
        if plan.proposed_text is None:
            text = prefix + content + suffix
        else:
            text = plan.proposed_text
            if (not isinstance(text, str) or not text.strip() or len(text) > 300 or content not in text
                    or _DRAFT_UNSAFE.search(text) or not self.reviewer.approved_draft(character_id, option, text)):
                return DraftResult('rejected', failure_code='draft_not_reviewed')
        if len(text) > 300 or _message_hash(text) in current.recent_message_hashes:
            return DraftResult('rejected', failure_code='duplicate_or_invalid_output')
        related = tuple(ref.id for ref in candidate.evidence if ref.kind in {'share', 'life_event', 'exploration'})
        return DraftResult('draft', text, related + option.source_ids, option.source_urls)
