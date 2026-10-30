from datetime import UTC, datetime, timedelta

from app.db import Database
from app.memory import (
    CorrectionIntent,
    DeterministicMemoryExtractor,
    MemoryConflictDetector,
    MemoryItem,
    MemoryRelation,
    MemoryStatus,
    MemoryType,
    RecommendedAction,
)
from app.memory.models import MemoryCreate
from app.memory.repositories import MemoryRepository
from app.memory.service import MemoryService


def make_memory(memory_id, content, *, memory_type=MemoryType.USER_PREFERENCE, status=MemoryStatus.ACTIVE, expires_at=None, canonical_key=None):
    return MemoryItem(memory_id, memory_type, content, canonical_key or f"{memory_type.value}:{memory_id}", 0.7, 0.8, status, expires_at, None, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00", None)


def candidate(text):
    extracted = DeterministicMemoryExtractor().extract_text(text)
    assert extracted, text
    return extracted[0]


def test_explicit_correction_matches_one_goal():
    detector = MemoryConflictDetector()
    result = detector.detect(candidate("我之前说错了，我的目标是雅思 7 分。"), [make_memory("old", "我的目标是雅思 6.5 分。", memory_type=MemoryType.GOAL)])

    assert len(result) == 1
    assert result[0].relation is MemoryRelation.CONFLICT
    assert result[0].recommended_action is RecommendedAction.PROPOSE_CORRECTION
    assert not result[0].needs_review
    assert result[0].confidence < 1


def test_explicit_correction_returns_all_possible_old_memories_for_review():
    detector = MemoryConflictDetector()
    results = detector.detect(candidate("我之前说错了，我的目标是雅思 7 分。"), [
        make_memory("old-a", "我的目标是雅思 6 分。", memory_type=MemoryType.GOAL),
        make_memory("old-b", "我的目标是雅思 6.5 分。", memory_type=MemoryType.GOAL),
    ])

    assert {item.matched_memory_id for item in results} == {"old-a", "old-b"}
    assert all(item.needs_review for item in results)
    assert all(item.recommended_action is RecommendedAction.REQUEST_CONFIRMATION for item in results)


def test_long_term_change_is_only_a_potential_conflict():
    result = MemoryConflictDetector().detect(candidate("我现在更喜欢 Rust 了。"), [make_memory("old", "我更喜欢 Python。")])

    assert len(result) == 1
    assert result[0].relation is MemoryRelation.POTENTIAL_CONFLICT
    assert result[0].needs_review
    assert result[0].recommended_action is RecommendedAction.PROPOSE_CORRECTION


def test_temporary_experiment_preserves_long_term_memory():
    result = MemoryConflictDetector().detect(candidate("这周我想暂时减少健身频率。"), [make_memory("old", "我平时每周健身三次。", memory_type=MemoryType.GROWTH)])

    assert len(result) == 1
    assert result[0].relation is MemoryRelation.COEXISTING
    assert not result[0].needs_review
    assert result[0].recommended_action is RecommendedAction.PRESERVE_BOTH


def test_ambiguous_change_needs_review_without_replacement():
    result = MemoryConflictDetector().detect(candidate("Rust 好像也不错。"), [make_memory("old", "我喜欢 Python。")])

    assert len(result) == 1
    assert result[0].relation is MemoryRelation.NEEDS_REVIEW
    assert result[0].needs_review
    assert result[0].recommended_action is RecommendedAction.REQUEST_CONFIRMATION


def test_coexisting_information_does_not_conflict():
    result = MemoryConflictDetector().detect(candidate("我也喜欢 Rust。"), [make_memory("old", "我喜欢 Python。")])

    assert len(result) == 1
    assert result[0].relation is MemoryRelation.COEXISTING
    assert result[0].recommended_action is RecommendedAction.PRESERVE_BOTH


def test_different_topics_and_similar_keywords_are_not_conflicts():
    detector = MemoryConflictDetector()
    different_type = detector.detect(candidate("我喜欢 Rust。"), [make_memory("goal", "我的目标是完成 Rust 项目。", memory_type=MemoryType.GOAL)])
    similar_preference = detector.detect(candidate("我喜欢清爽的界面。"), [make_memory("old", "我喜欢简洁的界面。")])

    assert different_type == []
    assert len(similar_preference) == 1
    assert similar_preference[0].relation is MemoryRelation.COEXISTING
    assert similar_preference[0].relation is not MemoryRelation.CONFLICT


def test_inactive_and_expired_memories_are_filtered():
    expired_at = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    memories = [
        make_memory("invalid", "我喜欢 Python。", status=MemoryStatus.INVALID),
        make_memory("superseded", "我喜欢 Python。", status=MemoryStatus.SUPERSEDED),
        make_memory("deleted", "我喜欢 Python。", status=MemoryStatus.DELETED),
        make_memory("expired-status", "我喜欢 Python。", status=MemoryStatus.EXPIRED),
        make_memory("expired-time", "我喜欢 Python。", expires_at=expired_at),
    ]

    assert MemoryConflictDetector().detect(candidate("我现在更喜欢 Rust 了。"), memories) == []


def test_none_candidate_and_empty_input_are_safe():
    detector = MemoryConflictDetector()
    assert detector.detect(None, [make_memory("old", "我喜欢 Python。")]) == []
    assert detector.detect(candidate("我喜欢 Python。"), []) == []


def test_detector_does_not_write_database(tmp_path):
    database = Database(str(tmp_path / "conflict-read-only.db"))
    database.initialize()
    service = MemoryService(MemoryRepository(database))
    memory = service.create_memory(MemoryCreate(MemoryType.USER_PREFERENCE, "我喜欢 Python。", "pref:python"))
    before = service.list_change_log()

    result = MemoryConflictDetector().detect(candidate("我现在更喜欢 Rust 了。"), [memory])

    assert result
    assert service.get_memory(memory.id).status is MemoryStatus.ACTIVE
    assert service.list_change_log() == before
