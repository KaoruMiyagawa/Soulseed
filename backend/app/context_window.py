"""Deterministic, bounded model context assembly.

The default estimator is intentionally conservative and replaceable. It is not
the provider's tokenizer and therefore must be treated as an estimate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol


class TokenEstimator(Protocol):
    def count_messages(self, messages: list[dict[str, str]]) -> int: ...


class ConservativeTokenEstimator:
    """Estimate CJK at one token/character and Latin text at four chars/token."""

    safety_multiplier = 1.2
    message_overhead = 6

    def count_messages(self, messages: list[dict[str, str]]) -> int:
        raw = 0
        for message in messages:
            content = str(message.get("content") or "")
            cjk = 0
            other = 0
            for char in content:
                if char.isspace():
                    continue
                if ord(char) > 127:
                    cjk += 1
                else:
                    other += 1
            raw += cjk + math.ceil(other / 4) + self.message_overhead
        return math.ceil(raw * self.safety_multiplier)


@dataclass(frozen=True)
class ContextWindowLimits:
    max_input_tokens: int = 16_000
    max_persona_tokens: int = 3_500
    max_expression_tokens: int = 3_500
    max_clarification_tokens: int = 4_500
    max_system_control_tokens: int = 4_500
    max_current_message_tokens: int = 2_048
    max_history_tokens: int = 6_000

    def __post_init__(self) -> None:
        values = vars(self)
        if any(not isinstance(value, int) or value < 1 for value in values.values()):
            raise ValueError("上下文预算必须是正整数")


class ContextBudgetExceeded(ValueError):
    """Safe rejection before an over-budget request reaches a model adapter."""

    def __init__(self, bucket: str, estimated_tokens: int, limit: int, *, message_saved: bool = True):
        self.bucket = bucket
        self.estimated_tokens = estimated_tokens
        self.limit = limit
        self.message_saved = message_saved
        labels = {
            "persona": "人格提示",
            "expression": "表达证据",
            "clarification": "记忆澄清状态",
            "system_controls": "系统规则",
            "current_message": "当前消息",
            "required_context": "必需上下文",
        }
        label = labels.get(bucket, "模型上下文")
        persistence = (
            "本条消息尚未保存，请缩短内容后重试。"
            if not message_saved
            else "本条消息已保存在会话中，但未交给模型处理；为避免重复，请不要原样重发。"
        )
        super().__init__(
            f"{label}超过安全上下文预算（估算 {estimated_tokens}，上限 {limit}）；{persistence}"
        )


@dataclass(frozen=True)
class ContextWindowResult:
    messages: list[dict[str, str]]
    estimated_before: int
    estimated_after: int
    bucket_tokens: dict[str, int]
    dropped_turns: int


class ModelContextWindow:
    """Keep all system/current facts and trim only oldest complete chat turns."""

    EXPRESSION_MARKERS = (
        "<expression_context", "<life_context", "<memory_context", "<memory_reference",
    )
    CLARIFICATION_MARKERS = (
        "<memory_clarification_context", "<memory_clarification_instruction",
    )

    def __init__(
        self,
        limits: ContextWindowLimits | None = None,
        estimator: TokenEstimator | None = None,
    ):
        self.limits = limits or ContextWindowLimits()
        self.estimator = estimator or ConservativeTokenEstimator()

    def validate_current_message(self, content: str) -> int:
        estimated = self.estimator.count_messages([{"role": "user", "content": content}])
        if estimated > self.limits.max_current_message_tokens:
            raise ContextBudgetExceeded(
                "current_message", estimated, self.limits.max_current_message_tokens,
                message_saved=False,
            )
        return estimated

    def fit(self, messages: list[dict[str, str]]) -> ContextWindowResult:
        copied = [{"role": str(m.get("role", "")), "content": str(m.get("content", ""))}
                  for m in messages]
        systems = [m for m in copied if m["role"] == "system"]
        dialogue = [m for m in copied if m["role"] != "system"]
        groups = self._turns(dialogue)
        latest_user_index = next(
            (i for i in range(len(dialogue) - 1, -1, -1) if dialogue[i]["role"] == "user"),
            None,
        )
        current_group_index = None
        if latest_user_index is not None:
            for index, group in enumerate(groups):
                if any(message is dialogue[latest_user_index] for message in group):
                    current_group_index = index
                    break

        current_group = groups[current_group_index] if current_group_index is not None else []
        historical_groups = groups[:current_group_index] if current_group_index is not None else groups
        buckets = self._system_buckets(systems)
        bucket_tokens = {name: self.estimator.count_messages(value) for name, value in buckets.items()}
        for name, ceiling in (
            ("persona", self.limits.max_persona_tokens),
            ("expression", self.limits.max_expression_tokens),
            ("clarification", self.limits.max_clarification_tokens),
            ("system_controls", self.limits.max_system_control_tokens),
        ):
            if bucket_tokens[name] > ceiling:
                raise ContextBudgetExceeded(name, bucket_tokens[name], ceiling)

        current_tokens = self.estimator.count_messages(current_group)
        bucket_tokens["current_message"] = current_tokens
        if current_tokens > self.limits.max_current_message_tokens:
            raise ContextBudgetExceeded(
                "current_message", current_tokens, self.limits.max_current_message_tokens
            )

        pinned = systems + current_group
        pinned_tokens = self.estimator.count_messages(pinned)
        if pinned_tokens > self.limits.max_input_tokens:
            raise ContextBudgetExceeded(
                "required_context", pinned_tokens, self.limits.max_input_tokens
            )

        remaining = min(
            self.limits.max_history_tokens,
            self.limits.max_input_tokens - pinned_tokens,
        )
        selected: list[list[dict[str, str]]] = []
        used = 0
        for group in reversed(historical_groups):
            group_tokens = self.estimator.count_messages(group)
            if used + group_tokens > remaining:
                # Keep a contiguous recent suffix; never leave a chronology gap.
                break
            selected.append(group)
            used += group_tokens
        selected.reverse()
        kept_dialogue = [message for group in selected for message in group] + current_group
        kept_ids = {id(message) for message in kept_dialogue}
        result_messages = [
            message for message in copied
            if message["role"] == "system" or id(message) in kept_ids
        ]
        total = self.estimator.count_messages(result_messages)
        # Message-envelope overhead makes separate bucket estimates slightly
        # non-additive, so the hard request ceiling is checked on final output.
        while total > self.limits.max_input_tokens and selected:
            selected.pop(0)
            kept_dialogue = [message for group in selected for message in group] + current_group
            kept_ids = {id(message) for message in kept_dialogue}
            result_messages = [
                message for message in copied
                if message["role"] == "system" or id(message) in kept_ids
            ]
            total = self.estimator.count_messages(result_messages)
        if total > self.limits.max_input_tokens:
            raise ContextBudgetExceeded("required_context", total, self.limits.max_input_tokens)

        bucket_tokens["history"] = self.estimator.count_messages(
            [message for group in selected for message in group]
        )
        return ContextWindowResult(
            messages=result_messages,
            estimated_before=self.estimator.count_messages(copied),
            estimated_after=total,
            bucket_tokens=bucket_tokens,
            dropped_turns=len(historical_groups) - len(selected),
        )

    def _system_buckets(self, systems: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
        result = {key: [] for key in ("persona", "expression", "clarification", "system_controls")}
        first_system = True
        for message in systems:
            content = message["content"]
            if first_system:
                result["persona"].append(message)
                first_system = False
            elif any(marker in content for marker in self.EXPRESSION_MARKERS):
                result["expression"].append(message)
            elif any(marker in content for marker in self.CLARIFICATION_MARKERS):
                result["clarification"].append(message)
            else:
                result["system_controls"].append(message)
        return result

    @staticmethod
    def _turns(messages: list[dict[str, str]]) -> list[list[dict[str, str]]]:
        """Group each user message with following assistant/tool messages."""
        groups: list[list[dict[str, str]]] = []
        current: list[dict[str, str]] = []
        for message in messages:
            if message["role"] == "user" and current:
                groups.append(current)
                current = []
            current.append(message)
        if current:
            groups.append(current)
        return groups
