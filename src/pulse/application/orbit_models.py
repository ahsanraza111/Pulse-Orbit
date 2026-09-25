from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True, slots=True)
class OrbitSession:
    access_token: str
    refresh_token: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class OrbitEmployee:
    id: str
    organization_id: str


@dataclass(frozen=True, slots=True)
class OrbitProject:
    id: str
    name: str


@dataclass(frozen=True, slots=True)
class OrbitTask:
    id: str
    name: str


@dataclass(frozen=True, slots=True)
class ParsedTimesheetDraft:
    project_name: str
    task_name: str
    entry_date: date
    duration_minutes: int
    description: str


@dataclass(frozen=True, slots=True)
class PendingTimesheetEntry:
    id: str
    teams_user_id: str
    employee_id: str
    organization_id: str
    project_id: str
    project_name: str
    task_id: str
    task_name: str
    entry_date: date
    duration_minutes: int
    description: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class CreatedTimesheetEntry:
    id: str
    entry_date: date
    duration_minutes: int


@dataclass(frozen=True, slots=True)
class ParsedTimesheetQuery:
    start_date: date
    end_date: date
    project_name: str | None = None
    status: str | None = None


@dataclass(frozen=True, slots=True)
class ResolvedTimesheetQuery:
    start_date: date
    end_date: date
    project_id: str | None = None
    project_name: str | None = None
    status: str | None = None
    page: int = 0
    page_size: int = 5


@dataclass(frozen=True, slots=True)
class OrbitTimesheetEntry:
    id: str
    entry_date: date
    duration_minutes: int
    description: str
    status: str
    project_name: str
    task_name: str


@dataclass(frozen=True, slots=True)
class TimesheetEntryBatch:
    entries: tuple[OrbitTimesheetEntry, ...]
    total_count: int


@dataclass(frozen=True, slots=True)
class TimesheetEntryPage:
    query: ResolvedTimesheetQuery
    entries: tuple[OrbitTimesheetEntry, ...]
    total_count: int
