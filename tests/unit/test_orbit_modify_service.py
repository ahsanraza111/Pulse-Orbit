from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from pulse.application.errors import (
    OrbitAmbiguousMatchError,
    OrbitMutationUncertainError,
    OrbitNotFoundError,
    OrbitValidationError,
)
from pulse.application.orbit_models import (
    EntrySelectionRequest,
    OrbitEmployee,
    OrbitProject,
    OrbitSession,
    OrbitTask,
    OrbitTimesheetEntry,
    ParsedEntryChangeRequest,
    PendingEntryChange,
    TimesheetEntryBatch,
)
from pulse.application.services.orbit_modify import (
    OrbitModifyEntryService,
    parse_duration_minutes,
)
from pulse.infrastructure.orbit.memory import (
    InMemoryEntrySelectionStore,
    InMemoryPendingEntryChangeStore,
)

NOW = datetime(2026, 9, 25, 12, tzinfo=UTC)
ENTRY_ID = "3c160089-5f25-4bab-a722-5179c0e5d3ae"
USER = "teams-user-1"
OTHER_ID = "8d0a4d6e-1b52-4f0e-9c52-1a2b3c4d5e6f"


def draft_entry(**overrides: object) -> OrbitTimesheetEntry:
    entry = OrbitTimesheetEntry(
        id=ENTRY_ID,
        entry_date=date(2026, 9, 25),
        duration_minutes=90,
        description="Fixed validation",
        status="draft",
        project_name="ADGM Form Submissions",
        task_name="Backend Development",
        project_id="project-1",
        task_id="task-1",
    )
    return replace(entry, **overrides)


class FakeAuthService:
    async def get_authenticated_session(self, teams_user_id: str):
        assert teams_user_id == USER
        return OrbitSession("access", "refresh", NOW + timedelta(hours=1)), "auth-user-1"


class FakeTimesheetPort:
    def __init__(self) -> None:
        self.entry: OrbitTimesheetEntry | None = draft_entry()
        self.employee = OrbitEmployee("employee-1", "organization-1")
        self.projects = [
            OrbitProject("project-1", "ADGM Form Submissions"),
            OrbitProject("project-2", "Internal Platform"),
        ]
        self.tasks = {
            "project-1": [OrbitTask("task-1", "Backend Development")],
            "project-2": [
                OrbitTask("task-2", "Code Review"),
                OrbitTask("task-3", "Code Refactor"),
            ],
        }
        self.updates: list[PendingEntryChange] = []
        self.deletes: list[tuple[str, str]] = []
        self.mutation_error: Exception | None = None
        self.listed: list[OrbitTimesheetEntry] | None = None
        self.list_received: dict[str, object] | None = None

    async def get_employee(self, access_token: str, auth_user_id: str) -> OrbitEmployee:
        return self.employee

    async def list_assigned_projects(self, access_token: str, employee_id: str):
        return self.projects

    async def list_active_tasks(self, access_token: str, project_id: str):
        return self.tasks[project_id]

    async def list_entries(self, access_token: str, **kwargs):
        self.list_received = kwargs
        entries = self.listed if self.listed is not None else [self.entry]
        return TimesheetEntryBatch(entries=tuple(entries), total_count=len(entries))

    async def get_entry(self, access_token: str, *, employee_id: str, entry_id: str):
        assert employee_id == "employee-1"
        if self.listed is not None:
            return next((item for item in self.listed if item.id == entry_id), None)
        return self.entry

    async def update_entry(self, access_token: str, change: PendingEntryChange):
        if self.mutation_error:
            raise self.mutation_error
        self.updates.append(change)
        after = change.after
        assert after is not None
        return replace(
            change.before,
            entry_date=after.entry_date,
            duration_minutes=after.duration_minutes,
            description=after.description,
            project_id=after.project_id,
            project_name=after.project_name,
            task_id=after.task_id,
            task_name=after.task_name,
        )

    async def delete_entry(self, access_token: str, *, employee_id: str, entry_id: str) -> None:
        if self.mutation_error:
            raise self.mutation_error
        self.deletes.append((employee_id, entry_id))


class FakeChangeParser:
    def __init__(self, request: ParsedEntryChangeRequest | None = None) -> None:
        self.request = request
        self.received = None

    async def parse(self, text: str, *, today: date) -> ParsedEntryChangeRequest:
        assert self.request is not None
        self.received = (text, today)
        return self.request


def build(
    port: FakeTimesheetPort | None = None,
    now=lambda: NOW,
    parser: FakeChangeParser | None = None,
):
    port = port or FakeTimesheetPort()
    store = InMemoryPendingEntryChangeStore()
    service = OrbitModifyEntryService(
        FakeAuthService(),  # type: ignore[arg-type]
        port,  # type: ignore[arg-type]
        store,
        parser or FakeChangeParser(),
        InMemoryEntrySelectionStore(),
        confirmation_ttl_seconds=600,
        max_duration_minutes=1440,
        max_notes_chars=2000,
        business_timezone="Asia/Karachi",
        max_range_days=366,
        now=now,
    )
    return service, port, store


async def prepare(service: OrbitModifyEntryService, **overrides: str) -> PendingEntryChange:
    values = {
        "entry_date_text": "2026-09-25",
        "duration_text": "1h 30m",
        "description": "Fixed validation",
        "project_id": "project-1",
        "task_name": "Backend Development",
    }
    values.update(overrides)
    return await service.prepare_edit(USER, ENTRY_ID, **values)


def change_request(**overrides: object) -> ParsedEntryChangeRequest:
    values: dict[str, object] = {"action": "edit", "start_date": date(2026, 9, 25)}
    values.update(overrides)
    return ParsedEntryChangeRequest(**values)  # type: ignore[arg-type]


def two_drafts(port: FakeTimesheetPort) -> None:
    assert port.entry is not None
    port.listed = [port.entry, draft_entry(id=OTHER_ID, description="Other work")]


@pytest.mark.asyncio
async def test_text_edit_with_single_match_goes_straight_to_confirmation() -> None:
    parser = FakeChangeParser(change_request(new_duration_minutes=180))
    service, port, store = build(parser=parser)

    change = await service.prepare_from_text(USER, "make the 25th 3 hours")

    assert change.kind == "update"
    assert change.changed_fields == ("duration_minutes",)
    assert await store.get(USER) == change
    assert parser.received == ("make the 25th 3 hours", date(2026, 9, 25))
    assert port.list_received is not None
    assert port.list_received["status"] == "draft"
    assert port.list_received["employee_id"] == "employee-1"


@pytest.mark.asyncio
async def test_text_edit_without_dates_searches_the_current_week_only() -> None:
    parser = FakeChangeParser(
        change_request(start_date=None, project_name="adgm", new_description="API fix")
    )
    service, port, _ = build(parser=parser)

    await service.prepare_from_text(USER, "update adgm notes")

    assert port.list_received is not None
    assert port.list_received["start_date"] == date(2026, 9, 21)
    assert port.list_received["end_date"] == date(2026, 9, 27)
    assert port.list_received["project_id"] == "project-1"


@pytest.mark.asyncio
async def test_text_delete_prepares_confirmation_without_deleting() -> None:
    parser = FakeChangeParser(change_request(action="delete"))
    service, port, _ = build(parser=parser)

    change = await service.prepare_from_text(USER, "delete the 25th entry")

    assert change.kind == "delete"
    assert change.after is None
    assert port.deletes == []


@pytest.mark.asyncio
async def test_text_change_with_several_matches_asks_user_to_choose() -> None:
    parser = FakeChangeParser(change_request(new_duration_minutes=60))
    service, port, store = build(parser=parser)
    two_drafts(port)

    selection = await service.prepare_from_text(USER, "make the 25th 1 hour")

    assert isinstance(selection, EntrySelectionRequest)
    assert [option.id for option in selection.options] == [ENTRY_ID, OTHER_ID]
    assert await store.get(USER) is None

    change = await service.select_entry(USER, selection.id, OTHER_ID)

    assert change.before.id == OTHER_ID
    assert change.changed_fields == ("duration_minutes",)
    with pytest.raises(OrbitNotFoundError, match="no longer current"):
        await service.select_entry(USER, selection.id, OTHER_ID)


@pytest.mark.asyncio
async def test_select_entry_rejects_entry_outside_the_matches() -> None:
    parser = FakeChangeParser(change_request(new_duration_minutes=60))
    service, port, _ = build(parser=parser)
    two_drafts(port)
    selection = await service.prepare_from_text(USER, "make the 25th 1 hour")
    assert isinstance(selection, EntrySelectionRequest)

    with pytest.raises(OrbitValidationError, match="not one of the matches"):
        await service.select_entry(USER, selection.id, "11111111-1111-4111-8111-111111111111")


@pytest.mark.asyncio
async def test_select_entry_rejects_expired_selection() -> None:
    clock = {"now": NOW}
    parser = FakeChangeParser(change_request(new_duration_minutes=60))
    service, port, _ = build(now=lambda: clock["now"], parser=parser)
    two_drafts(port)
    selection = await service.prepare_from_text(USER, "make the 25th 1 hour")
    assert isinstance(selection, EntrySelectionRequest)
    clock["now"] = NOW + timedelta(seconds=601)

    with pytest.raises(OrbitValidationError, match="expired"):
        await service.select_entry(USER, selection.id, OTHER_ID)


@pytest.mark.asyncio
async def test_new_change_invalidates_an_open_selection() -> None:
    parser = FakeChangeParser(change_request(new_duration_minutes=60))
    service, port, _ = build(parser=parser)
    two_drafts(port)
    selection = await service.prepare_from_text(USER, "make the 25th 1 hour")
    assert isinstance(selection, EntrySelectionRequest)

    await service.prepare_delete(USER, ENTRY_ID)

    with pytest.raises(OrbitNotFoundError, match="no longer current"):
        await service.select_entry(USER, selection.id, OTHER_ID)


@pytest.mark.asyncio
async def test_text_change_filters_by_task_and_notes() -> None:
    parser = FakeChangeParser(
        change_request(task_name="backend", notes_hint="validation", new_duration_minutes=60)
    )
    service, port, _ = build(parser=parser)
    assert port.entry is not None
    port.listed = [port.entry, draft_entry(id=OTHER_ID, task_name="Code Review")]

    change = await service.prepare_from_text(USER, "x")

    assert change.before.id == ENTRY_ID


@pytest.mark.asyncio
async def test_text_change_with_no_match_is_not_found() -> None:
    parser = FakeChangeParser(change_request(notes_hint="nonexistent", new_duration_minutes=60))
    service, _, _ = build(parser=parser)

    with pytest.raises(OrbitNotFoundError, match="No draft entries"):
        await service.prepare_from_text(USER, "x")


@pytest.mark.asyncio
async def test_text_change_with_too_many_matches_asks_to_narrow() -> None:
    parser = FakeChangeParser(change_request(new_duration_minutes=60))
    service, port, _ = build(parser=parser)
    port.listed = [draft_entry(id=f"00000000-0000-4000-8000-{index:012d}") for index in range(9)]

    with pytest.raises(OrbitValidationError, match="Narrow it down"):
        await service.prepare_from_text(USER, "x")


@pytest.mark.asyncio
async def test_text_change_requires_selector_and_change() -> None:
    no_selector = FakeChangeParser(change_request(start_date=None, new_duration_minutes=60))
    service, _, _ = build(parser=no_selector)
    with pytest.raises(OrbitValidationError, match="which entry"):
        await service.prepare_from_text(USER, "x")

    service, _, _ = build(parser=FakeChangeParser(change_request()))
    with pytest.raises(OrbitValidationError, match="what to change"):
        await service.prepare_from_text(USER, "x")


@pytest.mark.asyncio
async def test_text_project_change_requires_task_and_resolves_it() -> None:
    without_task = FakeChangeParser(change_request(new_project_name="internal"))
    service, _, _ = build(parser=without_task)
    with pytest.raises(OrbitValidationError, match="also name the task"):
        await service.prepare_from_text(USER, "x")

    with_task = FakeChangeParser(
        change_request(new_project_name="internal", new_task_name="code review")
    )
    service, _, _ = build(parser=with_task)
    change = await service.prepare_from_text(USER, "x")
    assert isinstance(change, PendingEntryChange)
    assert change.after is not None
    assert (change.after.project_id, change.after.task_id) == ("project-2", "task-2")


@pytest.mark.asyncio
async def test_text_edit_that_changes_nothing_is_rejected() -> None:
    parser = FakeChangeParser(change_request(new_duration_minutes=90))
    service, _, _ = build(parser=parser)

    with pytest.raises(OrbitValidationError, match="No changes"):
        await service.prepare_from_text(USER, "x")


@pytest.mark.parametrize(
    ("text", "minutes"),
    [
        ("90m", 90),
        ("1h 30m", 90),
        ("1h30m", 90),
        ("1.5h", 90),
        ("2 hours", 120),
        ("45 minutes", 45),
        ("1:30", 90),
    ],
)
def test_parse_duration_minutes(text: str, minutes: int) -> None:
    assert parse_duration_minutes(text) == minutes


@pytest.mark.parametrize("text", ["", "90", "abc", "1h abc", "-1h"])
def test_parse_duration_rejects_ambiguous_text(text: str) -> None:
    with pytest.raises(OrbitValidationError):
        parse_duration_minutes(text)


@pytest.mark.asyncio
async def test_start_edit_returns_entry_and_assigned_projects() -> None:
    service, port, _ = build()

    context = await service.start_edit(USER, ENTRY_ID)

    assert context.entry == port.entry
    assert [project.id for project in context.projects] == ["project-1", "project-2"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["submitted", "approved", "rejected"])
async def test_only_draft_entries_can_be_edited_or_deleted(status: str) -> None:
    service, port, _ = build()
    port.entry = draft_entry(status=status)

    with pytest.raises(OrbitValidationError, match="Only draft"):
        await service.start_edit(USER, ENTRY_ID)
    with pytest.raises(OrbitValidationError, match="Only draft"):
        await service.prepare_delete(USER, ENTRY_ID)


@pytest.mark.asyncio
async def test_missing_or_invalid_entry_is_rejected() -> None:
    service, port, _ = build()
    port.entry = None

    with pytest.raises(OrbitNotFoundError):
        await service.prepare_delete(USER, ENTRY_ID)
    with pytest.raises(OrbitValidationError, match="invalid"):
        await service.prepare_delete(USER, "not-a-uuid")


@pytest.mark.asyncio
async def test_prepare_edit_keeps_unchanged_task_without_lookup_and_reports_changes() -> None:
    service, _, store = build()

    change = await prepare(service, duration_text="2h", description="  New notes  ")

    assert change.kind == "update"
    assert change.changed_fields == ("duration_minutes", "description")
    assert change.after is not None
    assert change.after.task_id == "task-1"
    assert change.after.description == "New notes"
    assert await store.get(USER) == change


@pytest.mark.asyncio
async def test_prepare_edit_resolves_new_project_and_task() -> None:
    service, _, _ = build()

    change = await prepare(service, project_id="project-2", task_name="code review")

    assert change.after is not None
    assert (change.after.project_id, change.after.task_id) == ("project-2", "task-2")
    assert change.changed_fields == ("project_id", "task_id")


@pytest.mark.asyncio
async def test_prepare_edit_rejects_ambiguous_task() -> None:
    service, _, _ = build()

    with pytest.raises(OrbitAmbiguousMatchError):
        await prepare(service, project_id="project-2", task_name="code")


@pytest.mark.asyncio
async def test_prepare_edit_rejects_unassigned_project() -> None:
    service, _, _ = build()

    with pytest.raises(OrbitNotFoundError, match="not assigned"):
        await prepare(service, project_id="project-999")


@pytest.mark.asyncio
async def test_prepare_edit_requires_a_change() -> None:
    service, _, store = build()

    with pytest.raises(OrbitValidationError, match="No changes"):
        await prepare(service)
    assert await store.get(USER) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"entry_date_text": "25/09/2026"},
        {"duration_text": "25h"},
        {"duration_text": "0m"},
        {"description": "   "},
        {"description": "x" * 2001},
        {"task_name": " "},
    ],
)
async def test_prepare_edit_validates_form_values(overrides: dict[str, str]) -> None:
    service, _, store = build()

    with pytest.raises(OrbitValidationError):
        await prepare(service, **overrides)
    assert await store.get(USER) is None


@pytest.mark.asyncio
async def test_confirm_update_applies_once_and_cannot_be_replayed() -> None:
    service, port, store = build()
    change = await prepare(service, duration_text="2h")

    applied = await service.confirm(USER, change.id)

    assert applied.kind == "update"
    assert applied.entry.duration_minutes == 120
    assert port.updates == [change]
    assert await store.get(USER) is None
    with pytest.raises(OrbitNotFoundError, match="no longer current"):
        await service.confirm(USER, change.id)
    assert len(port.updates) == 1


@pytest.mark.asyncio
async def test_confirm_delete_removes_entry_after_confirmation_only() -> None:
    service, port, _ = build()
    change = await service.prepare_delete(USER, ENTRY_ID)

    assert port.deletes == []
    applied = await service.confirm(USER, change.id)

    assert applied.kind == "delete"
    assert applied.entry.id == ENTRY_ID
    assert port.deletes == [("employee-1", ENTRY_ID)]


@pytest.mark.asyncio
async def test_confirm_rejects_stale_card_after_newer_change() -> None:
    service, port, _ = build()
    old = await service.prepare_delete(USER, ENTRY_ID)
    await prepare(service, duration_text="2h")

    with pytest.raises(OrbitNotFoundError, match="no longer current"):
        await service.confirm(USER, old.id)
    assert port.deletes == []


@pytest.mark.asyncio
async def test_confirm_rejects_expired_change() -> None:
    clock = {"now": NOW}
    service, port, store = build(now=lambda: clock["now"])
    change = await service.prepare_delete(USER, ENTRY_ID)
    clock["now"] = NOW + timedelta(seconds=601)

    with pytest.raises(OrbitValidationError, match="expired"):
        await service.confirm(USER, change.id)
    assert port.deletes == []
    assert await store.get(USER) is None


@pytest.mark.asyncio
async def test_confirm_rejects_entry_changed_after_review() -> None:
    service, port, store = build()
    change = await service.prepare_delete(USER, ENTRY_ID)
    port.entry = draft_entry(description="Edited elsewhere")

    with pytest.raises(OrbitValidationError, match="changed in Orbit"):
        await service.confirm(USER, change.id)
    assert port.deletes == []
    assert await store.get(USER) is None


@pytest.mark.asyncio
async def test_confirm_rejects_entry_submitted_after_review() -> None:
    service, port, _ = build()
    change = await prepare(service, duration_text="2h")
    port.entry = draft_entry(status="submitted")

    with pytest.raises(OrbitValidationError, match="Only draft"):
        await service.confirm(USER, change.id)
    assert port.updates == []


@pytest.mark.asyncio
async def test_uncertain_mutation_is_not_retried_and_pending_is_claimed() -> None:
    service, port, store = build()
    change = await service.prepare_delete(USER, ENTRY_ID)
    port.mutation_error = OrbitMutationUncertainError("Check Orbit before retrying.")

    with pytest.raises(OrbitMutationUncertainError):
        await service.confirm(USER, change.id)
    assert await store.get(USER) is None
    with pytest.raises(OrbitNotFoundError):
        await service.confirm(USER, change.id)


@pytest.mark.asyncio
async def test_cancel_discards_pending_change() -> None:
    service, port, store = build()
    change = await service.prepare_delete(USER, ENTRY_ID)

    await service.cancel(USER, change.id)

    assert await store.get(USER) is None
    assert port.deletes == []
    with pytest.raises(OrbitNotFoundError):
        await service.cancel(USER, change.id)
