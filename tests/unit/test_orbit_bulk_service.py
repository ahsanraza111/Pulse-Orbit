from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from pulse.application.errors import (
    OrbitMutationUncertainError,
    OrbitNotFoundError,
    OrbitProviderError,
    OrbitValidationError,
)
from pulse.application.orbit_models import (
    BulkEntitySelectionRequest,
    BulkScheduleSelectionRequest,
    CreatedTimesheetEntry,
    OrbitEmployee,
    OrbitProject,
    OrbitSession,
    OrbitTask,
    ParsedBulkEntryTemplate,
    ParsedBulkTimesheetRequest,
    PendingTimesheetBatch,
)
from pulse.application.services.orbit_bulk import OrbitBulkEntryService
from pulse.infrastructure.orbit.memory import (
    InMemoryBulkPreparationStore,
    InMemoryPendingBatchStore,
)

NOW = datetime(2026, 9, 30, 12, tzinfo=UTC)


class FakeAuthService:
    async def get_authenticated_session(self, teams_user_id: str):
        return (
            OrbitSession("access", "refresh", NOW + timedelta(hours=1)),
            "auth-user-1",
        )


class FakeBulkParser:
    def __init__(self, parsed: ParsedBulkTimesheetRequest) -> None:
        self.parsed = parsed

    async def parse(self, text: str, *, today: date) -> ParsedBulkTimesheetRequest:
        return self.parsed


class FakeOrbitPort:
    def __init__(self) -> None:
        self.projects = [OrbitProject("p1", "ADGM Website Development")]
        self.tasks = {
            "p1": [
                OrbitTask("t1", "Meetings and Calls"),
                OrbitTask("t2", "Backend Development"),
            ]
        }
        self.created = []
        self.outcomes: dict[str, str] = {}

    async def get_employee(self, access_token: str, auth_user_id: str) -> OrbitEmployee:
        return OrbitEmployee("employee-1", "organization-1")

    async def list_assigned_projects(self, access_token: str, employee_id: str):
        return self.projects

    async def list_active_tasks(self, access_token: str, project_id: str):
        return self.tasks[project_id]

    async def create_entry(self, access_token: str, entry):
        self.created.append(entry)
        outcome = self.outcomes.get(entry.description)
        if outcome == "failed":
            raise OrbitProviderError("provider detail")
        if outcome == "uncertain":
            raise OrbitMutationUncertainError("provider detail")
        return CreatedTimesheetEntry(
            f"created-{len(self.created)}", entry.entry_date, entry.duration_minutes
        )


def request(
    *,
    start: date = date(2026, 9, 21),
    end: date = date(2026, 9, 21),
    schedule="single_day",
    entries: tuple[ParsedBulkEntryTemplate, ...] | None = None,
) -> ParsedBulkTimesheetRequest:
    return ParsedBulkTimesheetRequest(
        start_date=start,
        end_date=end,
        schedule=schedule,
        entries=entries
        or (
            ParsedBulkEntryTemplate(
                "ADGM Website", "Meetings and Calls", 30, "Standup"
            ),
            ParsedBulkEntryTemplate(
                "ADGM Website", "Backend Development", 120, "API fix"
            ),
        ),
    )


def service(
    parsed: ParsedBulkTimesheetRequest,
    port: FakeOrbitPort | None = None,
    *,
    max_templates: int = 10,
    max_range_days: int = 31,
    max_generated_entries: int = 100,
):
    port = port or FakeOrbitPort()
    bulk = OrbitBulkEntryService(
        FakeAuthService(),  # type: ignore[arg-type]
        port,  # type: ignore[arg-type]
        FakeBulkParser(parsed),
        InMemoryBulkPreparationStore(),
        InMemoryPendingBatchStore(),
        confirmation_ttl_seconds=600,
        business_timezone="Asia/Karachi",
        max_duration_minutes=1440,
        max_notes_chars=2000,
        max_templates=max_templates,
        max_range_days=max_range_days,
        max_generated_entries=max_generated_entries,
        now=lambda: NOW,
    )
    return port, bulk


@pytest.mark.asyncio
async def test_single_day_bulk_generates_each_template_once() -> None:
    _, bulk = service(request())

    prepared = await bulk.prepare("teams-user-1", "request")

    assert isinstance(prepared, PendingTimesheetBatch)
    assert prepared.schedule == "single_day"
    assert prepared.dates == (date(2026, 9, 21),)
    assert len(prepared.entries) == 2
    assert sum(entry.duration_minutes for entry in prepared.entries) == 150


@pytest.mark.asyncio
async def test_range_without_schedule_requires_user_selection() -> None:
    _, bulk = service(
        request(start=date(2026, 9, 21), end=date(2026, 9, 30), schedule=None)
    )

    selection = await bulk.prepare("teams-user-1", "request")

    assert isinstance(selection, BulkScheduleSelectionRequest)
    assert selection.template_count == 2


@pytest.mark.asyncio
async def test_weekday_selection_generates_inclusive_working_dates() -> None:
    _, bulk = service(
        request(start=date(2026, 9, 21), end=date(2026, 9, 30), schedule=None)
    )
    selection = await bulk.prepare("teams-user-1", "request")

    prepared = await bulk.select_schedule(
        "teams-user-1", selection.preparation_id, "weekdays"
    )

    assert isinstance(prepared, PendingTimesheetBatch)
    assert len(prepared.dates) == 8
    assert date(2026, 9, 26) not in prepared.dates
    assert len(prepared.entries) == 16
    assert sum(entry.duration_minutes for entry in prepared.entries) == 1200


@pytest.mark.asyncio
async def test_all_days_schedule_includes_weekends() -> None:
    _, bulk = service(
        request(
            start=date(2026, 9, 21),
            end=date(2026, 9, 30),
            schedule="all_days",
        )
    )

    prepared = await bulk.prepare("teams-user-1", "request")

    assert isinstance(prepared, PendingTimesheetBatch)
    assert len(prepared.dates) == 10
    assert len(prepared.entries) == 20


@pytest.mark.asyncio
async def test_ambiguous_project_selection_resumes_whole_batch() -> None:
    port = FakeOrbitPort()
    port.projects = [
        OrbitProject("p1", "ADGM Website Development"),
        OrbitProject("p2", "ADGM Form Submissions"),
    ]
    port.tasks["p2"] = port.tasks["p1"]
    parsed = request(
        entries=(
            ParsedBulkEntryTemplate("ADGM", "Meetings and Calls", 30, "Standup"),
        )
    )
    _, bulk = service(parsed, port)

    selection = await bulk.prepare("teams-user-1", "request")

    assert isinstance(selection, BulkEntitySelectionRequest)
    assert selection.entity == "project"
    assert [option.name for option in selection.options] == [
        "ADGM Website Development",
        "ADGM Form Submissions",
    ]

    prepared = await bulk.select_entity(
        "teams-user-1",
        selection.preparation_id,
        entity="project",
        template_index=0,
        option_id="p1",
    )
    assert isinstance(prepared, PendingTimesheetBatch)
    assert prepared.templates[0].project_id == "p1"


@pytest.mark.asyncio
async def test_generated_entry_limit_is_enforced_before_confirmation() -> None:
    _, bulk = service(
        request(
            start=date(2026, 9, 21),
            end=date(2026, 9, 30),
            schedule="all_days",
        ),
        max_generated_entries=10,
    )

    with pytest.raises(OrbitValidationError, match="generates 20 entries"):
        await bulk.prepare("teams-user-1", "request")


@pytest.mark.asyncio
async def test_confirmation_reports_created_failed_and_uncertain_entries() -> None:
    entries = (
        ParsedBulkEntryTemplate("ADGM Website", "Meetings and Calls", 30, "Created"),
        ParsedBulkEntryTemplate("ADGM Website", "Backend Development", 60, "Failed"),
        ParsedBulkEntryTemplate("ADGM Website", "Meetings and Calls", 45, "Uncertain"),
    )
    port = FakeOrbitPort()
    port.outcomes = {"Failed": "failed", "Uncertain": "uncertain"}
    _, bulk = service(request(entries=entries), port)
    prepared = await bulk.prepare("teams-user-1", "request")

    result = await bulk.confirm("teams-user-1", prepared.id)

    assert [outcome.status for outcome in result.outcomes] == [
        "created",
        "failed",
        "uncertain",
    ]
    assert len(port.created) == 3
    with pytest.raises(OrbitNotFoundError, match="no pending"):
        await bulk.confirm("teams-user-1", prepared.id)


@pytest.mark.asyncio
async def test_exact_duplicate_template_is_rejected() -> None:
    duplicate = ParsedBulkEntryTemplate(
        "ADGM Website", "Meetings and Calls", 30, "Standup"
    )
    _, bulk = service(request(entries=(duplicate, duplicate)))

    with pytest.raises(OrbitValidationError, match="exact duplicate"):
        await bulk.prepare("teams-user-1", "request")
