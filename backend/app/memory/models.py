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

