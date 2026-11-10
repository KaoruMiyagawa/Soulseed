"""Mock-only draft generation against disposable SQLite databases."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.autonomous_intent import Decision, DecisionKind, build_snapshot, dry_run, generate_candidates
from app.contact import ContactService
from app.memory.models import MemoryCreate
from app.memory.repositories import MemoryRepository
from app.ownership import character_scope
from app.proactive_expression import (DraftPlan, DraftService, MockDraftGenerator, MockEvidenceReviewer,
                                     PersonaProfile, PersonaRegistry, build_draft_context, draft_fingerprint)
from .test_autonomous_intent import ctx, conversation, discovery, dump, message


def now():
    return datetime.now(UTC) + timedelta(seconds=1)


def candidate_for(db, character='big_fish'):
    snapshot = build_snapshot(db, character, now=now())
    decision = dry_run(snapshot)
    candidate = next((item for item in generate_candidates(snapshot) if item.dedup_key == decision.dedup_key), None)
    return candidate, decision


def profile(character='big_fish', styles=('lively', 'curious', 'reflective')):
    return PersonaProfile(character, '活泼、调皮、有童真；也可以安静。', styles)


def ready_service(db, candidate, decision, persona, style='lively'):
    context = build_draft_context(db, persona.character_id, candidate, decision, persona, now=now())
    option = context.options[0]
    reviewer = MockEvidenceReviewer(frozenset({option.fingerprint(persona.character_id)}))
    generator = MockDraftGenerator(DraftPlan(style, option.key))
    return DraftService(db, generator, reviewer, PersonaRegistry((persona,)), clock=now), generator, context


def test_sourced_research_draft_is_read_only_and_carries_sources(ctx):
    db, life = ctx
    conversation(db, user='你好', reply='你好呀')
    run, source = discovery(db, life)
    candidate, decision = candidate_for(db)
    persona = profile()
    service, generator, context = ready_service(db, candidate, decision, persona)
    before = dump(db)
    result = service.generate('big_fish', candidate, decision)
    assert result.status == 'draft' and '海獭使用石块。' in result.text
    assert source['id'] in result.evidence_ids and result.source_urls == ('https://example.org/otters',)
    assert run['life_event_id'] in {x.evidence_id for x in context.expression.evidence}
    assert any('你好' in item for item in context.expression.recent_interactions)
    assert generator.calls == 1 and dump(db) == before


def test_distinct_personas_and_variants_for_same_intent(ctx):
    db, life = ctx
    conversation(db, user='你好', reply='你好呀')
    discovery(db, life)
    candidate, decision = candidate_for(db)
    playful = PersonaProfile('big_fish', '活泼、淘气、好奇', ('lively', 'curious'))
    calm = PersonaProfile('big_fish', '安静、审慎、喜欢思考', ('reflective',))
    messages = []
    for persona, style in ((playful, 'lively'), (playful, 'curious'), (calm, 'reflective')):
        service, _, _ = ready_service(db, candidate, decision, persona, style)
        result = service.generate('big_fish', candidate, decision)
        assert result.status == 'draft' and '海獭使用石块。' in result.text
        messages.append(result.text)
    assert len(set(messages)) == 3


def test_persona_profile_selects_mock_voice_without_caller_style_override(ctx):
    db, life = ctx
    conversation(db, user='你好', reply='你好呀')
    discovery(db, life)
    candidate, decision = candidate_for(db)
    messages = []
    for persona in (PersonaProfile('big_fish', '活泼好奇', ('lively',)),
                    PersonaProfile('big_fish', '安静审慎', ('reflective',))):
        context = build_draft_context(db, 'big_fish', candidate, decision, persona, now=now())
        reviewer = MockEvidenceReviewer(frozenset({context.options[0].fingerprint('big_fish')}))
        result = DraftService(db, MockDraftGenerator(None), reviewer, PersonaRegistry((persona,)), clock=now).generate('big_fish', candidate, decision)
        assert result.status == 'draft'
        messages.append(result.text)
    assert messages[0] != messages[1]


def test_reviewed_freeform_mock_drafts_allow_distinct_personal_voices(ctx):
    db, life = ctx
    conversation(db, user='你好', reply='你好呀')
    discovery(db, life)
    candidate, decision = candidate_for(db)
    messages = []
    for persona, style, text in (
        (PersonaProfile('big_fish', '活泼淘气', ('lively',)), 'lively', '海獭使用石块。看来小石头也能兼职当工具啦。'),
        (PersonaProfile('big_fish', '安静审慎', ('reflective',)), 'reflective', '海獭使用石块。这个细节值得再想想。'),
    ):
        context = build_draft_context(db, 'big_fish', candidate, decision, persona, now=now())
        option = context.options[0]
        review = MockEvidenceReviewer(frozenset({option.fingerprint('big_fish')}),
            frozenset({draft_fingerprint('big_fish', option, text)}))
        service = DraftService(db, MockDraftGenerator(DraftPlan(style, option.key, text)), review, PersonaRegistry((persona,)), clock=now)
        result = service.generate('big_fish', candidate, decision)
        assert result.status == 'draft' and result.text == text
        messages.append(result.text)
    assert messages[0] != messages[1]


def test_freeform_draft_requires_exact_review_and_cannot_invent_shared_plans(ctx):
    db, life = ctx
    conversation(db, user='你好', reply='你好呀')
    discovery(db, life)
    candidate, decision = candidate_for(db)
    persona = profile()
    context = build_draft_context(db, 'big_fish', candidate, decision, persona, now=now())
    option = context.options[0]
    for text in ('海獭使用石块。我们昨天约好一起去海边。', '海獭使用石块。你怎么还不回复我？'):
        review = MockEvidenceReviewer(frozenset({option.fingerprint('big_fish')}),
            frozenset({draft_fingerprint('big_fish', option, text)}))
        service = DraftService(db, MockDraftGenerator(DraftPlan('lively', option.key, text)), review, PersonaRegistry((persona,)), clock=now)
        assert service.generate('big_fish', candidate, decision).failure_code == 'draft_not_reviewed'
    review = MockEvidenceReviewer(frozenset({option.fingerprint('big_fish')}))
    for unreviewed in ('海獭使用石块。这个细节挺有趣。', '海獭使用石块。它们已经在月球建城。'):
        service = DraftService(db, MockDraftGenerator(DraftPlan('lively', option.key, unreviewed)), review, PersonaRegistry((persona,)), clock=now)
        assert service.generate('big_fish', candidate, decision).failure_code == 'draft_not_reviewed'


def test_explicit_common_topic_and_relevant_memory(ctx):
    db, _ = ctx
    _, anchor = conversation(db)
    repo = MemoryRepository(db)
    memory = repo.create(MemoryCreate('user_preference', '用户喜欢海獭', '海獭'))
    repo.add_source(memory.id, anchor, 'user', 'explicit_fact')
    candidate, decision = candidate_for(db)
    persona = profile()
    service, _, context = ready_service(db, candidate, decision, persona, 'curious')
    assert memory.id in {item.evidence_id for item in context.expression.evidence}
    assert 'reference-data-only' in context.expression.to_system_message()['content']
    result = service.generate('big_fish', candidate, decision)
    assert result.status == 'draft' and '海獭' in result.text and anchor in result.evidence_ids


@pytest.mark.parametrize('kind', [DecisionKind.SILENT, DecisionKind.DEFER])
def test_silent_and_defer_never_invoke_generator(ctx, kind):
    db, _ = ctx
    generator = MockDraftGenerator(DraftPlan('lively', 'anything'))
    result = DraftService(db, generator, MockEvidenceReviewer(), PersonaRegistry((profile(),))).generate(
        'big_fish', None, Decision(kind, None, (), ('reason',), False))
    assert result.status == 'suppressed' and generator.calls == 0


def test_no_review_or_fabricated_plan_cannot_assert_fact(ctx):
    db, life = ctx
    conversation(db, user='你好', reply='你好呀')
    discovery(db, life)
    candidate, decision = candidate_for(db)
    persona = profile()
    context = build_draft_context(db, 'big_fish', candidate, decision, persona, now=now())
    generator = MockDraftGenerator(DraftPlan('lively', context.options[0].key))
    result = DraftService(db, generator, MockEvidenceReviewer(), PersonaRegistry((persona,)), clock=now).generate('big_fish', candidate, decision)
    assert result.status == 'rejected' and result.failure_code == 'evidence_not_reviewed' and result.text is None
    review = MockEvidenceReviewer(frozenset({context.options[0].fingerprint('big_fish')}))
    for bad in (DraftPlan('lively', 'invented'), DraftPlan('unknown', context.options[0].key), '我和你约好了'):
        result = DraftService(db, MockDraftGenerator(bad), review, PersonaRegistry((persona,)), clock=now).generate('big_fish', candidate, decision)
        assert result.status == 'rejected' and result.text is None


def test_generated_personal_experience_in_sourced_claim_is_not_accepted(ctx):
    db, life = ctx
    conversation(db, user='你好', reply='你好呀')
    run, _ = discovery(db, life)
    with db.connection() as sql:
        import json
        row = sql.execute('SELECT claims_json FROM exploration_runs WHERE id=?', (run['id'],)).fetchone()
        claims = json.loads(row[0])
        for claim in claims:
            claim['text'] = '我和你约定一起去看海獭。'
        sql.execute('UPDATE exploration_runs SET claims_json=? WHERE id=?', (json.dumps(claims, ensure_ascii=False), run['id']))
    candidate, decision = candidate_for(db)
    generator = MockDraftGenerator(DraftPlan('lively', 'invented'))
    result = DraftService(db, generator, MockEvidenceReviewer(), PersonaRegistry((profile(),)), clock=now).generate('big_fish', candidate, decision)
    assert result.status == 'rejected' and result.text is None and generator.calls == 0


def test_fabricated_relationship_as_topic_is_rejected(ctx):
    db, _ = ctx
    conversation(db, user='下次聊聊我们结婚', reply='好呀，下次聊我们结婚')
    candidate, decision = candidate_for(db)
    assert candidate is not None
    generator = MockDraftGenerator(DraftPlan('lively', 'invented'))
    result = DraftService(db, generator, MockEvidenceReviewer(), PersonaRegistry((profile(),)), clock=now).generate('big_fish', candidate, decision)
    assert result.status == 'rejected' and result.text is None and generator.calls == 0


def test_wish_and_unverified_manual_event_do_not_become_draft_facts(ctx):
    db, life = ctx
    conversation(db)
    life.record('曾与用户看过海獭', generation_method='manual')
    life.record('想一起看海獭', status='wish')
    candidate, decision = candidate_for(db)
    persona = profile()
    service, _, context = ready_service(db, candidate, decision, persona)
    assert all('曾与用户' not in item.content for item in context.expression.evidence)
    result = service.generate('big_fish', candidate, decision)
    assert result.status == 'draft' and '看过' not in result.text


def test_simulated_active_activity_remains_labeled_and_is_not_a_draft_fact(ctx):
    db, life = ctx
    conversation(db)
    event = life.record('模拟探索角落', event_type='exploring', status='planned', generation_method='rule_engine')
    life.transition(event['id'], 'active')
    candidate, decision = candidate_for(db)
    persona = profile()
    service, _, context = ready_service(db, candidate, decision, persona)
    evidence = next(item for item in context.expression.evidence if item.evidence_id == event['id'])
    assert evidence.status == 'active' and evidence.generation_method == 'rule_engine'
    result = service.generate('big_fish', candidate, decision)
    assert result.status == 'draft' and '模拟探索角落' not in result.text


def test_source_change_during_generation_discards_draft(ctx, monkeypatch):
    db, life = ctx
    conversation(db, user='你好', reply='你好呀')
    _, source = discovery(db, life)
    candidate, decision = candidate_for(db)
    service, generator, context = ready_service(db, candidate, decision, profile())
    plan = DraftPlan('lively', context.options[0].key)
    def change_source(_context):
        with db.connection() as sql:
            sql.execute("UPDATE exploration_evidence SET page_relevance='irrelevant' WHERE id=?", (source['id'],))
        return plan
    monkeypatch.setattr(generator, 'generate', change_source)
    result = service.generate('big_fish', candidate, decision)
    assert result.status == 'rejected' and result.failure_code == 'context_changed' and result.text is None


def test_contact_refusal_during_generation_discards_draft(ctx, monkeypatch):
    db, life = ctx
    conversation(db, user='你好', reply='你好呀')
    discovery(db, life)
    candidate, decision = candidate_for(db)
    service, generator, context = ready_service(db, candidate, decision, profile())
    plan = DraftPlan('lively', context.options[0].key)
    def refuse_contact(_context):
        ContactService(life).set_preferences(allowed=False)
        return plan
    monkeypatch.setattr(generator, 'generate', refuse_contact)
    result = service.generate('big_fish', candidate, decision)
    assert result.status == 'rejected' and result.failure_code == 'context_changed' and result.text is None


def test_exact_duplicate_delivered_message_is_suppressed(ctx):
    db, life = ctx
    conversation(db, user='你好', reply='你好呀')
    discovery(db, life)
    candidate, decision = candidate_for(db)
    service, _, _ = ready_service(db, candidate, decision, profile())
    first = service.generate('big_fish', candidate, decision)
    assert first.status == 'draft'
    at = datetime.now(UTC) - timedelta(hours=50)
    intent = ContactService(life).create_intent(source='reminder', source_id='older', reason='user_requested', topic='提醒', now=at)
    with db.connection() as sql:
        sql.execute("INSERT INTO proactive_messages(id,intent_id,attempt_no,content,status,created_at,delivered_at) VALUES(?,?,0,?,'delivered',?,?)", (str(uuid4()), intent['id'], first.text, at.isoformat(), at.isoformat()))
    result = service.generate('big_fish', candidate, decision)
    assert result.status == 'rejected' and result.failure_code == 'duplicate_or_invalid_output'


def test_generation_failure_and_foreign_generator_never_write_or_call_external(ctx):
    db, life = ctx
    conversation(db, user='你好', reply='你好呀')
    discovery(db, life)
    candidate, decision = candidate_for(db)
    persona = profile()
    before = dump(db)
    mock = MockDraftGenerator(None, fail=True)
    assert DraftService(db, mock, MockEvidenceReviewer(), PersonaRegistry((persona,)), clock=now).generate('big_fish', candidate, decision).failure_code == 'generation_failed'
    class ForeignBackend:
        def generate(self, context):
            raise AssertionError('must not be called')
    assert DraftService(db, ForeignBackend(), MockEvidenceReviewer(), PersonaRegistry((persona,)), clock=now).generate('big_fish', candidate, decision).failure_code == 'mock_backend_required'
    assert dump(db) == before


def test_forged_decision_stale_source_and_cross_character_profile_are_rejected(ctx):
    db, life = ctx
    conversation(db, user='你好', reply='你好呀')
    run, source = discovery(db, life)
    candidate, decision = candidate_for(db)
    persona = profile()
    service, generator, _ = ready_service(db, candidate, decision, persona)
    forged = Decision(DecisionKind.CONTACT, candidate.kind, ('wrong',), ('forged',), True, candidate.dedup_key)
    assert service.generate('big_fish', candidate, forged).failure_code == 'context_not_eligible'
    assert service.generate('fish_b', candidate, decision).failure_code == 'persona_not_registered'
    with db.connection() as sql:
        sql.execute("UPDATE exploration_evidence SET page_relevance='irrelevant' WHERE id=?", (source['id'],))
    assert service.generate('big_fish', candidate, decision).failure_code == 'context_not_eligible'
    assert generator.calls == 0


def test_unanswered_proactive_message_blocks_generation(ctx):
    db, life = ctx
    conversation(db, user='你好', reply='你好呀')
    discovery(db, life)
    at = datetime.now(UTC) - timedelta(hours=1)
    intent = ContactService(life).create_intent(source='reminder', source_id='prior', reason='user_requested', topic='提醒', now=at)
    with db.connection() as sql:
        sql.execute("INSERT INTO proactive_messages(id,intent_id,attempt_no,content,status,created_at,delivered_at) VALUES(?,?,0,'先前消息','delivered',?,?)", (str(uuid4()), intent['id'], at.isoformat(), at.isoformat()))
    snapshot = build_snapshot(db, 'big_fish', now=now())
    decision = dry_run(snapshot)
    assert decision.result == DecisionKind.DEFER and decision.reasons == ('awaiting_user_reply',)
    generator = MockDraftGenerator(None)
    assert DraftService(db, generator, MockEvidenceReviewer(), PersonaRegistry((profile(),)), clock=now).generate('big_fish', None, decision).status == 'suppressed'
    assert generator.calls == 0


def test_role_scope_prevents_cross_role_evidence_and_memory(ctx):
    db, life = ctx
    conversation(db, user='你好', reply='你好呀')
    run_a, _ = discovery(db, life)
    candidate_a, decision_a = candidate_for(db)
    with db.connection() as sql:
        sql.execute("INSERT INTO characters VALUES ('fish_b','local','owned',datetime('now'))")
    with character_scope('fish_b'):
        life.ensure_state('fish_b')
        ContactService(life).set_preferences('fish_b', quiet_start_minute=0, quiet_end_minute=0)
        _, anchor = conversation(db, user='下次聊聊树蛙', reply='好呀，下次聊树蛙')
        repo = MemoryRepository(db)
        memory = repo.create(MemoryCreate('user_preference', '用户喜欢树蛙', '树蛙'))
        repo.add_source(memory.id, anchor, 'user', 'explicit_fact')
        candidate_b, decision_b = candidate_for(db, 'fish_b')
        persona_b = PersonaProfile('fish_b', '平静、善于思考', ('reflective',))
        service, generator, context = ready_service(db, candidate_b, decision_b, persona_b, 'reflective')
        assert memory.id in {item.evidence_id for item in context.expression.evidence}
        assert run_a['life_event_id'] not in {item.evidence_id for item in context.expression.evidence}
        assert service.generate('fish_b', candidate_a, decision_a).failure_code == 'context_not_eligible'
        assert generator.calls == 0
        assert service.generate('fish_b', candidate_b, decision_b).status == 'draft'
