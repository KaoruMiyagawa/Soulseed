from .models import MemoryActorType, MemoryChangeProposal, MemoryType, ProposalChangeLog, ProposalCreate, ProposalCreationResult, ProposalStatus
from .proposal_repositories import MemoryProposalRepository


class MemoryProposalService:
    def __init__(self, repository: MemoryProposalRepository):
        self.repository = repository

    def create_proposal(self, proposal: ProposalCreate, *, actor_type: MemoryActorType | str = MemoryActorType.SYSTEM_RULE) -> MemoryChangeProposal:
        return self.repository.create(proposal, actor_type=actor_type)

    def create_proposal_with_outcome(self, proposal: ProposalCreate, *, actor_type: MemoryActorType | str = MemoryActorType.SYSTEM_RULE) -> ProposalCreationResult:
        return self.repository.create_with_outcome(proposal, actor_type=actor_type)

    def get_proposal(self, proposal_id: str) -> MemoryChangeProposal:
        return self.repository.get(proposal_id)

    def get_proposal_with_related_read_data(self, proposal_id: str) -> tuple[MemoryChangeProposal, dict | None, dict | None]:
        return self.repository.get_with_related_read_data(proposal_id)

    def find_by_source_and_candidate(self, evidence_message_id: str, proposed_memory_type: MemoryType | str, proposed_content: str, proposed_canonical_key: str) -> list[MemoryChangeProposal]:
        return self.repository.find_by_source_and_candidate(evidence_message_id, proposed_memory_type, proposed_content, proposed_canonical_key)

    def list_proposals(self, *, status: ProposalStatus | str | None = None, limit: int = 50, offset: int = 0) -> list[MemoryChangeProposal]:
        return self.repository.list(status=status, limit=limit, offset=offset)

    def accept_proposal(self, proposal_id: str, *, reason: str | None = None, actor_type: MemoryActorType | str = MemoryActorType.USER, idempotency_key: str | None = None) -> MemoryChangeProposal:
        return self.repository.transition(proposal_id, ProposalStatus.ACCEPTED, reason=reason, actor_type=actor_type, idempotency_key=idempotency_key)

    def reject_proposal(self, proposal_id: str, *, reason: str | None = None, actor_type: MemoryActorType | str = MemoryActorType.USER, idempotency_key: str | None = None) -> MemoryChangeProposal:
        return self.repository.transition(proposal_id, ProposalStatus.REJECTED, reason=reason, actor_type=actor_type, idempotency_key=idempotency_key)

    def cancel_proposal(self, proposal_id: str, *, reason: str | None = None, actor_type: MemoryActorType | str = MemoryActorType.USER, idempotency_key: str | None = None) -> MemoryChangeProposal:
        return self.repository.transition(proposal_id, ProposalStatus.CANCELLED, reason=reason, actor_type=actor_type, idempotency_key=idempotency_key)

    def expire_proposal(self, proposal_id: str, *, reason: str | None = None, actor_type: MemoryActorType | str = MemoryActorType.SYSTEM_RULE, idempotency_key: str | None = None) -> MemoryChangeProposal:
        return self.repository.transition(proposal_id, ProposalStatus.EXPIRED, reason=reason, actor_type=actor_type, idempotency_key=idempotency_key)

    def list_changes(self, proposal_id: str, *, limit: int = 100, offset: int = 0) -> list[ProposalChangeLog]:
        return self.repository.list_changes(proposal_id, limit=limit, offset=offset)
