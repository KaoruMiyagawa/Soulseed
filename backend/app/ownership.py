"""Server-selected character scope. Client identifiers never establish authority."""

from contextlib import contextmanager
from contextvars import ContextVar


current_character: ContextVar[str] = ContextVar("soulseed_character", default="big_fish")


@contextmanager
def character_scope(character_id: str):
    token = current_character.set(character_id)
    try:
        yield
    finally:
        current_character.reset(token)
