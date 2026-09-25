from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Protocol

from pulse.application.orbit_models import (
    CreatedTimesheetEntry,
    OrbitEmployee,
    OrbitProject,
    OrbitSession,
    OrbitTask,
    ParsedTimesheetDraft,
    PendingTimesheetEntry,
)


class OrbitAuthPort(Protocol):
    async def sign_in(self, email: str, password: str) -> OrbitSession: ...

    async def refresh(self, refresh_token: str) -> OrbitSession: ...

    async def get_authenticated_user_id(self, access_token: str) -> str: ...


class OrbitTimesheetPort(Protocol):
    async def get_employee(self, access_token: str, auth_user_id: str) -> OrbitEmployee: ...

    async def list_assigned_projects(
        self, access_token: str, employee_id: str
    ) -> Sequence[OrbitProject]: ...

    async def list_active_tasks(
        self, access_token: str, project_id: str
    ) -> Sequence[OrbitTask]: ...

    async def create_entry(
        self, access_token: str, entry: PendingTimesheetEntry
    ) -> CreatedTimesheetEntry: ...


class TimesheetDraftParser(Protocol):
    async def parse(self, text: str, *, today: date) -> ParsedTimesheetDraft: ...


class OrbitSessionStore(Protocol):
    async def get(self, teams_user_id: str) -> OrbitSession | None: ...

    async def save(
        self,
        teams_user_id: str,
        session: OrbitSession,
        *,
        reset_ttl: bool = False,
    ) -> None: ...

    async def delete(self, teams_user_id: str) -> None: ...


class PendingEntryStore(Protocol):
    async def get(self, teams_user_id: str) -> PendingTimesheetEntry | None: ...

    async def save(self, entry: PendingTimesheetEntry) -> None: ...

    async def delete(self, teams_user_id: str) -> None: ...

