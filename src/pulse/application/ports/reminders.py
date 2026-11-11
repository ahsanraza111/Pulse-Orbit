from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date, datetime, time
from typing import Protocol

from pulse.application.reminder_models import ReminderSubscription

ReminderDelivery = Callable[[str, str], Awaitable[None]]
"""Delivers the reminder card for (conversation_id, service_url)."""


class ReminderStore(Protocol):
    async def upsert_conversation(
        self,
        teams_user_id: str,
        conversation_id: str,
        service_url: str,
        *,
        default_time: time,
        initial_last_sent_on: date | None,
        now: datetime,
    ) -> ReminderSubscription:
        """Create an enabled subscription, or refresh only the delivery details."""
        ...

    async def get(self, teams_user_id: str) -> ReminderSubscription | None: ...

    async def list_due(
        self,
        *,
        local_date: date,
        window_start: time,
        window_end: time,
        day_start: datetime,
        now: datetime,
    ) -> list[ReminderSubscription]:
        """Enabled subscriptions whose reminder is due and not yet sent today.

        A snooze that expired during the local day (between day_start and now) is due
        regardless of the window; otherwise reminder_time must be inside the window.
        """
        ...

    async def claim(self, teams_user_id: str, local_date: date) -> bool:
        """Atomically mark today's reminder as sent; False if it was already claimed."""
        ...

    async def record_delivered(self, teams_user_id: str) -> None: ...

    async def record_failure(self, teams_user_id: str, *, max_failures: int) -> bool:
        """Release today's claim and count a failure; True if the user was disabled."""
        ...

    async def set_time(self, teams_user_id: str, reminder_time: time) -> bool: ...

    async def set_enabled(self, teams_user_id: str, enabled: bool) -> bool: ...

    async def set_opt_out_pending(self, teams_user_id: str, pending: bool) -> bool: ...

    async def snooze(self, teams_user_id: str, until: datetime) -> bool: ...

    async def skip_on(self, teams_user_id: str, local_date: date) -> bool: ...


class ReminderSender(Protocol):
    async def send_reminder(self, conversation_id: str, service_url: str) -> None:
        """Deliver the reminder card; raise ReminderDeliveryError on failure."""
        ...
