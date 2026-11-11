from __future__ import annotations

import asyncio
from datetime import UTC, datetime, time
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from pulse.application.errors import ReminderError
from pulse.application.reminder_models import ReminderSubscription
from pulse.core.config import Settings
from pulse.infrastructure.reminders.scheduler import ReminderScheduler
from pulse.presentation.http import create_http_app
from pulse.presentation.teams import (
    OrbitLoginCardHandler,
    OrbitReminderCardHandler,
    TeamsMessageHandler,
    orbit_reminder_card,
    orbit_reminder_settings_card,
    teams_reminder_delivery,
)


def dump(value: object) -> str:
    return str(value.model_dump(by_alias=True, exclude_none=True))  # type: ignore[attr-defined]


def subscription(**overrides: object) -> ReminderSubscription:
    values: dict[str, object] = {
        "teams_user_id": "entra-user-1",
        "conversation_id": "a:conversation-1",
        "service_url": "https://smba.example/",
        "enabled": True,
        "reminder_time": time(18, 0),
    }
    values.update(overrides)
    return ReminderSubscription(**values)  # type: ignore[arg-type]


class FakeReminderService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.error: Exception | None = None
        self.turn_off_outcome = "off"
        self.pending_opt_out = False
        self.sub = subscription()

    def _record(self, name: str, *args: object) -> None:
        self.calls.append((name, args))
        if self.error:
            raise self.error

    async def register_conversation(self, user: str, conversation_id: str, url: str) -> None:
        self._record("register", user, conversation_id, url)

    async def get_settings(self, user: str) -> ReminderSubscription:
        self._record("get_settings", user)
        return self.sub

    async def set_time(self, user: str, text: str) -> time:
        self._record("set_time", user, text)
        return time(19, 30)

    async def snooze(self, user: str) -> datetime:
        self._record("snooze", user)
        return datetime(2026, 10, 16, 19, 0, tzinfo=UTC)

    async def skip_today(self, user: str) -> None:
        self._record("skip_today", user)

    async def turn_on(self, user: str) -> None:
        self._record("turn_on", user)

    async def request_turn_off(self, user: str, *, authenticated: bool) -> str:
        self._record("request_turn_off", user, authenticated)
        return self.turn_off_outcome

    async def complete_pending_opt_out(self, user: str) -> bool:
        self._record("complete_pending_opt_out", user)
        return self.pending_opt_out


class FakeAuth:
    def __init__(self, authenticated: bool = True) -> None:
        self.authenticated = authenticated

    async def is_authenticated(self, user: str) -> bool:
        return self.authenticated

    async def login(self, user: str, email: str, password: str) -> None:
        self.logged_in = (user, email)


class FakeCardContext:
    def __init__(self, data: dict[str, object], activity_id: str = "card-1") -> None:
        self.activity = SimpleNamespace(
            id=activity_id,
            from_=SimpleNamespace(id="teams-user-1", aad_object_id="entra-user-1"),
            value=SimpleNamespace(action=SimpleNamespace(data=data)),
        )
        self.sent: list[object] = []

    async def send(self, value: object) -> None:
        self.sent.append(value)


class FakeMessageContext:
    def __init__(self, text: str, *, conversation_type: str = "personal", activity_id="m-1"):
        self.activity = SimpleNamespace(
            id=activity_id,
            text=text,
            from_=SimpleNamespace(id="teams-user-1", aad_object_id="entra-user-1"),
            conversation=SimpleNamespace(
                id="a:conversation-1", conversation_type=conversation_type
            ),
            service_url="https://smba.example/",
        )
        self.sent: list[object] = []

    async def send(self, value: object) -> None:
        self.sent.append(value)


class FakeChat:
    async def reply(self, text: str) -> str:
        return "chat reply"


def message_handler(reminders: FakeReminderService, *, authenticated: bool = True):
    return TeamsMessageHandler(
        FakeChat(),  # type: ignore[arg-type]
        FakeAuth(authenticated),  # type: ignore[arg-type]
        reminder_service=reminders,  # type: ignore[arg-type]
    )


def card_handler(reminders: FakeReminderService, *, authenticated: bool = True):
    return OrbitReminderCardHandler(reminders, FakeAuth(authenticated))  # type: ignore[arg-type]


def test_reminder_card_offers_add_snooze_skip_and_turn_off() -> None:
    text = dump(orbit_reminder_card())

    for verb in ("orbit_reminder_add", "orbit_reminder_snooze", "orbit_reminder_skip"):
        assert verb in text
    assert "orbit_reminder_off" in text
    assert "Have you filled in today's Orbit timesheet?" in text


def test_settings_card_shows_time_and_matching_toggle() -> None:
    on = dump(orbit_reminder_settings_card(subscription(reminder_time=time(18, 30))))
    off = dump(orbit_reminder_settings_card(subscription(enabled=False)))

    assert "6:30 PM" in on and "18:30" in on and "Monday to Friday" in on
    assert "orbit_reminder_off" in on and "orbit_reminder_on" not in on
    assert "orbit_reminder_on" in off and "orbit_reminder_off" not in off
    assert "orbit_reminder_set_time" in on


@pytest.mark.asyncio
async def test_every_personal_message_records_the_conversation() -> None:
    reminders = FakeReminderService()

    await message_handler(reminders)(FakeMessageContext("tell me a joke"))

    assert reminders.calls[0] == (
        "register",
        ("entra-user-1", "a:conversation-1", "https://smba.example/"),
    )


@pytest.mark.asyncio
async def test_group_chats_are_never_enrolled_for_reminders() -> None:
    reminders = FakeReminderService()
    context = FakeMessageContext("hello team", conversation_type="groupChat")

    await message_handler(reminders)(context)

    assert reminders.calls == []


@pytest.mark.asyncio
async def test_a_reminder_storage_failure_does_not_break_the_chat_reply() -> None:
    reminders = FakeReminderService()
    reminders.error = RuntimeError("database down")
    context = FakeMessageContext("tell me a joke")

    await message_handler(reminders)(context)

    assert context.sent
    assert "Something went wrong" not in "".join(str(item) for item in context.sent)


@pytest.mark.asyncio
async def test_orbit_reminders_command_shows_settings_card() -> None:
    reminders = FakeReminderService()
    context = FakeMessageContext("orbit reminders")

    await message_handler(reminders)(context)

    assert "Timesheet reminders" in dump(context.sent[0])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "expected_call"),
    [
        ("orbit reminders on", "turn_on"),
        ("orbit reminders time 7:30 pm", "set_time"),
    ],
)
async def test_reminder_text_commands(text: str, expected_call: str) -> None:
    reminders = FakeReminderService()
    context = FakeMessageContext(text)

    await message_handler(reminders)(context)

    assert expected_call in [name for name, _ in reminders.calls]
    assert isinstance(context.sent[0], str)


@pytest.mark.asyncio
async def test_reminder_time_command_reports_invalid_time_safely() -> None:
    reminders = FakeReminderService()

    async def bad(user: str, text: str) -> time:
        raise ReminderError("Enter a time like `6:00 PM` or `18:00`.")

    reminders.set_time = bad  # type: ignore[method-assign]
    context = FakeMessageContext("orbit reminders time noon")

    await message_handler(reminders)(context)

    assert context.sent == ["Enter a time like `6:00 PM` or `18:00`."]


@pytest.mark.asyncio
async def test_off_command_when_signed_in_turns_reminders_off() -> None:
    reminders = FakeReminderService()
    context = FakeMessageContext("orbit reminders off")

    await message_handler(reminders, authenticated=True)(context)

    assert ("request_turn_off", ("entra-user-1", True)) in reminders.calls
    assert "Reminders turned off" in dump(context.sent[0])


@pytest.mark.asyncio
async def test_off_command_when_signed_out_asks_for_login_first() -> None:
    reminders = FakeReminderService()
    reminders.turn_off_outcome = "login_required"
    context = FakeMessageContext("orbit reminders off")

    await message_handler(reminders, authenticated=False)(context)

    text = dump(context.sent[0])
    assert ("request_turn_off", ("entra-user-1", False)) in reminders.calls
    assert "orbit_password" in text
    assert "Sign in to Orbit to turn off reminders" in text


@pytest.mark.asyncio
async def test_snooze_and_skip_buttons_replace_the_card_with_confirmation() -> None:
    reminders = FakeReminderService()
    handler = card_handler(reminders)

    snoozed = await handler(FakeCardContext({"action": "orbit_reminder_snooze"}))
    skipped = await handler(FakeCardContext({"action": "orbit_reminder_skip"}))

    assert "7:00 PM" in dump(snoozed.value)
    assert "No more reminders today" in dump(skipped.value)
    assert [name for name, _ in reminders.calls] == ["snooze", "skip_today"]


@pytest.mark.asyncio
async def test_add_entry_button_posts_the_templates() -> None:
    reminders = FakeReminderService()
    context = FakeCardContext({"action": "orbit_reminder_add"})

    await card_handler(reminders)(context)

    assert len(context.sent) == 2
    assert "Time-entry templates" in dump(context.sent[0])


@pytest.mark.asyncio
async def test_turn_off_button_requires_login_when_signed_out() -> None:
    reminders = FakeReminderService()
    reminders.turn_off_outcome = "login_required"

    response = await card_handler(reminders, authenticated=False)(
        FakeCardContext({"action": "orbit_reminder_off"})
    )

    assert "orbit_password" in dump(response.value)
    assert "Sign in to Orbit to turn off reminders" in dump(response.value)


@pytest.mark.asyncio
async def test_set_time_button_passes_the_time_input() -> None:
    reminders = FakeReminderService()

    response = await card_handler(reminders)(
        FakeCardContext({"action": "orbit_reminder_set_time", "reminder_time": "19:30"})
    )

    assert ("set_time", ("entra-user-1", "19:30")) in reminders.calls
    assert "7:30 PM" in dump(response.value)


@pytest.mark.asyncio
async def test_card_errors_are_shown_without_internal_detail() -> None:
    reminders = FakeReminderService()
    reminders.error = RuntimeError("secret provider detail")
    handler = card_handler(reminders)

    unexpected = await handler(FakeCardContext({"action": "orbit_reminder_skip"}))
    reminders.error = ReminderError("It is too late to snooze today's reminder.")
    expected = await handler(FakeCardContext({"action": "orbit_reminder_snooze"}))
    unknown = await handler(FakeCardContext({"action": "other"}))

    assert "secret" not in dump(unexpected.value)
    assert "Something went wrong" in dump(unexpected.value)
    assert "too late" in dump(expected.value)
    assert "not valid" in dump(unknown.value)


@pytest.mark.asyncio
async def test_login_applies_a_pending_opt_out_and_confirms_it() -> None:
    reminders = FakeReminderService()
    reminders.pending_opt_out = True
    context = FakeCardContext({"orbit_email": "me@example.com", "orbit_password": "pw"})

    await OrbitLoginCardHandler(FakeAuth(False), reminders)(context)  # type: ignore[arg-type]
    await asyncio.sleep(0)

    assert ("complete_pending_opt_out", ("entra-user-1",)) in reminders.calls
    assert "Reminders turned off" in dump(context.sent[0])
    # The compose suggestions stay last so Teams keeps showing them.
    assert "suggestedActions" in dump(context.sent[-1])


@pytest.mark.asyncio
async def test_login_without_a_pending_opt_out_sends_no_reminder_message() -> None:
    reminders = FakeReminderService()
    context = FakeCardContext({"orbit_email": "me@example.com", "orbit_password": "pw"})

    await OrbitLoginCardHandler(FakeAuth(False), reminders)(context)  # type: ignore[arg-type]
    await asyncio.sleep(0)

    assert "Reminders turned off" not in "".join(dump(item) for item in context.sent)


@pytest.mark.asyncio
async def test_a_failing_opt_out_never_blocks_login() -> None:
    reminders = FakeReminderService()
    reminders.error = RuntimeError("database down")
    context = FakeCardContext({"orbit_email": "me@example.com", "orbit_password": "pw"})
    auth = FakeAuth(False)

    response = await OrbitLoginCardHandler(auth, reminders)(context)  # type: ignore[arg-type]
    await asyncio.sleep(0)

    assert auth.logged_in == ("entra-user-1", "me@example.com")
    assert "sign-in complete" in dump(response.value)


@pytest.mark.asyncio
async def test_delivery_sends_the_reminder_card_to_the_stored_chat() -> None:
    sent: list[tuple[str, object, str | None]] = []

    class FakeApp:
        async def send(self, conversation_id: str, activity: object, *, service_url=None):
            sent.append((conversation_id, activity, service_url))

    await teams_reminder_delivery(FakeApp())("a:conversation-1", "https://smba.example/")  # type: ignore[arg-type]

    assert sent[0][0] == "a:conversation-1"
    assert sent[0][2] == "https://smba.example/"
    assert "Timesheet reminder" in dump(sent[0][1])


def cron_settings(secret: str | None) -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        teams_client_id="client",
        teams_client_secret="secret",
        teams_tenant_id="tenant",
        groq_api_key="groq",
        reminder_cron_secret=secret,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_cron_endpoint_runs_the_check_with_the_secret(method: str) -> None:
    calls: list[int] = []

    async def tick() -> int:
        calls.append(1)
        return 4

    app = create_http_app(cron_settings("cron-secret"), None, tick)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.request(
            method,
            "/internal/reminders/tick",
            headers={"Authorization": "Bearer cron-secret"},
        )

    assert response.status_code == 200
    assert response.json() == {"delivered": 4}
    assert calls == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize("header", [None, "Bearer wrong", "cron-secret", "Bearer "])
async def test_cron_endpoint_rejects_missing_or_wrong_secret(header: str | None) -> None:
    async def tick() -> int:
        raise AssertionError("must not run")

    app = create_http_app(cron_settings("cron-secret"), None, tick)
    headers = {"Authorization": header} if header is not None else {}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/internal/reminders/tick", headers=headers)

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_cron_endpoint_is_absent_without_a_secret() -> None:
    async def tick() -> int:
        return 0

    app = create_http_app(cron_settings(None), None, tick)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/internal/reminders/tick")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_scheduler_keeps_running_after_a_failed_check() -> None:
    results: list[object] = [RuntimeError("boom"), 2, 0]
    ticks = 0

    async def tick() -> int:
        nonlocal ticks
        ticks += 1
        result = results.pop(0) if results else 0
        if isinstance(result, Exception):
            raise result
        return result  # type: ignore[return-value]

    task = asyncio.create_task(ReminderScheduler(tick, interval_seconds=0.01).run())
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert ticks >= 3

