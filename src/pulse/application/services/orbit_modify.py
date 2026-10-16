from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pulse.application.errors import (
    OrbitAuthenticationError,
    OrbitNotFoundError,
    OrbitValidationError,
)
from pulse.application.orbit_models import (
    AppliedEntryChange,
    EditedEntryValues,
    EditFormContext,
    EntrySelectionRequest,
    OrbitEmployee,
    OrbitProject,
    OrbitSession,
    OrbitTimesheetEntry,
    ParsedEntryChangeRequest,
    PendingEntryChange,
)
from pulse.application.ports.orbit import (
    EntrySelectionStore,
    OrbitTimesheetPort,
    PendingEntryChangeStore,
    TimesheetChangeParser,
)
from pulse.application.services.orbit import (
    OrbitAddEntryService,
    OrbitAuthService,
    utc_now,
)

EDITABLE_STATUS = "draft"
CANDIDATE_FETCH_LIMIT = 50
MAX_SELECTION_OPTIONS = 8
_DURATION_PATTERN = re.compile(
    r"""^\s*
    (?:(?P<hours>\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hour|hours))?
    \s*,?\s*(?:and\s+)?
    (?:(?P<minutes>\d+)\s*(?:m|min|mins|minute|minutes))?
    \s*$""",
    flags=re.IGNORECASE | re.VERBOSE,
)
_CLOCK_DURATION_PATTERN = re.compile(r"^\s*(?P<hours>\d{1,2}):(?P<minutes>[0-5]\d)\s*$")


def _normalized(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def parse_duration_minutes(text: str) -> int:
    """Parse "1h 30m", "90m", "1.5h" or "1:30" into minutes."""
    clock = _CLOCK_DURATION_PATTERN.match(text)
    if clock:
        return int(clock["hours"]) * 60 + int(clock["minutes"])
    match = _DURATION_PATTERN.match(text)
    if match and (match["hours"] or match["minutes"]):
        hours = float(match["hours"]) if match["hours"] else 0.0
        minutes = int(match["minutes"]) if match["minutes"] else 0
        return round(hours * 60) + minutes
    raise OrbitValidationError(
        "Enter the duration with a unit, for example `1h 30m`, `90m`, `1.5h`, or `1:30`."
    )


class OrbitModifyEntryService:
    """Edit and delete the authenticated employee's own draft timesheet entries."""

    def __init__(
        self,
        auth_service: OrbitAuthService,
        timesheet_port: OrbitTimesheetPort,
        pending_store: PendingEntryChangeStore,
        change_parser: TimesheetChangeParser,
        selection_store: EntrySelectionStore,
        *,
        confirmation_ttl_seconds: int,
        max_duration_minutes: int,
        max_notes_chars: int,
        business_timezone: str,
        max_range_days: int,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._auth = auth_service
        self._timesheets = timesheet_port
        self._pending = pending_store
        self._parser = change_parser
        self._selections = selection_store
        self._confirmation_ttl_seconds = confirmation_ttl_seconds
        self._max_duration_minutes = max_duration_minutes
        self._max_notes_chars = max_notes_chars
        self._max_range_days = max_range_days
        self._now = now
        self._locks: dict[str, asyncio.Lock] = {}
        try:
            self._timezone = ZoneInfo(business_timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown Orbit business timezone: {business_timezone}") from exc

    # --- form-driven edit and button-driven delete -------------------------------------

    async def start_edit(self, teams_user_id: str, entry_id: str) -> EditFormContext:
        session, employee = await self._identity(teams_user_id)
        entry = await self._load_draft(session, employee, entry_id)
        projects = await self._timesheets.list_assigned_projects(
            session.access_token, employee.id
        )
        return EditFormContext(entry=entry, projects=tuple(projects))

    async def prepare_edit(
        self,
        teams_user_id: str,
        entry_id: str,
        *,
        entry_date_text: str,
        duration_text: str,
        description: str,
        project_id: str,
        task_name: str,
    ) -> PendingEntryChange:
        session, employee = await self._identity(teams_user_id)
        entry = await self._load_draft(session, employee, entry_id)
        try:
            entry_date = date.fromisoformat(entry_date_text.strip())
        except ValueError as exc:
            raise OrbitValidationError("Enter the date as YYYY-MM-DD.") from exc
        if not task_name.strip():
            raise OrbitValidationError("A task is required.")
        return await self._build_update(
            teams_user_id,
            session,
            employee,
            entry,
            entry_date=entry_date,
            duration_minutes=parse_duration_minutes(duration_text),
            description=description,
            project_id=project_id,
            project_name=None,
            task_name=task_name,
        )

    async def prepare_delete(self, teams_user_id: str, entry_id: str) -> PendingEntryChange:
        session, employee = await self._identity(teams_user_id)
        entry = await self._load_draft(session, employee, entry_id)
        return await self._build_delete(teams_user_id, employee, entry)

    # --- natural-language edit and delete ----------------------------------------------

    async def prepare_from_text(
        self, teams_user_id: str, request_text: str
    ) -> PendingEntryChange | EntrySelectionRequest:
        """Edit or delete one draft entry described in natural language.

        A single match goes straight to a confirmation; several matches ask the user to
        pick one, so PULSE never guesses which entry to change.
        """
        session, employee = await self._identity(teams_user_id)
        today = self._now().astimezone(self._timezone).date()
        request = await self._parser.parse(request_text, today=today)
        self._validate_request(request)

        candidates = await self._find_candidates(session, employee, request, today)
        if not candidates:
            raise OrbitNotFoundError(
                "No draft entries matched that description. Only draft entries can be "
                "changed; try a different date, project, or task."
            )
        if len(candidates) == 1:
            return await self._prepare_for_entry(
                teams_user_id, session, employee, candidates[0], request
            )
        if len(candidates) > MAX_SELECTION_OPTIONS:
            raise OrbitValidationError(
                f"{len(candidates)} draft entries matched. Narrow it down by date, "
                "project, or task, or open the entry from your timesheet list."
            )
        selection = EntrySelectionRequest(
            id=uuid4().hex,
            teams_user_id=teams_user_id,
            employee_id=employee.id,
            request=request,
            options=tuple(candidates),
            expires_at=self._expires_at(),
        )
        await self._pending.delete(teams_user_id)
        await self._selections.save(selection)
        return selection

    async def select_entry(
        self, teams_user_id: str, selection_id: str, entry_id: str
    ) -> PendingEntryChange:
        async with self._lock_for(teams_user_id):
            selection = await self._selections.get(teams_user_id)
            if not selection or selection.id != selection_id:
                raise OrbitNotFoundError(
                    "This selection card is no longer current. Send your request again."
                )
            if selection.expires_at <= self._now():
                await self._selections.delete(teams_user_id)
                raise OrbitValidationError("This selection expired. Send your request again.")
            if entry_id not in {option.id for option in selection.options}:
                raise OrbitValidationError("That entry was not one of the matches.")

            session, employee = await self._identity(teams_user_id)
            if selection.employee_id != employee.id:
                raise OrbitAuthenticationError(
                    "The Orbit identity changed. Please send your request again."
                )
            entry = await self._load_draft(session, employee, entry_id)
            return await self._prepare_for_entry(
                teams_user_id, session, employee, entry, selection.request
            )

    # --- confirmation ------------------------------------------------------------------

    async def confirm(self, teams_user_id: str, expected_change_id: str) -> AppliedEntryChange:
        async with self._lock_for(teams_user_id):
            change = await self._pending.get(teams_user_id)
            if not change or change.id != expected_change_id:
                raise OrbitNotFoundError(
                    "This confirmation card is no longer current. Open the entry again."
                )
            if change.expires_at <= self._now():
                await self._pending.delete(teams_user_id)
                raise OrbitValidationError(
                    "This confirmation expired. Open the entry again to continue."
                )

            session, employee = await self._identity(teams_user_id)
            if (
                change.employee_id != employee.id
                or change.organization_id != employee.organization_id
            ):
                raise OrbitAuthenticationError(
                    "The Orbit identity changed. Please open the entry again."
                )
            current = await self._load_draft(session, employee, change.before.id)
            if current != change.before:
                await self._pending.delete(teams_user_id)
                raise OrbitValidationError(
                    "This entry changed in Orbit after you reviewed it. Open it again."
                )

            # Claim the change before the network mutation so a timeout can never be
            # retried automatically from this confirmation.
            await self._pending.delete(teams_user_id)
            if change.kind == "delete":
                await self._timesheets.delete_entry(
                    session.access_token,
                    employee_id=employee.id,
                    entry_id=change.before.id,
                )
                return AppliedEntryChange(kind="delete", entry=change.before)
            updated = await self._timesheets.update_entry(session.access_token, change)
            return AppliedEntryChange(kind="update", entry=updated)

    async def cancel(self, teams_user_id: str, expected_change_id: str) -> None:
        async with self._lock_for(teams_user_id):
            change = await self._pending.get(teams_user_id)
            if not change or change.id != expected_change_id:
                raise OrbitNotFoundError(
                    "This confirmation card is no longer current. Open the entry again."
                )
            await self._pending.delete(teams_user_id)

    # --- internals ---------------------------------------------------------------------

    def _validate_request(self, request: ParsedEntryChangeRequest) -> None:
        if not any(
            (
                request.start_date,
                request.end_date,
                request.project_name,
                request.task_name,
                request.notes_hint,
            )
        ):
            raise OrbitValidationError(
                "Say which entry to change: its date, project, task, or a word from its notes."
            )
        if request.action == "edit" and not any(
            value is not None
            for value in (
                request.new_entry_date,
                request.new_duration_minutes,
                request.new_description,
                request.new_project_name,
                request.new_task_name,
            )
        ):
            raise OrbitValidationError(
                "Say what to change: the duration, notes, date, project, or task."
            )

    async def _find_candidates(
        self,
        session: OrbitSession,
        employee: OrbitEmployee,
        request: ParsedEntryChangeRequest,
        today: date,
    ) -> list[OrbitTimesheetEntry]:
        if request.start_date or request.end_date:
            start = request.start_date or request.end_date
            end = request.end_date or request.start_date
        else:
            start = today - timedelta(days=today.weekday())
            end = start + timedelta(days=6)
        assert start is not None and end is not None
        if start > end:
            raise OrbitValidationError("The start date must not follow the end date.")
        if (end - start).days + 1 > self._max_range_days:
            raise OrbitValidationError(f"Date ranges cannot exceed {self._max_range_days} days.")

        project_id: str | None = None
        if request.project_name:
            projects = await self._timesheets.list_assigned_projects(
                session.access_token, employee.id
            )
            project_id = OrbitAddEntryService.match_entity(
                "project", request.project_name, projects
            ).id
        batch = await self._timesheets.list_entries(
            session.access_token,
            employee_id=employee.id,
            start_date=start,
            end_date=end,
            project_id=project_id,
            status=EDITABLE_STATUS,
            limit=CANDIDATE_FETCH_LIMIT,
            offset=0,
        )
        if batch.total_count > len(batch.entries):
            raise OrbitValidationError(
                "Too many draft entries in that range. Narrow it down by date, project, or task."
            )
        task = _normalized(request.task_name) if request.task_name else None
        hint = _normalized(request.notes_hint) if request.notes_hint else None
        return [
            entry
            for entry in batch.entries
            if entry.status == EDITABLE_STATUS
            and (not task or task in _normalized(entry.task_name))
            and (not hint or hint in _normalized(entry.description))
        ]

    async def _prepare_for_entry(
        self,
        teams_user_id: str,
        session: OrbitSession,
        employee: OrbitEmployee,
        entry: OrbitTimesheetEntry,
        request: ParsedEntryChangeRequest,
    ) -> PendingEntryChange:
        if request.action == "delete":
            return await self._build_delete(teams_user_id, employee, entry)
        return await self._build_update(
            teams_user_id,
            session,
            employee,
            entry,
            entry_date=request.new_entry_date or entry.entry_date,
            duration_minutes=(
                request.new_duration_minutes
                if request.new_duration_minutes is not None
                else entry.duration_minutes
            ),
            description=request.new_description or entry.description,
            project_id=None,
            project_name=request.new_project_name,
            task_name=request.new_task_name,
        )

    async def _build_delete(
        self, teams_user_id: str, employee: OrbitEmployee, entry: OrbitTimesheetEntry
    ) -> PendingEntryChange:
        return await self._save_change(
            PendingEntryChange(
                id=uuid4().hex,
                kind="delete",
                teams_user_id=teams_user_id,
                employee_id=employee.id,
                organization_id=employee.organization_id,
                before=entry,
                after=None,
                expires_at=self._expires_at(),
            )
        )

    async def _build_update(
        self,
        teams_user_id: str,
        session: OrbitSession,
        employee: OrbitEmployee,
        entry: OrbitTimesheetEntry,
        *,
        entry_date: date,
        duration_minutes: int,
        description: str,
        project_id: str | None,
        project_name: str | None,
        task_name: str | None,
    ) -> PendingEntryChange:
        if duration_minutes <= 0 or duration_minutes > self._max_duration_minutes:
            raise OrbitValidationError(
                f"Duration must be between 1 and {self._max_duration_minutes} minutes."
            )
        notes = description.strip()
        if not notes:
            raise OrbitValidationError("Task notes are required.")
        if len(notes) > self._max_notes_chars:
            raise OrbitValidationError(
                f"Task notes must be {self._max_notes_chars} characters or fewer."
            )

        projects = await self._timesheets.list_assigned_projects(
            session.access_token, employee.id
        )
        project = self._resolve_project(
            projects, entry, project_id=project_id, project_name=project_name
        )

        requested_task = (task_name or "").strip()
        if not requested_task and project.id != entry.project_id:
            raise OrbitValidationError("When changing the project, also name the task.")
        if not requested_task or (
            project.id == entry.project_id
            and requested_task.casefold() == entry.task_name.casefold()
        ):
            task_id, resolved_task_name = entry.task_id, entry.task_name
        else:
            tasks = await self._timesheets.list_active_tasks(session.access_token, project.id)
            task = OrbitAddEntryService.match_entity("task", requested_task, tasks)
            task_id, resolved_task_name = task.id, task.name

        change = PendingEntryChange(
            id=uuid4().hex,
            kind="update",
            teams_user_id=teams_user_id,
            employee_id=employee.id,
            organization_id=employee.organization_id,
            before=entry,
            after=EditedEntryValues(
                project_id=project.id,
                project_name=project.name,
                task_id=task_id,
                task_name=resolved_task_name,
                entry_date=entry_date,
                duration_minutes=duration_minutes,
                description=notes,
            ),
            expires_at=self._expires_at(),
        )
        if not change.changed_fields:
            raise OrbitValidationError("No changes were made to this entry.")
        return await self._save_change(change)

    @staticmethod
    def _resolve_project(
        projects: Sequence[OrbitProject],
        entry: OrbitTimesheetEntry,
        *,
        project_id: str | None,
        project_name: str | None,
    ) -> OrbitProject:
        if project_name:
            return OrbitAddEntryService.match_entity("project", project_name, projects)
        wanted = project_id or entry.project_id
        project = next((item for item in projects if item.id == wanted), None)
        if project is None:
            raise OrbitNotFoundError("The selected Orbit project is not assigned to you.")
        return project

    async def _save_change(self, change: PendingEntryChange) -> PendingEntryChange:
        await self._pending.save(change)
        await self._selections.delete(change.teams_user_id)
        return change

    def _expires_at(self) -> datetime:
        return self._now() + timedelta(seconds=self._confirmation_ttl_seconds)

    def _lock_for(self, teams_user_id: str) -> asyncio.Lock:
        return self._locks.setdefault(teams_user_id, asyncio.Lock())

    async def _identity(self, teams_user_id: str) -> tuple[OrbitSession, OrbitEmployee]:
        session, auth_user_id = await self._auth.get_authenticated_session(teams_user_id)
        employee = await self._timesheets.get_employee(session.access_token, auth_user_id)
        return session, employee

    async def _load_draft(
        self,
        session: OrbitSession,
        employee: OrbitEmployee,
        entry_id: str,
    ) -> OrbitTimesheetEntry:
        try:
            UUID(entry_id)
        except (ValueError, AttributeError, TypeError) as exc:
            raise OrbitValidationError("This Orbit entry reference is invalid.") from exc
        entry = await self._timesheets.get_entry(
            session.access_token,
            employee_id=employee.id,
            entry_id=entry_id,
        )
        if entry is None:
            raise OrbitNotFoundError("This Orbit entry was not found.")
        if entry.status != EDITABLE_STATUS:
            raise OrbitValidationError(
                f"Only draft entries can be edited or deleted. This entry is "
                f"{entry.status}."
            )
        if not entry.project_id or not entry.task_id:
            raise OrbitValidationError("Orbit did not return the project and task for this entry.")
        return entry
