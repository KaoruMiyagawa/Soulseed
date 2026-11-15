"""Per-attempt hooks used only by asynchronous chat jobs."""

from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class ChatAttempt:
    id: str
    character_id: str
    max_output_tokens: int
    sent: Callable[[], None]
    responded: Callable[[int, str | None], None]
    interrupted: Callable[[str], None]


chat_attempt_factory: ContextVar[Callable[[str, int | None], ChatAttempt] | None] = ContextVar(
    "chat_attempt_factory", default=None,
)
active_chat_attempt: ContextVar[ChatAttempt | None] = ContextVar("active_chat_attempt", default=None)
