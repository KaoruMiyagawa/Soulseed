from dataclasses import dataclass
from enum import StrEnum


class MemoryType(StrEnum):
    USER_PREFERENCE = "user_preference"
    RELATIONSHIP = "relationship"
    EVENT = "event"
    GOAL = "goal"
    GROWTH = "growth"


class MemoryStatus(StrEnum):
    ACTIVE = "active"
    INVALID = "invalid"
    SUPERSEDED = "superseded"
    DELETED = "deleted"
    EXPIRED = "expired"


class SourceRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


class EvidenceType(StrEnum):
    EXPLICIT_FACT = "explicit_fact"
    SUMMARY = "summary"
    INFERENCE = "inference"
    CORRECTION = "correction"


class CorrectionIntent(StrEnum):
    NONE = "none"
    EXPLICIT_CORRECTION = "explicit_correction"
    LONG_TERM_CHANGE = "long_term_change"
    TEMPORARY_EXPERIMENT = "temporary_experiment"
    AMBIGUOUS_CHANGE = "ambiguous_change"
    COEXISTING_INFORMATION = "coexisting_information"


class MemoryChangeAction(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    INVALIDATED = "invalidated"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"
    SOFT_DELETED = "soft_deleted"
    PERMANENTLY_DELETED = "permanently_deleted"


class MemoryActorType(StrEnum):
    USER = "user"
    SYSTEM_RULE = "system_rule"
    ADMIN = "admin"


class ProposalType(StrEnum):
    CORRECTION = "correction"
    UPDATE = "update"
    INVALIDATION = "invalidation"
    EXPIRATION = "expiration"


class ProposalStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class ProposalApplicationStatus(StrEnum):
    NOT_APPLIED = "not_applied"
    APPLIED = "applied"


class ProposalChangeAction(StrEnum):
    CREATED = "created"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


@dataclass(frozen=True)
class MemoryCreate:
    memory_type: MemoryType | str
    content: str
    canonical_key: str
    importance: float = 0.5
    confidence: float = 0.5
    expires_at: str | None = None


@dataclass(frozen=True)
class MemoryUpdate:
    content: str | None = None
    canonical_key: str | None = None
    importance: float | None = None
    confidence: float | None = None
    expires_at: str | None = None
    status: MemoryStatus | str | None = None


@dataclass(frozen=True)
class MemoryItem:
    id: str
    memory_type: MemoryType
    content: str
    canonical_key: str
    importance: float
    confidence: float
    status: MemoryStatus
    expires_at: str | None
    superseded_by_id: str | None
    created_at: str
    updated_at: str
    last_accessed_at: str | None


@dataclass(frozen=True)
class MemoryChangeLog:
    id: str
    memory_id: str | None
    action: MemoryChangeAction
    old_content: str | None
    new_content: str | None
    old_canonical_key: str | None
    new_canonical_key: str | None
    old_status: MemoryStatus | None
    new_status: MemoryStatus | None
    old_importance: float | None
    new_importance: float | None
    old_confidence: float | None
    new_confidence: float | None
    reason: str | None
    source_message_id: str | None
    actor_type: MemoryActorType
    idempotency_key: str | None
    request_fingerprint: str | None
    created_at: str


@dataclass(frozen=True)
class MemoryChangeProposal:
    id: str
    proposal_type: ProposalType
    target_memory_id: str | None
    proposed_memory_type: MemoryType
    proposed_content: str
    proposed_canonical_key: str
    evidence_message_id: str | None
    correction_intent: CorrectionIntent
    relation: str
    confidence: float
    evidence: str
    status: ProposalStatus
    idempotency_key: str | None
    request_fingerprint: str | None
    created_at: str
    resolved_at: str | None
    resolution_reason: str | None
    application_status: ProposalApplicationStatus = ProposalApplicationStatus.NOT_APPLIED
    target_memory_updated_at: str | None = None
    applied_at: str | None = None
    applied_memory_id: str | None = None
    replacement_memory_id: str | None = None


@dataclass(frozen=True)
class ProposalCreate:
    proposal_type: ProposalType | str
    target_memory_id: str | None
    proposed_memory_type: MemoryType | str
    proposed_content: str
    proposed_canonical_key: str
    evidence_message_id: str
    correction_intent: CorrectionIntent | str
    relation: str
    confidence: float
    evidence: str
    idempotency_key: str | None = None


@dataclass(frozen=True)
class ProposalChangeLog:
    id: str
    proposal_id: str
    action: ProposalChangeAction
    old_status: ProposalStatus | None
    new_status: ProposalStatus
    reason: str | None
    actor_type: MemoryActorType
    idempotency_key: str | None
    request_fingerprint: str | None
    created_at: str


def normalize_canonical_key(value: str) -> str:
    normalized = " ".join(value.strip().casefold().split())
    if not normalized:
        raise ValueError("canonical_key 不能为空")
    return normalized


def validate_score(value: float, field_name: str) -> float:
    numeric = float(value)
    if not 0 <= numeric <= 1:
        raise ValueError(f"{field_name} 必须在 0 到 1 之间")
    return numeric


def validate_content(value: str) -> str:
    content = value.strip()
    if not content:
        raise ValueError("记忆内容不能为空")
    return content

