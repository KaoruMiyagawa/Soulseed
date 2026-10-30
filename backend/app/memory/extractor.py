import re
from dataclasses import dataclass

from .models import CorrectionIntent, EvidenceType, MemoryType, normalize_canonical_key


@dataclass(frozen=True)
class MemoryCandidate:
    memory_type: MemoryType
    content: str
    canonical_key: str
    importance: float
    confidence: float
    evidence_type: EvidenceType = EvidenceType.EXPLICIT_FACT
    excerpt: str | None = None
    expires_at: str | None = None
    intent: CorrectionIntent = CorrectionIntent.NONE


class DeterministicMemoryExtractor:
    """Conservative, explainable extraction for explicit user statements only."""

    _hypothetical_markers = ("如果", "假如", "要是", "万一", "会不会", "也许", "可能")
    _question_markers = ("？", "?")
    _third_party_prefixes = ("有人", "别人", "他人", "他说", "她说", "他们说", "有人说")
    _low_value_markers = ("今天有点累", "今天很累", "现在有点累", "今天很烦", "哈哈", "你好")
    _correction_markers = ("我之前说错了", "我说错了", "更正一下", "纠正一下", "不是，我", "不是我")

    def extract(self, user_message: dict) -> list[MemoryCandidate]:
        if user_message.get("role") != "user":
            return []
        return self.extract_text(user_message.get("content", ""))

    def extract_text(self, content: str) -> list[MemoryCandidate]:
        text = " ".join((content or "").strip().split())
        if not text or self._is_unsafe_to_infer(text):
            return []
        intent = self.classify_intent(text)
        candidates: list[MemoryCandidate] = []
        candidates.extend(self._extract_preference(text, intent))
        candidates.extend(self._extract_goal(text, intent))
        candidates.extend(self._extract_growth(text, intent))
        candidates.extend(self._extract_relationship(text, intent))
        candidates.extend(self._extract_event(text, intent))
        if not candidates and intent is not CorrectionIntent.NONE:
            # Keep the classified statement available to later correction or
            # proposal logic without pretending it is a durable preference.
            candidates.append(self._candidate(self._fallback_type(text), self._clean_content(text), 0.4, 0.5, intent))
        return self._deduplicate(candidates)

    @classmethod
    def classify_intent(cls, text: str) -> CorrectionIntent:
        if any(marker in text for marker in cls._correction_markers):
            return CorrectionIntent.EXPLICIT_CORRECTION
        if cls._contains_temporary_marker(text):
            return CorrectionIntent.TEMPORARY_EXPERIMENT
        if cls._contains_coexisting_marker(text):
            return CorrectionIntent.COEXISTING_INFORMATION
        if cls._contains_long_term_change_marker(text):
            return CorrectionIntent.LONG_TERM_CHANGE
        if cls._contains_ambiguous_marker(text):
            return CorrectionIntent.AMBIGUOUS_CHANGE
        return CorrectionIntent.NONE

    @staticmethod
    def _contains_temporary_marker(text: str) -> bool:
        return any(marker in text for marker in ("今天", "这周", "这星期", "这几天", "暂时", "试试", "尝试一下", "先试")) and any(marker in text for marker in ("想", "试", "换", "减少", "增加", "改"))

    @staticmethod
    def _contains_coexisting_marker(text: str) -> bool:
        return any(marker in text for marker in ("也喜欢", "也想", "同时", "又喜欢", "又想", "都喜欢", "都想"))

    @staticmethod
    def _contains_long_term_change_marker(text: str) -> bool:
        return any(marker in text for marker in ("现在更", "现在已经", "已经改成", "已经换成", "不再", "逐渐习惯", "长期改为", "从现在起"))

    @staticmethod
    def _contains_ambiguous_marker(text: str) -> bool:
        return any(marker in text for marker in ("好像", "有点想", "可能", "也许", "不太确定", "感觉最近"))

    def _extract_preference(self, text: str, intent: CorrectionIntent) -> list[MemoryCandidate]:
        markers = ("我喜欢", "我更喜欢", "我偏好", "我不喜欢", "我讨厌", "以后回答", "以后请")
        correction_preference = intent is CorrectionIntent.EXPLICIT_CORRECTION and bool(re.search(r"(?:其实|现在|改为|改成)[，,：: ]*(?:我)?(?:更喜欢|喜欢|偏好|不喜欢|讨厌)", text))
        if (not self._starts_with_any(text, markers) and not correction_preference) or not self._has_first_person_statement(text):
            return []
        return [self._candidate(MemoryType.USER_PREFERENCE, self._clean_content(text), 0.68, 0.91, intent)]

    def _extract_goal(self, text: str, intent: CorrectionIntent) -> list[MemoryCandidate]:
        markers = ("我的目标是", "我准备", "我打算", "我计划", "接下来我想", "以后我想", "我希望完成", "我希望能")
        statement = self._statement_after_correction(text, intent)
        if not self._starts_with_any(statement, markers) or not self._has_first_person_statement(statement):
            return []
        return [self._candidate(MemoryType.GOAL, self._clean_content(text), 0.76, 0.9, intent)]

    def _extract_growth(self, text: str, intent: CorrectionIntent) -> list[MemoryCandidate]:
        markers = ("我正在学习", "我在学习", "我坚持", "我一直在", "我想改善", "我在练习", "我开始养成", "我逐渐")
        statement = self._statement_after_correction(text, intent)
        if not self._starts_with_any(statement, markers) or not self._has_first_person_statement(statement):
            return []
        return [self._candidate(MemoryType.GROWTH, self._clean_content(text), 0.7, 0.86, intent)]

    def _extract_relationship(self, text: str, intent: CorrectionIntent) -> list[MemoryCandidate]:
        if not self._has_first_person_statement(text) or not re.search(r"(?:我的|我和)(?:家人|父母|妈妈|爸爸|兄弟|姐妹|哥哥|姐姐|弟弟|妹妹|朋友|同事|老师|伴侣|孩子)", text):
            return []
        return [self._candidate(MemoryType.RELATIONSHIP, self._clean_content(text), 0.7, 0.84, intent)]

    def _extract_event(self, text: str, intent: CorrectionIntent) -> list[MemoryCandidate]:
        event_markers = ("完成了", "参加了", "通过了", "毕业了", "入职了", "搬家了", "发布了", "上线了", "面试了", "启动了")
        valuable_context = ("项目", "考试", "比赛", "工作", "学校", "毕业", "mvp", "产品", "发布", "面试")
        if not text.startswith("我") or not any(marker in text for marker in event_markers) or not any(marker.casefold() in text.casefold() for marker in valuable_context):
            return []
        return [self._candidate(MemoryType.EVENT, self._clean_content(text), 0.66, 0.82, intent)]

    def _candidate(self, memory_type: MemoryType, content: str, importance: float, confidence: float, intent: CorrectionIntent) -> MemoryCandidate:
        evidence_type = EvidenceType.CORRECTION if intent is CorrectionIntent.EXPLICIT_CORRECTION else EvidenceType.EXPLICIT_FACT
        canonical_key = f"{memory_type.value}:{normalize_canonical_key(content)}"
        return MemoryCandidate(memory_type, content, canonical_key, importance, 0.95 if intent is CorrectionIntent.EXPLICIT_CORRECTION else confidence, evidence_type, content, None, intent)

    @staticmethod
    def _statement_after_correction(text: str, intent: CorrectionIntent) -> str:
        if intent is not CorrectionIntent.EXPLICIT_CORRECTION:
            return text
        parts = re.split(r"[，,：:；;]", text, maxsplit=1)
        return parts[1].strip() if len(parts) == 2 else text

    @staticmethod
    def _fallback_type(text: str) -> MemoryType:
        if any(marker in text for marker in ("健身", "学习", "练习", "习惯", "频率", "方法")):
            return MemoryType.GROWTH
        if any(marker in text for marker in ("目标", "计划", "准备", "打算")):
            return MemoryType.GOAL
        return MemoryType.USER_PREFERENCE

    def _is_unsafe_to_infer(self, text: str) -> bool:
        return (
            any(marker in text for marker in self._question_markers)
            or any(text.startswith(marker) for marker in self._hypothetical_markers)
            or any(text.startswith(marker) for marker in self._third_party_prefixes)
            or any(marker in text for marker in self._low_value_markers)
            or text.startswith(("有人喜欢", "别人喜欢", "他说", "她说"))
        )

    @staticmethod
    def _starts_with_any(text: str, markers: tuple[str, ...]) -> bool:
        return text.startswith(markers)

    @staticmethod
    def _has_first_person_statement(text: str) -> bool:
        return text.startswith(("我", "以后我", "接下来我", "我的", "以后请", "以后回答"))

    @staticmethod
    def _clean_content(text: str) -> str:
        return text.rstrip("。！？?! ")[:500]

    @staticmethod
    def _deduplicate(candidates: list[MemoryCandidate]) -> list[MemoryCandidate]:
        result: list[MemoryCandidate] = []
        seen: set[str] = set()
        for candidate in candidates:
            if candidate.canonical_key not in seen:
                seen.add(candidate.canonical_key)
                result.append(candidate)
        return result

