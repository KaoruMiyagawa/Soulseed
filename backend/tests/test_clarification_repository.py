from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from app import db as db_module
from app.db import Database
from app.memory import (
    ClarificationRepository,
    ClarificationStateError,
    ClarificationStatus,
    CorrectionIntent,
    MemoryCorrectionService,
    MemoryCreate,
    MemoryProposalRepository,
    MemoryProposalService,
    MemoryRepository,
    MemoryService,
    MemoryType,
    MemoryUpdate,
    ProposalCreate,
    ProposalType,
)
from app.repositories import ConversationRepository, MessageRepository


@pytest.fixture
def clarification_context(tmp_path):
    database = Database(str(tmp_path / "clarifications.db"))
    database.initialize()
    conversations = ConversationRepository(database)
    messages = MessageRepository(database)
    memories = MemoryService(MemoryRepository(database))
    proposals = MemoryProposalService(MemoryProposalRepository(database))
    conversation = conversations.create()
    trigger = messages.add(conversation["id"], "user", "我现在主要用 Linux。")
    target = memories.create_memory(
        MemoryCreate(MemoryType.USER_PREFERENCE, "我主要使用 Windows。", "environment:primary")
    )
    context = {
        "candidate": {
            "memory_type": MemoryType.USER_PREFERENCE.value,
            "content": "我现在主要用 Linux。",
            "canonical_key": "environment:primary",
        },
        "matched_memories": [
            {"id": target.id, "content": target.content, "updated_at": target.updated_at}
        ],
        "relation": "potential_conflict",
        "evidence": ["同一主要开发环境属性", "用户使用了长期变化表达"],
        "reason": "可能涉及主要开发环境的长期改变",
    }
    repository = ClarificationRepository(database)
    return database, conversations, messages, memories, proposals, repository, conversation, trigger, target, context


def new_clarification(context):
    _, _, _, _, _, repository, conversation, trigger, _, conflict_context = context
    return repository.create(conversation["id"], trigger["id"], conflict_context)


def test_fresh_database_has_migration_7_and_clarification_constraints(clarification_context):
    database = clarification_context[0]
    with database.connection() as db:
        assert db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 7
        columns = {row["name"] for row in db.execute("PRAGMA table_info(memory_correction_clarifications)")}
        assert {
            "id", "conversation_id", "trigger_message_id", "source_proposal_id", "conflict_context",
            "assistant_message_id", "response_message_id", "application_proposal_id", "status", "version",
            "processing_token", "lease_expires_at", "created_at", "updated_at",
        }.issubset(columns)
        indexes = {row["name"] for row in db.execute("PRAGMA index_list(memory_correction_clarifications)")}
        assert "idx_memory_clarifications_conversation_status" in indexes
        assert "idx_memory_clarifications_recovery" in indexes
        foreign_keys = {row["from"]: row["on_delete"] for row in db.execute("PRAGMA foreign_key_list(memory_correction_clarifications)")}
        assert foreign_keys["conversation_id"] == "CASCADE"
        assert foreign_keys["source_proposal_id"] == "SET NULL"
        assert foreign_keys["assistant_message_id"] == "CASCADE"
        assert foreign_keys["response_message_id"] == "CASCADE"
        assert foreign_keys["application_proposal_id"] == "RESTRICT"


def test_migration_7_upgrades_existing_database_and_preserves_data(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "upgrade-from-six.db"))
    with monkeypatch.context() as patch:
        patch.delitem(db_module.MIGRATIONS, 7)
        database.initialize()
        conversation = ConversationRepository(database).create()
        message = MessageRepository(database).add(conversation["id"], "user", "历史消息仍保留")
    with database.connection() as db:
        assert db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 6

    database.initialize()
    with database.connection() as db:
        assert db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 7
        assert db.execute("SELECT content FROM messages WHERE id = ?", (message["id"],)).fetchone()[0] == "历史消息仍保留"
        assert db.execute("SELECT title FROM conversations WHERE id = ?", (conversation["id"],)).fetchone()[0] == "新会话"
        assert db.execute("SELECT COUNT(*) FROM memory_correction_clarifications").fetchone()[0] == 0


def test_create_is_durable_and_idempotent_across_repository_restart(clarification_context):
    context = clarification_context
    database, _, _, _, _, repository, *_ = context
    first = new_clarification(context)
    restarted = ClarificationRepository(database).get(first.id)
    replay = ClarificationRepository(database).create(
        first.conversation_id,
        first.trigger_message_id,
        first.conflict_context,
    )
    assert restarted == first
    assert replay.id == first.id
    assert replay.status is ClarificationStatus.PREPARING


def test_optional_source_proposal_must_match_trigger_and_target_snapshot(clarification_context):
    _, _, _, _, proposals, repository, conversation, trigger, target, context = clarification_context
    proposal = proposals.create_proposal(
        ProposalCreate(
            proposal_type=ProposalType.CORRECTION,
            target_memory_id=target.id,
            proposed_memory_type=MemoryType.USER_PREFERENCE,
            proposed_content=context["candidate"]["content"],
            proposed_canonical_key=context["candidate"]["canonical_key"],
            evidence_message_id=trigger["id"],
            correction_intent=CorrectionIntent.LONG_TERM_CHANGE,
            relation="potential_conflict",
            confidence=0.8,
            evidence="已有目标与候选可能冲突",
            idempotency_key="clarification-source-proposal",
            expected_target_memory_updated_at=target.updated_at,
        )
    )
    linked = repository.create(
        conversation["id"],
        trigger["id"],
        context,
        source_proposal_id=proposal.id,
    )
    assert linked.source_proposal_id == proposal.id
    with pytest.raises(ClarificationStateError, match="已关联"):
        repository.create(
            conversation["id"],
            trigger["id"],
            {**context, "reason": "different clarification reason"},
            source_proposal_id=proposal.id,
        )


def test_waiting_requires_atomically_persisted_assistant_message(clarification_context):
    database, _, messages, _, _, repository, conversation, _, _, _ = clarification_context
    clarification = new_clarification(clarification_context)
    assert clarification.status is ClarificationStatus.PREPARING
    with pytest.raises(ClarificationStateError, match="已持久化"):
        repository.claim_response(clarification.id, "not-a-message")
    assert repository.get(clarification.id).status is ClarificationStatus.PREPARING

    prompt, waiting = repository.persist_prompt_and_wait(clarification.id, "你是已经换成 Linux 了，还是最近在试试？")
    assert prompt["role"] == "assistant"
    assert messages.list_for_conversation(conversation["id"])[-1]["id"] == prompt["id"]
    assert waiting.status is ClarificationStatus.AWAITING_USER
    assert waiting.assistant_message_id == prompt["id"]
    assert ClarificationRepository(database).get(clarification.id).status is ClarificationStatus.AWAITING_USER

    same = repository.persist_prompt_and_wait(clarification.id, prompt["content"])
    assert same[0] == prompt
    assert same[1] == waiting
    with pytest.raises(ClarificationStateError, match="不同内容"):
        repository.persist_prompt_and_wait(clarification.id, "换一条问题")


def test_only_persisted_questions_are_discoverable_and_multiple_pending_are_not_guessed(clarification_context):
    _, _, messages, _, _, repository, conversation, _, _, context = clarification_context
    first = new_clarification(clarification_context)
    assert repository.find_pending_for_conversation(conversation["id"]) is None
    repository.persist_prompt_and_wait(first.id, "这是长期改变还是暂时尝试？")
    assert repository.find_pending_for_conversation(conversation["id"]).id == first.id

    second_trigger = messages.add(conversation["id"], "user", "我可能也在换开发环境")
    second_context = {
        **context,
        "candidate": {**context["candidate"], "content": "我可能也在换开发环境"},
    }
    second = repository.create(conversation["id"], second_trigger["id"], second_context)
    repository.persist_prompt_and_wait(second.id, "你是在考虑长期换吗？")
    with pytest.raises(ClarificationStateError, match="多个待处理"):
        repository.find_pending_for_conversation(conversation["id"])


def test_prompt_insert_failure_rolls_back_without_entering_waiting(clarification_context):
    database, _, _, _, _, repository, _, _, _, _ = clarification_context
    clarification = new_clarification(clarification_context)
    with database.connection() as db:
        db.execute(
            "CREATE TRIGGER fail_assistant_prompt BEFORE INSERT ON messages "
            "WHEN NEW.role = 'assistant' BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
    with pytest.raises(Exception, match="injected"):
        repository.persist_prompt_and_wait(clarification.id, "澄清问题")
    record = repository.get(clarification.id)
    assert record.status is ClarificationStatus.PREPARING
    assert record.assistant_message_id is None


def test_claim_requires_persisted_same_conversation_user_reply(clarification_context):
    _, conversations, messages, _, _, repository, conversation, _, _, _ = clarification_context
    clarification = new_clarification(clarification_context)
    repository.persist_prompt_and_wait(clarification.id, "这是长期变化吗？")
    assistant = messages.add(conversation["id"], "assistant", "嗯？")
    other_conversation = conversations.create()
    foreign_user = messages.add(other_conversation["id"], "user", "是的")
    with pytest.raises(ClarificationStateError, match="user 消息"):
        repository.claim_response(clarification.id, assistant["id"])
    with pytest.raises(ClarificationStateError, match="同会话"):
        repository.claim_response(clarification.id, foreign_user["id"])
    assert repository.get(clarification.id).status is ClarificationStatus.AWAITING_USER


def test_concurrent_claim_of_same_reply_is_acquired_once(clarification_context):
    _, _, messages, _, _, repository, conversation, _, _, _ = clarification_context
    clarification = new_clarification(clarification_context)
    repository.persist_prompt_and_wait(clarification.id, "你是已经换环境还是正在尝试？")
    reply = messages.add(conversation["id"], "user", "我已经长期改用 Linux。")
    start = Barrier(2, timeout=5)

    def claim():
        start.wait()
        return ClarificationRepository(repository.database).claim_response(clarification.id, reply["id"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(claim), pool.submit(claim)]
        claims = [future.result(timeout=8) for future in results]
    assert sum(result.acquired for result in claims) == 1
    assert all(result.clarification.status is ClarificationStatus.PROCESSING for result in claims)
    assert len({result.clarification.processing_token for result in claims}) == 1
    assert repository.get(clarification.id).response_message_id == reply["id"]


def test_stale_worker_cannot_finish_after_lease_is_reclaimed(clarification_context):
    database, _, messages, _, _, repository, conversation, _, _, _ = clarification_context
    clarification = new_clarification(clarification_context)
    repository.persist_prompt_and_wait(clarification.id, "想再确认一下？")
    reply = messages.add(conversation["id"], "user", "长期改了")
    first = repository.claim_response(clarification.id, reply["id"])
    assert first.acquired
    with database.connection() as db:
        db.execute(
            "UPDATE memory_correction_clarifications SET lease_expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (clarification.id,),
        )
    assert repository.list_recoverable(conversation["id"])[0].id == clarification.id
    recovered = ClarificationRepository(database).claim_response(clarification.id, reply["id"])
    assert recovered.acquired
    assert recovered.clarification.processing_token != first.clarification.processing_token
    with pytest.raises(ClarificationStateError, match="租约已失效"):
        repository.finish_processing(clarification.id, first.clarification.processing_token, ClarificationStatus.EXPLAINED)


def test_processing_outcomes_and_failed_state_survive_restart(clarification_context):
    database, _, messages, _, _, repository, conversation, _, _, _ = clarification_context
    clarification = new_clarification(clarification_context)
    repository.persist_prompt_and_wait(clarification.id, "你指的是长期换了吗？")
    reply = messages.add(conversation["id"], "user", "我只是暂时试试")
    claim = repository.claim_response(clarification.id, reply["id"])
    failed = repository.finish_processing(
        clarification.id,
        claim.clarification.processing_token,
        ClarificationStatus.FAILED,
        error_code="classification_unavailable",
    )
    assert failed.status is ClarificationStatus.FAILED
    restarted = ClarificationRepository(database)
    assert restarted.get(clarification.id).response_message_id == reply["id"]
    assert restarted.list_recoverable(conversation["id"])[0].error_code == "classification_unavailable"
    retry = restarted.claim_response(clarification.id, reply["id"])
    assert retry.acquired
    temporary = restarted.finish_processing(
        clarification.id,
        retry.clarification.processing_token,
        ClarificationStatus.TEMPORARY,
    )
    assert temporary.status is ClarificationStatus.TEMPORARY
    assert temporary.processing_token is None


@pytest.mark.parametrize(
    "outcome",
    [ClarificationStatus.CONTINUE, ClarificationStatus.DEFERRED, ClarificationStatus.EXPLAINED, ClarificationStatus.DENIED],
)
def test_non_application_outcomes_are_durable_and_release_processing_lease(clarification_context, outcome):
    database, _, messages, _, _, repository, conversation, _, _, _ = clarification_context
    clarification = new_clarification(clarification_context)
    repository.persist_prompt_and_wait(clarification.id, "我想确认一下你说的变化")
    reply = messages.add(conversation["id"], "user", "我只是说说")
    claim = repository.claim_response(clarification.id, reply["id"])
    updated = repository.finish_processing(clarification.id, claim.clarification.processing_token, outcome)
    assert updated.status is outcome
    assert updated.processing_token is None
    assert ClarificationRepository(database).get(clarification.id) == updated


def test_transition_rejects_illegal_states_and_invalid_context(clarification_context):
    database, _, _, _, _, repository, conversation, trigger, target, _ = clarification_context
    with pytest.raises(ValueError, match="memory_type"):
        repository.create(conversation["id"], trigger["id"], {"candidate": {"content": "x"}, "matched_memories": [], "reason": "x"})
    clarification = new_clarification(clarification_context)
    with pytest.raises(ClarificationStateError, match="preparing"):
        repository.finish_processing(clarification.id, "fake-token", ClarificationStatus.DENIED)
    with database.connection() as db:
        with pytest.raises(Exception):
            db.execute(
                "INSERT INTO memory_correction_clarifications "
                "(id, idempotency_key, conversation_id, trigger_message_id, conflict_context, status, created_at, updated_at) "
                "VALUES ('bad-fk', 'bad-fk', ?, 'missing-message', '{}', 'preparing', 'now', 'now')",
                (conversation["id"],),
            )
        with pytest.raises(Exception):
            db.execute(
                "UPDATE memory_correction_clarifications SET status = 'made_up' WHERE id = ?",
                (clarification.id,),
            )
        with pytest.raises(Exception):
            db.execute(
                "INSERT INTO memory_correction_clarifications "
                "(id, idempotency_key, conversation_id, trigger_message_id, conflict_context, status, created_at, updated_at) "
                "VALUES ('bad-json', 'bad-json', ?, ?, '{invalid', 'preparing', 'now', 'now')",
                (conversation["id"], trigger["id"]),
            )


def test_create_rejects_stale_or_inactive_conflict_snapshot(clarification_context):
    _, _, _, memories, _, repository, conversation, trigger, target, context = clarification_context
    memories.update_memory(target.id, MemoryUpdate(content="目标已变化"))
    with pytest.raises(ClarificationStateError, match="快照已过期"):
        repository.create(conversation["id"], trigger["id"], context)


def test_applied_reconciliation_requires_real_application_log(clarification_context):
    database, _, messages, memories, proposals, repository, conversation, _, target, _ = clarification_context
    clarification = new_clarification(clarification_context)
    repository.persist_prompt_and_wait(clarification.id, "这是长期改变吗？")
    reply = messages.add(conversation["id"], "user", "我已经长期改用 Linux。")
    claim = repository.claim_response(clarification.id, reply["id"])

    proposal = proposals.create_proposal(
        ProposalCreate(
            proposal_type=ProposalType.CORRECTION,
            target_memory_id=target.id,
            proposed_memory_type=MemoryType.USER_PREFERENCE,
            proposed_content="我已经长期改用 Linux。",
            proposed_canonical_key="environment:primary:linux",
            evidence_message_id=reply["id"],
            correction_intent=CorrectionIntent.EXPLICIT_CORRECTION,
            relation="conflict",
            confidence=0.9,
            evidence="来自已持久化的用户答复",
            idempotency_key="clarification-final-proposal",
            expected_target_memory_updated_at=target.updated_at,
        )
    )
    repository.finish_processing(
        clarification.id,
        claim.clarification.processing_token,
        ClarificationStatus.EXPLAINED,
        application_proposal_id=proposal.id,
    )
    before_apply = repository.get(clarification.id)
    assert repository.reconcile_applied(clarification.id) == before_apply

    proposals.accept_proposal(proposal.id)
    MemoryCorrectionService(database).apply_proposal(proposal.id, idempotency_key="clarification-apply")
    reconciled = ClarificationRepository(database).reconcile_applied(clarification.id)
    assert reconciled.status is ClarificationStatus.APPLIED
    assert reconciled.application_proposal_id == proposal.id
    assert memories.get_memory(target.id, include_inactive=True).status.value == "superseded"


def test_application_state_cannot_be_fabricated_without_application_log(clarification_context):
    database, _, messages, _, proposals, repository, conversation, _, target, _ = clarification_context
    clarification = new_clarification(clarification_context)
    repository.persist_prompt_and_wait(clarification.id, "请解释一下变化？")
    reply = messages.add(conversation["id"], "user", "永久改变了")
    claim = repository.claim_response(clarification.id, reply["id"])
    proposal = proposals.create_proposal(
        ProposalCreate(
            proposal_type=ProposalType.CORRECTION,
            target_memory_id=target.id,
            proposed_memory_type=MemoryType.USER_PREFERENCE,
            proposed_content="永久改变了",
            proposed_canonical_key="environment:permanent:new",
            evidence_message_id=reply["id"],
            correction_intent=CorrectionIntent.EXPLICIT_CORRECTION,
            relation="conflict",
            confidence=0.9,
            evidence="真实用户回复",
            idempotency_key="clarification-not-applied",
            expected_target_memory_updated_at=target.updated_at,
        )
    )
    proposals.accept_proposal(proposal.id)
    with pytest.raises(ClarificationStateError, match="真实 Proposal application"):
        repository.finish_processing(
            clarification.id,
            claim.clarification.processing_token,
            ClarificationStatus.APPLIED,
            application_proposal_id=proposal.id,
        )
    assert repository.get(clarification.id).status is ClarificationStatus.PROCESSING
