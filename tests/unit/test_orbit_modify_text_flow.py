import asyncio
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from pulse.application.orbit_models import (
    EditedEntryValues,
    EntrySelectionRequest,
    OrbitTimesheetEntry,
    ParsedEntryChangeRequest,
    PendingEntryChange,
)
from pulse.presentation.teams import (
    OrbitEntrySelectCardHandler,
    TeamsMessageHandler,
    orbit_entry_selection_card,
)


class FakeChatService:
    def __init__(self) -> None:
        self.received: str | None = None

    async def reply(self, text: str) -> str:
        self.received = text
        return "chat reply"


class FakeAuthService:
    async def is_authenticated(self, teams_user_id: str) -> bool:
        return True


class FakeContext:
    def __init__(self, text: str, activity_id: str) -> None:
        self.activity = SimpleNamespace(
            id=activity_id,
            text=text,
            from_=SimpleNamespace(id="teams-user-1", aad_object_id=None),
        )
        self.sent: list[object] = []

    async def send(self, value: object) -> None:
        self.sent.append(value)


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


def entry(entry_id: str = "3c160089-5f25-4bab-a722-5179c0e5d3ae") -> OrbitTimesheetEntry:
    return OrbitTimesheetEntry(
        id=entry_id,
        entry_date=date(2026, 9, 25),
        duration_minutes=90,
        description="Fixed validation",
        status="draft",
        project_name="ADGM Form Submissions",
        task_name="Backend Development",
        project_id="project-1",
        task_id="task-1",
    )


def change(kind: str) -> PendingEntryChange:
    after = (
        EditedEntryValues(
            project_id="project-1",
            project_name="ADGM Form Submissions",
            task_id="task-1",
            task_name="Backend Development",
            entry_date=date(2026, 9, 25),
            duration_minutes=180,
            description="Fixed validation",
        )
        if kind == "update"
        else None
    )
    return PendingEntryChange(
        id="change-1",
        kind=kind,  # type: ignore[arg-type]
        teams_user_id="teams-user-1",
        employee_id="employee-1",
        organization_id="organization-1",
        before=entry(),
        after=after,
        expires_at=datetime(2026, 9, 25, 13, tzinfo=UTC),
    )


def selection(action: str = "edit") -> EntrySelectionRequest:
    return EntrySelectionRequest(
        id="selection-1",
        teams_user_id="teams-user-1",
        employee_id="employee-1",
        request=ParsedEntryChangeRequest(action=action, start_date=date(2026, 9, 25)),  # type: ignore[arg-type]
        options=(entry(), entry("8d0a4d6e-1b52-4f0e-9c52-1a2b3c4d5e6f")),
        expires_at=datetime(2026, 9, 25, 13, tzinfo=UTC),
    )


class FakeModifyService:
    def __init__(self, result: object) -> None:
        self.result = result
        self.prepared: list[tuple[str, str]] = []
        self.selected: list[tuple[str, str, str]] = []

    async def prepare_from_text(self, teams_user_id: str, text: str) -> object:
        self.prepared.append((teams_user_id, text))
        return self.result

    async def select_entry(self, teams_user_id: str, selection_id: str, entry_id: str):
        self.selected.append((teams_user_id, selection_id, entry_id))
        return change("delete")


def card_text(value: object) -> str:
    return str(value.model_dump(by_alias=True, exclude_none=True))  # type: ignore[attr-defined]


def handler_for(modify: FakeModifyService, chat: FakeChatService) -> TeamsMessageHandler:
    return TeamsMessageHandler(
        chat,  # type: ignore[arg-type]
        FakeAuthService(),  # type: ignore[arg-type]
        orbit_modify_service=modify,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "orbit edit yesterday's ADGM entry to 3 hours",
        "edit my entry from yesterday, make it 3 hours",
        "please update the ADGM timesheet entry notes",
        "delete today's code review entry",
        "kal wali entry hata do",
    ],
)
async def test_edit_and_delete_requests_are_routed_to_modify_service(text: str) -> None:
    chat = FakeChatService()
    modify = FakeModifyService(change("update"))
    context = FakeContext(text, activity_id=f"msg-{text}")

    await handler_for(modify, chat)(context)

    assert len(modify.prepared) == 1
    assert chat.received is None
    assert "Confirm Orbit entry update" in card_text(context.sent[0])


@pytest.mark.asyncio
async def test_delete_request_returns_delete_confirmation_not_a_deletion() -> None:
    modify = FakeModifyService(change("delete"))
    context = FakeContext("orbit delete yesterday's entry", activity_id="msg-delete")

    await handler_for(modify, FakeChatService())(context)

    text = card_text(context.sent[0])
    assert "Confirm Orbit entry deletion" in text
    assert "orbit_change_confirm" in text


@pytest.mark.asyncio
async def test_prefix_is_stripped_before_parsing() -> None:
    modify = FakeModifyService(change("update"))

    await handler_for(modify, FakeChatService())(
        FakeContext("orbit edit the 25th entry to 3 hours", activity_id="msg-prefix")
    )

    assert modify.prepared == [("teams-user-1", "edit the 25th entry to 3 hours")]


@pytest.mark.asyncio
async def test_ambiguous_match_returns_selection_card() -> None:
    modify = FakeModifyService(selection())
    context = FakeContext("edit my entry from the 25th", activity_id="msg-select")

    await handler_for(modify, FakeChatService())(context)

    text = card_text(context.sent[0])
    assert "Choose the entry to edit" in text
    assert text.count("orbit_entry_select") >= 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "add 2 hours to ADGM entry today",
        "tell me a joke",
        "change my password",
    ],
)
async def test_other_messages_do_not_trigger_entry_changes(text: str) -> None:
    modify = FakeModifyService(change("update"))

    await handler_for(modify, FakeChatService())(
        FakeContext(text, activity_id=f"msg-other-{text}")
    )

    assert modify.prepared == []


def test_selection_card_wording_follows_the_action() -> None:
    card = orbit_entry_selection_card(selection("delete"))

    assert "Choose the entry to delete" in card_text(card)


@pytest.mark.asyncio
async def test_selection_button_posts_confirmation_once() -> None:
    modify = FakeModifyService(None)
    context = FakeCardContext(
        {
            "action": "orbit_entry_select",
            "selection_id": "selection-1",
            "entry_id": "entry-2",
        },
        activity_id="select-1",
    )
    handler = OrbitEntrySelectCardHandler(modify)  # type: ignore[arg-type]

    response = await handler(context)
    await handler(context)
    await asyncio.sleep(0)

    assert "Preparing change" in card_text(response.value)
    assert modify.selected == [("entra-user-1", "selection-1", "entry-2")]
    assert len(context.sent) == 1
    assert "Confirm Orbit entry deletion" in card_text(context.sent[0])
