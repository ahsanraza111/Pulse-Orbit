from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


class ReminderScheduler:
    """Runs the due-reminder check on a fixed interval until cancelled.

    On a serverless host this loop is not used; an external cron calls the same check
    through the protected HTTP endpoint instead.
    """

    def __init__(
        self,
        tick: Callable[[], Awaitable[int]],
        *,
        interval_seconds: float,
    ) -> None:
        self._tick = tick
        self._interval = interval_seconds

    async def run(self) -> None:
        while True:
            try:
                delivered = await self._tick()
                if delivered:
                    logger.info("Delivered %d timesheet reminder(s)", delivered)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Timesheet reminder check failed")
            await asyncio.sleep(self._interval)
