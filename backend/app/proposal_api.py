from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .memory.models import MemoryActorType, MemoryChangeProposal, ProposalApplicationStatus, ProposalStatus
from .memory.correction_service import CorrectionApplicationResult, MemoryCorrectionService, ProposalApplicationError
from .memory.proposal_repositories import ProposalNotFoundError, ProposalStateError
from .memory.proposal_service import MemoryProposalService
from .memory.repositories import (
    DuplicateMemoryError,
    IdempotencyConflictError,
    MemoryNotFoundError,
    MessageNotFoundError,
)


EVIDENCE_PREVIEW_LIMIT = 500


class ProposalListItemResponse(BaseModel):
    id: str
    proposal_type: str
    status: ProposalStatus
    application_status: str
    proposed_memory_type: str
    proposed_content: str
    target_memory_id: str | None
    created_at: str
    resolved_at: str | None


class ProposalListResponse(BaseModel):
    items: list[ProposalListItemResponse]
    limit: int
    offset: int


class TargetMemoryResponse(BaseModel):
    exists: bool
    id: str | None = None
    content: str | None = None
    status: str | None = None
    updated_at: str | None = None


class SourceMessageResponse(BaseModel):
    exists: bool
    id: str | None = None
    role: str | None = None
    excerpt: str | None = None
    excerpt_truncated: bool = False


class ProposalDetailResponse(BaseModel):
    id: str
    proposal_type: str
    status: ProposalStatus
    application_status: str
    proposed_memory_type: str
    proposed_content: str
    target_memory_id: str | None
    target_memory: TargetMemoryResponse
    target_memory_updated_at: str | None
    evidence: str
    evidence_truncated: bool
    relation: str
    confidence: float
    evidence_message_id: str | None
    source_message: SourceMessageResponse
    created_at: str
    resolved_at: str | None
    applied_at: str | None
    applied_memory_id: str | None
    replacement_memory_id: str | None


class ProposalApplicationResponse(BaseModel):
    proposal_id: str
    status: ProposalStatus
    application_status: str
    applied_at: str | None
    applied_memory_id: str | None
    replacement_memory_id: str | None


class ProposalTransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=500)
    idempotency_key: str | None = Field(default=None, max_length=200)

    @field_validator("reason", "idempotency_key")
    @classmethod
    def strip_and_reject_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("字段不能只包含空白字符")
        return normalized


class ProposalTransitionResponse(BaseModel):
    proposal_id: str
    status: ProposalStatus
    application_status: ProposalApplicationStatus
    resolved_at: str | None
    resolution_reason: str | None


class ProposalApplyResponse(BaseModel):
    proposal_id: str
    status: ProposalStatus
    application_status: ProposalApplicationStatus
    applied_at: str | None
    applied_memory_id: str | None
    replacement_memory_id: str | None


def _preview(value: str, limit: int = EVIDENCE_PREVIEW_LIMIT) -> tuple[str, bool]:
    return value[:limit], len(value) > limit


def _list_item(proposal: MemoryChangeProposal) -> ProposalListItemResponse:
    return ProposalListItemResponse(
        id=proposal.id,
        proposal_type=proposal.proposal_type.value,
        status=proposal.status,
        application_status=proposal.application_status.value,
        proposed_memory_type=proposal.proposed_memory_type.value,
        proposed_content=proposal.proposed_content,
        target_memory_id=proposal.target_memory_id,
        created_at=proposal.created_at,
        resolved_at=proposal.resolved_at,
    )


def create_proposal_router(
    service_dependency: Callable[[], MemoryProposalService],
    correction_service_dependency: Callable[[], MemoryCorrectionService],
) -> APIRouter:
    router = APIRouter(prefix="/api/proposals", tags=["proposals"])

    @router.get("", response_model=ProposalListResponse)
    def list_proposals(
        status: ProposalStatus | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
        service: MemoryProposalService = Depends(service_dependency),
    ) -> ProposalListResponse:
        proposals = service.list_proposals(status=status, limit=limit, offset=offset)
        return ProposalListResponse(items=[_list_item(item) for item in proposals], limit=limit, offset=offset)

    @router.get("/{proposal_id}/application", response_model=ProposalApplicationResponse)
    def get_application_status(
        proposal_id: str,
        service: MemoryProposalService = Depends(service_dependency),
    ) -> ProposalApplicationResponse:
        try:
            proposal = service.get_proposal(proposal_id)
        except ProposalNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Proposal 不存在") from exc
        return ProposalApplicationResponse(
            proposal_id=proposal.id,
            status=proposal.status,
            application_status=proposal.application_status.value,
            applied_at=proposal.applied_at,
            applied_memory_id=proposal.applied_memory_id,
            replacement_memory_id=proposal.replacement_memory_id,
        )

    @router.get("/{proposal_id}", response_model=ProposalDetailResponse)
    def get_proposal(
        proposal_id: str,
        service: MemoryProposalService = Depends(service_dependency),
    ) -> ProposalDetailResponse:
        try:
            proposal, target, source = service.get_proposal_with_related_read_data(proposal_id)
        except ProposalNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Proposal 不存在") from exc

        evidence, evidence_truncated = _preview(proposal.evidence)
        source_content, source_truncated = _preview(source["content"]) if source is not None else (None, False)
        return ProposalDetailResponse(
            id=proposal.id,
            proposal_type=proposal.proposal_type.value,
            status=proposal.status,
            application_status=proposal.application_status.value,
            proposed_memory_type=proposal.proposed_memory_type.value,
            proposed_content=proposal.proposed_content,
            target_memory_id=proposal.target_memory_id,
            target_memory=TargetMemoryResponse(
                exists=target is not None,
                id=target["id"] if target is not None else None,
                content=target["content"] if target is not None else None,
                status=target["status"] if target is not None else None,
                updated_at=target["updated_at"] if target is not None else None,
            ),
            target_memory_updated_at=proposal.target_memory_updated_at,
            evidence=evidence,
            evidence_truncated=evidence_truncated,
            relation=proposal.relation,
            confidence=proposal.confidence,
            evidence_message_id=proposal.evidence_message_id,
            source_message=SourceMessageResponse(
                exists=source is not None,
                id=source["id"] if source is not None else None,
                role=source["role"] if source is not None else None,
                excerpt=source_content,
                excerpt_truncated=source_truncated,
            ),
            created_at=proposal.created_at,
            resolved_at=proposal.resolved_at,
            applied_at=proposal.applied_at,
            applied_memory_id=proposal.applied_memory_id,
            replacement_memory_id=proposal.replacement_memory_id,
        )

    def transition_proposal(
        proposal_id: str,
        request: ProposalTransitionRequest,
        *,
        accepted: bool,
        service: MemoryProposalService,
    ) -> ProposalTransitionResponse:
        try:
            if accepted:
                proposal = service.accept_proposal(
                    proposal_id,
                    reason=request.reason,
                    actor_type=MemoryActorType.USER,
                    idempotency_key=request.idempotency_key,
                )
            else:
                proposal = service.reject_proposal(
                    proposal_id,
                    reason=request.reason,
                    actor_type=MemoryActorType.USER,
                    idempotency_key=request.idempotency_key,
                )
        except ProposalNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Proposal 不存在") from exc
        except (ProposalStateError, IdempotencyConflictError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return ProposalTransitionResponse(
            proposal_id=proposal.id,
            status=proposal.status,
            application_status=proposal.application_status,
            resolved_at=proposal.resolved_at,
            resolution_reason=proposal.resolution_reason,
        )

    @router.post("/{proposal_id}/accept", response_model=ProposalTransitionResponse)
    def accept_proposal(
        proposal_id: str,
        request: ProposalTransitionRequest = ProposalTransitionRequest(),
        service: MemoryProposalService = Depends(service_dependency),
    ) -> ProposalTransitionResponse:
        return transition_proposal(proposal_id, request, accepted=True, service=service)

    @router.post("/{proposal_id}/reject", response_model=ProposalTransitionResponse)
    def reject_proposal(
        proposal_id: str,
        request: ProposalTransitionRequest = ProposalTransitionRequest(),
        service: MemoryProposalService = Depends(service_dependency),
    ) -> ProposalTransitionResponse:
        return transition_proposal(proposal_id, request, accepted=False, service=service)

    @router.post("/{proposal_id}/apply", response_model=ProposalApplyResponse)
    def apply_proposal(
        proposal_id: str,
        request: ProposalTransitionRequest = ProposalTransitionRequest(),
        correction_service: MemoryCorrectionService = Depends(correction_service_dependency),
    ) -> ProposalApplyResponse:
        try:
            result: CorrectionApplicationResult = correction_service.apply_proposal(
                proposal_id,
                reason=request.reason,
                actor_type=MemoryActorType.USER,
                idempotency_key=request.idempotency_key,
            )
        except ProposalNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Proposal 不存在") from exc
        except (
            ProposalStateError,
            ProposalApplicationError,
            IdempotencyConflictError,
            DuplicateMemoryError,
            MemoryNotFoundError,
            MessageNotFoundError,
        ) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return ProposalApplyResponse(
            proposal_id=result.proposal.id,
            status=result.proposal.status,
            application_status=result.proposal.application_status,
            applied_at=result.proposal.applied_at,
            applied_memory_id=result.proposal.applied_memory_id,
            replacement_memory_id=result.proposal.replacement_memory_id,
        )

    return router
