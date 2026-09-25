from __future__ import annotations

import asyncio
import logging
import re
from collections import OrderedDict
from collections.abc import Mapping
from datetime import date
from typing import Any, Literal

from fastapi import FastAPI
from microsoft_teams.api import (
    AdaptiveCardActionCardResponse,
    CardAction,
    MessageActivityInput,
    SuggestedActions,
)
from microsoft_teams.apps import App, FastAPIAdapter
from microsoft_teams.cards import (
    ActionSet,
    AdaptiveCard,
    Container,
    ExecuteAction,
    Fact,
    FactSet,
    TextBlock,
    TextInput,
)

from pulse.application.errors import (
    InvalidMessageError,
    OrbitAmbiguousMatchError,
    OrbitAuthenticationError,
    OrbitAuthRequiredError,
    OrbitMutationUncertainError,
    OrbitNotFoundError,
    OrbitProviderError,
    OrbitValidationError,
    ProviderError,
)
from pulse.application.orbit_models import (
    CreatedTimesheetEntry,
    OrbitTimesheetEntry,
    PendingTimesheetEntry,
    ResolvedTimesheetQuery,
    TimesheetEntryPage,
)
from pulse.application.services.chat import ChatService
from pulse.application.services.orbit import (
    OrbitAddEntryService,
    OrbitAuthService,
    OrbitViewEntriesService,
)
from pulse.core.config import Settings

logger = logging.getLogger(__name__)
MENTION_PATTERN = re.compile(r"<at>.*?</at>", flags=re.IGNORECASE | re.DOTALL)
PASSWORD_VALUE_PATTERN = re.compile(
    r"\b(?:orbit\s+)?pass(?:word)?\s*(?::|=|\bis\b)\s*\S+",
    flags=re.IGNORECASE,
)
ORBIT_LOGIN_ACTION = "orbit_login"
ORBIT_CONFIRM_ACTION = "orbit_confirm"
ORBIT_CANCEL_ACTION = "orbit_cancel"
ORBIT_VIEW_PAGE_ACTION = "orbit_view_page"
ORBIT_VIEW_DETAIL_ACTION = "orbit_view_detail"
TIME_ENTRY_ACTION_WORDS = {"add", "create", "enter", "log", "record", "worked"}
TIME_ENTRY_DETAIL_WORDS = {
    "entry",
    "hour",
    "hours",
    "minute",
    "minutes",
    "time",
    "timesheet",
}
TIME_ENTRY_VIEW_WORDS = {"check", "display", "find", "get", "list", "see", "show", "view"}
TIME_ENTRY_VIEW_TARGETS = {"entries", "entry", "hours", "time", "timesheet"}


class ComposeCardAction(CardAction):
    """Teams Action.Compose, pending a typed SDK model for this action."""

    type: Literal["Action.Compose"] = "Action.Compose"


class ComposeSuggestedActions(SuggestedActions):
    actions: list[ComposeCardAction]


class ComposeMessageActivity(MessageActivityInput):
    suggested_actions: ComposeSuggestedActions


def clean_teams_text(text: str | None) -> str:
    return MENTION_PATTERN.sub("", text or "").strip()


def teams_user_id(ctx: Any) -> str:
    sender = getattr(ctx.activity, "from_", None)
    user_id = getattr(sender, "aad_object_id", None) or getattr(sender, "id", None)
    if not isinstance(user_id, str) or not user_id:
        raise OrbitAuthenticationError("The Teams user identity is unavailable.")
    return user_id


def orbit_login_card(*, email: str = "", error: str | None = None) -> AdaptiveCard:
    body = [
        TextBlock(text="Connect your Orbit account", size="Large", weight="Bolder"),
        TextBlock(
            text=(
                "Enter your Orbit credentials below. The password field is masked and "
                "will not be posted as a visible Teams chat message. PULSE uses it only "
                "for this sign-in attempt and does not store it."
            ),
            wrap=True,
        ),
    ]
    if error:
        body.append(TextBlock(text=error, color="Attention", wrap=True))
    body.extend(
        [
            TextInput(
                id="orbit_email",
                label="Orbit email",
                style="Email",
                value=email or None,
                is_required=True,
                error_message="Enter your Orbit email.",
                max_length=320,
            ),
            TextInput(
                id="orbit_password",
                label="Orbit password",
                style="Password",
                is_required=True,
                error_message="Enter your Orbit password.",
                max_length=4096,
            ),
        ]
    )
    return AdaptiveCard(
        body=body,
        actions=[
            ExecuteAction(
                title="Connect Orbit",
                verb=ORBIT_LOGIN_ACTION,
                data={"action": ORBIT_LOGIN_ACTION},
            )
        ],
    )


def orbit_login_completed_card() -> AdaptiveCard:
    return AdaptiveCard(
        body=[
            TextBlock(text="Orbit sign-in complete", size="Medium", weight="Bolder"),
            TextBlock(
                text="Continue with the new PULSE message below.",
                color="Good",
                wrap=True,
            ),
        ]
    )


def orbit_confirmation_card(entry: PendingTimesheetEntry) -> AdaptiveCard:
    return AdaptiveCard(
        body=[
            TextBlock(text="Confirm Orbit time entry", size="Large", weight="Bolder"),
            TextBlock(
                text="Review the resolved Orbit details before creating this draft entry.",
                wrap=True,
            ),
            FactSet(
                facts=[
                    Fact(title="Date", value=entry.entry_date.isoformat()),
                    Fact(title="Project", value=entry.project_name),
                    Fact(title="Task", value=entry.task_name),
                    Fact(
                        title="Duration",
                        value=TeamsMessageHandler._format_duration(entry.duration_minutes),
                    ),
                    Fact(title="Notes", value=entry.description),
                    Fact(title="Status", value="Draft"),
                ]
            ),
            TextBlock(
                text="Nothing will be created until you select **Confirm entry**.",
                wrap=True,
                spacing="Medium",
            ),
        ],
        actions=[
            ExecuteAction(
                title="Confirm entry",
                verb=ORBIT_CONFIRM_ACTION,
                data={"action": ORBIT_CONFIRM_ACTION, "pending_id": entry.id},
                style="positive",
            ),
            ExecuteAction(
                title="Cancel",
                verb=ORBIT_CANCEL_ACTION,
                data={"action": ORBIT_CANCEL_ACTION, "pending_id": entry.id},
                style="destructive",
            ),
        ],
    )


def orbit_status_card(title: str, message: str, *, success: bool = False) -> AdaptiveCard:
    return AdaptiveCard(
        body=[
            TextBlock(text=title, size="Medium", weight="Bolder"),
            TextBlock(
                text=message,
                color="Good" if success else "Attention",
                wrap=True,
            ),
        ]
    )


def orbit_entry_created_card(created: CreatedTimesheetEntry) -> AdaptiveCard:
    return orbit_status_card(
        "Orbit entry created",
        (
            f"Date: {created.entry_date.isoformat()}\n\n"
            f"Duration: {TeamsMessageHandler._format_duration(created.duration_minutes)}\n\n"
            f"Reference: `{created.id}`"
        ),
        success=True,
    )


def _query_action_data(query: ResolvedTimesheetQuery, action: str) -> dict[str, object]:
    data: dict[str, object] = {
        "action": action,
        "start_date": query.start_date.isoformat(),
        "end_date": query.end_date.isoformat(),
        "page": query.page,
    }
    if query.project_id:
        data["project_id"] = query.project_id
    if query.status:
        data["status"] = query.status
    return data


def _truncate_notes(notes: str, limit: int = 140) -> str:
    normalized = " ".join(notes.split())
    return normalized if len(normalized) <= limit else normalized[: limit - 1].rstrip() + "…"


def orbit_timesheet_list_card(page: TimesheetEntryPage) -> AdaptiveCard:
    query = page.query
    filters = [f"{query.start_date.isoformat()} to {query.end_date.isoformat()}"]
    if query.project_name:
        filters.append(f"Project: {query.project_name}")
    if query.status:
        filters.append(f"Status: {query.status.title()}")

    body: list[Any] = [
        TextBlock(text="Orbit timesheet", size="Large", weight="Bolder"),
        TextBlock(text=" · ".join(filters), wrap=True),
    ]
    if not page.entries:
        body.append(
            TextBlock(
                text="No timesheet entries were found for these filters.",
                wrap=True,
                spacing="Medium",
            )
        )
        return AdaptiveCard(body=body)

    first = query.page * query.page_size + 1
    last = first + len(page.entries) - 1
    page_total = sum(entry.duration_minutes for entry in page.entries)
    body.append(
        TextBlock(
            text=(
                f"Showing {first}-{last} of {page.total_count} · "
                f"Displayed total: {TeamsMessageHandler._format_duration(page_total)}"
            ),
            wrap=True,
            spacing="Small",
        )
    )
    for entry in page.entries:
        detail_data = _query_action_data(query, ORBIT_VIEW_DETAIL_ACTION)
        detail_data["entry_id"] = entry.id
        body.append(
            Container(
                separator=True,
                spacing="Medium",
                items=[
                    TextBlock(
                        text=(
                            f"**{entry.entry_date.isoformat()} · "
                            f"{TeamsMessageHandler._format_duration(entry.duration_minutes)} · "
                            f"{entry.status.title()}**"
                        ),
                        wrap=True,
                    ),
                    TextBlock(
                        text=f"{entry.project_name} — {entry.task_name}",
                        wrap=True,
                        spacing="Small",
                    ),
                    TextBlock(
                        text=f"Notes: {_truncate_notes(entry.description) or 'None'}",
                        wrap=True,
                        spacing="Small",
                    ),
                    ActionSet(
                        actions=[
                            ExecuteAction(
                                title="View details",
                                verb=ORBIT_VIEW_DETAIL_ACTION,
                                data=detail_data,
                            )
                        ]
                    ),
                ],
            )
        )

    actions = []
    if query.page > 0:
        previous_query = ResolvedTimesheetQuery(
            start_date=query.start_date,
            end_date=query.end_date,
            project_id=query.project_id,
            project_name=query.project_name,
            status=query.status,
            page=query.page - 1,
            page_size=query.page_size,
        )
        actions.append(
            ExecuteAction(
                title="Previous",
                verb=ORBIT_VIEW_PAGE_ACTION,
                data=_query_action_data(previous_query, ORBIT_VIEW_PAGE_ACTION),
            )
        )
    if last < page.total_count:
        next_query = ResolvedTimesheetQuery(
            start_date=query.start_date,
            end_date=query.end_date,
            project_id=query.project_id,
            project_name=query.project_name,
            status=query.status,
            page=query.page + 1,
            page_size=query.page_size,
        )
        actions.append(
            ExecuteAction(
                title="Next",
                verb=ORBIT_VIEW_PAGE_ACTION,
                data=_query_action_data(next_query, ORBIT_VIEW_PAGE_ACTION),
            )
        )
    return AdaptiveCard(body=body, actions=actions)


def orbit_timesheet_detail_card(
    entry: OrbitTimesheetEntry,
    query: ResolvedTimesheetQuery | None = None,
) -> AdaptiveCard:
    actions = []
    if query is not None:
        actions.append(
            ExecuteAction(
                title="Back to list",
                verb=ORBIT_VIEW_PAGE_ACTION,
                data=_query_action_data(query, ORBIT_VIEW_PAGE_ACTION),
            )
        )
    return AdaptiveCard(
        body=[
            TextBlock(text="Orbit entry details", size="Large", weight="Bolder"),
            FactSet(
                facts=[
                    Fact(title="Date", value=entry.entry_date.isoformat()),
                    Fact(title="Project", value=entry.project_name),
                    Fact(title="Task", value=entry.task_name),
                    Fact(
                        title="Duration",
                        value=TeamsMessageHandler._format_duration(entry.duration_minutes),
                    ),
                    Fact(title="Status", value=entry.status.title()),
                ]
            ),
            TextBlock(text="Notes", weight="Bolder", spacing="Medium"),
            TextBlock(text=entry.description or "No notes", wrap=True, spacing="Small"),
        ],
        actions=actions,
    )


def time_entry_suggestions_message() -> ComposeMessageActivity:
    templates = [
        (
            "Add today's entry",
            "orbit add 1 hour today on <project name> project, "
            "<task name> task. <work notes>",
        ),
        (
            "Add yesterday's entry",
            "orbit add 1 hour yesterday on <project name> project, "
            "<task name> task. <work notes>",
        ),
        (
            "Add entry for a date",
            "orbit add 1 hour on YYYY-MM-DD on <project name> project, "
            "<task name> task. <work notes>",
        ),
    ]
    actions = [
        ComposeCardAction(
            title=title,
            value={
                "type": "Teams.chatMessage",
                "data": {
                    "body": {
                        "contentType": "text",
                        "content": command,
                    }
                },
            },
        )
        for title, command in templates
    ]
    return ComposeMessageActivity(
        text=(
            "✅ Orbit account connected successfully.\n\n"
            "Choose a time-entry template below. Teams will place it in your message "
            "box so you can edit the duration, project, task, and notes before sending."
        ),
        suggested_actions=ComposeSuggestedActions(to=[], actions=actions),
    )


class OrbitLoginCardHandler:
    def __init__(self, auth_service: OrbitAuthService) -> None:
        self._auth = auth_service
        self._submission_locks: dict[str, asyncio.Lock] = {}
        self._completed_submissions: OrderedDict[str, None] = OrderedDict()
        self._background_tasks: set[asyncio.Task[None]] = set()

    async def __call__(self, ctx: Any) -> Any:
        data = getattr(getattr(ctx.activity.value, "action", None), "data", None)
        if not isinstance(data, Mapping):
            return AdaptiveCardActionCardResponse(
                value=orbit_login_card(error="The sign-in form was invalid. Please try again.")
            )

        email_value = data.get("orbit_email", "")
        password_value = data.get("orbit_password", "")
        email = email_value.strip() if isinstance(email_value, str) else ""
        password = password_value if isinstance(password_value, str) else ""
        submission_id = self._submission_id(ctx)

        try:
            lock = self._submission_locks.setdefault(submission_id, asyncio.Lock())
            async with lock:
                if submission_id not in self._completed_submissions:
                    await self._auth.login(teams_user_id(ctx), email, password)
                    self._remember_completed(submission_id)
                    self._schedule_success_message(ctx)
        except OrbitAuthenticationError:
            return AdaptiveCardActionCardResponse(
                value=orbit_login_card(
                    email=email,
                    error="Orbit rejected the email or password. Check both and try again.",
                )
            )
        except OrbitValidationError as exc:
            return AdaptiveCardActionCardResponse(
                value=orbit_login_card(email=email, error=str(exc))
            )
        except OrbitProviderError:
            return AdaptiveCardActionCardResponse(
                value=orbit_login_card(
                    email=email,
                    error="Orbit is temporarily unavailable. Please try again shortly.",
                )
            )
        except Exception:
            logger.exception("Unexpected error while processing the Orbit login card")
            return AdaptiveCardActionCardResponse(
                value=orbit_login_card(
                    email=email,
                    error="Something went wrong while connecting Orbit. Please try again.",
                )
            )
        finally:
            password = ""

        return AdaptiveCardActionCardResponse(value=orbit_login_completed_card())

    @staticmethod
    def _submission_id(ctx: Any) -> str:
        activity_id = getattr(ctx.activity, "id", None)
        if isinstance(activity_id, str) and activity_id:
            return activity_id
        return f"missing:{teams_user_id(ctx)}:{id(ctx.activity)}"

    def _remember_completed(self, submission_id: str) -> None:
        self._completed_submissions[submission_id] = None
        self._completed_submissions.move_to_end(submission_id)
        while len(self._completed_submissions) > 256:
            self._completed_submissions.popitem(last=False)

    def _schedule_success_message(self, ctx: Any) -> None:
        task = asyncio.create_task(self._send_success_message(ctx))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    @staticmethod
    async def _send_success_message(ctx: Any) -> None:
        try:
            await ctx.send(time_entry_suggestions_message())
        except Exception:
            logger.exception("Failed to send the Orbit login success message")


class OrbitConfirmCardHandler:
    def __init__(self, add_service: OrbitAddEntryService) -> None:
        self._add = add_service
        self._started: OrderedDict[str, None] = OrderedDict()
        self._background_tasks: set[asyncio.Task[None]] = set()

    async def __call__(self, ctx: Any) -> AdaptiveCardActionCardResponse:
        pending_id = self._pending_id(ctx)
        if not pending_id:
            return AdaptiveCardActionCardResponse(
                value=orbit_status_card(
                    "Confirmation failed",
                    "This confirmation card is invalid. Create a new Orbit draft.",
                )
            )

        current_teams_user_id = teams_user_id(ctx)
        operation_id = f"{current_teams_user_id}:{pending_id}"
        if operation_id not in self._started:
            self._remember_started(operation_id)
            task = asyncio.create_task(
                self._confirm_and_report(ctx, current_teams_user_id, pending_id)
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

        return AdaptiveCardActionCardResponse(
            value=orbit_status_card(
                "Creating Orbit entry",
                "Your confirmed entry is being submitted. PULSE will post the result here.",
                success=True,
            )
        )

    async def _confirm_and_report(
        self,
        ctx: Any,
        current_teams_user_id: str,
        pending_id: str,
    ) -> None:
        try:
            created = await self._add.confirm(current_teams_user_id, pending_id)
            result = orbit_entry_created_card(created)
        except OrbitAuthRequiredError:
            result = orbit_status_card(
                "Orbit entry not created",
                "Your Orbit account is not linked. Send `orbit login` and try again.",
            )
        except (OrbitValidationError, OrbitNotFoundError) as exc:
            result = orbit_status_card("Orbit entry not created", str(exc))
        except OrbitAuthenticationError:
            result = orbit_status_card(
                "Orbit entry not created",
                "Orbit authentication failed. Send `orbit login` and try again.",
            )
        except OrbitMutationUncertainError as exc:
            result = orbit_status_card("Orbit result needs verification", str(exc))
        except OrbitProviderError:
            result = orbit_status_card(
                "Orbit entry not created",
                "Orbit is temporarily unavailable or rejected the operation. Please try later.",
            )
        except Exception:
            logger.exception("Unexpected error while confirming an Orbit entry card")
            result = orbit_status_card(
                "Orbit entry not created",
                "Something went wrong while creating the entry. Please try again.",
            )

        try:
            await ctx.send(result)
        except Exception:
            logger.exception("Failed to send the Orbit confirmation result")

    @staticmethod
    def _pending_id(ctx: Any) -> str | None:
        data = getattr(getattr(ctx.activity.value, "action", None), "data", None)
        if not isinstance(data, Mapping):
            return None
        value = data.get("pending_id")
        return value if isinstance(value, str) and value else None

    def _remember_started(self, operation_id: str) -> None:
        self._started[operation_id] = None
        self._started.move_to_end(operation_id)
        while len(self._started) > 256:
            self._started.popitem(last=False)


class OrbitCancelCardHandler:
    def __init__(self, add_service: OrbitAddEntryService) -> None:
        self._add = add_service
        self._locks: dict[str, asyncio.Lock] = {}
        self._completed: OrderedDict[str, None] = OrderedDict()

    async def __call__(self, ctx: Any) -> AdaptiveCardActionCardResponse:
        pending_id = OrbitConfirmCardHandler._pending_id(ctx)
        if not pending_id:
            return AdaptiveCardActionCardResponse(
                value=orbit_status_card(
                    "Cancellation failed",
                    "This confirmation card is invalid. Create a new Orbit draft.",
                )
            )

        current_teams_user_id = teams_user_id(ctx)
        operation_id = f"{current_teams_user_id}:{pending_id}"
        lock = self._locks.setdefault(operation_id, asyncio.Lock())
        async with lock:
            if operation_id not in self._completed:
                try:
                    await self._add.cancel(current_teams_user_id, pending_id)
                except OrbitNotFoundError as exc:
                    return AdaptiveCardActionCardResponse(
                        value=orbit_status_card("Cancellation failed", str(exc))
                    )
                self._remember_completed(operation_id)

        return AdaptiveCardActionCardResponse(
            value=orbit_status_card(
                "Orbit entry cancelled",
                "The pending entry was discarded and was not sent to Orbit.",
                success=True,
            )
        )

    def _remember_completed(self, operation_id: str) -> None:
        self._completed[operation_id] = None
        self._completed.move_to_end(operation_id)
        while len(self._completed) > 256:
            self._completed.popitem(last=False)


def _resolved_query_from_action(data: Mapping[str, object]) -> ResolvedTimesheetQuery:
    start_value = data.get("start_date")
    end_value = data.get("end_date")
    page_value = data.get("page", 0)
    project_value = data.get("project_id")
    status_value = data.get("status")
    if (
        not isinstance(start_value, str)
        or not isinstance(end_value, str)
        or not isinstance(page_value, int)
        or isinstance(page_value, bool)
        or (project_value is not None and not isinstance(project_value, str))
        or (status_value is not None and not isinstance(status_value, str))
    ):
        raise OrbitValidationError("This timesheet navigation action is invalid.")
    try:
        start_date = date.fromisoformat(start_value)
        end_date = date.fromisoformat(end_value)
    except ValueError as exc:
        raise OrbitValidationError("This timesheet navigation action is invalid.") from exc
    return ResolvedTimesheetQuery(
        start_date=start_date,
        end_date=end_date,
        project_id=project_value or None,
        status=status_value.casefold() if status_value else None,
        page=page_value,
        page_size=1,
    )


def _orbit_read_error_card(exc: Exception) -> AdaptiveCard:
    if isinstance(exc, OrbitAuthRequiredError):
        message = "Your Orbit account is not linked. Send `orbit login` first."
    elif isinstance(exc, OrbitAuthenticationError):
        message = "Orbit authentication failed. Send `orbit login` and try again."
    elif isinstance(exc, OrbitAmbiguousMatchError):
        options = ", ".join(exc.options)
        message = f"Multiple Orbit {exc.entity} matches were found: {options}."
    elif isinstance(exc, OrbitValidationError | OrbitNotFoundError):
        message = str(exc)
    elif isinstance(exc, OrbitProviderError):
        message = "Orbit is temporarily unavailable. Please try again later."
    else:
        message = "Something went wrong while reading your Orbit timesheet."
    return orbit_status_card("Timesheet unavailable", message)


class OrbitViewPageCardHandler:
    def __init__(self, view_service: OrbitViewEntriesService) -> None:
        self._view = view_service
        self._started: OrderedDict[str, None] = OrderedDict()
        self._background_tasks: set[asyncio.Task[None]] = set()

    async def __call__(self, ctx: Any) -> AdaptiveCardActionCardResponse:
        data = getattr(getattr(ctx.activity.value, "action", None), "data", None)
        if not isinstance(data, Mapping):
            return AdaptiveCardActionCardResponse(
                value=_orbit_read_error_card(
                    OrbitValidationError("This timesheet navigation action is invalid.")
                )
            )
        try:
            query = _resolved_query_from_action(data)
        except OrbitValidationError as exc:
            return AdaptiveCardActionCardResponse(value=_orbit_read_error_card(exc))

        operation_id = ":".join(
            (
                OrbitLoginCardHandler._submission_id(ctx),
                query.start_date.isoformat(),
                query.end_date.isoformat(),
                query.project_id or "",
                query.status or "",
                str(query.page),
            )
        )
        if operation_id not in self._started:
            self._remember_started(operation_id)
            task = asyncio.create_task(self._load_and_send(ctx, query))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
        return AdaptiveCardActionCardResponse(
            value=orbit_status_card(
                "Loading timesheet",
                "PULSE is loading the requested page and will post it here.",
                success=True,
            )
        )

    async def _load_and_send(self, ctx: Any, query: ResolvedTimesheetQuery) -> None:
        try:
            page = await self._view.list_page(teams_user_id(ctx), query)
            result = orbit_timesheet_list_card(page)
        except Exception as exc:
            if not isinstance(
                exc,
                (
                    OrbitAmbiguousMatchError,
                    OrbitAuthenticationError,
                    OrbitAuthRequiredError,
                    OrbitNotFoundError,
                    OrbitProviderError,
                    OrbitValidationError,
                ),
            ):
                logger.exception("Unexpected error while loading an Orbit timesheet page")
            result = _orbit_read_error_card(exc)
        try:
            await ctx.send(result)
        except Exception:
            logger.exception("Failed to send an Orbit timesheet page")

    def _remember_started(self, operation_id: str) -> None:
        self._started[operation_id] = None
        self._started.move_to_end(operation_id)
        while len(self._started) > 256:
            self._started.popitem(last=False)


class OrbitViewDetailCardHandler:
    def __init__(self, view_service: OrbitViewEntriesService) -> None:
        self._view = view_service
        self._started: OrderedDict[str, None] = OrderedDict()
        self._background_tasks: set[asyncio.Task[None]] = set()

    async def __call__(self, ctx: Any) -> AdaptiveCardActionCardResponse:
        data = getattr(getattr(ctx.activity.value, "action", None), "data", None)
        if not isinstance(data, Mapping) or not isinstance(data.get("entry_id"), str):
            return AdaptiveCardActionCardResponse(
                value=_orbit_read_error_card(
                    OrbitValidationError("This Orbit entry reference is invalid.")
                )
            )
        try:
            query = _resolved_query_from_action(data)
        except OrbitValidationError as exc:
            return AdaptiveCardActionCardResponse(value=_orbit_read_error_card(exc))
        entry_id = data["entry_id"]

        operation_id = f"{OrbitLoginCardHandler._submission_id(ctx)}:{entry_id}"
        if operation_id not in self._started:
            self._remember_started(operation_id)
            task = asyncio.create_task(self._load_and_send(ctx, entry_id, query))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
        return AdaptiveCardActionCardResponse(
            value=orbit_status_card(
                "Loading Orbit entry",
                "PULSE is loading the entry details and will post them here.",
                success=True,
            )
        )

    async def _load_and_send(
        self,
        ctx: Any,
        entry_id: str,
        query: ResolvedTimesheetQuery,
    ) -> None:
        try:
            entry = await self._view.get_detail(teams_user_id(ctx), entry_id)
            result = orbit_timesheet_detail_card(entry, query)
        except Exception as exc:
            if not isinstance(
                exc,
                (
                    OrbitAuthenticationError,
                    OrbitAuthRequiredError,
                    OrbitNotFoundError,
                    OrbitProviderError,
                    OrbitValidationError,
                ),
            ):
                logger.exception("Unexpected error while loading Orbit entry details")
            result = _orbit_read_error_card(exc)
        try:
            await ctx.send(result)
        except Exception:
            logger.exception("Failed to send Orbit entry details")

    def _remember_started(self, operation_id: str) -> None:
        self._started[operation_id] = None
        self._started.move_to_end(operation_id)
        while len(self._started) > 256:
            self._started.popitem(last=False)


class TeamsMessageHandler:
    def __init__(
        self,
        chat_service: ChatService,
        orbit_auth_service: OrbitAuthService | None = None,
        orbit_add_service: OrbitAddEntryService | None = None,
        orbit_view_service: OrbitViewEntriesService | None = None,
    ) -> None:
        self._chat_service = chat_service
        self._orbit_auth = orbit_auth_service
        self._orbit_add = orbit_add_service
        self._orbit_view = orbit_view_service

    async def __call__(self, ctx: Any) -> None:
        user_text = clean_teams_text(getattr(ctx.activity, "text", None))
        try:
            if PASSWORD_VALUE_PATTERN.search(user_text):
                response = (
                    "For your security, do not send passwords as a Teams message. "
                    "Delete that message, change the exposed password, then send "
                    "`orbit login` and use the masked sign-in card."
                )
            elif self._is_orbit_command(user_text):
                response = await self._handle_orbit(ctx, user_text)
            else:
                response = await self._chat_service.reply(user_text)
        except InvalidMessageError as exc:
            await ctx.send(str(exc))
        except ProviderError:
            await ctx.send("PULSE cannot reach the AI service right now. Please try again shortly.")
        except Exception:
            logger.exception("Unexpected error while handling a Teams message")
            await ctx.send("Something went wrong while processing your message. Please try again.")
        else:
            await ctx.send(response)

    @staticmethod
    def _is_orbit_command(text: str) -> bool:
        command = text.strip().casefold()
        return (
            command.startswith("orbit ")
            or command in {"orbit", "confirm", "cancel"}
            or TeamsMessageHandler._is_orbit_login_intent(command)
            or TeamsMessageHandler._is_time_entry_view_intent(command)
            or TeamsMessageHandler._is_time_entry_intent(command)
        )

    @staticmethod
    def _is_orbit_login_intent(text: str) -> bool:
        normalized = re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()
        if "orbit" not in normalized.split():
            return False
        return any(
            phrase in normalized
            for phrase in ("login", "log in", "signin", "sign in", "connect")
        )

    @staticmethod
    def _is_time_entry_intent(text: str) -> bool:
        words = set(re.findall(r"[a-z0-9]+", text.casefold()))
        return bool(words & TIME_ENTRY_ACTION_WORDS and words & TIME_ENTRY_DETAIL_WORDS)

    @staticmethod
    def _is_time_entry_view_intent(text: str) -> bool:
        words = set(re.findall(r"[a-z0-9]+", text.casefold()))
        explicit_view = bool(words & TIME_ENTRY_VIEW_WORDS and words & TIME_ENTRY_VIEW_TARGETS)
        work_question = bool(
            "what" in words
            and words & {"work", "worked"}
            and words & {"today", "yesterday", "week", "month"}
        )
        return explicit_view or work_question

    async def _handle_orbit(self, ctx: Any, text: str) -> str | AdaptiveCard:
        if not self._orbit_auth:
            return "Orbit integration is not configured yet."
        current_teams_user_id = teams_user_id(ctx)
        command = text.strip()
        normalized = command.casefold()

        try:
            if self._is_orbit_login_intent(normalized):
                return orbit_login_card()
            if normalized == "orbit logout":
                if self._orbit_add:
                    await self._orbit_add.cancel(current_teams_user_id)
                await self._orbit_auth.logout(current_teams_user_id)
                return "Your local Orbit session has been removed."
            if normalized in {"confirm", "orbit confirm"}:
                if not self._orbit_add:
                    return "Orbit add-entry integration is not configured yet."
                created = await self._orbit_add.confirm(current_teams_user_id)
                return (
                    "Orbit entry created successfully. "
                    f"Date: {created.entry_date.isoformat()}, "
                    f"duration: {self._format_duration(created.duration_minutes)}, "
                    f"reference: `{created.id}`."
                )
            if normalized in {"cancel", "orbit cancel"}:
                if not self._orbit_add:
                    return "Orbit add-entry integration is not configured yet."
                cancelled = await self._orbit_add.cancel(current_teams_user_id)
                return (
                    "Pending Orbit entry cancelled."
                    if cancelled
                    else "There is no pending Orbit entry to cancel."
                )
            if (
                normalized.startswith(("orbit entries", "orbit list", "orbit timesheet"))
                or self._is_time_entry_view_intent(normalized)
            ):
                if not self._orbit_view:
                    return "Orbit timesheet viewing is not configured yet."
                page = await self._orbit_view.list_from_text(
                    current_teams_user_id, command
                )
                return orbit_timesheet_list_card(page)
            if normalized.startswith("orbit add") or self._is_time_entry_intent(normalized):
                if not self._orbit_add:
                    return "Orbit add-entry integration is not configured yet."
                request = (
                    command[len("orbit add") :].strip()
                    if normalized.startswith("orbit add")
                    else command
                )
                if not request:
                    raise OrbitValidationError(
                        "Provide the entry details: project, task, date, duration, and notes."
                    )
                pending = await self._orbit_add.prepare(current_teams_user_id, request)
                return orbit_confirmation_card(pending)
            return (
                "Orbit commands: `orbit login`, `orbit add <details>`, "
                "`orbit list`, `orbit confirm`, `orbit cancel`, and `orbit logout`."
            )
        except OrbitAuthRequiredError:
            return "Your Orbit account is not linked. Send `orbit login` first."
        except OrbitAmbiguousMatchError as exc:
            options = ", ".join(exc.options)
            return f"Multiple Orbit {exc.entity} matches were found: {options}. Be more specific."
        except (OrbitValidationError, OrbitNotFoundError) as exc:
            return str(exc)
        except OrbitAuthenticationError:
            return "Orbit authentication failed. Send `orbit login` and try again."
        except OrbitMutationUncertainError as exc:
            return str(exc)
        except OrbitProviderError:
            return "Orbit is temporarily unavailable or rejected the operation. Please try later."

    @staticmethod
    def _format_duration(minutes: int) -> str:
        hours, remainder = divmod(minutes, 60)
        return f"{hours}h {remainder:02d}m"


def register_teams_app(
    fastapi_app: FastAPI,
    settings: Settings,
    chat_service: ChatService,
    orbit_auth_service: OrbitAuthService | None = None,
    orbit_add_service: OrbitAddEntryService | None = None,
    orbit_view_service: OrbitViewEntriesService | None = None,
) -> App:
    adapter = FastAPIAdapter(app=fastapi_app)
    kwargs: dict[str, Any] = {
        "http_server_adapter": adapter,
        "dangerously_allow_unauthenticated_requests": settings.teams_skip_auth,
    }
    if not settings.teams_skip_auth:
        kwargs.update(
            client_id=settings.teams_client_id,
            client_secret=(
                settings.teams_client_secret.get_secret_value()
                if settings.teams_client_secret
                else None
            ),
            tenant_id=settings.teams_tenant_id,
        )

    teams_app = App(**kwargs)
    teams_app.on_message(
        TeamsMessageHandler(
            chat_service,
            orbit_auth_service,
            orbit_add_service,
            orbit_view_service,
        )
    )
    if orbit_auth_service:
        teams_app.on_card_action_execute(
            ORBIT_LOGIN_ACTION,
            OrbitLoginCardHandler(orbit_auth_service),
        )
    if orbit_add_service:
        teams_app.on_card_action_execute(
            ORBIT_CONFIRM_ACTION,
            OrbitConfirmCardHandler(orbit_add_service),
        )
        teams_app.on_card_action_execute(
            ORBIT_CANCEL_ACTION,
            OrbitCancelCardHandler(orbit_add_service),
        )
    if orbit_view_service:
        teams_app.on_card_action_execute(
            ORBIT_VIEW_PAGE_ACTION,
            OrbitViewPageCardHandler(orbit_view_service),
        )
        teams_app.on_card_action_execute(
            ORBIT_VIEW_DETAIL_ACTION,
            OrbitViewDetailCardHandler(orbit_view_service),
        )
    return teams_app
