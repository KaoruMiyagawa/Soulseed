from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from .models import MemoryItem
from .repositories import MemorySource
from .service import MemoryService


@dataclass(frozen=True)
class MemoryMatch:
    memory: MemoryItem
    sources: tuple[MemorySource, ...]
    score: float
    matched_terms: tuple[str, ...]


class MemoryRetriever:
    """Small deterministic retriever; it scans bounded active-memory results in Python."""

    _stop_terms = {
        "我", "你", "他", "她", "它", "我们", "他们", "的", "了", "是", "在", "和", "与", "有", "想", "要", "吗", "呢", "啊", "请", "一下", "这个", "那个", "现在", "今天",
    }
    # These characters are useful for filtering low-information short fragments
    # at a term boundary. They are deliberately not a blanket character filter:
    # meaningful terms containing them can still be retained.
    _low_information_boundary_chars = {
        "我", "你", "他", "她", "它", "们", "的", "了", "是", "在", "和", "与", "有", "想", "要", "吗", "呢", "啊", "请", "这", "那", "更", "再", "都",
    }
    # A single generic conversational term should not be enough to retrieve a
    # memory. These remain available for diagnostics, but do not qualify a hit.
    _weak_cjk_terms = {"喜欢", "学习", "习惯", "怎样", "怎么", "记得", "什么", "是否"}

    def __init__(self, memory_service: MemoryService, *, max_candidates: int = 1000):
        self.memory_service = memory_service
        self.max_candidates = max_candidates

    def retrieve(self, query: str, *, limit: int = 5) -> list[MemoryMatch]:
        if not 1 <= limit <= 50:
            raise ValueError("limit 必须在 1 到 50 之间")
        terms = self.extract_terms(query)
        if not terms:
            return []
        memories = self.memory_service.list_memories(limit=self.max_candidates)
        matches = []
        for memory in memories:
            if self._is_expired(memory):
                continue
            score, matched = self.score_memory(query, terms, memory)
            if score <= 0:
                continue
            sources = tuple(self.memory_service.list_sources(memory.id))
            matches.append(MemoryMatch(memory, sources, score, tuple(matched)))
        matches.sort(key=lambda match: (-match.score, -match.memory.importance, -match.memory.confidence, match.memory.updated_at, match.memory.id))
        return matches[:limit]

    @classmethod
    def extract_terms(cls, text: str) -> tuple[str, ...]:
        normalized = (text or "").casefold().strip()
        if not normalized:
            return ()
        terms: set[str] = set()
        for token in re.findall(r"[a-z0-9][a-z0-9_+#.-]*|[\u3400-\u4dbf\u4e00-\u9fff]+", normalized):
            if re.fullmatch(r"[a-z0-9][a-z0-9_+#.-]*", token):
                if len(token) >= 2 and token not in cls._stop_terms:
                    terms.add(token)
                continue
            if token in cls._stop_terms:
                continue
            if len(token) <= 4:
                if cls._is_informative_fragment(token):
                    terms.add(token)
            else:
                for size in (2, 3):
                    terms.update(
                        fragment
                        for index in range(len(token) - size + 1)
                        if cls._is_informative_fragment(fragment := token[index:index + size])
                    )
        return tuple(sorted(terms))

    @classmethod
    def _is_informative_fragment(cls, fragment: str) -> bool:
        if not fragment or fragment in cls._stop_terms:
            return False
        # Short fragments such as “我更” and “你还” are common across many
        # unrelated Chinese sentences. Only reject those at a boundary; this
        # avoids deleting every meaningful word that happens to contain “我” or
        # “你” internally.
        return not (
            len(fragment) <= 2
            and (fragment[0] in cls._low_information_boundary_chars or fragment[-1] in cls._low_information_boundary_chars)
        )

    @classmethod
    def _is_strong_term(cls, term: str) -> bool:
        if re.fullmatch(r"[a-z0-9][a-z0-9_+#.-]*", term):
            return True
        return len(term) >= 3 or term not in cls._weak_cjk_terms

    @classmethod
    def score_memory(cls, query: str, terms: tuple[str, ...], memory: MemoryItem) -> tuple[float, list[str]]:
        searchable = f"{memory.content} {memory.canonical_key}".casefold()
        matched = [term for term in terms if term in searchable]
        if not matched:
            return 0.0, []
        strong_matched = [term for term in matched if cls._is_strong_term(term)]
        normalized_query = query.casefold().strip()
        phrase_bonus = 0.15 if normalized_query and normalized_query in memory.content.casefold() else 0.0
        if not strong_matched and not phrase_bonus:
            return 0.0, []
        coverage = len(strong_matched) / len(terms)
        quality_bonus = memory.importance * 0.1 + memory.confidence * 0.1
        return coverage * 0.65 + phrase_bonus + quality_bonus, strong_matched

    @staticmethod
    def _is_expired(memory: MemoryItem) -> bool:
        if not memory.expires_at:
            return False
        try:
            expires_at = datetime.fromisoformat(memory.expires_at.replace("Z", "+00:00"))
        except ValueError:
            return False
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        return expires_at <= datetime.now(UTC)

