from __future__ import annotations

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pulse.application.errors import ReminderDeliveryError, ReminderError
from pulse.application.services.reminders import (
    ReminderService,
    format_clock_time,
    parse_clock_time,
)
from pulse.infrastructure.database import Base
from pulse.infrastructure.reminders.dispatcher import ReminderDispatcher
from pulse.infrastructure.reminders.postgres import PostgresReminderStore

KARACHI = ZoneInfo("Asia/Karachi")
USER = "teams-user-1"
CONVERSATION = ("a:conversation-1", "https://smba.example/")


def local(day: int, hour: int, minute: int = 0) -> datetime:
    """A moment in Oct 2026 (the 16th is a Friday) expressed in UTC."""
    return datetime(2026, 10, day, hour, minute, tzinfo=KARACHI).astimezone(UTC)


class Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value

    def set(self, value: datetime) -> None:
        self.value = value


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail = False

    async def send_reminder(self, conversation_id: str, service_url: str) -> None:
        if self.fail:
            raise ReminderDeliveryError("down")
        self.sent.append((conversation_id, service_url))


@pytest.fixture
async def store(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'reminders.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield PostgresReminderStore(async_sessionmaker(engine, expire_on_commit=False))
    finally:
        await engine.dispose()


def build(store, *, start: datetime, max_failures: int = 3):
    clock = Clock(start)
    sender = FakeSender()
    service = ReminderService(
        store,
        sender,
        business_timezone="Asia/Karachi",
        default_time=time(18, 0),
        grace_minutes=120,
        snooze_minutes=60,
        max_send_failures=max_failures,
        now=clock.now,
    )
    return service, sender, clock


async def enroll(service: ReminderService, clock: Clock, *, at: datetime) -> None:
    """Enroll early on Friday so the first reminder is still ahead."""
    original = clock.value
    clock.set(at)
    await service.register_conversation(USER, *CONVERSATION)
    clock.set(original)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("18:00", time(18, 0)),
        ("6:30 pm", time(18, 30)),
        ("6pm", time(18, 0)),
        ("06:30PM", time(18, 30)),
        ("12 am", time(0, 0)),
        ("12:15 pm", time(12, 15)),
        ("7", time(7, 0)),
        ("9:05 a.m.", time(9, 5)),
    ],
)
def test_parse_clock_time(text: str, expected: time) -> None:
    assert parse_clock_time(text) == expected


@pytest.mark.parametrize("text", ["", "noon", "25:00", "6:75", "13pm", "0am", "6:5"])
def test_parse_clock_time_rejects_invalid_text(text: str) -> None:
    with pytest.raises(ReminderError):
        parse_clock_time(text)


def test_format_clock_time() -> None:
    assert format_clock_time(time(18, 0)) == "6:00 PM"
    assert format_clock_time(time(9, 5)) == "9:05 AM"


@pytest.mark.asyncio
async def test_first_message_enrolls_user_with_reminders_on_at_default_time(store) -> None:
    service, _, _ = build(store, start=local(16, 9))

    await service.register_conversation(USER, *CONVERSATION)

    subscription = await service.get_settings(USER)
    assert subscription.enabled is True
    assert subscription.reminder_time == time(18, 0)
    assert (subscription.conversation_id, subscription.service_url) == CONVERSATION


@pytest.mark.asyncio
async def test_new_conversation_details_are_refreshed_but_settings_are_kept(store) -> None:
    service, _, _ = build(store, start=local(16, 9))
    await service.register_conversation(USER, *CONVERSATION)
    await service.set_time(USER, "7:15 pm")

    await service.register_conversation(USER, "a:conversation-2", "https://other.example/")

    subscription = await service.get_settings(USER)
    assert subscription.conversation_id == "a:conversation-2"
    assert subscription.reminder_time == time(19, 15)


@pytest.mark.asyncio
async def test_reminder_is_sent_once_at_the_default_time(store) -> None:
    service, sender, clock = build(store, start=local(16, 9))
    await service.register_conversation(USER, *CONVERSATION)

    clock.set(local(16, 17, 59))
    assert await service.run_due() == 0
    clock.set(local(16, 18, 0))
    assert await service.run_due() == 1
    clock.set(local(16, 18, 1))
    assert await service.run_due() == 0

    assert sender.sent == [CONVERSATION]


@pytest.mark.asyncio
async def test_reminder_repeats_each_weekday_but_not_on_weekends(store) -> None:
    service, sender, clock = build(store, start=local(16, 9))
    await service.register_conversation(USER, *CONVERSATION)

    for day, expected in ((16, 1), (17, 0), (18, 0), (19, 1), (20, 1)):
        clock.set(local(day, 18, 30))
        assert await service.run_due() == expected, day
    assert len(sender.sent) == 3


@pytest.mark.asyncio
async def test_late_enrollment_does_not_trigger_an_instant_reminder(store) -> None:
    service, sender, clock = build(store, start=local(16, 18, 30))

    await service.register_conversation(USER, *CONVERSATION)

    assert await service.run_due() == 0
    clock.set(local(19, 18, 0))
    assert await service.run_due() == 1
    assert len(sender.sent) == 1


@pytest.mark.asyncio
async def test_missed_reminder_is_dropped_after_the_grace_window(store) -> None:
    service, sender, clock = build(store, start=local(16, 9))
    await service.register_conversation(USER, *CONVERSATION)

    clock.set(local(16, 21, 0))
    assert await service.run_due() == 0

    clock.set(local(16, 19, 59))
    assert await service.run_due() == 1
    assert len(sender.sent) == 1


@pytest.mark.asyncio
async def test_each_user_gets_their_own_time_and_chat(store) -> None:
    service, sender, clock = build(store, start=local(16, 9))
    await service.register_conversation("early", "a:early", "https://smba.example/")
    await service.register_conversation("late", "a:late", "https://smba.example/")
    await service.set_time("early", "5:00 pm")
    await service.set_time("late", "7:00 pm")

    clock.set(local(16, 17, 0))
    assert await service.run_due() == 1
    clock.set(local(16, 19, 0))
    assert await service.run_due() == 1

    assert [item[0] for item in sender.sent] == ["a:early", "a:late"]


@pytest.mark.asyncio
async def test_skip_today_suppresses_only_today(store) -> None:
    service, sender, clock = build(store, start=local(16, 9))
    await service.register_conversation(USER, *CONVERSATION)
    await service.skip_today(USER)

    clock.set(local(16, 18, 0))
    assert await service.run_due() == 0
    clock.set(local(19, 18, 0))
    assert await service.run_due() == 1
    assert len(sender.sent) == 1


@pytest.mark.asyncio
async def test_snooze_reminds_again_after_the_snooze_period(store) -> None:
    service, sender, clock = build(store, start=local(16, 9))
    await service.register_conversation(USER, *CONVERSATION)
    clock.set(local(16, 18, 0))
    await service.run_due()

    until = await service.snooze(USER)

    assert until.hour == 19 and until.minute == 0
    clock.set(local(16, 18, 30))
    assert await service.run_due() == 0
    clock.set(local(16, 19, 0))
    assert await service.run_due() == 1
    clock.set(local(16, 19, 5))
    assert await service.run_due() == 0
    assert len(sender.sent) == 2


@pytest.mark.asyncio
async def test_snooze_outlives_the_grace_window_within_the_same_day(store) -> None:
    service, sender, clock = build(store, start=local(16, 9))
    await service.register_conversation(USER, *CONVERSATION)
    clock.set(local(16, 23, 30))
    until = await service.snooze(USER)  # clamped to the end of the same local day

    assert (until.hour, until.minute) == (23, 59)
    clock.set(local(16, 23, 59))
    assert await service.run_due() == 1
    assert len(sender.sent) == 1


@pytest.mark.asyncio
async def test_undelivered_snooze_does_not_leak_into_the_next_working_day(store) -> None:
    service, sender, clock = build(store, start=local(16, 9))
    await service.register_conversation(USER, *CONVERSATION)
    clock.set(local(16, 23, 30))
    await service.snooze(USER)  # the process then stays down until Monday

    clock.set(local(19, 8, 0))
    assert await service.run_due() == 0
    clock.set(local(19, 18, 0))
    assert await service.run_due() == 1
    assert len(sender.sent) == 1


@pytest.mark.asyncio
async def test_snooze_late_at_night_is_rejected(store) -> None:
    service, _, clock = build(store, start=local(16, 9))
    await service.register_conversation(USER, *CONVERSATION)
    clock.set(datetime(2026, 10, 16, 23, 59, 30, tzinfo=KARACHI).astimezone(UTC))

    with pytest.raises(ReminderError, match="too late"):
        await service.snooze(USER)


@pytest.mark.asyncio
async def test_failed_delivery_is_retried_then_disabled_after_max_failures(store) -> None:
    service, sender, clock = build(store, start=local(16, 9), max_failures=2)
    await service.register_conversation(USER, *CONVERSATION)
    sender.fail = True

    clock.set(local(16, 18, 0))
    assert await service.run_due() == 0
    assert (await service.get_settings(USER)).failure_count == 1
    clock.set(local(16, 18, 1))
    assert await service.run_due() == 0
    assert (await service.get_settings(USER)).enabled is False

    sender.fail = False
    clock.set(local(16, 18, 2))
    assert await service.run_due() == 0
    assert sender.sent == []


@pytest.mark.asyncio
async def test_delivery_retry_succeeds_when_chat_recovers(store) -> None:
    service, sender, clock = build(store, start=local(16, 9))
    await service.register_conversation(USER, *CONVERSATION)
    sender.fail = True
    clock.set(local(16, 18, 0))
    await service.run_due()

    sender.fail = False
    clock.set(local(16, 18, 1))
    assert await service.run_due() == 1
    assert (await service.get_settings(USER)).failure_count == 0


@pytest.mark.asyncio
async def test_user_disabled_by_failures_resumes_when_they_message_again(store) -> None:
    service, sender, clock = build(store, start=local(16, 9), max_failures=1)
    await service.register_conversation(USER, *CONVERSATION)
    sender.fail = True
    clock.set(local(16, 18, 0))
    await service.run_due()
    assert (await service.get_settings(USER)).enabled is False

    # Same details are cached in memory, so a changed conversation forces the refresh.
    await service.register_conversation(USER, "a:conversation-9", "https://smba.example/")

    assert (await service.get_settings(USER)).enabled is True


@pytest.mark.asyncio
async def test_turn_off_with_login_is_immediate_and_turn_on_restores(store) -> None:
    service, sender, clock = build(store, start=local(16, 9))
    await service.register_conversation(USER, *CONVERSATION)

    assert await service.request_turn_off(USER, authenticated=True) == "off"
    clock.set(local(16, 18, 0))
    assert await service.run_due() == 0

    await service.turn_on(USER)
    clock.set(local(16, 18, 1))
    assert await service.run_due() == 1
    assert len(sender.sent) == 1


@pytest.mark.asyncio
async def test_turn_off_without_login_waits_until_the_user_signs_in(store) -> None:
    service, sender, clock = build(store, start=local(16, 9))
    await service.register_conversation(USER, *CONVERSATION)

    assert await service.request_turn_off(USER, authenticated=False) == "login_required"
    subscription = await service.get_settings(USER)
    assert subscription.enabled is True
    assert subscription.opt_out_pending is True
    clock.set(local(16, 18, 0))
    assert await service.run_due() == 1  # still on until login

    assert await service.complete_pending_opt_out(USER) is True
    subscription = await service.get_settings(USER)
    assert subscription.enabled is False
    assert subscription.opt_out_pending is False
    assert await service.complete_pending_opt_out(USER) is False


@pytest.mark.asyncio
async def test_login_without_a_pending_request_leaves_reminders_on(store) -> None:
    service, _, _ = build(store, start=local(16, 9))
    await service.register_conversation(USER, *CONVERSATION)

    assert await service.complete_pending_opt_out(USER) is False
    assert (await service.get_settings(USER)).enabled is True


@pytest.mark.asyncio
async def test_settings_require_an_enrolled_chat(store) -> None:
    service, _, _ = build(store, start=local(16, 9))

    for action in (
        service.get_settings(USER),
        service.set_time(USER, "6pm"),
        service.snooze(USER),
        service.skip_today(USER),
        service.turn_on(USER),
        service.request_turn_off(USER, authenticated=True),
    ):
        with pytest.raises(ReminderError, match="Send PULSE a message first"):
            await action
    assert await service.complete_pending_opt_out(USER) is False


@pytest.mark.asyncio
async def test_dispatcher_requires_binding_and_wraps_failures() -> None:
    dispatcher = ReminderDispatcher()
    with pytest.raises(ReminderDeliveryError, match="not connected"):
        await dispatcher.send_reminder("c", "u")

    async def broken(conversation_id: str, service_url: str) -> None:
        raise RuntimeError("secret provider detail")

    dispatcher.bind(broken)
    with pytest.raises(ReminderDeliveryError) as raised:
        await dispatcher.send_reminder("c", "u")
    assert "secret" not in str(raised.value)

    seen: list[tuple[str, str]] = []

    async def works(conversation_id: str, service_url: str) -> None:
        seen.append((conversation_id, service_url))

    dispatcher.bind(works)
    await dispatcher.send_reminder("c", "u")
    assert seen == [("c", "u")]
