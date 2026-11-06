"""Content-free, request-local chat timings. Durations are inclusive, not additive."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import logging
from pathlib import Path
from time import perf_counter
from uuid import uuid4


@dataclass
class ChatTrace:
    id: str = field(default_factory=lambda: uuid4().hex)
    started: float = field(default_factory=perf_counter)
    durations: dict[str, float] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    active: set[str] = field(default_factory=set)
    phase: str | None = None
    phase_start: float = field(default_factory=perf_counter)

    def checkpoint(self, name: str | None) -> None:
        now = perf_counter()
        if self.phase:
            self.durations[self.phase] = self.durations.get(self.phase, 0) + (now - self.phase_start) * 1000
        self.phase, self.phase_start = name, now

    def finish(self, status: int, path: str | None = None) -> dict:
        self.checkpoint(None)
        record = {"event": "chat_timing", "at": datetime.now(UTC).isoformat(),
                  "request_id": self.id, "status": status,
                  "total_ms": round((perf_counter() - self.started) * 1000, 3),
                  "durations_ms": {k: round(v, 3) for k, v in self.durations.items()},
                  "counts": self.counts}
        line = json.dumps(record, separators=(",", ":"))
        logging.getLogger(__name__).info("%s", line)
        if path:
            try:
                target = Path(path)
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                # Diagnostics must never turn a successful paid request into a retry.
                logging.getLogger(__name__).warning("chat_timing_write_failed")
        return record


current_trace: ContextVar[ChatTrace | None] = ContextVar("chat_trace", default=None)


def checkpoint(name: str) -> None:
    if trace := current_trace.get():
        trace.checkpoint(name)


def count(name: str, value: int = 1) -> None:
    if trace := current_trace.get():
        trace.counts[name] = trace.counts.get(name, 0) + value


@contextmanager
def measure(name: str):
    trace = current_trace.get()
    if trace is None or name in trace.active:
        yield
        return
    trace.active.add(name)
    start = perf_counter()
    try:
        yield
    finally:
        trace.durations[name] = trace.durations.get(name, 0) + (perf_counter() - start) * 1000
        trace.active.remove(name)
