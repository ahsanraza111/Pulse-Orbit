from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from pulse.application.errors import OrbitNotFoundError, OrbitValidationError
from pulse.application.orbit_models import (
    OrbitEmployee,
    OrbitProject,
    OrbitSession,
    OrbitTimesheetEntry,
    ParsedTimesheetQuery,
    ResolvedTimesheetQuery,
    TimesheetEntryBatch,
)
from pulse.application.services.orbit import OrbitViewEntriesService

NOW = datetime(2026, 9, 25, 12, tzinfo=UTC)
ENTRY_ID = "3c160089-5f25-4bab-a722-5179c0e5d3ae"


class FakeAuthService:
    async def get_authenticated_session(self, teams_user_id: str):
        assert teams_user_id == "teams-user-1"
        return (
            OrbitSession("access", "refresh", NOW + timedelta(hours=1)),
            "auth-user-1",
        )


class FakeQueryParser:
    def __init__(self, query: ParsedTimesheetQuery) -> None:
        self.query = query
        self.received = None

    async def parse(self, text: str, *, today: date) -> ParsedTimesheetQuery:
        self.received = (text, today)
        return self.query


class FakeTimesheetPort:
    def __init__(self) -> None:
        self.projects = [
            OrbitProject("project-1", "ADGM Form Submissions"),
            OrbitProject("project-2", "Internal Platform"),
        ]
        self.list_received = None
        self.detail_received = None
        self.entry = OrbitTimesheetEntry(
            id=ENTRY_ID,
            entry_date=date(2026, 9, 25),
            duration_minutes=90,
            description="Fixed validation",
            status="draft",
            project_name="ADGM Form Submissions",
            task_name="Backend Development",
        )

    async def get_employee(self, access_token: str, auth_user_id: str) -> OrbitEmployee:
        assert (access_token, auth_user_id) == ("access", "auth-user-1")
        return OrbitEmployee("employee-1", "organization-1")

    async def list_assigned_projects(self, access_token: str, employee_id: str):
        assert (access_token, employee_id) == ("access", "employee-1")
        return self.projects

    async def list_entries(self, access_token: str, **kwargs):
        self.list_received = (access_token, kwargs)
        return TimesheetEntryBatch(entries=(self.entry,), total_count=1)

    async def get_entry(self, access_token: str, **kwargs):
        self.detail_received = (access_token, kwargs)
        return self.entry


def service(parser: FakeQueryParser, port: FakeTimesheetPort) -> OrbitViewEntriesService:
    return OrbitViewEntriesService(
        FakeAuthService(),  # type: ignore[arg-type]
        port,  # type: ignore[arg-type]
        parser,
        business_timezone="Asia/Karachi",
        page_size=5,
        max_range_days=366,
        now=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_list_resolves_project_and_scopes_query_to_employee() -> None:
    parser = FakeQueryParser(
        ParsedTimesheetQuery(
            date(2026, 9, 21),
            date(2026, 9, 27),
            project_name="adgm forms",
            status="draft",
        )
    )
    port = FakeTimesheetPort()

    page = await service(parser, port).list_from_text(
        "teams-user-1", "show ADGM draft entries this week"
    )

    assert page.query.project_id == "project-1"
    assert page.query.project_name == "ADGM Form Submissions"
    assert page.entries == (port.entry,)
    assert parser.received == ("show ADGM draft entries this week", date(2026, 9, 25))
    assert port.list_received == (
        "access",
        {
            "employee_id": "employee-1",
            "start_date": date(2026, 9, 21),
            "end_date": date(2026, 9, 27),
            "project_id": "project-1",
            "status": "draft",
            "limit": 5,
            "offset": 0,
        },
    )


@pytest.mark.asyncio
async def test_navigation_revalidates_hidden_project_id() -> None:
    parser = FakeQueryParser(ParsedTimesheetQuery(date(2026, 9, 25), date(2026, 9, 25)))
    port = FakeTimesheetPort()
    query = ResolvedTimesheetQuery(
        date(2026, 9, 25),
        date(2026, 9, 25),
        project_id="tampered-project",
        page=1,
    )

    with pytest.raises(OrbitNotFoundError, match="not assigned"):
        await service(parser, port).list_page("teams-user-1", query)

    assert port.list_received is None


@pytest.mark.asyncio
async def test_list_rejects_oversized_date_range() -> None:
    parser = FakeQueryParser(
        ParsedTimesheetQuery(date(2025, 1, 1), date(2026, 9, 25))
    )
    port = FakeTimesheetPort()

    with pytest.raises(OrbitValidationError, match="cannot exceed"):
        await service(parser, port).list_from_text("teams-user-1", "show everything")


@pytest.mark.asyncio
async def test_list_rejects_unknown_status() -> None:
    parser = FakeQueryParser(
        ParsedTimesheetQuery(
            date(2026, 9, 25),
            date(2026, 9, 25),
            status="invented-status",
        )
    )
    port = FakeTimesheetPort()

    with pytest.raises(OrbitValidationError, match="status must be one of"):
        await service(parser, port).list_from_text("teams-user-1", "show unknown entries")


@pytest.mark.asyncio
async def test_detail_is_scoped_to_resolved_employee() -> None:
    parser = FakeQueryParser(ParsedTimesheetQuery(date(2026, 9, 25), date(2026, 9, 25)))
    port = FakeTimesheetPort()

    entry = await service(parser, port).get_detail("teams-user-1", ENTRY_ID)

    assert entry == port.entry
    assert port.detail_received == (
        "access",
        {"employee_id": "employee-1", "entry_id": ENTRY_ID},
    )
