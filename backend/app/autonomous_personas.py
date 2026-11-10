"""Server-owned persona catalog for autonomous drafts.

Character-specific text belongs in this catalog/source file. The intent and
expression engines consume a resolved profile and never derive one from a
candidate or conversation.
"""

from __future__ import annotations

import re

from .db import Database
from .ownership import current_character
from .persona import PERSONA_PROFILE_PATH, PersonaConfigurationError
from .proactive_expression import PersonaProfile, PersonaRegistry

_CONFIGURED_PERSONAS = {
    "big_fish": (PERSONA_PROFILE_PATH, ("lively", "curious", "reflective"), "soulseed-2.9.0-d2.1"),
}


def _trusted_description(path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PersonaConfigurationError("角色人格配置无法读取") from exc
    match = re.search(r"^## 核心气质\s*$([\s\S]*?)(?=^## |\Z)", text, re.MULTILINE)
    description = match.group(1).strip() if match else ""
    if not description or len(description) > 1200:
        raise PersonaConfigurationError("角色人格配置缺失或超出长度限制")
    return description


def resolve_trusted_persona(database: Database, character_id: str) -> PersonaProfile:
    """Resolve code-managed persona only for the currently authorized owned role."""
    if not character_id or current_character.get() != character_id:
        raise PersonaConfigurationError("角色作用域不匹配")
    config = _CONFIGURED_PERSONAS.get(character_id)
    if config is None:
        raise PersonaConfigurationError("该角色没有已审核的人格配置")
    path, styles, version = config
    with database.connection() as db:
        if db.in_transaction:
            raise PersonaConfigurationError("人格解析需要独立只读连接")
        previous = db.execute("PRAGMA query_only").fetchone()[0]
        db.execute("PRAGMA query_only=ON")
        try:
            db.execute("BEGIN")
            owned = db.execute("SELECT 1 FROM characters WHERE id=? AND ownership_status='owned'", (character_id,)).fetchone()
            if not owned:
                raise PersonaConfigurationError("角色未授权")
        finally:
            db.rollback()
            db.execute(f"PRAGMA query_only={int(previous)}")
    return PersonaProfile(character_id, _trusted_description(path), styles, version)


def trusted_persona_registry(database: Database, character_id: str) -> PersonaRegistry:
    """Build a one-role immutable registry from the server-owned catalog."""
    return PersonaRegistry((resolve_trusted_persona(database, character_id),))
