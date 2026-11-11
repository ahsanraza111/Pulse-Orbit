from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Literal


@dataclass(frozen=True, slots=True)
class ReminderSubscription:
    """A Teams user's daily timesheet reminder and where to deliver it."""

    teams_user_id: str
    conversation_id: str
    service_url: str
    enabled: bool
    reminder_time: time
    last_sent_on: date | None = None
    snoozed_until: datetime | None = None
    skip_on: date | None = None
    opt_out_pending: bool = False
    failure_count: int = 0


TurnOffOutcome = Literal["off", "login_required"]
