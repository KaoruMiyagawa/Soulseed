"""A separate low-frequency clock for in-app contact decisions."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from .contact import ContactService

logger = logging.getLogger(__name__)


class ContactScheduler:
    def __init__(self, service: ContactService, *, interval: timedelta = timedelta(hours=1)):
        if interval < timedelta(minutes=30):
            raise ValueError("主动联系检查间隔不能短于 30 分钟")
        self.service = service
        self.interval = interval

    async def run_forever(self, stop: asyncio.Event) -> None:
        if not self.service.enabled:
            return
        while not stop.is_set():
            try:
                await self.service.tick()
            except Exception:
                logger.exception("站内联系决策失败，将在下个周期重试")
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.interval.total_seconds())
            except TimeoutError:
                pass
