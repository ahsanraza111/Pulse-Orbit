from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pulse.application.errors import ReminderDeliveryError, ReminderError
from pulse.application.ports.reminders import ReminderSender, ReminderStore
from pulse.application.reminder_models import ReminderSubscription, TurnOffOutcome

logger = logging.getLogger(__name__)

_CLOCK_PATTERN = re.compile(
    r"^\s*(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<meridiem>[ap]\.?m\.?)?\s*$",
    flags=re.IGNORECASE,
)
_NO_SUBSCRIPTION = "Send PULSE a message first so reminders can be set up for this chat."


def utc_now() -> datetime:
    return datetime.now(UTC)


def parse_clock_time(text: str) -> time:
    """Parse "18:00", "6:30 pm", "6pm" or "06:30PM" into a time of day."""
    match = _CLOCK_PATTERN.match(text)
    if not match:
        raise ReminderError("Enter a time like `6:00 PM` or `18:00`.")
    hour = int(match["hour"])
    minute = int(match["minute"] or 0)
    meridiem = (match["meridiem"] or "").lower().replace(".", "")
    if meridiem:
        if not 1 <= hour <= 12:
            raise ReminderError("Enter a time like `6:00 PM` or `18:00`.")
        hour = hour % 12 + (12 if meridiem == "pm" else 0)
    if hour > 23 or minute > 59:
        raise ReminderError("Enter a time like `6:00 PM` or `18:00`.")
    return time(hour, minute)


def format_clock_time(value: time) -> str:
    return value.strftime("%I:%M %p").lstrip("0")


class ReminderService:
    """Daily Monday-to-Friday timesheet reminders, delivered to each user's own chat."""

    def __init__(
        self,
        store: ReminderStore,
        sender: ReminderSender,
        *,
        business_timezone: str,
        default_time: time,
        grace_minutes: int,
        snooze_minutes: int,
        max_send_failures: int,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._store = store
        self._sender = sender
        self._default_time = default_time
        self._grace = timedelta(minutes=grace_minutes)
        self._snooze = timedelta(minutes=snooze_minutes)
        self._max_failures = max_send_failures
        self._now = now
        self._known_conversations: dict[str, tuple[str, str]] = {}
        try:
            self._timezone = ZoneInfo(business_timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown Orbit business timezone: {business_timezone}") from exc

    async def register_conversation(
        self, teams_user_id: str, conversation_id: str, service_url: str
    ) -> None:
        """Enroll a user (reminders on by default) or refresh where to reach them."""
        details = (conversation_id, service_url)
        if self._known_conversations.get(teams_user_id) == details:
            return
        now = self._now()
        local_now = now.astimezone(self._timezone)
        await self._store.upsert_conversation(
            teams_user_id,
            conversation_id,
            service_url,
            default_time=self._default_time,
            # Enrolling after today's default time must not trigger an instant reminder.
            initial_last_sent_on=(
                local_now.date() if local_now.time() >= self._default_time else None
            ),
            now=now,
        )
        self._known_conversations[teams_user_id] = details

    async def get_settings(self, teams_user_id: str) -> ReminderSubscription:
        return await self._require(teams_user_id)

    async def run_due(self) -> int:
        """Send every reminder that is due now; returns how many were delivered."""
        now = self._now()
        local_now = now.astimezone(self._timezone)
        if local_now.weekday() >= 5:
            return 0
        local_date = local_now.date()
        window_end = local_now.time().replace(second=0, microsecond=0)
        window_start_at = local_now - self._grace
        window_start = window_start_at.time() if window_start_at.date() == local_date else time.min
        day_start = datetime.combine(local_date, time.min, tzinfo=self._timezone)

        due = await self._store.list_due(
            local_date=local_date,
            window_start=window_start,
            window_end=window_end,
            day_start=day_start.astimezone(UTC),
            now=now,
        )
        delivered = 0
        for subscription in due:
            if not await self._store.claim(subscription.teams_user_id, local_date):
                continue
            try:
                await self._sender.send_reminder(
                    subscription.conversation_id, subscription.service_url
                )
            except ReminderDeliveryError:
                disabled = await self._store.record_failure(
                    subscription.teams_user_id, max_failures=self._max_failures
                )
                logger.warning(
                    "Timesheet reminder delivery failed%s",
                    "; reminders disabled for the user" if disabled else "",
                )
                continue
            await self._store.record_delivered(subscription.teams_user_id)
            delivered += 1
        return delivered

    async def set_time(self, teams_user_id: str, text: str) -> time:
        reminder_time = parse_clock_time(text)
        await self._require(teams_user_id)
        await self._store.set_time(teams_user_id, reminder_time)
        return reminder_time

    async def snooze(self, teams_user_id: str) -> datetime:
        """Remind again later today; never carries a reminder into another day."""
        await self._require(teams_user_id)
        now = self._now()
        local_now = now.astimezone(self._timezone)
        end_of_day = datetime.combine(
            local_now.date(), time(23, 59), tzinfo=self._timezone
        ).astimezone(UTC)
        until = min(now + self._snooze, end_of_day)
        if until <= now:
            raise ReminderError("It is too late to snooze today's reminder.")
        await self._store.snooze(teams_user_id, until)
        return until.astimezone(self._timezone)

    async def skip_today(self, teams_user_id: str) -> None:
        await self._require(teams_user_id)
        await self._store.skip_on(
            teams_user_id, self._now().astimezone(self._timezone).date()
        )

    async def turn_on(self, teams_user_id: str) -> None:
        await self._require(teams_user_id)
        await self._store.set_enabled(teams_user_id, True)

    async def request_turn_off(
        self, teams_user_id: str, *, authenticated: bool
    ) -> TurnOffOutcome:
        """Turning reminders off needs an Orbit login; remember the request until then."""
        await self._require(teams_user_id)
        if authenticated:
            await self._store.set_enabled(teams_user_id, False)
            return "off"
        await self._store.set_opt_out_pending(teams_user_id, True)
        return "login_required"

    async def complete_pending_opt_out(self, teams_user_id: str) -> bool:
        """Apply a turn-off requested before sign-in; True if reminders were turned off."""
        subscription = await self._store.get(teams_user_id)
        if subscription is None or not subscription.opt_out_pending:
            return False
        await self._store.set_enabled(teams_user_id, False)
        return True

    async def _require(self, teams_user_id: str) -> ReminderSubscription:
        subscription = await self._store.get(teams_user_id)
        if subscription is None:
            raise ReminderError(_NO_SUBSCRIPTION)
        return subscription
