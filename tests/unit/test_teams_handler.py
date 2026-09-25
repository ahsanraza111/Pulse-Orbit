import asyncio
from datetime import date
from types import SimpleNamespace

import pytest
from microsoft_teams.api import (
    AdaptiveCardActionCardResponse,
)
from microsoft_teams.cards import AdaptiveCard

from pulse.application.errors import OrbitAuthenticationError, ProviderError
from pulse.application.orbit_models import (
    CreatedTimesheetEntry,
    OrbitTimesheetEntry,
    ResolvedTimesheetQuery,
    TimesheetEntryPage,
)
from pulse.presentation.teams import (
    OrbitCancelCardHandler,
    OrbitConfirmCardHandler,
    OrbitLoginCardHandler,
    OrbitViewDetailCardHandler,
    OrbitViewPageCardHandler,
    TeamsMessageHandler,
    clean_teams_text,
    orbit_timesheet_list_card,
)


class FakeChatService:
    def __init__(self, response: str = "reply", error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.received = None

    async def reply(self, text: str) -> str:
        self.received = text
        if self.error:
            raise self.error
        return self.response


class FakeContext:
    def __init__(self, text: str | None) -> None:
        self.activity = SimpleNamespace(
            text=text, from_=SimpleNamespace(id="teams-user-1", aad_object_id=None)
        )
        self.sent: list[object] = []

    async def send(self, value: object) -> None:
        self.sent.append(value)


class FakeOrbitAuthService:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.received = None
        self.login_calls = 0

    async def login(self, teams_user_id: str, email: str, password: str) -> None:
        self.login_calls += 1
        self.received = (teams_user_id, email, password)
        if self.error:
            raise self.error


class FakeOrbitAddService:
    def __init__(self) -> None:
        self.received = None
        self.confirm_received = None
        self.cancel_received = None

    async def prepare(self, teams_user_id: str, request: str):
        self.received = (teams_user_id, request)
        return SimpleNamespace(
            id="pending-entry-1",
            entry_date=SimpleNamespace(isoformat=lambda: "2026-09-25"),
            project_name="ADGM Form Submissions",
            task_name="Backend Development",
            duration_minutes=240,
            description="API fix",
        )

    async def confirm(
        self, teams_user_id: str, expected_pending_id: str | None = None
    ) -> CreatedTimesheetEntry:
        self.confirm_received = (teams_user_id, expected_pending_id)
        return CreatedTimesheetEntry("orbit-entry-1", date(2026, 9, 25), 240)

    async def cancel(
        self, teams_user_id: str, expected_pending_id: str | None = None
    ) -> bool:
        self.cancel_received = (teams_user_id, expected_pending_id)
        return True


class FakeOrbitViewService:
    def __init__(self) -> None:
        self.list_text_received = None
        self.list_page_received = None
        self.detail_received = None
        self.entry = OrbitTimesheetEntry(
            id="3c160089-5f25-4bab-a722-5179c0e5d3ae",
            entry_date=date(2026, 9, 25),
            duration_minutes=90,
            description="Fixed validation",
            status="draft",
            project_name="ADGM Form Submissions",
            task_name="Backend Development",
        )
        self.query = ResolvedTimesheetQuery(
            start_date=date(2026, 9, 21),
            end_date=date(2026, 9, 27),
            page=0,
            page_size=5,
        )

    async def list_from_text(self, teams_user_id: str, text: str) -> TimesheetEntryPage:
        self.list_text_received = (teams_user_id, text)
        return TimesheetEntryPage(self.query, (self.entry,), 1)

    async def list_page(
        self, teams_user_id: str, query: ResolvedTimesheetQuery
    ) -> TimesheetEntryPage:
        self.list_page_received = (teams_user_id, query)
        return TimesheetEntryPage(query, (self.entry,), 6)

    async def get_detail(self, teams_user_id: str, entry_id: str) -> OrbitTimesheetEntry:
        self.detail_received = (teams_user_id, entry_id)
        return self.entry


class FakeCardContext:
    def __init__(self, data: dict[str, object], activity_id: str = "login-submit-1") -> None:
        self.activity = SimpleNamespace(
            id=activity_id,
            from_=SimpleNamespace(id="teams-user-1", aad_object_id="entra-user-1"),
            value=SimpleNamespace(action=SimpleNamespace(data=data)),
        )
        self.sent: list[object] = []

    async def send(self, value: object) -> None:
        self.sent.append(value)


def test_clean_teams_text_removes_mentions() -> None:
    assert clean_teams_text("<at>PULSE</at>  hello") == "hello"


@pytest.mark.asyncio
async def test_handler_replies_in_context() -> None:
    service = FakeChatService()
    context = FakeContext("<at>PULSE</at> hello")

    await TeamsMessageHandler(service)(context)

    assert service.received == "hello"
    assert context.sent == ["reply"]


@pytest.mark.asyncio
async def test_handler_returns_safe_provider_error() -> None:
    service = FakeChatService(error=ProviderError("secret provider detail"))
    context = FakeContext("hello")

    await TeamsMessageHandler(service)(context)

    assert context.sent == [
        "PULSE cannot reach the AI service right now. Please try again shortly."
    ]


@pytest.mark.asyncio
async def test_orbit_command_reports_missing_configuration() -> None:
    context = FakeContext("orbit login")

    await TeamsMessageHandler(FakeChatService())(context)

    assert context.sent == ["Orbit integration is not configured yet."]


@pytest.mark.asyncio
async def test_orbit_login_stays_in_teams_and_returns_masked_card() -> None:
    chat = FakeChatService()
    context = FakeContext("lets connect orbit via email and password")

    await TeamsMessageHandler(
        chat,
        FakeOrbitAuthService(),  # type: ignore[arg-type]
        FakeOrbitAddService(),  # type: ignore[arg-type]
    )(context)

    assert chat.received is None
    assert len(context.sent) == 1
    card = context.sent[0]
    assert isinstance(card, AdaptiveCard)
    payload = card.model_dump(by_alias=True, exclude_none=True)
    assert payload["actions"][0]["type"] == "Action.Execute"
    assert payload["actions"][0]["data"] == {"action": "orbit_login"}
    password_input = next(item for item in payload["body"] if item.get("id") == "orbit_password")
    assert password_input["style"] == "Password"


@pytest.mark.asyncio
async def test_plain_text_password_is_blocked_before_groq() -> None:
    chat = FakeChatService()
    context = FakeContext("Orbit password: exposed-secret")

    await TeamsMessageHandler(chat)(context)

    assert chat.received is None
    assert "do not send passwords" in str(context.sent[0])
    assert "exposed-secret" not in str(context.sent[0])


@pytest.mark.asyncio
async def test_natural_language_time_entry_routes_to_orbit_without_prefix() -> None:
    chat = FakeChatService()
    add = FakeOrbitAddService()
    context = FakeContext(
        "add an entry for today on ADGM forms task backend development "
        "notes API fix duration 4 hours"
    )

    await TeamsMessageHandler(
        chat,
        FakeOrbitAuthService(),  # type: ignore[arg-type]
        add,  # type: ignore[arg-type]
    )(context)

    assert chat.received is None
    assert add.received == ("teams-user-1", context.activity.text)
    assert isinstance(context.sent[0], AdaptiveCard)
    payload = context.sent[0].model_dump(by_alias=True, exclude_none=True)
    assert "Confirm Orbit time entry" in str(payload)
    assert [action["title"] for action in payload["actions"]] == [
        "Confirm entry",
        "Cancel",
    ]
    assert payload["actions"][0]["data"] == {
        "action": "orbit_confirm",
        "pending_id": "pending-entry-1",
    }
    assert payload["actions"][1]["data"] == {
        "action": "orbit_cancel",
        "pending_id": "pending-entry-1",
    }


@pytest.mark.asyncio
async def test_natural_language_view_request_returns_timesheet_card() -> None:
    chat = FakeChatService()
    view = FakeOrbitViewService()
    context = FakeContext("show my draft entries for this week")

    await TeamsMessageHandler(
        chat,
        FakeOrbitAuthService(),  # type: ignore[arg-type]
        FakeOrbitAddService(),  # type: ignore[arg-type]
        view,  # type: ignore[arg-type]
    )(context)

    assert chat.received is None
    assert view.list_text_received == ("teams-user-1", context.activity.text)
    payload = context.sent[0].model_dump(by_alias=True, exclude_none=True)
    assert "Orbit timesheet" in str(payload)
    assert "View details" in str(payload)
    assert "3c160089-5f25-4bab-a722-5179c0e5d3ae" in str(payload)


def test_timesheet_list_card_has_detail_and_next_actions() -> None:
    view = FakeOrbitViewService()
    second_entry = OrbitTimesheetEntry(
        id="4d270190-6f36-4cac-b833-6280d1f6e4bf",
        entry_date=date(2026, 9, 24),
        duration_minutes=30,
        description="Reviewed API",
        status="submitted",
        project_name="Internal Platform",
        task_name="Code Review",
    )
    page = TimesheetEntryPage(view.query, (view.entry, second_entry), 6)

    payload = orbit_timesheet_list_card(page).model_dump(
        by_alias=True, exclude_none=True
    )

    assert "Displayed total: 2h 00m" in str(payload)
    assert sum("View details" in str(item) for item in payload["body"]) == 2
    assert payload["actions"][0]["title"] == "Next"
    assert payload["actions"][0]["data"]["page"] == 1


def test_timesheet_list_card_handles_empty_results() -> None:
    view = FakeOrbitViewService()

    payload = orbit_timesheet_list_card(
        TimesheetEntryPage(view.query, (), 0)
    ).model_dump(by_alias=True, exclude_none=True)

    assert "No timesheet entries were found" in str(payload)
    assert "View details" not in str(payload)


@pytest.mark.asyncio
async def test_timesheet_page_button_loads_page_without_typed_message() -> None:
    view = FakeOrbitViewService()
    context = FakeCardContext(
        {
            "action": "orbit_view_page",
            "start_date": "2026-09-21",
            "end_date": "2026-09-27",
            "page": 1,
        },
        activity_id="page-action-1",
    )

    handler = OrbitViewPageCardHandler(view)  # type: ignore[arg-type]
    response = await handler(context)
    duplicate = await handler(context)
    await asyncio.sleep(0)

    assert isinstance(response, AdaptiveCardActionCardResponse)
    assert isinstance(duplicate, AdaptiveCardActionCardResponse)
    assert "Loading timesheet" in str(response.value)
    assert view.list_page_received is not None
    assert view.list_page_received[0] == "entra-user-1"
    assert view.list_page_received[1].page == 1
    assert len(context.sent) == 1
    assert "Orbit timesheet" in str(context.sent[0])


@pytest.mark.asyncio
async def test_timesheet_detail_button_loads_employee_scoped_detail() -> None:
    view = FakeOrbitViewService()
    context = FakeCardContext(
        {
            "action": "orbit_view_detail",
            "entry_id": view.entry.id,
            "start_date": "2026-09-21",
            "end_date": "2026-09-27",
            "page": 0,
        },
        activity_id="detail-action-1",
    )

    response = await OrbitViewDetailCardHandler(view)(context)  # type: ignore[arg-type]
    await asyncio.sleep(0)

    assert isinstance(response, AdaptiveCardActionCardResponse)
    assert "Loading Orbit entry" in str(response.value)
    assert view.detail_received == ("entra-user-1", view.entry.id)
    assert len(context.sent) == 1
    detail = context.sent[0].model_dump(by_alias=True, exclude_none=True)
    assert "Orbit entry details" in str(detail)
    assert "Back to list" in str(detail)


@pytest.mark.asyncio
async def test_confirmation_card_click_creates_entry_without_typed_message() -> None:
    add = FakeOrbitAddService()
    context = FakeCardContext(
        {"action": "orbit_confirm", "pending_id": "pending-entry-1"},
        activity_id="confirm-submit-1",
    )

    response = await OrbitConfirmCardHandler(add)(context)  # type: ignore[arg-type]
    await asyncio.sleep(0)

    assert isinstance(response, AdaptiveCardActionCardResponse)
    replacement = response.value.model_dump(by_alias=True, exclude_none=True)
    assert "Creating Orbit entry" in str(replacement)
    assert add.confirm_received == ("entra-user-1", "pending-entry-1")
    assert len(context.sent) == 1
    result = context.sent[0].model_dump(by_alias=True, exclude_none=True)
    assert "Orbit entry created" in str(result)
    assert "orbit-entry-1" in str(result)


@pytest.mark.asyncio
async def test_cancel_card_click_discards_entry_without_typed_message() -> None:
    add = FakeOrbitAddService()
    context = FakeCardContext(
        {"action": "orbit_cancel", "pending_id": "pending-entry-1"},
        activity_id="cancel-submit-1",
    )

    response = await OrbitCancelCardHandler(add)(context)  # type: ignore[arg-type]

    assert isinstance(response, AdaptiveCardActionCardResponse)
    replacement = response.value.model_dump(by_alias=True, exclude_none=True)
    assert "Orbit entry cancelled" in str(replacement)
    assert add.cancel_received == ("entra-user-1", "pending-entry-1")
    assert context.sent == []


@pytest.mark.asyncio
async def test_login_card_submission_authenticates_current_teams_user() -> None:
    auth = FakeOrbitAuthService()
    context = FakeCardContext(
        {"orbit_email": " employee@example.com ", "orbit_password": "secret-password"}
    )

    response = await OrbitLoginCardHandler(auth)(context)  # type: ignore[arg-type]
    await asyncio.sleep(0)

    assert auth.received == ("entra-user-1", "employee@example.com", "secret-password")
    assert isinstance(response, AdaptiveCardActionCardResponse)
    assert len(context.sent) == 1

    success_payload = context.sent[0].model_dump(by_alias=True, exclude_none=True)
    assert "connected successfully" in success_payload["text"]
    actions = success_payload["suggestedActions"]["actions"]
    assert len(actions) == 3
    assert all(action["type"] == "Action.Compose" for action in actions)
    assert all(
        action["value"]["type"] == "Teams.chatMessage" for action in actions
    )
    assert all(
        action["value"]["data"]["body"]["content"].startswith("orbit add")
        for action in actions
    )
    rendered = str(success_payload)
    assert "secret-password" not in rendered

    replacement = response.value.model_dump(by_alias=True, exclude_none=True)
    assert "sign-in complete" in str(replacement)


@pytest.mark.asyncio
async def test_duplicate_login_card_delivery_is_idempotent() -> None:
    auth = FakeOrbitAuthService()
    context = FakeCardContext(
        {"orbit_email": "employee@example.com", "orbit_password": "secret-password"}
    )
    handler = OrbitLoginCardHandler(auth)  # type: ignore[arg-type]

    first = await handler(context)
    second = await handler(context)
    await asyncio.sleep(0)

    assert isinstance(first, AdaptiveCardActionCardResponse)
    assert isinstance(second, AdaptiveCardActionCardResponse)
    assert auth.login_calls == 1
    assert len(context.sent) == 1


@pytest.mark.asyncio
async def test_login_card_replaces_itself_with_safe_retry_on_bad_credentials() -> None:
    auth = FakeOrbitAuthService(OrbitAuthenticationError("provider detail"))
    context = FakeCardContext(
        {"orbit_email": "employee@example.com", "orbit_password": "bad-secret"}
    )

    response = await OrbitLoginCardHandler(auth)(context)  # type: ignore[arg-type]

    assert isinstance(response, AdaptiveCardActionCardResponse)
    payload = response.value.model_dump(by_alias=True, exclude_none=True)
    rendered = str(payload)
    assert "Orbit rejected" in rendered
    assert "employee@example.com" in rendered
    assert "bad-secret" not in rendered
