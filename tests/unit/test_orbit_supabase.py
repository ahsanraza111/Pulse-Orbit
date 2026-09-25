from datetime import UTC, date, datetime

import httpx
import pytest

from pulse.application.errors import OrbitAuthenticationError, OrbitMutationUncertainError
from pulse.application.orbit_models import PendingTimesheetEntry
from pulse.infrastructure.orbit.supabase import SupabaseOrbitClient


@pytest.mark.asyncio
async def test_sign_in_uses_password_grant_and_api_key() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/auth/v1/token"
        assert request.url.params["grant_type"] == "password"
        assert request.headers["apikey"] == "publishable-key"
        assert "authorization" not in request.headers
        return httpx.Response(
            200,
            json={
                "access_token": "access",
                "refresh_token": "refresh",
                "expires_at": 1_800_000_000,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = SupabaseOrbitClient(
            http, base_url="https://orbit.example", api_key="publishable-key"
        )
        result = await client.sign_in("employee@example.com", "password")

    assert result.access_token == "access"
    assert result.refresh_token == "refresh"


@pytest.mark.asyncio
async def test_sign_in_maps_rejected_credentials() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = SupabaseOrbitClient(http, base_url="https://orbit.example", api_key="key")
        with pytest.raises(OrbitAuthenticationError):
            await client.sign_in("employee@example.com", "wrong")


@pytest.mark.asyncio
async def test_create_entry_requires_returned_row() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer user-token"
        assert request.headers["prefer"] == "return=representation"
        return httpx.Response(
            201,
            json=[
                {
                    "id": "entry-1",
                    "entry_date": "2026-09-22",
                    "duration_minutes": 90,
                }
            ],
        )

    entry = pending_entry()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = SupabaseOrbitClient(http, base_url="https://orbit.example", api_key="key")
        created = await client.create_entry("user-token", entry)

    assert created.id == "entry-1"
    assert created.duration_minutes == 90


@pytest.mark.asyncio
async def test_create_timeout_is_reported_as_uncertain() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = SupabaseOrbitClient(http, base_url="https://orbit.example", api_key="key")
        with pytest.raises(OrbitMutationUncertainError, match="Check Orbit"):
            await client.create_entry("user-token", pending_entry())


@pytest.mark.asyncio
async def test_list_entries_uses_employee_date_and_optional_filters() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rest/v1/timesheet_entries"
        assert request.headers["authorization"] == "Bearer user-token"
        assert request.headers["prefer"] == "count=exact"
        assert request.url.params["employee_id"] == "eq.employee-1"
        assert request.url.params["and"] == (
            "(entry_date.gte.2026-09-21,entry_date.lte.2026-09-27)"
        )
        assert request.url.params["project_id"] == "eq.project-1"
        assert request.url.params["status"] == "eq.draft"
        assert request.url.params["limit"] == "5"
        assert request.url.params["offset"] == "5"
        return httpx.Response(
            200,
            headers={"Content-Range": "5-5/6"},
            json=[timesheet_row()],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = SupabaseOrbitClient(http, base_url="https://orbit.example", api_key="key")
        batch = await client.list_entries(
            "user-token",
            employee_id="employee-1",
            start_date=date(2026, 9, 21),
            end_date=date(2026, 9, 27),
            project_id="project-1",
            status="draft",
            limit=5,
            offset=5,
        )

    assert batch.total_count == 6
    assert len(batch.entries) == 1
    assert batch.entries[0].project_name == "ADGM Form Submissions"
    assert batch.entries[0].task_name == "Backend Development"


@pytest.mark.asyncio
async def test_get_entry_is_scoped_to_employee() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["id"] == "eq.3c160089-5f25-4bab-a722-5179c0e5d3ae"
        assert request.url.params["employee_id"] == "eq.employee-1"
        return httpx.Response(200, json=[timesheet_row()])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = SupabaseOrbitClient(http, base_url="https://orbit.example", api_key="key")
        entry = await client.get_entry(
            "user-token",
            employee_id="employee-1",
            entry_id="3c160089-5f25-4bab-a722-5179c0e5d3ae",
        )

    assert entry is not None
    assert entry.description == "Fixed validation"
    assert entry.status == "draft"


def pending_entry() -> PendingTimesheetEntry:
    return PendingTimesheetEntry(
        id="draft-1",
        teams_user_id="teams-user",
        employee_id="employee-1",
        organization_id="organization-1",
        project_id="project-1",
        project_name="Alpha",
        task_id="task-1",
        task_name="API",
        entry_date=date(2026, 9, 22),
        duration_minutes=90,
        description="Implemented retries",
        expires_at=datetime(2026, 9, 22, 13, tzinfo=UTC),
    )


def timesheet_row() -> dict[str, object]:
    return {
        "id": "3c160089-5f25-4bab-a722-5179c0e5d3ae",
        "entry_date": "2026-09-25",
        "duration_minutes": 90,
        "description": "Fixed validation",
        "status": "draft",
        "project": {"name": "ADGM Form Submissions"},
        "task": {"name": "Backend Development"},
    }

