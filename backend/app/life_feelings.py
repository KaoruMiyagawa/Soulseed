"""Conservative interpretation of an explicitly recorded activity feeling.

This is not a model of a character's internal state. Unclear, quoted, or
mixed reports must not silently alter durable activity preferences.
"""

from __future__ import annotations

import re
from typing import Literal

FeelingSignal = Literal["positive", "negative", "mixed", "unknown"]

GROWTH_POSITIVE_TERMS = ("开心", "喜欢", "愉快", "满足")
SHARE_POSITIVE_TERMS = ("开心", "喜欢", "有趣", "惊喜", "满足")
_NEGATIVE_TERMS = ("难过", "讨厌", "疲惫", "累")
_NEGATION = re.compile(r"并不|没有|不是|不怎么|不太|不算|谈不上|不|没|未")
_CONDITIONAL = ("如果", "假如", "要是", "可能", "也许", "大概", "说不定", "似乎", "好像", "希望", "但愿", "会不会", "是不是", "是否", "问我", "问你")
_THIRD_PARTY = re.compile(r"(?:我朋友|我同事|我家人|我同学|朋友|同事|家人|同学|别人|有人|他|她|他们|她们|你)(?:说|觉得|认为|表示|很|有点|特别|挺|不|是)")
_BOUNDARY = re.compile(r"[，,。！!；;：:、]|但是|不过|可是|但|却|然而|后来|而且")
_INTENSIFIERS = ("很", "太", "那么", "特别", "觉得", "感到", "真", "的", "非常", "有点", "十分", "挺")


def classify_feeling(
    feeling: str | None, *, positive_terms: tuple[str, ...] = GROWTH_POSITIVE_TERMS,
) -> FeelingSignal:
    """Classify only unambiguous, direct reports; unknown/mixed cause no growth."""
    text = "".join((feeling or "").split())
    if not text or any(c in text for c in '?？"“”‘’\'「」『』'):
        return "unknown"
    if any(marker in text for marker in _CONDITIONAL) or _THIRD_PARTY.search(text):
        return "unknown"
    if any(marker in text for marker in ("听说", "据说", "转述", "引用")):
        return "unknown"

    terms = sorted(set(positive_terms) | set(_NEGATIVE_TERMS), key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(term) for term in terms))
    signals: set[str] = set()
    uncertain = False
    for match in pattern.finditer(text):
        before = _BOUNDARY.split(text[:match.start()])[-1][-12:]
        negations = list(_NEGATION.finditer(before))
        negated = False
        if negations:
            final = negations[-1]
            between = before[final.end():]
            if len(negations) > 1:
                uncertain = True
                continue
            if len(between) > 6 or (between and not _split_intensifiers(between)):
                uncertain = True
                continue
            negated = True
        if match.group() in _NEGATIVE_TERMS:
            if negated:
                uncertain = True  # "不讨厌" is not an explicit positive preference.
            else:
                signals.add("negative")
        else:
            signals.add("negative" if negated else "positive")
    if uncertain:
        return "unknown"
    if len(signals) > 1:
        return "mixed"
    return next(iter(signals)) if signals else "unknown"


def _split_intensifiers(value: str) -> list[tuple[int, str]]:
    """Return chunks if every character is a recognized modifier; otherwise []."""
    result = []
    offset = 0
    while offset < len(value):
        word = next((w for w in _INTENSIFIERS if value.startswith(w, offset)), None)
        if word is None:
            return []
        result.append((offset, word))
        offset += len(word)
    return result
