from __future__ import annotations

import logging
from datetime import UTC, datetime

from .clarification_repository import ClarificationRepository, ClarificationStatus
from .correction_service import CorrectionApplicationResult, MemoryCorrectionService, ProposalApplicationError
from .models import MemoryActorType, ProposalApplicationStatus, ProposalStatus, ProposalType
from .proposal_repositories import ProposalNotFoundError, ProposalStateError
from .proposal_service import MemoryProposalService
from .repositories import DuplicateMemoryError, IdempotencyConflictError, MemoryNotFoundError, MessageNotFoundError

logger = logging.getLogger(__name__)


class ClarificationApplicationService:
    """Safely accept/apply a Proposal derived from a persisted clarification answer."""

    _accept_reason = "用户在澄清对话中明确说明长期记忆变化"
    _apply_reason = "依据用户在澄清对话中的明确答复更新记忆"
    _cancel_source_reason = "后续澄清答复提供了新的正式纠正证据"
    _cancel_orphan_reason = "原始 Proposal 已终结，未执行新的自动应用"

    def __init__(
        self,
        clarifications: ClarificationRepository,
        proposals: MemoryProposalService,
        corrections: MemoryCorrectionService,
    ):
        self.clarifications = clarifications
        self.proposals = proposals
        self.corrections = corrections

    def apply_linked(self, clarification_id: str, processing_token: str) -> CorrectionApplicationResult:
        clarification = self.clarifications.get(clarification_id)
        if (
            clarification.status is not ClarificationStatus.PROCESSING
            or clarification.processing_token != processing_token
            or clarification.application_proposal_id is None
            or clarification.resolution not in ("explicit_change", "original_was_wrong")
        ):
            raise ProposalStateError("澄清没有可应用的已验证 Proposal")

        proposal_id = clarification.application_proposal_id
        proposal = self.proposals.get_proposal(proposal_id)
        if proposal.proposal_type is not ProposalType.CORRECTION:
            raise ProposalStateError("澄清只能应用 correction Proposal")
        if proposal.evidence_message_id != clarification.response_message_id:
            raise ProposalStateError("Proposal 来源与澄清答复不一致")

        source_id = clarification.source_proposal_id
        if source_id is not None and source_id != proposal_id:
            source = self.proposals.get_proposal(source_id)
            if source.status is ProposalStatus.PENDING:
                self.proposals.cancel_proposal(
                    source_id,
                    reason=self._cancel_source_reason,
                    actor_type=MemoryActorType.USER,
                    idempotency_key=f"clarification:{clarification_id}:cancel-source:{source_id}",
                )
            elif source.status is ProposalStatus.CANCELLED and source.resolution_reason == self._cancel_source_reason:
                # Crash recovery after source cancellation may safely continue this same operation.
                pass
            else:
                # A separately accepted or terminal source is never overwritten by this flow.
                self._cancel_orphan_proposal(clarification_id, proposal)
                raise ProposalStateError(f"原始 Proposal 已进入 {source.status.value}，不自动替换其决定")

        proposal = self.proposals.get_proposal(proposal_id)
        if proposal.application_status is not ProposalApplicationStatus.APPLIED:
            if proposal.status is ProposalStatus.PENDING:
                proposal = self.proposals.accept_proposal(
                    proposal_id,
                    reason=self._accept_reason,
                    actor_type=MemoryActorType.USER,
                    idempotency_key=f"clarification:{clarification_id}:accept:{proposal_id}",
                )
            elif proposal.status is not ProposalStatus.ACCEPTED:
                raise ProposalStateError(f"新 Proposal 已进入 {proposal.status.value}，不自动覆盖该状态")

        return self.corrections.apply_proposal(
            proposal_id,
            reason=self._apply_reason,
            actor_type=MemoryActorType.USER,
            idempotency_key=f"clarification:{clarification_id}:apply:{proposal_id}",
        )

    def cancel_unneeded_source(self, clarification_id: str, resolution: str) -> bool:
        """Cancel only a still-pending original inference; never reverse a terminal decision."""
        clarification = self.clarifications.get(clarification_id)
        source_id = clarification.source_proposal_id
        if source_id is None:
            return True
        source = self.proposals.get_proposal(source_id)
        if source.status is ProposalStatus.PENDING:
            reason = f"澄清结果为 {resolution}，原始纠正推断不再适用"
            self.proposals.cancel_proposal(
                source_id,
                reason=reason,
                actor_type=MemoryActorType.USER,
                idempotency_key=f"clarification:{clarification_id}:resolve-source:{resolution}:{source_id}",
            )
            return True
        if source.status is ProposalStatus.CANCELLED and source.resolution_reason == (
            f"澄清结果为 {resolution}，原始纠正推断不再适用"
        ):
            return True
        return source.status in (ProposalStatus.REJECTED, ProposalStatus.EXPIRED)

    def _cancel_orphan_proposal(self, clarification_id: str, proposal) -> None:
        if proposal.status is ProposalStatus.PENDING:
            self.proposals.cancel_proposal(
                proposal.id,
                reason=self._cancel_orphan_reason,
                actor_type=MemoryActorType.USER,
                idempotency_key=f"clarification:{clarification_id}:cancel-orphan:{proposal.id}",
            )

    def recover_after_restart(self) -> tuple[str, ...]:
        """Resume only expired, already-linked application work; never infer a new answer."""
        self.clarifications.reconcile_completed_applications()
        recovered: list[str] = []
        now = datetime.now(UTC)
        for record in self.clarifications.list_recoverable():
            recoverable_status = record.status is ClarificationStatus.FAILED and record.error_code == "application_interrupted"
            expired_processing = (
                record.status is ClarificationStatus.PROCESSING
                and record.lease_expires_at is not None
                and datetime.fromisoformat(record.lease_expires_at) <= now
            )
            if (
                not (recoverable_status or expired_processing)
                or record.application_proposal_id is None
                or record.response_message_id is None
            ):
                continue
            try:
                if record.status is ClarificationStatus.PROCESSING:
                    claim = self.clarifications.reclaim_application_after_restart(record.id)
                else:
                    claim = self.clarifications.claim_response(record.id, record.response_message_id)
                if not claim.acquired:
                    continue
                token = claim.clarification.processing_token
                if token is None:
                    continue
                try:
                    self.apply_linked(record.id, token)
                    self.clarifications.reconcile_applied(record.id)
                    recovered.append(record.id)
                except (
                    ProposalStateError,
                    ProposalApplicationError,
                    IdempotencyConflictError,
                    DuplicateMemoryError,
                    MemoryNotFoundError,
                    MessageNotFoundError,
                    ProposalNotFoundError,
                ):
                    logger.exception("澄清应用存在不可自动恢复的业务冲突 clarification_id=%s", record.id)
                    self.clarifications.finish_processing(
                        record.id, token, ClarificationStatus.FAILED, error_code="application_conflict"
                    )
                except Exception:
                    logger.exception("澄清应用遇到可重试中断 clarification_id=%s", record.id)
                    self.clarifications.finish_processing(
                        record.id, token, ClarificationStatus.FAILED, error_code="application_interrupted"
                    )
            except Exception:
                logger.exception("澄清应用恢复失败 clarification_id=%s", record.id)
        return tuple(recovered)
