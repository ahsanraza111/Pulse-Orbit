from __future__ import annotations

import asyncio
import logging
import re
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Coroutine, Mapping
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
    Choice,
    ChoiceSetInput,
    Container,
    DateInput,
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
    AppliedEntryChange,
    BulkCreationResult,
    BulkEntitySelectionRequest,
    BulkScheduleSelectionRequest,
    CreatedTimesheetEntry,
    EditFormContext,
    EntrySelectionRequest,
    OrbitTimesheetEntry,
    PendingEntryChange,
    PendingTimesheetBatch,
    PendingTimesheetEntry,
    ProjectSelectionRequest,
    ResolvedTimesheetQuery,
    TimesheetEntryPage,
)
from pulse.application.services.chat import ChatService
from pulse.application.services.orbit import (
    OrbitAddEntryService,
    OrbitAuthService,
    OrbitViewEntriesService,
)
from pulse.application.services.orbit_bulk import (
    BulkPreparationResult,
    OrbitBulkEntryService,
)
from pulse.application.services.orbit_modify import OrbitModifyEntryService
from pulse.core.config import Settings

logger = logging.getLogger(__name__)
MENTION_PATTERN = re.compile(r"<at>.*?</at>", flags=re.IGNORECASE | re.DOTALL)
PASSWORD_VALUE_PATTERN = re.compile(
    r"\b(?:orbit\s+)?pass(?:word)?\s*(?::|=|\bis\b)\s*\S+",
    flags=re.IGNORECASE,
)
ORBIT_LOGIN_ACTION = "orbit_login"
ORBIT_OPEN_LOGIN_ACTION = "orbit_open_login"
ORBIT_CONFIRM_ACTION = "orbit_confirm"
ORBIT_CANCEL_ACTION = "orbit_cancel"
ORBIT_SELECT_PROJECT_ACTION = "orbit_select_project"
ORBIT_BULK_SCHEDULE_ACTION = "orbit_bulk_schedule"
ORBIT_BULK_ENTITY_ACTION = "orbit_bulk_entity"
ORBIT_BULK_CONFIRM_ACTION = "orbit_bulk_confirm"
ORBIT_BULK_CANCEL_ACTION = "orbit_bulk_cancel"
ORBIT_VIEW_PAGE_ACTION = "orbit_view_page"
ORBIT_VIEW_DETAIL_ACTION = "orbit_view_detail"
ORBIT_EDIT_START_ACTION = "orbit_edit_start"
ORBIT_EDIT_SUBMIT_ACTION = "orbit_edit_submit"
ORBIT_DELETE_START_ACTION = "orbit_delete_start"
ORBIT_CHANGE_CONFIRM_ACTION = "orbit_change_confirm"
ORBIT_CHANGE_CANCEL_ACTION = "orbit_change_cancel"
ORBIT_ENTRY_SELECT_ACTION = "orbit_entry_select"
ENTRY_CHANGE_WORDS = {
    "badal",
    "badlo",
    "change",
    "correct",
    "delete",
    "edit",
    "hata",
    "hatao",
    "modify",
    "remove",
    "tabdeel",
    "update",
}
ENTRY_CHANGE_TARGETS = {"entries", "entry", "hour", "hours", "timesheet"}
ENTRY_CHANGE_PREFIXES = (
    "orbit edit",
    "orbit update",
    "orbit change",
    "orbit modify",
    "orbit delete",
    "orbit remove",
)
TIME_ENTRY_ACTION_WORDS = {
    "add",
    "create",
    "daal",
    "daalo",
    "dal",
    "dalo",
    "enter",
    "log",
    "record",
    "worked",
}
TIME_ENTRY_DETAIL_WORDS = {
    "entry",
    "entries",
    "hour",
    "hours",
    "minute",
    "minutes",
    "time",
    "timesheet",
}
TIME_ENTRY_VIEW_WORDS = {"check", "display", "find", "get", "list", "see", "show", "view"}
TIME_ENTRY_VIEW_TARGETS = {"entries", "entry", "hours", "time", "timesheet"}
TIME_ENTRY_START_WORDS = {"add", "create", "enter", "log", "submit", "want"}


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


def pulse_introduction_card() -> AdaptiveCard:
    return AdaptiveCard(
        body=[
            TextBlock(text="Hi, I'm PULSE", size="Large", weight="Bolder"),
            TextBlock(
                text=(
                    "I'm your internal AI assistant for Orbit timesheets. I can help "
                    "you connect your Orbit account, add time entries, and view your "
                    "timesheet without leaving Teams."
                ),
                wrap=True,
            ),
            TextBlock(
                text="Select **Orbit login** to connect or check your Orbit session.",
                wrap=True,
                spacing="Medium",
            ),
        ],
        actions=[
            ExecuteAction(
                title="Orbit login",
                verb=ORBIT_OPEN_LOGIN_ACTION,
                data={"action": ORBIT_OPEN_LOGIN_ACTION},
            )
        ],
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


def orbit_bulk_schedule_card(request: BulkScheduleSelectionRequest) -> AdaptiveCard:
    range_days = (request.end_date - request.start_date).days + 1
    return AdaptiveCard(
        body=[
            TextBlock(text="Choose bulk-entry schedule", size="Large", weight="Bolder"),
            TextBlock(
                text=(
                    "Should every entry template repeat only on weekdays or on every "
                    "calendar day? PULSE will not create anything until final confirmation."
                ),
                wrap=True,
            ),
            FactSet(
                facts=[
                    Fact(
                        title="Date range",
                        value=(
                            f"{request.start_date.isoformat()} to "
                            f"{request.end_date.isoformat()}"
                        ),
                    ),
                    Fact(title="Calendar days", value=str(range_days)),
                    Fact(title="Entry templates", value=str(request.template_count)),
                ]
            ),
        ],
        actions=[
            ExecuteAction(
                title="Weekdays only",
                verb=ORBIT_BULK_SCHEDULE_ACTION,
                data={
                    "action": ORBIT_BULK_SCHEDULE_ACTION,
                    "preparation_id": request.preparation_id,
                    "schedule": "weekdays",
                },
            ),
            ExecuteAction(
                title="Every day",
                verb=ORBIT_BULK_SCHEDULE_ACTION,
                data={
                    "action": ORBIT_BULK_SCHEDULE_ACTION,
                    "preparation_id": request.preparation_id,
                    "schedule": "all_days",
                },
            ),
            ExecuteAction(
                title="Cancel",
                verb=ORBIT_BULK_CANCEL_ACTION,
                data={"action": ORBIT_BULK_CANCEL_ACTION},
                style="destructive",
            ),
        ],
    )


def orbit_bulk_entity_selection_card(
    request: BulkEntitySelectionRequest,
) -> AdaptiveCard:
    return AdaptiveCard(
        body=[
            TextBlock(
                text=f"Choose Orbit {request.entity}",
                size="Large",
                weight="Bolder",
            ),
            TextBlock(
                text=(
                    f"Entry template {request.template_index + 1} has multiple matches "
                    f"for **{request.requested_name}**. Choose the correct {request.entity}."
                ),
                wrap=True,
            ),
        ],
        actions=[
            *[
                ExecuteAction(
                    title=option.name,
                    verb=ORBIT_BULK_ENTITY_ACTION,
                    data={
                        "action": ORBIT_BULK_ENTITY_ACTION,
                        "preparation_id": request.preparation_id,
                        "entity": request.entity,
                        "template_index": request.template_index,
                        "option_id": option.id,
                    },
                )
                for option in request.options
            ],
            ExecuteAction(
                title="Cancel batch",
                verb=ORBIT_BULK_CANCEL_ACTION,
                data={"action": ORBIT_BULK_CANCEL_ACTION},
                style="destructive",
            ),
        ],
    )


def orbit_bulk_confirmation_card(batch: PendingTimesheetBatch) -> AdaptiveCard:
    schedule_labels = {
        "single_day": "Single day",
        "weekdays": "Weekdays only",
        "all_days": "Every day",
    }
    daily_minutes = sum(item.duration_minutes for item in batch.templates)
    overall_minutes = sum(item.duration_minutes for item in batch.entries)
    body: list[Any] = [
        TextBlock(text="Confirm Orbit bulk entries", size="Large", weight="Bolder"),
        TextBlock(
            text="Review the complete generated batch before creating any entries.",
            wrap=True,
        ),
        FactSet(
            facts=[
                Fact(
                    title="Date range",
                    value=f"{batch.start_date.isoformat()} to {batch.end_date.isoformat()}",
                ),
                Fact(title="Schedule", value=schedule_labels[batch.schedule]),
                Fact(title="Eligible days", value=str(len(batch.dates))),
                Fact(title="Templates per day", value=str(len(batch.templates))),
                Fact(title="Generated entries", value=str(len(batch.entries))),
                Fact(
                    title="Daily duration",
                    value=TeamsMessageHandler._format_duration(daily_minutes),
                ),
                Fact(
                    title="Overall duration",
                    value=TeamsMessageHandler._format_duration(overall_minutes),
                ),
            ]
        ),
        TextBlock(text="Resolved templates", weight="Bolder", spacing="Medium"),
    ]
    for index, template in enumerate(batch.templates, start=1):
        body.append(
            TextBlock(
                text=(
                    f"**{index}. {template.project_name} — {template.task_name}**\n\n"
                    f"{TeamsMessageHandler._format_duration(template.duration_minutes)} · "
                    f"{template.description}"
                ),
                wrap=True,
                separator=True,
            )
        )
    body.append(
        TextBlock(
            text="Nothing will be created until you select **Create all entries**.",
            wrap=True,
            spacing="Medium",
        )
    )
    return AdaptiveCard(
        body=body,
        actions=[
            ExecuteAction(
                title=f"Create all {len(batch.entries)} entries",
                verb=ORBIT_BULK_CONFIRM_ACTION,
                data={
                    "action": ORBIT_BULK_CONFIRM_ACTION,
                    "batch_id": batch.id,
                },
                style="positive",
            ),
            ExecuteAction(
                title="Cancel batch",
                verb=ORBIT_BULK_CANCEL_ACTION,
                data={
                    "action": ORBIT_BULK_CANCEL_ACTION,
                    "batch_id": batch.id,
                },
                style="destructive",
            ),
        ],
    )


def orbit_bulk_preparation_card(result: BulkPreparationResult) -> AdaptiveCard:
    if isinstance(result, BulkScheduleSelectionRequest):
        return orbit_bulk_schedule_card(result)
    if isinstance(result, BulkEntitySelectionRequest):
        return orbit_bulk_entity_selection_card(result)
    return orbit_bulk_confirmation_card(result)


def orbit_bulk_result_card(result: BulkCreationResult) -> AdaptiveCard:
    created = sum(item.status == "created" for item in result.outcomes)
    failed = sum(item.status == "failed" for item in result.outcomes)
    uncertain = sum(item.status == "uncertain" for item in result.outcomes)
    lines = []
    for item in result.outcomes:
        icon = {"created": "✅", "failed": "❌", "uncertain": "⚠️"}[item.status]
        suffix = (
            f" · `{item.reference}`"
            if item.reference
            else f" · {item.message or 'No reference returned'}"
        )
        lines.append(
            f"{icon} {item.entry.entry_date.isoformat()} · "
            f"{item.entry.project_name} / {item.entry.task_name} · "
            f"{TeamsMessageHandler._format_duration(item.entry.duration_minutes)}{suffix}"
        )
    return AdaptiveCard(
        body=[
            TextBlock(text="Orbit bulk-entry result", size="Large", weight="Bolder"),
            FactSet(
                facts=[
                    Fact(title="Created", value=str(created)),
                    Fact(title="Failed", value=str(failed)),
                    Fact(title="Needs verification", value=str(uncertain)),
                ]
            ),
            TextBlock(text="\n\n".join(lines), wrap=True, spacing="Medium"),
        ]
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


def orbit_project_selection_card(selection: ProjectSelectionRequest) -> AdaptiveCard:
    return AdaptiveCard(
        body=[
            TextBlock(text="Choose Orbit project", size="Large", weight="Bolder"),
            TextBlock(
                text=(
                    "I found more than one assigned project matching "
                    f"**{selection.parsed.project_name}**. Choose the correct one to "
                    "continue this same time entry."
                ),
                wrap=True,
            ),
        ],
        actions=[
            ExecuteAction(
                title=project.name,
                verb=ORBIT_SELECT_PROJECT_ACTION,
                data={
                    "action": ORBIT_SELECT_PROJECT_ACTION,
                    "selection_id": selection.id,
                    "project_id": project.id,
                },
            )
            for project in selection.options
        ],
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
    if entry.status == "draft":
        actions.extend(
            [
                ExecuteAction(
                    title="Edit",
                    verb=ORBIT_EDIT_START_ACTION,
                    data={"action": ORBIT_EDIT_START_ACTION, "entry_id": entry.id},
                ),
                ExecuteAction(
                    title="Delete",
                    verb=ORBIT_DELETE_START_ACTION,
                    data={"action": ORBIT_DELETE_START_ACTION, "entry_id": entry.id},
                    style="destructive",
                ),
            ]
        )
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


def _form_duration(minutes: int) -> str:
    hours, remainder = divmod(minutes, 60)
    if hours and remainder:
        return f"{hours}h {remainder}m"
    return f"{hours}h" if hours else f"{remainder}m"


def _entry_facts(entry: OrbitTimesheetEntry) -> FactSet:
    return FactSet(
        facts=[
            Fact(title="Date", value=entry.entry_date.isoformat()),
            Fact(title="Project", value=entry.project_name),
            Fact(title="Task", value=entry.task_name),
            Fact(
                title="Duration",
                value=TeamsMessageHandler._format_duration(entry.duration_minutes),
            ),
            Fact(title="Notes", value=entry.description or "No notes"),
        ]
    )


def orbit_edit_form_card(context: EditFormContext) -> AdaptiveCard:
    entry = context.entry
    return AdaptiveCard(
        body=[
            TextBlock(text="Edit Orbit entry", size="Large", weight="Bolder"),
            TextBlock(
                text=(
                    "Change any field, then select **Review changes**. Nothing is "
                    "updated until you confirm the next card. If you change the project, "
                    "also change the task to one of that project's tasks."
                ),
                wrap=True,
            ),
            DateInput(
                id="edit_date",
                label="Date",
                value=entry.entry_date.isoformat(),
                is_required=True,
                error_message="Choose a date.",
            ),
            TextInput(
                id="edit_duration",
                label="Duration (for example 1h 30m, 90m, or 1:30)",
                value=_form_duration(entry.duration_minutes),
                is_required=True,
                error_message="Enter the duration.",
                max_length=20,
            ),
            ChoiceSetInput(
                id="edit_project",
                label="Project",
                value=entry.project_id,
                style="compact",
                is_required=True,
                error_message="Choose a project.",
                choices=[
                    Choice(title=project.name, value=project.id) for project in context.projects
                ],
            ),
            TextInput(
                id="edit_task",
                label="Task",
                value=entry.task_name,
                is_required=True,
                error_message="Enter the task.",
                max_length=200,
            ),
            TextInput(
                id="edit_notes",
                label="Notes",
                value=entry.description,
                is_multiline=True,
                is_required=True,
                error_message="Enter the task notes.",
                max_length=2000,
            ),
        ],
        actions=[
            ExecuteAction(
                title="Review changes",
                verb=ORBIT_EDIT_SUBMIT_ACTION,
                data={"action": ORBIT_EDIT_SUBMIT_ACTION, "entry_id": entry.id},
                style="positive",
            )
        ],
    )


def _change_actions(
    change: PendingEntryChange, *, confirm_title: str, cancel_title: str
) -> list[ExecuteAction]:
    return [
        ExecuteAction(
            title=confirm_title,
            verb=ORBIT_CHANGE_CONFIRM_ACTION,
            data={"action": ORBIT_CHANGE_CONFIRM_ACTION, "change_id": change.id},
            style="positive" if change.kind == "update" else "destructive",
        ),
        ExecuteAction(
            title=cancel_title,
            verb=ORBIT_CHANGE_CANCEL_ACTION,
            data={"action": ORBIT_CHANGE_CANCEL_ACTION, "change_id": change.id},
        ),
    ]


def orbit_update_confirmation_card(change: PendingEntryChange) -> AdaptiveCard:
    before, after = change.before, change.after
    assert after is not None
    fields = (
        ("Date", before.entry_date.isoformat(), after.entry_date.isoformat()),
        ("Project", before.project_name, after.project_name),
        ("Task", before.task_name, after.task_name),
        (
            "Duration",
            TeamsMessageHandler._format_duration(before.duration_minutes),
            TeamsMessageHandler._format_duration(after.duration_minutes),
        ),
        ("Notes", before.description or "No notes", after.description),
    )
    changed = [Fact(title=n, value=f"{old} → {new}") for n, old, new in fields if old != new]
    unchanged = [Fact(title=n, value=old) for n, old, new in fields if old == new]
    body: list[Any] = [
        TextBlock(text="Confirm Orbit entry update", size="Large", weight="Bolder"),
        TextBlock(text="Changes", weight="Bolder", spacing="Medium"),
        FactSet(facts=changed),
    ]
    if unchanged:
        body.extend(
            [
                TextBlock(text="Unchanged", weight="Bolder", spacing="Medium"),
                FactSet(facts=unchanged),
            ]
        )
    body.append(
        TextBlock(
            text="The entry is not updated until you select **Update entry**.",
            wrap=True,
            spacing="Medium",
        )
    )
    return AdaptiveCard(
        body=body,
        actions=_change_actions(change, confirm_title="Update entry", cancel_title="Cancel"),
    )


def orbit_delete_confirmation_card(change: PendingEntryChange) -> AdaptiveCard:
    return AdaptiveCard(
        body=[
            TextBlock(text="Confirm Orbit entry deletion", size="Large", weight="Bolder"),
            TextBlock(
                text="This draft entry will be permanently deleted from Orbit.",
                color="Attention",
                wrap=True,
            ),
            _entry_facts(change.before),
            TextBlock(
                text="The entry is not deleted until you select **Delete entry**.",
                wrap=True,
                spacing="Medium",
            ),
        ],
        actions=_change_actions(change, confirm_title="Delete entry", cancel_title="Keep entry"),
    )


def orbit_entry_selection_card(selection: EntrySelectionRequest) -> AdaptiveCard:
    verb_text = "delete" if selection.request.action == "delete" else "edit"
    body: list[Any] = [
        TextBlock(text=f"Choose the entry to {verb_text}", size="Large", weight="Bolder"),
        TextBlock(
            text=(
                f"More than one draft entry matched. Select the one to {verb_text}; "
                "you will confirm before anything changes."
            ),
            wrap=True,
        ),
    ]
    for entry in selection.options:
        body.append(
            Container(
                separator=True,
                spacing="Medium",
                items=[
                    TextBlock(
                        text=(
                            f"**{entry.entry_date.isoformat()} · "
                            f"{TeamsMessageHandler._format_duration(entry.duration_minutes)}**"
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
                                title=f"Select to {verb_text}",
                                verb=ORBIT_ENTRY_SELECT_ACTION,
                                data={
                                    "action": ORBIT_ENTRY_SELECT_ACTION,
                                    "selection_id": selection.id,
                                    "entry_id": entry.id,
                                },
                            )
                        ]
                    ),
                ],
            )
        )
    return AdaptiveCard(body=body)


def orbit_change_confirmation_card(change: PendingEntryChange) -> AdaptiveCard:
    if change.kind == "delete":
        return orbit_delete_confirmation_card(change)
    return orbit_update_confirmation_card(change)


def orbit_change_result_card(applied: AppliedEntryChange) -> AdaptiveCard:
    title = "Orbit entry updated" if applied.kind == "update" else "Orbit entry deleted"
    return AdaptiveCard(
        body=[
            TextBlock(text=title, size="Medium", weight="Bolder", color="Good"),
            _entry_facts(applied.entry),
        ]
    )


def _orbit_change_error_card(title: str, exc: Exception) -> AdaptiveCard:
    if isinstance(exc, OrbitAuthRequiredError):
        message = "Your Orbit account is not linked. Send `orbit login` and try again."
    elif isinstance(exc, OrbitAuthenticationError):
        message = f"{exc} Send `orbit login` if the problem continues.".strip()
    elif isinstance(exc, OrbitMutationUncertainError):
        return orbit_status_card("Orbit result needs verification", str(exc))
    elif isinstance(exc, OrbitAmbiguousMatchError):
        message = (
            f"Multiple Orbit {exc.entity} matches were found: {', '.join(exc.options)}. "
            "Open the entry again and be more specific."
        )
    elif isinstance(exc, OrbitValidationError | OrbitNotFoundError):
        message = str(exc)
    elif isinstance(exc, OrbitProviderError):
        message = "Orbit is temporarily unavailable or rejected the operation. Please try later."
    else:
        message = "Something went wrong. Please try again."
    return orbit_status_card(title, message)


TIME_ENTRY_TEMPLATES: tuple[tuple[str, str, str], ...] = (
    (
        "today",
        "Add today's entry",
        "add 1 hour to [project name], task [task name], notes [work notes], date today",
    ),
    (
        "yesterday",
        "Add yesterday's entry",
        "add 1 hour to [project name], task [task name], notes [work notes], date yesterday",
    ),
    (
        "date",
        "Add entry for a date",
        "add 1 hour to [project name], task [task name], notes [work notes], date YYYY-MM-DD",
    ),
    (
        "range",
        "Add entries for a date range",
        "log these entries from this [from date] to [to date]\n"
        "add 1 hour to [project name], task [task name], notes [work notes]",
    ),
)


def time_entry_templates_card() -> AdaptiveCard:
    return AdaptiveCard(
        body=[
            TextBlock(text="Time-entry templates", size="Large", weight="Bolder"),
            TextBlock(
                text=(
                    "Copy a template, replace the [bracketed] parts, and send it. PULSE "
                    "will show a confirmation card before anything is created."
                ),
                wrap=True,
            ),
            *[_template_block(title, command) for _, title, command in TIME_ENTRY_TEMPLATES],
        ]
    )


def _template_block(title: str, command: str) -> Container:
    return Container(
        separator=True,
        spacing="Medium",
        items=[
            TextBlock(text=title, weight="Bolder", wrap=True),
            *[
                TextBlock(text=line, font_type="Monospace", wrap=True, spacing="Small")
                for line in command.split("\n")
            ],
        ],
    )


def time_entry_suggestions_message(*, newly_connected: bool = True) -> ComposeMessageActivity:
    templates = [(title, command) for _, title, command in TIME_ENTRY_TEMPLATES]
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
            (
                "✅ Orbit account connected successfully.\n\n"
                if newly_connected
                else "✅ You are already signed in to Orbit.\n\n"
            )
            + "Choose a time-entry template below. Teams will place it in your message "
            "box so you can edit the duration, project, task, and notes before sending."
        ),
        suggested_actions=ComposeSuggestedActions(to=[], actions=actions),
    )


def time_entry_help_messages(
    *, newly_connected: bool = True
) -> list[AdaptiveCard | ComposeMessageActivity]:
    """The templates card, then the compose suggestions.

    Teams shows suggested actions only from the last message in a personal chat, so the
    suggestion message must be sent last.
    """
    suggestions = time_entry_suggestions_message(newly_connected=newly_connected)
    return [time_entry_templates_card(), suggestions]


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
            for message in time_entry_help_messages():
                await ctx.send(message)
        except Exception:
            logger.exception("Failed to send the Orbit login success message")


class OrbitOpenLoginCardHandler:
    def __init__(self, auth_service: OrbitAuthService) -> None:
        self._auth = auth_service
        self._locks: dict[str, asyncio.Lock] = {}
        self._responses: OrderedDict[str, AdaptiveCard] = OrderedDict()

    async def __call__(self, ctx: Any) -> AdaptiveCardActionCardResponse:
        operation_id = self._operation_id(ctx)
        lock = self._locks.setdefault(operation_id, asyncio.Lock())
        async with lock:
            response = self._responses.get(operation_id)
            if response is None:
                try:
                    if await self._auth.is_authenticated(teams_user_id(ctx)):
                        for message in time_entry_help_messages(newly_connected=False):
                            await ctx.send(message)
                        response = orbit_status_card(
                            "Orbit already connected",
                            "Your editable time-entry formats are available below.",
                            success=True,
                        )
                    else:
                        response = orbit_login_card()
                except OrbitProviderError:
                    response = orbit_status_card(
                        "Orbit unavailable",
                        "PULSE could not check your Orbit session. Please try again.",
                    )
                except Exception:
                    logger.exception("Unexpected error while opening Orbit login")
                    response = orbit_status_card(
                        "Orbit login unavailable",
                        "Something went wrong. Please try again.",
                    )
                self._remember_response(operation_id, response)

        return AdaptiveCardActionCardResponse(value=response)

    @staticmethod
    def _operation_id(ctx: Any) -> str:
        activity_id = getattr(ctx.activity, "id", None)
        if isinstance(activity_id, str) and activity_id:
            return f"{teams_user_id(ctx)}:{activity_id}"
        return f"{teams_user_id(ctx)}:{id(ctx.activity)}"

    def _remember_response(self, operation_id: str, response: AdaptiveCard) -> None:
        self._responses[operation_id] = response
        self._responses.move_to_end(operation_id)
        while len(self._responses) > 256:
            removed_id, _ = self._responses.popitem(last=False)
            self._locks.pop(removed_id, None)


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


class OrbitProjectSelectionCardHandler:
    def __init__(self, add_service: OrbitAddEntryService) -> None:
        self._add = add_service
        self._started: OrderedDict[str, None] = OrderedDict()
        self._background_tasks: set[asyncio.Task[None]] = set()

    async def __call__(self, ctx: Any) -> AdaptiveCardActionCardResponse:
        data = getattr(getattr(ctx.activity.value, "action", None), "data", None)
        selection_id = data.get("selection_id") if isinstance(data, Mapping) else None
        project_id = data.get("project_id") if isinstance(data, Mapping) else None
        if not isinstance(selection_id, str) or not isinstance(project_id, str):
            return AdaptiveCardActionCardResponse(
                value=orbit_status_card(
                    "Project selection failed",
                    "This project selection card is invalid. Start the entry again.",
                )
            )

        current_user_id = teams_user_id(ctx)
        operation_id = f"{current_user_id}:{selection_id}:{project_id}"
        if operation_id not in self._started:
            self._remember_started(operation_id)
            task = asyncio.create_task(
                self._select_and_report(ctx, current_user_id, selection_id, project_id)
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

        return AdaptiveCardActionCardResponse(
            value=orbit_status_card(
                "Resolving Orbit project",
                "PULSE is preparing your time-entry confirmation.",
                success=True,
            )
        )

    async def _select_and_report(
        self,
        ctx: Any,
        current_user_id: str,
        selection_id: str,
        project_id: str,
    ) -> None:
        try:
            pending = await self._add.select_project(
                current_user_id,
                project_id=project_id,
                expected_selection_id=selection_id,
            )
            result = orbit_confirmation_card(pending)
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
                logger.exception("Unexpected error while selecting an Orbit project")
            result = _orbit_read_error_card(exc)
        try:
            await ctx.send(result)
        except Exception:
            logger.exception("Failed to send the Orbit project-selection result")

    def _remember_started(self, operation_id: str) -> None:
        self._started[operation_id] = None
        self._started.move_to_end(operation_id)
        while len(self._started) > 256:
            self._started.popitem(last=False)


class OrbitBulkSelectionCardHandler:
    def __init__(self, bulk_service: OrbitBulkEntryService) -> None:
        self._bulk = bulk_service
        self._started: OrderedDict[str, None] = OrderedDict()
        self._background_tasks: set[asyncio.Task[None]] = set()

    async def __call__(self, ctx: Any) -> AdaptiveCardActionCardResponse:
        data = getattr(getattr(ctx.activity.value, "action", None), "data", None)
        if not isinstance(data, Mapping):
            return AdaptiveCardActionCardResponse(
                value=orbit_status_card(
                    "Bulk selection failed",
                    "This bulk-entry selection is invalid. Start the batch again.",
                )
            )
        action = data.get("action")
        preparation_id = data.get("preparation_id")
        if action not in {ORBIT_BULK_SCHEDULE_ACTION, ORBIT_BULK_ENTITY_ACTION} or not isinstance(
            preparation_id, str
        ):
            return AdaptiveCardActionCardResponse(
                value=orbit_status_card(
                    "Bulk selection failed",
                    "This bulk-entry selection is invalid. Start the batch again.",
                )
            )

        operation_id = self._operation_id(ctx, data)
        if operation_id not in self._started:
            self._remember_started(operation_id)
            task = asyncio.create_task(
                self._select_and_report(ctx, teams_user_id(ctx), data)
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
        return AdaptiveCardActionCardResponse(
            value=orbit_status_card(
                "Preparing Orbit bulk entries",
                "PULSE is resolving the batch and will post the next step here.",
                success=True,
            )
        )

    async def _select_and_report(
        self, ctx: Any, current_user_id: str, data: Mapping[str, object]
    ) -> None:
        try:
            preparation_id = str(data["preparation_id"])
            if data["action"] == ORBIT_BULK_SCHEDULE_ACTION:
                schedule = data.get("schedule")
                if schedule not in {"weekdays", "all_days"}:
                    raise OrbitValidationError("Choose a valid bulk-entry schedule.")
                result = await self._bulk.select_schedule(
                    current_user_id,
                    preparation_id,
                    schedule,  # type: ignore[arg-type]
                )
            else:
                entity = data.get("entity")
                template_index = data.get("template_index")
                option_id = data.get("option_id")
                if (
                    entity not in {"project", "task"}
                    or not isinstance(template_index, int)
                    or isinstance(template_index, bool)
                    or not isinstance(option_id, str)
                ):
                    raise OrbitValidationError("Choose a valid Orbit project or task.")
                result = await self._bulk.select_entity(
                    current_user_id,
                    preparation_id,
                    entity=entity,  # type: ignore[arg-type]
                    template_index=template_index,
                    option_id=option_id,
                )
            response = orbit_bulk_preparation_card(result)
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
                logger.exception("Unexpected error while resolving an Orbit bulk entry")
            response = _orbit_read_error_card(exc)
        try:
            await ctx.send(response)
        except Exception:
            logger.exception("Failed to send the Orbit bulk-entry selection result")

    @staticmethod
    def _operation_id(ctx: Any, data: Mapping[str, object]) -> str:
        activity_id = getattr(ctx.activity, "id", None) or id(ctx.activity)
        return f"{teams_user_id(ctx)}:{activity_id}:{data.get('action')}"

    def _remember_started(self, operation_id: str) -> None:
        self._started[operation_id] = None
        self._started.move_to_end(operation_id)
        while len(self._started) > 256:
            self._started.popitem(last=False)


class OrbitBulkConfirmCardHandler:
    def __init__(self, bulk_service: OrbitBulkEntryService) -> None:
        self._bulk = bulk_service
        self._started: OrderedDict[str, None] = OrderedDict()
        self._background_tasks: set[asyncio.Task[None]] = set()

    async def __call__(self, ctx: Any) -> AdaptiveCardActionCardResponse:
        data = getattr(getattr(ctx.activity.value, "action", None), "data", None)
        batch_id = data.get("batch_id") if isinstance(data, Mapping) else None
        if not isinstance(batch_id, str) or not batch_id:
            return AdaptiveCardActionCardResponse(
                value=orbit_status_card(
                    "Bulk confirmation failed",
                    "This bulk confirmation card is invalid. Start the batch again.",
                )
            )
        operation_id = f"{teams_user_id(ctx)}:{batch_id}"
        if operation_id not in self._started:
            self._remember_started(operation_id)
            task = asyncio.create_task(
                self._confirm_and_report(ctx, teams_user_id(ctx), batch_id)
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
        return AdaptiveCardActionCardResponse(
            value=orbit_status_card(
                "Creating Orbit bulk entries",
                "PULSE is creating the confirmed entries and will post every result here.",
                success=True,
            )
        )

    async def _confirm_and_report(
        self, ctx: Any, current_user_id: str, batch_id: str
    ) -> None:
        try:
            result = await self._bulk.confirm(current_user_id, batch_id)
            response = orbit_bulk_result_card(result)
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
                logger.exception("Unexpected error while confirming Orbit bulk entries")
            response = _orbit_read_error_card(exc)
        try:
            await ctx.send(response)
        except Exception:
            logger.exception("Failed to send the Orbit bulk-entry result")

    def _remember_started(self, operation_id: str) -> None:
        self._started[operation_id] = None
        self._started.move_to_end(operation_id)
        while len(self._started) > 256:
            self._started.popitem(last=False)


class OrbitBulkCancelCardHandler:
    def __init__(self, bulk_service: OrbitBulkEntryService) -> None:
        self._bulk = bulk_service

    async def __call__(self, ctx: Any) -> AdaptiveCardActionCardResponse:
        data = getattr(getattr(ctx.activity.value, "action", None), "data", None)
        batch_id = data.get("batch_id") if isinstance(data, Mapping) else None
        expected_id = batch_id if isinstance(batch_id, str) and batch_id else None
        try:
            cancelled = await self._bulk.cancel(teams_user_id(ctx), expected_id)
            message = (
                "The pending bulk entry was discarded and nothing else will be created."
                if cancelled
                else "There is no pending Orbit bulk entry to cancel."
            )
            response = orbit_status_card("Orbit bulk entry cancelled", message, success=True)
        except (OrbitNotFoundError, OrbitValidationError) as exc:
            response = orbit_status_card("Bulk cancellation failed", str(exc))
        return AdaptiveCardActionCardResponse(value=response)


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


def _action_data(ctx: Any) -> Mapping[str, object] | None:
    data = getattr(getattr(ctx.activity.value, "action", None), "data", None)
    return data if isinstance(data, Mapping) else None


def _text_field(data: Mapping[str, object], key: str) -> str:
    value = data.get(key)
    return value if isinstance(value, str) else ""


class _BackgroundCardHandler:
    """Runs one background job per card submission and replies in the chat."""

    def __init__(self) -> None:
        self._started: OrderedDict[str, None] = OrderedDict()
        self._background_tasks: set[asyncio.Task[None]] = set()

    def _launch_once(self, operation_id: str, job: Coroutine[Any, Any, None]) -> None:
        if operation_id in self._started:
            job.close()
            return
        self._started[operation_id] = None
        while len(self._started) > 256:
            self._started.popitem(last=False)
        task = asyncio.create_task(job)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    @staticmethod
    async def _send(
        ctx: Any, build: Callable[[], Awaitable[AdaptiveCard]], error_title: str
    ) -> None:
        try:
            result = await build()
        except Exception as exc:
            if not isinstance(
                exc,
                (
                    OrbitAmbiguousMatchError,
                    OrbitAuthenticationError,
                    OrbitAuthRequiredError,
                    OrbitMutationUncertainError,
                    OrbitNotFoundError,
                    OrbitProviderError,
                    OrbitValidationError,
                ),
            ):
                logger.exception("Unexpected error while handling an Orbit entry change")
            result = _orbit_change_error_card(error_title, exc)
        try:
            await ctx.send(result)
        except Exception:
            logger.exception("Failed to send an Orbit entry change message")


class OrbitEditStartCardHandler(_BackgroundCardHandler):
    def __init__(self, modify_service: OrbitModifyEntryService) -> None:
        super().__init__()
        self._modify = modify_service

    async def __call__(self, ctx: Any) -> AdaptiveCardActionCardResponse:
        data = _action_data(ctx)
        entry_id = data.get("entry_id") if data else None
        if not isinstance(entry_id, str) or not entry_id:
            return AdaptiveCardActionCardResponse(
                value=orbit_status_card("Edit unavailable", "This entry reference is invalid.")
            )
        user_id = teams_user_id(ctx)

        async def build() -> AdaptiveCard:
            return orbit_edit_form_card(await self._modify.start_edit(user_id, entry_id))

        self._launch_once(
            f"{OrbitLoginCardHandler._submission_id(ctx)}:{entry_id}",
            self._send(ctx, build, "Edit unavailable"),
        )
        return AdaptiveCardActionCardResponse(
            value=orbit_status_card(
                "Opening edit form",
                "PULSE is loading the entry and will post the edit form here.",
                success=True,
            )
        )


class OrbitEditSubmitCardHandler(_BackgroundCardHandler):
    def __init__(self, modify_service: OrbitModifyEntryService) -> None:
        super().__init__()
        self._modify = modify_service

    async def __call__(self, ctx: Any) -> AdaptiveCardActionCardResponse:
        data = _action_data(ctx)
        entry_id = data.get("entry_id") if data else None
        if data is None or not isinstance(entry_id, str) or not entry_id:
            return AdaptiveCardActionCardResponse(
                value=orbit_status_card("Edit failed", "This edit form is invalid.")
            )
        user_id = teams_user_id(ctx)

        async def build() -> AdaptiveCard:
            change = await self._modify.prepare_edit(
                user_id,
                entry_id,
                entry_date_text=_text_field(data, "edit_date"),
                duration_text=_text_field(data, "edit_duration"),
                description=_text_field(data, "edit_notes"),
                project_id=_text_field(data, "edit_project"),
                task_name=_text_field(data, "edit_task"),
            )
            return orbit_update_confirmation_card(change)

        self._launch_once(
            f"{OrbitLoginCardHandler._submission_id(ctx)}:{entry_id}",
            self._send(ctx, build, "Edit not prepared"),
        )
        return AdaptiveCardActionCardResponse(
            value=orbit_status_card(
                "Reviewing changes",
                "PULSE is checking your changes and will post a confirmation card here.",
                success=True,
            )
        )


class OrbitDeleteStartCardHandler(_BackgroundCardHandler):
    def __init__(self, modify_service: OrbitModifyEntryService) -> None:
        super().__init__()
        self._modify = modify_service

    async def __call__(self, ctx: Any) -> AdaptiveCardActionCardResponse:
        data = _action_data(ctx)
        entry_id = data.get("entry_id") if data else None
        if not isinstance(entry_id, str) or not entry_id:
            return AdaptiveCardActionCardResponse(
                value=orbit_status_card("Delete unavailable", "This entry reference is invalid.")
            )
        user_id = teams_user_id(ctx)

        async def build() -> AdaptiveCard:
            return orbit_delete_confirmation_card(
                await self._modify.prepare_delete(user_id, entry_id)
            )

        self._launch_once(
            f"{OrbitLoginCardHandler._submission_id(ctx)}:{entry_id}",
            self._send(ctx, build, "Delete not prepared"),
        )
        return AdaptiveCardActionCardResponse(
            value=orbit_status_card(
                "Preparing deletion",
                "PULSE is loading the entry and will post a confirmation card here.",
                success=True,
            )
        )


class OrbitChangeConfirmCardHandler(_BackgroundCardHandler):
    def __init__(self, modify_service: OrbitModifyEntryService) -> None:
        super().__init__()
        self._modify = modify_service

    async def __call__(self, ctx: Any) -> AdaptiveCardActionCardResponse:
        data = _action_data(ctx)
        change_id = data.get("change_id") if data else None
        if not isinstance(change_id, str) or not change_id:
            return AdaptiveCardActionCardResponse(
                value=orbit_status_card(
                    "Confirmation failed", "This confirmation card is invalid."
                )
            )
        user_id = teams_user_id(ctx)

        async def build() -> AdaptiveCard:
            return orbit_change_result_card(await self._modify.confirm(user_id, change_id))

        self._launch_once(
            f"{user_id}:{change_id}",
            self._send(ctx, build, "Orbit entry not changed"),
        )
        return AdaptiveCardActionCardResponse(
            value=orbit_status_card(
                "Applying Orbit change",
                "Your confirmed change is being sent. PULSE will post the result here.",
                success=True,
            )
        )


class OrbitEntrySelectCardHandler(_BackgroundCardHandler):
    def __init__(self, modify_service: OrbitModifyEntryService) -> None:
        super().__init__()
        self._modify = modify_service

    async def __call__(self, ctx: Any) -> AdaptiveCardActionCardResponse:
        data = _action_data(ctx)
        selection_id = data.get("selection_id") if data else None
        entry_id = data.get("entry_id") if data else None
        if (
            not isinstance(selection_id, str)
            or not selection_id
            or not isinstance(entry_id, str)
            or not entry_id
        ):
            return AdaptiveCardActionCardResponse(
                value=orbit_status_card("Selection failed", "This selection card is invalid.")
            )
        user_id = teams_user_id(ctx)

        async def build() -> AdaptiveCard:
            return orbit_change_confirmation_card(
                await self._modify.select_entry(user_id, selection_id, entry_id)
            )

        self._launch_once(
            f"{user_id}:{selection_id}",
            self._send(ctx, build, "Entry not selected"),
        )
        return AdaptiveCardActionCardResponse(
            value=orbit_status_card(
                "Preparing change",
                "PULSE is checking the entry and will post a confirmation card here.",
                success=True,
            )
        )


class OrbitChangeCancelCardHandler:
    def __init__(self, modify_service: OrbitModifyEntryService) -> None:
        self._modify = modify_service

    async def __call__(self, ctx: Any) -> AdaptiveCardActionCardResponse:
        data = _action_data(ctx)
        change_id = data.get("change_id") if data else None
        if not isinstance(change_id, str) or not change_id:
            return AdaptiveCardActionCardResponse(
                value=orbit_status_card(
                    "Cancellation failed", "This confirmation card is invalid."
                )
            )
        try:
            await self._modify.cancel(teams_user_id(ctx), change_id)
        except OrbitNotFoundError:
            pass  # Already cancelled, applied, or replaced; the outcome is the same.
        return AdaptiveCardActionCardResponse(
            value=orbit_status_card(
                "Change cancelled",
                "No change was sent to Orbit.",
                success=True,
            )
        )


class TeamsMessageHandler:
    def __init__(
        self,
        chat_service: ChatService,
        orbit_auth_service: OrbitAuthService | None = None,
        orbit_add_service: OrbitAddEntryService | None = None,
        orbit_view_service: OrbitViewEntriesService | None = None,
        orbit_bulk_service: OrbitBulkEntryService | None = None,
        orbit_modify_service: OrbitModifyEntryService | None = None,
    ) -> None:
        self._chat_service = chat_service
        self._orbit_auth = orbit_auth_service
        self._orbit_add = orbit_add_service
        self._orbit_view = orbit_view_service
        self._orbit_bulk = orbit_bulk_service
        self._orbit_modify = orbit_modify_service
        self._seen_users: OrderedDict[str, None] = OrderedDict()
        self._processed_message_ids: OrderedDict[str, None] = OrderedDict()

    async def __call__(self, ctx: Any) -> None:
        if not self._claim_message(ctx):
            return
        user_text = clean_teams_text(getattr(ctx.activity, "text", None))
        try:
            if PASSWORD_VALUE_PATTERN.search(user_text):
                response = (
                    "For your security, do not send passwords as a Teams message. "
                    "Delete that message, change the exposed password, then send "
                    "`orbit login` and use the masked sign-in card."
                )
            elif self._is_greeting(user_text):
                self._remember_user(teams_user_id(ctx))
                response = pulse_introduction_card()
            elif self._is_orbit_command(user_text):
                self._remember_user(teams_user_id(ctx))
                response = await self._handle_orbit(ctx, user_text)
            elif self._orbit_add and await self._orbit_add.has_project_selection(
                teams_user_id(ctx)
            ):
                self._remember_user(teams_user_id(ctx))
                pending = await self._orbit_add.select_project(
                    teams_user_id(ctx), project_name=user_text
                )
                response = orbit_confirmation_card(pending)
            elif self._orbit_auth and self._is_first_message(teams_user_id(ctx)):
                if await self._orbit_auth.is_authenticated(teams_user_id(ctx)):
                    response = time_entry_help_messages(newly_connected=False)
                else:
                    response = orbit_login_card()
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
            for message in response if isinstance(response, list) else [response]:
                await ctx.send(message)

    @staticmethod
    def _is_orbit_command(text: str) -> bool:
        command = text.strip().casefold()
        return (
            command.startswith("orbit ")
            or command in {"orbit", "confirm", "cancel"}
            or TeamsMessageHandler._is_orbit_login_intent(command)
            or TeamsMessageHandler._is_entry_change_intent(command)
            or TeamsMessageHandler._is_time_entry_view_intent(command)
            or TeamsMessageHandler._is_time_entry_intent(command)
            or TeamsMessageHandler._is_time_entry_start_intent(command)
        )

    @staticmethod
    def _is_greeting(text: str) -> bool:
        words = re.findall(r"[a-z0-9]+", text.casefold())
        return bool(words and words[0] in {"hey", "hi", "hello"})

    def _is_first_message(self, current_user_id: str) -> bool:
        if current_user_id in self._seen_users:
            return False
        self._remember_user(current_user_id)
        return True

    def _remember_user(self, current_user_id: str) -> None:
        self._seen_users[current_user_id] = None
        self._seen_users.move_to_end(current_user_id)
        while len(self._seen_users) > 1024:
            self._seen_users.popitem(last=False)

    def _claim_message(self, ctx: Any) -> bool:
        activity_id = getattr(ctx.activity, "id", None)
        if not isinstance(activity_id, str) or not activity_id:
            return True
        if activity_id in self._processed_message_ids:
            return False
        self._processed_message_ids[activity_id] = None
        self._processed_message_ids.move_to_end(activity_id)
        while len(self._processed_message_ids) > 2048:
            self._processed_message_ids.popitem(last=False)
        return True

    @staticmethod
    def _is_orbit_login_intent(text: str) -> bool:
        normalized = re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()
        if "orbit" not in normalized.split():
            return False
        return any(
            phrase in normalized for phrase in ("login", "log in", "signin", "sign in", "connect")
        )

    @staticmethod
    def _is_time_entry_intent(text: str) -> bool:
        words = set(re.findall(r"[a-z0-9]+", text.casefold()))
        return bool(words & TIME_ENTRY_ACTION_WORDS and words & TIME_ENTRY_DETAIL_WORDS)

    @staticmethod
    def _is_bulk_time_entry_intent(text: str) -> bool:
        words = set(re.findall(r"[a-z0-9]+", text.casefold()))
        has_action = bool(words & TIME_ENTRY_ACTION_WORDS)
        explicit_bulk = bool(words & {"bulk", "entries"})
        bullet_count = len(re.findall(r"(?m)^\s*(?:[-*]|\d+[.)])\s+", text))
        return has_action and (explicit_bulk or bullet_count >= 2)

    @staticmethod
    def _is_entry_change_intent(text: str) -> bool:
        words = set(re.findall(r"[a-z0-9]+", text.casefold()))
        return bool(
            words & ENTRY_CHANGE_WORDS
            and words & ENTRY_CHANGE_TARGETS
            and not words & TIME_ENTRY_ACTION_WORDS
        )

    @staticmethod
    def _is_time_entry_start_intent(text: str) -> bool:
        words = set(re.findall(r"[a-z0-9]+", text.casefold()))
        return bool(words & {"entry", "timesheet"} and words & TIME_ENTRY_START_WORDS)

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

    async def _handle_orbit(
        self, ctx: Any, text: str
    ) -> str | AdaptiveCard | list[AdaptiveCard | ComposeMessageActivity]:
        if not self._orbit_auth:
            return "Orbit integration is not configured yet."
        current_teams_user_id = teams_user_id(ctx)
        command = text.strip()
        normalized = command.casefold()

        try:
            if self._is_orbit_login_intent(normalized):
                if await self._orbit_auth.is_authenticated(current_teams_user_id):
                    return time_entry_help_messages(newly_connected=False)
                return orbit_login_card()
            if normalized == "orbit logout":
                if self._orbit_add:
                    await self._orbit_add.cancel(current_teams_user_id)
                if self._orbit_bulk:
                    await self._orbit_bulk.cancel(current_teams_user_id)
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
                if not self._orbit_add and not self._orbit_bulk:
                    return "Orbit entry integration is not configured yet."
                cancelled = False
                if self._orbit_add:
                    cancelled = await self._orbit_add.cancel(current_teams_user_id)
                if self._orbit_bulk:
                    cancelled = (
                        await self._orbit_bulk.cancel(current_teams_user_id) or cancelled
                    )
                return (
                    "Pending Orbit entry cancelled."
                    if cancelled
                    else "There is no pending Orbit entry to cancel."
                )
            if normalized.startswith(ENTRY_CHANGE_PREFIXES) or self._is_entry_change_intent(
                normalized
            ):
                if not self._orbit_modify:
                    return "Orbit entry editing is not configured yet."
                request = command[len("orbit ") :].strip() if normalized.startswith(
                    "orbit "
                ) else command
                prepared = await self._orbit_modify.prepare_from_text(
                    current_teams_user_id, request
                )
                if isinstance(prepared, EntrySelectionRequest):
                    return orbit_entry_selection_card(prepared)
                return orbit_change_confirmation_card(prepared)
            if normalized.startswith(
                ("orbit entries", "orbit list", "orbit timesheet")
            ) or self._is_time_entry_view_intent(normalized):
                if not self._orbit_view:
                    return "Orbit timesheet viewing is not configured yet."
                page = await self._orbit_view.list_from_text(current_teams_user_id, command)
                return orbit_timesheet_list_card(page)
            if normalized.startswith("orbit bulk") or self._is_bulk_time_entry_intent(
                command
            ):
                if not self._orbit_bulk:
                    return "Orbit bulk-entry integration is not configured yet."
                if self._orbit_add:
                    await self._orbit_add.cancel(current_teams_user_id)
                request = command
                if normalized.startswith("orbit bulk"):
                    request = command[len("orbit bulk") :].strip()
                elif normalized.startswith("orbit add"):
                    request = command[len("orbit add") :].strip()
                if not request:
                    raise OrbitValidationError(
                        "Provide at least two entries and a date or date range."
                    )
                prepared = await self._orbit_bulk.prepare(
                    current_teams_user_id, request
                )
                return orbit_bulk_preparation_card(prepared)
            if self._is_time_entry_start_intent(normalized) and not self._is_time_entry_intent(
                normalized
            ):
                if await self._orbit_auth.is_authenticated(current_teams_user_id):
                    return time_entry_help_messages(newly_connected=False)
                return orbit_login_card()
            if normalized.startswith("orbit add") or self._is_time_entry_intent(normalized):
                if not self._orbit_add:
                    return "Orbit add-entry integration is not configured yet."
                if self._orbit_bulk:
                    await self._orbit_bulk.cancel(current_teams_user_id)
                request = (
                    command[len("orbit add") :].strip()
                    if normalized.startswith("orbit add")
                    else command
                )
                if not request:
                    raise OrbitValidationError(
                        "Provide the entry details: project, task, date, duration, and notes."
                    )
                prepared = await self._orbit_add.prepare(current_teams_user_id, request)
                if isinstance(prepared, ProjectSelectionRequest):
                    return orbit_project_selection_card(prepared)
                return orbit_confirmation_card(prepared)
            return (
                "Orbit commands: `orbit login`, `orbit add <details>`, `orbit list`, "
                "`orbit edit <entry and change>`, `orbit delete <entry>`, "
                "`orbit confirm`, `orbit cancel`, and `orbit logout`."
            )
        except OrbitAuthRequiredError:
            return orbit_login_card(
                error="Your Orbit session is missing or expired. Sign in to continue."
            )
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
    orbit_bulk_service: OrbitBulkEntryService | None = None,
    orbit_modify_service: OrbitModifyEntryService | None = None,
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
            orbit_bulk_service,
            orbit_modify_service,
        )
    )
    if orbit_auth_service:
        teams_app.on_card_action_execute(
            ORBIT_OPEN_LOGIN_ACTION,
            OrbitOpenLoginCardHandler(orbit_auth_service),
        )
        teams_app.on_card_action_execute(
            ORBIT_LOGIN_ACTION,
            OrbitLoginCardHandler(orbit_auth_service),
        )
    if orbit_add_service:
        teams_app.on_card_action_execute(
            ORBIT_SELECT_PROJECT_ACTION,
            OrbitProjectSelectionCardHandler(orbit_add_service),
        )
        teams_app.on_card_action_execute(
            ORBIT_CONFIRM_ACTION,
            OrbitConfirmCardHandler(orbit_add_service),
        )
        teams_app.on_card_action_execute(
            ORBIT_CANCEL_ACTION,
            OrbitCancelCardHandler(orbit_add_service),
        )
    if orbit_bulk_service:
        bulk_selection_handler = OrbitBulkSelectionCardHandler(orbit_bulk_service)
        teams_app.on_card_action_execute(
            ORBIT_BULK_SCHEDULE_ACTION,
            bulk_selection_handler,
        )
        teams_app.on_card_action_execute(
            ORBIT_BULK_ENTITY_ACTION,
            bulk_selection_handler,
        )
        teams_app.on_card_action_execute(
            ORBIT_BULK_CONFIRM_ACTION,
            OrbitBulkConfirmCardHandler(orbit_bulk_service),
        )
        teams_app.on_card_action_execute(
            ORBIT_BULK_CANCEL_ACTION,
            OrbitBulkCancelCardHandler(orbit_bulk_service),
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
    if orbit_modify_service:
        teams_app.on_card_action_execute(
            ORBIT_EDIT_START_ACTION,
            OrbitEditStartCardHandler(orbit_modify_service),
        )
        teams_app.on_card_action_execute(
            ORBIT_EDIT_SUBMIT_ACTION,
            OrbitEditSubmitCardHandler(orbit_modify_service),
        )
        teams_app.on_card_action_execute(
            ORBIT_DELETE_START_ACTION,
            OrbitDeleteStartCardHandler(orbit_modify_service),
        )
        teams_app.on_card_action_execute(
            ORBIT_ENTRY_SELECT_ACTION,
            OrbitEntrySelectCardHandler(orbit_modify_service),
        )
        teams_app.on_card_action_execute(
            ORBIT_CHANGE_CONFIRM_ACTION,
            OrbitChangeConfirmCardHandler(orbit_modify_service),
        )
        teams_app.on_card_action_execute(
            ORBIT_CHANGE_CANCEL_ACTION,
            OrbitChangeCancelCardHandler(orbit_modify_service),
        )
    return teams_app
