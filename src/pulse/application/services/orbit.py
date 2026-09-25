from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pulse.application.errors import (
    OrbitAmbiguousMatchError,
    OrbitAuthenticationError,
    OrbitAuthRequiredError,
    OrbitNotFoundError,
    OrbitValidationError,
)
from pulse.application.orbit_models import (
    CreatedTimesheetEntry,
    OrbitEmployee,
    OrbitProject,
    OrbitSession,
    OrbitTask,
    PendingTimesheetEntry,
)
from pulse.application.ports.orbit import (
    OrbitAuthPort,
    OrbitSessionStore,
    OrbitTimesheetPort,
    PendingEntryStore,
    TimesheetDraftParser,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


class OrbitAuthService:
    def __init__(
        self,
        auth_port: OrbitAuthPort,
        session_store: OrbitSessionStore,
        *,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._auth = auth_port
        self._sessions = session_store
        self._now = now

    async def login(self, teams_user_id: str, email: str, password: str) -> None:
        if not email.strip() or not password:
            raise OrbitValidationError("Email and password are required.")
        session = await self._auth.sign_in(email.strip(), password)
        await self._sessions.save(teams_user_id, session, reset_ttl=True)

    async def get_valid_session(self, teams_user_id: str) -> OrbitSession:
        session = await self._sessions.get(teams_user_id)
        if not session:
            raise OrbitAuthRequiredError("Your Orbit account is not linked.")
        if session.expires_at > self._now() + timedelta(seconds=60):
            return session
        return await self._refresh(teams_user_id, session)

    async def get_authenticated_session(self, teams_user_id: str) -> tuple[OrbitSession, str]:
        session = await self.get_valid_session(teams_user_id)
        try:
            user_id = await self._auth.get_authenticated_user_id(session.access_token)
            return session, user_id
        except OrbitAuthenticationError:
            session = await self._refresh(teams_user_id, session)
            user_id = await self._auth.get_authenticated_user_id(session.access_token)
            return session, user_id

    async def logout(self, teams_user_id: str) -> None:
        await self._sessions.delete(teams_user_id)

    async def _refresh(self, teams_user_id: str, session: OrbitSession) -> OrbitSession:
        try:
            refreshed = await self._auth.refresh(session.refresh_token)
        except OrbitAuthenticationError:
            await self._sessions.delete(teams_user_id)
            raise OrbitAuthRequiredError(
                "Your Orbit session expired. Please sign in again."
            ) from None
        await self._sessions.save(teams_user_id, refreshed, reset_ttl=False)
        return refreshed


class OrbitAddEntryService:
    def __init__(
        self,
        auth_service: OrbitAuthService,
        timesheet_port: OrbitTimesheetPort,
        draft_parser: TimesheetDraftParser,
        pending_store: PendingEntryStore,
        *,
        confirmation_ttl_seconds: int,
        business_timezone: str,
        max_duration_minutes: int,
        max_notes_chars: int,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._auth = auth_service
        self._timesheets = timesheet_port
        self._parser = draft_parser
        self._pending = pending_store
        self._confirmation_ttl_seconds = confirmation_ttl_seconds
        self._max_duration_minutes = max_duration_minutes
        self._max_notes_chars = max_notes_chars
        self._now = now
        self._confirmation_locks: dict[str, asyncio.Lock] = {}
        try:
            self._timezone = ZoneInfo(business_timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown Orbit business timezone: {business_timezone}") from exc

    async def prepare(self, teams_user_id: str, request_text: str) -> PendingTimesheetEntry:
        session, auth_user_id = await self._auth.get_authenticated_session(teams_user_id)
        employee = await self._timesheets.get_employee(session.access_token, auth_user_id)
        today = self._now().astimezone(self._timezone).date()
        parsed = await self._parser.parse(request_text, today=today)
        self._validate_parsed(parsed.duration_minutes, parsed.description)

        projects = await self._timesheets.list_assigned_projects(
            session.access_token, employee.id
        )
        project = self._match_entity("project", parsed.project_name, projects)
        tasks = await self._timesheets.list_active_tasks(session.access_token, project.id)
        task = self._match_entity("task", parsed.task_name, tasks)

        entry = PendingTimesheetEntry(
            id=uuid4().hex,
            teams_user_id=teams_user_id,
            employee_id=employee.id,
            organization_id=employee.organization_id,
            project_id=project.id,
            project_name=project.name,
            task_id=task.id,
            task_name=task.name,
            entry_date=parsed.entry_date,
            duration_minutes=parsed.duration_minutes,
            description=parsed.description.strip(),
            expires_at=self._now() + timedelta(seconds=self._confirmation_ttl_seconds),
        )
        await self._pending.save(entry)
        return entry

    async def confirm(
        self,
        teams_user_id: str,
        expected_pending_id: str | None = None,
    ) -> CreatedTimesheetEntry:
        lock = self._confirmation_locks.setdefault(teams_user_id, asyncio.Lock())
        async with lock:
            entry = await self._pending.get(teams_user_id)
            if not entry:
                raise OrbitNotFoundError("There is no pending Orbit entry to confirm.")
            if expected_pending_id is not None and entry.id != expected_pending_id:
                raise OrbitNotFoundError(
                    "This confirmation card is no longer current. Create a new Orbit draft."
                )
            if entry.expires_at <= self._now():
                await self._pending.delete(teams_user_id)
                raise OrbitValidationError(
                    "The pending Orbit entry expired. Please create it again."
                )

            session, auth_user_id = await self._auth.get_authenticated_session(teams_user_id)
            employee = await self._timesheets.get_employee(session.access_token, auth_user_id)
            self._verify_same_employee(entry, employee)

            # Claim the draft before the network mutation. A timeout therefore cannot
            # be retried automatically and accidentally create a duplicate entry.
            await self._pending.delete(teams_user_id)
            return await self._timesheets.create_entry(session.access_token, entry)

    async def cancel(
        self,
        teams_user_id: str,
        expected_pending_id: str | None = None,
    ) -> bool:
        lock = self._confirmation_locks.setdefault(teams_user_id, asyncio.Lock())
        async with lock:
            entry = await self._pending.get(teams_user_id)
            if expected_pending_id is not None:
                if not entry:
                    raise OrbitNotFoundError("There is no pending Orbit entry to cancel.")
                if entry.id != expected_pending_id:
                    raise OrbitNotFoundError(
                        "This confirmation card is no longer current. Create a new Orbit draft."
                    )
            await self._pending.delete(teams_user_id)
            return entry is not None

    def _validate_parsed(self, duration_minutes: int, description: str) -> None:
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

    @staticmethod
    def _match_entity(
        entity: str,
        requested_name: str,
        options: Sequence[OrbitProject] | Sequence[OrbitTask],
    ) -> OrbitProject | OrbitTask:
        needle = requested_name.strip()
        needle_tokens = OrbitAddEntryService._name_tokens(needle)
        if not needle_tokens:
            raise OrbitValidationError(f"Please specify an Orbit {entity}.")

        normalized_needle = " ".join(needle_tokens)
        normalized_options = [
            (item, OrbitAddEntryService._name_tokens(item.name)) for item in options
        ]
        exact = [
            item
            for item, tokens in normalized_options
            if " ".join(tokens) == normalized_needle
        ]
        if len(exact) == 1:
            return exact[0]

        needle_set = set(needle_tokens)
        contained = [
            item
            for item, tokens in normalized_options
            if needle_set.issubset(set(tokens))
        ]
        if len(contained) == 1:
            return contained[0]
        if len(contained) > 1:
            raise OrbitAmbiguousMatchError(entity, [item.name for item in contained[:8]])

        ranked = sorted(
            (
                (OrbitAddEntryService._name_similarity(needle_tokens, tokens), item)
                for item, tokens in normalized_options
            ),
            key=lambda match: match[0],
            reverse=True,
        )
        if ranked and ranked[0][0] >= 0.72:
            close = [item for score, item in ranked if score >= ranked[0][0] - 0.08]
            if len(close) == 1:
                return ranked[0][1]
            raise OrbitAmbiguousMatchError(entity, [item.name for item in close[:8]])

        available = ", ".join(item.name for item in options[:8]) or "none"
        raise OrbitNotFoundError(
            f"Orbit {entity} '{requested_name}' was not found. Available: {available}."
        )

    @staticmethod
    def _name_tokens(value: str) -> tuple[str, ...]:
        return tuple(
            OrbitAddEntryService._singularize(token)
            for token in re.findall(r"[a-z0-9]+", value.casefold())
        )

    @staticmethod
    def _singularize(token: str) -> str:
        if len(token) > 4 and token.endswith("ies"):
            return f"{token[:-3]}y"
        if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
            return token[:-1]
        return token

    @staticmethod
    def _name_similarity(requested: tuple[str, ...], candidate: tuple[str, ...]) -> float:
        if not requested or not candidate:
            return 0.0
        phrase_score = SequenceMatcher(None, " ".join(requested), " ".join(candidate)).ratio()
        token_scores = [
            max(
                OrbitAddEntryService._token_similarity(token, option)
                for option in candidate
            )
            for token in requested
        ]
        return max(phrase_score, sum(token_scores) / len(token_scores) * 0.9)

    @staticmethod
    def _token_similarity(left: str, right: str) -> float:
        if left == right:
            return 1.0
        if min(len(left), len(right)) >= 3 and (
            left.startswith(right) or right.startswith(left)
        ):
            return 0.9
        return SequenceMatcher(None, left, right).ratio()

    @staticmethod
    def _verify_same_employee(
        entry: PendingTimesheetEntry, employee: OrbitEmployee
    ) -> None:
        if (
            entry.employee_id != employee.id
            or entry.organization_id != employee.organization_id
        ):
            raise OrbitAuthenticationError(
                "The Orbit identity changed. Please create the entry again."
            )
