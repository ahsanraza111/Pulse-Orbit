from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import TypeVar
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pulse.application.errors import (
    OrbitAmbiguousMatchError,
    OrbitAuthenticationError,
    OrbitMutationUncertainError,
    OrbitNotFoundError,
    OrbitProviderError,
    OrbitValidationError,
)
from pulse.application.orbit_models import (
    BulkCreationResult,
    BulkEntity,
    BulkEntitySelectionRequest,
    BulkEntryOutcome,
    BulkPreparation,
    BulkSchedule,
    BulkScheduleSelectionRequest,
    BulkSelectionOption,
    OrbitEmployee,
    OrbitProject,
    OrbitTask,
    ParsedBulkEntryTemplate,
    PendingTimesheetBatch,
    PendingTimesheetEntry,
    ResolvedBulkEntryTemplate,
)
from pulse.application.ports.orbit import (
    BulkPreparationStore,
    OrbitTimesheetPort,
    PendingBatchStore,
    TimesheetBatchParser,
)
from pulse.application.services.orbit import OrbitAddEntryService, OrbitAuthService, utc_now

BulkPreparationResult = (
    PendingTimesheetBatch | BulkScheduleSelectionRequest | BulkEntitySelectionRequest
)
BulkOption = TypeVar("BulkOption", OrbitProject, OrbitTask)


class OrbitBulkEntryService:
    def __init__(
        self,
        auth_service: OrbitAuthService,
        timesheet_port: OrbitTimesheetPort,
        parser: TimesheetBatchParser,
        preparation_store: BulkPreparationStore,
        pending_store: PendingBatchStore,
        *,
        confirmation_ttl_seconds: int,
        business_timezone: str,
        max_duration_minutes: int,
        max_notes_chars: int,
        max_templates: int,
        max_range_days: int,
        max_generated_entries: int,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._auth = auth_service
        self._timesheets = timesheet_port
        self._parser = parser
        self._preparations = preparation_store
        self._pending = pending_store
        self._confirmation_ttl_seconds = confirmation_ttl_seconds
        self._max_duration_minutes = max_duration_minutes
        self._max_notes_chars = max_notes_chars
        self._max_templates = max_templates
        self._max_range_days = max_range_days
        self._max_generated_entries = max_generated_entries
        self._now = now
        self._confirmation_locks: dict[str, asyncio.Lock] = {}
        try:
            self._timezone = ZoneInfo(business_timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown Orbit business timezone: {business_timezone}") from exc

    async def prepare(self, teams_user_id: str, request_text: str) -> BulkPreparationResult:
        await self.cancel(teams_user_id)
        session, auth_user_id = await self._auth.get_authenticated_session(teams_user_id)
        employee = await self._timesheets.get_employee(session.access_token, auth_user_id)
        today = self._now().astimezone(self._timezone).date()
        parsed = await self._parser.parse(request_text, today=today)
        self._validate_request(parsed.entries, parsed.start_date, parsed.end_date)

        schedule = parsed.schedule
        if parsed.start_date == parsed.end_date:
            schedule = "single_day"
        elif schedule == "single_day":
            raise OrbitValidationError(
                "A multi-day range must use weekdays or every-day repetition."
            )

        preparation = BulkPreparation(
            id=uuid4().hex,
            teams_user_id=teams_user_id,
            employee_id=employee.id,
            organization_id=employee.organization_id,
            parsed=parsed,
            schedule=schedule,
            project_choices=(),
            task_choices=(),
            awaiting_entity=None,
            awaiting_template_index=None,
            allowed_option_ids=(),
            expires_at=self._expiry(),
        )
        await self._preparations.save(preparation)
        if schedule is None:
            return self._schedule_request(preparation)
        projects = await self._timesheets.list_assigned_projects(
            session.access_token, employee.id
        )
        return await self._continue_preparation(
            preparation, session.access_token, employee, projects
        )

    async def select_schedule(
        self,
        teams_user_id: str,
        preparation_id: str,
        schedule: BulkSchedule,
    ) -> BulkPreparationResult:
        if schedule not in {"weekdays", "all_days"}:
            raise OrbitValidationError("Choose weekdays or every day for this date range.")
        preparation = await self._load_preparation(teams_user_id, preparation_id)
        if preparation.parsed.start_date == preparation.parsed.end_date:
            raise OrbitValidationError("A one-day batch does not need a repeat schedule.")
        preparation = replace(
            preparation,
            schedule=schedule,
            awaiting_entity=None,
            awaiting_template_index=None,
            allowed_option_ids=(),
        )
        await self._preparations.save(preparation)
        return await self._resume(preparation)

    async def select_entity(
        self,
        teams_user_id: str,
        preparation_id: str,
        *,
        entity: BulkEntity,
        template_index: int,
        option_id: str,
    ) -> BulkPreparationResult:
        preparation = await self._load_preparation(teams_user_id, preparation_id)
        if (
            preparation.awaiting_entity != entity
            or preparation.awaiting_template_index != template_index
            or option_id not in preparation.allowed_option_ids
        ):
            raise OrbitValidationError(
                "This bulk selection is no longer current. Start the batch again."
            )

        project_choices = dict(preparation.project_choices)
        task_choices = dict(preparation.task_choices)
        if entity == "project":
            project_choices[template_index] = option_id
            task_choices.pop(template_index, None)
        else:
            task_choices[template_index] = option_id
        preparation = replace(
            preparation,
            project_choices=tuple(sorted(project_choices.items())),
            task_choices=tuple(sorted(task_choices.items())),
            awaiting_entity=None,
            awaiting_template_index=None,
            allowed_option_ids=(),
        )
        await self._preparations.save(preparation)
        return await self._resume(preparation)

    async def confirm(
        self,
        teams_user_id: str,
        expected_batch_id: str | None = None,
    ) -> BulkCreationResult:
        lock = self._confirmation_locks.setdefault(teams_user_id, asyncio.Lock())
        async with lock:
            batch = await self._pending.get(teams_user_id)
            if not batch:
                raise OrbitNotFoundError("There is no pending Orbit bulk entry to confirm.")
            if expected_batch_id is not None and batch.id != expected_batch_id:
                raise OrbitNotFoundError(
                    "This bulk confirmation card is no longer current. Start the batch again."
                )
            if batch.expires_at <= self._now():
                await self._pending.delete(teams_user_id)
                raise OrbitValidationError(
                    "The pending Orbit bulk entry expired. Please create it again."
                )

            session, auth_user_id = await self._auth.get_authenticated_session(teams_user_id)
            employee = await self._timesheets.get_employee(session.access_token, auth_user_id)
            self._verify_employee(batch, employee)

            # Claim before the first mutation so repeated confirmation can never replay
            # entries whose provider outcome may already be final or uncertain.
            await self._pending.delete(teams_user_id)
            outcomes: list[BulkEntryOutcome] = []
            for entry in batch.entries:
                try:
                    created = await self._timesheets.create_entry(
                        session.access_token, entry
                    )
                    outcomes.append(
                        BulkEntryOutcome(
                            entry=entry,
                            status="created",
                            reference=created.id,
                        )
                    )
                except OrbitMutationUncertainError:
                    outcomes.append(
                        BulkEntryOutcome(
                            entry=entry,
                            status="uncertain",
                            message="Check Orbit before retrying this entry.",
                        )
                    )
                except (OrbitProviderError, OrbitAuthenticationError):
                    outcomes.append(
                        BulkEntryOutcome(
                            entry=entry,
                            status="failed",
                            message="Orbit rejected or could not complete this entry.",
                        )
                    )
            return BulkCreationResult(batch_id=batch.id, outcomes=tuple(outcomes))

    async def cancel(
        self,
        teams_user_id: str,
        expected_batch_id: str | None = None,
    ) -> bool:
        lock = self._confirmation_locks.setdefault(teams_user_id, asyncio.Lock())
        async with lock:
            batch = await self._pending.get(teams_user_id)
            preparation = await self._preparations.get(teams_user_id)
            if expected_batch_id is not None:
                if not batch:
                    raise OrbitNotFoundError("There is no pending Orbit bulk entry to cancel.")
                if batch.id != expected_batch_id:
                    raise OrbitNotFoundError(
                        "This bulk confirmation card is no longer current."
                    )
            await self._pending.delete(teams_user_id)
            await self._preparations.delete(teams_user_id)
            return batch is not None or preparation is not None

    async def _resume(self, preparation: BulkPreparation) -> BulkPreparationResult:
        session, auth_user_id = await self._auth.get_authenticated_session(
            preparation.teams_user_id
        )
        employee = await self._timesheets.get_employee(session.access_token, auth_user_id)
        self._verify_preparation_employee(preparation, employee)
        projects = await self._timesheets.list_assigned_projects(
            session.access_token, employee.id
        )
        return await self._continue_preparation(
            preparation, session.access_token, employee, projects
        )

    async def _continue_preparation(
        self,
        preparation: BulkPreparation,
        access_token: str,
        employee: OrbitEmployee,
        projects: Sequence[OrbitProject],
    ) -> BulkPreparationResult:
        if preparation.schedule is None:
            return self._schedule_request(preparation)

        project_choices = dict(preparation.project_choices)
        task_choices = dict(preparation.task_choices)
        resolved: list[ResolvedBulkEntryTemplate] = []
        task_cache: dict[str, Sequence[OrbitTask]] = {}

        for index, template in enumerate(preparation.parsed.entries):
            project = self._chosen_option(project_choices.get(index), projects)
            if project is None:
                try:
                    project = OrbitAddEntryService.match_entity(
                        "project", template.project_name, projects
                    )
                except OrbitAmbiguousMatchError as exc:
                    return await self._request_entity_selection(
                        preparation,
                        "project",
                        index,
                        template.project_name,
                        projects,
                        exc.options,
                    )

            tasks = task_cache.get(project.id)
            if tasks is None:
                tasks = await self._timesheets.list_active_tasks(access_token, project.id)
                task_cache[project.id] = tasks
            task = self._chosen_option(task_choices.get(index), tasks)
            if task is None:
                try:
                    task = OrbitAddEntryService.match_entity(
                        "task", template.task_name, tasks
                    )
                except OrbitAmbiguousMatchError as exc:
                    return await self._request_entity_selection(
                        preparation,
                        "task",
                        index,
                        template.task_name,
                        tasks,
                        exc.options,
                    )

            resolved.append(
                ResolvedBulkEntryTemplate(
                    project_id=project.id,
                    project_name=project.name,
                    task_id=task.id,
                    task_name=task.name,
                    duration_minutes=template.duration_minutes,
                    description=template.description.strip(),
                )
            )

        self._reject_duplicate_templates(resolved)
        dates = self._generate_dates(
            preparation.parsed.start_date,
            preparation.parsed.end_date,
            preparation.schedule,
        )
        generated_count = len(dates) * len(resolved)
        if generated_count > self._max_generated_entries:
            raise OrbitValidationError(
                f"This request generates {generated_count} entries; the maximum is "
                f"{self._max_generated_entries}."
            )
        daily_minutes = sum(item.duration_minutes for item in resolved)
        if daily_minutes > self._max_duration_minutes:
            raise OrbitValidationError(
                "The generated daily duration exceeds the configured Orbit daily limit."
            )

        expires_at = self._expiry()
        entries = tuple(
            PendingTimesheetEntry(
                id=uuid4().hex,
                teams_user_id=preparation.teams_user_id,
                employee_id=employee.id,
                organization_id=employee.organization_id,
                project_id=template.project_id,
                project_name=template.project_name,
                task_id=template.task_id,
                task_name=template.task_name,
                entry_date=entry_date,
                duration_minutes=template.duration_minutes,
                description=template.description,
                expires_at=expires_at,
            )
            for entry_date in dates
            for template in resolved
        )
        batch = PendingTimesheetBatch(
            id=uuid4().hex,
            teams_user_id=preparation.teams_user_id,
            employee_id=employee.id,
            organization_id=employee.organization_id,
            start_date=preparation.parsed.start_date,
            end_date=preparation.parsed.end_date,
            schedule=preparation.schedule,
            dates=dates,
            templates=tuple(resolved),
            entries=entries,
            expires_at=expires_at,
        )
        await self._pending.save(batch)
        await self._preparations.delete(preparation.teams_user_id)
        return batch

    async def _request_entity_selection(
        self,
        preparation: BulkPreparation,
        entity: BulkEntity,
        template_index: int,
        requested_name: str,
        live_options: Sequence[OrbitProject] | Sequence[OrbitTask],
        matched_names: Sequence[str],
    ) -> BulkEntitySelectionRequest:
        names = set(matched_names)
        options = tuple(
            BulkSelectionOption(id=item.id, name=item.name)
            for item in live_options
            if item.name in names
        )
        updated = replace(
            preparation,
            awaiting_entity=entity,
            awaiting_template_index=template_index,
            allowed_option_ids=tuple(option.id for option in options),
        )
        await self._preparations.save(updated)
        return BulkEntitySelectionRequest(
            preparation_id=preparation.id,
            entity=entity,
            template_index=template_index,
            requested_name=requested_name,
            options=options,
        )

    async def _load_preparation(
        self, teams_user_id: str, preparation_id: str
    ) -> BulkPreparation:
        preparation = await self._preparations.get(teams_user_id)
        if not preparation or preparation.id != preparation_id:
            raise OrbitNotFoundError(
                "This bulk-entry selection is no longer current. Start the batch again."
            )
        if preparation.expires_at <= self._now():
            await self._preparations.delete(teams_user_id)
            raise OrbitValidationError(
                "The bulk-entry preparation expired. Please create it again."
            )
        return preparation

    def _validate_request(
        self,
        entries: Sequence[ParsedBulkEntryTemplate],
        start_date: date,
        end_date: date,
    ) -> None:
        if len(entries) > self._max_templates:
            raise OrbitValidationError(
                f"A bulk request can contain at most {self._max_templates} entry templates."
            )
        if end_date < start_date:
            raise OrbitValidationError("The bulk end date cannot be before its start date.")
        range_days = (end_date - start_date).days + 1
        if range_days > self._max_range_days:
            raise OrbitValidationError(
                f"A bulk date range cannot exceed {self._max_range_days} days."
            )
        for index, entry in enumerate(entries, start=1):
            if entry.duration_minutes <= 0 or entry.duration_minutes > self._max_duration_minutes:
                raise OrbitValidationError(
                    f"Bulk entry {index} duration must be between 1 and "
                    f"{self._max_duration_minutes} minutes."
                )
            if not entry.project_name.strip() or not entry.task_name.strip():
                raise OrbitValidationError(
                    f"Bulk entry {index} requires a project and task."
                )
            notes = entry.description.strip()
            if not notes:
                raise OrbitValidationError(f"Bulk entry {index} requires task notes.")
            if len(notes) > self._max_notes_chars:
                raise OrbitValidationError(
                    f"Bulk entry {index} notes must be {self._max_notes_chars} characters "
                    "or fewer."
                )

    @staticmethod
    def _chosen_option(
        option_id: str | None,
        options: Sequence[BulkOption],
    ) -> BulkOption | None:
        if not option_id:
            return None
        selected = next((item for item in options if item.id == option_id), None)
        if selected is None:
            raise OrbitValidationError(
                "A previously selected Orbit project or task is no longer available."
            )
        return selected

    @staticmethod
    def _reject_duplicate_templates(entries: Sequence[ResolvedBulkEntryTemplate]) -> None:
        seen: set[tuple[str, str, int, str]] = set()
        for entry in entries:
            signature = (
                entry.project_id,
                entry.task_id,
                entry.duration_minutes,
                entry.description.casefold(),
            )
            if signature in seen:
                raise OrbitValidationError(
                    "The bulk request contains an exact duplicate entry template."
                )
            seen.add(signature)

    @staticmethod
    def _generate_dates(
        start_date: date,
        end_date: date,
        schedule: BulkSchedule,
    ) -> tuple[date, ...]:
        days = (end_date - start_date).days + 1
        dates = tuple(start_date + timedelta(days=offset) for offset in range(days))
        if schedule == "weekdays":
            dates = tuple(item for item in dates if item.weekday() < 5)
        if not dates:
            raise OrbitValidationError(
                "The selected schedule does not contain any eligible dates."
            )
        return dates

    @staticmethod
    def _schedule_request(preparation: BulkPreparation) -> BulkScheduleSelectionRequest:
        return BulkScheduleSelectionRequest(
            preparation_id=preparation.id,
            start_date=preparation.parsed.start_date,
            end_date=preparation.parsed.end_date,
            template_count=len(preparation.parsed.entries),
        )

    def _expiry(self) -> datetime:
        return self._now() + timedelta(seconds=self._confirmation_ttl_seconds)

    @staticmethod
    def _verify_preparation_employee(
        preparation: BulkPreparation, employee: OrbitEmployee
    ) -> None:
        if (
            preparation.employee_id != employee.id
            or preparation.organization_id != employee.organization_id
        ):
            raise OrbitAuthenticationError(
                "The Orbit identity changed. Please create the bulk entry again."
            )

    @staticmethod
    def _verify_employee(batch: PendingTimesheetBatch, employee: OrbitEmployee) -> None:
        if (
            batch.employee_id != employee.id
            or batch.organization_id != employee.organization_id
        ):
            raise OrbitAuthenticationError(
                "The Orbit identity changed. Please create the bulk entry again."
            )
