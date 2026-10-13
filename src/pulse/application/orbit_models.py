from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal


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
class ProjectSelectionRequest:
    id: str
    teams_user_id: str
    employee_id: str
    organization_id: str
    parsed: ParsedTimesheetDraft
    options: tuple[OrbitProject, ...]
    expires_at: datetime


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


BulkSchedule = Literal["single_day", "weekdays", "all_days"]
BulkEntity = Literal["project", "task"]
BulkOutcomeStatus = Literal["created", "failed", "uncertain"]


@dataclass(frozen=True, slots=True)
class ParsedBulkEntryTemplate:
    project_name: str
    task_name: str
    duration_minutes: int
    description: str


@dataclass(frozen=True, slots=True)
class ParsedBulkTimesheetRequest:
    start_date: date
    end_date: date
    schedule: BulkSchedule | None
    entries: tuple[ParsedBulkEntryTemplate, ...]


@dataclass(frozen=True, slots=True)
class BulkSelectionOption:
    id: str
    name: str


@dataclass(frozen=True, slots=True)
class BulkPreparation:
    id: str
    teams_user_id: str
    employee_id: str
    organization_id: str
    parsed: ParsedBulkTimesheetRequest
    schedule: BulkSchedule | None
    project_choices: tuple[tuple[int, str], ...]
    task_choices: tuple[tuple[int, str], ...]
    awaiting_entity: BulkEntity | None
    awaiting_template_index: int | None
    allowed_option_ids: tuple[str, ...]
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class BulkScheduleSelectionRequest:
    preparation_id: str
    start_date: date
    end_date: date
    template_count: int


@dataclass(frozen=True, slots=True)
class BulkEntitySelectionRequest:
    preparation_id: str
    entity: BulkEntity
    template_index: int
    requested_name: str
    options: tuple[BulkSelectionOption, ...]


@dataclass(frozen=True, slots=True)
class ResolvedBulkEntryTemplate:
    project_id: str
    project_name: str
    task_id: str
    task_name: str
    duration_minutes: int
    description: str


@dataclass(frozen=True, slots=True)
class PendingTimesheetBatch:
    id: str
    teams_user_id: str
    employee_id: str
    organization_id: str
    start_date: date
    end_date: date
    schedule: BulkSchedule
    dates: tuple[date, ...]
    templates: tuple[ResolvedBulkEntryTemplate, ...]
    entries: tuple[PendingTimesheetEntry, ...]
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class BulkEntryOutcome:
    entry: PendingTimesheetEntry
    status: BulkOutcomeStatus
    reference: str | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class BulkCreationResult:
    batch_id: str
    outcomes: tuple[BulkEntryOutcome, ...]


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
