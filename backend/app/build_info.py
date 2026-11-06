"""Non-secret source identity for checking a running local service."""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4


_APP_DIR = Path(__file__).resolve().parent


def _source_build_id() -> str:
    digest = hashlib.sha256()
    for source in sorted(_APP_DIR.glob("*.py"), key=lambda item: item.name):
        digest.update(source.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(source.read_bytes())
        digest.update(b"\0")
    return "dev-" + digest.hexdigest()[:16]


# Computed once at import time so this value identifies the source loaded when
# the process started, not files edited on disk after startup.
BUILD_ID = _source_build_id()
STARTUP_ID = uuid4().hex[:16]
