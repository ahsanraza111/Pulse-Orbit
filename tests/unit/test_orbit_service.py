from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from pulse.application.errors import (
    OrbitAmbiguousMatchError,
    OrbitNotFoundError,
    OrbitValidationError,
)
from pulse.application.orbit_models import (
    CreatedTimesheetEntry,
    OrbitEmployee,
    OrbitProject,
    OrbitSession,
    OrbitTask,
    ParsedTimesheetDraft,
)
from pulse.application.services.orbit import OrbitAddEntryService, OrbitAuthService
from pulse.infrastructure.orbit.memory import InMemoryPendingEntryStore

NOW = datetime(2026, 9, 22, 12, tzinfo=UTC)


class MemorySessionStore:
    def __init__(self, session: OrbitSession | None = None) -> None:
        self.session = session
        self.reset_ttl_values: list[bool] = []

    async def get(self, teams_user_id: str) -> OrbitSession | None:
        return self.session

    async def save(
        self,
        teams_user_id: str,
        session: OrbitSession,
        *,
        reset_ttl: bool = False,
    ) -> None:
        self.session = session
        self.reset_ttl_values.append(reset_ttl)

    async def delete(self, teams_user_id: str) -> None:
        self.session = None


class FakeOrbitClient:
    def __init__(self) -> None:
        self.projects = [OrbitProject("project-1", "Alpha Project")]
        self.tasks = [OrbitTask("task-1", "API Development")]
        self.created = []
        self.refresh_calls = 0
        self.sign_in_password = None

    async def sign_in(self, email: str, password: str) -> OrbitSession:
        self.sign_in_password = password
        return session()

    async def refresh(self, refresh_token: str) -> OrbitSession:
        self.refresh_calls += 1
        return session(access_token="refreshed-access")

    async def get_authenticated_user_id(self, access_token: str) -> str:
        return "auth-user-1"

    async def get_employee(self, access_token: str, auth_user_id: str) -> OrbitEmployee:
        return OrbitEmployee("employee-1", "organization-1")

    async def list_assigned_projects(
        self, access_token: str, employee_id: str
    ) -> list[OrbitProject]:
        return self.projects

    async def list_active_tasks(self, access_token: str, project_id: str) -> list[OrbitTask]:
        return self.tasks

    async def create_entry(self, access_token: str, entry):
        self.created.append(entry)
        return CreatedTimesheetEntry("entry-1", entry.entry_date, entry.duration_minutes)


class FakeParser:
    def __init__(self, parsed: ParsedTimesheetDraft | None = None) -> None:
        self.parsed = parsed or ParsedTimesheetDraft(
            project_name="Alpha",
            task_name="API",
            entry_date=date(2026, 9, 22),
            duration_minutes=120,
            description="Fixed retry handling",
        )

    async def parse(self, text: str, *, today: date) -> ParsedTimesheetDraft:
        return self.parsed


def session(
    *, access_token: str = "access", expires_at: datetime | None = None
) -> OrbitSession:
    return OrbitSession(
        access_token=access_token,
        refresh_token="refresh",
        expires_at=expires_at or NOW + timedelta(hours=1),
    )


def services(
    client: FakeOrbitClient | None = None,
    parser: FakeParser | None = None,
    stored_session: OrbitSession | None = None,
):
    client = client or FakeOrbitClient()
    auth = OrbitAuthService(
        client,
        MemorySessionStore(stored_session or session()),
        now=lambda: NOW,
    )
    add = OrbitAddEntryService(
        auth,
        client,
        parser or FakeParser(),
        InMemoryPendingEntryStore(),
        confirmation_ttl_seconds=600,
        business_timezone="Asia/Karachi",
        max_duration_minutes=1440,
        max_notes_chars=2000,
        now=lambda: NOW,
    )
    return client, auth, add


@pytest.mark.asyncio
async def test_login_authenticates_and_stores_session_without_storing_password() -> None:
    client = FakeOrbitClient()
    store = MemorySessionStore()
    auth = OrbitAuthService(client, store, now=lambda: NOW)

    await auth.login("teams-user-1", " employee@example.com ", "secret-password")

    assert client.sign_in_password == "secret-password"
    assert store.session == session()
    assert store.reset_ttl_values == [True]
    assert not hasattr(store.session, "password")


@pytest.mark.asyncio
async def test_prepare_and_confirm_creates_exactly_once() -> None:
    client, _, add = services()

    pending = await add.prepare("teams-user-1", "natural language request")
    created = await add.confirm("teams-user-1")

    assert pending.project_name == "Alpha Project"
    assert pending.task_name == "API Development"
    assert created.id == "entry-1"
    assert len(client.created) == 1


@pytest.mark.asyncio
async def test_stale_confirmation_card_cannot_create_newer_pending_entry() -> None:
    client, _, add = services()

    stale = await add.prepare("teams-user-1", "first natural language request")
    current = await add.prepare("teams-user-1", "new natural language request")

    with pytest.raises(OrbitNotFoundError, match="no longer current"):
        await add.confirm("teams-user-1", stale.id)

    created = await add.confirm("teams-user-1", current.id)
    assert created.id == "entry-1"
    assert len(client.created) == 1


@pytest.mark.asyncio
async def test_stale_cancel_card_cannot_discard_newer_pending_entry() -> None:
    client, _, add = services()

    stale = await add.prepare("teams-user-1", "first natural language request")
    current = await add.prepare("teams-user-1", "new natural language request")

    with pytest.raises(OrbitNotFoundError, match="no longer current"):
        await add.cancel("teams-user-1", stale.id)

    created = await add.confirm("teams-user-1", current.id)
    assert created.id == "entry-1"
    assert len(client.created) == 1
    with pytest.raises(OrbitNotFoundError, match="no pending"):
        await add.confirm("teams-user-1")
    assert len(client.created) == 1


@pytest.mark.asyncio
async def test_prepare_rejects_ambiguous_project() -> None:
    client = FakeOrbitClient()
    client.projects = [
        OrbitProject("p1", "Alpha API"),
        OrbitProject("p2", "Alpha Web"),
    ]
    _, _, add = services(client=client)

    with pytest.raises(OrbitAmbiguousMatchError) as error:
        await add.prepare("teams-user-1", "request")

    assert error.value.options == ["Alpha API", "Alpha Web"]


@pytest.mark.asyncio
async def test_prepare_fuzzy_matches_live_project_and_task_names() -> None:
    client = FakeOrbitClient()
    client.projects = [
        OrbitProject("p1", "ADGM Form Submissions"),
        OrbitProject("p2", "Internal Platform"),
    ]
    client.tasks = [
        OrbitTask("t1", "Backend Development"),
        OrbitTask("t2", "Quality Assurance"),
    ]
    parser = FakeParser(
        ParsedTimesheetDraft(
            "adgm forms",
            "backend dev",
            date(2026, 9, 22),
            240,
            "API fix",
        )
    )
    _, _, add = services(client=client, parser=parser)

    pending = await add.prepare("teams-user-1", "natural language request")

    assert pending.project_id == "p1"
    assert pending.project_name == "ADGM Form Submissions"
    assert pending.task_id == "t1"
    assert pending.task_name == "Backend Development"


@pytest.mark.asyncio
async def test_prepare_fuzzy_matches_minor_typo() -> None:
    client = FakeOrbitClient()
    client.projects = [OrbitProject("p1", "ADGM Form Submissions")]
    parser = FakeParser(
        ParsedTimesheetDraft(
            "adgm frms",
            "API Development",
            date(2026, 9, 22),
            60,
            "Fixed validation",
        )
    )
    _, _, add = services(client=client, parser=parser)

    pending = await add.prepare("teams-user-1", "natural language request")

    assert pending.project_name == "ADGM Form Submissions"


@pytest.mark.asyncio
async def test_prepare_validates_duration() -> None:
    parser = FakeParser(
        ParsedTimesheetDraft("Alpha", "API", date(2026, 9, 22), 0, "notes")
    )
    _, _, add = services(parser=parser)

    with pytest.raises(OrbitValidationError, match="Duration"):
        await add.prepare("teams-user-1", "request")


@pytest.mark.asyncio
async def test_expired_session_is_refreshed() -> None:
    client, auth, _ = services(stored_session=session(expires_at=NOW - timedelta(seconds=1)))

    valid = await auth.get_valid_session("teams-user-1")

    assert valid.access_token == "refreshed-access"
    assert client.refresh_calls == 1
