from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable

from .conflict_detector import MemoryConflictDetector, MemoryRelation, RecommendedAction
from .extractor import MemoryCandidate
from .models import CorrectionIntent, MemoryActorType, ProposalApplicationStatus, ProposalCreate, ProposalStatus, ProposalType
from .proposal_service import MemoryProposalService
from .proposal_repositories import ProposalTargetInactiveError, ProposalTargetVersionConflictError
from .service import MemoryService


class CorrectionFlowStatus(StrEnum):
    NO_ACTION = "no_action"
    PROPOSAL_CREATED = "proposal_created"
    PROPOSAL_REUSED = "proposal_reused"
    PROPOSAL_ACCEPTED = "proposal_accepted"
    PROPOSAL_APPLIED = "proposal_applied"
    PROPOSAL_REJECTED = "proposal_rejected"
    PROPOSAL_CANCELLED = "proposal_cancelled"
    PROPOSAL_EXPIRED = "proposal_expired"
    CLARIFICATION_NEEDED = "clarification_needed"
    COEXISTING = "coexisting"
    PROCESSING_FAILED = "processing_failed"


@dataclass(frozen=True)
class CorrectionProposalSummary:
    proposal_id: str
    target_memory_id: str
    relation: MemoryRelation
    confidence: float
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class CorrectionFlowResult:
    status: CorrectionFlowStatus
    reason: str
    proposal: CorrectionProposalSummary | None = None
    clarification: tuple[str, ...] = ()
    error_code: str | None = None


class CorrectionFlowService:
    """Orchestrate correction detection without changing formal memories.

    The service accepts candidates produced by the deterministic extractor so
    callers do not need to extract the same user message twice. It only reads
    active memories, runs the read-only conflict detector, and creates pending
    proposals. Formal memory changes remain exclusively owned by
    MemoryCorrectionService.
    """

    _proposal_intents = {
        CorrectionIntent.EXPLICIT_CORRECTION,
        CorrectionIntent.LONG_TERM_CHANGE,
    }
    _proposal_relations = {
        MemoryRelation.CONFLICT,
        MemoryRelation.POTENTIAL_CONFLICT,
    }

    def __init__(
        self,
        memory_service: MemoryService,
        proposal_service: MemoryProposalService,
        conflict_detector: MemoryConflictDetector | None = None,
        *,
        memory_limit: int = 1000,
    ):
        if memory_limit < 1:
            raise ValueError("memory_limit 必须大于 0")
        self.memory_service = memory_service
        self.proposal_service = proposal_service
        self.conflict_detector = conflict_detector or MemoryConflictDetector()
        self.memory_limit = memory_limit

    def process(
        self,
        user_message: dict,
        candidates: Iterable[MemoryCandidate],
    ) -> tuple[CorrectionFlowResult, ...]:
        """Process already-extracted candidates in input order."""
        return tuple(self.process_candidate(user_message, candidate) for candidate in candidates)

    def process_candidate(
        self,
        user_message: dict,
        candidate: MemoryCandidate,
    ) -> CorrectionFlowResult:
        """Process one candidate and return a public, non-database result."""
        if candidate.intent is CorrectionIntent.NONE:
            return self._no_action("候选没有纠正意图")

        try:
            memories = self.memory_service.list_memories(
                limit=self.memory_limit,
                memory_type=candidate.memory_type,
            )
            conflicts = tuple(self.conflict_detector.detect(candidate, memories))
        except Exception as exc:
            return self._failure("读取记忆或执行冲突检测失败", "conflict_detection_failed", exc)

        if candidate.intent is CorrectionIntent.AMBIGUOUS_CHANGE:
            return self._clarification(
                "表达包含不确定变化，不能直接修改长期记忆",
                ("请确认这是长期变化，还是暂时尝试。",),
            )

        if candidate.intent in (CorrectionIntent.TEMPORARY_EXPERIMENT, CorrectionIntent.COEXISTING_INFORMATION):
            if any(result.relation is MemoryRelation.COEXISTING for result in conflicts):
                return CorrectionFlowResult(
                    status=CorrectionFlowStatus.COEXISTING,
                    reason="候选与已有记忆可以同时成立，不创建替代 Proposal",
                )
            return self._no_action("临时尝试或共存表达不创建纠正 Proposal")

        if any(result.temporary_signal for result in conflicts):
            if any(result.relation is MemoryRelation.COEXISTING for result in conflicts):
                return CorrectionFlowResult(
                    status=CorrectionFlowStatus.COEXISTING,
                    reason="候选包含临时或一次性行为信号，不替代长期记忆",
                )
            return self._clarification(
                "候选包含临时或一次性行为信号，不能替代长期记忆",
                ("请确认这是长期变化，还是暂时尝试。",),
            )

        if candidate.intent not in self._proposal_intents:
            return self._no_action("当前意图不进入纠正 Proposal 流程")

        reviewable = tuple(
            result
            for result in conflicts
            if result.relation in self._proposal_relations
        )

        if len(reviewable) > 1:
            return self._clarification(
                "存在多个可能的旧记忆，不能自动选择目标",
                tuple(f"候选目标：{result.matched_memory_id}" for result in reviewable),
            )

        eligible = tuple(result for result in reviewable if self._allows_proposal(candidate.intent, result))
        if not eligible:
            if not memories:
                return self._no_action("没有相关的 active 记忆")
            return self._clarification(
                "找到了 active 记忆，但没有足够可靠的冲突证据定位目标",
                ("请明确要替代哪一条已有记忆。",),
            )

        conflict = eligible[0]
        try:
            current_memories = self.memory_service.list_memories(
                limit=self.memory_limit,
                memory_type=candidate.memory_type,
            )
            target = next(
                (memory for memory in current_memories if memory.id == conflict.matched_memory_id),
                None,
            )
        except Exception as exc:
            return self._failure("无法重新读取冲突目标", "target_memory_read_failed", exc)

        if target is None or target.updated_at != self._memory_updated_at(memories, conflict.matched_memory_id):
            return self._failure(
                "冲突目标在检测期间发生变化，未创建 Proposal",
                "target_changed_during_detection",
            )

        message_id = user_message.get("id")
        if user_message.get("role") != "user" or not message_id:
            return self._failure(
                "Proposal 来源必须是已经持久化的 user 消息",
                "invalid_user_message",
            )

        proposal = ProposalCreate(
            proposal_type=ProposalType.CORRECTION,
            target_memory_id=target.id,
            proposed_memory_type=candidate.memory_type,
            proposed_content=candidate.content,
            proposed_canonical_key=candidate.canonical_key,
            evidence_message_id=message_id,
            correction_intent=candidate.intent,
            relation=conflict.relation,
            confidence=conflict.confidence,
            evidence="；".join(conflict.evidence),
            idempotency_key=self._proposal_idempotency_key(message_id, candidate, target.id),
            expected_target_memory_updated_at=target.updated_at,
        )
        try:
            outcome = self.proposal_service.create_proposal_with_outcome(proposal, actor_type=MemoryActorType.SYSTEM_RULE)
        except ProposalTargetVersionConflictError as exc:
            return self._failure("目标记忆在 Proposal 创建时发生变化，未创建 Proposal", "target_changed_during_creation", exc)
        except ProposalTargetInactiveError as exc:
            return self._failure("目标记忆在 Proposal 创建时已不是 active，未创建 Proposal", "target_inactive_during_creation", exc)
        except Exception as exc:
            return self._failure("创建纠正 Proposal 失败", "proposal_creation_failed", exc)

        created = outcome.proposal
        summary = CorrectionProposalSummary(
            proposal_id=created.id,
            target_memory_id=target.id,
            relation=conflict.relation,
            confidence=conflict.confidence,
            evidence=conflict.evidence,
        )
        if not outcome.created:
            return self._replayed_result(created, summary)
        return CorrectionFlowResult(
            status=CorrectionFlowStatus.PROPOSAL_CREATED,
            reason="已创建待确认的纠正 Proposal，尚未修改正式记忆",
            proposal=summary,
        )

    @classmethod
    def _allows_proposal(cls, intent: CorrectionIntent, result) -> bool:
        if not result.evidence or result.recommended_action is not RecommendedAction.PROPOSE_CORRECTION:
            return False
        if result.relation is MemoryRelation.CONFLICT:
            return not result.needs_review
        return (
            intent is CorrectionIntent.LONG_TERM_CHANGE
            and result.same_attribute
            and result.explicit_replacement_signal
        )

    @staticmethod
    def _replayed_result(proposal, summary: CorrectionProposalSummary) -> CorrectionFlowResult:
        if proposal.status is ProposalStatus.PENDING:
            return CorrectionFlowResult(
                status=CorrectionFlowStatus.PROPOSAL_REUSED,
                reason="已复用历史 pending Proposal，未创建新的 Proposal",
                proposal=summary,
            )
        if proposal.status is ProposalStatus.ACCEPTED:
            status = (
                CorrectionFlowStatus.PROPOSAL_APPLIED
                if proposal.application_status is ProposalApplicationStatus.APPLIED
                else CorrectionFlowStatus.PROPOSAL_ACCEPTED
            )
            reason = "历史 Proposal 已 accepted，未重新创建 Proposal"
            if status is CorrectionFlowStatus.PROPOSAL_APPLIED:
                reason = "历史 Proposal 已 applied，未重新创建 Proposal"
            return CorrectionFlowResult(status=status, reason=reason, proposal=summary)
        if proposal.status is ProposalStatus.REJECTED:
            return CorrectionFlowResult(CorrectionFlowStatus.PROPOSAL_REJECTED, "历史 Proposal 已 rejected，未重新创建 Proposal", proposal=summary)
        if proposal.status is ProposalStatus.CANCELLED:
            return CorrectionFlowResult(CorrectionFlowStatus.PROPOSAL_CANCELLED, "历史 Proposal 已 cancelled，未重新创建 Proposal", proposal=summary)
        return CorrectionFlowResult(CorrectionFlowStatus.PROPOSAL_EXPIRED, "历史 Proposal 已 expired，未重新创建 Proposal", proposal=summary)

    @staticmethod
    def _memory_updated_at(memories, memory_id: str) -> str | None:
        for memory in memories:
            if memory.id == memory_id:
                return memory.updated_at
        return None

    @staticmethod
    def _proposal_idempotency_key(message_id: str, candidate: MemoryCandidate, target_memory_id: str) -> str:
        payload = "\x1f".join((message_id, candidate.canonical_key, candidate.content, target_memory_id))
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return f"correction-flow:{digest}"

    @staticmethod
    def _no_action(reason: str) -> CorrectionFlowResult:
        return CorrectionFlowResult(CorrectionFlowStatus.NO_ACTION, reason)

    @staticmethod
    def _clarification(reason: str, clarification: tuple[str, ...]) -> CorrectionFlowResult:
        return CorrectionFlowResult(CorrectionFlowStatus.CLARIFICATION_NEEDED, reason, clarification=clarification)

    @staticmethod
    def _failure(reason: str, error_code: str, exc: Exception | None = None) -> CorrectionFlowResult:
        # Keep exception text out of the structured result: database and source
        # details may contain private data. Callers can log the original error.
        _ = exc
        return CorrectionFlowResult(CorrectionFlowStatus.PROCESSING_FAILED, reason, error_code=error_code)
