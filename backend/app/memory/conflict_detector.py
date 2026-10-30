from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from .extractor import MemoryCandidate
from .models import CorrectionIntent, MemoryItem, MemoryStatus, MemoryType


class MemoryRelation(StrEnum):
    CONFLICT = "conflict"
    POTENTIAL_CONFLICT = "potential_conflict"
    COEXISTING = "coexisting"
    UNRELATED = "unrelated"
    NEEDS_REVIEW = "needs_review"


class RecommendedAction(StrEnum):
    NONE = "none"
    PRESERVE_BOTH = "preserve_both"
    PROPOSE_CORRECTION = "propose_correction"
    REQUEST_CONFIRMATION = "request_confirmation"
    MANUAL_REVIEW = "manual_review"


@dataclass(frozen=True)
class MemoryConflictResult:
    candidate: MemoryCandidate
    matched_memory_id: str
    relation: MemoryRelation
    evidence: tuple[str, ...]
    confidence: float
    needs_review: bool
    recommended_action: RecommendedAction


class MemoryConflictDetector:
    """Read-only, conservative relation checks for a candidate and memories.

    This detector never calls MemoryService and never writes to SQLite. Its
    lexical signals are only evidence for a review decision, not semantic proof.
    """

    _generic_terms = {
        "我", "你", "他", "她", "它", "的", "了", "是", "在", "和", "与", "有", "想", "要", "更", "现在", "以前", "已经", "喜欢", "偏好", "目标", "方法", "方式",
    }
    _slot_markers = {
        MemoryType.USER_PREFERENCE: ("喜欢", "偏好", "讨厌", "不喜欢", "使用", "习惯"),
        MemoryType.GOAL: ("目标", "准备", "打算", "计划", "希望"),
        MemoryType.GROWTH: ("学习", "练习", "坚持", "改善", "养成", "健身", "习惯"),
        MemoryType.EVENT: ("完成", "参加", "通过", "毕业", "入职", "搬家", "发布", "上线", "面试"),
        MemoryType.RELATIONSHIP: ("家人", "父母", "朋友", "同事", "老师", "伴侣", "孩子"),
    }

    def detect(self, candidate: MemoryCandidate | None, memories: list[MemoryItem] | tuple[MemoryItem, ...], *, include_unrelated: bool = False) -> list[MemoryConflictResult]:
        if candidate is None or not candidate.content.strip():
            return []
        results: list[MemoryConflictResult] = []
        for memory in memories or ():
            if not self._is_eligible(memory):
                continue
            result = self._classify(candidate, memory)
            if result is None and include_unrelated:
                result = self._result(candidate, memory, MemoryRelation.UNRELATED, ("没有发现同类型、同主题或明确变化依据",), 0.95, False, RecommendedAction.NONE)
            if result is not None and (include_unrelated or result.relation is not MemoryRelation.UNRELATED):
                results.append(result)
        if len(results) > 1 and any(result.relation in (MemoryRelation.CONFLICT, MemoryRelation.POTENTIAL_CONFLICT, MemoryRelation.NEEDS_REVIEW) for result in results):
            results = [
                MemoryConflictResult(result.candidate, result.matched_memory_id, result.relation, result.evidence + ("存在多个可能的旧记忆，不能自动选择",), result.confidence, True, RecommendedAction.REQUEST_CONFIRMATION)
                for result in results
            ]
        return results

    @classmethod
    def _classify(cls, candidate: MemoryCandidate, memory: MemoryItem) -> MemoryConflictResult | None:
        candidate_type = MemoryType(candidate.memory_type)
        memory_type = MemoryType(memory.memory_type)
        if candidate_type != memory_type:
            if candidate.intent is CorrectionIntent.EXPLICIT_CORRECTION:
                return cls._result(candidate, memory, MemoryRelation.UNRELATED, ("memory_type 不同，不能据此认定为同一纠正对象",), 0.98, False, RecommendedAction.NONE)
            return None

        exact_key = candidate.canonical_key.strip().casefold() == memory.canonical_key.strip().casefold()
        shared_terms = cls._terms(candidate.content) & cls._terms(memory.content)
        same_slot = cls._same_slot(candidate_type, candidate.content, memory.content)
        related = exact_key or bool(shared_terms) or same_slot
        evidence = []
        if exact_key:
            evidence.append("canonical_key 完全一致")
        if shared_terms:
            evidence.append("存在明确主题词重合：" + ", ".join(sorted(shared_terms)))
        if same_slot:
            evidence.append("记忆类型和表达槽位相近")

        if candidate.intent is CorrectionIntent.TEMPORARY_EXPERIMENT:
            if not related:
                return None
            evidence.append("候选包含短期尝试或时间限定")
            return cls._result(candidate, memory, MemoryRelation.COEXISTING, tuple(evidence), 0.9, False, RecommendedAction.PRESERVE_BOTH)

        if candidate.intent is CorrectionIntent.COEXISTING_INFORMATION:
            if not related and not same_slot:
                return None
            evidence.append("候选明确表达也可以同时成立")
            return cls._result(candidate, memory, MemoryRelation.COEXISTING, tuple(evidence), 0.92, False, RecommendedAction.PRESERVE_BOTH)

        if candidate.intent is CorrectionIntent.AMBIGUOUS_CHANGE:
            if not related and candidate_type != memory_type:
                return None
            evidence.append("候选包含不确定或可能性表达")
            if not related:
                evidence.append("与旧记忆同类型，但缺少可靠主题重合")
            return cls._result(candidate, memory, MemoryRelation.NEEDS_REVIEW, tuple(evidence), 0.45, True, RecommendedAction.REQUEST_CONFIRMATION)

        if candidate.intent is CorrectionIntent.EXPLICIT_CORRECTION:
            evidence.append("候选包含明确纠正表达")
            if related:
                return cls._result(candidate, memory, MemoryRelation.CONFLICT, tuple(evidence), 0.88 if exact_key or same_slot else 0.7, False, RecommendedAction.PROPOSE_CORRECTION)
            # An explicit correction with the same type but no reliable topic
            # is still a possible target; return it for multi-candidate review.
            return cls._result(candidate, memory, MemoryRelation.POTENTIAL_CONFLICT, tuple(evidence) + ("缺少可靠主题重合，不能自动定位",), 0.55, True, RecommendedAction.REQUEST_CONFIRMATION)

        if candidate.intent is CorrectionIntent.LONG_TERM_CHANGE:
            if not related:
                return None
            evidence.append("候选包含长期变化表达")
            return cls._result(candidate, memory, MemoryRelation.POTENTIAL_CONFLICT, tuple(evidence), 0.72, True, RecommendedAction.PROPOSE_CORRECTION)

        if not related:
            return None
        if cls._has_negation(candidate.content) != cls._has_negation(memory.content):
            evidence.append("新旧表达的否定方向不同")
            return cls._result(candidate, memory, MemoryRelation.POTENTIAL_CONFLICT, tuple(evidence), 0.7, True, RecommendedAction.REQUEST_CONFIRMATION)
        evidence.append("存在词面或表达槽位重合，但没有明确替代证据")
        return cls._result(candidate, memory, MemoryRelation.COEXISTING, tuple(evidence), 0.6, False, RecommendedAction.PRESERVE_BOTH)

    @classmethod
    def _same_slot(cls, memory_type: MemoryType, left: str, right: str) -> bool:
        markers = cls._slot_markers.get(memory_type, ())
        return any(marker in left and marker in right for marker in markers)

    @classmethod
    def _terms(cls, text: str) -> set[str]:
        terms: set[str] = set()
        for token in re.findall(r"[a-z0-9][a-z0-9_+#.-]*|[\u3400-\u4dbf\u4e00-\u9fff]+", (text or "").casefold()):
            if re.fullmatch(r"[a-z0-9][a-z0-9_+#.-]*", token):
                if len(token) >= 2 and token not in cls._generic_terms:
                    terms.add(token)
                continue
            if len(token) <= 4:
                if token not in cls._generic_terms:
                    terms.add(token)
            else:
                terms.update(token[index:index + 2] for index in range(len(token) - 1) if token[index:index + 2] not in cls._generic_terms)
        return terms

    @staticmethod
    def _has_negation(text: str) -> bool:
        return any(marker in (text or "") for marker in ("不喜欢", "讨厌", "不再", "不是", "没有", "放弃"))

    @staticmethod
    def _is_eligible(memory: MemoryItem) -> bool:
        if memory.status is not MemoryStatus.ACTIVE and memory.status != MemoryStatus.ACTIVE.value:
            return False
        if not memory.expires_at:
            return True
        try:
            expires_at = datetime.fromisoformat(memory.expires_at.replace("Z", "+00:00"))
        except ValueError:
            return False
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        return expires_at > datetime.now(UTC)

    @staticmethod
    def _result(candidate: MemoryCandidate, memory: MemoryItem, relation: MemoryRelation, evidence: tuple[str, ...], confidence: float, needs_review: bool, recommended_action: RecommendedAction) -> MemoryConflictResult:
        return MemoryConflictResult(candidate, memory.id, relation, evidence, confidence, needs_review, recommended_action)
