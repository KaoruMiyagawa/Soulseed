"""Conservative in-process clock; each tick is persisted and safe to repeat."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from .life import LifeRepository, _iso
from .life_engine import LifeEngine

logger = logging.getLogger(__name__)


class LifeScheduler:
    def __init__(self, repository: LifeRepository, *, enabled: bool = True,
                 interval: timedelta = timedelta(minutes=30), character_ids: tuple[str, ...] = ("big_fish",), seed: int = 0):
        if interval < timedelta(minutes=5):
            raise ValueError("后台生活推进间隔不能短于 5 分钟")
        self.repository = repository
        self.enabled = enabled
        self.interval = interval
        self.character_ids = character_ids
        self.seed = seed

    def tick(self, *, now: datetime | None = None) -> list[dict]:
        if not self.enabled:
            return []
        current = now or datetime.now(UTC)
        at = _iso(current)
        changes = []
        for character_id in self.character_ids:
            self.repository.ensure_state(character_id, now=current)
            if self.repository.instance_owner is not None:
                self.repository.instance_owner.assert_owned()
            with self.repository.database.atomic() as db:
                row = db.execute("SELECT last_tick_at FROM life_scheduler_ticks WHERE character_id = ?", (character_id,)).fetchone()
                if row and current.astimezone(UTC) - datetime.fromisoformat(row[0]) < self.interval:
                    continue
                with self.repository.advance_lease(character_id) as lease_token:
                    if not lease_token:
                        continue
                    change = LifeEngine(self.repository, seed=self.seed)._advance_locked(character_id, now=current, allow_autonomous=True)
                    db.execute("INSERT INTO life_scheduler_ticks(character_id, last_tick_at) VALUES (?, ?) ON CONFLICT(character_id) DO UPDATE SET last_tick_at = excluded.last_tick_at", (character_id, at))
                    changes.extend(change)
        return changes

    async def run_forever(self, stop: asyncio.Event) -> None:
        if not self.enabled:
            return
        try:
            self.tick()
        except Exception:
            logger.exception("后台生活推进失败，将在下个周期重试")
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.interval.total_seconds())
            except TimeoutError:
                try:
                    self.tick()
                except Exception:
                    logger.exception("后台生活推进失败，将在下个周期重试")
